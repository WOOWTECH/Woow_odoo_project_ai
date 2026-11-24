# -*- coding: utf-8 -*-
"""Shared fixtures of the project AI tests (no real HTTP, no real key: every
request is mocked)."""

import base64
import json
import socket
import threading
import time
from contextlib import nullcontext
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError, ProtocolError

from odoo import Command, fields
from odoo.tests.common import TransactionCase, new_test_user

BACKEND_POST = 'odoo.addons.woow_ai_task.models.ai_assistant_backend.requests.post'
BACKEND_GET = 'odoo.addons.woow_ai_task.models.ai_assistant_backend.requests.get'
CONNECTION_GET = 'odoo.addons.woow_ai_task.models.ai_connection.requests.get'
CONNECTION_POST = 'odoo.addons.woow_ai_task.models.ai_connection.requests.post'
MEDIA_GET = 'odoo.addons.woow_ai_task.tools.media._http_get'
BACKEND_SLEEP = 'odoo.addons.woow_ai_task.models.ai_assistant_backend.time.sleep'

TEST_KEY = 'test-backend-key'  # placeholder, not a real key

CONNECTION_FIELDS = ('backend_type', 'endpoint_url', 'api_key', 'model', 'timeout_seconds', 'max_retries',
                     'openai_stream', 'document_mode', 'log_payload', 'download_domains', 'max_download_mb',
                     'customer_safe')

# 1x1 transparent PNG
TINY_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==')
TINY_PDF = b'%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n'


def make_assistant(env, connection=None, **vals):
    """Project AI assistant for tests. Connection fields among `vals` go to its
    own new AI connection (or are written on `connection`, shared, when given)."""
    connection_vals = {key: vals.pop(key) for key in list(vals) if key in CONNECTION_FIELDS}
    if not connection:
        connection_vals.setdefault('name', '%s connection' % vals.get('name', 'AI'))
        connection = env['woow.project.ai.connection'].create(connection_vals)
    elif connection_vals:
        connection.write(connection_vals)
    return env['woow.project.ai.assistant'].create(dict(vals, connection_id=connection.id))


def http_response(status=200, json_data=None, text=None, headers=None, chunks=None):
    response = MagicMock()
    response.status_code = status
    response.headers = headers or {}
    if json_data is not None:
        response.json.return_value = json_data
        response.text = json.dumps(json_data)
    else:
        response.json.side_effect = ValueError('not json')
        response.text = text or ''
    response.iter_content.return_value = chunks or []
    return response


def openai_ok(content='您好，我是 AI。', usage=None):
    data = {'choices': [{'message': {'role': 'assistant', 'content': content}}]}
    if usage:
        data['usage'] = usage
    return http_response(200, data)


def sse_response(chunks, status=200, error=None):
    """Streamed (SSE) answer; `chunks` are str/bytes pieces of the raw body.
    `error` is raised by iter_content after the chunks (e.g. an idle timeout)."""
    response = MagicMock()
    response.status_code = status
    response.headers = {'Content-Type': 'text/event-stream; charset=utf-8'}
    raw = [c.encode() if isinstance(c, str) else c for c in chunks]

    def iter_content(chunk_size=None):
        yield from raw
        if error:
            raise error

    response.iter_content.side_effect = iter_content
    response.json.side_effect = ValueError('not json')
    return response


def sse_event(data, event=None):
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return ('event: %s\n' % event if event else '') + 'data: %s\n\n' % payload


def delta(content, finish_reason=None, **extra):
    choice = {'index': 0, 'delta': {'content': content} if content is not None else {}}
    if finish_reason:
        choice['finish_reason'] = finish_reason
    return dict({'choices': [choice]}, **extra)


def refused():
    """requests' error for a connection that could not be opened (nothing sent)."""
    return requests.ConnectionError(MaxRetryError(None, '/v1/chat/completions',
                                                  NewConnectionError(None, 'Connection refused')))


