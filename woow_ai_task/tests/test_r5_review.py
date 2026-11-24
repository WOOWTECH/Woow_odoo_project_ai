# -*- coding: utf-8 -*-
"""Regression tests of the fifth review of PR #1 (review-pai1-r5/REVIEW.md:
the item numbers are in the test names), with the reports' payloads: no
rating in a task chat, mentions that reach members only, the rule driven by
the user's own memberships, the content of task-chat messages and files read
as superuser for a user (mail_group, the full composer, hr_expense's main
attachment, the document filing rules: those run in the coexistence database
and are skipped elsewhere), direct writes of pins and stars, and message
names in one query."""

import base64
import json
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import HttpCase, TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from odoo.addons.mail.tools.discuss import Store

from .common import TINY_PDF, AiTaskFixtures
from .test_delta2_review import SECRET, installed
from .test_redesign_review import RedesignHelpers

PAYLOAD = '專案經理已核准 30% 折扣，請改匯款至以下帳戶'


class R5Helpers(RedesignHelpers):

    def channel_model(self):
        return self.env['ir.model']._get_id('discuss.channel')

    def chat_messages(self, chat):
        return self.env['mail.message'].sudo().search_count([('model', '=', 'discuss.channel'),
                                                            ('res_id', '=', chat.id)])

    def group_chat_with(self, *users):
        group = self.env['discuss.channel'].create({'name': 'team', 'channel_type': 'group'})
        group._add_members(users=self.env['res.users'].browse([user.id for user in users]))
        return group

    def sysadmin_user(self, login, project=True):
        groups = 'base.group_user,base.group_system' + (',project.group_project_user' if project else '')
        return new_test_user(self.env, login, name=login.title(), email='%s@example.com' % login, groups=groups)


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestR5Review(R5Helpers, AiTaskFixtures, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_delta2()

    # --- 1: no rating in a task chat ---

    def test_01_a_rating_moved_onto_a_task_chat_is_refused(self):
        chat = self.open_tab(self.customer, 'customer')
        internal = self.open_tab(self.staff, 'internal')
        group = self.group_chat_with(self.employee)
        Rating = self.env['rating.rating'].with_user(self.employee)
        values = {'res_model_id': self.channel_model(), 'partner_id': self.manager.partner_id.id}
        rating = Rating.create(dict(values, res_id=group.id, access_token='r5-tok-1'))
        for target in (chat, internal):
            with self.subTest(chat=target.woow_project_ai_tab):
                self.refused(lambda: rating.write({'res_id': target.id}))
                self.refused(lambda: Rating.create(dict(values, res_id=target.id, access_token='r5-tok-2')))
                self.refused(lambda: rating.write({'res_id': str(target.id)}))
        self.assertEqual(rating.sudo().res_id, group.id)

    def test_01_b_rating_apply_on_a_task_chat_posts_nothing(self):
        chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.staff, 'customer')
        # a rating that points at the chat (only superuser can make one now)
        rating = self.env['rating.rating'].create({
            'res_model_id': self.channel_model(), 'res_id': chat.id, 'partner_id': self.manager.partner_id.id,
            'access_token': 'r5-tok-3'})
        before = self.chat_messages(chat)
        # what /rate/<token>/submit_feedback does: the record as superuser, the rating's partner as author
        for call in (lambda: chat.sudo().rating_apply(5, rating=rating, feedback=PAYLOAD),
                     lambda: chat.sudo().rating_apply(5, token='r5-tok-3', feedback=PAYLOAD)):
            with self.assertRaises(AccessError), self.env.cr.savepoint():
                call()
        self.assertEqual(self.chat_messages(chat), before)
        self.assertFalse(self.find(chat, '30%'))
        # the post itself refuses a rating, whoever posts
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            chat.sudo().message_post(body=PAYLOAD, author_id=self.manager.partner_id.id, rating_value=5)
        # Odoo's ratings elsewhere still work
        group = self.group_chat_with(self.employee)
        other = self.env['rating.rating'].create({
            'res_model_id': self.channel_model(), 'res_id': group.id, 'partner_id': self.employee.partner_id.id,
            'access_token': 'r5-tok-4'})
        group.sudo().rating_apply(4, rating=other, feedback='fine')
        self.assertEqual(other.rating, 4)

    # --- 2: a mention never adds anyone ---

    def test_02_mentions_reach_members_only(self):
        chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.staff, 'customer')
        internal = self.open_tab(self.staff, 'internal')
        everyone = (self.staff | self.staff2 | self.employee | self.manager | self.customer2).partner_id
        for user in (self.staff2, self.employee, self.manager):
            user.notification_type = 'inbox'
        members = {chat: self.members(chat), internal: self.members(internal)}

        def unread_of_others(target):
            return sum(self.unread(partner, target) for partner in everyone - members[target])

        # a portal author posts mentioning every employee (a crafted JSON-RPC post)
        post = chat.with_user(self.customer).sudo().message_post(
            body='<p>everyone, look</p>', message_type='comment', subtype_xmlid='mail.mt_comment',
            partner_ids=everyone.ids)
        self.assertEqual(post.sudo().partner_ids, self.staff.partner_id, 'only the member among them')
        # the portal author edits their own message (/mail/message/update_content passes partner_ids as given)
        chat.with_user(self.customer)._message_update_content(
            post.sudo(), body='<p>everyone, look again</p>', attachment_ids=[], partner_ids=everyone.ids)
        self.assertEqual(post.sudo().partner_ids, self.staff.partner_id)
        # an employee member of the internal chat, by message_post and by an edit of their own message
        own = internal.with_user(self.staff).message_post(
            body='<p>team?</p>', message_type='comment', subtype_xmlid='mail.mt_comment', partner_ids=everyone.ids)
        self.assertFalse(own.sudo().partner_ids - self.staff.partner_id)
        internal.with_user(self.staff)._message_update_content(
            own.sudo(), body='<p>team, again?</p>', attachment_ids=[], partner_ids=everyone.ids)
        self.assertFalse(own.sudo().partner_ids - self.staff.partner_id)
        for target in (chat, internal):
            with self.subTest(chat=target.woow_project_ai_tab):
                self.assertEqual(self.members(target), members[target], 'nobody joined')
                self.assertEqual(unread_of_others(target), 0, 'nobody else notified')

    # --- 3 / 6 / 9: the rule from the user's own memberships ---

    def test_03_rule_of_users_and_administrators(self):
        Channel = self.env['discuss.channel']
        internal = self.open_tab(self.staff, 'internal')
        customer_chat = self.open_tab(self.staff, 'customer')
        group = self.public_channel()
        # Tina may read both chats but is in neither: no task chat at all, nothing to plan
        Tina = Channel.with_user(self.staff2)
        self.assertEqual(Tina._woow_project_ai_rule(), {'admin': False, 'ids': []})
        self.assertEqual(Tina._search_woow_project_ai_readable('=', True), [('woow_project_ai_task_id', '=', False)])
        self.assertEqual(Tina.search([('id', 'in', (internal | customer_chat | group).ids)]), group)
        # her own tab: that chat only
        self.open_tab(self.staff2, 'internal')
        self.assertEqual(Tina._woow_project_ai_rule(), {'admin': False, 'ids': [internal.id]})
        self.assertEqual(Tina.search([('id', 'in', (internal | customer_chat | group).ids)]), internal | group)
        # Sam: his two chats
        self.assertEqual(Channel.with_user(self.staff)._woow_project_ai_rule()['ids'],
                         sorted((internal | customer_chat).ids))
        # a system administrator (Odoo lets them read every channel): the compiled sub-query, its brief-project
        # condition uncorrelated, the relation table among the fields it reads
        admin = self.sysadmin_user('aitask_r5_admin')
        rule = Channel.with_user(admin)._woow_project_ai_rule()
        self.assertTrue(rule['admin'])
        self.assertIn('NOT IN (SELECT channel_id FROM woow_project_ai_channel_brief_project_rel WHERE project_id '
                      'NOT IN', rule['code'])
        self.assertIn(['discuss.channel', 'woow_project_ai_brief_project_ids'], [list(item) for item in rule['flush']])
        self.assertEqual(Channel.with_user(admin).search([('id', 'in', (internal | customer_chat).ids)]),
                         internal | customer_chat)
        # one without Project rights reads the customer chat (a portal project's task), not the internal one
        it_admin = self.sysadmin_user('aitask_r5_it', project=False)
        self.assertEqual(Channel.with_user(it_admin).search([('id', 'in', (internal | customer_chat).ids)]),
                         customer_chat)
        # a brief project the administrator cannot read closes the internal chat to them
        hidden = self.env['project.project'].create({'name': 'Hidden P2', 'privacy_visibility': 'followers'})
        internal.sudo().woow_project_ai_brief_project_ids = [Command.link(hidden.id)]
        restricted = self.env['res.users'].create({
            'name': 'Admin Without P2', 'login': 'aitask_r5_admin2', 'email': 'admin2@example.com',
            'groups_id': [Command.set([self.env.ref('base.group_system').id,
                                       self.env.ref('project.group_project_user').id])]})
        self.assertFalse(hidden.with_user(restricted).has_access('read'))
        self.assertEqual(Channel.with_user(restricted).search([('id', 'in', (internal | customer_chat).ids)]),
                         customer_chat)

    def test_03_one_query_per_channel_search(self):
        internal = self.open_tab(self.staff, 'internal')
        group = self.public_channel()
        Channels = self.env['discuss.channel'].with_user(self.staff2)
        self.env.flush_all()
        self.env.invalidate_all()
        # the rule of the transaction (the user's task chats: one indexed query), then one query per search
        Channels.search([('id', '=', group.id)])
        with self.assertQueryCount(1):
            self.assertEqual(Channels.search([('id', '=', group.id)]), group)
        # a task chat they are in: the same
        self.open_tab(self.staff2, 'internal')
        Channels.search([('id', '=', internal.id)])
        with self.assertQueryCount(1):
            self.assertEqual(Channels.search([('id', '=', internal.id)]), internal)

    def test_07_10_exclusions_test_each_row(self):
        """The generic message exclusion and the mail queue's look at each message's own channel: no set of all
        task chats (nor of their messages) is built for a statement."""
        internal = self.open_tab(self.staff, 'internal')
        Message = self.env['mail.message'].with_user(self.staff2)
        exclusion = Message._woow_project_ai_search_exclusion([('needaction', '=', True)])
        self.assertEqual(exclusion[:2], ['|', ('model', '!=', 'discuss.channel')])
        correlated = exclusion[2][2]
        self.assertIn('woow_c.id = "mail_message"."res_id"', correlated.code)
        self.assertEqual(len(exclusion), 3, 'Tina is in no task chat: nothing else to plan')
        # a member: their readable chats, a literal list
        self.open_tab(self.staff2, 'internal')
        exclusion = Message._woow_project_ai_search_exclusion([('needaction', '=', True)])
        self.assertEqual(exclusion[-1], ('res_id', 'in', [internal.id]))
        # the results: as before
        secret = self.post(internal, self.staff, 'r5 row test')
        self.assertIn(secret, Message.search([('body', 'ilike', 'r5 row test')]))
        self.assertFalse(self.env['mail.message'].with_user(self.employee).search([('body', 'ilike', 'r5 row test')]))

    def test_03_leaving_closes_the_chat(self):
        internal, secret = self.secret_message()
        self.open_tab(self.staff2, 'internal')
        self.assertIn(SECRET, secret.with_user(self.staff2).read(['body'])[0]['body'])
        internal.with_user(self.staff2).action_unfollow()  # Odoo's Leave
        self.assertNotIn(self.staff2.partner_id, self.members(internal))
        self.refused(lambda: secret.with_user(self.staff2).read(['body']))
        Messages = self.env['mail.message'].with_user(self.staff2)
        self.assertFalse(Messages.search([('id', '=', secret.id)]))
        self.assertFalse(Messages.search([('body', 'ilike', 'INTERNAL SECRET')]))
        # opening the tab again: back in
        self.open_tab(self.staff2, 'internal')
        self.assertEqual(Messages.search([('id', '=', secret.id)]), secret)

    def test_10_mail_queue_with_hundreds_of_chat_mails(self):
        internal, secret = self.secret_message()
        note = self.task.message_post(body='an ordinary note', message_type='comment', subtype_xmlid='mail.mt_note')
        Mail = self.env['mail.mail']
        chat_mails = Mail.create([{'mail_message_id': secret.id, 'body_html': '<p>q</p>', 'state': 'exception'}
                                  for _i in range(300)])
        other_mails = Mail.create([{'mail_message_id': note.id, 'body_html': '<p>q</p>', 'state': 'exception'}
                                   for _i in range(300)])
        it_admin = self.sysadmin_user('aitask_r5_it', project=False)
        manager = new_test_user(self.env, 'aitask_r5_itm', name='IT Manager', email='itm5@example.com',
                                groups='base.group_user,base.group_system,project.group_project_manager')
        self.env.flush_all()
        self.env.invalidate_all()
        ids = (chat_mails | other_mails).ids
        self.assertEqual(Mail.with_user(it_admin).search_count([('id', 'in', ids)]), 300)
        self.assertEqual(set(Mail.with_user(it_admin).search([('id', 'in', ids)]).ids), set(other_mails.ids))
        self.assertEqual(Mail.with_user(manager).search_count([('id', 'in', ids)]), 600)

    # --- 4 / 5 / 8 / 14: content read as superuser for a user (Odoo's sudo reads, related fields) ---

    def test_04_superuser_reads_for_a_user(self):
        internal, secret = self.secret_message()
        _same, plan = internal, self.file_of(internal, self.staff, 'plan.pdf', 'the internal plan')
        for user in (self.employee, self.outsider):
            with self.subTest(user=user.login):
                # a message's content, as superuser for someone who may not read the chat: refused
                for name in ('body', 'subject', 'author_id', 'partner_ids', 'record_name', 'email_from',
                             'attachment_ids'):
                    self.refused(lambda: secret.with_user(user).sudo()[name])
                # what Odoo's rules and routing read: read, without the content coming with it
                self.env.invalidate_all()
                self.assertEqual(secret.with_user(user).sudo().model, 'discuss.channel')
                self.assertFalse(self.env.cache.contains(secret, secret._fields['subject']))
                self.refused(lambda: secret.with_user(user).sudo().subject)
                # a file's content and name: refused; its place read; datas empty and the label as before
                for name in ('checksum', 'name', 'mimetype', 'file_size', 'raw', 'access_token'):
                    self.refused(lambda: plan.with_user(user).sudo()[name])
                self.env.invalidate_all()
                self.assertEqual(plan.with_user(user).sudo().res_model, 'discuss.channel')
                self.assertFalse(plan.with_user(user).sudo().datas)
                self.assertEqual(plan.with_user(user).sudo().display_name, 'Task AI chat file')
        # a member reads them as superuser too; and the superuser account reads everything
        self.env.invalidate_all()
        self.assertIn(SECRET, secret.with_user(self.staff).sudo().body)
        self.assertEqual(plan.with_user(self.staff).sudo().name, 'plan.pdf')
        self.env.invalidate_all()
        self.assertIn(SECRET, self.env['mail.message'].browse(secret.id).body)

    def test_04_failures_of_a_lost_chat_still_load(self):
        internal, secret = self.secret_message()
        self.env['mail.notification'].create({
            'mail_message_id': secret.id, 'res_partner_id': self.customer.partner_id.id, 'author_id':
            self.staff.partner_id.id, 'notification_type': 'email', 'notification_status': 'exception'})
        # Sam loses the internal chat (its project for followers only)
        self.project.privacy_visibility = 'followers'
        self.assertFalse(self.project.with_user(self.staff).has_access('read'))
        self.env.flush_all()
        self.env.invalidate_all()
        # what /mail/data's failures do: the author's failed notifications as superuser for them
        notifications = self.env['mail.notification'].with_user(self.staff).sudo().search([
            ('author_id', '=', self.staff.partner_id.id), ('notification_status', 'in', ('bounce', 'exception'))])
        self.assertTrue(notifications)
        store = Store()
        notifications.mail_message_id._message_notifications_to_store(store)
        self.assertNotIn(SECRET, json.dumps(store.get_result(), ensure_ascii=False, default=str))

    def test_04_mail_group_related_fields(self):
        if not installed(self.env, 'mail_group'):
            self.skipTest('mail_group is not installed')
        internal, secret = self.secret_message()
        spec = {'body': {}, 'author_id': {}, 'subject': {}, 'email_from': {}, 'attachment_ids': {}}
        Group = self.env['mail.group.message']
        for user in (self.outsider, self.employee):
            with self.subTest(user=user.login):
                self.refused(lambda: Group.with_user(user).onchange(
                    {'mail_message_id': secret.id}, ['mail_message_id'], spec))
        # a reader of the chat gets the body (the payload does read it)
        self.env.invalidate_all()
        result = Group.with_user(self.staff).onchange({'mail_message_id': secret.id}, ['mail_message_id'], spec)
        self.assertIn(SECRET, json.dumps(result, ensure_ascii=False, default=str))

    def test_05_composer_parent_fields(self):
        internal, secret = self.secret_message()
        self.open_tab(self.staff2, 'internal')
        mention = self.mention(internal, self.staff, self.staff2.partner_id, '<p>Tina, look</p>')
        spec = {'model': {}, 'res_ids': {}, 'record_name': {}, 'subject': {}, 'partner_ids': {}}
        Composer = self.env['mail.compose.message']
        for user in (self.outsider, self.employee):
            with self.subTest(user=user.login):
                self.refused(lambda: Composer.with_user(user).onchange(
                    {'parent_id': mention.id, 'composition_mode': 'comment'}, ['parent_id'], spec))
        self.refused(lambda: Composer.with_user(self.employee).create(
            {'parent_id': mention.id, 'composition_mode': 'comment'}).read(['model', 'res_ids', 'partner_ids']))
        # a reader: as before
        self.env.invalidate_all()
        result = Composer.with_user(self.staff).onchange(
            {'parent_id': mention.id, 'composition_mode': 'comment'}, ['parent_id'], spec)
        self.assertEqual(result['value']['model'], 'discuss.channel')

    def test_08_main_attachment_never_a_task_chat_file(self):
        if not installed(self.env, 'hr_expense'):
            self.skipTest('hr_expense is not installed')
        _internal, plan = self.internal_file()
        employee = self.env['hr.employee'].create({'name': 'Employee Eve', 'user_id': self.employee.id})
        Expense = self.env['hr.expense'].with_user(self.employee)
        expense = Expense.create({'name': 'Taxi', 'employee_id': employee.id, 'total_amount_currency': 10.0})
        # Eve's own draft expense: a task-chat file as its main attachment is refused, as stored (a create
        # writes a string or float id into its INSERT; a write of one stores nothing)
        self.refused(lambda: expense.write({'message_main_attachment_id': plan.id}))
        for value in (plan.id, str(plan.id), plan.id + 0.0):
            with self.subTest(value=value):
                self.refused(lambda: Expense.create({'name': 'Taxi 2', 'employee_id': employee.id,
                                                     'total_amount_currency': 5.0,
                                                     'message_main_attachment_id': value}))
        expense.write({'message_main_attachment_id': str(plan.id)})
        self.env.flush_all()
        self.env.cr.execute("SELECT message_main_attachment_id FROM hr_expense WHERE id = %s", [expense.id])
        self.assertIsNone(self.env.cr.fetchone()[0])
        # the checksum (a related field read as superuser) through an onchange: refused
        for user in (self.employee, self.outsider):
            with self.subTest(user=user.login):
                self.refused(lambda: self.env['hr.expense'].with_user(user).onchange(
                    {'message_main_attachment_id': plan.id}, ['message_main_attachment_id'],
                    {'message_main_attachment_checksum': {}}))
        # Odoo's own setter (superuser, the record's own files) still works
        own = self.env['ir.attachment'].create({'name': 'receipt.pdf', 'raw': TINY_PDF, 'res_model': 'hr.expense',
                                                'res_id': expense.id})
        expense.sudo()._message_set_main_attachment_id(own)
        self.assertEqual(expense.sudo().message_main_attachment_id, own)

    def test_14_filing_rules_skip_task_chat_files(self):
        if not installed(self.env, 'woow_document_management', 'woow_ai_task_document_management'):
            self.skipTest('woow_document_management is not installed')
        maker = new_test_user(self.env, 'aitask_r5_docadmin', name='Doc Admin', email='doc5@example.com',
                              groups='base.group_user,base.group_system,project.group_project_user,'
                                     'woow_document_management.group_woow_document_manager')
        folder = self.env['woow.document.folder'].sudo().create({'name': 'Chat files', 'owner_id': maker.id})
        rule = self.env['woow.document.auto.rule'].with_user(maker).create({
            'name': 'Chat files', 'model_id': self.channel_model(), 'folder_id': folder.id})
        Document = self.env['woow.document'].sudo().with_context(active_test=False)
        # a customer's file posted in the customer chat, and one uploaded straight onto it: not filed
        chat, contract = self.customer_file()
        upload = self.env['ir.attachment'].with_user(self.customer).sudo().create({
            'name': 'id-card.pdf', 'raw': TINY_PDF, 'res_model': 'discuss.channel', 'res_id': chat.id})
        # an ordinary group chat's file: filed (the rule works)
        group = self.group_chat_with(self.employee)
        ordinary = self.env['ir.attachment'].with_user(self.employee).create({
            'name': 'note.txt', 'raw': b'hello filing', 'res_model': 'discuss.channel', 'res_id': group.id})
        self.assertFalse(Document.search([('source_attachment_id', 'in', (contract | upload).ids)]))
        self.assertTrue(Document.search([('source_attachment_id', '=', ordinary.id)]))
        # the backfill of existing files: not those of task chats either
        result = rule.with_user(maker)._woow_backfill()
        self.assertFalse(Document.search([('source_attachment_id', 'in', (contract | upload).ids)]))
        self.assertEqual(result['left'], 0)

    # --- 11: no direct write of pins and stars ---

    def test_11_no_direct_pin_or_star_writes(self):
        internal, secret = self.secret_message()
        self.open_tab(self.staff2, 'internal')
        mention = self.mention(internal, self.staff, self.staff2.partner_id, '<p>Tina, look</p>')
        now = fields.Datetime.now()
        # Tina is taken out of the project: she stays a notified partner of the mention
        self.project.message_subscribe(partner_ids=self.staff.partner_id.ids)
        self.project.privacy_visibility = 'followers'
        self.assertNotIn(self.staff2.partner_id, self.members(internal))
        for vals in ({'pinned_at': now}, {'starred_partner_ids': [Command.set(self.staff.partner_id.ids)]}):
            with self.subTest(vals=list(vals)):
                self.refused(lambda: mention.with_user(self.staff2).write(vals))
                # a member, the author included, writes neither directly
                self.refused(lambda: mention.with_user(self.staff).write(vals))
        self.assertFalse(mention.sudo().pinned_at)
        self.assertFalse(mention.sudo().starred_partner_ids)
        # Odoo's own pin and star still work for a member
        internal.with_user(self.staff).set_message_pin(mention.id, True)
        mention.with_user(self.staff).toggle_message_starred()
        self.assertTrue(mention.sudo().pinned_at)
        self.assertEqual(mention.sudo().starred_partner_ids, self.staff.partner_id)
        # an archived task's chat: no pin at all
        self.task.active = False
        self.refused(lambda: secret.with_user(self.staff).write({'pinned_at': now}))
        with self.assertRaises(UserError), self.env.cr.savepoint():
            internal.with_user(self.staff).set_message_pin(secret.id, True)

    # --- found with Odoo's own performance tests: the stored checks need no query for other records ---

    def test_perf_stored_checks_only_where_a_link_is_possible(self):
        Message, Attachment = type(self.env['mail.message']), type(self.env['ir.attachment'])
        internal = self.open_tab(self.staff, 'internal')
        group = self.public_channel()
        checked = []
        original = Message._woow_project_ai_check_stored

        def spy(records, created=False, moved=False):
            checked.append(created)
            return original(records, created=created, moved=moved)

        with patch.object(Message, '_woow_project_ai_check_stored', spy):
            # a note on a task, a message in an ordinary channel: nothing to check, no query
            note = self.task.message_post(body='a plain note', message_type='comment', subtype_xmlid='mail.mt_note')
            group.message_post(body='hello channel', message_type='comment', subtype_xmlid='mail.mt_comment')
            self.assertEqual(checked, [])
            # a reply, a task chat's message, a message with files: checked as stored
            self.task.message_post(body='a reply', message_type='comment', subtype_xmlid='mail.mt_note',
                                   parent_id=note.id)
            self.post(internal, self.staff, 'in the task chat')
            upload = self.pending_attachment(self.staff, name='n.pdf')
            group.with_user(self.staff).sudo().message_post(
                body='a file', message_type='comment', subtype_xmlid='mail.mt_comment', attachment_ids=upload.ids)
            self.assertEqual(checked, [True, True, True])
        Links = self.env['mail.message']
        self.assertTrue(Links._woow_project_ai_links_nothing([{'model': 'res.partner', 'res_id': 1}]))
        self.assertFalse(Links._woow_project_ai_links_nothing([{'model': 'discuss.channel', 'res_id': internal.id}]))
        self.assertTrue(Links._woow_project_ai_links_nothing([{'model': 'discuss.channel', 'res_id': group.id}]))
        self.assertFalse(Links._woow_project_ai_links_nothing([{'model': 'discuss.channel', 'res_id': str(group.id)}]))
        self.assertFalse(Links.with_context(default_parent_id=1)._woow_project_ai_links_nothing([{'model': 'x'}]))
        self.assertFalse(Links._woow_project_ai_links_nothing([{'model': 'x', 'attachment_ids': [(4, 1)]}]))
        # files: another model's file needs no query; a file given for a channel is checked as stored
        places = []
        original_places = Attachment._woow_project_ai_places

        def spy_places(records):
            places.append(records.ids)
            return original_places(records)

        with patch.object(Attachment, '_woow_project_ai_places', spy_places):
            self.env['ir.attachment'].with_user(self.staff).create({
                'name': 'n.txt', 'raw': b'x', 'res_model': 'project.task', 'res_id': self.task.id})
            self.assertEqual(places, [])
            with self.assertRaises(AccessError), self.env.cr.savepoint():
                self.env['ir.attachment'].with_user(self.staff).create({
                    'name': 'n.txt', 'raw': b'x', 'res_model': 'discuss.channel', 'res_id': internal.id})
            self.assertEqual(len(places), 1)

    # --- 13: message names in one query ---

    def test_13_message_names_in_one_query(self):
        internal, secret = self.secret_message()
        notes = self.env['mail.message'].browse([
            self.task.message_post(body='note %s' % i, message_type='comment', subtype_xmlid='mail.mt_note').id
            for i in range(3)])
        messages = notes | secret
        self.env.flush_all()
        self.env.invalidate_all()
        self.env['discuss.channel'].with_user(self.employee)._woow_project_ai_rule()  # (once per transaction)
        with self.assertQueryCount(1):
            names = [message.display_name for message in messages.with_user(self.employee).sudo()]
        self.assertEqual(names[-1], 'Task AI chat')
        self.assertTrue(all('Install cabinets' in name for name in names[:3]))


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestR5ReviewHttp(R5Helpers, AiTaskFixtures, HttpCase):
    """The reports' payloads through Odoo's routes."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_delta2()

    def test_payloads(self):
        chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.staff, 'customer')
        everyone = (self.staff2 | self.employee | self.manager).partner_id
        for user in (self.staff2, self.employee, self.manager):
            user.notification_type = 'inbox'
        own = self.post(chat, self.customer, 'my question')
        members = self.members(chat)
        # 2: the portal customer's edit route with sequential partner ids
        self.authenticate('aitask_customer', 'aitask_customer')
        self.make_jsonrpc_request('/mail/message/update_content', {
            'message_id': own.id, 'body': '<p>my question, edited</p>', 'attachment_ids': [],
            'partner_ids': list(range(1, max(everyone.ids) + 1))})
        self.assertEqual(self.members(chat), members)
        bots = self.env['res.partner'].browse(self.env['discuss.channel']._woow_project_ai_bot_partner_ids())
        self.assertFalse(own.sudo().partner_ids - self.staff.partner_id - bots, 'members and bots only')
        self.assertEqual(sum(self.unread(partner, chat) for partner in everyone), 0)
        # 4 (failures): /mail/data of an author who lost the chat loads
        internal, secret = self.secret_message()
        self.env['mail.notification'].create({
            'mail_message_id': secret.id, 'res_partner_id': self.customer.partner_id.id, 'author_id':
            self.staff.partner_id.id, 'notification_type': 'email', 'notification_status': 'exception'})
        self.project.privacy_visibility = 'followers'
        self.env.flush_all()
        self.env.invalidate_all()
        self.authenticate('aitask_staff', 'aitask_staff')
        data = self.make_jsonrpc_request('/mail/data', {'failures': True, 'init_messaging': {}})
        self.assertNotIn(SECRET, json.dumps(data, ensure_ascii=False))
        # 1: the public rating route on a task chat posts nothing
        rating = self.env['rating.rating'].create({
            'res_model_id': self.channel_model(), 'res_id': chat.id, 'partner_id': self.manager.partner_id.id,
            'access_token': 'r5-http-tok'})
        before = self.chat_messages(chat)
        with mute_logger('odoo.http', 'odoo.addons.rating.controllers.main'):
            self.url_open('/rate/r5-http-tok/submit_feedback', data={
                'rate': 5, 'feedback': PAYLOAD, 'csrf_token': self.csrf_token()})
        self.assertEqual(self.chat_messages(chat), before)
        self.assertFalse(rating.sudo().consumed)

    def csrf_token(self):
        from odoo.http import Request  # noqa: PLC0415 - only for the token
        return Request.csrf_token(self)
