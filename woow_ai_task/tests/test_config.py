# -*- coding: utf-8 -*-
"""Project configuration: who sets what, the assistant lists, the customer-safe
constraint, readiness states without connection details."""

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, tagged

from .common import AiTaskFixtures, make_assistant

PROJECT_FIELDS = {
    'woow_project_ai_internal_brief': 'new internal text',
    'woow_project_ai_customer_brief': 'new customer text',
    'woow_project_ai_customer_welcome': 'Welcome!',
    'woow_project_ai_pause_minutes': 15,
}
AI_PROJECT_FIELDS = ('woow_project_ai_internal_brief', 'woow_project_ai_customer_brief',
                     'woow_project_ai_internal_assistant_id', 'woow_project_ai_customer_assistant_id',
                     'woow_project_ai_internal_state_reason', 'woow_project_ai_customer_welcome')


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestProjectConfiguration(AiTaskFixtures, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_ai_task()

    def zh(self):
        self.env['res.lang']._activate_lang('zh_TW')
        self.env['ir.module.module']._load_module_terms(['woow_ai_task'], ['zh_TW'], overwrite=True)
        return self.env(context=dict(self.env.context, lang='zh_TW'))

    def test_project_managers_set_the_fields(self):
        project = self.project.with_user(self.manager)
        project.write(dict(PROJECT_FIELDS, woow_project_ai_customer_assistant_id=self.customer_n8n.id,
                           woow_project_ai_internal_assistant_id=self.internal_n8n.id))
        self.assertEqual(project.woow_project_ai_customer_assistant_id, self.customer_n8n)
        self.assertEqual(project.woow_project_ai_pause_minutes, 15)
        # project users read them but cannot set them; portal users cannot even read them
        as_staff = self.project.with_user(self.staff)
        self.assertEqual(as_staff.woow_project_ai_customer_brief, 'new customer text')
        for user in (self.staff, self.customer):
            for field, value in dict(PROJECT_FIELDS, woow_project_ai_customer_assistant_id=self.customer_ai.id).items():
                with self.subTest(user=user.login, field=field), self.assertRaises(AccessError):
                    self.project.with_user(user).write({field: value})
        # a customer following the project reads it, but none of the AI fields
        self.project.message_subscribe(partner_ids=self.customer.partner_id.ids)
        self.assertEqual(self.project.with_user(self.customer).read(['name'])[0]['name'], 'Kitchen Renovation')
        for field in AI_PROJECT_FIELDS:
            with self.subTest(field=field), self.assertRaises(AccessError):
                self.project.with_user(self.customer).read([field])
        # only administrators prepare assistants and connections
        with self.assertRaises(AccessError):
            self.internal_ai.with_user(self.manager).write({'name': 'renamed'})
        with self.assertRaises(AccessError):
            self.env['woow.project.ai.assistant'].with_user(self.manager).create({
                'name': 'Mine', 'connection_id': self.internal_ai.connection_id.id})
        for user in (self.manager, self.staff):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                self.internal_ai.connection_id.with_user(user).read(['name'])

    def test_assistant_lists(self):
        Assistant = self.env['woow.project.ai.assistant'].with_user(self.manager)
        internal_domain = self.env['project.project']._fields['woow_project_ai_internal_assistant_id'].domain
        customer_domain = self.env['project.project']._fields['woow_project_ai_customer_assistant_id'].domain
        archived = make_assistant(self.env, self.customer_ai.connection_id, name='Retired Helper')
        archived.active = False
        # the manager sees every active assistant (names only)
        internal = Assistant.search(internal_domain)
        expected = {self.internal_ai.id, self.customer_ai.id, self.customer_n8n.id, self.internal_n8n.id}
        self.assertEqual(set(internal.ids) & expected, expected)
        self.assertNotIn(archived, internal.with_context(active_test=False))
        customer = Assistant.search(customer_domain)
        self.assertIn(self.customer_ai, customer, 'LiteLLM: chat-only')
        self.assertIn(self.customer_n8n, customer, 'n8n marked safe for customers')
        self.assertNotIn(self.internal_n8n, customer, 'n8n not marked safe for customers')
        # what the web client reads: the assistant names, never the connection
        names = [name for _id, name in Assistant.name_search('Customer', customer_domain)]
        self.assertIn('Customer Helper', names)
        with self.assertRaises(AccessError):
            self.customer_ai.with_user(self.manager).read(['connection_id'])
        for field in ('system_prompt', 'push_key_hint', 'error_message'):
            with self.subTest(field=field), self.assertRaises(AccessError):
                self.customer_ai.with_user(self.staff).read([field])
        # employees without project rights and portal users read no assistant at all
        for user in (self.employee, self.customer):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                self.env['woow.project.ai.assistant'].with_user(user).search_read([], ['name'])

    def test_customer_assistant_must_be_customer_safe(self):
        env = self.zh()
        project = self.project.with_env(env).with_user(self.manager)
        with self.assertRaises(ValidationError) as error:
            project.write({'woow_project_ai_customer_assistant_id': self.internal_n8n.id})
        self.assertIn('可給客戶使用', str(error.exception))
        self.assertIn('系統管理員', str(error.exception))
        self.assertIn('Internal Workflow', str(error.exception))
        # the internal tab takes any active assistant
        project.write({'woow_project_ai_internal_assistant_id': self.internal_n8n.id})
        # the constraint holds whatever the domain: an archived assistant
        archived = make_assistant(self.env, self.customer_ai.connection_id, name='Old Helper')
        archived.active = False
        for field in ('woow_project_ai_customer_assistant_id', 'woow_project_ai_internal_assistant_id'):
            with self.subTest(field=field), self.assertRaises(ValidationError) as error:
                project.write({field: archived.id})
            self.assertIn('已封存', str(error.exception))
        # creating a project is checked too
        with self.assertRaises(ValidationError):
            self.env['project.project'].create({'name': 'Bad',
                                                'woow_project_ai_customer_assistant_id': self.internal_n8n.id})
        self.assertEqual(self.project.woow_project_ai_customer_assistant_id, self.customer_ai)

    def test_pause_range(self):
        for minutes in (0, 1441):
            with self.subTest(minutes=minutes), self.assertRaises(ValidationError):
                self.project.write({'woow_project_ai_pause_minutes': minutes})
        self.project.write({'woow_project_ai_pause_minutes': 1440})

    def test_states_never_show_connection_details(self):
        self.project.woow_project_ai_customer_assistant_id = self.customer_n8n
        project = self.project.with_user(self.manager)
        self.assertEqual((project.woow_project_ai_internal_state, project.woow_project_ai_customer_state),
                         ('ok', 'ok'))
        # an administrator unticks "Safe for Customers" on the n8n connection, and the internal key is removed
        self.customer_n8n.connection_id.customer_safe = False
        connection = self.internal_ai.connection_id
        connection.action_clear_api_key()
        project.invalidate_recordset()
        self.assertEqual((project.woow_project_ai_internal_state, project.woow_project_ai_customer_state),
                         ('blocked', 'blocked'))
        internal_reason = project.woow_project_ai_internal_state_reason
        customer_reason = project.woow_project_ai_customer_state_reason
        self.assertIn('API key', internal_reason)
        self.assertIn('Safe for Customers', customer_reason)
        for secret in ('llm.example.com', 'n8n.example.com', 'https://', 'test-backend-key', connection.name,
                       self.customer_n8n.connection_id.name):
            self.assertNotIn(secret, internal_reason + customer_reason)
        # zh_TW, still without details
        zh = project.with_env(self.zh()).with_user(self.manager)
        zh.invalidate_recordset()
        self.assertIn('金鑰', zh.woow_project_ai_internal_state_reason)
        self.assertIn('可給客戶使用', zh.woow_project_ai_customer_state_reason)
        self.assertNotIn('llm.example.com', zh.woow_project_ai_internal_state_reason)
        # no assistant: no tab
        project.sudo().woow_project_ai_internal_assistant_id = False
        project.invalidate_recordset()
        self.assertEqual(project.woow_project_ai_internal_state, 'none')

    def test_project_form_reads_for_project_users(self):
        spec = {'woow_project_ai_internal_assistant_id': {'fields': {'display_name': {}}},
                'woow_project_ai_customer_assistant_id': {'fields': {'display_name': {}}},
                'woow_project_ai_internal_state': {}, 'woow_project_ai_customer_state_reason': {},
                'woow_project_ai_usage_internal_month': {}, 'woow_project_ai_usage_customer_month': {}}
        for user in (self.manager, self.staff):
            with self.subTest(user=user.login):
                data = self.project.with_user(user).web_read(spec)[0]
                self.assertEqual(data['woow_project_ai_internal_assistant_id']['display_name'], 'Internal Helper')
                self.assertEqual(data['woow_project_ai_usage_customer_month'], 0)

    def test_project_form_for_employees_outside_the_project_app(self):
        # an employee without project rights who opens a project form (a link, another app) reads no AI
        # assistant (only administrators and project users may): the AI page and fields are not theirs
        Project = self.env['project.project'].with_user(self.employee)
        fields_ = Project.get_views([(False, 'form')])['models']['project.project']['fields']
        self.assertFalse([name for name in fields_ if name.startswith('woow_project_ai_')])
        spec = {name: ({'fields': {'display_name': {}}} if field['type'] == 'many2one' else {})
                for name, field in fields_.items() if field['type'] not in ('one2many', 'many2many')}
        self.assertEqual(self.project.with_user(self.employee).web_read(spec)[0]['name'], 'Kitchen Renovation')
        # project users still get the page
        staff_fields = self.env['project.project'].with_user(self.staff).get_views(
            [(False, 'form')])['models']['project.project']['fields']
        self.assertIn('woow_project_ai_internal_assistant_id', staff_fields)
