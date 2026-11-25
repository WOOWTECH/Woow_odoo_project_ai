# -*- coding: utf-8 -*-
"""Fixtures of the PaaS bridge tests (no real HTTP to any backend, no real key).

Users: B an employee only (base.group_user), U a project user (not a
manager), M a project manager (a characterization case: the stock rules keep
everything for M), A a system administrator (with Project rights but not a
project manager, and a member of W: on the platform an administrator reads a
bound project only as a workspace member, like any employee), C a portal user
who follows task T and is a member of workspace W, C2 a portal user who
follows T and is not a member; outsiders X (portal) and E (employee, no
workspace).

Data: workspace W with service S; project P bound to S, shared with portal
users, both AI tabs on LiteLLM stub connections (always safe for customers);
task T in P. The old task AI seed: a project with ``use_agent_runtime``, a
``reaped`` session with a fake hash, a task with ``chat_enabled`` and its
"Task Chat: ..." channel holding 2 messages, and a task whose ``channel_id``
points to an ordinary company channel.

The database may be a copy of a site: its own agent sessions are marked
absent inside each test class's transaction (rolled back afterwards), so
that "live" means the test's own sessions; the assertions on old chats and
records look at the test's own ids.
"""

import json
import uuid
from contextlib import contextmanager
from unittest.mock import patch

from odoo import Command
from odoo.tests.common import new_test_user
from odoo.tools import SQL

from odoo.addons.woow_ai_task.tests.common import TEST_KEY, make_assistant

LEGACY_PARAM = 'woow_ai_task_paas.legacy_task_ai'
RESTRICTED_PARAM = 'woow_ai_task_paas.restricted_chats'
PARKED_PARAM = 'woow_ai_task_paas.parked_crons'
SESSION = 'woow_paas_platform.agent_session'
LEGACY_CRONS = (
    'woow_paas_platform.ir_cron_agent_session_drift',
    'woow_paas_platform.ir_cron_agent_session_idle_reaper',
    'woow_paas_platform.ir_cron_agent_session_gc',
    'woow_paas_platform.ir_cron_reach_reconciler',
)
REFUSAL_ZH = '舊版任務 AI 已停用。客戶請在任務頁使用「客戶 AI 對話」；同事請在後台任務表單使用「內部 AI 對話」。'


