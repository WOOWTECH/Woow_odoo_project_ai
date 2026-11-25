# -*- coding: utf-8 -*-
"""Turning a backend reply into a message posted by the AI in a task chat.

Two steps, so that slow downloads never happen while the channel row is
locked or inside a transaction that may be dropped:

1. ``_prepare_reply`` (no database writes): markdown -> safe HTML; files
   linked in the text or listed in ``attachments`` are downloaded (allowlisted
   hosts only, size-capped) or decoded (base64, and ``data:image`` URIs in the
   text, which need no allowlist), their mimetype sniffed.
2. ``_post_prepared``: create the ``ir.attachment`` records and post as
   superuser in the assistant's name (the only posts the task chats take
   as the AI).

Ported from woow_ai_contact 18.0.2.0.0 (``ai_contact_reply.py``).
"""

import logging
import re
import time

from markupsafe import Markup

from odoo import models

from ..tools import media
from ..tools.logs import log_unexpected
from ..tools.markdown import markdown_to_html
from .ai_assistant_backend import strip_think
from .discuss_channel import trusted

_logger = logging.getLogger(__name__)

MAX_FILES_PER_REPLY = 10
# one backend answer (all its replies, up to ai_assistant_backend.MAX_REPLIES),
# all its files together: the downloads end in time (inside the job's margin
# for downloads and posting, ai_job.WORKER_LIMIT_MARGIN_SECONDS) and stay
# bounded in memory, whatever the per-file limit
REPLY_DOWNLOAD_SECONDS = 45
MAX_REPLY_BYTES = 100 * 1024 * 1024

# ![alt](url) and [text](url) with an optional "title"
_MD_LINK_RE = re.compile(
    r'(?P<bang>!?)\[(?P<label>[^\]\n]{0,500})\]\((?P<url>https?://[^\s()<>]{1,2000})(?:\s+"[^"\n]*")?\)')

# data URIs in the text: ![alt](data:...) or bare (models that make images inline them)
_DATA_URI = r'data:(?P<type>[a-z0-9.+-]+/[a-z0-9.+-]+)(?:;[a-z0-9.+=-]+)*;base64,(?P<data>[A-Za-z0-9+/]+=*)'
_DATA_URI_RE = re.compile(
    r'(?:!?\[(?P<label>[^\]\n]{0,500})\]\(\s*' + _DATA_URI + r'\s*(?:"[^"\n]*")?\)|' + _DATA_URI.replace(
        '?P<type>', '?P<type2>').replace('?P<data>', '?P<data2>') + ')',
    re.IGNORECASE)
_DATA_IMAGE_TYPES = ('image/png', 'image/jpeg', 'image/jpg', 'image/gif', 'image/webp')
_IMAGE_EXTENSIONS = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/webp': 'webp'}


