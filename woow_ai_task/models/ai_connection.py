# -*- coding: utf-8 -*-
"""AI connections of project AI: where the brain of an AI assistant is, and how
it is called.

Two backend types: a LiteLLM server (any OpenAI compatible
``/v1/chat/completions`` server; Odoo sends the conversation and never any
tool, so these connections are chat-only) and an n8n webhook. A connection
holds the URL and key, the model and the call behaviour (timeouts, retries,
streaming, documents, files in replies, content logging). AI assistants
(``woow.project.ai.assistant``) are the personas; each one points to one
connection, and several assistants may share it.

Only system administrators can read or change connections (ACL: no other
group has access). Runtime reads go through sudo. The API key is write-only:
it lives in an SQL-only column (``api_key_secret``, not an ORM field), so no
read, search, export or form ever returns it.

"Safe for Customers" (``customer_safe``) decides whether the customer AI chat
of a project may use the connection: always for LiteLLM (chat-only); for n8n
only once a system administrator ticked it, cleared again when the workflow
URL or the backend type changes.

Ported from woow_ai_contact 18.0.2.0.0 (``woow.ai.connection``), with its two
backend types only (n8n and LiteLLM). Nothing is shared with that module.
"""

import logging
import requests

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ..tools import media
from ..tools.urls import split_url
from .ai_assistant_backend import CONNECT_TIMEOUT, TEST_TIMEOUT, _json_dumps, _signature_headers

_logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300
DEFAULT_N8N_TIMEOUT = 120  # a new n8n connection's timeout (create and onchange)
MAX_TIMEOUT = 3600
DEFAULT_MAX_DOWNLOAD_MB = 20
MAX_DOWNLOAD_MB = 100
DEFAULT_MAX_RETRIES = 1
MAX_MAX_RETRIES = 2

BACKEND_LITELLM = 'openai'  # technical value: the OpenAI chat-completions protocol
BACKEND_N8N = 'n8n'

READINESS_OK = 'ok'
READINESS_BLOCKED = 'blocked'


def endpoint_origin(url):
    """(scheme, host, port) of an endpoint URL, None when empty; the server a
    key is sent to. A URL whose host is not certain (tools/urls.py) is its own
    origin: changing to or from it drops the saved key."""
    url = (url or '').strip()
    if not url:
        return None
    parts = split_url(url)
    if parts is None:
        return ('?', url)
    scheme = (parts.scheme or '').lower()
    return (scheme, (parts.hostname or '').lower(), parts.port or {'https': 443, 'http': 80}.get(scheme))


def endpoint_url_allowed(url):
    """https, or http to localhost / an in-cluster *.svc.cluster.local host;
    never a URL whose host is not certain (tools/urls.py: a backslash,
    userinfo, a host urlsplit and requests read differently)."""
    parts = split_url((url or '').strip())
    if parts is None:
        return False
    host = (parts.hostname or '').lower()
    if not host:
        return False
    if parts.scheme == 'https':
        return True
    if parts.scheme == 'http':
        return media.is_internal_host(host)
    return False


