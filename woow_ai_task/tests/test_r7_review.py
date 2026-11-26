# -*- coding: utf-8 -*-
"""Seventh review (79bb013): the logs of every handler (item 1), the
uninstall hook and the administrators' point check under test (item 3), the
internal chat's labels explained (item 6), the reply job from explicit values
(item 7), the notification bridge's hook pin (item 10) and no statement for
ordinary delivery-failure notices (item 11). The document bridge's call-shape
check (items 2 and 4): tests/test_r6_review.py test_16."""

import ast
import json
import logging
from pathlib import Path
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools.misc import file_path

from odoo.addons.woow_ai_task.models.discuss_channel import with_sync

from .common import AiTaskFixtures
from .test_delta2_review import installed
from .test_r6_review import R6Helpers

TEXT = 'exception-text-must-stay-out-7f3a'  # a marker, not a real value
MODULES = ('woow_ai_task', 'woow_ai_task_portal_discuss', 'woow_ai_task_portal_notification',
           'woow_ai_task_snailmail', 'woow_ai_task_document_management')


def formatted(logs):
    """Every record as the server log prints it (message, exc_info, stack)."""
    formatter = logging.Formatter('%(levelname)s %(name)s: %(message)s')
    return '\n'.join(formatter.format(record) for record in logs.records)


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestR7Review(R6Helpers, AiTaskFixtures, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_delta2()

    # --- 1: every handler logs the type and the code lines, never the text (contract v1.2 3.9) ---

    def test_01_sources_never_log_exception_text(self):
        offenders = []
        for module in MODULES:
            try:
                root = Path(file_path(module))
            except FileNotFoundError:
                continue
            for path in sorted(root.rglob('*.py')):
                if 'tests' in path.relative_to(root).parts:
                    continue
                for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                        continue
                    where = '%s:%s' % (path.relative_to(root.parent), node.lineno)
                    if node.func.attr in ('exception', 'format_exc', 'print_exc'):
                        offenders.append('%s %s()' % (where, node.func.attr))
                    if any(keyword.arg in ('exc_info', 'stack_info') for keyword in node.keywords):
                        offenders.append('%s exc_info/stack_info' % where)
        self.assertFalse(offenders, 'log unexpected exceptions with tools/logs.log_unexpected')

    def test_01_message_trigger_logs_the_type_only(self):
        chat = self.open_tab(self.staff, 'internal')
        Channel = type(self.env['discuss.channel'])
        with patch.object(Channel, '_woow_project_ai_handle_new_message', side_effect=RuntimeError(TEXT)), \
                self.assertLogs('odoo.addons.woow_ai_task', logging.ERROR) as logs:
            message = self.post(chat, self.staff, 'hello')
        self.assertTrue(message.exists(), 'the message is kept')
        text = formatted(logs)
        self.assertIn('RuntimeError', text)
        self.assertNotIn(TEXT, text)
        self.assertNotIn('Traceback (most recent call last)', text, 'frames only, no exc_info')

    # --- 3: the uninstall hook's two statements ---

    def test_03_uninstall_hook_marks_read_and_removes_people(self):
        from odoo.addons.woow_ai_task.hooks import uninstall_hook  # noqa: PLC0415
        task = self.portal_task('Uninstall check')
        chat = self.open_tab(self.customer, 'customer', task=task)
        self.open_tab(self.staff, 'customer', task=task)
        message = self.mention(chat, self.staff, self.customer.partner_id, '<p>Carl, the quote is ready</p>')
        note = self.env['mail.notification'].search([('mail_message_id', '=', message.id),
                                                     ('res_partner_id', '=', self.customer.partner_id.id)])
        self.assertTrue(note)
        note.write({'is_read': False, 'read_date': False})
        # a member without partner (a guest), as Odoo allows in a channel
        guest = self.env['mail.guest'].create({'name': 'Visitor Vic'})
        with_sync(self.env['discuss.channel.member'].sudo()).create({'channel_id': chat.id, 'guest_id': guest.id})
        Channel = self.env['discuss.channel'].sudo().with_context(active_test=False)
        bots = set(Channel._woow_project_ai_bot_partner_ids())
        chats = Channel.search([('woow_project_ai_task_id', '!=', False)])
        self.env.flush_all()

        def state():
            self.env.invalidate_all()
            members = chats.channel_member_ids
            return {
                'read': (note.is_read, note.read_date),
                'people': members.filtered(lambda m: not m.partner_id or m.partner_id.id not in bots),
                'bots': sorted(members.partner_id.ids),
                'active': chats.filtered('active'),
            }

        uninstall_hook(self.env)
        first = state()
        self.assertTrue(first['read'][0])
        self.assertTrue(first['read'][1], 'read_date set')
        self.assertFalse(first['people'], 'no person left, guests included')
        self.assertTrue(first['bots'] and set(first['bots']) <= bots, 'the bots stay')
        self.assertFalse(first['active'], 'every task chat archived')
        # a second run changes nothing
        uninstall_hook(self.env)
        self.assertEqual(state(), first)

    # --- 3: the administrators' point check and their member-only message lists ---

    def test_03_administrators_read_chats_they_are_not_in(self):
        admin = new_test_user(self.env, 'r7_admin', name='Admin Ada', groups='base.group_user,base.group_system,'
                              'project.group_project_manager')
        internal = self.open_tab(self.staff, 'internal')
        customer_chat = self.open_tab(self.customer, 'customer')
        posted = self.post(internal, self.staff, 'an internal question') | self.post(customer_chat, self.customer,
                                                                                    'a customer question')
        Message = self.env['mail.message'].with_user(admin)
        for chat in (internal, customer_chat):
            with self.subTest(chat=chat.woow_project_ai_tab):
                self.assertNotIn(admin.partner_id, chat.sudo().channel_member_ids.partner_id)
                found = Message.search([('model', '=', 'discuss.channel'), ('res_id', '=', chat.id)])
                self.assertIn(posted.filtered(lambda m, chat=chat: m.res_id == chat.id), found)
                self.assertTrue(found.with_user(admin).read(['body']))
                self.assertEqual(
                    self.env['discuss.channel'].with_user(admin)._woow_project_ai_readable_chat_ids({chat.id}),
                    {chat.id})
        # the lists of messages (Inbox, History...): the chats they are a member of, as a literal list (no
        # compiled arm), not the ones they only may open
        own = self.open_tab(admin, 'internal', task=self.portal_task('Admin own'))
        exclusion = Message._woow_project_ai_search_exclusion([('needaction', '=', True)])
        lists = [leaf[2] for leaf in exclusion
                 if isinstance(leaf, (list, tuple)) and len(leaf) == 3 and tuple(leaf[:2]) == ('res_id', 'in')]
        for value in lists:
            self.assertIsInstance(value, (list, tuple), 'member chats as a list, no compiled arm')
        listed = {chat_id for value in lists for chat_id in value}
        self.assertIn(own.id, listed)
        self.assertFalse({internal.id, customer_chat.id} & listed)
        # a system administrator without Project rights is still refused the internal chat
        plain = new_test_user(self.env, 'r7_plain_admin', name='Plain Pat', groups='base.group_user,base.group_system')
        self.assertFalse(self.env['discuss.channel'].with_user(plain)._woow_project_ai_readable_chat_ids(
            {internal.id}))
        self.assertFalse(self.env['mail.message'].with_user(plain).search([('model', '=', 'discuss.channel'),
                                                                          ('res_id', '=', internal.id)]))
        with self.assertRaises(AccessError):
            posted.filtered(lambda m: m.res_id == internal.id).with_user(plain).read(['body'])

    # --- 6: the internal chat's name labels are explained ---

    def test_06_internal_labels_are_explained(self):
        from odoo.addons.woow_ai_task.models.discuss_channel import INTERNAL_LABEL_NOTE  # noqa: PLC0415 - round 7
        internal = self.open_tab(self.staff, 'internal')
        self.post(internal, self.staff, 'when is the quote due?')

        def prompt():
            history = internal._woow_project_ai_history(
                self.internal_ai, internal._woow_project_ai_context_messages(self.internal_ai))
            return history, internal._woow_project_ai_system_prompt(self.internal_ai, history)

        history, text = prompt()
        self.assertTrue(history[-1]['text'].startswith('[Staff Sam] '))
        self.assertIn(INTERNAL_LABEL_NOTE, text)
        # (the flag stays inside: n8n gets the entries' own keys)
        payload = self.internal_ai._n8n_payload(internal, history, self.internal_ai._identity(self.staff.partner_id),
                                                'session', 'https://odoo.example.com')
        self.assertNotIn('woow_project_ai_labelled', json.dumps(payload, default=str))
        # no label without a name (a name with an '@'): nothing to explain
        self.staff.name = 'sam.staff@example.com'
        history, text = prompt()
        self.assertFalse(history[-1]['text'].startswith('['))
        self.assertNotIn(INTERNAL_LABEL_NOTE, text)

    # --- 7: the reply job from explicit values (a personal ir.default reaches no field) ---

    def test_07_personal_defaults_never_reach_a_reply_job(self):
        internal = self.open_tab(self.staff, 'internal')
        Default = self.env['ir.default'].with_user(self.staff)
        for field, value in (('state', 'running'), ('run_count', 3), ('started_at', '2099-01-01 00:00:00'),
                             ('finished_at', '2099-01-01 00:00:00'), ('error', 'forged'),
                             ('context_message_id', 99)):
            Default.set('woow.project.ai.job', field, value, user_id=True)
        message = self.post(internal, self.staff, 'a question')
        self.assert_fresh_job(self.Job.search([('message_id', '=', message.id)]))

    # --- 10: the notification bridge's private hook, pinned by a CRITICAL log ---

    def test_10_notification_bridge_hook_pin(self):
        if not installed(self.env, 'woow_portal_notification', 'woow_ai_task_portal_notification'):
            self.skipTest('the portal notification centre is not installed')
        from odoo.addons.woow_ai_task_portal_notification.models import res_users as bridge  # noqa: PLC0415
        Users = self.env['res.users']
        self.assertFalse(bridge.hook_missing(type(Users), bridge.ResUsers))
        base = type('Base', (), {})
        middle = type('Bridge', (base,), {})
        self.assertTrue(bridge.hook_missing(type('Model', (middle,), {}), middle))
        # a release without the hook: logged as CRITICAL when the registry loads, never a refusal
        with patch.object(bridge, 'hook_missing', return_value=True), \
                self.assertLogs(bridge.__name__, logging.CRITICAL) as logs:
            Users._register_hook()
        self.assertIn('_wpn_public_message_domain', logs.output[0])

    # --- 11: ordinary delivery-failure notices: no statement of the module ---

    def test_11_failure_notices_without_task_chats_need_no_statement(self):
        note = self.task.message_post(body='an ordinary note', message_type='comment', subtype_xmlid='mail.mt_note')
        statements = []
        Environment = type(self.env)
        original = Environment.execute_query

        def spy(env, query):
            statements.append(str(getattr(query, 'code', query)))
            return original(env, query)

        with patch.object(Environment, 'execute_query', spy):
            note._notify_message_notification_update()
        self.assertFalse([s for s in statements if 'author_id FROM mail_message' in s])
