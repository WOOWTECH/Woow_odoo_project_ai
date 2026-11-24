# -*- coding: utf-8 -*-
"""Surface and scope in both payloads, the monthly usage per tab, the counts
on the project."""

from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from .common import BACKEND_POST, AiTaskFixtures, http_response, openai_ok


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestSurfaceAndUsage(AiTaskFixtures, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_ai_task()

    def ask(self, channel, user, response, text='q'):
        self.post(channel, user, text)
        with patch(BACKEND_POST, return_value=response) as post:
            self.run_due(channel)
        return self.sent(post) if post.called else None

    def test_surface_and_scope_in_both_payloads(self):
        internal = self.open_tab(self.staff, 'internal')
        body = self.ask(internal, self.staff, openai_ok('ok'))
        surface = {k: v for k, v in body['metadata'].items() if k.startswith('surface_')}
        self.assertEqual(surface, {'surface_type': 'project_task', 'surface_project_id': str(self.project.id),
                                   'surface_task_id': str(self.task.id), 'surface_tab': 'internal'})
        self.project.woow_project_ai_customer_assistant_id = self.customer_n8n
        customer = self.open_tab(self.customer, 'customer')
        body = self.ask(customer, self.customer, http_response(200, {'reply': 'ok'}))
        # the customer follows the task, not the project: no project in the surface (team review 34)
        self.assertFalse(self.project.with_user(self.customer).has_access('read'))
        self.assertEqual(body['surface'], {'type': 'project_task', 'project_id': None,
                                           'task_id': self.task.id, 'tab': 'customer'})
        self.project.message_subscribe(partner_ids=self.customer.partner_id.ids)
        body = self.ask(customer, self.customer, http_response(200, {'reply': 'ok'}), text='q2')
        self.assertEqual(body['surface']['project_id'], self.project.id)
        self.assertEqual(body['channel'], {'id': customer.id, 'type': 'group'})
        self.assertTrue(body['user']['is_portal'])
        logs = self.env['woow.project.ai.call.log'].search([('channel_id', 'in', (internal | customer).ids)])
        self.assertEqual(set(logs.mapped('scope_model')), {'project.project'})
        self.assertEqual(set(logs.mapped('scope_res_id')), {self.project.id})
        self.assertEqual(set(logs.mapped('surface_type')), {'project_task'})

    def test_usage_separated_per_tab(self):
        # the same AI assistant on both tabs: still two rows
        self.project.woow_project_ai_internal_assistant_id = self.customer_ai
        internal = self.open_tab(self.staff, 'internal')
        customer = self.open_tab(self.customer, 'customer')
        self.open_tab(self.customer2, 'customer')
        usage = {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}
        self.ask(internal, self.staff, openai_ok('a', usage=usage))
        self.ask(customer, self.customer, openai_ok('b', usage=usage))
        self.ask(customer, self.customer2, openai_ok('c', usage=usage))
        rows = self.env['woow.project.ai.usage.month'].search([('assistant_id', '=', self.customer_ai.id)])
        self.assertEqual(len(rows), 2)
        by_tab = {row.surface_tab: row for row in rows}
        self.assertEqual((by_tab['internal'].replies_ok, by_tab['customer'].replies_ok), (1, 2))
        self.assertEqual(by_tab['customer'].prompt_tokens, 20)
        for row in rows:
            self.assertEqual((row.surface_type, row.scope_model, row.scope_res_id),
                             ('project_task', 'project.project', self.project.id))
            self.assertEqual(row.month, fields.Datetime.now().date().replace(day=1))
        # the project shows this month's totals to its users (numbers only)
        project = self.project.with_user(self.staff)
        self.assertEqual((project.woow_project_ai_usage_internal_month, project.woow_project_ai_usage_customer_month), (1, 2))
        # a failed reply is not a reply
        self.ask(customer, self.customer, http_response(500))
        project.invalidate_recordset()
        self.assertEqual(project.woow_project_ai_usage_customer_month, 2)
        # administrators open the rows from the project
        action = self.project.action_woow_project_ai_view_usage()
        self.assertEqual(self.env['woow.project.ai.usage.month'].search(action['domain']), rows)

    def test_burst_counts_one_reply(self):
        customer = self.open_tab(self.customer, 'customer')
        self.post(customer, self.customer, 'a')
        self.post(customer, self.customer, 'b')
        with patch(BACKEND_POST, return_value=openai_ok('one')):
            self.run_due(customer)
        self.assertEqual(self.project.woow_project_ai_usage_customer_month, 1)