class WoowProjectAiConnection(models.Model):
    _name = 'woow.project.ai.connection'
    _description = 'Project AI Connection'
    _order = 'name, id'

    name = fields.Char(string='Name', required=True)
    active = fields.Boolean(default=True)
    description = fields.Text(string='Description',
                              help='For administrators: what this backend is and who runs it.')
    backend_type = fields.Selection(
        [(BACKEND_LITELLM, 'LiteLLM (OpenAI compatible)'), (BACKEND_N8N, 'n8n webhook')],
        string='Backend', required=True, default=BACKEND_LITELLM,
        help='LiteLLM: Odoo sends the conversation to /v1/chat/completions and posts the answer; it never sends '
             'tools, so the connection is chat-only. n8n: Odoo sends the conversation to a webhook; what the '
             'workflow does (data lookups, tools, memory) is up to the workflow.',
    )
    endpoint_url = fields.Char(
        string='Endpoint URL',
        help='LiteLLM: the base URL ending in /v1 (…/chat/completions is added). n8n: the full production '
             'webhook URL (a Chat Trigger: …/webhook/<id>/chat). Must be https, except localhost and '
             '*.svc.cluster.local.',
    )
    api_key = fields.Char(
        string='API Key', compute='_compute_api_key', inverse='_inverse_api_key', store=False, copy=False,
        help='LiteLLM (required): sent as "Authorization: Bearer" (a LiteLLM virtual key: limits and budgets '
             'are set there). n8n (optional): the signing secret of the X-Woow-Signature HMAC (never sent); '
             'without one requests are not signed, e.g. for a public n8n Chat Trigger. Leave empty to keep the '
             'current key; the saved key is never shown again. Changing the endpoint host or the backend type '
             'removes the saved key, so it is never sent to another server: enter it again.',
    )
    api_key_set = fields.Boolean(string='API Key Configured', compute='_compute_api_key')
    model = fields.Char(string='Model',
                        help='LiteLLM only (required): the LiteLLM model name, which picks the deployment. A '
                             'LiteLLM connection without one is not ready and fails Test Connection.')
    timeout_seconds = fields.Integer(
        string='Timeout (seconds)', default=DEFAULT_TIMEOUT,
        help='Total time allowed for one answer, retries included (5-3600 seconds; 300 by default, 120 for a '
             'new n8n connection). n8n: an answer waits at most 300 seconds (a long workflow uses an '
             'in-cluster URL, or answers {"skip": true} and pushes the reply later).')
    openai_stream = fields.Boolean(
        string='Stream Replies', default=True,
        help='LiteLLM only: ask for a streamed (SSE) answer. Bytes keep flowing while the backend works, so '
             'proxies such as Cloudflare (about 100 seconds without data) do not cut long answers. The call '
             'fails when no data arrives for 30 seconds.')
    max_retries = fields.Integer(
        string='Retries', default=DEFAULT_MAX_RETRIES,
        help='Retries (0-2) when the connection fails or the backend answers HTTP 429 or 503, after 3 '
             'and 10 seconds. Read timeouts and other HTTP errors are never retried: the backend may '
             'already be working on the answer.')
    document_mode = fields.Selection(
        [('file', 'Send the files to the model'),
         ('link', 'Download links only')],
        string='Documents Users Send', default='file', required=True,
        help='LiteLLM only: how the documents users send (PDF, docx, xlsx, xls, csv, txt, md, json) reach the '
             'model. "Send the files": each document is sent as it is, as a file part with its own type '
             '(LiteLLM and OpenAI read PDF, Word and Excel files); Odoo converts nothing. "Download links '
             'only": a signed 1-hour link, for a server that refuses file parts. Images are always sent '
             'inline. Size limits: docs/PROTOCOL.md.')
    download_domains = fields.Text(
        string='Download Domains',
        help='Files linked in AI replies are downloaded and attached only from these hosts, over https '
             '(plain http only from localhost and *.svc.cluster.local hosts listed here); a link with a user '
             'name or a backslash is never downloaded. One per line: an exact host name or *.example.com.',
    )
    max_download_mb = fields.Integer(string='Max File Size (MB)', default=DEFAULT_MAX_DOWNLOAD_MB)
    log_payload = fields.Boolean(
        string='Log Request Content',
        help='Debugging only: store the request and response content in the call logs. '
             'They contain private conversations. Turning it off erases stored content.',
    )

    # --- Customer chats ---
    customer_safe = fields.Boolean(
        string='Safe for Customers', copy=False,
        help='The customer AI chat of a project may use this connection. LiteLLM connections are chat-only '
             '(Odoo sends the conversation, never tools): always safe. n8n: tick it only after checking that '
             'the workflow looks up no company data, calls no tools and keeps no memory across conversations; '
             'it is cleared when the endpoint URL or the backend type changes.')
    readiness = fields.Selection(
        [(READINESS_OK, 'Ready'), (READINESS_BLOCKED, 'Blocked')], string='Readiness',
        compute='_compute_readiness', search='_search_readiness',
        help='Ready: the connection can be used. Blocked: see the details.')
    readiness_reason = fields.Text(string='Readiness Details', compute='_compute_readiness')

    assistant_ids = fields.One2many('woow.project.ai.assistant', 'connection_id', string='AI Assistants',
                                    context={'active_test': False}, readonly=True)
    assistant_count = fields.Integer(compute='_compute_assistant_count')
    call_log_count = fields.Integer(compute='_compute_call_log_count')

    def init(self):
        # SQL-only column: never an ORM field, so never returned by any read.
        self.env.cr.execute("ALTER TABLE woow_project_ai_connection ADD COLUMN IF NOT EXISTS api_key_secret VARCHAR")

    # --- API key (write-only) ---

    def _compute_api_key(self):
        keys = self._get_api_keys()
        for connection in self:
            connection.api_key = False
            connection.api_key_set = bool(keys.get(connection.id))

    def _inverse_api_key(self):
        for connection in self:
            new_key = (connection.api_key or '').strip()
            if new_key:
                connection._check_admin()
                connection._set_api_key(new_key)
        self.invalidate_recordset(['api_key', 'api_key_set', 'readiness', 'readiness_reason'])

    def _set_api_key(self, key):
        """Store (or remove, ``key`` empty) the key in the SQL-only column."""
        self.ensure_one()
        self.flush_recordset()
        self.env.cr.execute("UPDATE woow_project_ai_connection SET api_key_secret = %s WHERE id = %s",
                            (key or None, self.id))
        self.invalidate_recordset(['api_key', 'api_key_set', 'readiness', 'readiness_reason'])
        _logger.info("Project AI connection %s: API key %s by user %s", self.id, 'updated' if key else 'removed',
                     self.env.uid)

    def _get_api_keys(self):
        ids = [i for i in self.ids if isinstance(i, int)]
        if not ids:
            return {}
        self.env.cr.execute("SELECT id, api_key_secret FROM woow_project_ai_connection WHERE id IN %s",
                            (tuple(ids),))
        return dict(self.env.cr.fetchall())

    def _get_api_key(self):
        self.ensure_one()
        return self._get_api_keys().get(self.id) or ''

    def action_clear_api_key(self):
        # Public action_* methods are reachable over XML-RPC/JSON-RPC: return
        # True, never None (XML-RPC cannot marshal None).
        self._check_admin()
        for connection in self:
            connection._set_api_key(False)
        return True

    def _check_admin(self):
        # (a request without a user, uid None: nobody)
        if not self.env.su and not (self.env.uid and self.env.user.has_group('base.group_system')):
            raise AccessError(self.env._('Only system administrators can change AI connections.'))

    # --- Readiness ---

    @api.depends('active', 'backend_type', 'endpoint_url', 'api_key', 'model')
    def _compute_readiness(self):
        keys = self._get_api_keys()
        _ = self.env._
        for connection in self:
            has_key = bool(keys.get(connection._origin.id)) or bool((connection.api_key or '').strip())
            reasons = []
            if not connection.active:
                reasons.append(_('The connection is archived.'))
            if not (connection.endpoint_url or '').strip():
                reasons.append(_('Set the endpoint URL.'))
            if connection.backend_type == BACKEND_LITELLM and not has_key:
                reasons.append(_('Set the API key: LiteLLM connections need one.'))
            if connection.backend_type == BACKEND_LITELLM and not (connection.model or '').strip():
                reasons.append(_('Set the model: LiteLLM picks the deployment by its name.'))
            connection.readiness = READINESS_BLOCKED if reasons else READINESS_OK
            connection.readiness_reason = '\n'.join(reasons) if reasons else _('Ready to use.')

    def _search_readiness(self, operator, value):
        if operator not in ('=', '!=') or value not in (READINESS_OK, READINESS_BLOCKED):
            raise UserError(self.env._('Readiness can only be searched as equal to "ok" or "blocked".'))
        self.flush_model()
        self.env.cr.execute("""
            SELECT id FROM woow_project_ai_connection
             WHERE active AND COALESCE(btrim(endpoint_url), '') <> ''
               AND (backend_type <> %s OR COALESCE(api_key_secret, '') <> '')
               AND (backend_type <> %s OR COALESCE(btrim(model), '') <> '')
        """, (BACKEND_LITELLM, BACKEND_LITELLM))
        ready_ids = [row[0] for row in self.env.cr.fetchall()]
        wants_ready = (operator == '=') == (value == READINESS_OK)
        return [('id', 'in' if wants_ready else 'not in', ready_ids)]

    # --- Constraints ---

    @api.constrains('endpoint_url')
    def _check_endpoint_url(self):
        for connection in self.sudo():
            url = (connection.endpoint_url or '').strip()
            if url and not endpoint_url_allowed(url):
                raise ValidationError(self.env._(
                    'The endpoint URL must use https (plain http is only allowed for localhost '
                    'and *.svc.cluster.local), without a user name or a backslash.'))

    @api.constrains('timeout_seconds', 'max_download_mb', 'max_retries')
    def _check_limits(self):
        for connection in self.sudo():
            if not 5 <= connection.timeout_seconds <= MAX_TIMEOUT:
                raise ValidationError(self.env._('The timeout must be between 5 and 3600 seconds.'))
            if not 1 <= connection.max_download_mb <= MAX_DOWNLOAD_MB:
                raise ValidationError(self.env._('The maximum file size must be between 1 and 100 MB.'))
            if not 0 <= connection.max_retries <= MAX_MAX_RETRIES:
                raise ValidationError(self.env._('Retries must be between 0 and 2.'))

    @api.constrains('backend_type', 'customer_safe')
    def _check_customer_safe(self):
        for connection in self.sudo():
            if connection.backend_type == BACKEND_LITELLM and not connection.customer_safe:
                raise ValidationError(self.env._(
                    'LiteLLM connections are chat-only: they are always safe for customers.'))

    # --- Writes ---

    def _target_changes(self, vals, keep_when_empty=False):
        """True when `vals` points this connection at another server (other
        scheme / host / port, endpoint removed) or changes the backend type.
        ``keep_when_empty``: a first endpoint (none before) is not a change."""
        self.ensure_one()
        connection = self.sudo()
        if 'backend_type' in vals and vals['backend_type'] != connection.backend_type:
            return True
        if 'endpoint_url' in vals:
            old = endpoint_origin(connection.endpoint_url)
            if not old and keep_when_empty:
                return False
            return endpoint_origin(vals['endpoint_url']) != old
        return False

    def _workflow_changes(self, vals):
        """True when `vals` changes the backend type or the endpoint URL itself
        (another n8n workflow may sit behind another path of the same host)."""
        self.ensure_one()
        connection = self.sudo()
        if 'backend_type' in vals and vals['backend_type'] != connection.backend_type:
            return True
        return 'endpoint_url' in vals \
            and (vals['endpoint_url'] or '').strip() != (connection.endpoint_url or '').strip()

    @api.onchange('backend_type')
    def _onchange_backend_type_timeout(self):
        # the form fills in the field default (300): switching to n8n while it is still that default gives n8n's,
        # and back
        for connection in self:
            if connection.backend_type == BACKEND_N8N and connection.timeout_seconds == DEFAULT_TIMEOUT:
                connection.timeout_seconds = DEFAULT_N8N_TIMEOUT
            elif connection.backend_type == BACKEND_LITELLM and connection.timeout_seconds == DEFAULT_N8N_TIMEOUT:
                connection.timeout_seconds = DEFAULT_TIMEOUT

    @api.onchange('backend_type', 'endpoint_url')
    def _onchange_customer_safe(self):
        # the form shows what saving does (write())
        for connection in self:
            origin = connection._origin
            if connection.backend_type == BACKEND_LITELLM:
                connection.customer_safe = True
            elif not origin or origin.backend_type != connection.backend_type \
                    or (origin.endpoint_url or '').strip() != (connection.endpoint_url or '').strip():
                connection.customer_safe = False

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = list(vals_list)  # any iterable (a one-shot generator too): read again below
        for vals in vals_list:
            if vals.get('backend_type', BACKEND_LITELLM) == BACKEND_LITELLM:
                vals['customer_safe'] = True
            elif 'timeout_seconds' not in vals:
                vals['timeout_seconds'] = DEFAULT_N8N_TIMEOUT
        return super().create(vals_list)

    def write(self, vals):
        vals = dict(vals)
        if vals.get('backend_type') == BACKEND_LITELLM:
            vals['customer_safe'] = True
        drop_key = unsafe = self.browse()
        if {'endpoint_url', 'backend_type'} & set(vals):
            keys = self._get_api_keys()
            if not (vals.get('api_key') or '').strip():
                # a key set while the URL was empty survives filling the URL in
                drop_key = self.filtered(lambda c: keys.get(c.id) and c._target_changes(vals, keep_when_empty=True))
            if 'customer_safe' not in vals:
                # an administrator's "safe for customers" was about one n8n workflow
                unsafe = self.filtered(lambda c: c.customer_safe and c._workflow_changes(vals))
        res = super().write(vals)
        for connection in drop_key:
            connection._set_api_key(False)
            _logger.warning("Project AI connection %s: saved API key removed because the endpoint host or the "
                            "backend type changed (user %s)", connection.id, self.env.uid)
        unsafe = unsafe.filtered(lambda c: c.backend_type == BACKEND_N8N)
        if unsafe:
            unsafe.write({'customer_safe': False})
            _logger.warning("Project AI connection(s) %s: no longer safe for customers (the n8n workflow URL or the "
                            "backend type changed; user %s)", unsafe.ids, self.env.uid)
        if 'log_payload' in vals and not vals['log_payload']:
            self.env['woow.project.ai.call.log'].sudo()._erase_payloads(connection_ids=self.ids)
        if {'active', 'timeout_seconds'} & set(vals):
            self.env['woow.project.ai.job']._runner_cache_clear()
        return res

    def copy_data(self, default=None):
        vals_list = super().copy_data(default=default)
        for connection, vals in zip(self, vals_list):
            vals['name'] = self.env._('%s (copy)', connection.name)
            if connection.backend_type == BACKEND_LITELLM:
                vals['customer_safe'] = True
        return vals_list

    # --- Test connection (system administrators) ---

    def action_test_connection(self):
        self.ensure_one()
        return self._test_connection()

    def _test_connection(self, assistant=None):
        """Check the backend without running the AI: LiteLLM ``GET
        {endpoint}/models`` (a chat completion would cost tokens); n8n the
        ping ``{"event": "ping", "connection": …, "action":
        "loadPreviousSession", "sessionId": …}`` (plus ``assistant`` when
        started from an AI assistant), signed only when a signing secret is
        set (a Chat Trigger answers loadPreviousSession without running the
        agent; any HTTP 200 is a success). Only the HTTP status and a short
        sentence are shown, never the upstream answer."""
        self.ensure_one()
        self._check_admin()
        connection = self.sudo()
        _ = self.env._
        api_key = connection._get_api_key()
        endpoint = (connection.endpoint_url or '').strip()
        status = None
        if not endpoint and connection.backend_type == BACKEND_N8N:
            message, ok = _('Set the endpoint URL first.'), False
        elif not endpoint or (connection.backend_type == BACKEND_LITELLM and not api_key):
            message, ok = _('Set the endpoint URL and the API key first.'), False
        elif connection.backend_type == BACKEND_LITELLM and not (connection.model or '').strip():
            message, ok = _('Set the LiteLLM model name first.'), False
        else:
            try:
                if connection.backend_type == BACKEND_LITELLM:
                    response = requests.get(endpoint.rstrip('/') + '/models',
                                            headers={'Authorization': 'Bearer %s' % api_key},
                                            timeout=(CONNECT_TIMEOUT, TEST_TIMEOUT), allow_redirects=False)
                else:
                    ping = {'event': 'ping'}
                    if assistant:
                        ping['assistant'] = {'id': assistant.id, 'name': assistant.name or ''}
                    ping['connection'] = {'id': connection.id, 'name': connection.name or ''}
                    ping['action'] = 'loadPreviousSession'
                    ping['sessionId'] = ('woow-project-ai-ping-%s' % assistant.id if assistant
                                         else 'woow-project-ai-ping-connection-%s' % connection.id)
                    body = _json_dumps(ping).encode()
                    headers = {'Content-Type': 'application/json'}
                    if api_key:
                        headers.update(_signature_headers(api_key, body))
                    response = requests.post(endpoint, data=body, headers=headers,
                                             timeout=(CONNECT_TIMEOUT, TEST_TIMEOUT), allow_redirects=False)
                status = response.status_code
                response.close()
            except requests.Timeout:
                message, ok = _('The backend did not answer within %s seconds.', TEST_TIMEOUT), False
            except requests.RequestException:
                message, ok = _('Could not connect to the backend. Check the URL and the network policy.'), False
            if status is not None:
                ok = status == 200
                if ok:
                    message = _('Connection OK (HTTP %s).', status)
                elif status in (401, 403):
                    message = _('The backend refused the key or the signature (HTTP %s).', status)
                elif status == 404:
                    message = _('Endpoint not found (HTTP %s). Check the URL.', status)
                else:
                    message = _('The backend answered HTTP %s.', status)
        _logger.info("Project AI connection %s: connection test by user %s: HTTP %s", connection.id, self.env.uid,
                     status)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Test Connection'),
                'message': message,
                'type': 'success' if ok else 'danger',
                'sticky': False,
            },
        }

    # --- Assistants and call logs ---

    def _compute_assistant_count(self):
        data = self.env['woow.project.ai.assistant'].sudo().with_context(active_test=False)._read_group(
            [('connection_id', 'in', self.ids)], ['connection_id'], ['__count'])
        counts = {connection.id: count for connection, count in data}
        for connection in self:
            connection.assistant_count = counts.get(connection.id, 0)

    def _compute_call_log_count(self):
        data = self.env['woow.project.ai.call.log'].sudo()._read_group(
            [('connection_id', 'in', self.ids)], ['connection_id'], ['__count'])
        counts = {connection.id: count for connection, count in data}
        for connection in self:
            connection.call_log_count = counts.get(connection.id, 0)

    def action_view_assistants(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('woow_ai_task.action_woow_project_ai_assistant')
        action['domain'] = [('connection_id', '=', self.id)]
        action['context'] = {'default_connection_id': self.id, 'active_test': False}
        return action

    def action_view_call_logs(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('woow_ai_task.action_woow_project_ai_call_log')
        action['domain'] = [('connection_id', '=', self.id)]
        return action
