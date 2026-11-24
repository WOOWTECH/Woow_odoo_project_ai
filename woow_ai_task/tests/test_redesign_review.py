# -*- coding: utf-8 -*-
"""Regression tests of the review of the redesign (f5dfcc0..4a015d8,
review-pai1-redesign/REVIEW.md: the item numbers are in the test names),
with the reports' payloads: the task-chat cache that remembers only channels
that exist, the superuser composer, names and contents of files read through
other models, a request without a user, reactions, the mail queue list, the
Inbox counter, archived brief projects, x2many fetches, ratings, the readable
task chats computed once per transaction, and one fetch per rendered batch."""

import base64
import json
from unittest.mock import patch

from odoo import Command, api
from odoo.exceptions import AccessError
from odoo.tests.common import HttpCase, JsonRpcException, TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from odoo.addons.mail.tools.discuss import Store
from odoo.addons.woow_ai_task.models.discuss_channel import TASK_CHAT_CACHE

from .common import TINY_PDF, TINY_PNG, AiTaskFixtures
from .test_delta2_review import SECRET, Delta2Helpers


class RedesignHelpers(Delta2Helpers):

    def next_channel_id(self):
        self.env.flush_all()
        self.env.cr.execute("SELECT last_value, is_called FROM discuss_channel_id_seq")
        last, called = self.env.cr.fetchone()
        return last + 1 if called else last

    def mention(self, chat, author, partner, body):
        return chat.with_user(author).sudo().message_post(
            body=body, message_type='comment', subtype_xmlid='mail.mt_comment', partner_ids=partner.ids)

    def inbox_user(self, login, groups='base.group_user,project.group_project_user'):
        user = new_test_user(self.env, login, name=login.title(), email='%s@example.com' % login, groups=groups)
        user.notification_type = 'inbox'
        return user

    def unread(self, partner, chat):
        self.env.flush_all()
        self.env.cr.execute("""
            SELECT count(*) FROM mail_notification n JOIN mail_message m ON m.id = n.mail_message_id
             WHERE n.res_partner_id = %s AND n.is_read IS NOT TRUE
               AND m.model = 'discuss.channel' AND m.res_id = %s""", [partner.id, chat.id])
        return self.env.cr.fetchone()[0]


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestRedesignReview(RedesignHelpers, AiTaskFixtures, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_delta2()

    # --- 1: no lookup marks a channel that does not exist yet ---

    def test_01_future_chats_are_never_remembered_as_ordinary(self):
        Channel = self.env['discuss.channel']
        start = self.next_channel_id()
        future = list(range(start, start + 20))
        # a portal user's searches about channel ids that have no row yet (a search_count, a thread fetch)
        self.env['mail.message'].with_user(self.outsider).search_count(
            [('model', '=', 'discuss.channel'), ('res_id', 'in', future)])
        self.env['mail.message'].with_user(self.outsider).search(
            [('model', '=', 'discuss.channel'), ('res_id', '=', start)])
        known = getattr(self.env.registry, TASK_CHAT_CACHE, None) or {}
        self.assertFalse(set(future) & set(known), 'a channel id without a row is never remembered')
        # the chats created later get those ids
        internal, secret = self.secret_message()
        self.assertIn(internal.id, future)
        self.mention(internal, self.staff, self.staff2.partner_id, '<p>%s for Tina</p>' % SECRET)
        _same, plan = internal, self.file_of(internal, self.staff, 'plan.pdf', 'the internal plan')
        self.assertEqual(Channel._woow_project_ai_known_task_chats({internal.id}), {internal.id})
        known = getattr(self.env.registry, TASK_CHAT_CACHE)
        self.assertIs(known.get(internal.id), True)
        # copy as a portal user
        default = {'model': False, 'res_id': False, 'parent_id': False, 'attachment_ids': []}
        self.refused(lambda: self.env['mail.message'].with_user(self.outsider).browse(secret.id).copy(default))
        # the pinned search leaves the chat out, also among the ids looked up before
        for domain in ([('model', '=', 'discuss.channel'), ('res_id', 'in', future)],
                       [('model', '=', 'discuss.channel'), ('res_id', '=', internal.id)], [('id', '=', secret.id)]):
            with self.subTest(domain=domain):
                self.assertFalse(self.env['mail.message'].with_user(self.outsider).search(domain))
                self.assertFalse(self.env['mail.message'].with_user(self.employee).search(domain))
        # a reply elsewhere, created directly
        group = self.env['discuss.channel'].with_user(self.employee).create({'name': 'mine', 'channel_type': 'group'})
        self.refused(lambda: self.env['mail.message'].with_user(self.employee).create({
            'model': 'discuss.channel', 'res_id': group.id, 'body': 'x', 'message_type': 'comment',
            'parent_id': secret.id}))
        # the file through a letter's onchange: neither its content nor its name
        if 'snailmail.letter' in self.env:
            content = base64.b64encode(TINY_PDF).decode()
            for user in (self.employee, self.outsider):
                with self.subTest(user=user.login):
                    self.env.invalidate_all()
                    try:
                        result = self.env['snailmail.letter'].with_user(user).onchange(
                            {'attachment_id': plan.id}, ['attachment_id'],
                            {'attachment_id': {}, 'attachment_datas': {}, 'attachment_fname': {}})
                    except AccessError:
                        continue
                    dump = str(result).replace("b'", "'")
                    self.assertNotIn(content, dump)
                    self.assertNotIn('plan.pdf', dump)
        # a mentioned colleague who lost the access reads nothing any more
        tina = self.env['mail.message'].sudo().search([('model', '=', 'discuss.channel'), ('res_id', '=', internal.id),
                                                      ('partner_ids', 'in', self.staff2.partner_id.ids)])
        self.assertTrue(tina)
        self.env.invalidate_all()
        self.assertIn(SECRET, tina.with_user(self.staff2).read(['body'])[0]['body'])
        self.project.privacy_visibility = 'followers'
        self.assertFalse(self.task.with_user(self.staff2).has_access('read'))
        self.refused(lambda: tina.with_user(self.staff2).read(['body']))
        # a direct write of a message there
        self.refused(lambda: secret.with_user(self.staff).write({'body': 'changed'}))

    def test_01_known_chats_after_a_lookup_of_ids_without_rows(self):
        Channel = self.env['discuss.channel']
        start = self.next_channel_id()
        self.assertEqual(Channel._woow_project_ai_known_task_chats(set(range(start, start + 5))), set())
        group = self.public_channel()
        internal = self.open_tab(self.staff, 'internal')
        self.assertEqual(Channel._woow_project_ai_known_task_chats({group.id, internal.id}), {internal.id})

    # --- 2: a new task-chat message only through the chat's own post, superuser included ---

    def mass_mail(self, user, chat, author, body):
        composer = self.env['mail.compose.message'].with_user(user).create({
            'composition_mode': 'mass_mail', 'model': 'discuss.channel', 'res_ids': str([chat.id]),
            'author_id': author.id, 'subject': 'AI', 'body': body})
        composer.action_send_mail()

    def test_02_no_message_through_the_mass_mail_composer(self):
        internal = self.open_tab(self.staff, 'internal')
        customer_chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.staff, 'customer')
        cases = [
            ('an employee without Project rights, as the AI', self.employee, internal, self.internal_ai.partner_id),
            ('a member, as a colleague', self.staff, internal, self.staff2.partner_id),
            ('a member, as the customer', self.staff, customer_chat, self.customer.partner_id),
        ]
        for name, user, chat, author in cases:
            with self.subTest(name):
                body = '<p>FORGED APPROVAL %s</p>' % name
                self.refused(lambda: self.mass_mail(user, chat, author, body))
                self.assertFalse(self.find(chat, 'FORGED APPROVAL'))
        # not even as superuser outside the chat's own post
        self.refused(lambda: self.env['mail.message'].create({
            'model': 'discuss.channel', 'res_id': internal.id, 'body': 'direct', 'message_type': 'comment',
            'author_id': self.internal_ai.partner_id.id, 'subtype_id': self.comment()}))
        self.refused(lambda: self.env['mail.mail'].create({
            'model': 'discuss.channel', 'res_id': internal.id, 'body_html': '<p>queued</p>', 'body': 'queued',
            'author_id': self.internal_ai.partner_id.id}))
        # elsewhere, as before: the composer and superuser's own messages
        group = self.env['discuss.channel'].with_user(self.staff).create({'name': 'team', 'channel_type': 'group'})
        self.mass_mail(self.staff, group, self.staff.partner_id, '<p>a mass mail in a group</p>')
        self.assertTrue(self.find(group, 'a mass mail in a group'))
        self.env['mail.message'].create({
            'model': 'discuss.channel', 'res_id': group.id, 'body': 'superuser log', 'message_type': 'notification',
            'author_id': self.env.ref('base.partner_root').id})
        self.assertTrue(self.find(group, 'superuser log'))

    def test_02_odoo_posts_in_task_chats_still_work(self):
        internal, secret = self.secret_message()
        self.open_tab(self.staff2, 'internal')
        # the pin notice (as the member)
        internal.with_user(self.staff).set_message_pin(secret.id, pinned=True)
        self.assertTrue(self.find(internal, 'pinned a message'))
        # the leave notice (Odoo posts it as superuser in the member's name)
        internal.with_user(self.staff2).action_unfollow()
        notice = self.find(internal, 'left the channel')
        self.assertEqual(notice.author_id, self.staff2.partner_id)
        self.assertNotIn(self.staff2.partner_id, self.members(internal))
        # the AI's answer and its notices: through the chat's message_post
        answer = self.internal_ai._post_text(internal, 'an AI answer')
        self.assertEqual(answer.author_id, self.internal_ai.partner_id)
        # the customer chat's welcome text (posted when the chat is created)
        self.project.woow_project_ai_customer_welcome = 'WELCOME TEXT'
        customer_chat = self.open_tab(self.customer, 'customer')
        self.assertTrue(self.find(customer_chat, 'WELCOME TEXT'))
        # a call notice: a task chat has no calls (as before: only Odoo itself, as superuser, posts notices there)
        # (as /mail/rtc/channel/join_call: the member's own, then superuser)
        member = internal.sudo().channel_member_ids.filtered(lambda m: m.partner_id == self.staff.partner_id)
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            member.with_user(self.staff).sudo()._rtc_join_call()
        # ordinary channels: calls, OdooBot and superuser posts as before
        group = self.env['discuss.channel'].with_user(self.staff).create({'name': 'team', 'channel_type': 'group'})
        group_member = group.sudo().channel_member_ids.filtered(lambda m: m.partner_id == self.staff.partner_id)
        group_member.with_user(self.staff).sudo()._rtc_join_call()
        self.assertTrue(self.find(group, 'started a live conference'))
        if hasattr(type(self.env['res.users']), '_init_odoobot'):
            chat = self.staff.with_user(self.staff)._init_odoobot()
            self.assertTrue(self.env['mail.message'].sudo().search(
                [('model', '=', 'discuss.channel'), ('res_id', '=', chat.id),
                 ('author_id', '=', self.env.ref('base.partner_root').id)]))

    # --- 3: message names extend Odoo's (and any other module's) ---

    def test_03_message_names_call_super(self):
        note = self.task.message_post(body='a note', message_type='comment', subtype_xmlid='mail.mt_note')
        internal, secret = self.secret_message()
        Message = type(self.env['mail.message'])
        mro = Message.__mro__
        ours = next(cls for cls in mro if cls.__module__ == 'odoo.addons.woow_ai_task.models.mail_message')
        parent = mro[mro.index(ours) + 1]
        original = parent._compute_display_name
        seen = []

        def spy(records):
            seen.append(set(records.ids))
            return original(records)

        self.env.invalidate_all()
        with patch.object(parent, '_compute_display_name', spy):
            messages = (note | secret).with_user(self.employee).sudo()
            names = {message.id: message.display_name for message in messages}
        self.assertEqual(seen, [{note.id}], 'the next override (Odoo, another module) names the other messages')
        self.assertEqual(names[secret.id], 'Task AI chat')
        self.assertEqual(names[note.id], note.record_name)

    # --- 4: no user (auth='none') ---

    def test_04_datas_in_a_request_without_a_user(self):
        partner = self.env['res.partner'].create({'name': 'Logo', 'image_1920': base64.b64encode(TINY_PNG)})
        _chat, contract = self.customer_file()
        self.env.flush_all()
        self.env.invalidate_all()
        env = api.Environment(self.env.cr, None, {})
        self.assertTrue(env['res.partner'].sudo().browse(partner.id).image_128)
        self.assertFalse(env['ir.attachment'].sudo().browse(contract.id).datas, 'a task-chat file: empty')
        self.assertTrue(env['ir.attachment'].sudo().browse(contract.id).raw, 'Odoo itself (raw) unchanged')
        self.assertEqual(env['ir.attachment'].sudo().browse(contract.id).display_name, 'Task AI chat file')

    # --- 5 / 10: names of task-chat files ---

    def test_05_10_file_names_through_other_models(self):
        if 'snailmail.letter' not in self.env:
            self.skipTest('snailmail is not installed')
        _internal, plan = self.internal_file()
        _chat, contract = self.customer_file()
        Letter = self.env['snailmail.letter']
        payloads = {
            'attachment_fname': lambda user, file: Letter.with_user(user).onchange(
                {'attachment_id': file.id}, ['attachment_id'], {'attachment_id': {}, 'attachment_fname': {}}),
            'first call display_name': lambda user, file: Letter.with_user(user).onchange(
                {'attachment_id': file.id}, [], {'attachment_id': {'fields': {'display_name': {}}}}),
        }
        for name, call in payloads.items():
            # reader controls: the file's own name
            self.env.invalidate_all()
            self.assertIn('plan.pdf', json.dumps(call(self.staff, plan), default=str), name)
            for user in (self.employee, self.outsider):
                with self.subTest(name, user=user.login):
                    self.leaks_name(lambda: call(user, plan), 'plan.pdf')
            # the customer's contract: every employee may read a customer chat of a portal project
            self.leaks_name(lambda: call(self.outsider, contract), 'contract.pdf')
        # a stored letter of a reader keeps the name and the content (Odoo's own flow)
        letter = Letter.with_user(self.staff).new({'attachment_id': plan.id})
        self.assertEqual(letter.attachment_fname, 'plan.pdf')
        self.assertTrue(letter.attachment_datas)

    def leaks_name(self, call, name):
        self.env.flush_all()
        self.env.invalidate_all()
        try:
            with self.env.cr.savepoint():
                result = call()
        except AccessError:
            return
        self.assertNotIn(name, json.dumps(result, ensure_ascii=False, default=str))

    # --- 6: reactions ---

    def test_06_reactions_only_from_members_who_may_read(self):
        internal, secret = self.secret_message()
        parent = self.mention(internal, self.staff, self.staff2.partner_id, '<p>Tina, see this</p>')
        reply = internal.with_user(self.staff).sudo().message_post(
            body='a reply', message_type='comment', subtype_xmlid='mail.mt_comment', parent_id=parent.id)
        self.project.message_subscribe(partner_ids=self.staff.partner_id.ids)  # Sam keeps the access
        self.project.privacy_visibility = 'followers'
        self.assertFalse(self.task.with_user(self.staff2).has_access('read'))
        Message = self.env['mail.message']

        def react(user, message):
            # as /mail/message/reaction: Odoo's 'create' check (a recipient of the parent passes), then superuser
            found = Message.with_user(user)._get_with_access(message.id, 'create')
            self.assertTrue(found)
            found.sudo()._message_reaction('👎', 'add', user.partner_id, self.env['mail.guest'])

        self.refused(lambda: react(self.staff2, reply))
        self.assertFalse(reply.sudo().reaction_ids)
        react(self.staff, secret)
        self.assertEqual(secret.sudo().reaction_ids.partner_id, self.staff.partner_id)

    # --- 7 / 11: the readable task chats once per transaction ---

    def test_07_11_readable_chats_once_per_transaction(self):
        internal = self.open_tab(self.staff, 'internal')
        group = self.public_channel()
        Channel = type(self.env['discuss.channel'])
        original = Channel._woow_project_ai_readable_sql_of
        compiled = []

        def spy(records, user):
            compiled.append(user.id)
            return original(records, user)

        self.env.invalidate_all()
        with patch.object(Channel, '_woow_project_ai_readable_sql_of', spy):
            Channels = self.env['discuss.channel'].with_user(self.staff)
            Messages = self.env['mail.message'].with_user(self.staff)
            for _i in range(5):
                self.assertEqual(Channels.search([('id', '=', internal.id)]), internal)
                self.assertEqual(Channels.search([('id', '=', group.id)]), group)
                Messages.search([('needaction', '=', True)])
            self.assertEqual(compiled, [self.staff.id], 'the rule compiled once per transaction and user')
            # a new task chat: read by the compiled rule at once (it reads the data at each use)
            customer_chat = self.open_tab(self.staff, 'customer')
            self.assertEqual(Channels.search([('id', '=', customer_chat.id)]), customer_chat)
            # someone who gains access in the transaction (a group: the rule compiled again for them)
            Eve = self.env['discuss.channel'].with_user(self.employee)
            self.assertFalse(Eve._woow_project_ai_readable_chat_ids({internal.id}))
            self.employee.groups_id = [Command.link(self.env.ref('project.group_project_user').id)]
            self.assertEqual(Eve._woow_project_ai_readable_chat_ids({internal.id}), {internal.id})
            self.assertEqual(compiled.count(self.employee.id), 2)
            self.open_tab(self.employee, 'internal')
            self.assertEqual(Eve.search([('id', '=', internal.id)]), internal)
        # the rule is a sub-query PostgreSQL evaluates for task chats only: one query per channel search
        self.env.invalidate_all()
        Channels.search([('id', '=', group.id)])
        with self.assertQueryCount(1):
            Channels.search([('id', '=', group.id)])

    # --- 8: the mail queue list ---

    def test_08_mail_queue_list_for_administrators(self):
        admin = new_test_user(self.env, 'aitask_it_admin', name='IT Admin', email='it@example.com',
                              groups='base.group_user,base.group_system')
        self.assertFalse(admin.has_group('project.group_project_user'))
        internal, secret = self.secret_message()
        mail = self.env['mail.mail'].create({'mail_message_id': secret.id, 'body_html': '<p>queued</p>',
                                             'state': 'exception'})
        other = self.env['mail.mail'].create({'subject': 'An ordinary mail', 'body_html': '<p>hi</p>'})
        self.env.flush_all()
        self.env.invalidate_all()
        rows = self.env['mail.mail'].with_user(admin).search_read([], ['subject', 'author_id', 'date'])
        ids = {row['id'] for row in rows}
        self.assertIn(other.id, ids)
        self.assertNotIn(mail.id, ids)
        self.refused(lambda: mail.with_user(admin).read(['subject']))
        # a reader of the chat lists it
        manager = new_test_user(self.env, 'aitask_it_manager', name='IT Manager', email='itm@example.com',
                                groups='base.group_user,base.group_system,project.group_project_manager')
        self.assertIn(mail, self.env['mail.mail'].with_user(manager).search([]))

    # --- 9: the Inbox counter ---

    def test_09_inbox_counter_follows_the_access(self):
        tom = self.inbox_user('aitask_tom')
        uma = self.inbox_user('aitask_uma')
        self.staff2.notification_type = 'inbox'
        internal = self.open_tab(self.staff, 'internal')
        self.open_tab(tom, 'internal')
        # Tom (a member), Uma and Tina are mentioned: Uma and Tina, who had not opened the tab, join (their
        # notification is shown in their own environment, where Odoo's membership rule decides)
        self.mention(internal, self.staff, tom.partner_id | uma.partner_id | self.staff2.partner_id,
                     '<p>Tom, Uma and Tina, look</p>')
        people = tom.partner_id | uma.partner_id | self.staff2.partner_id
        self.assertEqual(people & self.members(internal), people)
        # Uma leaves the chat: her unread mention stays (as in Odoo)
        internal.with_user(uma).action_unfollow()
        self.assertNotIn(uma.partner_id, self.members(internal))
        self.assertEqual([self.unread(partner, internal) for partner in people], [1, 1, 1])
        before = {partner: partner._get_needaction_count() for partner in people}
        self.env['bus.bus'].sudo().search([]).unlink()
        # Tom and Uma lose the access (a trigger: the project's privacy); Sam and Tina follow the project
        self.project.message_subscribe(partner_ids=(self.staff | self.staff2).partner_id.ids)
        self.project.privacy_visibility = 'followers'
        self.env.flush_all()
        self.env.cr.precommit.run()  # (the bus notifications are written at commit)
        self.assertEqual([self.unread(partner, internal) for partner in people], [0, 0, 1])
        notices = self.env['bus.bus'].sudo().search([('message', 'like', 'mail.message/mark_as_read')])
        for partner in tom.partner_id | uma.partner_id:
            self.assertEqual(partner._get_needaction_count(), before[partner] - 1)
            self.assertTrue(any(json.loads(notice.channel)[1:3] == ['res.partner', partner.id] for notice in notices))
        self.assertEqual(self.staff2.partner_id._get_needaction_count(), before[self.staff2.partner_id])
        self.assertNotIn(tom.partner_id, self.members(internal))

    def test_09_mentioned_colleagues_join_and_read_their_inbox(self):
        """A mention of a colleague who may be in the chat but has not opened its tab: the post works, they
        join (muted in a customer chat), and their Inbox shows the mention (both read the chat in their own
        environment)."""
        self.staff2.notification_type = 'inbox'
        for tab in ('internal', 'customer'):
            with self.subTest(tab=tab):
                chat = self.open_tab(self.staff, tab)
                self.assertNotIn(self.staff2.partner_id, self.members(chat))
                self.mention(chat, self.staff, self.staff2.partner_id, '<p>Tina, the %s chat</p>' % tab)
                self.assertIn(self.staff2.partner_id, self.members(chat))
                member = chat.sudo().channel_member_ids.filtered(lambda m: m.partner_id == self.staff2.partner_id)
                self.assertEqual(member.custom_notifications == 'no_notif', tab == 'customer')
                self.env.invalidate_all()
                messages = self.env['mail.message'].with_user(self.staff2)._message_fetch(
                    [('needaction', '=', True)])['messages']
                result = Store(messages, for_current_user=True).get_result()
                self.assertIn('Tina, the %s chat' % tab, json.dumps(result, ensure_ascii=False))
        # someone who may not be in the chat: neither notified nor joined
        internal = self.open_tab(self.staff, 'internal')
        message = self.mention(internal, self.staff, self.employee.partner_id, '<p>Eve?</p>')
        self.assertNotIn(self.employee.partner_id, message.sudo().partner_ids)
        self.assertNotIn(self.employee.partner_id, self.members(internal))

    def test_09_hourly_sweep_marks_them_read(self):
        uma = self.inbox_user('aitask_uma')
        internal = self.open_tab(self.staff, 'internal')
        self.mention(internal, self.staff, uma.partner_id, '<p>Uma, look</p>')
        self.assertEqual(self.unread(uma.partner_id, internal), 1)
        # the access goes without any trigger (a change in SQL): only the sweep sees it
        self.env.flush_all()
        self.env.cr.execute("UPDATE project_project SET privacy_visibility = 'followers' WHERE id = %s",
                            [self.project.id])
        self.env.invalidate_all()
        self.env['discuss.channel']._woow_project_ai_forget_readable()
        self.env['discuss.channel']._woow_project_ai_cron_sync()
        self.assertEqual(self.unread(uma.partner_id, internal), 0)

    # --- 12: archived brief projects ---

    def test_12_archived_brief_projects_count(self):
        """A member (Odoo's own channel rule lets members read) whose access to an archived brief project went
        away without a trigger: the module's rule refuses the chat in every channel search, in the default
        context too (the rule's 'not any' leaf on the brief projects would skip archived ones)."""
        secret_project = self.env['project.project'].create({
            'name': 'Secret P1', 'privacy_visibility': 'followers',
            'woow_project_ai_internal_assistant_id': self.internal_ai.id,
            'woow_project_ai_internal_brief': 'P1 MARGIN TEXT'})
        secret_project.message_subscribe(partner_ids=(self.staff | self.staff2).partner_id.ids)
        task = self.env['project.task'].create({'name': 'Moved task', 'project_id': secret_project.id,
                                                'user_ids': [Command.set((self.staff | self.staff2).ids)]})
        chat = self.open_tab(self.staff, 'internal', task=task)
        self.open_tab(self.staff2, 'internal', task=task)
        chat.sudo().woow_project_ai_brief_project_ids = [Command.link(secret_project.id)]
        task.project_id = self.project
        secret_project.active = False
        self.assertIn(self.staff2.partner_id, self.members(chat))
        # Tina stops following P1 without any trigger (only the sync would see it)
        self.env.flush_all()
        self.env.cr.execute("DELETE FROM mail_followers WHERE res_model = 'project.project' AND res_id = %s "
                            "AND partner_id = %s", (secret_project.id, self.staff2.partner_id.id))
        self.env.invalidate_all()
        self.assertTrue(task.with_user(self.staff2).has_access('read'))
        self.assertFalse(secret_project.with_user(self.staff2).with_context(active_test=False).has_access('read'))
        self.assertIn(self.staff2.partner_id, self.members(chat))
        for active_test in (True, False):
            with self.subTest(active_test=active_test):
                self.env.invalidate_all()
                Channels = self.env['discuss.channel'].with_user(self.staff2).with_context(active_test=active_test)
                self.assertFalse(Channels.search([('id', '=', chat.id)]))
                self.assertFalse(chat.with_user(self.staff2).has_access('read'))
        # Sam (a follower of P1) still reads it
        self.assertEqual(self.env['discuss.channel'].with_user(self.staff).search([('id', '=', chat.id)]), chat)

    # --- 13: x2many lines of task-chat messages ---

    def test_13_x2many_fetch_is_checked(self):
        internal, secret = self.secret_message()
        secret.sudo().starred_partner_ids = [Command.link(self.staff.partner_id.id)]
        self.env.flush_all()
        self.env.invalidate_all()
        for names in (['starred_partner_ids'], ['child_ids'], ['partner_ids']):
            with self.subTest(names=names):
                self.refused(lambda: secret.with_user(self.employee).fetch(names))
        notification = self.env['mail.notification'].create({
            'mail_message_id': secret.id, 'res_partner_id': self.employee.partner_id.id, 'notification_type': 'inbox'})
        exporter = new_test_user(self.env, 'aitask_exporter', name='Exporter', email='exp@example.com',
                                 groups='base.group_user,base.group_allow_export')
        self.refused(lambda: notification.with_user(exporter).export_data(
            ['mail_message_id/starred_partner_ids/name']))
        # a reader: as before
        self.assertEqual(secret.with_user(self.staff).starred_partner_ids, self.staff.partner_id)

    # --- 14: ratings ---

    def test_14_no_rating_on_task_chat_messages(self):
        internal, secret = self.secret_message()
        Rating = self.env['rating.rating'].with_user(self.employee)
        model = self.env['ir.model']._get_id('res.partner')
        values = {'res_model_id': model, 'res_id': self.employee.partner_id.id, 'rating': 1, 'consumed': True}
        self.refused(lambda: Rating.create(dict(values, message_id=secret.id)))
        self.refused(lambda: Rating.create(dict(values, message_id=str(secret.id))))
        note = self.task.message_post(body='a note', message_type='comment', subtype_xmlid='mail.mt_note')
        rating = Rating.create(dict(values, message_id=note.id))
        self.refused(lambda: rating.write({'message_id': secret.id}))
        self.assertFalse(secret.sudo().rating_ids)

    # --- 15: one fetch per rendered batch ---

    def test_15_one_fetch_per_rendered_batch(self):
        internal = self.open_tab(self.staff, 'internal')
        for i in range(5):
            self.post(internal, self.staff, 'message %s' % i)
        messages = self.env['mail.message'].with_user(self.staff).search(
            [('model', '=', 'discuss.channel'), ('res_id', '=', internal.id)])
        self.env.invalidate_all()
        Message = type(self.env['mail.message'])
        original = Message._fetch_query
        columns = []

        def spy(records, query, fields):
            names = {field.name for field in fields if field.column_type}
            if names:
                columns.append(names)
            return original(records, query, fields)

        with patch.object(Message, '_fetch_query', spy):
            Store(messages).get_result()
        # the columns of the batch in one query, the body with them (x2many lines are read apart, as in Odoo)
        self.assertEqual(len(columns), 1, columns)
        self.assertIn('body', columns[0])

    # --- found with woow_document_management in the coexistence database ---

    def test_followup_a_rule_check_does_not_open_the_cache(self):
        """A record rule on messages (woow_document_management adds one): Odoo checks it with filtered_domain
        as superuser, which puts the message's columns in the cache, where a read finds them without fetch.
        The author who lost the chat reads nothing of their message all the same."""
        self.env['ir.rule'].create({'name': 'A rule on messages', 'model_id': self.env.ref('mail.model_mail_message').id,
                                    'domain_force': "[(1, '=', 1)]"})
        chat = self.open_tab(self.customer, 'customer')
        own = self.post(chat, self.customer, 'my own question')
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertEqual(own.with_user(self.customer).read(['author_id'])[0]['author_id'][0],
                         self.customer.partner_id.id)
        self.lose_access_silently(self.customer)
        for names in (['author_id'], ['record_name'], ['date'], ['body']):
            with self.subTest(names=names):
                self.refused(lambda: own.with_user(self.customer).read(names))
        self.assertFalse(own.with_user(self.customer).has_access('read'))


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestRedesignReviewHttp(RedesignHelpers, AiTaskFixtures, HttpCase):
    """The reports' JSON-RPC payloads, through Odoo's routes."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_delta2()

    def test_payloads(self):
        self.staff2.notification_type = 'inbox'
        internal, secret = self.secret_message()
        parent = self.mention(internal, self.staff, self.staff2.partner_id, '<p>Tina, see this</p>')
        self.assertEqual(self.unread(self.staff2.partner_id, internal), 1)
        reply = internal.with_user(self.staff).sudo().message_post(
            body='a reply', message_type='comment', subtype_xmlid='mail.mt_comment', parent_id=parent.id)
        self.project.privacy_visibility = 'followers'
        self.env.flush_all()
        self.env.invalidate_all()
        # 6: the reaction route
        self.authenticate('aitask_staff2', 'aitask_staff2')
        with mute_logger('odoo.http'), self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request('/mail/message/reaction',
                                      {'message_id': reply.id, 'content': '👎', 'action': 'add'})
        self.assertFalse(reply.sudo().reaction_ids)
        # 9: the Inbox lists nothing of the chat, and the counter agrees
        data = self.make_jsonrpc_request('/mail/inbox/messages', {'limit': 30})
        self.assertNotIn('Tina, see this', json.dumps(data, ensure_ascii=False))
        self.assertEqual(self.unread(self.staff2.partner_id, internal), 0)
        # 1: a thread fetch of ids without rows yet remembers nothing
        start = self.next_channel_id()
        self.authenticate('aitask_outsider', 'aitask_outsider')
        with mute_logger('odoo.http'):
            try:
                self.make_jsonrpc_request('/mail/thread/messages',
                                          {'thread_model': 'discuss.channel', 'thread_id': start})
            except Exception:  # noqa: BLE001 - Odoo may refuse a thread that does not exist
                pass
        known = getattr(self.env.registry, TASK_CHAT_CACHE, None) or {}
        self.assertNotIn(start, known)

