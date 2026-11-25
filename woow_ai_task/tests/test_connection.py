# -*- coding: utf-8 -*-
"""AI connections of project AI (own core): write-only key, endpoint rules,
"Safe for Customers", readiness, access, Test Connection, neutralize."""
import json
from unittest.mock import patch

from odoo.addons.base.controllers.rpc import dumps as xmlrpc_dumps
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.modules import neutralize
from odoo.tests import Form
from odoo.tests.common import new_test_user, tagged
from odoo.tools import mute_logger

from .common import (CONNECTION_GET, CONNECTION_POST, BACKEND_POST, TEST_KEY, ProjectAiCase, http_response,
                     make_assistant, openai_ok)


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestAiConnection(ProjectAiCase):

    def key_of(self, connection):
        self.env.cr.execute("SELECT api_key_secret FROM woow_project_ai_connection WHERE id = %s", (connection.id,))
        return self.env.cr.fetchone()[0]

    # --- key and endpoint ---

    def test_api_key_write_only(self):
        connection = self.connection
        self.assertEqual(connection._get_api_key(), TEST_KEY)
        data = connection.read(['api_key', 'api_key_set'])[0]
        self.assertFalse(data['api_key'])
        self.assertTrue(data['api_key_set'])
        self.assertNotIn('api_key_secret', self.env['woow.project.ai.connection']._fields)
        # web_save read-back does not echo the key
        result = connection.web_save({'api_key': 'new-key'}, {'api_key': {}, 'api_key_set': {}})
        self.assertFalse(result[0]['api_key'])
        self.assertEqual(connection._get_api_key(), 'new-key')
        # empty keeps the key
        connection.write({'api_key': ''})
        self.assertEqual(connection._get_api_key(), 'new-key')
        self.assertIs(connection.action_clear_api_key(), True)
        self.assertFalse(connection._get_api_key())
        self.assertFalse(connection.api_key_set)
        # never in an export either
        export = connection.export_data(['api_key'])['datas']
        self.assertFalse(export[0][0])
        # the assistant has no key field
        self.assertNotIn('api_key', self.env['woow.project.ai.assistant']._fields)
        self.assertNotIn('endpoint_url', self.env['woow.project.ai.assistant']._fields)

    def test_api_key_removed_when_the_server_changes(self):
        connection = self.connection
        # same server, other path: kept
        connection.endpoint_url = 'https://llm.example.com/v2'
        self.assertEqual(connection._get_api_key(), TEST_KEY)
        connection.endpoint_url = 'HTTPS://LLM.example.com:443/v1'
        self.assertEqual(connection._get_api_key(), TEST_KEY)
        # other host without a new key: removed before anything is sent there
        connection.endpoint_url = 'https://openrouter.example.org/api/v1'
        self.assertFalse(connection.api_key_set)
        self.assertEqual(connection._get_api_key(), '')
        with patch(CONNECTION_GET) as get:
            result = connection.action_test_connection()
        get.assert_not_called()
        self.assertEqual(result['params']['type'], 'danger')
        # other host with a new key in the same write: the new key is kept
        connection.write({'endpoint_url': 'https://llm2.example.com/v1', 'api_key': 'second-placeholder'})
        self.assertEqual(connection._get_api_key(), 'second-placeholder')
        # other port / scheme counts as another server
        connection.endpoint_url = 'https://llm2.example.com:8443/v1'
        self.assertFalse(connection.api_key_set)
        # a key set while the URL was empty survives filling the URL in
        connection.write({'endpoint_url': False, 'api_key': 'third-placeholder'})
        connection.endpoint_url = 'https://llm.example.com/v1'
        self.assertEqual(connection._get_api_key(), 'third-placeholder')
        # n8n signing secret -> LiteLLM: never becomes a Bearer token
        self.n8n_connection.backend_type = 'openai'
        self.assertFalse(self.n8n_connection.api_key_set)
        self.n8n_connection.write({'backend_type': 'n8n', 'api_key': 'n8n-placeholder'})
        self.n8n_connection.write({'name': 'Renamed', 'backend_type': 'n8n'})  # unchanged type: kept
        self.assertEqual(self.n8n_connection._get_api_key(), 'n8n-placeholder')

    def test_endpoint_and_limits(self):
        with self.assertRaises(ValidationError):
            self.connection.endpoint_url = 'http://llm.example.com/v1'
        self.connection.endpoint_url = 'http://litellm.ai.svc.cluster.local:4000/v1'
        for field, value in (('timeout_seconds', 4), ('timeout_seconds', 3601), ('max_retries', 3),
                             ('max_retries', -1), ('max_download_mb', 0), ('max_download_mb', 101)):
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                self.connection.write({field: value})
        connection = self.env['woow.project.ai.connection'].create({'name': 'Defaults'})
        self.assertEqual((connection.backend_type, connection.timeout_seconds, connection.max_retries,
                          connection.openai_stream, connection.document_mode, connection.max_download_mb,
                          connection.log_payload, connection.customer_safe),
                         ('openai', 300, 1, True, 'file', 20, False, True))
        # two backend types only, no Hermes fields
        self.assertEqual([key for key, _label in
                          self.env['woow.project.ai.connection']._fields['backend_type'].selection],
                         ['openai', 'n8n'])
        for name in ('is_hermes', 'hermes_locked_confirmed', 'has_data_tools', 'has_memory'):
            self.assertNotIn(name, self.env['woow.project.ai.connection']._fields)

    def test_copy_has_no_key(self):
        copy = self.connection.copy()
        self.assertEqual(copy.name, '%s (copy)' % self.connection.name)
        self.assertEqual(copy.endpoint_url, self.connection.endpoint_url)
        self.assertFalse(copy.api_key_set)
        self.assertIsNone(self.key_of(copy))
        self.assertTrue(copy.customer_safe, 'LiteLLM: always')
        self.customer_n8n.connection_id.customer_safe = True
        self.assertFalse(self.customer_n8n.connection_id.copy().customer_safe, 'n8n: to be checked again')

    def test_one_connection_for_several_assistants(self):
        second = make_assistant(self.env, self.connection, name='Second Helper')
        self.assertEqual(self.connection.assistant_ids, self.assistant | second)
        self.assertEqual(self.connection.assistant_count, 2)
        for assistant in (self.assistant, second):
            chat = self.chat(self.internal, assistant)
            self.post(chat, self.internal, 'hi')
            with patch(BACKEND_POST, return_value=openai_ok('ok')) as post:
                self.run_due(chat)
            self.assertEqual(post.call_args.kwargs['headers']['Authorization'], 'Bearer %s' % TEST_KEY)
            self.assertEqual(post.call_args.args[0], 'https://llm.example.com/v1/chat/completions')
        # a connection in use cannot be deleted
        with mute_logger('odoo.sql_db'), self.assertRaises(Exception), self.env.cr.savepoint():
            self.connection.unlink()

    # --- safe for customers ---

    def test_customer_safe_litellm_always(self):
        Connection = self.env['woow.project.ai.connection']
        litellm = Connection.create({'name': 'LiteLLM', 'endpoint_url': 'https://llm.example.com/v1'})
        self.assertTrue(litellm.customer_safe, 'chat-only')
        with self.assertRaises(ValidationError):
            litellm.customer_safe = False
        self.assertTrue(Connection.create({'name': 'x', 'customer_safe': False}).customer_safe, 'forced')
        # n8n -> LiteLLM: safe; LiteLLM -> n8n: to be checked by an administrator
        litellm.backend_type = 'n8n'
        self.assertFalse(litellm.customer_safe)
        litellm.backend_type = 'openai'
        self.assertTrue(litellm.customer_safe)

    def test_customer_safe_n8n_flag(self):
        Connection = self.env['woow.project.ai.connection']
        n8n = Connection.create({'name': 'n8n', 'backend_type': 'n8n',
                                 'endpoint_url': 'https://n8n.example.com/webhook/a'})
        self.assertFalse(n8n.customer_safe, 'not until an administrator ticks it')
        n8n.customer_safe = True
        n8n.write({'name': 'Renamed', 'timeout_seconds': 60, 'api_key': 'secret-placeholder'})
        self.assertTrue(n8n.customer_safe)
        n8n.write({'endpoint_url': 'https://n8n.example.com/webhook/a  '})  # same URL
        self.assertTrue(n8n.customer_safe)
        # another workflow (same host, other path), another host: cleared
        n8n.endpoint_url = 'https://n8n.example.com/webhook/b'
        self.assertFalse(n8n.customer_safe)
        n8n.write({'endpoint_url': 'https://n8n2.example.com/webhook/a', 'customer_safe': True})
        self.assertTrue(n8n.customer_safe, 'ticked in the same save as the new URL: kept')
        n8n.endpoint_url = False
        self.assertFalse(n8n.customer_safe)
        # searchable
        mine = [('id', 'in', (n8n | self.connection | self.n8n_connection).ids)]
        self.assertEqual(Connection.search(mine + [('customer_safe', '=', True)]),
                                         self.connection | self.n8n_connection)
        # the form shows what saving does
        n8n.write({'endpoint_url': 'https://n8n.example.com/webhook/a', 'customer_safe': True})
        with Form(n8n) as form:
            form.endpoint_url = 'https://n8n.example.com/webhook/c'
            self.assertFalse(form.customer_safe)
            form.customer_safe = True
        self.assertTrue(n8n.customer_safe)
        with Form(n8n) as form:
            form.backend_type = 'openai'
            self.assertTrue(form.customer_safe)
            form.api_key = 'typed-placeholder'

    # --- readiness ---

    def test_readiness(self):
        Connection = self.env['woow.project.ai.connection']
        self.assertEqual(self.connection.readiness, 'ok')
        self.assertEqual(self.n8n_connection.readiness, 'ok')
        cases = {
            'no URL': (Connection.create({'name': 'a', 'api_key': TEST_KEY, 'model': 'm'}), 'Set the endpoint URL.'),
            'no key': (Connection.create({'name': 'b', 'endpoint_url': 'https://llm.example.com/v1', 'model': 'm'}),
                       'Set the API key'),
            # the LiteLLM model is required (the contract): LiteLLM picks the deployment by its name
            'no model': (Connection.create({'name': 'c', 'endpoint_url': 'https://llm.example.com/v1',
                                            'api_key': TEST_KEY, 'model': '  '}), 'Set the model'),
        }
        for label, (connection, reason) in cases.items():
            with self.subTest(label):
                self.assertEqual(connection.readiness, 'blocked')
                self.assertIn(reason, connection.readiness_reason)
        # n8n needs no key
        n8n = Connection.create({'name': 'd', 'backend_type': 'n8n', 'endpoint_url': 'https://n8n.example.com/w'})
        self.assertEqual(n8n.readiness, 'ok')
        # a key given later makes it ready; removing it blocks again
        cases['no key'][0].api_key = TEST_KEY
        self.assertEqual(cases['no key'][0].readiness, 'ok')
        cases['no key'][0].action_clear_api_key()
        self.assertEqual(cases['no key'][0].readiness, 'blocked')
        self.connection.active = False
        self.assertEqual(self.connection.readiness, 'blocked')
        self.assertIn('archived', self.connection.readiness_reason)
        self.connection.active = True
        # searchable
        mine = [('id', 'in', (self.connection | self.n8n_connection | n8n).ids
                 + [connection.id for connection, _reason in cases.values()])]
        ready = Connection.search(mine + [('readiness', '=', 'ok')])
        self.assertEqual(ready, self.connection | self.n8n_connection | n8n)
        blocked = Connection.search(mine + [('readiness', '!=', 'ok')])
        self.assertEqual(blocked, cases['no URL'][0] | cases['no key'][0] | cases['no model'][0])
        self.assertEqual(Connection.search(mine + [('readiness', '=', 'blocked')]), blocked)
        with self.assertRaises(UserError):
            Connection.search([('readiness', 'in', ['ok'])])
        # the form shows it while editing, typed key included
        with Form(Connection) as form:
            form.name = 'typed'
            form.endpoint_url = 'https://llm.example.com/v1'
            self.assertEqual(form.readiness, 'blocked')
            form.api_key = 'typed-placeholder'
            self.assertEqual(form.readiness, 'blocked')
            form.model = 'typed-model'
            self.assertEqual(form.readiness, 'ok')

    # --- access ---

    def test_only_administrators_read_connections(self):
        for user in (self.internal, self.portal, self.manager):
            Connection = self.env['woow.project.ai.connection'].with_user(user)
            with self.assertRaises(AccessError):
                Connection.search([])
            with self.assertRaises(AccessError):
                self.connection.with_user(user).read(['name'])
            with self.assertRaises(AccessError):
                Connection.create({'name': 'x'})
            with self.assertRaises(AccessError):
                self.connection.with_user(user).write({'name': 'x'})
            with self.assertRaises(AccessError):
                self.assistant.with_user(user).read(['connection_id'])
            with self.assertRaises(AccessError):
                self.connection.with_user(user).action_test_connection()
        admin = new_test_user(self.env, 'aitask_conn_admin', groups='base.group_user,base.group_system')
        self.assertEqual(self.env['woow.project.ai.connection'].with_user(admin).search(
            [('id', '=', self.connection.id)]), self.connection)
        self.assertEqual(self.assistant.with_user(admin).connection_id, self.connection)
        # nobody else has an access right on the model
        rights = self.env['ir.model.access'].search([('model_id.model', '=', 'woow.project.ai.connection')])
        self.assertEqual(rights.group_id, self.env.ref('base.group_system'))

    def test_archived_connection_answers_nothing(self):
        chat = self.chat(self.internal)
        self.connection.active = False
        with patch(BACKEND_POST) as post:
            self.post(chat, self.internal, 'hi')
            self.run_due(chat)
        post.assert_not_called()
        self.assertFalse(self.jobs(chat), 'a blocked tab queues nothing')
        notice = self.env['mail.message'].sudo().search([('model', '=', 'discuss.channel'), ('res_id', '=', chat.id),
                                                         ('message_type', '=', 'notification')])
        self.assertEqual(len(notice), 1)

    # --- test connection ---

    def test_test_connection_from_the_connection(self):
        with patch(CONNECTION_POST, return_value=http_response(200, {'skip': True})) as post:
            params = self.n8n_connection.action_test_connection()['params']
        self.assertEqual(params['type'], 'success')
        self.assertEqual(json.loads(post.call_args.kwargs['data']), {
            'event': 'ping', 'connection': {'id': self.n8n_connection.id, 'name': 'Customer Workflow connection'},
            'action': 'loadPreviousSession',
            'sessionId': 'woow-project-ai-ping-connection-%s' % self.n8n_connection.id,
        })
        self.assertTrue(post.call_args.kwargs['headers']['X-Woow-Signature'].startswith('sha256='))
        with patch(CONNECTION_GET, return_value=http_response(200, {'data': []})) as get, \
                patch(CONNECTION_POST) as post:
            params = self.connection.action_test_connection()['params']
        post.assert_not_called()
        self.assertEqual(get.call_args.args[0], 'https://llm.example.com/v1/models')
        self.assertFalse(get.call_args.kwargs['allow_redirects'])
        self.assertEqual(params['type'], 'success')
        # from the assistant: the n8n ping names it
        with patch(CONNECTION_POST, return_value=http_response(401)) as post:
            params = self.n8n.action_test_connection()['params']
        self.assertEqual(params['type'], 'danger')
        self.assertIn('401', params['message'])
        self.assertEqual(json.loads(post.call_args.kwargs['data'])['assistant'],
                         {'id': self.n8n.id, 'name': 'Customer Workflow'})
        for user in (self.internal, self.manager):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                self.n8n.with_user(user).action_test_connection()

    def test_public_actions_xmlrpc_marshallable(self):
        for model, record in (('woow.project.ai.connection', self.connection),
                              ('woow.project.ai.assistant', self.assistant)):
            cls = type(self.env[model])
            names = sorted(name for name in dir(cls)
                           if name.startswith('action_') and callable(getattr(cls, name))
                           and getattr(cls, name).__module__.startswith('odoo.addons.woow_ai_task.'))
            self.assertTrue(names, model)
            for name in names:
                copy = record.copy()
                with self.subTest(model=model, action=name), \
                        patch(CONNECTION_GET, return_value=http_response(200)), \
                        patch(CONNECTION_POST, return_value=http_response(200)):
                    result = getattr(copy, name)()
                    self.assertIsNotNone(result)
                    xmlrpc_dumps((result,))

    # --- neutralize ---

    def test_neutralize(self):
        self.customer_n8n.connection_id.customer_safe = True
        self.assistant.action_generate_push_key()
        self.env.flush_all()
        queries = list(neutralize.get_neutralization_queries(['woow_ai_task']))
        self.assertEqual(len(queries), 1)
        for query in queries:
            self.env.cr.execute(query)
        self.env.invalidate_all()
        Connection = self.env['woow.project.ai.connection'].with_context(active_test=False)
        for connection in Connection.search([]):
            self.assertFalse(connection.active)
            self.assertFalse(connection.endpoint_url)
            self.assertFalse(connection.api_key_set)
            self.assertEqual(connection.customer_safe, connection.backend_type == 'openai')
            self.assertEqual(connection.readiness, 'blocked')
        self.env.cr.execute("SELECT count(*) FROM woow_project_ai_assistant WHERE push_key_hash IS NOT NULL")
        self.assertEqual(self.env.cr.fetchone()[0], 0)
        # a neutralized copy never calls a backend
        chat = self.chat(self.internal)
        with patch(BACKEND_POST) as post, patch(CONNECTION_GET) as get:
            self.post(chat, self.internal, 'hi')
            self.run_due(chat)
        post.assert_not_called()
        get.assert_not_called()
        self.assertFalse(self.jobs(chat))

    def test_neutralize_before_the_tables_exist(self):
        # the to_regclass guards: a database where the module is installed but its tables are missing
        query = next(iter(neutralize.get_neutralization_queries(['woow_ai_task'])))
        self.env.flush_all()
        cr = self.env.cr
        cr.execute("ALTER TABLE woow_project_ai_connection RENAME TO woow_project_ai_connection_gone")
        cr.execute("ALTER TABLE woow_project_ai_assistant RENAME TO woow_project_ai_assistant_gone")
        try:
            with cr.savepoint(flush=False):
                cr.execute(query)  # no UndefinedTable
        finally:
            cr.execute("ALTER TABLE woow_project_ai_connection_gone RENAME TO woow_project_ai_connection")
            cr.execute("ALTER TABLE woow_project_ai_assistant_gone RENAME TO woow_project_ai_assistant")
