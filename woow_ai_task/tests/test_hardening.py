# -*- coding: utf-8 -*-
"""Regression tests of the PR #4 review (review-pr4/REVIEW.md, item numbers in
the docstrings): the findings about woow_ai_task, fixed in the standalone
module; and of the independent review of this module (F numbers)."""

import json
from datetime import datetime
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests.common import HttpCase, JsonRpcException, TransactionCase, tagged
from odoo.tools import mute_logger

from odoo.addons.woow_ai_task.models.discuss_channel import with_sync

from .common import BACKEND_POST, AiTaskFixtures, http_response, openai_ok

GUARD = 'managed by the task itself'


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestTaskLinks(AiTaskFixtures, TransactionCase):
    """1 / 2 (blocker), 18: a task's links to its chats and its To-Do cannot be
    written; a tab's chat is found through the chat's own link only."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_ai_task()
        # another team's private group chat the staff member is not in
        cls.secret = cls.env['discuss.channel'].with_user(cls.manager).create(
            {'name': 'Management only', 'channel_type': 'group'})
        cls.secret.message_post(body='secret plans', message_type='comment')

    def test_pointers_are_not_writable(self):
        task = self.task.with_user(self.staff)
        for field in ('woow_project_ai_internal_channel_id', 'woow_project_ai_customer_channel_id',
                      'woow_project_ai_alert_activity_id'):
            value = self.secret.id if 'channel' in field else self.env['mail.activity'].search([], limit=1).id or 1
            with self.subTest(field=field), self.assertRaises(AccessError) as error:
                task.write({field: value})
            self.assertIn(GUARD, str(error.exception))
        with self.assertRaises(AccessError):
            self.env['project.task'].with_user(self.staff).create({
                'name': 'tampered', 'project_id': self.project.id,
                'woow_project_ai_internal_channel_id': self.secret.id})

    def test_default_context_pointers_ignored_or_refused(self):
        activity = self.task.activity_schedule('mail.mail_activity_data_todo', summary='other')
        Task = self.env['project.task'].with_user(self.staff)
        with self.assertRaises(AccessError):
            Task.with_context(default_woow_project_ai_alert_activity_id=activity.id).create(
                {'name': 'tampered', 'project_id': self.project.id})
        # the chat links are computed from the chats: a default changes nothing
        task = Task.with_context(default_woow_project_ai_internal_channel_id=self.secret.id).create(
            {'name': 'plain', 'project_id': self.project.id})
        self.assertFalse(task.woow_project_ai_internal_channel_id)
        channel = self.open_tab(self.staff, 'internal', task=task)
        self.assertNotEqual(channel, self.secret)
        self.assertEqual(task.woow_project_ai_internal_channel_id, channel)

    def test_opening_never_joins_another_channel(self):
        # even if a link were set behind the module's back (SQL), the tab uses the chat's own link
        channel = self.open_tab(self.staff, 'internal')
        self.assertEqual((channel.woow_project_ai_task_id, channel.woow_project_ai_tab), (self.task, 'internal'))
        self.assertNotIn(self.staff.partner_id, self.members(self.secret))
        self.assertEqual(self.task.with_user(self.staff2).woow_project_ai_tab_info('internal'), {'member': False})
        # joining is only for task chats, whoever calls it
        with self.assertRaises(AccessError):
            self.secret._woow_project_ai_join(self.staff)
        self.assertNotIn(self.staff.partner_id, self.members(self.secret))
        # renaming the task renames its own chats only
        self.task.name = 'Renamed task'
        self.assertEqual(self.secret.name, 'Management only')
        self.assertEqual(channel.name, 'Renamed task | Internal AI')

    def test_todo_converted_into_the_project(self):
        # project_todo: any employee writes their own to-do, then moves it into a project with an AI
        if 'project_todo' not in self.env.registry._init_modules:
            self.skipTest('project_todo is not installed')
        todo = self.env['project.task'].with_user(self.staff2).create({'name': 'my to-do'})
        with self.assertRaises(AccessError):
            todo.write({'woow_project_ai_internal_channel_id': self.secret.id})
        todo.write({'project_id': self.project.id})
        channel = self.open_tab(self.staff2, 'internal', task=todo)
        self.assertEqual(channel.woow_project_ai_task_id, todo)
        self.assertNotIn(self.staff2.partner_id, self.members(self.secret))

    def test_alert_activity_of_another_record_is_not_reused(self):
        other = self.env['res.partner'].create({'name': 'Other'}).activity_schedule(
            'mail.mail_activity_data_todo', summary='unrelated')
        # (set the way only this module can: a plain context value is no SYNC)
        with self.assertRaises(AccessError):
            self.task.with_context(woow_project_ai_sync=True).woow_project_ai_alert_activity_id = other
        with_sync(self.task.sudo()).woow_project_ai_alert_activity_id = other
        activity = self.task._woow_project_ai_alert_activity('handoff', minutes=5)
        self.assertNotEqual(activity, other, 'an activity of another record is never taken for the task To-Do')
        self.assertEqual((activity.res_model, activity.res_id), ('project.task', self.task.id))


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestContextInjection(AiTaskFixtures, HttpCase):
    """3 / 5 and 4 (major): context defaults sent by a client never create a
    task chat, nor reach the trusted create of the real one."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_ai_task()

    def setUp(self):
        super().setUp()
        patcher = patch.object(type(self.env['woow.project.ai.job']), '_start_after_commit')
        patcher.start()
        self.addCleanup(patcher.stop)

    def call_kw(self, model, method, args, kwargs=None, context=None):
        params = {'model': model, 'method': method, 'args': args, 'kwargs': dict(kwargs or {})}
        if context:
            params['kwargs']['context'] = context
        return self.make_jsonrpc_request('/web/dataset/call_kw/%s/%s' % (model, method), params)

    def test_fake_task_chat_refused(self):
        self.authenticate('aitask_employee', 'aitask_employee')
        context = {'install_mode': True, 'default_woow_project_ai_task_id': self.task.id,
                   'default_woow_project_ai_tab': 'customer'}
        with mute_logger('odoo.http'), self.assertRaises(JsonRpcException):
            self.call_kw('discuss.channel', 'create', [{'name': 'x', 'channel_type': 'channel'}], context=context)
        group = self.env['discuss.channel'].with_user(self.employee).create({'name': 'mine', 'channel_type': 'group'})
        with mute_logger('odoo.http'), self.assertRaises(JsonRpcException):
            self.call_kw('discuss.channel', 'copy', [[group.id]], context=context)
        self.assertFalse(self.env['discuss.channel'].sudo().with_context(active_test=False).search(
            [('woow_project_ai_task_id', '=', self.task.id)]))
        # the tab still opens normally
        self.assertTrue(self.open_tab(self.customer, 'customer'))
        # in Python too, whatever the route (create, copy)
        context = dict(install_mode=True, default_woow_project_ai_task_id=self.task.id,
                       default_woow_project_ai_tab='internal')
        with self.assertRaises(AccessError) as error:
            self.env['discuss.channel'].with_context(**context).create({'name': 'y', 'channel_type': 'channel'})
        self.assertIn('created by their task only', str(error.exception))
        with self.assertRaises(AccessError) as error:
            group.sudo().with_context(**context).copy()  # (sudo: an employee may not read every field to copy)
        self.assertIn('created by their task only', str(error.exception))

    def test_open_tab_ignores_the_client_context(self):
        public_channel = self.env['discuss.channel'].create({'name': 'Everyone', 'channel_type': 'channel'})
        self.authenticate('aitask_staff', 'aitask_staff')
        context = {'default_woow_project_ai_paused_until': '2099-01-01 00:00:00',
                   'default_channel_partner_ids': [self.staff.partner_id.id, self.outsider.partner_id.id],
                   'default_parent_channel_id': public_channel.id, 'install_mode': True,
                   'default_allow_public_upload': False}
        result = self.call_kw('project.task', 'woow_project_ai_open_tab', [[self.task.id], 'customer'],
                              context=context)
        channel = self.env['discuss.channel'].browse(result['channel_id'])
        self.assertFalse(channel.woow_project_ai_paused_until)
        self.assertFalse(channel.parent_channel_id)
        self.assertTrue(channel.allow_public_upload)
        self.assertEqual(self.members(channel), self.customer_ai.partner_id | self.staff.partner_id)
        self.assertNotIn(self.staff.partner_id, self.members(public_channel))


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestArchivedCompany(AiTaskFixtures, TransactionCase):
    """6 (major): a user whose Allowed Companies include an archived company
    keeps using Discuss and the task chats."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_ai_task()
        cls.old_company = cls.env['res.company'].create({'name': 'Closed Branch'})
        for user in (cls.staff, cls.staff2, cls.customer):
            user.company_ids = [Command.link(cls.old_company.id)]
        cls.old_company.active = False

    def test_discuss_and_task_chats(self):
        self.assertIn(self.old_company, self.staff.with_context(active_test=False).company_ids)
        chat = self.env['discuss.channel'].with_user(self.staff).channel_get([self.staff2.partner_id.id])
        chat = self.env['discuss.channel'].browse(chat.id)
        chat.with_user(self.staff).message_post(body='hello colleague', message_type='comment')
        # what Odoo's fetches do: searches and reads with active_test=False
        Channel = self.env['discuss.channel'].with_user(self.staff).with_context(active_test=False)
        self.assertEqual(Channel.search([('id', '=', chat.id)]), chat)
        chat.with_user(self.staff).with_context(active_test=False).check_access('read')
        messages = self.env['mail.message'].with_user(self.staff).with_context(active_test=False).search(
            [('model', '=', 'discuss.channel'), ('res_id', '=', chat.id)])
        self.assertTrue(messages)
        # task chats: members are allowed, posts and syncs work
        internal = self.open_tab(self.staff, 'internal')
        self.open_tab(self.staff2, 'internal')
        customer = self.open_tab(self.customer, 'customer')
        self.post(internal, self.staff, 'internal question')
        self.post(customer, self.customer, 'customer question')
        self.assertEqual(len(self.jobs(internal)), 1)
        self.assertEqual(len(self.jobs(customer)), 1)
        self.assertTrue(Channel.search([('id', '=', internal.id)]))
        self.task.write({'user_ids': [Command.link(self.staff2.id)]})
        self.assertIn(self.staff2.partner_id, self.members(internal))
        self.assertEqual(self.env['discuss.channel']._woow_project_ai_cron_sync(), 2)

    def test_rule_never_raises(self):
        # whatever goes wrong in the task rules for a user: task chats closed to them, the rest unchanged
        group = self.env['discuss.channel'].with_user(self.staff).create({'name': 'team', 'channel_type': 'group'})
        internal = self.open_tab(self.staff, 'internal')
        Task = type(self.env['project.task'])
        with patch.object(Task, 'has_access', side_effect=AccessError('broken company setup')), \
                mute_logger('odoo.addons.woow_ai_task.models.discuss_channel'):
            found = self.env['discuss.channel'].with_user(self.staff).search([('id', 'in', (group | internal).ids)])
        self.assertEqual(found, group)


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestUnansweredCustomers(AiTaskFixtures, TransactionCase):
    """8 (major): unanswered customer messages reach a person."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_ai_task()

    def setUp(self):
        super().setUp()
        self.customer_chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.staff, 'customer')

    def todos(self):
        return self.env['mail.activity'].search([('res_model', '=', 'project.task'), ('res_id', '=', self.task.id)])

    def test_blocked_customer_chat_schedules_one_todo(self):
        self.project.woow_project_ai_customer_assistant_id = self.customer_n8n
        self.customer_n8n.connection_id.customer_safe = False
        with patch(BACKEND_POST) as post:
            self.post(self.customer_chat, self.customer, 'is anyone there?')
            self.post(self.customer_chat, self.customer, 'hello?')
            self.run_due(self.customer_chat)
        post.assert_not_called()
        todo = self.todos()
        self.assertEqual(len(todo), 1, 'one To-Do, not one per message')
        self.assertEqual(todo.user_id, self.staff, 'the assignee')
        self.assertEqual(todo, self.task.woow_project_ai_alert_activity_id)
        self.assertIn('cannot answer', todo.note)
        # a later handoff reuses the open To-Do
        self.customer_n8n.connection_id.customer_safe = True
        self.post(self.customer_chat, self.customer, 'a person please')
        with patch(BACKEND_POST, return_value=http_response(200, {'handoff': True})):
            self.run_due(self.customer_chat)
        self.assertEqual(self.todos(), todo)
        # the internal chat's notice schedules nothing (staff read it)
        internal = self.open_tab(self.staff, 'internal')
        self.internal_ai.connection_id.action_clear_api_key()
        self.post(internal, self.staff, 'anyone?')
        self.assertEqual(self.todos(), todo)

    def test_staff_post_unmutes_only_the_author(self):
        self.open_tab(self.staff2, 'customer')
        members = self.customer_chat.sudo().channel_member_ids
        sam = members.filtered(lambda m: m.partner_id == self.staff.partner_id)
        tina = members.filtered(lambda m: m.partner_id == self.staff2.partner_id)
        self.assertTrue(sam.mute_until_dt and tina.mute_until_dt)
        self.post(self.customer_chat, self.staff, 'I take this one')
        self.assertFalse(sam.mute_until_dt)
        self.assertTrue(tina.mute_until_dt, 'the others stay muted')


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestReviewMinors(AiTaskFixtures, TransactionCase):
    """10 / 13, 11, 12, 19 / 21, 20, 25, 32, 36."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_ai_task()

    def test_project_copy_drops_assistants_that_cannot_be_chosen(self):
        # 10 / 13: Duplicate (and project templates of sale_project) never fail on a blocked tab
        self.project.woow_project_ai_customer_assistant_id = self.customer_n8n
        self.customer_n8n.connection_id.customer_safe = False
        self.internal_ai.active = False
        copy = self.project.copy()
        self.assertFalse(copy.woow_project_ai_customer_assistant_id)
        self.assertFalse(copy.woow_project_ai_internal_assistant_id)
        self.internal_ai.active = True
        self.customer_n8n.connection_id.customer_safe = True
        copy = self.project.copy()
        self.assertEqual((copy.woow_project_ai_internal_assistant_id, copy.woow_project_ai_customer_assistant_id),
                         (self.internal_ai, self.customer_n8n), 'valid choices are kept')

    def test_sweep_survives_a_deleted_chat(self):
        # 11: a chat deleted mid-sweep (its task deleted) does not abort its batch
        internal = self.open_tab(self.staff, 'internal')
        customer = self.open_tab(self.customer, 'customer')
        other_task = self.env['project.task'].create({'name': 'Doomed', 'project_id': self.project.id})
        doomed = self.open_tab(self.staff, 'internal', task=other_task)
        Channel = type(self.env['discuss.channel'])
        original = Channel.search

        def search_then_delete(channels, domain, *args, **kwargs):
            found = original(channels, domain, *args, **kwargs)
            if ('woow_project_ai_task_id', '!=', False) in domain and other_task.exists():
                other_task.unlink()  # the sweep's own search: its task deleted right after
            return found

        self.env.flush_all()
        self.env.cr.execute("DELETE FROM mail_followers WHERE res_model = 'project.task' AND res_id = %s "
                            "AND partner_id = %s", (self.task.id, self.customer.partner_id.id))
        self.env.invalidate_all()
        with patch.object(Channel, 'search', autospec=True, side_effect=search_then_delete):
            self.env['discuss.channel']._woow_project_ai_cron_sync(batch_size=10)
        self.assertFalse(doomed.exists())
        self.assertNotIn(self.customer.partner_id, self.members(customer), 'the rest of the batch was swept')
        self.assertIn(self.staff.partner_id, self.members(internal))

    def test_subtasks_follow_their_parent(self):
        # 12: a subtask moved with its parent (no write of its own) is synced
        sub = self.env['project.task'].create({'name': 'Sub', 'parent_id': self.task.id, 'display_in_project': False})
        self.assertEqual(sub.project_id, self.project)
        sub.message_subscribe(partner_ids=self.customer.partner_id.ids)
        chat = self.open_tab(self.customer, 'customer', task=sub)
        self.assertIn(self.customer.partner_id, self.members(chat))
        internal_only = self.env['project.project'].create({
            'name': 'Internal only', 'privacy_visibility': 'employees',
            'woow_project_ai_customer_assistant_id': self.customer_ai.id})
        self.task.project_id = internal_only
        self.assertEqual(sub.project_id, internal_only)
        self.assertNotIn(self.customer.partner_id, self.members(chat))

    def test_muted_join_leaves_no_cron_trigger(self):
        # 19 / 21: no 9999-12-31 unmute trigger per join
        cron = self.env.ref('mail.ir_cron_discuss_channel_member_unmute')
        before = self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id)])
        self.open_tab(self.staff, 'customer')
        self.open_tab(self.staff2, 'customer')
        self.assertEqual(self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id)]), before)
        member = self.task.woow_project_ai_customer_channel_id.sudo().channel_member_ids.filtered(
            lambda m: m.partner_id == self.staff2.partner_id)
        self.assertEqual(member.mute_until_dt.year, datetime.max.year)

    def test_task_without_project(self):
        # 20: a staff post in the customer chat of a task whose project was removed logs no error
        chat = self.open_tab(self.staff, 'customer')
        self.task.project_id = False  # a private task: its assignee (staff) still reads it
        self.assertIn(self.staff.partner_id, self.members(chat))
        self.assertEqual(self.task._woow_project_ai_pause_minutes(), 10)
        with patch('odoo.addons.woow_ai_task.models.discuss_channel._logger') as logger:
            self.post(chat, self.staff, 'still here')
        logger.exception.assert_not_called()
        self.assertTrue(chat.sudo()._woow_project_ai_is_paused())

    def test_usage_tabs_have_labels(self):
        # 25: the usage list shows "Internal AI Chat" / "Customer AI Chat", not the codes
        selection = dict(self.env['woow.project.ai.usage.month']._fields['surface_tab'].selection)
        self.assertEqual(selection, {'internal': 'Internal AI Chat', 'customer': 'Customer AI Chat'})
        self.assertEqual(dict(self.env['woow.project.ai.call.log']._fields['surface_tab'].selection), selection)

    def test_internal_tab_for_project_users_only(self):
        # 32: the internal project text is a Project field: the internal chat is for Project users
        self.assertTrue(self.task.with_user(self.employee).has_access('read'), 'an employee reads the task')
        self.assertFalse(self.task._woow_project_ai_user_allowed(self.employee, 'internal'))
        self.assertTrue(self.task._woow_project_ai_user_allowed(self.employee, 'customer'))
        internal = self.open_tab(self.staff, 'internal')
        with self.assertRaises(AccessError):
            self.open_tab(self.employee, 'internal')
        self.assertFalse(self.env['discuss.channel'].with_user(self.employee).search([('id', '=', internal.id)]))
        # (independent review F8) the task form shows the internal page to Project users only

        def form_arch(user):
            return self.env['project.task'].with_user(user).get_views([(False, 'form')])['views']['form']['arch']

        self.assertNotIn('woow_project_ai_internal_chat', form_arch(self.employee))
        self.assertIn('woow_project_ai_customer_chat', form_arch(self.employee))
        self.assertIn('woow_project_ai_internal_chat', form_arch(self.staff2))
        # a project user who loses the Project rights leaves the internal chat
        self.staff.write({'groups_id': [Command.unlink(self.env.ref('project.group_project_user').id)]})
        self.assertFalse(self.staff.has_group('project.group_project_user'))
        self.assertNotIn(self.staff.partner_id, self.members(internal))

    def test_reply_counts_labelled_as_ai_replies(self):
        # 36
        fields_ = self.env['project.project']._fields
        self.assertEqual(fields_['woow_project_ai_usage_customer_month'].string,
                         'AI Replies This Month (Customer Chats)')
        self.assertEqual(fields_['woow_project_ai_usage_internal_month'].string,
                         'AI Replies This Month (Internal Chats)')


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestIndependentReview(AiTaskFixtures, TransactionCase):
    """Findings of the independent review of the standalone module (F1, F3,
    F5, F10, F11); F6 in test_media / test_job, item 33 in test_context."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_ai_task()

    def setUp(self):
        super().setUp()
        patcher = patch.object(type(self.env['woow.project.ai.job']), '_start_after_commit')
        patcher.start()
        self.addCleanup(patcher.stop)

    def statements(self, function):
        """The SQL statements `function` runs."""
        seen = []
        execute = type(self.env.cr).execute

        def spy(cr, query, *args, **kwargs):
            seen.append(str(getattr(query, 'code', query)).strip())
            return execute(cr, query, *args, **kwargs)

        with patch.object(type(self.env.cr), 'execute', spy):
            function()
        return seen

    def test_first_open_writes_the_task_row_before_creating(self):
        # F1: of two first openers at once, the second must fail with a serialization failure (Odoo retries it,
        # and it joins the first one's chat): only a write of the task row gives that in REPEATABLE READ (a
        # plain row lock lets it create a second chat, refused by the unique constraint, not retried)
        seen = self.statements(lambda: self.open_tab(self.staff, 'internal'))
        locks = [i for i, query in enumerate(seen)
                 if query.startswith('UPDATE project_task SET write_date = write_date WHERE id')]
        creates = [i for i, query in enumerate(seen) if query.startswith('INSERT INTO "discuss_channel"')]
        self.assertTrue(locks and creates, seen[-30:])
        self.assertLess(locks[0], creates[0])
        # an existing chat is joined without writing the task
        seen = self.statements(lambda: self.open_tab(self.staff2, 'internal'))
        self.assertFalse([query for query in seen if query.startswith('UPDATE project_task')])

    def test_no_log_note_in_the_customer_chat(self):
        # F3: the customers read everything in the customer chat: no log note gets in, and the customer AI
        # never sees one
        chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.staff, 'customer')
        for author in (self.staff, self.customer):
            for extra in ({'subtype_xmlid': 'mail.mt_note'},
                          {'subtype_xmlid': 'mail.mt_comment', 'is_internal': True}):
                with self.subTest(author=author.name, extra=extra), self.assertRaises(AccessError) as error:
                    chat.with_user(author).sudo().message_post(body='a note', message_type='comment', **extra)
                self.assertIn('internal notes', str(error.exception))
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            self.env['mail.message'].with_user(self.customer).create({
                'model': 'discuss.channel', 'res_id': chat.id, 'body': 'a direct note', 'message_type': 'comment',
                'subtype_id': self.env.ref('mail.mt_note').id})
        self.assertFalse(self.env['mail.message'].sudo().search(
            [('model', '=', 'discuss.channel'), ('res_id', '=', chat.id), ('body', 'ilike', 'note')]))
        # a post without a subtype (Odoo's default would be a log note): a message the customers read
        plain = chat.with_user(self.customer).sudo().message_post(body='a plain post', message_type='comment')
        self.assertEqual(plain.subtype_id, self.env.ref('mail.mt_comment'))
        self.assertTrue(self.env['mail.message'].with_user(self.customer).search([('id', '=', plain.id)]))
        # a note that got in some other way (here: created as superuser) never reaches the customer AI
        self.env['mail.message'].sudo().create({
            'model': 'discuss.channel', 'res_id': chat.id, 'body': 'SECRET STAFF NOTE', 'message_type': 'comment',
            'subtype_id': self.env.ref('mail.mt_note').id, 'author_id': self.staff.partner_id.id})
        self.post(chat, self.customer, 'what is new?')
        with patch(BACKEND_POST, return_value=openai_ok('ok')) as post:
            self.run_due(chat)
        dump = json.dumps(self.sent(post), ensure_ascii=False)
        self.assertNotIn('SECRET STAFF NOTE', dump)
        self.assertIn('what is new?', dump)
        # the internal chat (colleagues only) keeps Odoo's behaviour
        internal = self.open_tab(self.staff, 'internal')
        note = internal.with_user(self.staff).sudo().message_post(body='internal note', message_type='comment',
                                                                  subtype_xmlid='mail.mt_note')
        self.assertTrue(note.subtype_id.internal)

    def test_ai_messages_reach_only_members_with_access(self):
        # F5: a member who lost the access in a way no trigger saw is removed before an AI reply or a push,
        # as before a person's post
        chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.customer2, 'customer')
        self.env.flush_all()
        self.env.cr.execute("DELETE FROM mail_followers WHERE res_model = 'project.task' AND res_id = %s "
                            "AND partner_id = %s", (self.task.id, self.customer.partner_id.id))
        self.env.invalidate_all()
        self.assertFalse(self.task.with_user(self.customer).has_access('read'))
        self.assertIn(self.customer.partner_id, self.members(chat), 'no trigger saw it')
        self.customer_ai.sudo()._post_text(chat, 'an AI message')
        self.assertNotIn(self.customer.partner_id, self.members(chat))
        self.assertIn(self.customer2.partner_id, self.members(chat))

    def test_new_followers_do_not_resync(self):
        # F10: a new follower only gains access: no sync of the task's chats (an unsubscription still syncs)
        self.open_tab(self.customer, 'customer')
        Channel = type(self.env['discuss.channel'])
        with patch.object(Channel, '_woow_project_ai_sync_tasks', autospec=True) as sync_tasks, \
                patch.object(Channel, '_woow_project_ai_sync_projects', autospec=True) as sync_projects:
            self.task.message_subscribe(partner_ids=self.outsider.partner_id.ids)
            self.project.message_subscribe(partner_ids=self.outsider.partner_id.ids)
            sync_tasks.assert_not_called()
            sync_projects.assert_not_called()
            self.task.message_unsubscribe(partner_ids=self.outsider.partner_id.ids)
            self.project.message_unsubscribe(partner_ids=self.outsider.partner_id.ids)
            self.assertTrue(sync_tasks.called)
            self.assertTrue(sync_projects.called)

    def test_usage_rows_carry_the_project(self):
        # F11: the usage list groups by project (a relation, not the scope's raw id)
        chat = self.open_tab(self.customer, 'customer')
        self.post(chat, self.customer, 'hello?')
        with patch(BACKEND_POST, return_value=openai_ok('hi')):
            self.run_due(chat)
        Usage = self.env['woow.project.ai.usage.month']
        rows = Usage.search([('scope_model', '=', 'project.project'), ('scope_res_id', '=', self.project.id)])
        self.assertTrue(rows)
        self.assertEqual(rows.project_id, self.project)
        groups = Usage.read_group([('id', 'in', rows.ids)], ['replies_ok:sum'], ['project_id'])
        self.assertEqual([group['project_id'] for group in groups], [(self.project.id, self.project.display_name)])


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestIndependentReviewHttp(AiTaskFixtures, HttpCase):
    """F7: the edit route runs the post check (the user's own environment)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_ai_task()

    def setUp(self):
        super().setUp()
        patcher = patch.object(type(self.env['woow.project.ai.job']), '_start_after_commit')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_archived_task_messages_stay_as_they_are(self):
        chat = self.open_tab(self.staff, 'internal')
        message = self.post(chat, self.staff, 'before archiving')
        self.task.active = False
        self.authenticate('aitask_staff', 'aitask_staff')
        for body in ('edited after archiving', ''):  # an empty body without files: a deletion
            with self.subTest(body=body), mute_logger('odoo.http'), self.assertRaises(JsonRpcException):
                self.make_jsonrpc_request('/mail/message/update_content', {
                    'message_id': message.id, 'body': body, 'attachment_ids': []})
        message.invalidate_recordset()
        self.assertIn('before archiving', message.body)
        # (the backend tab of an archived task shows no message actions: static/src/backend/task_chat.xml)
