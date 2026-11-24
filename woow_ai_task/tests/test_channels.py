# -*- coding: utf-8 -*-
"""Task AI conversations: lazy creation, one per task and tab, private, the bot
member, muted staff in the customer chat, archived and deleted tasks."""

from datetime import datetime

import psycopg2

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger

from odoo.addons.woow_ai_task.hooks import uninstall_hook
from odoo.addons.woow_ai_task.models.discuss_channel import with_sync

from .common import AiTaskFixtures


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestTaskChats(AiTaskFixtures, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_ai_task()

    def test_created_on_first_open_once_per_tab(self):
        self.assertFalse(self.task.woow_project_ai_internal_channel_id)
        self.assertFalse(self.env['discuss.channel'].search([('woow_project_ai_task_id', '=', self.task.id)]))
        internal = self.open_tab(self.staff, 'internal')
        self.assertEqual(self.task.woow_project_ai_internal_channel_id, internal)
        self.assertEqual((internal.channel_type, internal.group_public_id.id, internal.woow_project_ai_tab),
                         ('group', False, 'internal'))
        self.assertEqual(internal.name, 'Install cabinets | Internal AI')
        self.assertEqual(self.members(internal), self.internal_ai.partner_id | self.staff.partner_id)
        # the next person joins the same conversation
        self.assertEqual(self.open_tab(self.staff2, 'internal'), internal)
        self.assertEqual(self.members(internal),
                         self.internal_ai.partner_id | self.staff.partner_id | self.staff2.partner_id)
        customer = self.open_tab(self.customer, 'customer')
        self.assertNotEqual(customer, internal)
        self.assertEqual(self.open_tab(self.customer2, 'customer'), customer)
        self.assertEqual(self.env['discuss.channel'].search_count([('woow_project_ai_task_id', '=', self.task.id)]), 2)
        # the database refuses a second conversation of the same tab
        with mute_logger('odoo.sql_db'), self.assertRaises(psycopg2.IntegrityError), self.env.cr.savepoint():
            with_sync(self.env['discuss.channel'].sudo()).create({
                'name': 'dup', 'channel_type': 'group', 'woow_project_ai_task_id': self.task.id,
                'woow_project_ai_tab': 'internal'})
        # renaming the task renames its conversations
        self.task.name = 'Install wall cabinets'
        self.assertEqual(internal.name, 'Install wall cabinets | Internal AI')
        self.assertEqual(customer.name, 'Install wall cabinets | Customer AI')

    def test_private(self):
        internal = self.open_tab(self.staff, 'internal')
        self.post(internal, self.staff, 'internal plans')
        # staff2 can read the task but did not open the tab: no access to the conversation
        Channel = self.env['discuss.channel'].with_user(self.staff2)
        self.assertFalse(Channel.search([('id', '=', internal.id)]))
        with self.assertRaises(AccessError):
            internal.with_user(self.staff2).read(['name'])
        Message = self.env['mail.message']
        self.assertFalse(Message.with_user(self.staff2).search(
            [('model', '=', 'discuss.channel'), ('res_id', '=', internal.id)]))
        message = Message.sudo().search([('model', '=', 'discuss.channel'), ('res_id', '=', internal.id)], limit=1)
        with self.assertRaises(AccessError):
            message.with_user(self.staff2).read(['body'])
        # nobody creates task conversations or links a channel to a task by hand
        other = self.env['discuss.channel'].with_user(self.staff).create({'name': 'mine', 'channel_type': 'group'})
        with self.assertRaises(AccessError):
            other.write({'woow_project_ai_task_id': self.task.id, 'woow_project_ai_tab': 'customer'})
        with self.assertRaises(AccessError):
            self.env['discuss.channel'].with_user(self.staff).create({
                'name': 'fake', 'channel_type': 'group', 'woow_project_ai_task_id': self.task.id,
                'woow_project_ai_tab': 'customer'})
        # nor clears the pause
        with self.assertRaises(AccessError):
            internal.with_user(self.staff).write({'woow_project_ai_paused_until': False})

    def test_customer_chat_staff_join_muted(self):
        channel = self.open_tab(self.staff, 'customer')
        member = channel.sudo().channel_member_ids.filtered(lambda m: m.partner_id == self.staff.partner_id)
        self.assertEqual(member.mute_until_dt.year, datetime.max.year, 'muted until turned back on')
        self.assertEqual(member.custom_notifications, 'no_notif')
        self.assertTrue(channel.allow_public_upload, 'portal users may upload files')
        # a customer joins with the default notifications
        self.open_tab(self.customer, 'customer')
        member = channel.sudo().channel_member_ids.filtered(lambda m: m.partner_id == self.customer.partner_id)
        self.assertFalse(member.mute_until_dt)
        # an employee who opens the tab later joins muted too
        self.open_tab(self.staff2, 'customer')
        member = channel.sudo().channel_member_ids.filtered(lambda m: m.partner_id == self.staff2.partner_id)
        self.assertTrue(member.mute_until_dt)
        self.assertFalse(self.open_tab(self.staff, 'internal').allow_public_upload)

    def test_welcome_posted_once(self):
        channel = self.open_tab(self.customer, 'customer')
        welcome = self.bot_messages(channel, self.customer_ai)
        self.assertEqual(len(welcome), 1)
        self.assertIn('客戶 AI 助理', welcome.body)
        self.open_tab(self.customer2, 'customer')
        self.open_tab(self.staff, 'customer')
        self.assertEqual(len(self.bot_messages(channel, self.customer_ai)), 1)
        self.assertFalse(self.jobs(channel), 'the welcome is never answered')
        # no welcome in the internal chat
        self.assertFalse(self.bot_messages(self.open_tab(self.staff, 'internal'), self.internal_ai))

    def test_bot_swap(self):
        internal = self.open_tab(self.staff, 'internal')
        customer = self.open_tab(self.customer, 'customer')
        self.project.woow_project_ai_customer_assistant_id = self.customer_n8n
        self.assertIn(self.customer_n8n.partner_id, self.members(customer))
        self.assertNotIn(self.customer_ai.partner_id, self.members(customer))
        self.assertIn(self.customer.partner_id, self.members(customer), 'people stay')
        self.assertIn(self.internal_ai.partner_id, self.members(internal), 'the other tab is unchanged')
        # no customer assistant any more: no bot, no tab
        self.project.woow_project_ai_customer_assistant_id = False
        self.assertNotIn(self.customer_n8n.partner_id, self.members(customer))
        self.assertFalse(self.task.woow_project_ai_customer_available)
        with self.assertRaises(UserError):
            self.open_tab(self.customer, 'customer')

    def test_archived_bot_posts(self):
        # project-only assistants keep their bot partner archived; it is still a member and posts
        self.assertFalse(self.customer_ai.partner_id.active)
        channel = self.open_tab(self.customer, 'customer')
        self.assertIn(self.customer_ai.partner_id, self.members(channel))
        self.assertTrue(self.bot_messages(channel, self.customer_ai))

    def test_tab_needs_the_assistant(self):
        project = self.env['project.project'].create({'name': 'No AI', 'privacy_visibility': 'portal',
                                                      'woow_project_ai_internal_assistant_id': self.internal_ai.id})
        task = self.env['project.task'].create({'name': 'Plain', 'project_id': project.id})
        self.assertTrue(task.woow_project_ai_internal_available)
        self.assertFalse(task.woow_project_ai_customer_available)
        with self.assertRaises(UserError):
            task.with_user(self.staff).woow_project_ai_open_tab('customer')
        with self.assertRaises(UserError):
            task.with_user(self.staff).woow_project_ai_open_tab('everyone')

    def test_archived_task_is_read_only(self):
        internal = self.open_tab(self.staff, 'internal')
        customer = self.open_tab(self.customer, 'customer')
        self.task.active = False
        # the customer cannot read an archived task (portal rules): removed; employees still read
        self.assertNotIn(self.customer.partner_id, self.members(customer))
        self.assertIn(self.staff.partner_id, self.members(internal))
        self.assertTrue(internal.with_user(self.staff).read(['name']))
        with self.assertRaises(UserError):
            self.post(internal, self.staff, 'still there?')
        self.assertFalse(self.jobs(internal))
        self.assertFalse(internal._woow_project_ai_assistant(), 'the AI does not answer')
        self.assertTrue(self.task.with_user(self.staff).woow_project_ai_open_tab('internal')['readonly'])
        # restored: answering again
        self.task.active = True
        self.post(internal, self.staff, 'back')
        self.assertEqual(len(self.jobs(internal)), 1)

    def test_deleted_task_deletes_its_chats(self):
        internal = self.open_tab(self.staff, 'internal')
        customer = self.open_tab(self.customer, 'customer')
        self.post(internal, self.staff, 'hi')
        self.task.unlink()
        self.assertFalse(internal.exists())
        self.assertFalse(customer.exists())
        self.assertFalse(self.env['mail.message'].sudo().search(
            [('model', '=', 'discuss.channel'), ('res_id', 'in', (internal | customer).ids)]))

    def test_tab_info_has_no_side_effect(self):
        # the form pager asks this before showing another task's conversation
        self.assertEqual(self.task.with_user(self.staff).woow_project_ai_tab_info('internal'), {'member': False})
        self.assertFalse(self.task.woow_project_ai_internal_channel_id, 'nothing created')
        self.open_tab(self.staff, 'internal')
        self.assertEqual(self.task.with_user(self.staff).woow_project_ai_tab_info('internal'), {'member': True})
        self.assertEqual(self.task.with_user(self.staff2).woow_project_ai_tab_info('internal'), {'member': False})
        self.assertNotIn(self.staff2.partner_id, self.members(self.task.woow_project_ai_internal_channel_id))
        with self.assertRaises(AccessError):
            self.task.with_user(self.outsider).woow_project_ai_tab_info('customer')

    def test_members_change_only_name_and_description(self):
        internal = self.open_tab(self.staff, 'internal')
        as_staff = internal.with_user(self.staff)
        for vals in ({'active': False}, {'allow_public_upload': True}, {'uuid': 'forged-uuid'}):
            with self.subTest(vals=vals), self.assertRaises(AccessError):
                as_staff.write(vals)
        self.assertTrue(internal.active)
        # what Odoo lets members do in a group chat: rename it, describe it (the task renames it again)
        as_staff.channel_rename('Cabinets, internal')
        as_staff.channel_change_description('North wall')
        self.assertEqual((internal.name, internal.description), ('Cabinets, internal', 'North wall'))
        self.task.name = 'Install cabinets (north)'
        self.assertEqual(internal.name, 'Install cabinets (north) | Internal AI')

    def test_uninstall_removes_people_and_archives(self):
        internal = self.open_tab(self.staff, 'internal')
        customer = self.open_tab(self.customer, 'customer')
        self.post(customer, self.customer, 'question')
        uninstall_hook(self.env)
        for channel in (internal, customer):
            self.assertFalse(channel.active)
        self.assertEqual(self.members(internal), self.internal_ai.partner_id)
        self.assertEqual(self.members(customer), self.customer_ai.partner_id)
        self.assertTrue(self.env['mail.message'].sudo().search(
            [('model', '=', 'discuss.channel'), ('res_id', '=', customer.id), ('body', 'ilike', 'question')]),
            'history kept')
        self.assertFalse(self.env.ref('woow_ai_task.rule_discuss_channel_woow_project_ai').active,
                         'the global rule is switched off first')

    def test_other_channels_unchanged(self):
        """Direct chats and group chats keep Odoo's behaviour (no task, no rule effect, no AI)."""
        group = self.env['discuss.channel'].with_user(self.staff).create({'name': 'team', 'channel_type': 'group'})
        group.with_user(self.staff).add_members(partner_ids=self.staff2.partner_id.ids)
        self.assertIn(self.staff2.partner_id, self.members(group))
        self.assertTrue(group.with_user(self.staff2).read(['name']))
        # members change their group chat as before (archiving included)
        group.with_user(self.staff).write({'allow_public_upload': True})
        message = group.with_user(self.staff).message_post(body='plain', message_type='comment')
        message.with_user(self.staff).write({'body': '<p>edited directly</p>'})
        self.assertFalse(group._woow_project_ai_assistant())
        self.assertFalse(self.env['woow.project.ai.job'].search([('channel_id', '=', group.id)]))
        # a direct chat with the bot partner of a project assistant: nobody answers there
        chat = self.env['discuss.channel'].with_user(self.staff).channel_get([self.internal_ai.partner_id.id])
        chat = self.env['discuss.channel'].browse(chat.id)
        chat.with_user(self.staff).message_post(body='hello bot', message_type='comment')
        self.assertFalse(chat._woow_project_ai_assistant())
        self.assertFalse(self.env['woow.project.ai.job'].search([('channel_id', '=', chat.id)]))
        self.assertTrue(chat.with_user(self.staff).woow_project_ai_readable)
        self.assertTrue(group.with_user(self.staff2).woow_project_ai_readable)
