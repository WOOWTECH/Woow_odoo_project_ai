# -*- coding: utf-8 -*-
"""Regression tests of the fix-delta review of PR #1 (review-pai1-delta/REVIEW.md:
the item numbers are in the test names). The bridges (8, 10) are in
test_coexistence."""

from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests.common import HttpCase, JsonRpcException, TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from odoo.addons.woow_ai_task.models import ai_job
from odoo.addons.woow_ai_task.models.discuss_channel import TASK_CHAT_CACHE as CACHE_ATTRIBUTE

from .common import BACKEND_POST, AiTaskFixtures, http_response, openai_ok
from .test_team_review import ReviewHelpers


class DeltaHelpers(ReviewHelpers):

    def group_of(self, user, name='mine'):
        """A group chat of `user` (another thread they may post in), with a message of theirs."""
        group = self.env['discuss.channel'].with_user(user).create({'name': name, 'channel_type': 'group'})
        own = group.with_user(user).message_post(body='my own message', message_type='comment',
                                                 subtype_xmlid='mail.mt_comment')
        return group, own

    def secret_message(self):
        internal = self.open_tab(self.staff, 'internal')
        return internal, self.post(internal, self.staff, 'INTERNAL SECRET: our margin')

    def ask(self, channel, user, response, text='q'):
        self.post(channel, user, text)
        with patch(BACKEND_POST, return_value=response) as post:
            self.run_due(channel)
        return self.sent(post) if post.called else None


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestDeltaReview(DeltaHelpers, AiTaskFixtures, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_ai_task()
        cls.sysadmin = new_test_user(cls.env, 'aitask_sysadmin', name='Admin Ada', email='ada@example.com',
                                     groups='base.group_user,base.group_system,project.group_project_user')
        cls.boss = new_test_user(cls.env, 'aitask_boss', name='Boss Bo', email='bo@example.com',
                                 groups='base.group_user,base.group_erp_manager,project.group_project_user')

    # --- 1: a chat's file never leaves it, never goes through another message ---

    def test_01_a_chat_file_stays_in_its_chat(self):
        chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.staff, 'customer')
        contract = self.file_of(chat, self.customer, 'contract.pdf', 'my signed contract')
        signed = self.find(chat, 'my signed contract')
        group, own = self.group_of(self.staff)
        Message = self.env['mail.message'].with_user(self.staff)
        attempts = {
            # /mail/message/post into another thread (Odoo keeps any file id for employees)
            'post elsewhere': lambda: self.post(group, self.staff, 'took it', attachments=contract),
            'post on the task': lambda: self.task.with_user(self.staff).message_post(
                body='took it', message_type='comment', subtype_xmlid='mail.mt_comment',
                attachment_ids=contract.ids),
            'create elsewhere': lambda: Message.create({
                'model': 'discuss.channel', 'res_id': group.id, 'body': 'took it', 'message_type': 'comment',
                'subtype_id': self.comment(), 'attachment_ids': [Command.link(contract.id)]}),
            'write elsewhere': lambda: own.with_user(self.staff).write(
                {'attachment_ids': [Command.link(contract.id)]}),
            'edit elsewhere': lambda: group.with_user(self.staff)._message_update_content(
                own.sudo(), 'took it', attachment_ids=contract.ids),
            'copy elsewhere': lambda: signed.with_user(self.staff).copy(
                {'model': 'discuss.channel', 'res_id': group.id}),
        }
        for name, attempt in attempts.items():
            with self.subTest(name), self.assertRaises(AccessError), self.env.cr.savepoint():
                attempt()
        own.invalidate_recordset()
        self.assertFalse(own.sudo().attachment_ids)
        # not even superuser links it there (the stored rows are checked: Odoo's post route runs as superuser)
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            self.env['mail.message'].create({
                'model': 'discuss.channel', 'res_id': group.id, 'body': 'old link', 'message_type': 'comment',
                'subtype_id': self.comment(), 'author_id': self.staff.partner_id.id,
                'attachment_ids': [Command.link(contract.id)]})
        # a message elsewhere that would hold such a file (a row written in SQL) never deletes it either: emptied
        # by its author, or through Odoo's delete route (with sudo for portal users)
        holder = self.env['mail.message'].create({
            'model': 'discuss.channel', 'res_id': group.id, 'body': 'old link', 'message_type': 'comment',
            'subtype_id': self.comment(), 'author_id': self.staff.partner_id.id})
        self.env.cr.execute("INSERT INTO message_attachment_rel (message_id, attachment_id) VALUES (%s, %s)",
                            (holder.id, contract.id))
        holder.invalidate_recordset(['attachment_ids'])
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            group.with_user(self.staff)._message_update_content(holder.sudo(), '', attachment_ids=[])
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            contract.with_user(self.staff).sudo()._delete_and_notify(holder)
        self.assertTrue(contract.exists())
        # nobody moves a chat file to another document: its uploader, an administrator neither
        plan = self.file_of(chat, self.staff, 'plan.pdf', 'the plan')
        for user in (self.staff, self.sysadmin):
            for vals in ({'res_id': group.id}, {'res_model': 'res.partner', 'res_id': user.partner_id.id},
                         {'res_model': 'mail.compose.message', 'res_id': 0}):
                with self.subTest(user=user.login, vals=vals), self.assertRaises(AccessError), \
                        self.env.cr.savepoint():
                    plan.with_user(user).write(vals)
        plan.with_user(self.staff).write({'res_model': 'discuss.channel', 'res_id': chat.id})  # where it is
        self.assertEqual((contract.res_model, contract.res_id), ('discuss.channel', chat.id))
        self.assertEqual((plan.res_model, plan.res_id), ('discuss.channel', chat.id))
        self.assertEqual(signed.attachment_ids, contract)
        # in the chat itself the author still deletes their own file (Odoo's paths)
        message = self.find(chat, 'the plan')
        chat.with_user(self.staff)._message_update_content(message.sudo(), '', attachment_ids=[])
        self.assertFalse(plan.exists())

    # --- 2: a copy (or an export) reads with sudo ---

    def test_02_copy_and_export_need_read_access(self):
        internal, secret = self.secret_message()
        default = {'model': False, 'res_id': False, 'attachment_ids': [], 'parent_id': False}
        # customers, an employee without Project rights: they cannot read the internal chat
        for user in (self.customer, self.outsider, self.employee):
            with self.subTest(user=user.login), self.assertRaises(AccessError), self.env.cr.savepoint():
                secret.with_user(user).copy(default)
        admin = new_test_user(self.env, 'aitask_rights_admin', groups='base.group_user,base.group_erp_manager')
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            secret.with_user(admin).export_data(['body'])  # Odoo lets administrators export any message
        self.assertEqual(self.env['mail.message'].search_count([('body', 'ilike', 'INTERNAL SECRET')]), 1)
        # people who may read the chat (its members, colleagues who may open it) copy and export as before
        copied = secret.with_user(self.staff).copy(default)
        self.assertIn('INTERNAL SECRET', copied.body)
        exported = secret.with_user(self.boss).export_data(['body'])
        self.assertIn('INTERNAL SECRET', exported['datas'][0][0])
        # any other message: Odoo's own rules, unchanged
        note = self.task.message_post(body='TASK NOTE', message_type='comment', subtype_xmlid='mail.mt_note')
        self.assertIn('TASK NOTE', note.with_user(self.staff2).copy(default).body)

    # --- 3 / 11: the bus poll checks task chats only when they have news ---

    def test_03_11_bus_poll_checks_only_task_chats_with_news(self):
        chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.customer2, 'customer')
        group = self.env['discuss.channel'].with_user(self.staff).create({'name': 'g', 'channel_type': 'group'})
        group.with_user(self.staff).add_members(partner_ids=self.customer.partner_id.ids)
        Bus = self.env['bus.bus']
        Channel = type(self.env['discuss.channel'])
        original_search = Channel.search
        searched = []

        def spy(records, domain, *args, **kwargs):
            if records.env.uid == self.customer.id:
                searched.append(domain)
            return original_search(records, domain, *args, **kwargs)

        def last_id():
            self.env.flush_all()
            self.env.cr.precommit.run()
            return Bus.sudo().search([], order='id desc', limit=1).id or 0

        def polled(since):
            self.env.flush_all()
            self.env.cr.precommit.run()
            with patch.object(Channel, 'search', spy):
                return Bus.with_user(self.customer)._poll(channels, last=since)

        def chat_notes(notifications):
            return [n for n in notifications if 'my question' in str(n['message'])]

        if hasattr(self.env.registry, CACHE_ATTRIBUTE):
            delattr(self.env.registry, CACHE_ATTRIBUTE)
        channels = [chat, (chat, 'members'), group, self.customer.partner_id, 'broadcast']
        start = last_id()
        self.assertEqual(polled(start), [])
        self.assertEqual(searched, [], 'nothing new: no access check at all')
        known = getattr(self.env.registry, CACHE_ATTRIBUTE)
        self.assertEqual((known.get(chat.id), known.get(group.id)), (True, False), 'remembered per process')
        # news in the group chat only: still no access check
        group.with_user(self.staff).message_post(body='hi', message_type='comment', subtype_xmlid='mail.mt_comment')
        self.assertTrue(polled(start))
        self.assertEqual(searched, [])
        # news in the task chat: one check, for that chat only
        start = last_id()
        self.post(chat, self.customer2, 'my question')
        self.assertTrue(chat_notes(polled(start)))
        self.assertEqual(searched, [[('id', 'in', [chat.id])]])
        # they lose the access unseen: the chat's news are left out, the group chat's are not
        self.lose_access_silently(self.customer)
        start = last_id()
        self.post(chat, self.customer2, 'my question again')
        group.with_user(self.staff).message_post(body='hi again', message_type='comment',
                                                 subtype_xmlid='mail.mt_comment')
        notifications = polled(start)
        self.assertFalse(chat_notes(notifications))
        self.assertTrue([n for n in notifications if 'hi again' in str(n['message'])])

    # --- 4 / 12: the sweep's cursor, and the cron pass's time ---

    def test_04_12_sweep_cursor_and_pass_budget(self):
        chats = self.open_tab(self.customer, 'customer') | self.open_tab(self.staff, 'internal')
        Channel = self.env['discuss.channel']
        cron = self.env.ref('woow_ai_task.ir_cron_project_ai_sync_members')
        InCron = Channel.with_context(cron_id=cron.id)
        progress = []
        with patch.object(type(self.env['ir.cron']), '_notify_progress',
                          lambda c, done, remaining, deactivate=False: progress.append((done, remaining))), \
                patch.object(type(self.env['ir.config_parameter']), 'set_param',
                             side_effect=AssertionError('the cursor clears no cache')):
            self.assertEqual(Channel._woow_project_ai_cron_sync(batch_size=1, time_slice=0), 1)
            self.assertEqual(Channel._woow_project_ai_sweep_cursor(), min(chats.ids))
            # a cron pass with too little time left: nothing synced, the pass ends, a fresh one in a minute
            with patch.object(ai_job, '_cron_pass_left', return_value=10):
                self.assertEqual(InCron._woow_project_ai_cron_sync(batch_size=1), 0)
            self.assertEqual(progress[-1], (0, 0))
            later = self.env['ir.cron.trigger'].search([('cron_id', '=', cron.id),
                                                        ('call_at', '>', fields.Datetime.now())])
            self.assertTrue(later)
            self.assertEqual(Channel._woow_project_ai_sweep_cursor(), min(chats.ids), 'it goes on from there')
            # time enough in the pass: the slice applies, from the cursor
            with patch.object(ai_job, '_cron_pass_left', return_value=1000):
                self.assertEqual(InCron._woow_project_ai_cron_sync(batch_size=1, time_slice=0), 1)
            self.assertEqual(progress[-1], (1, 0))
            self.assertEqual(Channel._woow_project_ai_sweep_cursor(), 0)
            # no limit at all (limit_time_real 0): the slice only
            with patch.object(ai_job, '_cron_pass_left', return_value=None):
                self.assertEqual(InCron._woow_project_ai_cron_sync(batch_size=10), 2)

    def test_12_pass_left_follows_the_cron_limit(self):
        with patch.object(ai_job, '_cron_limit', return_value=0):
            self.assertIsNone(ai_job._cron_pass_left())
        with patch.object(ai_job, '_cron_limit', return_value=120), \
                patch.object(ai_job, '_prefork_pass_used', return_value=70.0):
            self.assertEqual(ai_job._cron_pass_left(), 50.0)

    # --- 5: system administrators manage files ---

    def test_05_system_administrators_manage_chat_files(self):
        chat = self.open_tab(self.customer, 'customer')
        contract = self.file_of(chat, self.customer, 'contract.pdf', 'signed')
        other = self.env['ir.attachment'].create({'name': 'other.txt', 'raw': b'other'})
        Attachment = self.env['ir.attachment'].with_user(self.sysadmin)
        # what force_storage's migration writes, as the administrator
        Attachment.browse(contract.id).write({'raw': contract.raw, 'mimetype': contract.mimetype})
        # a bulk delete with a chat file in it
        Attachment.browse([contract.id, other.id]).unlink()
        self.assertFalse(contract.exists() or other.exists())
        # a colleague still cannot
        self.open_tab(self.staff, 'customer')
        receipt = self.file_of(chat, self.customer, 'receipt.pdf', 'receipt')
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            receipt.with_user(self.staff).unlink()

    # --- 6: only the author edits a task chat message ---

    def test_06_only_its_author_edits_a_chat_message(self):
        chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.boss, 'customer')
        contract = self.file_of(chat, self.customer, 'contract.pdf', 'I accept the quote')
        message = self.find(chat, 'I accept the quote')
        for body in ('I accept the extra cost', ''):
            with self.subTest(body=body), self.assertRaises(AccessError), self.env.cr.savepoint():
                chat.with_user(self.boss)._message_update_content(message.sudo(), body, attachment_ids=[])
        message.invalidate_recordset()
        self.assertIn('I accept the quote', message.body)
        self.assertTrue(contract.exists())
        # the AI's message neither
        self.customer_ai.sudo()._post_text(chat, 'the AI answer')
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            chat.with_user(self.boss)._message_update_content(self.find(chat, 'the AI answer').sudo(), 'edited')
        # their own: fine
        own = self.post(chat, self.boss, 'a colleague writes')
        chat.with_user(self.boss)._message_update_content(own.sudo(), 'a colleague writes, edited')
        self.assertIn('edited', own.body)

    # --- 7: the internal chat needs the project (its text is in the answers) ---

    def test_07_internal_chat_needs_the_project(self):
        private = self.env['project.project'].create({
            'name': 'Hidden', 'privacy_visibility': 'followers', 'user_id': self.manager.id,
            'woow_project_ai_internal_assistant_id': self.internal_ai.id,
            'woow_project_ai_internal_brief': 'HIDDEN PROJECT TEXT'})
        task = self.env['project.task'].create({'name': 'Followed task', 'project_id': private.id,
                                                'user_ids': [Command.set([self.staff2.id])]})
        task.message_subscribe(partner_ids=self.staff.partner_id.ids)
        for user in (self.staff, self.staff2):  # a follower and an assignee of the task, not of the project
            self.assertTrue(task.with_user(user).has_access('read'))
            self.assertFalse(private.with_user(user).has_access('read'))
            self.assertFalse(task._woow_project_ai_user_allowed(user, 'internal'))
            with self.subTest(user=user.login), self.assertRaises(AccessError), self.env.cr.savepoint():
                self.open_tab(user, 'internal', task=task)
        # once they follow the project: in, and the project text goes to the AI
        private.message_subscribe(partner_ids=(self.staff | self.staff2).partner_id.ids)
        chat = self.open_tab(self.staff, 'internal', task=task)
        self.open_tab(self.staff2, 'internal', task=task)
        body = self.ask(chat, self.staff, openai_ok('ok'), text='what is this project about?')
        self.assertIn('HIDDEN PROJECT TEXT', body['messages'][0]['content'])
        self.assertEqual(body['metadata'].get('surface_project_id'), str(private.id))
        # one of them stops following the project: out of the chat (sync), and it reads nothing of it
        private.message_unsubscribe(partner_ids=self.staff2.partner_id.ids)
        self.assertNotIn(self.staff2.partner_id, self.members(chat))
        self.assertIn(self.staff.partner_id, self.members(chat))
        self.assertFalse(self.env['discuss.channel'].with_user(self.staff2).search([('id', '=', chat.id)]))
        # the rule alone (before any sync): someone who reads the task only cannot read the chat
        self.env.flush_all()
        self.env.cr.execute("DELETE FROM mail_followers WHERE res_model = 'project.project' AND res_id = %s "
                            "AND partner_id = %s", (private.id, self.staff.partner_id.id))
        self.env.invalidate_all()
        self.assertTrue(task.with_user(self.staff).has_access('read'))
        self.assertFalse(self.env['discuss.channel'].with_user(self.staff).search([('id', '=', chat.id)]))

    # --- 13: the parent check reads no message row ---

    def test_13_parent_check_reads_ids_only(self):
        note = self.task.message_post(body='<p>%s</p>' % ('long body ' * 500), message_type='comment',
                                      subtype_xmlid='mail.mt_note')
        child = self.env['mail.message'].create({
            'model': 'project.task', 'res_id': self.task.id, 'body': 'a reply', 'message_type': 'comment',
            'subtype_id': self.env.ref('mail.mt_note').id, 'parent_id': note.id})
        self.env.invalidate_all()
        Message = type(self.env['mail.message'])
        original = Message.fetch
        fetched = []

        def spy(records, field_names):
            fetched.append(set(records._ids))
            return original(records, field_names)

        with patch.object(Message, 'fetch', spy):
            child.with_user(self.staff)._woow_project_ai_check_stored()
        self.assertFalse([ids for ids in fetched if note.id in ids], 'the parent is never read')
        # and a parent in a task chat is refused, as stored, for superuser too
        _internal, secret = self.secret_message()
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            child.sudo().parent_id = secret

    # --- 18: the sync cron is not rewritten by an upgrade ---

    def test_18_sync_cron_is_noupdate(self):
        data = self.env['ir.model.data'].search([('module', '=', 'woow_ai_task'),
                                                 ('name', '=', 'ir_cron_project_ai_sync_members')])
        self.assertTrue(data.noupdate)

    # --- 19: the customer chat's project only when every customer reads it ---

    def test_19_customer_surface_project_needs_every_customer(self):
        self.project.woow_project_ai_customer_assistant_id = self.customer_n8n
        self.project.message_subscribe(partner_ids=self.customer.partner_id.ids)
        chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.customer2, 'customer')
        self.assertTrue(self.project.with_user(self.customer).has_access('read'))
        self.assertFalse(self.project.with_user(self.customer2).has_access('read'))
        body = self.ask(chat, self.customer, http_response(200, {'reply': 'ok'}))
        self.assertIsNone(body['surface']['project_id'], 'the other customer reads the answer, not the project')
        self.project.message_subscribe(partner_ids=self.customer2.partner_id.ids)
        body = self.ask(chat, self.customer, http_response(200, {'reply': 'ok'}), text='q2')
        self.assertEqual(body['surface']['project_id'], self.project.id)


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestDeltaReviewHttp(DeltaHelpers, AiTaskFixtures, HttpCase):
    """The same, through Odoo's own routes."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_ai_task()
        cls.boss = new_test_user(cls.env, 'aitask_boss', name='Boss Bo', email='bo@example.com',
                                 groups='base.group_user,base.group_erp_manager,project.group_project_user')

    def call_kw(self, model, method, args, kwargs=None):
        params = {'model': model, 'method': method, 'args': args, 'kwargs': dict(kwargs or {})}
        return self.make_jsonrpc_request('/web/dataset/call_kw/%s/%s' % (model, method), params)

    def refused(self, route, params):
        with mute_logger('odoo.http'), self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request(route, params)

    def test_01_no_detour_through_another_thread(self):
        chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.staff, 'customer')
        contract = self.file_of(chat, self.customer, 'contract.pdf', 'my signed contract')
        group, own = self.group_of(self.staff)
        self.authenticate('aitask_staff', 'aitask_staff')
        self.refused('/mail/message/post', {
            'thread_model': 'discuss.channel', 'thread_id': group.id,
            'post_data': {'body': 'took it', 'message_type': 'comment', 'subtype_xmlid': 'mail.mt_comment',
                          'attachment_ids': contract.ids}})
        self.refused('/mail/message/update_content', {'message_id': own.id, 'body': 'took it',
                                                      'attachment_ids': contract.ids})
        self.refused('/web/dataset/call_kw/mail.message/create', {
            'model': 'mail.message', 'method': 'create', 'kwargs': {}, 'args': [{
                'model': 'discuss.channel', 'res_id': group.id, 'body': 'took it', 'message_type': 'comment',
                'subtype_id': self.comment(), 'attachment_ids': [Command.link(contract.id)]}]})
        self.make_jsonrpc_request('/mail/message/update_content', {'message_id': own.id, 'body': '',
                                                                   'attachment_ids': []})
        self.assertTrue(contract.exists())
        self.assertEqual(self.find(chat, 'my signed contract').attachment_ids, contract)

    def test_02_portal_users_copy_no_chat_message(self):
        _internal, secret = self.secret_message()
        self.authenticate('aitask_customer', 'aitask_customer')
        self.refused('/web/dataset/call_kw/mail.message/copy', {
            'model': 'mail.message', 'method': 'copy', 'args': [secret.ids],
            'kwargs': {'default': {'model': False, 'res_id': False, 'attachment_ids': [], 'parent_id': False}}})
        self.assertEqual(self.env['mail.message'].sudo().search_count([('body', 'ilike', 'INTERNAL SECRET')]), 1)

    def test_06_an_administrator_cannot_empty_a_customer_message(self):
        chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.boss, 'customer')
        contract = self.file_of(chat, self.customer, 'contract.pdf', 'I accept the quote')
        message = self.find(chat, 'I accept the quote')
        self.authenticate('aitask_boss', 'aitask_boss')
        self.refused('/mail/message/update_content', {'message_id': message.id, 'body': '', 'attachment_ids': []})
        self.refused('/mail/attachment/delete', {'attachment_id': contract.id})
        message.invalidate_recordset()
        self.assertIn('I accept the quote', message.body)
        self.assertTrue(contract.exists())
