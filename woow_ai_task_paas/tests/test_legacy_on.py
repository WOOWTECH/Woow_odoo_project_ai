# -*- coding: utf-8 -*-
"""The old platform task AI switched on (for the platform's own tests of it,
or brought back on purpose): the old routes serve the old chats again, but
never a Project AI task chat; the backend forms are the platform's own; the
switch fails closed without the task-chat guard."""

from unittest.mock import patch

from lxml import etree

from odoo.tests.common import HttpCase, tagged

from odoo.addons.woow_ai_task_paas.controllers import legacy as legacy_controllers
from odoo.addons.woow_ai_task_paas.models.legacy import legacy_task_ai_enabled

from .common import PaasBridgeFixtures, RpcMixin


@tagged('post_install', '-at_install')
class TestLegacyOn(PaasBridgeFixtures, RpcMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_paas()
        cls.setup_legacy_seed()

    def setUp(self):
        super().setUp()
        self.set_legacy(True)

    def test_old_routes_serve_an_old_chat(self):
        self.authenticate(self.user_b.login, self.user_b.login)
        result = self.json_rpc('/api/ai/chat/history', {'channel_id': self.legacy_chat.id}).json()
        self.assertTrue(result.get('result', {}).get('success'), result)
        self.assertIn('legacy question', str(result['result'].get('data')))

    def test_a_task_chat_is_still_refused(self):
        chat = self.open_tab(self.user_u, 'internal')
        self.assertIn(self.user_u.partner_id, self.members(chat))
        self.authenticate(self.user_u.login, self.user_u.login)
        for url, params in (('/api/ai/chat/history', {'channel_id': chat.id}),
                            ('/api/ai/chat/post', {'channel_id': chat.id, 'body': 'through the old route'})):
            result = self.json_rpc(url, params).json()['result']
            self.assertFalse(result.get('success'), url)
            self.assertEqual(result.get('error'), 'Access denied', url)
        self.assertFalse(self.env['mail.message'].sudo().search_count([
            ('model', '=', 'discuss.channel'), ('res_id', '=', chat.id), ('body', 'ilike', 'through the old route')]))

    def test_the_forms_are_the_platforms(self):
        task_arch = etree.fromstring(self.env['project.task'].get_view(view_type='form')['arch'])
        [group] = task_arch.xpath("//group[field[@name='chat_enabled']]")
        self.assertNotEqual(group.get('invisible'), '1')
        [page] = task_arch.xpath("//page[@name='ai_chat']")
        self.assertEqual(page.get('string'), 'AI Chat')
        project_arch = etree.fromstring(self.env['project.project'].get_view(view_type='form')['arch'])
        [field] = project_arch.xpath("//field[@name='use_agent_runtime']")
        self.assertNotEqual(field.get('invisible'), '1')

    def test_orm_rules_are_off(self):
        project = self.env['project.project'].sudo().create({'name': 'On', 'use_agent_runtime': True})
        self.assertTrue(project.use_agent_runtime)

    def test_the_switch_fails_closed_without_the_guard(self):
        self.assertTrue(legacy_task_ai_enabled(self.env))
        with patch.object(legacy_controllers, 'TASK_CHAT_GUARD_LOADED', False):
            self.assertFalse(legacy_task_ai_enabled(self.env))
        for value, expected in (('1', True), (' TRUE ', True), ('yes', False), ('0', False), ('', False)):
            self.env['ir.config_parameter'].sudo().set_param('woow_ai_task_paas.legacy_task_ai', value)
            self.assertIs(legacy_task_ai_enabled(self.env), expected, value)
