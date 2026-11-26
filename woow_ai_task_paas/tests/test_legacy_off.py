# -*- coding: utf-8 -*-
"""The old platform task AI while the switch is off (the default)."""

import re
from types import SimpleNamespace
from unittest.mock import patch

from lxml import etree

from odoo.exceptions import UserError
from odoo.modules.neutralize import get_neutralization_queries
from odoo.tests.common import HttpCase, TransactionCase, tagged

from odoo.addons.woow_ai_task_paas.models.legacy import legacy_rule
from odoo.addons.woow_paas_platform.services.agent_runtime_client import AgentRuntimeClient

from .common import LEGACY_PARAM, REFUSAL_ZH, PaasBridgeFixtures, RpcMixin

REFUSAL_EN = 'The old task AI is turned off.'
JSON_ROUTES = ('/api/ai/agents', '/api/ai/chat/history', '/api/ai/chat/post', '/api/ai/connection-status',
               '/api/ai/agent-status', '/api/ai/agent-pod-status')
AUTO_ENABLE = 'odoo.addons.woow_paas_platform.controllers.ai_assistant._agent_runtime_autoenable_ready'


def load_zh_tw(env):
    env['res.lang']._activate_lang('zh_TW')
    env['ir.module.module']._load_module_terms(['woow_ai_task_paas'], ['zh_TW'], overwrite=True)


