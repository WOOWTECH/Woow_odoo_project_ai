# -*- coding: utf-8 -*-
"""What the AI gets about the task: the tab's project text and a task summary
read with the asker's own rights (customer: portal-readable fields only),
never the chatter."""

import json
from unittest.mock import patch

from odoo import Command
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_ai_task.models import project_task as task_module

from .common import BACKEND_POST, AiTaskFixtures, http_response, openai_ok

SECRET_NOTE = 'SECRET-LOG-NOTE-4711'
SECRET_MESSAGE = 'CHATTER-MESSAGE-0815'


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestTaskContext(AiTaskFixtures, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_ai_task()
        cls.task.message_post(body=SECRET_NOTE, message_type='comment', subtype_xmlid='mail.mt_note')
        cls.task.message_post(body=SECRET_MESSAGE, message_type='comment', subtype_xmlid='mail.mt_comment')
        cls.parent = cls.env['project.task'].create({'name': 'Kitchen master plan', 'project_id': cls.project.id})
        cls.task.parent_id = cls.parent
        # a subtask the customer follows, and one only the team sees
        cls.visible_sub = cls.env['project.task'].create({'name': 'Choose handles', 'project_id': cls.project.id,
                                                          'parent_id': cls.task.id})
        cls.visible_sub.message_subscribe(partner_ids=cls.customer.partner_id.ids)
        cls.hidden_sub = cls.env['project.task'].create({'name': 'Negotiate supplier discount',
                                                         'project_id': cls.project.id, 'parent_id': cls.task.id})
        # Odoo copies the parent's followers to a new subtask: this one is for the team only
        cls.hidden_sub.message_unsubscribe(partner_ids=(cls.customer | cls.customer2).partner_id.ids)

    def ask(self, tab, user, text='請問進度？', response=None):
        channel = self.open_tab(user, tab)
        self.post(channel, user, text)
        with patch(BACKEND_POST, return_value=response or openai_ok('ok')) as post:
            self.run_due(channel)
        self.assertEqual(post.call_count, 1)
        return self.sent(post)

    def system(self, body):
        return body['messages'][0]['content']

    def test_customer_payload(self):
        body = self.ask('customer', self.customer)
        system = self.system(body)
        self.assertIn('You help customers.', system, "the assistant's own prompt first")
        self.assertIn('CUSTOMER BRIEF: we install kitchens in Taipei.', system)
        self.assertIn('- 任務：Install cabinets', system)
        self.assertIn('Cabinets on the north wall.', system, 'the description, as plain text')
        self.assertIn('Urgent Kitchen', system)
        # the related tasks are left out: the customers of one chat may see different ones, and every
        # member reads the answer (review 33)
        for absent in ('INTERNAL BRIEF', SECRET_NOTE, SECRET_MESSAGE, '<b>', 'Negotiate supplier discount',
                       'Kitchen master plan', 'Choose handles', '上級任務', '子任務'):
            self.assertNotIn(absent, system, absent)
        # nothing internal anywhere in the request (history, metadata...)
        dump = json.dumps(body, ensure_ascii=False)
        for absent in ('INTERNAL BRIEF', SECRET_NOTE, SECRET_MESSAGE, 'Negotiate supplier discount'):
            self.assertNotIn(absent, dump, absent)
        self.assertIn('- 階段：In Progress', system)

    def test_customer_fields_follow_the_portal_readable_set(self):
        # Odoo's set decides: a field it does not list (here the tags) never reaches the customer AI
        readable = set(self.env['project.task'].SELF_READABLE_FIELDS) - {'tag_ids'}
        with patch.object(type(self.env['project.task']), 'SELF_READABLE_FIELDS', readable):
            customer = self.system(self.ask('customer', self.customer, 'first'))
            internal = self.system(self.ask('internal', self.staff, 'second'))
        self.assertNotIn('Urgent Kitchen', customer)
        self.assertIn('Urgent Kitchen', internal, 'an internal-only field stays in the internal chat')
        self.assertIn('- 任務：Install cabinets', customer)

    def test_internal_payload(self):
        system = self.system(self.ask('internal', self.staff, 'what is left?'))
        self.assertIn('You help the team.', system)
        self.assertIn('INTERNAL BRIEF: our margin is 30 percent.', system)
        self.assertNotIn('CUSTOMER BRIEF', system)
        for expected in ('Install cabinets', 'Cabinets on the north wall.', 'Staff Sam', 'Urgent Kitchen',
                         'Kitchen master plan', 'Choose handles', 'Negotiate supplier discount', '- 階段：'):
            self.assertIn(expected, system)
        for absent in (SECRET_NOTE, SECRET_MESSAGE):
            self.assertNotIn(absent, system)

    def test_built_with_the_askers_rights(self):
        # the same task, two askers: what each can read themselves (a portal user reads no user record,
        # so the assignees' names are not theirs to get)
        task = self.task.sudo()
        customer_block = task._woow_project_ai_context_block(self.customer, 'customer')
        staff_block = task._woow_project_ai_context_block(self.staff, 'customer')
        self.assertNotIn('Staff Sam', customer_block)
        self.assertIn('Staff Sam', staff_block)
        # the internal block, with the related tasks, follows the asker's rights too
        internal_block = task._woow_project_ai_context_block(self.staff, 'internal')
        self.assertIn('Negotiate supplier discount', internal_block)
        self.assertIn('Choose handles', internal_block)
        # never sudo: the block is read in the asker's environment
        seen = []
        original = task_module.ProjectTask._woow_project_ai_field_text

        def spy(record, name):
            seen.append((record.env.uid, record.env.su))
            return original(record, name)

        with patch.object(type(self.env['project.task']), '_woow_project_ai_field_text', spy):
            task._woow_project_ai_context_block(self.customer, 'customer')
        self.assertTrue(seen)
        self.assertEqual(set(seen), {(self.customer.id, False)})
        # someone who cannot read the task gets nothing
        self.assertEqual(task._woow_project_ai_context_block(self.outsider, 'customer'), '')

    def test_requester_is_of_the_tab_audience(self):
        # the summary is read with a customer's rights in the customer chat, never a
        # colleague's (who reads more: the team's subtask), whatever message is answered
        channel = self.open_tab(self.customer, 'customer')
        self.open_tab(self.staff, 'customer')
        self.post(channel, self.customer, 'question')
        self.post(channel, self.staff, 'a colleague answers')
        messages = self.env['mail.message'].sudo().search(
            [('model', '=', 'discuss.channel'), ('res_id', '=', channel.id)], order='id')
        staff_message = messages.filtered(lambda message: message.author_id == self.staff.partner_id)
        history = channel._woow_project_ai_history(self.customer_ai, messages)
        self.assertEqual(history[-1]['message_id'], staff_message.id)
        self.assertEqual(channel._woow_project_ai_requester_user(history), self.customer)
        as_trigger = channel.with_context(woow_project_ai_trigger_id=staff_message.id)
        self.assertEqual(as_trigger._woow_project_ai_requester_user(history), self.customer)
        prompt = as_trigger._woow_project_ai_system_prompt(self.customer_ai, history)
        self.assertIn('- 任務：Install cabinets', prompt)
        self.assertNotIn('Staff Sam', prompt, "read with the customer's rights, never the colleague's")
        # only colleagues wrote: no task summary at all
        self.assertFalse(channel._woow_project_ai_requester_user(history[-1:]))

    def test_block_capped(self):
        self.task.description = '<p>%s</p>' % ('長' * 20000)
        block = self.task.sudo()._woow_project_ai_context_block(self.staff, 'internal')
        self.assertLessEqual(len(block), task_module.MAX_CONTEXT_CHARS)
        self.assertIn('- 任務：Install cabinets', block)
        self.assertTrue(block.endswith(task_module.TRUNCATED))

    def test_n8n_gets_the_same_prompt(self):
        self.project.woow_project_ai_customer_assistant_id = self.customer_n8n
        body = self.ask('customer', self.customer, response=http_response(200, {'reply': 'ok'}))
        self.assertIn('CUSTOMER BRIEF', body['system_prompt'])
        self.assertNotIn('INTERNAL BRIEF', body['system_prompt'])
        self.assertNotIn(SECRET_NOTE, json.dumps(body, ensure_ascii=False))
        self.assertEqual(body['message']['text'], '[客戶 Customer Carl] 請問進度？')

    def test_new_assignee_names_follow_access(self):
        self.task.user_ids = [Command.set([self.staff.id, self.staff2.id])]
        block = self.task.sudo()._woow_project_ai_context_block(self.staff, 'internal')
        self.assertIn('Staff Sam', block)
        self.assertIn('Staff Tina', block)
