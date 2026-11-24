# -*- coding: utf-8 -*-
"""Regression tests of the second fix-delta review of PR #1
(review-pai1-delta2/REVIEW.md: the item numbers are in the test names), with
the reports' payloads: the read choke point (mail.message.fetch, the datas of
a task-chat file), the links checked on the stored rows (any id type), and
the brief projects of an internal chat. Payloads that need im_livechat's
parent_body run in the coexistence database (skipped elsewhere)."""

import base64
import json
import xmlrpc.client
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError, MissingError
from odoo.tests.common import HttpCase, JsonRpcException, TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from .common import BACKEND_POST, TINY_PDF, TINY_PNG, AiTaskFixtures, openai_ok
from .test_team_review import ReviewHelpers

SECRET = 'INTERNAL SECRET: our margin'


def installed(env, *modules):
    return all(module in env.registry._init_modules for module in modules)


class Delta2Helpers(ReviewHelpers):

    @classmethod
    def setup_delta2(cls):
        cls.setup_ai_task()
        # Access Rights administrator without Project rights: cannot read the task's internal chat
        cls.rights_admin = new_test_user(cls.env, 'aitask_rights_admin', name='Rights Ray', email='ray@example.com',
                                         groups='base.group_user,base.group_erp_manager')

    def secret_message(self):
        internal = self.open_tab(self.staff, 'internal')
        return internal, self.post(internal, self.staff, SECRET)

    def customer_file(self):
        chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.staff, 'customer')
        return chat, self.file_of(chat, self.customer, 'contract.pdf', 'my signed contract')

    def internal_file(self):
        """A file of the internal chat: employees without Project rights cannot read it (any employee may
        read a customer chat: a portal project's tasks are visible to every employee)."""
        internal = self.open_tab(self.staff, 'internal')
        return internal, self.file_of(internal, self.staff, 'plan.pdf', 'the internal plan')

    def has_parent_body(self):
        return 'parent_body' in self.env['mail.message']._fields

    def need_parent_body(self):
        if not self.has_parent_body():
            self.skipTest('im_livechat is not installed (parent_body)')

    def public_channel(self, name='general'):
        return self.env['discuss.channel'].create({
            'name': name, 'channel_type': 'channel', 'group_public_id': self.env.ref('base.group_user').id})

    def leaks_nothing(self, call):
        """An onchange payload: the read of the task-chat message is refused
        (mail.message.fetch); Odoo's compute then gives the value False, or
        the AccessError comes through. Either way the body is not in it."""
        self.env.flush_all()
        self.env.invalidate_all()
        try:
            with self.env.cr.savepoint():
                result = call()
        except AccessError:
            return
        self.assertNotIn(SECRET, json.dumps(result, ensure_ascii=False, default=str))

    def refused(self, call):
        # a request starts with an empty cache: what this test created is not in it there
        self.env.flush_all()
        self.env.invalidate_all()
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            call()

    @staticmethod
    def deep_chain(message_id, levels=8):
        """mail.message onchange values and spec: `levels` nested create commands
        (partner_ids / starred_message_ids), a message {'parent_id': M} inside."""
        values, spec = {'parent_id': message_id}, {'parent_body': {}}
        for name in ['starred_message_ids', 'partner_ids'] * (levels // 2):
            values = {name: [[0, 0, values]]}
            spec = {name: {'fields': spec}}
        return values, spec


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestDelta2Review(Delta2Helpers, AiTaskFixtures, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_delta2()

    # --- 1 / 3 / 6: onchange shapes (the read is refused wherever the id came from) ---

    def test_01_03_06_onchange_shapes(self):
        self.need_parent_body()
        _internal, secret = self.secret_message()
        M = secret.id
        Message, Channel, Partner = self.env['mail.message'], self.env['discuss.channel'], self.env['res.partner']
        chain_values, chain_spec = self.deep_chain(M)
        payloads = {
            # 1: {'id': [M]} (browse gives the origin M)
            'id list': lambda user: Message.with_user(user).onchange(
                {'parent_id': {'id': [M]}}, ['parent_id'], {'parent_body': {}}),
            # 1: a dict item in an x2many list, nested under a create command
            'sub-channel dict line': lambda user: Channel.with_user(user).onchange(
                {'sub_channel_ids': [[0, 'v1', {'message_ids': [{'parent_id': {'id': M}}]}]]}, [],
                {'sub_channel_ids': {'fields': {'message_ids': {'fields': {'parent_body': {}}}}}}),
            # 3: res.partner, a first call (field_names []) with a dict line under a create command
            'partner dict line': lambda user: Partner.with_user(user).onchange(
                {'child_ids': [[0, 0, {'name': 'x', 'starred_message_ids': [{'parent_id': M}]}]]}, [],
                {'child_ids': {'fields': {'starred_message_ids': {'fields': {'parent_body': {}}}}}}),
            # 6: eight nested create commands
            'depth 8': lambda user: Message.with_user(user).onchange(chain_values, [], chain_spec),
        }
        for name, call in payloads.items():
            # the payload does read the body for someone who may read the chat (nothing changes for them)
            self.env.invalidate_all()
            self.assertIn(SECRET, json.dumps(call(self.staff), ensure_ascii=False, default=str), name)
            for user in (self.outsider, self.employee):
                with self.subTest(name, user=user.login):
                    self.leaks_nothing(lambda: call(user))
        # a read of the message's name with sudo first (Odoo shows a linked record's name so) puts nothing of
        # its content in the cache: the computed body is still refused
        def primed():
            self.assertEqual(secret.with_user(self.outsider).sudo().display_name, 'Task AI chat')
            return payloads['id list'](self.outsider)
        self.leaks_nothing(primed)

    def test_01_03_files_through_a_letter_onchange(self):
        # 1 (d) / 3 (1b): snailmail.letter's attachment_datas is related as sudo: empty for a non-reader
        if 'snailmail.letter' not in self.env:
            self.skipTest('snailmail is not installed')
        _internal, plan = self.internal_file()
        content = base64.b64encode(TINY_PDF)
        Letter = self.env['snailmail.letter']
        spec = {'attachment_datas': {}}

        def letter(user):
            return Letter.with_user(user).onchange({'attachment_id': {'id': [plan.id]}}, ['attachment_id'], spec)

        def partner_line(user):
            return self.env['res.partner'].with_user(user).onchange(
                {'child_ids': [[0, 0, {'name': 'x', 'starred_message_ids': [
                    {'letter_ids': [{'attachment_id': plan.id}]}]}]]}, [],
                {'child_ids': {'fields': {'starred_message_ids': {'fields': {'letter_ids': {'fields': {
                    'attachment_datas': {}}}}}}}})

        # a colleague who may read the chat gets the content: the payloads do read it
        self.assertIn(content.decode(), str(letter(self.staff2)).replace("b'", "'"))
        for user in (self.employee, self.rights_admin):
            with self.subTest(user=user.login):
                self.assertNotIn(content.decode(), str(letter(user)).replace("b'", "'"))
                self.assertNotIn(content.decode(), str(partner_line(user)).replace("b'", "'"))

    # --- 2: a string id that Odoo stores (sub-channels) ---

    def test_02_no_thread_from_a_chat_message(self):
        _internal, secret = self.secret_message()
        general = self.public_channel()
        chat = self.open_tab(self.customer, 'customer')
        Channel = self.env['discuss.channel'].with_user(self.employee)
        for vals in ({'name': 'x', 'channel_type': 'channel', 'parent_channel_id': general.id,
                      'from_message_id': str(secret.id)},
                     {'name': 'y', 'channel_type': 'channel', 'from_message_id': str(secret.id)},
                     {'name': 'z', 'channel_type': 'channel', 'from_message_id': secret.id + 0.0},
                     {'name': 'w', 'channel_type': 'group', 'parent_channel_id': str(chat.id)}):
            with self.subTest(vals=vals):
                self.refused(lambda: Channel.create(vals))
        self.assertFalse(self.env['discuss.channel'].search([('from_message_id', '=', secret.id)]))
        # superuser neither: a task chat has no threads (with an int id Odoo's own constraint refuses it first)
        self.refused(lambda: self.env['discuss.channel'].create(
            {'name': 'v', 'channel_type': 'channel', 'from_message_id': str(secret.id)}))
        # a thread of an ordinary channel: as before
        root = general.message_post(body='hello', message_type='comment', subtype_xmlid='mail.mt_comment')
        thread = general._create_sub_channel(from_message_id=root.id)
        self.assertEqual(thread.from_message_id, root)

    # --- 4 / 15 / 16 / 17: float and string ids ---

    def test_04_letters_take_no_chat_file(self):
        if 'snailmail.letter' not in self.env:
            self.skipTest('snailmail is not installed')
        self.assertTrue(installed(self.env, 'woow_ai_task_snailmail'), 'the bridge must be installed')
        _chat, contract = self.customer_file()
        group = self.env['discuss.channel'].with_user(self.employee).create({'name': 'mine', 'channel_type': 'group'})
        Letter = self.env['snailmail.letter']
        for value in (contract.id + 0.0, str(contract.id), contract.id):
            with self.subTest(value=value):
                self.refused(lambda: Letter.with_user(self.employee).create({
                    'model': 'discuss.channel', 'res_id': group.id, 'partner_id': self.employee.partner_id.id,
                    'attachment_id': value}))
        self.refused(lambda: Letter.create({
            'model': 'discuss.channel', 'res_id': group.id, 'partner_id': self.employee.partner_id.id,
            'attachment_id': contract.id}))
        self.assertFalse(Letter.search([('attachment_id', '=', contract.id)]))

    def test_04_17_float_file_ids_on_messages(self):
        chat, contract = self.customer_file()
        group = self.env['discuss.channel'].with_user(self.staff).create({'name': 'mine', 'channel_type': 'group'})
        # 4: posted to another thread with a float id; 17: in the chat itself, someone else's file (a post with a
        # float id is refused by Odoo's message_post itself; an edit takes it)
        for thread in (group, chat):
            with self.subTest(thread=thread.name), self.assertRaises(Exception) as caught, self.env.cr.savepoint():
                thread.with_user(self.staff).sudo().message_post(
                    body='took it', message_type='comment', subtype_xmlid='mail.mt_comment',
                    attachment_ids=[contract.id + 0.0])
            self.assertIsInstance(caught.exception, (ValueError, AccessError))
        mine_elsewhere = group.with_user(self.staff).message_post(body='mine', message_type='comment',
                                                                 subtype_xmlid='mail.mt_comment')
        own = self.post(chat, self.staff, 'mine')
        for thread, message in ((group, mine_elsewhere), (chat, own)):
            with self.subTest('edit', thread=thread.name):
                self.refused(lambda: thread.with_user(self.staff)._message_update_content(
                    message.sudo(), 'took it', attachment_ids=[contract.id + 0.0]))
        self.refused(lambda: own.sudo().write({'attachment_ids': [Command.link(str(contract.id))]}))
        self.refused(lambda: own.sudo().write({'attachment_ids': [Command.set([contract.id + 0.0])]}))
        self.assertTrue(contract.exists())
        self.assertEqual(self.find(chat, 'my signed contract').attachment_ids, contract)
        self.assertFalse(own.attachment_ids)

    def test_15_float_file_ids_on_files(self):
        chat, contract = self.customer_file()
        Attachment = self.env['ir.attachment'].with_user(self.staff)
        float_id = contract.id + 0.0
        for name, call in {
            'unlink': lambda: Attachment.browse([float_id]).unlink(),
            'overwrite': lambda: Attachment.browse([float_id]).write(
                {'datas': base64.b64encode(b'%PDF-1.4 another file')}),
            'move': lambda: Attachment.browse([float_id]).write(
                {'res_model': 'res.partner', 'res_id': self.staff.partner_id.id}),
        }.items():
            with self.subTest(name):
                self.refused(call)
        self.assertEqual((contract.res_model, contract.res_id), ('discuss.channel', chat.id))
        self.assertEqual(contract.raw, TINY_PDF)

    def test_16_float_message_id_copy(self):
        _internal, secret = self.secret_message()
        default = {'model': False, 'res_id': False, 'parent_id': False, 'attachment_ids': []}
        for ids in ([secret.id + 0.0], [secret.id]):
            with self.subTest(ids=ids):
                self.refused(lambda: self.env['mail.message'].with_user(self.outsider).browse(ids).copy(default))
        self.assertEqual(self.env['mail.message'].search_count([('body', 'ilike', SECRET)]), 1)

    # --- 5: exports through other models ---

    def test_05_exports_through_other_models(self):
        # users created from Odoo's default user template carry the export right
        export = self.env.ref('base.group_allow_export')
        (self.staff | self.staff2 | self.employee).write({'groups_id': [Command.link(export.id)]})
        internal, secret = self.secret_message()
        # a mention: notification rows that every employee can read
        mention = internal.with_user(self.staff).sudo().message_post(
            body='%s, @Tina?' % SECRET, message_type='comment', subtype_xmlid='mail.mt_comment',
            partner_ids=self.staff2.partner_id.ids, parent_id=secret.id)
        notifications = self.env['mail.notification'].with_user(self.employee).search(
            [('res_partner_id', '=', self.staff2.partner_id.id)])
        self.assertTrue(notifications)
        self.refused(lambda: notifications.export_data(
            ['mail_message_id/record_name', 'mail_message_id/body', 'mail_message_id/parent_id/body']))
        # starred messages (the report's float star: Odoo's own write refuses it; a star set any other way)
        secret.with_user(self.staff).toggle_message_starred()
        with self.assertRaises(Exception), self.env.cr.savepoint():
            self.employee.partner_id.sudo().write({'starred_message_ids': [[4, mention.id + 0.0]]})
        self.staff2.partner_id.sudo().write({'starred_message_ids': [Command.link(mention.id)]})
        for partner in (self.staff.partner_id, self.staff2.partner_id):
            with self.subTest(partner=partner.name):
                self.refused(lambda: partner.with_user(self.employee).with_context(import_compat=False).export_data(
                    ['starred_message_ids/body']))
        # readers export as before
        exported = notifications.with_user(self.staff).export_data(['mail_message_id/body'])
        self.assertIn(SECRET, json.dumps(exported, ensure_ascii=False))
        # a member who lost the access, still listed: their member row's seen / fetched message
        self.open_tab(self.staff2, 'internal')
        member = internal.sudo().channel_member_ids.filtered(lambda m: m.partner_id == self.staff2.partner_id)
        member.with_user(self.staff2)._mark_as_read(mention.id)
        self.assertEqual(member.seen_message_id, mention)
        self.env.flush_all()
        self.env.cr.execute("UPDATE project_project SET privacy_visibility = 'followers' WHERE id = %s",
                            (self.project.id,))
        self.env.invalidate_all()
        self.assertFalse(self.task.with_user(self.staff2).has_access('read'))
        mine = self.env['discuss.channel.member'].with_user(self.staff2).search(
            [('partner_id', '=', self.staff2.partner_id.id), ('channel_id', '=', internal.id)])
        self.assertEqual(mine, member)
        self.refused(lambda: mine.export_data(['seen_message_id/body', 'fetched_message_id/body']))

    # --- 7: a task moved to another project ---

    def test_07_brief_projects_stay_with_the_chat(self):
        secret_project = self.env['project.project'].create({
            'name': 'Margins', 'privacy_visibility': 'followers', 'user_id': self.manager.id,
            'woow_project_ai_internal_assistant_id': self.internal_ai.id,
            'woow_project_ai_internal_brief': 'P1 MARGIN TEXT'})
        open_project = self.env['project.project'].create({
            'name': 'Everyone', 'privacy_visibility': 'employees',
            'woow_project_ai_internal_assistant_id': self.internal_ai.id})
        secret_project.message_subscribe(partner_ids=self.staff.partner_id.ids)
        task = self.env['project.task'].create({'name': 'Moving task', 'project_id': secret_project.id,
                                                'user_ids': [Command.set(self.staff.ids)]})
        chat = self.open_tab(self.staff, 'internal', task=task)
        self.post(chat, self.staff, 'what margin do we aim for?')
        with patch(BACKEND_POST, return_value=openai_ok('30 percent, says P1')) as post:
            self.run_due(chat)
        self.assertIn('P1 MARGIN TEXT', self.sent(post)['messages'][0]['content'])
        self.assertEqual(chat.sudo().woow_project_ai_brief_project_ids, secret_project)
        task.project_id = open_project
        self.assertTrue(task.with_user(self.staff2).has_access('read'))
        self.assertTrue(open_project.with_user(self.staff2).has_access('read'))
        # a reader of the new project only: not in the chat, which hides the earlier answers
        self.assertFalse(task._woow_project_ai_user_allowed(self.staff2, 'internal'))
        self.refused(lambda: self.open_tab(self.staff2, 'internal', task=task))
        self.assertFalse(self.env['discuss.channel'].with_user(self.staff2)._woow_project_ai_readable_chat_ids(
            {chat.id}), 'the module rule itself')
        # who reads both stays; a reader of the old project too joins
        self.assertIn(self.staff.partner_id, self.members(chat))
        secret_project.message_subscribe(partner_ids=self.staff2.partner_id.ids)
        self.assertEqual(self.open_tab(self.staff2, 'internal', task=task), chat)

    # --- 8 / 9: no hook on every model any more ---

    def test_08_a_generator_create(self):
        files = self.env['ir.attachment'].with_user(self.staff).create(
            {'name': 'g%s.txt' % i, 'raw': b'x'} for i in range(2))
        self.assertEqual(len(files), 2)

    def test_09_mark_as_read_elsewhere_checks_no_task_chat(self):
        group = self.env['discuss.channel'].with_user(self.staff).create({'name': 'g', 'channel_type': 'group'})
        group.with_user(self.staff).add_members(partner_ids=self.staff2.partner_id.ids)
        last = group.with_user(self.staff2).message_post(body='hi', message_type='comment',
                                                        subtype_xmlid='mail.mt_comment')
        member = group.sudo().channel_member_ids.filtered(lambda m: m.partner_id == self.staff.partner_id)
        Channel = type(self.env['discuss.channel'])
        with patch.object(Channel, '_woow_project_ai_readable_chat_ids',
                          side_effect=AssertionError('a task-chat check on another channel')):
            member.with_user(self.staff)._mark_as_read(last.id)
            group.with_user(self.staff).read(['name'])
            self.env['mail.message'].with_user(self.staff).search_read(
                [('model', '=', 'discuss.channel'), ('res_id', '=', group.id)], ['body'])
        self.assertEqual(member.seen_message_id, last)

    # --- 10: template rendering ---

    def test_10_templates_render_no_chat_message(self):
        _internal, secret = self.secret_message()
        editor = new_test_user(self.env, 'aitask_template_editor', name='Editor Ed', email='ed@example.com',
                               groups='base.group_user,mail.group_mail_template_editor')
        template = self.env['mail.template'].with_user(editor).create({
            'name': 'peek', 'model_id': self.env.ref('mail.model_mail_message').id, 'subject': '{{ object.body }}'})
        preview = self.env['mail.template.preview'].with_user(editor).create({
            'mail_template_id': template.id, 'resource_ref': 'mail.message,%d' % secret.id})
        self.assertNotIn(SECRET, preview.subject or '')
        self.assertTrue(preview.error_msg)

    # --- 11: names shown with sudo ---

    def test_11_chat_message_names_for_non_readers(self):
        internal, _secret = self.secret_message()
        internal.with_user(self.staff).sudo().message_post(
            body='@Tina, look', message_type='comment', subtype_xmlid='mail.mt_comment',
            partner_ids=self.staff2.partner_id.ids)
        domain = [('res_partner_id', '=', self.staff2.partner_id.id)]
        row = self.env['mail.notification'].with_user(self.employee).search_read(domain, ['mail_message_id'])[0]
        self.assertNotIn('Install cabinets', row['mail_message_id'][1])
        self.assertEqual(row['mail_message_id'][1], 'Task AI chat')
        row = self.env['mail.notification'].with_user(self.staff).search_read(domain, ['mail_message_id'])[0]
        self.assertIn('Install cabinets', row['mail_message_id'][1])

    # --- 12: link previews ---

    def test_12_link_previews_only_by_their_author(self):
        chat = self.open_tab(self.customer, 'customer')
        message = self.post(chat, self.customer, 'see https://example.com/plan')
        Preview = self.env['mail.link.preview']
        preview = Preview.sudo().create({'message_id': message.id, 'source_url': 'https://example.com/plan',
                                         'og_title': 'The plan'})
        admin = self.rights_admin
        for name, call in {
            'hide (the route runs as superuser)': lambda: preview.with_user(admin).sudo()._hide_and_notify(),
            'write': lambda: preview.with_user(admin).write({'og_title': 'Forged'}),
            'unlink': lambda: preview.with_user(admin).unlink(),
            'create': lambda: Preview.with_user(admin).create({'message_id': message.id,
                                                               'source_url': 'https://evil.example.com'}),
        }.items():
            with self.subTest(name):
                self.refused(call)
        self.assertEqual((preview.og_title, preview.is_hidden), ('The plan', False))
        preview.with_user(self.customer).sudo()._hide_and_notify()
        self.assertTrue(preview.is_hidden)

    # --- 13: no file straight into a task chat ---

    def test_13_no_file_created_in_a_chat(self):
        chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.staff, 'customer')
        Attachment = self.env['ir.attachment'].with_user(self.staff)
        for vals in ({'name': 'avatar.png', 'res_model': 'discuss.channel', 'res_id': chat.id,
                      'res_field': 'image_128', 'datas': base64.b64encode(TINY_PNG)},
                     {'name': 'file.pdf', 'res_model': 'discuss.channel', 'res_id': chat.id, 'raw': TINY_PDF}):
            with self.subTest(name=vals['name']):
                self.refused(lambda: Attachment.create(vals))
        plain = Attachment.create({'name': 'mine.pdf', 'raw': TINY_PDF})
        self.refused(lambda: plain.write({'res_model': 'discuss.channel', 'res_id': chat.id}))
        # an upload (Odoo's route creates it as superuser) and its post: as before
        upload = Attachment.sudo().create({'name': 'up.pdf', 'raw': TINY_PDF, 'res_model': 'discuss.channel',
                                           'res_id': chat.id})
        posted = self.post(chat, self.staff, 'my upload', attachments=upload)
        self.assertEqual(posted.attachment_ids, upload)

    # --- 14 / 18: defaults that reach nested lines, personal defaults ---

    def test_14_18_defaults(self):
        _internal, secret = self.secret_message()
        Message, Channel = self.env['mail.message'], self.env['discuss.channel']
        if self.has_parent_body():
            line_spec = {'message_ids': {'fields': {'parent_body': {}}}}
            payloads = {
                'context default on a nested line': lambda user: Channel.with_user(user).with_context(
                    default_parent_id=secret.id).onchange({'message_ids': [[0, 'm', {}]]}, [], line_spec),
                'fields_spec context': lambda user: Channel.with_user(user).onchange(
                    {'message_ids': [[0, 'm', {}]]}, [],
                    {'message_ids': {'fields': {'parent_body': {}}, 'context': {'default_parent_id': secret.id}}}),
            }
            for name, call in payloads.items():
                self.env.invalidate_all()
                self.assertIn(SECRET, json.dumps(call(self.staff), ensure_ascii=False, default=str), name)
                for user in (self.outsider, self.employee):
                    with self.subTest(name, user=user.login):
                        self.leaks_nothing(lambda: call(user))
            for user in (self.staff, self.employee):
                self.env['ir.default'].with_user(user).set('mail.message', 'parent_id', secret.id, user_id=True)
            self.env.invalidate_all()
            self.assertIn(SECRET, json.dumps(Message.with_user(self.staff).onchange(
                {}, [], {'parent_id': {}, 'parent_body': {}}), ensure_ascii=False, default=str))
            self.leaks_nothing(lambda: Message.with_user(self.employee).onchange(
                {}, [], {'parent_id': {}, 'parent_body': {}}))
        if 'snailmail.letter' in self.env:
            _internal, plan = self.internal_file()
            content = base64.b64encode(TINY_PDF).decode()

            def nested_letter(user):
                return str(Message.with_user(user).with_context(default_attachment_id=plan.id).onchange(
                    {'letter_ids': [[0, 'l', {}]]}, [], {'letter_ids': {'fields': {'attachment_datas': {}}}})
                ).replace("b'", "'")

            # (in this Odoo build the nested default gives no content to a reader either)
            self.assertNotIn(content, nested_letter(self.employee))

    # --- follow-up (own check of the stored-row design): joins and file places, whatever the value ---

    def test_followup_member_channel_any_id(self):
        """A member plants someone else (an employee without Project rights, a
        customer) in the internal chat: refused on the member rows as stored.
        Odoo's own member create refuses a float id (browse) and a string id
        (it cannot read the channel) before that."""
        internal = self.open_tab(self.staff, 'internal')
        Member = self.env['discuss.channel.member'].with_user(self.staff)
        planted = self.employee.partner_id | self.customer.partner_id
        for partner in planted:
            with self.subTest(partner=partner.name):
                self.refused(lambda: Member.create({'channel_id': internal.id, 'partner_id': partner.id}))
            for channel_id in (internal.id + 0.0, str(internal.id)):
                with self.subTest(channel_id=repr(channel_id), partner=partner.name):
                    self.env.flush_all()
                    self.env.invalidate_all()
                    with self.assertRaises(Exception) as caught, self.env.cr.savepoint():
                        Member.create({'channel_id': channel_id, 'partner_id': partner.id})
                    self.assertIsInstance(caught.exception, (TypeError, ValueError, AccessError, MissingError))
        self.assertFalse(self.env['discuss.channel.member'].search(
            [('channel_id', '=', internal.id), ('partner_id', 'in', planted.ids)]))
        # an ordinary group chat: as before
        group = self.env['discuss.channel'].with_user(self.staff).create({'name': 'team', 'channel_type': 'group'})
        Member.create({'channel_id': group.id, 'partner_id': self.employee.partner_id.id})
        self.assertIn(self.employee.partner_id, group.sudo().channel_member_ids.partner_id)

    def test_followup_partner_channel_link(self):
        """res.partner.channel_ids is a many2many on the member table: its
        link inserts the row without the member model. Odoo's NOT NULL
        new_message_separator refuses that row, superuser included."""
        internal = self.open_tab(self.staff, 'internal')
        for link in (internal.id, internal.id + 0.0):
            with self.subTest(link=repr(link)):
                self.env.flush_all()
                self.env.invalidate_all()
                with self.assertRaises(Exception), self.env.cr.savepoint(), mute_logger('odoo.sql_db'):
                    self.employee.partner_id.sudo().write({'channel_ids': [Command.link(link)]})
                    self.env.flush_all()
        self.assertFalse(self.env['discuss.channel.member'].search(
            [('channel_id', '=', internal.id), ('partner_id', '=', self.employee.partner_id.id)]))

    def test_followup_file_model_any_value(self):
        """A file put straight into a task chat with res_model as bytes, or as
        XML-RPC's base64 value in a default (Odoo stores the text
        'discuss.channel'; its own create check skips a model name it does not
        know, and checks nothing when res_id comes from a default). XML-RPC's
        base64 value in the values themselves fails in Odoo's create
        (unhashable)."""
        chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.staff, 'customer')
        Attachment = self.env['ir.attachment'].with_user(self.staff)
        base64_name = xmlrpc.client.Binary(b'discuss.channel')  # what XML-RPC gives for a <base64> value
        cases = {
            'bytes': (Attachment, {'res_model': b'discuss.channel', 'res_id': chat.id}),
            'bytes, id by default': (Attachment.with_context(default_res_id=chat.id),
                                     {'res_model': b'discuss.channel'}),
            'XML-RPC base64 default': (Attachment.with_context(default_res_model=base64_name,
                                                               default_res_id=chat.id), {}),
            'bytes default': (Attachment.with_context(default_res_model=b'discuss.channel',
                                                      default_res_id=chat.id), {}),
        }
        for name, (model, vals) in cases.items():
            with self.subTest(name):
                self.refused(lambda: model.create(dict(vals, name='planted.pdf', raw=TINY_PDF)))
        self.assertFalse(self.env['ir.attachment'].search(
            [('res_model', '=', 'discuss.channel'), ('res_id', '=', chat.id), ('name', '=', 'planted.pdf')]))
        # ordinary files: as before
        mine = Attachment.create({'name': 'mine.pdf', 'raw': TINY_PDF, 'res_model': 'project.task',
                                  'res_id': self.task.id})
        self.assertEqual((mine.sudo().res_model, mine.sudo().res_id), ('project.task', self.task.id))

    def test_followup_task_alert_default_any_id(self):
        """A task's AI To-Do is set by the task only: an int default is
        refused (the cache holds the stored value); Odoo's default_get turns
        a float or a string id into False, so such a task has none."""
        activity = self.task.activity_schedule('mail.mail_activity_data_todo', summary='Not yours',
                                               user_id=self.staff2.id)
        Task = self.env['project.task'].with_user(self.staff)
        self.refused(lambda: Task.with_context(default_woow_project_ai_alert_activity_id=activity.id).create(
            {'name': 'planted', 'project_id': self.project.id}))
        for value in (activity.id + 0.0, str(activity.id)):
            with self.subTest(value=repr(value)):
                # (a context equal to the int one, 1.0 == 1, would give back that environment)
                task = Task.with_context(default_woow_project_ai_alert_activity_id=value, case=repr(value)).create(
                    {'name': 'plain', 'project_id': self.project.id})
                self.env.flush_all()
                self.env.cr.execute('SELECT woow_project_ai_alert_activity_id FROM project_task WHERE id = %s',
                                    [task.id])
                self.assertIsNone(self.env.cr.fetchone()[0])


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestDelta2ReviewHttp(Delta2Helpers, AiTaskFixtures, HttpCase):
    """The reports' JSON-RPC payloads (floats and strings as JSON sends them)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_delta2()

    def call_kw(self, model, method, args, kwargs=None):
        params = {'model': model, 'method': method, 'args': args, 'kwargs': dict(kwargs or {})}
        return self.make_jsonrpc_request('/web/dataset/call_kw/%s/%s' % (model, method), params)

    def refused_kw(self, model, method, args, kwargs=None):
        # the test's records are in the cursor's cache, which the request shares: a request starts empty
        self.env.flush_all()
        self.env.invalidate_all()
        with mute_logger('odoo.http'), self.assertRaises(JsonRpcException):
            self.call_kw(model, method, args, kwargs)

    def test_payloads(self):
        _internal, secret = self.secret_message()
        chat, contract = self.customer_file()
        general = self.public_channel()
        group = self.env['discuss.channel'].with_user(self.employee).create({'name': 'mine', 'channel_type': 'group'})
        # 16: a portal account copies with a float id
        self.authenticate('aitask_outsider', 'aitask_outsider')
        self.refused_kw('mail.message', 'copy', [[secret.id + 0.0]],
                        {'default': {'model': False, 'res_id': False, 'parent_id': False, 'attachment_ids': []}})
        if self.has_parent_body():
            self.env.flush_all()
            self.env.invalidate_all()
            try:
                result = self.call_kw('mail.message', 'onchange', [[], {'parent_id': {'id': [secret.id]}}, [],
                                                                   {'parent_body': {}}])
            except JsonRpcException:
                result = None
            self.assertNotIn(SECRET, json.dumps(result, ensure_ascii=False))
        # 2 / 4 / 5: an employee
        self.authenticate('aitask_employee', 'aitask_employee')
        self.refused_kw('discuss.channel', 'create', [{'name': 'x', 'channel_type': 'channel',
                                                       'parent_channel_id': general.id,
                                                       'from_message_id': str(secret.id)}])
        data = self.make_jsonrpc_request('/discuss/channel/sub_channel/fetch', {'parent_channel_id': general.id})
        self.assertNotIn(SECRET, json.dumps(data, ensure_ascii=False))
        if 'snailmail.letter' in self.env:
            self.refused_kw('snailmail.letter', 'create', [{
                'model': 'discuss.channel', 'res_id': group.id, 'partner_id': self.employee.partner_id.id,
                'attachment_id': contract.id + 0.0}])
        # 15 / 17: a member of the customer chat
        self.authenticate('aitask_staff', 'aitask_staff')
        self.refused_kw('ir.attachment', 'unlink', [[contract.id + 0.0]])
        with mute_logger('odoo.http'), self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request('/mail/message/post', {
                'thread_model': 'discuss.channel', 'thread_id': chat.id,
                'post_data': {'body': 'x', 'message_type': 'comment', 'subtype_xmlid': 'mail.mt_comment',
                              'attachment_ids': [contract.id + 0.0]}})
        self.assertTrue(contract.exists())
        self.assertEqual(self.find(chat, 'my signed contract').attachment_ids, contract)