def reset_after_send():
    """requests' error when the backend got the request and then reset the connection."""
    return requests.ConnectionError(ProtocolError('Connection aborted.', ConnectionResetError(104, 'reset')))


class LocalServer:
    """Raw TCP server on 127.0.0.1 (the only host tests may reach) for wire
    level cases a mock cannot show. ``handler(conn, request_bytes)`` answers
    one connection; ``requests`` counts the requests received."""

    def __init__(self, handler):
        self.handler = handler
        self.requests = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.url = 'http://127.0.0.1:%s' % self.port
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(5)
        self.sock.close()
        return False

    @staticmethod
    def read_request(conn):
        conn.settimeout(5)
        data = b''
        while b'\r\n\r\n' not in data:
            chunk = conn.recv(65536)
            if not chunk:
                return data
            data += chunk
        head, _sep, body = data.partition(b'\r\n\r\n')
        length = 0
        for line in head.split(b'\r\n')[1:]:
            name, _sep, value = line.partition(b':')
            if name.strip().lower() == b'content-length':
                length = int(value.strip())
        while len(body) < length:
            chunk = conn.recv(65536)
            if not chunk:
                break
            body += chunk
        return head + b'\r\n\r\n' + body

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _addr = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            request = self.read_request(conn)
            self.requests.append(request)
            self.handler(conn, request)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def free_port():
    """A local port nobody listens on (connection refused)."""
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def slow_sse_handler(seconds, interval=0.2):
    """Non-chunked SSE answer (HTTP/1.0 style: no Content-Length, closed at the
    end) that only sends small keepalives for `seconds`."""
    def handler(conn, _request):
        conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n')
        end = time.time() + seconds
        while time.time() < end:
            conn.sendall(b': keepalive\n\n')
            time.sleep(interval)
    return handler


def run_after_commit_usage(test, request):
    """Run `request()` (an HTTP call) and then what its commit would run for
    the usage (``woow.project.ai.usage.month._add_for_channel_after_commit``):
    test cursors drop post-commit hooks; the count's own READ COMMITTED cursor
    is the test cursor here."""
    from odoo.addons.woow_ai_task.models import ai_job  # noqa: PLC0415
    Callbacks = type(test.env.cr.postcommit)
    added = []
    original = Callbacks.add

    def capture(callbacks, func):
        if getattr(func, 'woow_project_ai_usage', None):
            added.append(func)
        return original(callbacks, func)

    with patch.object(Callbacks, 'add', capture):
        result = request()
    with patch.object(ai_job, '_job_cursor', lambda db_name: nullcontext(test.env.cr)):
        for func in added:
            func()
    test.env['woow.project.ai.usage.month'].invalidate_model()
    return result, added


