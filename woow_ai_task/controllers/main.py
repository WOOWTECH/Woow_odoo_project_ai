# -*- coding: utf-8 -*-
"""HTTP endpoints of project AI's own core (routes under /woow_project_ai/,
none shared with woow_ai_contact's /woow_ai/):

- signed download of the files people send in a task AI chat, for the
  backends (n8n gets links; LiteLLM gets links for what is not sent inline);
- the push API: a backend posts a later reply into a task AI chat its
  assistant answers (n8n "skip, then push").
"""

import json
import logging
import time

from werkzeug.exceptions import NotFound

from odoo import http, SUPERUSER_ID
from odoo.http import request

from ..models.ai_assistant_backend import clean_answer
from ..tools import media

_logger = logging.getLogger(__name__)

MAX_PUSH_BODY_BYTES = 80 * 1024 * 1024  # base64 attachments included


def _json_response(data, status=200):
    return request.make_json_response(data, status=status)


class WoowProjectAiController(http.Controller):

    # --- Signed download of user attachments (for the backends) ---

    @http.route('/woow_project_ai/attachment/<int:attachment_id>/<string:token>', type='http', auth='public',
                methods=['GET'], readonly=True)
    def woow_project_ai_attachment(self, attachment_id, token):
        env = request.env(user=SUPERUSER_ID, su=True)
        attachment = env['ir.attachment'].browse(attachment_id).exists()
        if not attachment:
            raise NotFound()
        secret = env['woow.project.ai.assistant']._signing_secret()
        if not media.check_token(secret, attachment.id, attachment.checksum, token):
            raise NotFound()
        # only the files of a task AI chat an assistant answers now
        message = env['mail.message'].search([
            ('attachment_ids', 'in', attachment.ids), ('model', '=', 'discuss.channel'),
        ], limit=1)
        channel = env['discuss.channel'].browse(message.res_id).exists() if message else None
        if not channel or not channel.woow_project_ai_task_id or not channel._woow_project_ai_assistant():
            raise NotFound()
        stream = env['ir.binary']._get_stream_from(attachment, 'raw')
        response = stream.get_response(as_attachment=True)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Cache-Control'] = 'private, no-store'
        return response

    # --- Push API ---

    @http.route('/woow_project_ai/v1/push', type='http', auth='none', methods=['POST'], csrf=False,
                save_session=False, readonly=False)
    def woow_project_ai_push(self, **_kwargs):
        """``POST {"to": {"channel_id": <task AI chat>}, "text": …, "attachments": [...]}``
        with ``Authorization: Bearer <push key of the assistant>`` (docs/PROTOCOL.md)."""
        if not request.db:
            raise NotFound()
        env = request.env(user=SUPERUSER_ID, su=True)
        started = time.time()
        auth = request.httprequest.headers.get('Authorization') or ''
        key = auth[7:].strip() if auth[:7].lower() == 'bearer ' else ''
        assistant = env['woow.project.ai.assistant']._find_by_push_key(key)
        if not assistant or not assistant.active:
            return _json_response({'ok': False, 'error': 'invalid_key'}, status=401)

        def fail(status, code, detail=None, channel=None):
            env['woow.project.ai.assistant'].browse(assistant.id)._create_call_log(
                channel, None, {'status': 'error', 'http_status': status, 'error': 'push: %s' % code,
                                'request': None, 'response': None},
                int((time.time() - started) * 1000), backend_type='push')
            body = {'ok': False, 'error': code}
            if detail:
                body['detail'] = detail
            return _json_response(body, status=status)

        raw = request.httprequest.get_data()  # cached: Odoo may retry the request
        if len(raw) > MAX_PUSH_BODY_BYTES:
            return fail(400, 'too_large')
        try:
            data = json.loads(raw or b'{}')
        except ValueError:
            return fail(400, 'invalid_json')
        if not isinstance(data, dict):
            return fail(400, 'invalid_body')
        to = data.get('to')
        text = data.get('text') or ''
        attachments = data.get('attachments') or []
        if not isinstance(to, dict) or not isinstance(text, str) or not isinstance(attachments, list):
            return fail(400, 'invalid_body')
        if not text.strip() and not attachments:
            return fail(400, 'empty_message')
        channel_id = to.get('channel_id')
        if not isinstance(channel_id, int) or isinstance(channel_id, bool) or channel_id <= 0:
            return fail(400, 'invalid_recipient', 'push to a task AI chat: {"to": {"channel_id": <id>}}')
        channel = env['discuss.channel'].browse(channel_id).exists()
        if not channel or not channel.woow_project_ai_task_id:
            # only task AI chats (no direct chat, no Live Chat, no other module's conversation)
            return fail(404, 'channel_not_found')
        # the assistant answering the chat now: the same rules as a reply (assistant, connection
        # ready, customer tab safe for customers, task active)
        if channel._woow_project_ai_assistant() != assistant:
            return fail(403, 'not_allowed', channel=channel)
        if channel._woow_project_ai_is_paused():
            return fail(409, 'paused', channel=channel)
        # a text the AI posts: a speaker label the workflow's model copied to its start goes, as from an answer
        # (contract 3.14 "Labels in answers": the same labels, those of the chat's members and history authors in
        # the internal chat, 其他 in the customer chat); a text of labels only is refused like an empty one (below)
        names, kinds = channel._woow_project_ai_answer_labels(assistant=assistant)
        text = clean_answer(text, names, kinds)
        prepared = assistant._prepare_reply({'text': text, 'attachments': attachments}, channel=channel)
        if not str(prepared['html']).strip() and not prepared['files']:
            return fail(400, 'empty_message', 'no text and no usable attachment', channel=channel)
        message = assistant._post_prepared(channel, prepared)
        warnings = '; '.join(prepared.get('warnings') or []) or None
        call_log = assistant._create_call_log(channel, None, {'status': 'ok', 'http_status': 200, 'error': warnings,
                                                              'request': None, 'response': None},
                                              int((time.time() - started) * 1000), backend_type='push')
        # counted once the push is committed (a READ COMMITTED cursor: the request's snapshot would make the
        # upsert fail when a reply of the same month was counted meanwhile)
        env['woow.project.ai.usage.month']._add_for_channel_after_commit(channel, assistant,
                                                                         call_log.connection_id or None)
        return _json_response({'ok': True, 'channel_id': channel.id, 'message_id': message.id})