class PaasBridgeFixtures:

    @classmethod
    def setup_paas(cls):
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.env['ir.config_parameter'].sudo().set_param(LEGACY_PARAM, False)
        Session = cls.env[SESSION]
        cls.env.cr.execute(SQL("UPDATE %s SET state = 'absent' WHERE state != 'absent'",
                               SQL.identifier(Session._table)))
        Session.invalidate_model(['state'])

        def user(login, name, groups):
            return new_test_user(cls.env, login, name=name, email='%s@example.com' % login, groups=groups)

        cls.user_b = user('paasb_ben', 'Employee Ben', 'base.group_user')
        cls.user_u = user('paasb_una', 'Project User Una', 'base.group_user,project.group_project_user')
        cls.user_m = user('paasb_max', 'Project Manager Max', 'base.group_user,project.group_project_manager')
        cls.user_a = user('paasb_ada', 'Administrator Ada',
                          'base.group_user,base.group_system,project.group_project_user')
        cls.user_c = user('paasb_cleo', 'Customer Cleo', 'base.group_portal')
        cls.user_c2 = user('paasb_cora', 'Customer Cora', 'base.group_portal')
        cls.user_x = user('paasb_xena', 'Outsider Xena', 'base.group_portal')
        cls.user_e = user('paasb_eric', 'Employee Eric', 'base.group_user')
        cls.owner = user('paasb_owen', 'Workspace Owner Owen', 'base.group_user')

        Workspace = cls.env['woow_paas_platform.workspace'].sudo()
        cls.workspace = Workspace.create({'name': 'W bridge %s' % uuid.uuid4().hex[:6], 'owner_id': cls.owner.id})
        Access = cls.env['woow_paas_platform.workspace_access'].sudo()
        cls.access = {
            key: Access.create({'workspace_id': cls.workspace.id, 'user_id': member.id, 'role': 'user'})
            for key, member in (('b', cls.user_b), ('u', cls.user_u), ('c', cls.user_c), ('a', cls.user_a))
        }
        cls.template = cls.env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name': 'Bridge app', 'slug': 'bridge-%s' % uuid.uuid4().hex[:6], 'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/bridge', 'helm_chart_version': '1.0.0',
            'workload_type': 'container', 'post_deploy_init_type': 'none', 'mcp_enabled': False,
        })
        cls.service = cls.make_service(cls.workspace, 'S bridge')

        cls.internal_ai = make_assistant(
            cls.env, name='PaaS Internal Helper', backend_type='openai', endpoint_url='https://llm.example.com/v1',
            api_key=TEST_KEY, model='internal-model', openai_stream=False)
        cls.customer_ai = make_assistant(
            cls.env, name='PaaS Customer Helper', backend_type='openai',
            endpoint_url='https://llm-public.example.com/v1', api_key=TEST_KEY, model='public-model',
            openai_stream=False)
        cls.project = cls.env['project.project'].sudo().create({
            'name': 'Support P',
            'privacy_visibility': 'portal',
            'cloud_service_ids': [Command.set(cls.service.ids)],
            'woow_project_ai_internal_assistant_id': cls.internal_ai.id,
            'woow_project_ai_customer_assistant_id': cls.customer_ai.id,
        })
        cls.task = cls.env['project.task'].sudo().create({'name': 'Task T', 'project_id': cls.project.id})
        cls.task.message_subscribe(partner_ids=(cls.user_c.partner_id | cls.user_c2.partner_id).ids)

    @classmethod
    def make_service(cls, workspace, name, **extra):
        from odoo.addons.woow_paas_platform.models.cloud_service import _internal_provision  # noqa: PLC0415
        vals = {
            'name': name, 'workspace_id': workspace.id, 'template_id': cls.template.id, 'state': 'running',
            'helm_namespace': 'paas-ws-test-bridge', 'helm_release_name': 'svc-bridge-%s' % uuid.uuid4().hex[:6],
            'helm_values': json.dumps({'config': {'nonSensitive': {}}}),
        }
        vals.update(extra)
        return _internal_provision(cls.env['woow_paas_platform.cloud_service'].sudo()).create(vals)

    @classmethod
    def setup_legacy_seed(cls):
        """The old task AI's data, as staging has it (written in SQL where the
        bridge refuses the value: that is what the bridge must cope with)."""
        Project = cls.env['project.project'].sudo()
        cls.legacy_project = Project.create({'name': 'Legacy runtime project', 'privacy_visibility': 'portal'})
        cls.env.cr.execute("UPDATE project_project SET use_agent_runtime = TRUE WHERE id = %s",
                           [cls.legacy_project.id])
        cls.legacy_project.invalidate_recordset(['use_agent_runtime'])
        Session = cls.env[SESSION].sudo()
        with patch.object(type(Session), '_get_namespace_secret', return_value=b'bridge-test-secret'):
            cls.legacy_session = Session.create({
                'project_id': cls.legacy_project.id, 'state': 'reaped',
                'project_namespace_hash': 'fa%s' % uuid.uuid4().hex[:14]})
        Channel = cls.env['discuss.channel'].sudo()
        cls.legacy_task = cls.env['project.task'].sudo().create({
            'name': 'Legacy chat task', 'project_id': cls.legacy_project.id,
            'user_ids': [Command.set(cls.user_b.ids)]})
        cls.legacy_chat = Channel.create({
            'name': 'Task Chat: Legacy chat task', 'channel_type': 'channel',
            'channel_member_ids': [Command.create({'partner_id': cls.user_b.partner_id.id})]})
        for body in ('legacy question', 'legacy answer'):
            cls.legacy_chat.message_post(body=body, message_type='comment', subtype_xmlid='mail.mt_comment')
        cls.company_channel = Channel.create({'name': 'Company news', 'channel_type': 'channel'})
        cls.pointer_task = cls.env['project.task'].sudo().create({
            'name': 'Task pointing to a company channel', 'project_id': cls.legacy_project.id})
        cls.env.cr.execute("UPDATE project_task SET chat_enabled = TRUE, channel_id = %s WHERE id = %s",
                           [cls.legacy_chat.id, cls.legacy_task.id])
        cls.env.cr.execute("UPDATE project_task SET channel_id = %s WHERE id = %s",
                           [cls.company_channel.id, cls.pointer_task.id])
        (cls.legacy_task | cls.pointer_task).invalidate_recordset(['chat_enabled', 'channel_id'])

    # --- helpers ---

    def set_legacy(self, on):
        self.env['ir.config_parameter'].sudo().set_param(LEGACY_PARAM, 'True' if on else False)

    @contextmanager
    def legacy_on(self):
        self.set_legacy(True)
        try:
            yield
        finally:
            self.set_legacy(False)

    def open_tab(self, user, tab, task=None):
        result = (task or self.task).with_user(user).woow_project_ai_open_tab(tab)
        return self.env['discuss.channel'].browse(result['channel_id'])

    def members(self, channel):
        channel.invalidate_recordset(['channel_member_ids'])
        return channel.sudo().channel_member_ids.partner_id


class RpcMixin:
    """JSON-RPC over the test HTTP server, as the browser sends it."""

    def json_rpc(self, url, params=None):
        response = self.url_open(url, data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params or {},
                                                       'id': 1}),
                                 headers={'Content-Type': 'application/json'}, timeout=30)
        self.env.invalidate_all()
        return response

    def call_kw(self, model, method, args, kwargs=None):
        response = self.json_rpc('/web/dataset/call_kw', {'model': model, 'method': method, 'args': args,
                                                          'kwargs': kwargs or {}})
        return response.json()

    @staticmethod
    def error_of(result):
        data = (result.get('error') or {}).get('data') or {}
        return '%s %s' % (data.get('name', ''), data.get('message', ''))