class AiTaskFixtures:
    """Users, AI assistants, a portal project and a task followed by a customer."""

    @classmethod
    def setup_ai_task(cls):
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.env['ir.config_parameter'].sudo().set_param('web.base.url', 'https://odoo.example.com')
        cls.manager = new_test_user(cls.env, 'aitask_manager', name='Manager Mia', email='mia@example.com',
                                    groups='base.group_user,project.group_project_manager')
        cls.staff = new_test_user(cls.env, 'aitask_staff', name='Staff Sam', email='sam@example.com',
                                  groups='base.group_user,project.group_project_user')
        cls.staff2 = new_test_user(cls.env, 'aitask_staff2', name='Staff Tina', email='tina@example.com',
                                   groups='base.group_user,project.group_project_user')
        cls.employee = new_test_user(cls.env, 'aitask_employee', name='Employee Eve', email='eve@example.com',
                                     groups='base.group_user')
        cls.customer = new_test_user(cls.env, 'aitask_customer', name='Customer Carl', email='carl@example.com',
                                     groups='base.group_portal')
        cls.customer2 = new_test_user(cls.env, 'aitask_customer2', name='Customer Cora', email='cora@example.com',
                                      groups='base.group_portal')
        cls.outsider = new_test_user(cls.env, 'aitask_outsider', name='Outsider Otto', email='otto@example.com',
                                     groups='base.group_portal')
        # internal tab: a LiteLLM connection
        cls.internal_ai = make_assistant(
            cls.env, name='Internal Helper', backend_type='openai', endpoint_url='https://llm.example.com/v1',
            api_key=TEST_KEY, model='internal-model', openai_stream=False, system_prompt='You help the team.')
        # customer: a LiteLLM connection (chat-only: always safe for customers)
        cls.customer_ai = make_assistant(
            cls.env, name='Customer Helper', backend_type='openai', endpoint_url='https://llm-public.example.com/v1',
            api_key=TEST_KEY, model='public-model', openai_stream=False, system_prompt='You help customers.')
        # an n8n workflow an administrator checked and marked safe for customers
        cls.customer_n8n = make_assistant(
            cls.env, name='Customer Workflow', backend_type='n8n', endpoint_url='https://n8n.example.com/webhook/c',
            api_key=TEST_KEY, customer_safe=True)
        # an n8n workflow with data tools: internal only
        cls.internal_n8n = make_assistant(
            cls.env, name='Internal Workflow', backend_type='n8n', endpoint_url='https://n8n.example.com/webhook/i',
            api_key=TEST_KEY)
        cls.tag = cls.env['project.tags'].create({'name': 'Urgent Kitchen'})
        cls.project = cls.env['project.project'].create({
            'name': 'Kitchen Renovation',
            'privacy_visibility': 'portal',
            'user_id': cls.manager.id,
            'woow_project_ai_internal_assistant_id': cls.internal_ai.id,
            'woow_project_ai_customer_assistant_id': cls.customer_ai.id,
            'woow_project_ai_internal_brief': 'INTERNAL BRIEF: our margin is 30 percent.',
            'woow_project_ai_customer_brief': 'CUSTOMER BRIEF: we install kitchens in Taipei.',
            'woow_project_ai_customer_welcome': '您好，我是客戶 AI 助理。',
        })
        cls.stage = cls.env['project.task.type'].create({'name': 'In Progress', 'project_ids': [Command.link(
            cls.project.id)]})
        cls.task = cls.env['project.task'].create({
            'name': 'Install cabinets',
            'project_id': cls.project.id,
            'stage_id': cls.stage.id,
            'user_ids': [Command.set([cls.staff.id])],
            'description': '<p>Cabinets on the <b>north</b> wall.</p>',
            'tag_ids': [Command.set([cls.tag.id])],
            'date_deadline': fields.Datetime.now() + timedelta(days=7),
        })
        cls.task.message_subscribe(partner_ids=(cls.customer.partner_id | cls.customer2.partner_id).ids)
        cls.Job = cls.env['woow.project.ai.job']

    # --- helpers ---

    def open_tab(self, user, tab, task=None):
        result = (task or self.task).with_user(user).woow_project_ai_open_tab(tab)
        return self.env['discuss.channel'].browse(result['channel_id'])

    def post(self, channel, user, body='hello', attachments=None):
        # as /mail/message/post does: the poster's environment, then sudo
        return channel.with_user(user).sudo().message_post(
            body=body, message_type='comment', subtype_xmlid='mail.mt_comment',
            attachment_ids=(attachments or self.env['ir.attachment']).ids)

    def pending_attachment(self, user, name='報價.pdf', raw=None, mimetype=None):
        """A file uploaded in the composer, not posted yet (as the upload route makes it)."""
        vals = {'name': name, 'raw': raw or TINY_PDF, 'res_model': 'mail.compose.message', 'res_id': 0}
        if mimetype:
            vals['mimetype'] = mimetype
        return self.env['ir.attachment'].with_user(user).sudo().create(vals)

    def jobs(self, channel):
        return self.Job.search([('channel_id', '=', channel.id)], order='id')

    def run_job(self, job):
        job._run_job(commit=False)
        return job

    def due_all(self, channel):
        self.jobs(channel).write({'run_after': fields.Datetime.now() - timedelta(seconds=5)})
        self.env.flush_all()

    def run_due(self, channel, limit=10):
        """Run the due jobs of `channel` in the dispatcher's order."""
        self.due_all(channel)
        for _i in range(limit):
            job_id = self.Job._due_job_id(channel.id)
            if not job_id:
                break
            self.run_job(self.Job.browse(job_id))

    def members(self, channel):
        channel.invalidate_recordset(['channel_member_ids'])
        return channel.sudo().channel_member_ids.partner_id

    def bot_messages(self, channel, assistant=None):
        assistant = assistant or channel._woow_project_ai_tab_assistant()
        return self.env['mail.message'].sudo().search([
            ('model', '=', 'discuss.channel'), ('res_id', '=', channel.id),
            ('author_id', '=', assistant.partner_id.id)], order='id')

    @staticmethod
    def sent(post, index=-1):
        """JSON body of a mocked backend call."""
        return json.loads(post.call_args_list[index].kwargs['data'])