class WoowProjectAiAssistantReply(models.Model):
    _inherit = 'woow.project.ai.assistant'

    def _max_download_bytes(self):
        """Size limit of a file in a reply (the AI connection's *Max File Size*)."""
        self.ensure_one()
        return max(1, self.sudo().connection_id.max_download_mb or 20) * 1024 * 1024

    def _download_domains(self):
        """Hosts whose files in replies are attached (the AI connection's *Download Domains*)."""
        self.ensure_one()
        return media.parse_domains(self.sudo().connection_id.download_domains)

    @staticmethod
    def _reply_budget():
        """What all the files of one backend answer may take together: {'deadline', 'bytes'}."""
        return {'deadline': time.monotonic() + REPLY_DOWNLOAD_SECONDS, 'bytes': MAX_REPLY_BYTES}

    @staticmethod
    def _budget_bytes(budget, max_bytes):
        """The size limit of the next file of a reply (raises when nothing is left)."""
        if budget is None:
            return max_bytes
        if budget['bytes'] <= 0:
            raise media.DownloadRefused('the files of this reply are too large together')
        return min(max_bytes, budget['bytes'])

    def _fetch_file(self, url, name=None, budget=None, domains=None):
        """Download an allowlisted file -> {'name', 'raw', 'mimetype'}; raises
        DownloadRefused. ``budget`` (see _reply_budget): shared by the files of
        one reply, its bytes are counted down. ``domains``: the allowlist
        (default: the AI connection's download domains), redirects included."""
        self.ensure_one()
        max_bytes = self._budget_bytes(budget, self._max_download_bytes())
        allowed = domains if domains is not None else self._download_domains()
        raw, final_url = media.download(url, allowed, max_bytes,
                                        deadline=budget['deadline'] if budget else None)
        if budget is not None:
            budget['bytes'] -= len(raw)
        filename = media.clean_filename(name) if name else media.filename_from_url(final_url)
        return {'name': filename, 'raw': raw, 'mimetype': media.safe_mimetype(raw)}

    def _decode_file(self, data, max_bytes, budget=None):
        """A base64 file of a reply (counted in ``budget``); raises DownloadRefused."""
        raw = media.decode_base64(data, self._budget_bytes(budget, max_bytes))
        if budget is not None:
            budget['bytes'] -= len(raw)
        return raw

    def _reply_note(self, channel, source, message=None):
        """Short note put in the reply text, in the requester's language
        (``message``: the message being answered, see _woow_project_ai_requester)."""
        lang = None
        if channel:
            try:
                lang = channel._woow_project_ai_identity(self, message).get('lang')
            except Exception:  # noqa: BLE001 - a note is cosmetic
                lang = None
        env = self.with_context(lang=self._installed_lang(lang)).env
        notes = {
            'image': env._('(An image in the reply could not be attached.)'),
        }
        return notes[source]

    def _prepare_reply(self, reply, channel=None, message=None, budget=None):
        """{'text', 'attachments'} from a backend -> {'html': Markup, 'files': [...], 'warnings': [...]}

        Args:
            reply (dict): text (markdown) and attachments (n8n format)
            channel (discuss.channel): where it will be posted (link hosts and
                note language); None for the default rules
            message (mail.message): the message being answered (the language
                of the notes is its author's)
            budget (dict): what the files may take (_reply_budget), shared by
                every reply of one backend answer; a new one when omitted
        """
        self.ensure_one()
        text = strip_think(reply.get('text') or '')
        files = []
        warnings = []
        domains = self._download_domains()
        max_bytes = self._max_download_bytes()
        if budget is None:
            budget = self._reply_budget()
        link_hosts = channel._woow_project_ai_link_hosts(self) if channel else None
        if link_hosts is not None:
            # a consulted chat (records were sent to the AI): a URL the model wrote is downloaded only from a
            # host both the AI connection's download domains and the chat's link hosts allow; any other stays a
            # link (rendered as text unless its host is allowed). data: images and base64 files need no request
            domains = media.intersect_domains(domains, link_hosts)

        def replace_data_uri(match):
            declared = (match.group('type') or match.group('type2') or '').lower()
            data = match.group('data') or match.group('data2') or ''
            label = match.group('label')
            reason = 'not an image'
            if declared in _DATA_IMAGE_TYPES:
                if len(files) >= MAX_FILES_PER_REPLY:
                    reason = 'too many files'
                else:
                    try:
                        raw = self._decode_file(data, max_bytes, budget)
                    except media.DownloadRefused as e:
                        reason = str(e)
                    else:
                        mimetype = media.safe_mimetype(raw)
                        if mimetype in media.INLINE_IMAGE_TYPES:
                            ext = _IMAGE_EXTENSIONS[mimetype]
                            stem = media.clean_filename(label, default='') if label else ''
                            name = media.clean_filename(
                                '%s.%s' % (stem or 'image-%s' % (len(files) + 1), ext))
                            files.append({'name': name, 'raw': raw, 'mimetype': mimetype})
                            return name
                        reason = 'content is %s' % mimetype
            warnings.append('inline %s not attached (%s)' % (declared or 'data', reason))
            return self._reply_note(channel, 'image', message)

        # data:image needs no download, so no domain allowlist
        text = _DATA_URI_RE.sub(replace_data_uri, text)

        def replace_link(match):
            url = match.group('url')
            is_image = bool(match.group('bang'))
            if not (is_image or media.looks_like_file(url)):
                return match.group(0)
            if len(files) >= MAX_FILES_PER_REPLY or not media.url_allowed(url, domains):
                return match.group(0)
            try:
                file = self._fetch_file(url, budget=budget, domains=domains)
            except media.DownloadRefused as e:
                _logger.info("Project AI assistant %s: file not attached (%s)", self.id, e)
                return match.group(0)
            except Exception as e:  # noqa: BLE001 - a file never costs the reply: it stays a link
                # (the type and the code lines: the text of an error of a download may carry the file's URL)
                log_unexpected(_logger, "Project AI assistant %s: file not attached (unexpected error)", (self.id,),
                               e, level=logging.WARNING)
                return match.group(0)
            files.append(file)
            return file['name']

        text = _MD_LINK_RE.sub(replace_link, text)

        extra_links = []
        for item in reply.get('attachments') or []:
            if not isinstance(item, dict) or len(files) >= MAX_FILES_PER_REPLY:
                continue
            name = item.get('name') if isinstance(item.get('name'), str) else None
            if isinstance(item.get('data_base64'), str):
                try:
                    raw = self._decode_file(item['data_base64'], max_bytes, budget)
                except media.DownloadRefused as e:
                    _logger.info("Project AI assistant %s: base64 attachment dropped (%s)", self.id, e)
                    continue
                files.append({
                    'name': media.clean_filename(name, default='file'),
                    'raw': raw,
                    'mimetype': media.safe_mimetype(raw),
                })
            elif isinstance(item.get('url'), str) and item['url'].lower().startswith(('http://', 'https://')):
                url = item['url']
                try:
                    if not media.url_allowed(url, domains):
                        raise media.DownloadRefused('domain not allowed')
                    files.append(self._fetch_file(url, name=name, budget=budget, domains=domains))
                except Exception as e:  # noqa: BLE001 - DownloadRefused, or anything else: the file stays a link
                    if isinstance(e, media.DownloadRefused):
                        _logger.info("Project AI assistant %s: attachment kept as a link (%s)", self.id, e)
                    else:
                        log_unexpected(_logger, "Project AI assistant %s: attachment kept as a link (unexpected "
                                       "error)", (self.id,), e, level=logging.WARNING)
                    extra_links.append('[%s](%s)' % ((name or media.filename_from_url(url)).replace(']', ''), url))
        if extra_links:
            text = (text + '\n\n' + '\n'.join('- %s' % link for link in extra_links)).strip()
        try:
            html = markdown_to_html(text, link_hosts=link_hosts)
        except Exception as e:  # noqa: BLE001 - the renderer never loses a reply: escaped plain text
            log_unexpected(_logger, "Project AI assistant %s: reply shown as plain text (Markdown rendering "
                           "failed)", (self.id,), e, level=logging.WARNING)
            html = Markup('<p>%s</p>') % text
            warnings.append('reply shown as plain text')
        return {'html': html, 'files': files, 'warnings': warnings}

    def _post_prepared(self, channel, prepared):
        """Post a prepared reply in `channel` as the AI; returns the message (or
        empty). From a trusted context (the welcome text is posted in the
        request of whoever opens the tab, a push in the push route's): no
        client default_* key reaches the files or the message."""
        self.ensure_one()
        assistant = trusted(self.sudo())
        channel = trusted(channel.sudo())
        html = prepared.get('html') or Markup('')
        files = prepared.get('files') or []
        if not str(html).strip() and not files:
            return self.env['mail.message']
        attachments = assistant.env['ir.attachment'].create([{
            'name': file['name'],
            'raw': file['raw'],
            'mimetype': file['mimetype'],
            'res_model': 'discuss.channel',
            'res_id': channel.id,
        } for file in files]) if files else assistant.env['ir.attachment']
        for attachment, file in zip(attachments, files):
            if attachment.mimetype != file['mimetype']:
                # ir.attachment re-sniffs "application/octet-stream" from the
                # content (giving back text/html, image/svg+xml...): force it
                # without the content, which keeps the given type
                attachment.write({'mimetype': file['mimetype']})
        return channel.with_context(mail_create_nosubscribe=True).message_post(
            body=Markup(html),
            author_id=assistant.partner_id.id,
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            attachment_ids=attachments.ids,
        )

    def _post_replies(self, channel, prepared_list):
        messages = self.env['mail.message']
        for prepared in prepared_list:
            messages |= self._post_prepared(channel, prepared)
        return messages
