# -*- coding: utf-8 -*-
"""Backend protocols of a project AI assistant: LiteLLM (OpenAI compatible
chat completions) and n8n webhook.

Everything about how the backend is called (type, URL, key, model, timeouts,
retries, streaming, documents, content logging) is read from the assistant's
AI connection (``assistant.connection_id``, sudo). See docs/PROTOCOL.md for
the wire format. No transaction is open during the HTTP call: the caller
passes ``before_call`` (the job commits there).

The n8n body also carries the n8n Chat Trigger fields (``action``,
``sessionId``, ``chatInput``), the answer may be the AI Agent's ``output``,
the signing secret is optional (empty: nothing is signed, e.g. for a public
Chat Trigger) and an answer waits at most 300 s: the conventions of the
cross-product contract, which the Discuss AI module (woow_ai_contact) follows
too, so one workflow can serve both; nothing is shared with it. The
``events`` key is always there, empty (task chats have no events).

Contract v1.3: every request carries the current time (n8n ``now``, a LiteLLM
system line: _current_time); a speaker label a model copied to the start of
its answer is removed before the answer is posted (strip_labels); a LiteLLM
turn whose file the model cannot read is sent again without it, and the
conversation remembers that file (_call_backend).

Ported from woow_ai_contact 18.0.2.0.0 (``ai_contact_backend.py``), with its
two backend types only (n8n and LiteLLM) and no conversation events.
"""

import base64
import hashlib
import hmac
import json
import logging
import re
import socket
import threading
import time
import unicodedata
from datetime import datetime, timezone

import pytz
import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError, ReadTimeoutError

from odoo import api, models

from ..tools import media
from ..tools.logs import log_unexpected  # noqa: F401 - also imported from here by older code
from ..tools.record_text import clean_data_text
from .discuss_channel import one_line_name, trusted

_logger = logging.getLogger(__name__)


INLINE_IMAGE_MAX_BYTES = 5 * 1024 * 1024
INLINE_IMAGE_MESSAGES = 3  # the most recent user messages whose images may be inlined
INLINE_IMAGE_MAX_COUNT = 4  # images inlined per request
INLINE_IMAGE_BUDGET = 7 * 1024 * 1024  # data URI characters per request (proxies often cap requests at 10 MB)
# User documents (document_mode, docs/PROTOCOL.md "Documents"): sent as they
# are as OpenAI file parts, sharing INLINE_IMAGE_BUDGET with the images
DOC_MESSAGES = 5  # the most recent user messages whose documents are sent
DOC_MAX_COUNT = 5  # documents sent per request
DOC_FILE_MAX_BYTES = 4 * 1024 * 1024  # a larger document is a link only
CONNECT_TIMEOUT = 10
STREAM_IDLE_TIMEOUT = 30  # seconds without any byte from a streamed answer
RETRY_WAITS = (3, 10)  # seconds before the 1st / 2nd retry
RETRY_HTTP_STATUSES = (429, 503)
N8N_MAX_TIMEOUT = 300  # seconds an n8n answer may wait (a long workflow: an in-cluster URL, or {"skip": true} and a push)
TEST_TIMEOUT = 15
MAX_LOG_PAYLOAD = 200000  # characters
MAX_ERROR_LENGTH = 300
MAX_REPLIES = 10
MAX_HANDOFF_REASON = 300  # characters of an n8n "handoff_reason" kept
METADATA_MAX_KEYS = 16
METADATA_MAX_VALUE = 512
SSE_CHUNK_SIZE = 1024
EARLIER_ASSISTANT_PREFIX = '[Messages you (the assistant) sent before this user message]\n'
# The model (or the route LiteLLM picks) cannot read a file (contract 3.7 "Fallback when the model cannot read a
# file"): a LiteLLM request with file parts that gets one of these statuses is sent again at once, first without
# the file parts of earlier messages, then without any; a file left out becomes its [附件] line and this note
# (literal protocol text, never translated)
FALLBACK_HTTP_STATUSES = (400, 413, 415, 422, 500)
FALLBACK_MIN_SECONDS = 1.0  # a fallback request is never started with less time left before the answer's deadline
FALLBACK_NOTE = '模型無法讀取這個檔案，只附下載連結。'
# The current time of a request (contract 3.6, 3.7): English day names, whatever the server's locale, and the one
# system line of LiteLLM (literal protocol text)
WEEKDAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
TIME_LINE = 'Current time: %s %s (%s), time zone %s (UTC%s).'
# Labels in answers (contract 3.14): the kinds of speaker label every chat removes from the start of an answer (a
# chat whose own labels use another kind adds it: discuss.channel._woow_project_ai_label_kinds). A label is
# "[<kind>]" or "[<kind> <text without ] or a line break>]", the kind followed by "]", a space or U+3000, so
# "[客戶服務中心]" is no label
LABEL_KINDS = ('真人客服', '客戶', '負責同事')
# the white space around labels besides what str.isspace() counts (U+3000 and the no-break spaces included): the
# zero-width U+200B and U+FEFF
_LABEL_ZERO_WIDTH = frozenset('\u200b\ufeff')
MAX_LABEL_NAME = 80  # characters of a name-only label's name (one_line_name)
MAX_CLEAN_PASSES = 8  # strip_think passes of clean_answer
_kind_label_res = {}  # kinds -> compiled "[<kind>]" / "[<kind> <text>]" at a position

_THINK_TAG_RE = re.compile(r'<(think|thinking)>', re.IGNORECASE)
_THINK_END_RE = {name: re.compile(r'</%s\s*>' % name, re.IGNORECASE) for name in ('think', 'thinking')}
_THINK_OPEN_RE = re.compile(r'<(?:think|thinking)>.*$', re.DOTALL | re.IGNORECASE)
# reasoning without its opening tag: "...</think>answer"
_THINK_CLOSE_RE = re.compile(r'^.*?</(?:think|thinking)\s*>', re.DOTALL | re.IGNORECASE)
_SPECIAL_TOKEN_NAME = frozenset('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_')
_DATA_URI_RE = re.compile(r'(data:[a-z0-9.+-]+/[a-z0-9.+-]+(?:;[a-z0-9.+=-]+)*;base64),([A-Za-z0-9+/]+=*)',
                          re.IGNORECASE)


class BackendError(Exception):
    """A backend call failed; ``status`` is a woow.project.ai.call.log status."""

    def __init__(self, status, message, http_status=None):
        super().__init__(message)
        self.status = status
        self.http_status = http_status


class GateClosed(BackendError):
    """A retry was not sent: the run-time gate of contract 3.5 closed during the wait (_http_post)."""


def _drop_think_blocks(text):
    """Remove every closed ``<think>...</think>`` (or ``thinking``) block:
    from an opening tag to the first closing tag of the same name (white
    space allowed before its ``>``, any case). Linear in the length of the
    text: a regex with a lazy ``.*?`` between the tags rescans the rest of the
    text from every opening tag that has no closing tag, and the text is the
    backend's."""
    parts, position, search, unclosed = [], 0, 0, set()
    while True:
        opening = _THINK_TAG_RE.search(text, search)
        if not opening:
            break
        name = opening.group(1).lower()
        closing = None if name in unclosed else _THINK_END_RE[name].search(text, opening.end())
        if not closing:
            # no closing tag after this opening tag, so none after a later one either
            unclosed.add(name)
            search = opening.start() + 1
            continue
        parts.append(text[position:opening.start()])
        position = search = closing.end()
    parts.append(text[position:])
    return ''.join(parts)


def _drop_special_tokens(text):
    """Remove special tokens such as ``<|im_end|>`` (``<|``, 1 to 40 ASCII
    letters, digits or ``_``, ``|>``), also those that removing an inner one
    forms (``<|<|x|>x|>``), however deep: one pass, each ``|>`` is checked
    against what was kept so far. Linear in the length of the text."""
    if '|>' not in text:
        return text
    kept = []
    for char in text:
        kept.append(char)
        if char != '>' or len(kept) < 5 or kept[-2] != '|':
            continue
        name_end = len(kept) - 3  # the last character of the name
        start = name_end
        while start >= 0 and name_end - start < 40 and kept[start] in _SPECIAL_TOKEN_NAME:
            start -= 1
        if 1 <= start < name_end and kept[start] == '|' and kept[start - 1] == '<':
            del kept[start - 1:]
    return ''.join(kept)