@tagged('post_install', '-at_install')
class TestLegacyOffOrm(PaasBridgeFixtures, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_paas()
        cls.setup_legacy_seed()

    # --- use_agent_runtime ---

    def test_create_forces_the_runtime_off(self):
        Project = self.env['project.project'].sudo()
        self.assertFalse(Project.create({'name': 'Explicit', 'use_agent_runtime': True}).use_agent_runtime)
        self.assertFalse(Project.with_context(default_use_agent_runtime=True).create({'name': 'Default'})
                         .use_agent_runtime)
        self.env['ir.default'].set('project.project', 'use_agent_runtime', True)
        self.assertFalse(Project.create({'name': 'ir.default'}).use_agent_runtime)

    def test_copy_forces_the_runtime_off(self):
        self.assertTrue(self.legacy_project.use_agent_runtime)
        self.assertFalse(self.legacy_project.sudo().copy().use_agent_runtime)

    def test_turning_it_on_is_refused_outside_superuser(self):
        # a system administrator (the platform's own guard lets them), on a project they created (they read it)
        project = self.env['project.project'].with_user(self.user_a).create({'name': 'Off'})
        with self.assertRaisesRegex(UserError, 'The old task AI is turned off: the Agent Runtime cannot be turned'):
            project.with_user(self.user_a).write({'use_agent_runtime': True})
        self.assertFalse(project.use_agent_runtime)

    def test_superuser_write_is_corrected_and_logged(self):
        project = self.env['project.project'].sudo().create({'name': 'Off'})
        with self.assertLogs('odoo.addons.woow_ai_task_paas.models.project_project', 'WARNING') as logs:
            project.sudo().write({'use_agent_runtime': True, 'description': 'kept'})
        self.assertFalse(project.use_agent_runtime)
        self.assertIn('kept', str(project.description))
        self.assertIn(str(project.id), '\n'.join(logs.output))

    def test_true_to_true_and_off_pass(self):
        mixed = self.legacy_project | self.env['project.project'].sudo().create({'name': 'Off'})
        mixed.sudo().write({'use_agent_runtime': True})
        self.assertEqual(mixed.mapped('use_agent_runtime'), [True, False])
        self.legacy_project.with_user(self.user_m).write({'use_agent_runtime': True})  # True -> True, not superuser
        self.legacy_project.sudo().write({'use_agent_runtime': False})
        self.assertFalse(self.legacy_project.use_agent_runtime)

    # --- chat_enabled ---

    def test_chat_enabled_never_turns_on(self):
        Channel = self.env['discuss.channel'].sudo().with_context(active_test=False)
        before = Channel.search_count([])
        task = self.env['project.task'].sudo().create({'name': 'Chat?', 'project_id': self.legacy_project.id,
                                                        'chat_enabled': True})
        self.assertFalse(task.chat_enabled)
        task.write({'chat_enabled': True, 'description': 'still written'})
        self.assertFalse(task.chat_enabled)
        self.assertIn('still written', str(task.description))
        task.with_context(default_chat_enabled=True).write({'chat_enabled': True})
        self.assertFalse(task.chat_enabled)
        task2 = self.env['project.task'].sudo().with_context(default_chat_enabled=True).create({'name': 'Default'})
        self.assertFalse(task2.chat_enabled)
        self.assertFalse(task._create_chat_channel())
        self.assertFalse(task.channel_id)
        self.assertEqual(Channel.search_count([]), before)

    def test_existing_channel_is_returned_never_created(self):
        self.assertEqual(self.legacy_task._create_chat_channel(), self.legacy_chat)

    # --- backend forms, in code ---

    def test_task_form_hides_the_old_chat(self):
        load_zh_tw(self.env)
        arch = etree.fromstring(self.env['project.task'].with_context(lang='zh_TW').get_view(
            view_type='form')['arch'])
        for xpath in ("//group[field[@name='chat_enabled']]", "//group[field[@name='channel_id']]"):
            [group] = arch.xpath(xpath)
            self.assertEqual(group.get('invisible'), '1', xpath)
        [page] = arch.xpath("//page[@name='ai_chat']")
        self.assertEqual(page.get('string'), '關聯雲端服務')
        # the page keeps the service fields
        self.assertTrue(page.xpath(".//field[@name='primary_cloud_service_id']"))

    def test_project_form_hides_the_runtime(self):
        arch = etree.fromstring(self.env['project.project'].get_view(view_type='form')['arch'])
        [field] = arch.xpath("//field[@name='use_agent_runtime']")
        self.assertEqual(field.get('invisible'), '1')

    def test_view_cache_key_carries_the_switch(self):
        for model in ('project.task', 'project.project'):
            off = self.env[model]._get_view_cache_key(view_type='form')
            self.env['ir.config_parameter'].sudo().set_param(LEGACY_PARAM, 'True')
            on = self.env[model]._get_view_cache_key(view_type='form')
            self.env['ir.config_parameter'].sudo().set_param(LEGACY_PARAM, False)
            self.assertEqual(len(off), len(on), model)
            # the switch is the only part of the key that changes (the project's also carries the user's right)
            self.assertEqual([(a, b) for a, b in zip(off, on) if a != b], [(False, True)], model)

    def test_every_route_under_the_old_prefix_is_the_old_task_ai(self):
        """A route the platform adds under /api/ai/ is refused before a release of this module knows it."""
        def rule(url):
            return SimpleNamespace(rule=url, endpoint=SimpleNamespace(routing={'type': 'json'}))
        self.assertTrue(legacy_rule(rule('/api/ai/chat/history')))
        self.assertTrue(legacy_rule(rule('/api/ai/a-route-added-later')))
        self.assertFalse(legacy_rule(rule('/api/support/tasks/detail/<int:task_id>')))
        self.assertFalse(legacy_rule(rule('/woow')))

    # --- neutralize ---

    def test_neutralize_clears_the_switch(self):
        self.env['ir.config_parameter'].sudo().set_param(LEGACY_PARAM, 'True')
        self.env.flush_all()
        [query] = list(get_neutralization_queries(['woow_ai_task_paas']))
        self.env.cr.execute(query)
        self.env['ir.config_parameter'].invalidate_model()
        self.env.registry.clear_cache()
        self.assertFalse(self.env['ir.config_parameter'].sudo().get_param(LEGACY_PARAM))


@tagged('post_install', '-at_install')
class TestLegacyOffHttp(PaasBridgeFixtures, RpcMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_paas()
        cls.setup_legacy_seed()
        # C reads the old project and its chat task on /my
        cls.legacy_project.message_subscribe(partner_ids=cls.user_c.partner_id.ids)
        cls.legacy_task.message_subscribe(partner_ids=cls.user_c.partner_id.ids)

    def csrf_token(self):
        for page in ('/my', '/odoo'):
            response = self.url_open(page, timeout=30)
            found = re.search(r'csrf_token[\"\']?\s*[:=]\s*[\"\']([^\"\']+)[\"\']', response.text)
            if found:
                return found.group(1)
        self.fail('no csrf token found')

    def legacy_counts(self):
        Message = self.env['mail.message'].sudo()
        return (Message.search_count([('model', '=', 'discuss.channel'), ('res_id', '=', self.legacy_chat.id)]),
                self.env['ir.attachment'].sudo().search_count([('res_model', '=', 'discuss.channel'),
                                                               ('res_id', '=', self.legacy_chat.id)]))

    def test_json_routes_are_refused(self):
        self.authenticate(self.user_b.login, self.user_b.login)
        before = self.legacy_counts()
        params = {'channel_id': self.legacy_chat.id, 'body': 'a new question', 'project_id': self.legacy_project.id}
        with patch.object(AgentRuntimeClient, 'spawn_or_attach') as spawn:
            for url in JSON_ROUTES:
                result = self.json_rpc(url, params).json()
                self.assertNotIn('result', result, url)
                self.assertIn(REFUSAL_EN, self.error_of(result), url)
                self.assertIn('UserError', self.error_of(result), url)
        spawn.assert_not_called()
        self.assertEqual(self.legacy_counts(), before)

    def test_refusal_in_the_users_language(self):
        load_zh_tw(self.env)
        self.user_b.lang = 'zh_TW'
        self.authenticate(self.user_b.login, self.user_b.login)
        result = self.json_rpc('/api/ai/agents', {}).json()
        self.assertIn(REFUSAL_ZH, self.error_of(result))

    def test_upload_and_stream_are_gone(self):
        self.authenticate(self.user_b.login, self.user_b.login)
        token = self.csrf_token()
        before = self.legacy_counts()
        with patch.object(AgentRuntimeClient, 'spawn_or_attach') as spawn:
            upload = self.url_open('/api/ai/chat/upload',
                                   data={'channel_id': str(self.legacy_chat.id), 'csrf_token': token},
                                   files={'file': ('note.txt', b'hello', 'text/plain')}, timeout=30)
            stream = self.url_open('/api/ai/stream/%s?csrf_token=%s' % (self.legacy_chat.id, token), timeout=30)
        self.assertEqual(upload.status_code, 410)
        self.assertEqual(stream.status_code, 410)
        spawn.assert_not_called()
        self.assertEqual(self.legacy_counts(), before)

    def test_woow_project_create_does_not_auto_enable(self):
        self.authenticate(self.user_a.login, self.user_a.login)
        with patch(AUTO_ENABLE, return_value=True):
            result = self.json_rpc('/api/support/projects', {'action': 'create', 'name': 'Auto enable?'}).json()
        self.assertTrue(result.get('result', {}).get('success'), result)
        project = self.env['project.project'].sudo().search([('name', '=', 'Auto enable?')])
        self.assertEqual(len(project), 1)
        self.assertFalse(project.use_agent_runtime)

    def test_portal_pages_have_no_old_card(self):
        Channel = self.env['discuss.channel'].sudo().with_context(active_test=False)
        channels_before = Channel.search_count([])
        self.authenticate(self.user_c.login, self.user_c.login)
        for url in ('/my/tasks/%s' % self.legacy_task.id, '/my/projects/%s' % self.legacy_project.id):
            response = self.url_open(url, timeout=30)
            self.assertEqual(response.status_code, 200, url)
            self.assertNotIn('paas_task_chat', response.text, url)
            self.assertNotIn('PortalAgentStatus', response.text, url)
        self.assertEqual(Channel.search_count([]), channels_before)
        self.assertNotIn(self.user_c.partner_id, self.legacy_chat.channel_member_ids.partner_id)
        # a task without a channel gets none either
        plain = self.env['project.task'].sudo().create({'name': 'No chat', 'project_id': self.legacy_project.id})
        plain.message_subscribe(partner_ids=self.user_c.partner_id.ids)
        self.assertEqual(self.url_open('/my/tasks/%s' % plain.id, timeout=30).status_code, 200)
        self.assertFalse(plain.channel_id)
        self.assertEqual(Channel.search_count([]), channels_before)

    def test_session_info_flag(self):
        self.authenticate(self.user_b.login, self.user_b.login)
        info = self.json_rpc('/web/session/get_session_info').json()['result']
        self.assertIs(info['woow_project_ai_paas_legacy'], False)
        self.set_legacy(True)
        info = self.json_rpc('/web/session/get_session_info').json()['result']
        self.assertIs(info['woow_project_ai_paas_legacy'], True)
        self.set_legacy(False)

    def test_task_chat_members_still_cannot_use_the_old_routes(self):
        """A member of a Project AI task chat gets the refusal of the route itself, before the endpoint (the
        task-chat guard behind it, for the switch on, is TestLegacyOn's)."""
        chat = self.open_tab(self.user_u, 'internal')
        self.authenticate(self.user_u.login, self.user_u.login)
        result = self.json_rpc('/api/ai/chat/history', {'channel_id': chat.id}).json()
        self.assertIn(REFUSAL_EN, self.error_of(result))
        self.assertTrue(chat.exists())
