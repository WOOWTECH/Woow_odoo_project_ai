# -*- coding: utf-8 -*-
"""AI assistants of project AI (own core): bot partner, Test Connection,
access, push key, limits."""
import base64
import hashlib
import hmac
import json
from unittest.mock import patch

import requests

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import new_test_user, tagged
from odoo.tools import mute_logger

from odoo.addons.woow_ai_task.models.ai_assistant import hash_push_key
from odoo.addons.woow_ai_task.models.ai_connection import endpoint_url_allowed

from .common import (CONNECTION_GET, CONNECTION_POST, TEST_KEY, TINY_PNG, ProjectAiCase, http_response,
                     make_assistant)


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestAiAssistant(ProjectAiCase):

    def test_bot_partner_created_archived_and_synced(self):
        partner = self.assistant.partner_id
        self.assertFalse(partner.active, 'never offered in "Start a conversation" nor in mentions')
        self.assertFalse(partner.user_ids)
        self.assertEqual(partner.name, 'Internal Helper')
        self.assertIn(self.env.ref('woow_ai_task.partner_category_project_ai_assistant'), partner.category_id)
        self.assistant.write({'name': '內部助理', 'image_1920': base64.b64encode(TINY_PNG)})
        self.assertEqual(partner.name, '內部助理')
        self.assertTrue(partner.image_1920)
        self.assistant.active = False
        self.assistant.active = True
        self.assertFalse(partner.active)
        # "bot" presence, as OdooBot
        self.assertEqual(partner.im_status, 'bot')
        self.assertNotEqual(self.staff.partner_id.im_status, 'bot')
        # nobody finds it in "Start a conversation"
        found = self.env['res.partner'].with_user(self.staff).im_search('Helper', 20)
        self.assertNotIn(partner.id, [p['id'] for p in found.get('res.partner', [])])
        with self.assertRaises(ValidationError):
            self.assistant.partner_id = self.staff.partner_id

    def test_copy_gets_own_partner(self):
        copy = self.assistant.copy()
        self.assertTrue(copy.partner_id)
        self.assertNotEqual(copy.partner_id, self.assistant.partner_id)
        self.assertEqual(copy.connection_id, self.connection, 'a copy uses the same AI connection')
        self.assertEqual(copy.name, 'Internal Helper (copy)')

    def test_test_connection_litellm(self):
        with patch(CONNECTION_GET, return_value=http_response(200, {'data': [{'id': 'secret-model-list'}]})) as get:
            action = self.assistant.action_test_connection()
        args, kwargs = get.call_args
        self.assertEqual(args[0], 'https://llm.example.com/v1/models')
        self.assertEqual(kwargs['headers'], {'Authorization': 'Bearer %s' % TEST_KEY})
        self.assertFalse(kwargs['allow_redirects'])
        params = action['params']
        self.assertEqual(action['tag'], 'display_notification')
        self.assertEqual(params['type'], 'success')
        self.assertIn('200', params['message'])
        self.assertNotIn('secret-model-list', json.dumps(action))
        for status in (401, 404, 502):
            with patch(CONNECTION_GET, return_value=http_response(status, text='upstream detail')):
                params = self.assistant.action_test_connection()['params']
            self.assertEqual(params['type'], 'danger')
            self.assertIn(str(status), params['message'])
            self.assertNotIn('upstream detail', params['message'])
        with patch(CONNECTION_GET, side_effect=requests.ConnectionError('down')):
            self.assertEqual(self.assistant.action_test_connection()['params']['type'], 'danger')
        with patch(CONNECTION_GET, side_effect=requests.ReadTimeout('slow')):
            self.assertEqual(self.assistant.action_test_connection()['params']['type'], 'danger')
        with patch(CONNECTION_POST) as post, patch(CONNECTION_GET, return_value=http_response(200, {})):
            self.assistant.action_test_connection()
        post.assert_not_called()  # never a chat completion (it would cost tokens)

    def test_test_connection_n8n(self):
        with patch(CONNECTION_POST, return_value=http_response(200, {'skip': True})) as post:
            params = self.n8n.action_test_connection()['params']
        self.assertEqual(params['type'], 'success')
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'https://n8n.example.com/webhook/c')
        headers = kwargs['headers']
        self.assertNotIn('Authorization', headers)
        expected = hmac.new(TEST_KEY.encode(), headers['X-Woow-Timestamp'].encode() + b'.' + kwargs['data'],
                            hashlib.sha256).hexdigest()
        self.assertEqual(headers['X-Woow-Signature'], 'sha256=%s' % expected)
        self.assertEqual(json.loads(kwargs['data']), {
            'event': 'ping', 'assistant': {'id': self.n8n.id, 'name': 'Customer Workflow'},
            'connection': {'id': self.n8n_connection.id, 'name': 'Customer Workflow connection'},
            'action': 'loadPreviousSession', 'sessionId': 'woow-project-ai-ping-%s' % self.n8n.id,
        })
        self.assertFalse(kwargs['allow_redirects'])

    def test_n8n_assistant_without_signing_secret(self):
        # e.g. a public n8n Chat Trigger: works without a secret, nothing is signed
        assistant = make_assistant(self.env, name='Open Workflow', backend_type='n8n',
                                   endpoint_url='https://n8n.example.com/webhook/open/chat')
        self.assertFalse(assistant.connection_id.api_key_set)
        self.assertEqual(assistant.connection_id.readiness, 'ok')
        with patch(CONNECTION_POST, return_value=http_response(200, {'data': []})) as post:
            params = assistant.action_test_connection()['params']
        self.assertEqual(params['type'], 'success', params['message'])
        self.assertEqual(post.call_args.kwargs['headers'], {'Content-Type': 'application/json'})
        # without an endpoint nothing is sent
        assistant.connection_id.endpoint_url = False
        with patch(CONNECTION_POST) as post:
            params = assistant.action_test_connection()['params']
        post.assert_not_called()
        self.assertEqual(params['type'], 'danger')
        # the connection form shows Test Connection for n8n without a secret, never for LiteLLM without a key
        arch = self.env['woow.project.ai.connection'].get_view(view_type='form')['arch']
        self.assertIn("(backend_type == 'openai' and (not api_key_set or not model))", arch)

    def test_test_connection_admin_only(self):
        with self.assertRaises(AccessError):
            self.assistant.with_user(self.internal).action_test_connection()
        self.connection.action_clear_api_key()
        with patch(CONNECTION_GET) as get:
            params = self.assistant.action_test_connection()['params']
        get.assert_not_called()
        self.assertEqual(params['type'], 'danger')

    def test_list_and_form_for_administrators_only(self):
        for xmlid in ('action_woow_project_ai_assistant', 'action_woow_project_ai_connection',
                      'action_woow_project_ai_job', 'action_woow_project_ai_call_log',
                      'action_woow_project_ai_usage_month'):
            self.assertEqual(self.env.ref('woow_ai_task.%s' % xmlid).groups_id, self.env.ref('base.group_system'))
        # the menus sit under Project > Configuration > Project AI, for administrators
        root = self.env.ref('woow_ai_task.menu_woow_project_ai_root')
        self.assertEqual(root.parent_id, self.env.ref('project.menu_project_config'))
        self.assertEqual(root.groups_id, self.env.ref('base.group_system'))
        admin = new_test_user(self.env, 'aitask_admin_ui', groups='base.group_user,base.group_system')
        spec = {'name': {}, 'description': {}, 'connection_id': {}}
        rows = self.env['woow.project.ai.assistant'].with_user(admin).web_search_read(
            [('id', '=', self.assistant.id)], spec)
        self.assertEqual(rows['records'][0]['name'], 'Internal Helper')

    def test_non_admin_reads_names_only(self):
        assistant = self.assistant.with_user(self.internal)  # a project user
        self.assertEqual(assistant.name, 'Internal Helper')
        for field in ('connection_id', 'system_prompt', 'push_key_hint', 'error_message', 'max_history'):
            with self.subTest(field=field), self.assertRaises(AccessError):
                assistant.read([field])
        with self.assertRaises(AccessError):
            assistant.write({'name': 'x'})
        with self.assertRaises(AccessError):
            self.assistant.with_user(self.portal).read(['name'])

    def test_endpoint_https_rule(self):
        self.assertTrue(endpoint_url_allowed('https://llm.example.com/v1'))
        self.assertTrue(endpoint_url_allowed('http://localhost:8080/v1'))
        self.assertTrue(endpoint_url_allowed('http://litellm.ai.svc.cluster.local:4000/v1'))
        self.assertFalse(endpoint_url_allowed('http://llm.example.com/v1'))
        self.assertFalse(endpoint_url_allowed('http://svc.cluster.local.evil.com/v1'))
        self.assertFalse(endpoint_url_allowed('ftp://llm.example.com'))
        with self.assertRaises(ValidationError):
            self.connection.endpoint_url = 'http://llm.example.com/v1'

    def test_limits(self):
        with self.assertRaises(ValidationError):
            self.assistant.max_history = 0
        with self.assertRaises(ValidationError):
            self.assistant.max_history = 201
        self.assistant.max_history = 200

    def test_push_key_hash_only(self):
        Assistant = self.env['woow.project.ai.assistant']
        action = self.assistant.action_generate_push_key()
        key = action['context']['default_push_key']
        self.assertTrue(key.startswith('wpai_'), 'its own prefix (Discuss AI keys start with "wpk_")')
        self.assertEqual(self.assistant.push_key_hash, hash_push_key(key))
        self.assertNotIn(key, self.assistant.push_key_hint)
        self.assertEqual(Assistant._find_by_push_key(key), self.assistant)
        self.assertFalse(Assistant._find_by_push_key(key + 'x'))
        self.assertFalse(Assistant._find_by_push_key(''))
        self.assertFalse(Assistant._find_by_push_key(self.assistant.push_key_hash))
        self.assertFalse(Assistant._find_by_push_key('wpk_' + key[5:]))
        # regenerate: the old key stops working
        new_key = self.assistant.action_generate_push_key()['context']['default_push_key']
        self.assertFalse(Assistant._find_by_push_key(key))
        self.assertEqual(Assistant._find_by_push_key(new_key), self.assistant)
        self.assertIs(self.assistant.action_revoke_push_key(), True)
        self.assertFalse(Assistant._find_by_push_key(new_key))
        for user in (self.internal, self.manager):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                self.assistant.with_user(user).action_generate_push_key()

    def test_projects_listed(self):
        self.assertEqual(self.assistant.project_ids, self.project)
        self.assertEqual(self.customer_ai.project_ids, self.project)
        self.assertFalse(self.customer_n8n.project_ids)
        # an assistant in use by a project cannot be deleted (archive it instead)
        with mute_logger('odoo.sql_db'), self.assertRaises(Exception), self.env.cr.savepoint():
            self.assistant.unlink()