def strip_think(text):
    """Remove reasoning blocks (<think>, <thinking>, also unclosed) and special tokens."""
    if not text:
        return ''
    text = _drop_special_tokens(text)
    text = _drop_think_blocks(text)
    text = _THINK_OPEN_RE.sub('', text)
    text = _THINK_CLOSE_RE.sub('', text)
    return text.strip()


def _kind_label_re(kinds):
    """The compiled label of `kinds` where it starts: ``[<kind>]`` or ``[<kind> <text without ] or a line break>]``."""
    pattern = _kind_label_res.get(kinds)
    if pattern is None:
        pattern = _kind_label_res[kinds] = re.compile(r'\[(?:%s)(?:[ \u3000][^\]\r\n]*)?\]' % '|'.join(
            re.escape(kind) for kind in kinds))
    return pattern


def _skip_label_space(text, position):
    """The first position from `position` on that is not white space around
    labels (contract 3.14: what str.isspace() counts, and U+200B, U+FEFF)."""
    end = len(text)
    while position < end and (text[position].isspace() or text[position] in _LABEL_ZERO_WIDTH):
        position += 1
    return position


def _name_label_end(text, position, labels):
    """The end of a name-only label ``[<name>]`` of `labels` at `position`, or None."""
    if not labels or not text.startswith('[', position):
        return None
    close = text.find(']', position + 1, position + MAX_LABEL_NAME + 2)
    if close < 0 or text[position:close + 1] not in labels:
        return None
    return close + 1


def _later_line_has_name_label(text, labels):
    """True when a line after the first one that is not blank starts (after
    white space) with one of the name-only `labels`, not as a link text: the
    answer is then a list of who does what (contract 3.14)."""
    seen_text = False
    for line in text.splitlines():
        start = _skip_label_space(line, 0)
        if start == len(line):
            continue  # a blank line
        if seen_text:
            end = _name_label_end(line, start, labels)
            if end is not None and not line.startswith('(', end):
                return True
        seen_text = True
    return False


def strip_labels(text, names=(), kinds=()):
    """An answer without the speaker labels a model copied to its start
    (contract 3.14 "Labels in answers"), applied after strip_think: while the
    text starts, after white space (what str.isspace() counts, and U+200B,
    U+FEFF), with a label ``[<kind>]`` or ``[<kind> <text>]`` (a kind of
    LABEL_KINDS, or of `kinds`: the other kinds of the chat's own labels,
    followed by ``]``, a space or U+3000; text without ``]`` or a line break),
    that label and the white space after it are removed. With `names` (the
    labels of the people of a chat whose labels carry the name only: the
    internal chat), a leading ``[<name>]`` of one of them goes too, unless a
    later line of the answer also starts with one of them (a list of who does
    what stays as it is). A label directly followed by ``(`` is the text of a
    Markdown link (``[客戶](https://…)``) and stays. Nothing else changes: a
    bracket that is no such label, or a label after the start, stays. Pure;
    linear in the length of the text."""
    if not text:
        return ''
    pattern = _kind_label_re(LABEL_KINDS + tuple(kind for kind in kinds or () if kind not in LABEL_KINDS))
    labels = {'[%s]' % name for name in names or () if name}
    if labels and _later_line_has_name_label(text, labels):
        labels = set()
    position = 0
    while True:
        start = _skip_label_space(text, position)
        match = pattern.match(text, start)
        end = match.end() if match else _name_label_end(text, start, labels)
        if end is None or text.startswith('(', end):
            return text[position:]
        position = _skip_label_space(text, end)


def clean_answer(text, names=(), kinds=()):
    """A text the AI posts, as it is posted (contract 3.14): strip_think until
    it changes nothing more (removing a reasoning block can join the halves of
    a special token; at most MAX_CLEAN_PASSES passes), then the labels at its
    start (strip_labels). strip_think leaves the result as it is, so no later
    step (_prepare_reply) brings a label back to the start."""
    text = strip_think(text)
    for _pass in range(MAX_CLEAN_PASSES):
        again = strip_think(text)
        if again == text:
            break
        text = again
    return strip_labels(text, names, kinds)


def _utcnow():
    """Now, in UTC (aware): the one clock of the current time a request
    carries (contract 3.7; tests freeze it here)."""
    return datetime.now(timezone.utc)


def time_zone(name):
    """The pytz time zone of an IANA name, or None when `name` is not one
    (pytz, as Odoo checks a time zone)."""
    if not isinstance(name, str) or not name:
        return None
    try:
        return pytz.timezone(name)
    except Exception:  # noqa: BLE001 - pytz.UnknownTimeZoneError, or a value pytz cannot read: no time zone
        return None