class ProjectAiCase(AiTaskFixtures, TransactionCase):
    """The core's tests (ported from woow_ai_contact): `self.chat(user)` is the
    internal AI chat of the task opened by `user`, answered by `self.assistant`
    (LiteLLM); `self.chat(user, self.n8n)` answers with an n8n assistant (the
    internal one, or the customer one for a portal user)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_ai_task()
        cls.internal = cls.staff
        cls.portal = cls.customer
        cls.assistant = cls.internal_ai
        cls.assistant.connection_id.write({'download_domains': 'files.example.com\n*.cdn.example.com',
                                           'openai_stream': True, 'model': 'test-model'})
        cls.assistant.system_prompt = 'You are helpful.'
        cls.connection = cls.assistant.connection_id
        # an n8n workflow marked safe for customers: usable on both tabs
        cls.n8n = cls.customer_n8n
        cls.n8n.connection_id.download_domains = 'files.example.com'
        cls.n8n_connection = cls.n8n.connection_id

    def chat(self, user, assistant=None):
        """The task AI chat of `user` answered by `assistant` (default: the
        LiteLLM one): the internal chat for an employee, the customer chat
        for a portal user."""
        assistant = assistant or self.assistant
        tab = 'internal' if user._is_internal() else 'customer'
        field = 'woow_project_ai_%s_assistant_id' % tab
        if self.project[field] != assistant:
            self.project[field] = assistant
        return self.open_tab(user, tab)

    def other_chat(self, user, assistant, tab='internal'):
        """The `tab` chat of a task of another project, answered by `assistant`
        (a task has one chat per tab: another assistant needs another task)."""
        field = 'woow_project_ai_%s_assistant_id' % tab
        project = self.env['project.project'].create({'name': 'Other %s' % assistant.name,
                                                      'privacy_visibility': 'portal', field: assistant.id})
        task = self.env['project.task'].create({'name': 'Other task', 'project_id': project.id,
                                                'user_ids': [Command.set([self.staff.id])]})
        if tab == 'customer':
            task.message_subscribe(partner_ids=user.partner_id.ids)
        return self.open_tab(user, tab, task=task)

    def attachment(self, chat, user, name='a.png', raw=TINY_PNG, mimetype=None):
        # pending upload (as the composer does): message_post links it to the channel
        return self.pending_attachment(user, name=name, raw=raw, mimetype=mimetype)

    def post(self, chat, user, body='hello', attachments=None, message_type='comment'):
        return chat.with_user(user).sudo().message_post(
            body=body, message_type=message_type, subtype_xmlid='mail.mt_comment',
            attachment_ids=(attachments or self.env['ir.attachment']).ids)
