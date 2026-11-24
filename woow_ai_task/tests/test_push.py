# -*- coding: utf-8 -*-
"""Push API into a task AI conversation (n8n "skip, then push"): only the
assistant answering that conversation, not while paused; counted as a reply.
Only task AI chats: no direct chat, no other conversation."""

import json
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import HttpCase, tagged

from .common import AiTaskFixtures


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestTaskPush(AiTaskFixtures, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_ai_task()

    def setUp(self):
        super().setUp()
        patcher = patch.object(type(self.env['woow.project.ai.job']), '_start_after_commit')
        patcher.start()
        self.addCleanup(patcher.stop)

    def push(self, key, body, raw=None):
        response = self.url_open('/woow_project_ai/v1/push', data=raw if raw is not None else json.dumps(body),
                                 headers={'Content-Type': 'application/json', 'Authorization': 'Bearer %s' % key})
        return response.status_code, response.json()

    def key(self, assistant):
        return assistant.action_generate_push_key()['context']['default_push_key']

    def test_push_into_its_own_task_chat(self):
        customer = self.open_tab(self.customer, 'customer')
        internal = self.open_tab(self.staff, 'internal')
        status, data = self.push(self.key(self.customer_ai), {'to': {'channel_id': customer.id},
                                                              'text': '**稍後回覆**：報價已更新。'})
        self.assertEqual(status, 200, data)
        message = self.env['mail.message'].sudo().browse(data['message_id'])
        self.assertEqual((message.res_id, message.author_id), (customer.id, self.customer_ai.partner_id))
        self.assertIn('<strong>稍後回覆</strong>', message.body)
        self.assertFalse(self.jobs(customer), 'a push is never answered')
        row = self.env['woow.project.ai.usage.month'].search([('assistant_id', '=', self.customer_ai.id)])
        self.assertEqual((row.replies_ok, row.surface_tab, row.scope_res_id), (1, 'customer', self.project.id))
        log = self.env['woow.project.ai.call.log'].search([('assistant_id', '=', self.customer_ai.id)], limit=1)
        self.assertEqual((log.backend_type, log.status, log.channel_id), ('push', 'ok', customer))
        # another assistant cannot push into it, nor this one into the internal chat
        status, data = self.push(self.key(self.internal_ai), {'to': {'channel_id': customer.id}, 'text': 'x'})
        self.assertEqual((status, data['error']), (403, 'not_allowed'))
        status, data = self.push(self.key(self.customer_ai), {'to': {'channel_id': internal.id}, 'text': 'x'})
        self.assertEqual((status, data['error']), (403, 'not_allowed'))
        # paused (a colleague took over): refused
        customer.sudo().woow_project_ai_paused_until = fields.Datetime.add(fields.Datetime.now(), minutes=5)
        status, data = self.push(self.key(self.customer_ai), {'to': {'channel_id': customer.id}, 'text': 'x'})
        self.assertEqual((status, data['error']), (409, 'paused'))
        # a customer connection that is no longer safe for customers no longer answers there: refused
        customer.sudo().woow_project_ai_paused_until = False
        self.project.woow_project_ai_customer_assistant_id = self.customer_n8n
        n8n_key = self.key(self.customer_n8n)
        self.assertEqual(self.push(n8n_key, {'to': {'channel_id': customer.id}, 'text': 'n8n'})[0], 200)
        self.customer_n8n.connection_id.customer_safe = False
        status, data = self.push(n8n_key, {'to': {'channel_id': customer.id}, 'text': 'x'})
        self.assertEqual((status, data['error']), (403, 'not_allowed'))
        # an archived task: refused
        self.customer_n8n.connection_id.customer_safe = True
        self.task.active = False
        status, data = self.push(n8n_key, {'to': {'channel_id': customer.id}, 'text': 'x'})
        self.assertEqual((status, data['error']), (403, 'not_allowed'))

    def test_only_task_chats(self):
        key = self.key(self.internal_ai)
        internal = self.open_tab(self.staff, 'internal')
        # a direct chat with the bot, a group chat: never reached (not found, whatever exists)
        chat = self.env['discuss.channel'].with_user(self.staff).channel_get([self.internal_ai.partner_id.id])
        group = self.env['discuss.channel'].with_user(self.staff).create({'name': 'team', 'channel_type': 'group'})
        for channel_id in (chat.id, group.id, 999999):
            status, data = self.push(key, {'to': {'channel_id': channel_id}, 'text': 'x'})
            self.assertEqual((status, data['error']), (404, 'channel_not_found'))
        # a user key (direct chats of the Discuss AI module): not a recipient here
        for to in ({'user_id': self.staff.id}, {'email': self.staff.email}, {'channel_id': str(internal.id)},
                   {'channel_id': True}, {}):
            with self.subTest(to=to):
                status, data = self.push(key, {'to': to, 'text': 'x'})
                self.assertEqual((status, data['error']), (400, 'invalid_recipient'))
        self.assertEqual(self.push(key, {'to': {'channel_id': internal.id}, 'text': 'ok'})[0], 200)

    def test_keys_and_bodies(self):
        internal = self.open_tab(self.staff, 'internal')
        key = self.key(self.internal_ai)
        to = {'to': {'channel_id': internal.id}, 'text': 'x'}
        # no key, a wrong key, a revoked key, a key of an archived assistant; the key of the
        # Discuss AI module's push API ("wpk_...") is never one of these
        for bad in ('', 'wpai_wrong', 'wpk_' + key[5:], key.upper()):
            with self.subTest(key=bad):
                self.assertEqual(self.push(bad, to), (401, {'ok': False, 'error': 'invalid_key'}))
        self.assertEqual(self.push(key, to)[0], 200)
        self.internal_ai.action_revoke_push_key()
        self.assertEqual(self.push(key, to)[0], 401)
        key = self.key(self.internal_ai)
        self.internal_ai.active = False
        self.assertEqual(self.push(key, to)[0], 401)
        self.internal_ai.active = True
        # bodies
        self.assertEqual(self.push(key, None, raw='{not json'), (400, {'ok': False, 'error': 'invalid_json'}))
        self.assertEqual(self.push(key, ['x'])[1]['error'], 'invalid_body')
        self.assertEqual(self.push(key, {'to': {'channel_id': internal.id}, 'text': '  '})[1]['error'],
                         'empty_message')
        self.assertEqual(self.push(key, {'to': {'channel_id': internal.id}, 'text': 5})[1]['error'], 'invalid_body')
        logs = self.env['woow.project.ai.call.log'].search([('assistant_id', '=', self.internal_ai.id),
                                                            ('backend_type', '=', 'push')])
        self.assertIn('push: empty_message', logs.mapped('error'))