def utc_offset(moment):
    """The UTC offset of an aware datetime: ``+08:00``, ``-05:00``, ``+05:45``."""
    minutes = round(moment.utcoffset().total_seconds() / 60)
    return '%s%02d:%02d' % ('-' if minutes < 0 else '+', abs(minutes) // 60, abs(minutes) % 60)


def time_line(now):
    """The current-time line of the LiteLLM system message (contract 3.7), from
    the request's ``now`` (_current_time), so that both renderings are one
    instant: ``Current time: YYYY-MM-DD HH:MM (<weekday>), time zone <tz> (UTC±HH:MM).``"""
    local = now['local']  # YYYY-MM-DDTHH:MM:SS±HH:MM
    return TIME_LINE % (local[:10], local[11:16], now['weekday'], now['tz'], local[19:])


def current_position(history, trigger_message=None):
    """History position of the message being answered: the trigger message's
    entry, else the last user entry (None without one)."""
    if trigger_message:
        for position, entry in enumerate(history):
            if entry.get('message_id') == trigger_message.id:
                return position
    return max((position for position, entry in enumerate(history) if entry['role'] == 'user'), default=None)


def _new_log():
    """What one request of an answer writes in its call log row."""
    return {'status': 'error', 'http_status': None, 'error': None, 'request': None, 'response': None,
            'usage': None}


def redact(data):
    """Copy of a request / response payload without inline file data
    (data: URIs anywhere in strings, n8n ``data_base64``): only lengths stay."""
    if isinstance(data, str):
        return _DATA_URI_RE.sub(lambda m: '%s,<%d base64 characters omitted>' % (m.group(1), len(m.group(2))), data)
    if isinstance(data, dict):
        return {key: ('<%d base64 characters omitted>' % len(value)
                      if key == 'data_base64' and isinstance(value, str) else redact(value))
                for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [redact(value) for value in data]
    return data


def _turn_text(turn):
    content = turn['content']
    if isinstance(content, list):
        return '\n'.join(part.get('text') or '' for part in content if isinstance(part, dict))
    return content or ''


def normalize_turns(turns):
    """OpenAI chat turns (no system message) that start and end with a user turn.

    Agent backends treat the last item as the new input, and Anthropic-style
    models would continue a trailing assistant turn or reject a leading one:

    - assistant turns before the first user turn (the welcome message) are dropped;
    - assistant turns after the last user turn (push, refusal or error
      messages) are moved just before it;
    - if that leaves assistant turns first (only one user turn), they become one
      user turn quoting them, so their content is not lost.
    """
    turns = list(turns)
    while turns and turns[0]['role'] != 'user':
        turns.pop(0)
    last_user = max((i for i, turn in enumerate(turns) if turn['role'] == 'user'), default=None)
    if last_user is None:
        return turns
    if last_user < len(turns) - 1:
        turns = turns[:last_user] + turns[last_user + 1:] + [turns[last_user]]
    lead = 0
    while turns[lead]['role'] != 'user':
        lead += 1
    if lead:
        quoted = '\n\n'.join(_turn_text(turn) for turn in turns[:lead])
        turns = [{'role': 'user', 'content': EARLIER_ASSISTANT_PREFIX + quoted}] + turns[lead:]
    return turns


def _json_dumps(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':'), default=str)


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace('+00:00', 'Z')


def _signature_headers(secret, body, timestamp=None):
    """n8n request signature: hex HMAC-SHA256(secret, "<timestamp>.<body bytes>").
    The secret itself is never sent."""
    timestamp = str(int(timestamp if timestamp is not None else time.time()))
    signature = hmac.new(secret.encode(), timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
    return {'X-Woow-Timestamp': timestamp, 'X-Woow-Signature': 'sha256=%s' % signature}


def _is_read_timeout(error):
    """requests turns a read timeout while streaming into a ConnectionError."""
    if isinstance(error, requests.ReadTimeout):
        return True
    return any(isinstance(arg, ReadTimeoutError) for arg in getattr(error, 'args', ()))


def _request_not_sent(error):
    """True when a requests.ConnectionError happened before the request was sent.

    Only a connect timeout or a connection that could not be opened (refused,
    unknown host: urllib3 NewConnectionError, NameResolutionError is a
    subclass) qualify. requests raises ConnectionError too when the backend
    received the whole request and then reset or closed the connection
    (ProtocolError "Connection aborted", RemoteDisconnected): the backend may
    be working on the answer then, so that is never retried. TLS and
    proxy errors are not retried either."""
    if isinstance(error, requests.ConnectTimeout):
        return True
    if isinstance(error, (requests.exceptions.SSLError, requests.exceptions.ProxyError)):
        return False
    reason = error.args[0] if getattr(error, 'args', None) else None
    if isinstance(reason, MaxRetryError):
        reason = reason.reason
    return isinstance(reason, NewConnectionError)


class _StreamWatchdog:
    """Cuts a streamed response at the total deadline.

    The reading thread may be blocked inside one read for a long time (see
    _sse_lines); a timer shuts the socket down at the deadline, which makes
    that read return, and ``fired`` tells the reader it was a timeout."""

    def __init__(self, response, deadline):
        self.response = response
        self.deadline = deadline
        self.fired = False
        self._timer = None

    def __enter__(self):
        self._timer = threading.Timer(max(0.0, self.deadline - time.time()), self._fire)
        self._timer.daemon = True
        self._timer.start()
        return self

    def __exit__(self, *exc):
        self._timer.cancel()
        return False

    def _socket(self):
        raw = getattr(self.response, 'raw', None)
        conn = getattr(raw, '_connection', None) or getattr(raw, 'connection', None)
        sock = getattr(conn, 'sock', None)
        if sock is None:
            fp = getattr(getattr(raw, '_fp', None), 'fp', None)
            sock = getattr(getattr(fp, 'raw', None), '_sock', None)
        return sock if isinstance(sock, socket.socket) else None

    def _fire(self):
        self.fired = True
        try:
            sock = self._socket()
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)  # wakes a blocked recv()
            else:
                self.response.close()
        except Exception as error:  # noqa: BLE001 - best effort; the reader still sees `fired`
            log_unexpected(_logger, "could not cut a streamed answer at its deadline", (), error, logging.DEBUG)


def attachment_raw(attachment):
    """Content of one attachment. ``raw`` is computed: read through the history's
    shared prefetch set it would load the files of every attachment in the
    context, so it is read with a prefetch set of this record only."""
    return attachment.with_prefetch(attachment._ids).raw or b''


def _int_or_zero(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def usage_tokens(usage):
    """(prompt, completion, total) tokens of an OpenAI ``usage`` object; also
    accepts ``input_tokens`` / ``output_tokens``; total = sum when missing."""
    if not isinstance(usage, dict):
        return 0, 0, 0
    prompt = _int_or_zero(usage.get('prompt_tokens')) or _int_or_zero(usage.get('input_tokens'))
    completion = _int_or_zero(usage.get('completion_tokens')) or _int_or_zero(usage.get('output_tokens'))
    total = _int_or_zero(usage.get('total_tokens')) or prompt + completion
    return prompt, completion, total


def _pick_usage(current, new):
    """Usage of a stream: the last chunk carrying one, unless it is empty and an
    earlier chunk had real counts (some servers repeat ``usage`` with zeros)."""
    if not isinstance(new, dict):
        return current
    if current and any(usage_tokens(current)) and not any(usage_tokens(new)):
        return current
    return new


class WoowProjectAiAssistantBackend(models.Model):
    _inherit = 'woow.project.ai.assistant'

    # --- Context ---

    @api.model
    def _session_id(self, channel):
        """Stable id of one conversation: ``woow-project-ai-<db uuid 8 hex>-<channel id>``."""
        uuid = self.env['ir.config_parameter'].sudo().get_param('database.uuid') or ''
        return 'woow-project-ai-%s-%s' % (uuid.replace('-', '')[:8], channel.id)

    @api.model
    def _identity(self, partner):
        """Basic identity of the person the AI talks to (no login is delegated).
        ``name`` is the name as stored; a name with an '@' (a partner named
        after an e-mail address) counts as no name: '' (contract 3.6, 3.14)."""
        partner = partner.sudo()
        user = partner.with_context(active_test=False).user_ids[:1] if partner else self.env['res.users']
        name = partner.name or ''
        if '@' in unicodedata.normalize('NFKC', name):
            name = ''
        return {
            'user_id': user.id or None,
            'partner_id': partner.id or None,
            'name': name,
            'email': partner.email or '',
            'lang': (user.lang if user else partner.lang) or '',
            'tz': (user.tz if user else partner.tz) or '',
            'is_portal': bool(user and user._is_portal()),
            'company_id': user.company_id.id if user else None,
        }

    @api.model
    def _identity_system_text(self, identity):
        """Identity block of the openai system message (after the system
        prompt). Never the email: what the model sees may stay in provider
        logs. Every value is cleaned as a one-line value of the data section
        (tools/record_text.clean_data_text), and the name then follows the
        contract's name rule (one_line_name): a name (anyone may set their own)
        can neither break the block into lines nor write a data marker."""
        if identity.get('is_portal'):
            account = 'portal user (customer)'
        elif identity.get('user_id'):
            account = 'internal user (employee)'
        else:
            account = 'contact without user account'
        lines = ['You are chatting in Odoo with this person (information only, no login is delegated):']
        # the kind of account first, then the ids, the language and the time zone; the name last (contract 3.7: a
        # name, which anybody may choose, never comes before the facts the system sets), cleaned as a data value
        # first, then by the contract's name rule; a name with an '@' is '' here already (_identity): no line
        name = one_line_name(clean_data_text(identity.get('name'), one_line=True))
        for label, value in (('account', account), ('user_id', identity.get('user_id')),
                             ('partner_id', identity.get('partner_id')), ('language', identity.get('lang')),
                             ('timezone', identity.get('tz')), ('name', name)):
            if value not in (None, '', False):
                text = clean_data_text(value, one_line=True)
                if text:
                    lines.append('- %s: %s' % (label, text))
        return '\n'.join(lines)

    @api.model
    def _company_time_zone(self):
        """The current company's time zone (contract 3.7; ``env.company``: the
        database's main company for a reply job, which runs as the
        superuser): the time zone of its working schedule (the Resource
        module, which Project installs; set on the working schedule form, a
        required field there), else the ``tz`` of its partner (no standard
        form shows it); None when neither is a known IANA time zone."""
        company = self.env.company.sudo()
        if 'resource_calendar_id' in company._fields:
            zone = time_zone(company.resource_calendar_id.tz)
            if zone:
                return zone
        return time_zone(company.partner_id.tz)

    @api.model
    def _current_time(self, identity=None):
        """The current time of one request (contract 3.6, 3.7 "The current
        time"), computed when the request is built: {'utc', 'local', 'tz',
        'weekday'}. The time zone is the first one set of: the current
        company's (_company_time_zone: its working schedule's, else its
        partner's), the person's own (the identity's ``tz``), UTC; a name that
        is not a known IANA time zone counts as none. n8n gets it as ``now``,
        LiteLLM as one system line (time_line); never the metadata."""
        zone = self._company_time_zone() or time_zone((identity or {}).get('tz')) or pytz.utc
        utc = _utcnow().astimezone(timezone.utc).replace(microsecond=0)
        local = utc.astimezone(zone)
        return {
            'utc': utc.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'local': local.strftime('%Y-%m-%dT%H:%M:%S') + utc_offset(local),
            'tz': zone.zone,
            'weekday': WEEKDAYS[local.weekday()],
        }

    @api.model
    def _openai_metadata(self, session_id, identity, surface=None):
        """OpenAI ``metadata``: string keys and string values only (at most 16
        keys, 512 characters each, as the OpenAI API requires). Informational:
        LiteLLM does not pass it to the model. The surface
        (``discuss.channel._woow_project_ai_surface_info``) comes first, flat, as
        ``surface_<key>`` (e.g. ``surface_type``), then the identity as
        ``odoo_<key>``; keys over the limit are left out. Never the email (it
        may stay in provider logs; only n8n gets it, in ``user``)."""
        metadata = {'session_id': session_id}
        for prefix, values in (('surface', surface or {}), ('odoo', identity)):
            for key, value in values.items():
                if len(metadata) >= METADATA_MAX_KEYS:
                    return metadata
                if (prefix, key) == ('odoo', 'email'):
                    continue
                if value in (None, '') or isinstance(value, (dict, list, tuple)):
                    continue
                if isinstance(value, bool):
                    value = 'true' if value else 'false'
                metadata['%s_%s' % (prefix, key)] = str(value)[:METADATA_MAX_VALUE]
        return metadata

    @api.model
    def _signing_secret(self):
        return self.env['ir.config_parameter'].sudo().get_param('database.secret') or ''

    @api.model
    def _attachment_info(self, attachment, base_url, now=None):
        """ATT object (docs/PROTOCOL.md) with a signed download URL valid 1 hour."""
        attachment = attachment.sudo()
        token, expires = media.make_token(self._signing_secret(), attachment.id, attachment.checksum, now=now)
        return {
            'id': attachment.id,
            'name': attachment.name or '',
            'mimetype': attachment.mimetype or 'application/octet-stream',
            'size': attachment.file_size or 0,
            'url': '%s/woow_project_ai/attachment/%s/%s' % (base_url.rstrip('/'), attachment.id, token),
            'expires_at': _iso(expires),
        }

    @staticmethod
    def _attachment_line(info):
        return '[附件] %s（%s，%s）：%s' % (
            info['name'], info['mimetype'], media.human_size(info['size']), info['url'])

    # --- Request building ---

    @staticmethod
    def _inline_images(history, exclude=(), linked=None):
        """Images to inline as data URIs: {(history position, attachment id): data URI}.

        Only images of the INLINE_IMAGE_MESSAGES most recent user messages,
        newest first, at most INLINE_IMAGE_MAX_COUNT images and
        INLINE_IMAGE_BUDGET characters in total. The type is sniffed from the
        content (the uploaded mimetype is not trusted).

        The attachments of ``exclude`` (the files the model could not read,
        3.7) are never inlined and count for nothing; ``linked`` (a set,
        filled) gets the keys of those that would be one here (in the window,
        an image type, within the size limit): they become their link line and
        the fallback note, the others stay as usual (a link line). Their
        content is not read: the conversation remembers files that were sent
        as file parts once.
        """
        positions = [i for i, entry in enumerate(history) if entry['role'] == 'user'][-INLINE_IMAGE_MESSAGES:]
        chosen = {}
        budget = INLINE_IMAGE_BUDGET
        for position in reversed(positions):
            for attachment in history[position]['attachments'].sudo():
                if attachment.id in exclude:
                    if linked is not None and attachment.mimetype in media.INLINE_IMAGE_TYPES \
                            and (attachment.file_size or 0) <= INLINE_IMAGE_MAX_BYTES:
                        linked.add((position, attachment.id))
                    continue
                if len(chosen) >= INLINE_IMAGE_MAX_COUNT:
                    continue  # (later remembered files are still listed in `linked`)
                if (attachment.mimetype not in media.INLINE_IMAGE_TYPES
                        or (attachment.file_size or 0) > INLINE_IMAGE_MAX_BYTES):
                    continue
                raw = attachment_raw(attachment)
                if not raw or len(raw) > INLINE_IMAGE_MAX_BYTES:
                    continue
                mimetype = media.sniff_mimetype(raw)
                if mimetype not in media.INLINE_IMAGE_TYPES:
                    continue
                prefix = 'data:%s;base64,' % mimetype
                if len(prefix) + 4 * ((len(raw) + 2) // 3) > budget:
                    continue  # an older, smaller image may still fit
                uri = prefix + base64.b64encode(raw).decode()
                budget -= len(uri)
                chosen[(position, attachment.id)] = uri
        return chosen

    @staticmethod
    def _document_note(name, reason):
        """Line telling the model what happened to a document (it never sees an empty gap)."""
        return '[附件說明] %s：%s' % (name, reason)

    def _inline_documents(self, history, mode, data_budget, exclude=(), linked=None):
        """Documents of user messages for an openai request (docs/PROTOCOL.md "Documents").

        Mode ``file``: every document (PDF, docx, xlsx, xls, csv, txt, md,
        json) is sent as it is, as an OpenAI ``file`` content part with its
        own type (media.document_mimetype); Odoo reads or converts nothing.
        Mode ``link``: nothing here, the signed link line only (for a server
        that refuses file parts).

        Returns {(history position, attachment id): {'file', 'mimetype'} or
        {'note'}}: ``file`` the content part, ``note`` a line for the model
        when a document is only a link (too old, too many, too large, over
        the budget). Attachments that are not documents (images, archives,
        media, active types) get no entry: the link line only, as before.

        Only documents of the DOC_MESSAGES most recent user messages, newest
        first: at most DOC_MAX_COUNT documents, DOC_FILE_MAX_BYTES each and
        ``data_budget`` data URI characters in total (what the inlined images
        left). Assistant messages are never read.

        The attachments of ``exclude`` (the files the model could not read,
        3.7) are never a file part and count for nothing: those that would be
        one here (a document of a recent message, within the size limit, in
        ``file`` mode) go to ``linked`` (a set, filled): their link line and
        the fallback note; the others get their usual note (an older message,
        too large) or none. Their content is not read.
        """
        found = {}
        if mode != 'file':
            return found
        user_positions = [i for i, entry in enumerate(history) if entry['role'] == 'user']
        recent = set(user_positions[-DOC_MESSAGES:])
        count = 0
        for position in reversed(user_positions):
            for attachment in history[position]['attachments'].sudo():
                name = attachment.name or 'file'
                if not media.is_document(name, attachment.mimetype):
                    continue
                key = (position, attachment.id)
                if position not in recent:
                    found[key] = {'note': self._document_note(
                        name, '在較早的訊息中，這次沒有再附上檔案（需要時請使用者重新上傳）。')}
                    continue
                if attachment.id in exclude:
                    if (attachment.file_size or 0) > DOC_FILE_MAX_BYTES:
                        found[key] = {'note': self._document_note(name, '檔案太大（%s，上限 %s），沒有附上檔案。' % (
                            media.human_size(attachment.file_size), media.human_size(DOC_FILE_MAX_BYTES)))}
                    elif linked is not None:
                        linked.add(key)
                    continue
                if count >= DOC_MAX_COUNT:
                    found[key] = {'note': self._document_note(
                        name, '沒有附上檔案：每次最多附上 %s 個文件。' % DOC_MAX_COUNT)}
                    continue
                too_large = self._document_note(name, '檔案太大（%s，上限 %s），沒有附上檔案。' % (
                    media.human_size(attachment.file_size), media.human_size(DOC_FILE_MAX_BYTES)))
                if (attachment.file_size or 0) > DOC_FILE_MAX_BYTES:
                    found[key] = {'note': too_large}
                    continue
                raw = attachment_raw(attachment)
                mimetype = media.document_mimetype(name, raw)
                if not mimetype:
                    continue  # not a document after all: the link line only
                if len(raw) > DOC_FILE_MAX_BYTES:
                    found[key] = {'note': too_large}
                    continue
                prefix = 'data:%s;base64,' % mimetype
                uri_length = len(prefix) + 4 * ((len(raw) + 2) // 3)
                if uri_length > data_budget:
                    found[key] = {'note': self._document_note(
                        name, '這次請求的檔案額度（約 %s，與圖片共用）已用完，沒有附上檔案。' % (
                            media.human_size(INLINE_IMAGE_BUDGET * 3 // 4)))}
                    continue
                data_budget -= uri_length
                count += 1
                found[key] = {'mimetype': mimetype, 'file': {'type': 'file', 'file': {
                    'filename': name, 'file_data': prefix + base64.b64encode(raw).decode()}}}
        return found

    def _openai_messages(self, channel, history, identity, base_url, now=None, link_only=(), plan=None,
                         files=None):
        """The ``messages`` of a LiteLLM request: one system message (the
        assistant's prompt and the surface text, the identity block, the
        current time: contract 3.7's order), then the turns.

        ``now``: the request's current time (_current_time; computed here when
        not given). ``link_only``: attachment ids sent as no file part (3.7:
        the files the model could not read): where one would be a file part,
        its link line and the fallback note. ``files``: the choice of files
        of an earlier request of the same turn, (inline, docs, linked), kept
        as it was (a fallback request, 3.7); None: chosen now. ``plan`` (a
        dict, filled): what a fallback needs (the choice of files, what was
        sent as file parts)."""
        self.ensure_one()
        assistant = self.sudo()
        now = now or assistant._current_time(identity)
        system = [part for part in (channel._woow_project_ai_system_prompt(assistant, history),
                                    assistant._identity_system_text(identity), time_line(now)) if part]
        system = [{'role': 'system', 'content': '\n\n'.join(system)}]
        link_only = frozenset(link_only or ())
        if files is None:
            linked = set()
            inline = self._inline_images(history, exclude=link_only, linked=linked)
            data_budget = INLINE_IMAGE_BUDGET - sum(len(uri) for uri in inline.values())
            docs = self._inline_documents(history, assistant.connection_id.document_mode or 'link', data_budget,
                                          exclude=link_only, linked=linked)
        else:
            inline, docs, linked = files
        sent = {}
        turns = assistant._openai_turns(history, base_url, inline, docs, link_only, sent, linked)
        if plan is not None:
            plan.update(files=(inline, docs, linked), link_only=link_only, sent=sent)
        return system + turns

    def _openai_turns(self, history, base_url, inline, docs, link_only=frozenset(), sent=None, linked=frozenset()):
        """The turns of a LiteLLM request (normalize_turns): each entry's
        text, its inline images and document file parts (``inline`` and
        ``docs``, as _inline_images and _inline_documents chose them), every
        other file as its signed link line with its note. An attachment of
        ``link_only`` that would be a file part (chosen, or in ``linked``) is
        its link line and the fallback note instead (3.7: the model could not
        read it); one that would not is as usual. ``sent`` (a dict, filled):
        {attachment id: history position} of every file part of these turns."""
        turns = []
        for position, entry in enumerate(history):
            lines = [entry['text']] if entry['text'] else []
            images = []
            files = []
            for attachment in entry['attachments'].sudo():
                key = (position, attachment.id)
                uri = inline.get(key)
                doc = docs.get(key)
                if attachment.id in link_only and (uri or (doc and doc.get('file')) or key in linked):
                    lines.append(self._attachment_line(self._attachment_info(attachment, base_url)))
                    lines.append(self._document_note(attachment.name or 'file', FALLBACK_NOTE))
                    continue
                if uri:
                    images.append(uri)
                elif doc and doc.get('file'):
                    files.append(doc['file'])
                    lines.append('[附件] %s（%s，%s）：檔案已附在這則訊息' % (
                        attachment.name or 'file', doc['mimetype'], media.human_size(attachment.file_size)))
                else:
                    lines.append(self._attachment_line(self._attachment_info(attachment, base_url)))
                    if doc and doc.get('note'):
                        lines.append(doc['note'])
                    continue
                if sent is not None:
                    sent[attachment.id] = position
            text = '\n'.join(lines)
            if images or files:
                content = [{'type': 'text', 'text': text}] if text else []
                content += [{'type': 'image_url', 'image_url': {'url': uri}} for uri in images]
                content += files
            else:
                content = text
            turns.append({'role': entry['role'], 'content': content})
        return normalize_turns(turns)

    @staticmethod
    def _fallback_drop(plan):
        """The attachments the next request of a fallback (3.7) leaves out:
        the file parts of earlier messages; when only the message being
        answered has some left, all of them."""
        sent = plan.get('sent') or {}
        earlier = {attachment_id for attachment_id, position in sent.items() if position != plan.get('current')}
        return earlier or set(sent)

    def _build_request(self, channel, history, trigger_message=None, plan=None, link_only=None, files=None):
        """Returns (url, headers, body bytes, payload for the call log).

        ``trigger_message`` (the message being answered): the requester is
        its author (several people share a task chat), and the task summary
        of the system prompt is read with their rights. The request's current
        time is computed here, once per request. LiteLLM: ``link_only`` (None:
        what the conversation remembers, 3.7) and ``files`` (None: chosen now)
        are given by a fallback request (the same choice of files minus the
        ones it leaves out); ``plan`` (a dict, filled): what a fallback needs
        (_openai_messages; the position of the message being answered)."""
        self.ensure_one()
        assistant = self.sudo()
        connection = assistant.connection_id.sudo()
        if trigger_message:
            channel = channel.with_context(woow_project_ai_trigger_id=trigger_message.id)
        if not connection or not connection.active:
            raise BackendError('error', 'backend not configured (no active AI connection)')
        api_key = connection._get_api_key()
        endpoint = (connection.endpoint_url or '').strip()
        if not endpoint or (connection.backend_type == 'openai' and not api_key):
            raise BackendError('error', 'backend not configured (endpoint URL or API key missing)')
        base_url = channel.get_base_url()
        session_id = self._session_id(channel)
        identity = channel._woow_project_ai_identity(assistant, trigger_message)
        now = assistant._current_time(identity)
        headers = {'Content-Type': 'application/json'}
        if connection.backend_type == 'openai':
            url = endpoint.rstrip('/') + '/chat/completions'
            headers['Authorization'] = 'Bearer %s' % api_key
            if link_only is None:
                # the files of this conversation the model could not read before (3.7 "Fallback")
                link_only = self.env['woow.project.ai.job'].sudo()._unreadable_files_of(channel)
            payload = {
                'messages': assistant._openai_messages(channel, history, identity, base_url, now=now,
                                                       link_only=link_only, plan=plan, files=files),
                'user': session_id,
                'metadata': assistant._openai_metadata(session_id, identity,
                                                       surface=channel._woow_project_ai_surface_info()),
            }
            if (connection.model or '').strip():
                payload = {'model': connection.model.strip(), **payload}
            if connection.openai_stream:
                payload['stream'] = True
                # usage of a streamed answer (a last chunk with empty "choices"):
                # LiteLLM and OpenAI send it only when asked; servers that do not
                # know the option ignore it
                payload['stream_options'] = {'include_usage': True}
                headers['Accept'] = 'text/event-stream'
            body = _json_dumps(payload).encode()
            if plan is not None:
                plan['current'] = current_position(history, trigger_message)
        else:
            # n8n: the API key is only the optional signing secret, never sent
            # (n8n stores the webhook headers of every execution); without one
            # (e.g. a public Chat Trigger) the request is not signed
            url = endpoint
            payload = assistant._n8n_payload(channel, history, identity, session_id, base_url, trigger_message,
                                             now=now)
            body = _json_dumps(payload).encode()
            if api_key:
                headers.update(_signature_headers(api_key, body))
        return url, headers, body, payload

    def _n8n_payload(self, channel, history, identity, session_id, base_url, trigger_message=None, now=None):
        """n8n webhook body (docs/PROTOCOL.md), plus the fields an n8n Chat
        Trigger reads (``action``, ``sessionId``, ``chatInput``); a
        Webhook-node workflow ignores them. ``now``: the request's current
        time (_current_time; computed here when not given)."""
        self.ensure_one()
        assistant = self.sudo()

        def entry_json(entry):
            return {
                'role': entry['role'],
                'text': entry['text'],
                'attachments': [self._attachment_info(a, base_url) for a in entry['attachments'].sudo()],
            }

        current = None
        if trigger_message:
            current = next((e for e in history if e.get('message_id') == trigger_message.id), None)
        if current is None:
            current = next((e for e in reversed(history) if e['role'] == 'user'), None)
        past = [e for e in history if e is not current]
        message = {'id': None, 'text': '', 'attachments': []}
        if current:
            message = {'id': current.get('message_id'), 'text': current['text'],
                       'attachments': entry_json(current)['attachments']}
        return {
            'event': 'message',
            'session_id': session_id,
            # n8n Chat Trigger ("hosted chat") aliases
            'action': 'sendMessage',
            'sessionId': session_id,
            'chatInput': message['text'],
            'assistant': {'id': assistant.id, 'name': assistant.name},
            'user': identity,
            'channel': channel._woow_project_ai_channel_info(assistant),
            'surface': channel._woow_project_ai_surface_info(),
            'system_prompt': channel._woow_project_ai_system_prompt(assistant, history) or '',
            'message': message,
            'history': [entry_json(e) for e in past],
            # events of the conversation (the contract's key, always present): task chats have none
            'events': [],
            # the current time (contract 3.6, 3.7): {utc, local, tz, weekday}
            'now': now or assistant._current_time(identity),
        }

    # --- Response parsing ---

    @staticmethod
    def _openai_text(data, names=(), kinds=()):
        if not isinstance(data, dict) or not isinstance(data.get('choices'), list) or not data['choices']:
            raise BackendError('invalid_response', "invalid response: no 'choices'")
        message = (data['choices'][0] or {}).get('message') or {}
        content = message.get('content')
        if isinstance(content, list):
            content = '\n'.join(
                part.get('text') or '' for part in content
                if isinstance(part, dict) and part.get('type') in ('text', 'output_text'))
        if content is not None and not isinstance(content, str):
            raise BackendError('invalid_response', 'invalid response: content is not text')
        return clean_answer(content or '', names, kinds)

    @staticmethod
    def _sse_lines(response, deadline, watchdog=None):
        """Lines (str) of a streamed response; checks the total deadline.

        A read can block far longer than one chunk suggests (without chunked
        encoding urllib3 waits for a full chunk or EOF, and small keepalives
        keep resetting the socket timeout), so ``watchdog`` (_StreamWatchdog)
        also cuts the connection at the deadline."""
        buffer = b''
        for chunk in response.iter_content(chunk_size=SSE_CHUNK_SIZE):
            if time.time() > deadline or (watchdog and watchdog.fired):
                raise BackendError('timeout', 'timeout: the streamed answer took too long')
            if not chunk:
                continue
            buffer += chunk
            *lines, buffer = buffer.split(b'\n')
            for line in lines:
                yield line.rstrip(b'\r').decode('utf-8', 'replace')
        if buffer:
            yield buffer.rstrip(b'\r').decode('utf-8', 'replace')

    @classmethod
    def _read_sse(cls, response, deadline):
        """Read an OpenAI streamed answer (server-sent events).

        Comment lines (``: keepalive``) and events other than the default
        ``message`` (e.g. a proxy's progress events) are ignored; ``delta.content``
        is accumulated, ``reasoning_content`` ignored, ``[DONE]`` ends it.

        Returns:
            dict: content (str), usage (dict or None), finish_reason, done (bool)
        """
        state = {'content': [], 'usage': None, 'finish_reason': None, 'done': False}
        event = {'type': None, 'data': []}

        def dispatch():
            data = '\n'.join(event['data'])
            kind = event['type']
            event['type'], event['data'] = None, []
            if not data or kind not in (None, '', 'message'):
                return
            if data.strip() == '[DONE]':
                state['done'] = True
                return
            try:
                chunk = json.loads(data)
            except ValueError:
                raise BackendError('invalid_response', 'invalid response: stream chunk is not JSON')
            if not isinstance(chunk, dict):
                return
            if chunk.get('error'):
                raise BackendError('invalid_response', 'invalid response: error in the stream')
            state['usage'] = _pick_usage(state['usage'], chunk.get('usage'))
            for choice in chunk.get('choices') or []:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get('delta') or {}
                content = delta.get('content') if isinstance(delta, dict) else None
                if isinstance(content, list):
                    content = ''.join(part.get('text') or '' for part in content if isinstance(part, dict))
                if isinstance(content, str):
                    state['content'].append(content)
                if choice.get('finish_reason'):
                    state['finish_reason'] = choice['finish_reason']

        with _StreamWatchdog(response, deadline) as watchdog:
            try:
                for line in cls._sse_lines(response, deadline, watchdog):
                    if not line:
                        dispatch()
                        if state['done']:
                            break
                        continue
                    if line.startswith(':'):
                        continue  # comment / keepalive
                    field, _sep, value = line.partition(':')
                    if value.startswith(' '):
                        value = value[1:]
                    if field == 'data':
                        event['data'].append(value)
                    elif field == 'event':
                        event['type'] = value
            except (requests.RequestException, OSError):
                if watchdog.fired and not state['done']:
                    raise BackendError('timeout', 'timeout: the streamed answer took too long')
                raise
            if watchdog.fired and not state['done']:
                raise BackendError('timeout', 'timeout: the streamed answer took too long')
        if not state['done']:
            dispatch()
        if not state['done'] and not state['finish_reason']:
            raise BackendError('invalid_response', 'invalid response: the stream ended before [DONE]')
        return {'content': ''.join(state['content']), 'usage': state['usage'],
                'finish_reason': state['finish_reason'], 'done': state['done']}

    @staticmethod
    def _n8n_replies(data, names=(), kinds=()):
        """n8n JSON -> (replies, skip).

        Accepted: ``{"skip": true}``,
        ``{"replies": [...]}``, ``{"reply": {...}}`` / ``{"reply": "text"}``,
        the n8n AI Agent / Chat Trigger answer ``{"output": "text"}`` (one
        message), each also as an n8n item list ``[{...}]`` (first item).
        ``{"handoff": true}`` alone (no reply field) is a handoff without
        text (_n8n_handoff); with ``handoff`` the ``skip`` flag is ignored."""
        if isinstance(data, list) and data and isinstance(data[0], dict):
            data = data[0]  # "Respond to Webhook" with all incoming items
        if not isinstance(data, dict):
            raise BackendError('invalid_response', 'invalid response: not a JSON object')
        handoff = data.get('handoff') is True
        if handoff and not {'replies', 'reply', 'output'} & set(data):
            return [], False
        if data.get('skip') is True and not handoff:
            return [], True
        if 'replies' in data:
            raw = data['replies']
            if not isinstance(raw, list):
                raise BackendError('invalid_response', "invalid response: 'replies' is not a list")
        elif 'reply' in data:
            raw = [data['reply']]
        elif 'output' in data:
            if not isinstance(data['output'], str):
                raise BackendError('invalid_response', "invalid response: 'output' is not a string")
            raw = [data['output']]
        else:
            raise BackendError('invalid_response', "invalid response: no 'reply', 'replies', 'skip' or 'output'")
        replies = []
        for item in raw[:MAX_REPLIES]:
            if isinstance(item, str):
                item = {'text': item}
            if not isinstance(item, dict):
                raise BackendError('invalid_response', 'invalid response: reply is not an object')
            text = item.get('text') or ''
            attachments = item.get('attachments') or []
            if not isinstance(text, str) or not isinstance(attachments, list):
                raise BackendError('invalid_response', 'invalid response: bad reply fields')
            replies.append({'text': clean_answer(text, names, kinds), 'attachments': attachments})
        return replies, False

    @staticmethod
    def _n8n_handoff(data):
        """n8n JSON -> (handoff requested, reason or None): ``"handoff": true``
        (top level or in the first item of an n8n item list) and an optional
        ``"handoff_reason"`` string. What a handoff does is up to the channel
        (``discuss.channel._woow_project_ai_review_answer``): the customer AI
        chat of a task pauses and schedules a To-Do, the internal one ignores
        it."""
        if isinstance(data, list) and data and isinstance(data[0], dict):
            data = data[0]
        if not isinstance(data, dict) or data.get('handoff') is not True:
            return False, None
        reason = data.get('handoff_reason')
        reason = ' '.join(reason.split())[:MAX_HANDOFF_REASON] if isinstance(reason, str) else ''
        return True, reason or None

    # --- Call ---

    def _http_post(self, url, headers, body, read_timeout, stream=False, max_retries=1, deadline=None,
                   before_retry=None, connect_timeout=CONNECT_TIMEOUT):
        """POST with explicit timeouts and bounded retries.

        Retried (at most ``max_retries`` times, after RETRY_WAITS seconds and
        only while the total ``deadline`` allows it): a connection that could
        not be opened (refused, unknown host, connect timeout: the request was
        not sent, see _request_not_sent) and HTTP 429 / 503 (the backend
        refused before working). Never retried: a connection reset or closed
        after the request was sent, read timeouts, 502 / 504 / 524 and every
        other answer, because the backend may already be working on it.

        Redirects are never followed: a 3xx is returned like any other answer
        (an http_error for the caller), so the conversation is never re-sent
        to a host or a scheme that endpoint_url_allowed did not check. Reads
        nothing from the database itself (no transaction is open here);
        ``before_retry`` (from _call_backend: the run-time gate of contract
        3.5, then a commit) is asked after each wait, before the request is
        sent again: False stops the retries (GateClosed).

        ``deadline`` is the total time of the answer: a retry's read timeout is
        what is left of it (at least 1 s), so a retry never makes the call wait
        past the connection's timeout (by more than the connect timeout of the
        last attempt, inside the runner's margin).
        """
        attempt = 0
        while True:
            retry_reason = None
            attempt_read_timeout = read_timeout
            if deadline and attempt:
                attempt_read_timeout = min(read_timeout, max(1.0, deadline - time.time()))
            timeout = (connect_timeout, attempt_read_timeout)
            try:
                response = requests.post(url, data=body, headers=headers, timeout=timeout, stream=stream,
                                         allow_redirects=False)
            except requests.ConnectionError as e:
                if not _request_not_sent(e):
                    raise
                retry_reason, error = type(e).__name__, e
            else:
                if response.status_code not in RETRY_HTTP_STATUSES:
                    return response
                retry_reason, error = 'HTTP %s' % response.status_code, None
            wait = RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)]
            if attempt >= max_retries or (deadline and time.time() + wait >= deadline):
                if error is not None:
                    raise error
                return response
            if error is None:
                response.close()
            attempt += 1
            _logger.info("Project AI assistant %s: %s, retry %s/%s in %s s", self._ids, retry_reason, attempt,
                         max_retries, wait)
            time.sleep(wait)
            if before_retry is not None and not before_retry():
                raise GateClosed('error', 'the AI cannot answer any more: not sent again',
                                 response.status_code if error is None else None)

    def _send_and_read(self, url, headers, body, read_timeout, stream, max_retries, deadline, backend_type, labels,
                       log, result, before_retry=None, connect_timeout=CONNECT_TIMEOUT):
        """One request of an answer (its bounded retries included) and what
        it returns, read into ``log`` (HTTP status, response, usage) and
        ``result`` (replies, skip, handoff); raises BackendError. The reply
        texts lose the labels a model copied (clean_answer with ``labels``:
        the chat's (names, kinds)), a streamed answer as a whole once it
        ended. Reads nothing from the database (no transaction is open here)."""
        names, kinds = labels
        try:
            response = self._http_post(url, headers, body, read_timeout, stream=stream, max_retries=max_retries,
                                       deadline=deadline, before_retry=before_retry, connect_timeout=connect_timeout)
        except requests.Timeout:
            raise BackendError('timeout', 'timeout: no answer within %s s' % int(read_timeout))
        except requests.RequestException as e:
            raise BackendError('error', 'connection failed (%s)' % type(e).__name__)
        try:
            log['http_status'] = response.status_code
            if response.status_code != 200:
                _logger.warning("Project AI assistant %s: backend HTTP %s", self.id, response.status_code)
                raise BackendError('http_error', 'HTTP %s' % response.status_code, response.status_code)
            content_type = (response.headers.get('Content-Type') or '').lower()
            if stream and 'text/event-stream' in content_type:
                try:
                    streamed = self._read_sse(response, deadline)
                except requests.RequestException as e:
                    if _is_read_timeout(e):
                        raise BackendError('timeout', 'timeout: no data for %s s while streaming'
                                           % int(read_timeout))
                    raise BackendError('error', 'stream interrupted (%s)' % type(e).__name__)
                log['response'] = {'stream': True, 'content': streamed['content'],
                                   'finish_reason': streamed['finish_reason'], 'usage': streamed['usage']}
                log['usage'] = streamed['usage']
                # the labels of the whole answer, once the stream ended (never per delta)
                text = clean_answer(streamed['content'], names, kinds)
            else:
                try:
                    data = response.json()
                except ValueError:
                    raise BackendError('invalid_response', 'invalid response: not JSON')
                except requests.RequestException as e:
                    if _is_read_timeout(e):
                        raise BackendError('timeout', 'timeout: no data for %s s' % int(read_timeout))
                    raise BackendError('error', 'connection lost (%s)' % type(e).__name__)
                log['response'] = data
                if backend_type == 'openai':
                    log['usage'] = data.get('usage') if isinstance(data, dict) else None
                    text = self._openai_text(data, names, kinds)
                else:
                    text = None
                    result['replies'], result['skip'] = self._n8n_replies(data, names, kinds)
                    result['handoff'], result['handoff_reason'] = self._n8n_handoff(data)
                    if not result['skip'] and not result['handoff'] \
                            and not any(r['text'] or r['attachments'] for r in result['replies']):
                        raise BackendError('invalid_response', 'empty reply')
            if text is not None:
                if not text:
                    raise BackendError('invalid_response', 'empty reply')
                result['replies'] = [{'text': text, 'attachments': []}]
        finally:
            close = getattr(response, 'close', None)
            if close:
                close()

    def _call_backend(self, channel, history, job=None, before_call=None, gate=None):
        """Call the backend once (bounded retries aside) and log the call.

        A speaker label the model copied to the start of a reply text is
        removed (clean_answer with the chat's labels:
        discuss.channel._woow_project_ai_answer_labels), before the
        empty-answer rule.

        LiteLLM, when the model cannot read a file (contract 3.7 "Fallback
        when the model cannot read a file"): a request that carries file
        parts (inline images, documents in ``file`` mode) and gets HTTP 400,
        413, 415, 422 or 500 (after the usual retries) is followed at once by
        a fallback request, with no wait, outside ``max_retries``, within the
        same deadline (never started with less than FALLBACK_MIN_SECONDS
        left, checked again right before it is sent; its connect and read
        timeouts end at the deadline): first without the file parts of
        earlier messages, then, when the message being answered still has
        some, without any (_fallback_drop). Before each fallback request, and
        before a request is sent again after a wait (a refused connection,
        HTTP 429 or 503), the run-time gate of 3.5 is checked again
        (``gate``, a callable: True while the job may still ask); when it
        blocks, nothing more is sent (``gate_blocked``). Each fallback request is built again
        (_build_request: its own current time), with the same choice of
        files minus the ones it leaves out, each its link line and the
        fallback note. Each request writes its own call log row, and only
        one; the answer is the first success. The files the request that
        succeeded left out are remembered on the job (``unreadable_files``,
        with what the conversation remembered before: each job that asks
        carries the whole set): later requests of the conversation send them
        as links where they would be file parts. Without a success the turn
        fails as without the fallback, and nothing new is remembered.

        Returns:
            dict: replies (list of {'text', 'attachments'}), skip (bool),
                handoff (bool: the n8n answer asked for a human, see
                _n8n_handoff), handoff_reason (str or None), error (short
                text or None), gate_blocked (True when the gate stopped a
                fallback request), unreadable_files (the attachment ids this
                call wrote on the job, when it wrote some), call_log
                (woow.project.ai.call.log: the row of the last request sent)
        """
        self.ensure_one()
        assistant = self.sudo()
        connection = assistant.connection_id.sudo()
        trigger = job.message_id if job else None
        started = time.time()
        # what the request being built or sent writes in its row; None while no request is pending since the last
        # row written (a refused request's: last_row)
        log = _new_log()
        last_row = self.env['woow.project.ai.call.log']
        result = {'replies': [], 'skip': False, 'handoff': False, 'handoff_reason': None, 'error': None}
        plan = {}
        remembered, dropped = frozenset(), set()
        try:
            url, headers, body, payload = assistant._build_request(channel, history, trigger, plan=plan)
            remembered = frozenset(plan.get('link_only') or ())
            log['request'] = payload
            # the chat's speaker labels a model may copy (3.14): name-only ones (the internal chat), other kinds
            labels = channel._woow_project_ai_answer_labels(history) if channel else (frozenset(), ())
            total_timeout = connection.timeout_seconds or 300
            backend_type = connection.backend_type
            if backend_type == 'n8n':
                total_timeout = min(total_timeout, N8N_MAX_TIMEOUT)
            stream = backend_type == 'openai' and bool(payload.get('stream'))
            max_retries = connection.max_retries
            assistant_id = assistant.id
            if before_call:
                before_call()  # commits: no transaction stays open during the call

            def before_retry():
                """Before a request is sent again (a refused connection, HTTP 429 or 503): the run-time gate
                of 3.5 once more, then the read transaction ends again (before_call)."""
                allowed = gate is None or gate()
                if before_call:
                    before_call()
                return allowed

            started = time.time()
            deadline = started + total_timeout
            first_read_timeout = read_timeout = min(STREAM_IDLE_TIMEOUT, total_timeout) if stream else total_timeout
            connect_timeout = CONNECT_TIMEOUT
            while True:
                try:
                    assistant._send_and_read(url, headers, body, read_timeout, stream, max_retries, deadline,
                                             backend_type, labels, log, result, before_retry=before_retry,
                                             connect_timeout=connect_timeout)
                    break
                except BackendError as error:
                    drop = error.http_status in FALLBACK_HTTP_STATUSES and plan.get('sent') \
                        and deadline - time.time() >= FALLBACK_MIN_SECONDS and assistant._fallback_drop(plan)
                    if not drop:
                        raise
                    # the refused request's own row (http_error), its only one; its error is the turn's unless a
                    # fallback request answers
                    log['status'], log['error'] = error.status, str(error)
                    last_row = assistant._create_call_log(channel, job, log, int((time.time() - started) * 1000))
                    result['error'] = str(error)
                    log = None
                    if gate is not None and not gate():
                        # the run-time gate (3.5), checked again before a fallback request: nothing more is sent
                        result['gate_blocked'] = True
                        break
                    _logger.info("Project AI assistant %s: HTTP %s to a request with %s file part(s); the turn is "
                                 "sent again at once without %s of them", assistant_id, error.http_status,
                                 len(plan['sent']), len(drop))
                    dropped |= drop
                    # built again (its own current time), the same choice of files minus the ones left out
                    url, headers, body, payload = assistant._build_request(
                        channel, history, trigger, plan=plan, link_only=plan['link_only'] | frozenset(drop),
                        files=plan['files'])
                    if before_call:
                        before_call()  # the row above and what the request read: no transaction during the call
                    started = time.time()
                    left = deadline - started
                    if left < FALLBACK_MIN_SECONDS:
                        break  # never started with less than a second left (3.7): no request, the refusal stands
                    # the deadline bounds the whole request, the connection too
                    read_timeout = min(first_read_timeout, left)
                    connect_timeout = min(CONNECT_TIMEOUT, left)
                    log = _new_log()
                    log['request'] = payload
            if log is not None:
                log['status'] = 'ok'
                result['error'] = None
        except BackendError as e:
            if log is not None:
                log['status'], log['error'] = e.status, str(e)
                if e.http_status:
                    log['http_status'] = e.http_status
                result['error'] = str(e)
            # else: building a fallback request failed (no request of its own): the refused request's answer stands
            if isinstance(e, GateClosed):
                result['gate_blocked'] = True  # a retry was not sent: the job ends as a blocked one
        except Exception as e:  # noqa: BLE001 - every failure becomes an error reply
            # the type name only, in the log too: an exception's text may carry a key, an address or an upstream
            # answer
            log_unexpected(_logger, "Project AI assistant %s: backend call failed", (assistant.id,), e)
            if log is not None:
                log['status'], log['error'] = 'error', type(e).__name__
                result['error'] = log['error']
        if log is not None:
            result['call_log'] = assistant._create_call_log(channel, job, log, int((time.time() - started) * 1000))
        else:
            result['call_log'] = last_row
        if job and plan.get('files') is not None:
            # the conversation remembers (3.7): what it knew, plus the files the request that got the answer left
            # out (none when no request succeeded). Every job that asks carries the whole set (the cleanup keeps
            # the newest of them, woow.project.ai.job._cleanup_old_jobs); a retry of this job reads it too
            succeeded = log is not None and log['status'] == 'ok'
            files = remembered | (dropped if succeeded else set())
            if files and job._remember_unreadable_files(files):
                result['unreadable_files'] = sorted(files)
        return result

    # --- Call log ---

    @staticmethod
    def _redact_payload(payload):
        """Copy of a request payload without inline file data."""
        return redact(payload)

    @staticmethod
    def _redact_response(data):
        """Copy of a response without inline file data (data URIs, base64)."""
        return redact(data)

    def _create_call_log(self, channel, job, log, duration_ms, backend_type=None):
        self.ensure_one()
        assistant = self.sudo()
        connection = assistant.connection_id.sudo()
        prompt_tokens, completion_tokens, total_tokens = usage_tokens(log.get('usage'))
        surface, scope_model, scope_res_id = {}, False, False
        if channel:
            try:
                surface = channel._woow_project_ai_surface_info() or {}
                scope_model, scope_res_id = channel._woow_project_ai_usage_scope() or (False, False)
            except Exception as error:  # noqa: BLE001 - a log must never break the reply
                log_unexpected(_logger, "Project AI assistant %s: no surface for the call log", (assistant.id,), error)
        vals = {
            'assistant_id': assistant.id,
            'connection_id': connection.id or False,
            'channel_id': channel.id if channel else False,
            'job_id': job.id if job else False,
            'surface_type': surface.get('type') or False,
            'surface_tab': surface.get('tab') if isinstance(surface.get('tab'), str) else False,
            'scope_model': scope_model or False,
            'scope_res_id': scope_res_id or False,
            'backend_type': backend_type or connection.backend_type,
            'status': log['status'],
            'http_status': log['http_status'] or 0,
            'duration_ms': duration_ms,
            'error': (log['error'] or '')[:MAX_ERROR_LENGTH] or False,
            'prompt_tokens': prompt_tokens,
            'completion_tokens': completion_tokens,
            'total_tokens': total_tokens,
            # (explicit, as every other field: no default of any kind reaches a call log)
            'request_payload': False,
            'response_payload': False,
        }
        if connection.log_payload:
            if log['request'] is not None:
                vals['request_payload'] = _json_dumps(self._redact_payload(log['request']))[:MAX_LOG_PAYLOAD]
            if log['response'] is not None:
                vals['response_payload'] = _json_dumps(self._redact_response(log['response']))[:MAX_LOG_PAYLOAD]
        try:
            with self.env.cr.savepoint():
                # (never a client's default_* key: Test Connection is called with the client's context)
                return trusted(self.env['woow.project.ai.call.log'].sudo()).create(vals)
        except Exception as e:  # noqa: BLE001 - a log must never break the reply
            log_unexpected(_logger, "Project AI assistant %s: could not create the call log", (assistant.id,), e)
            return self.env['woow.project.ai.call.log']
