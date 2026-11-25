# -*- coding: utf-8 -*-
"""Regression tests of the team review (the item numbers are in the test
names). Downloads (2, 6, 7) are in test_media /
test_job, the dispatchers (28, 35, 36) in test_dispatch, the Markdown
nesting (27) in test_markdown, the portal counters (3, 10, 38) in
test_coexistence."""

import json
import re
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import HttpCase, JsonRpcException, TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from odoo.addons.woow_ai_task.models.discuss_channel import SWEEP_CURSOR_PARAM

from .common import BACKEND_POST, AiTaskFixtures, http_response, run_after_commit_usage


class ReviewHelpers:

    def setUp(self):
        super().setUp()
        patcher = patch.object(type(self.env['woow.project.ai.job']), '_start_after_commit')
        patcher.start()
        self.addCleanup(patcher.stop)

    def comment(self):
        return self.env.ref('mail.mt_comment').id

    def find(self, channel, body):
        return self.env['mail.message'].sudo().search(
            [('model', '=', 'discuss.channel'), ('res_id', '=', channel.id), ('body', 'ilike', body)])

    def lose_access_silently(self, user, task=None):
        """`user` stops following the task without any trigger (only the sync sees it)."""
        self.env.flush_all()
        self.env.cr.execute("DELETE FROM mail_followers WHERE res_model = 'project.task' AND res_id = %s "
                            "AND partner_id = %s", ((task or self.task).id, user.partner_id.id))
        self.env.invalidate_all()

    def todos(self):
        return self.env['mail.activity'].search([('res_model', '=', 'project.task'), ('res_id', '=', self.task.id)])

    def make_old(self, job):
        self.env.flush_all()
        self.env.cr.execute("UPDATE woow_project_ai_job SET create_date = %s WHERE id = %s",
                            (fields.Datetime.now() - timedelta(hours=1), job.id))
        job.invalidate_recordset()

    def file_of(self, chat, user, name, body):
        """A file `user` uploaded and posted in `chat` (as the composer does)."""
        attachment = self.pending_attachment(user, name=name)
        message = self.post(chat, user, body, attachments=attachment)
        self.assertEqual(message.attachment_ids, attachment)
        return attachment


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestTeamReview(ReviewHelpers, AiTaskFixtures, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_ai_task()

    # --- 1: a reply's parent ---

    def test_01_no_parent_from_a_task_chat(self):
        customer_chat = self.open_tab(self.customer, 'customer')
        internal = self.open_tab(self.staff, 'internal')
        secret = self.post(internal, self.staff, 'INTERNAL SECRET: our margin')
        # an employee's own group chat: a message created with that parent, or written to it
        group = self.env['discuss.channel'].with_user(self.staff2).create({'name': 'team', 'channel_type': 'group'})
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            self.env['mail.message'].with_user(self.staff2).create({
                'model': 'discuss.channel', 'res_id': group.id, 'body': 'quote', 'message_type': 'comment',
                'subtype_id': self.comment(), 'parent_id': secret.id})
        own = group.with_user(self.staff2).message_post(body='mine', message_type='comment',
                                                        subtype_xmlid='mail.mt_comment')
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            own.with_user(self.staff2).write({'parent_id': secret.id})
        self.assertFalse(own.parent_id)
        # a task chat message is never created directly (whatever its parent: another chat's, a log note)
        note = self.task.message_post(body='LOG NOTE', message_type='comment', subtype_xmlid='mail.mt_note')
        for parent in (secret, note, self.env['mail.message']):
            with self.subTest(parent=parent.body), self.assertRaises(AccessError), self.env.cr.savepoint():
                self.env['mail.message'].with_user(self.customer).create({
                    'model': 'discuss.channel', 'res_id': customer_chat.id, 'body': '.', 'message_type': 'comment',
                    'subtype_id': self.comment(), 'parent_id': parent.id or False})
        # a post with a foreign parent: Odoo keeps a channel message's parent in that channel
        reply = customer_chat.with_user(self.customer).sudo().message_post(
            body='hello', message_type='comment', subtype_xmlid='mail.mt_comment', parent_id=secret.id)
        self.assertFalse(reply.parent_id)
        # a reply inside the chat stays possible
        self.open_tab(self.staff2, 'internal')
        answer = self.post(internal, self.staff2, 'ok')
        inside = internal.with_user(self.staff).sudo().message_post(
            body='re', message_type='comment', subtype_xmlid='mail.mt_comment', parent_id=answer.id)
        self.assertEqual(inside.parent_id, answer)

    # --- 5: other people's files ---

    def test_05_members_cannot_change_others_files(self):
        chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.staff, 'customer')
        contract = self.file_of(chat, self.customer, 'contract.pdf', 'my signed contract')
        Attachment = self.env['ir.attachment'].with_user(self.staff)
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            Attachment.browse(contract.id).write({'raw': b'%PDF-1.4 another file'})
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            Attachment.browse(contract.id).write({'res_id': self.open_tab(self.staff, 'internal').id})
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            Attachment.browse(contract.id).unlink()
        self.assertTrue(contract.exists())
        # a colleague's own file stays theirs to change
        mine = self.file_of(chat, self.staff, 'plan.pdf', 'the plan')
        Attachment.browse(mine.id).write({'name': 'plan-v2.pdf'})
        # files of other channels: Odoo's rules only
        group = self.env['discuss.channel'].with_user(self.staff).create({'name': 'g', 'channel_type': 'group'})
        other = self.env['ir.attachment'].create({'name': 'x.txt', 'raw': b'x', 'res_model': 'discuss.channel',
                                                  'res_id': group.id})
        self.assertFalse(other.with_user(self.staff)._woow_project_ai_foreign_files())

    # --- 9 / 13: unanswered and failed customer messages ---

    def test_09_13_every_unanswered_customer_message_alerts(self):
        chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.staff, 'customer')
        self.project.woow_project_ai_customer_assistant_id = self.customer_n8n
        self.customer_n8n.connection_id.customer_safe = False
        with patch(BACKEND_POST) as post:
            self.post(chat, self.customer, 'is anyone there?')
            self.run_due(chat)
        post.assert_not_called()
        first = self.todos()
        self.assertEqual(len(first), 1)
        # the To-Do is done, the AI is still blocked: the next customer message alerts again
        first.action_feedback(feedback='called back')
        self.assertFalse(self.todos())
        self.post(chat, self.customer, 'still waiting')
        second = self.todos()
        self.assertEqual(len(second), 1, 'a new To-Do in the same blocked period')
        second.action_feedback(feedback='done')
        # a backend failure in the customer chat: the error message, and the colleagues' To-Do
        self.customer_n8n.connection_id.customer_safe = True
        self.post(chat, self.customer, 'what about the price?')
        with patch(BACKEND_POST, return_value=http_response(500, text='boom')):
            for _attempt in range(4):
                self.run_due(chat)
        failed = self.todos()
        self.assertEqual(len(failed), 1)
        self.assertIn('could not answer', failed.note)
        # a question that waited too long: told (and the To-Do again)
        failed.action_feedback(feedback='done')
        self.post(chat, self.customer, 'hello again')
        job = self.jobs(chat)[-1]
        self.make_old(job)
        before = len(self.bot_messages(chat))
        self.run_due(chat)
        self.assertEqual(job.state, 'failed')
        self.assertEqual(len(self.bot_messages(chat)), before + 1, 'the error message is posted')
        self.assertEqual(len(self.todos()), 1)

    def test_09_old_internal_question_told(self):
        internal = self.open_tab(self.staff, 'internal')
        self.post(internal, self.staff, 'old question')
        job = self.jobs(internal)[-1]
        self.make_old(job)
        with patch(BACKEND_POST) as post:
            self.run_due(internal)
        post.assert_not_called()
        self.assertEqual(job.state, 'failed')
        self.assertTrue(job.reply_message_id, 'the person learns their question was not answered')
        self.assertFalse(self.todos(), 'colleagues read the internal chat: no To-Do')

    # --- 11: the global rule closes on any error ---

    def test_11_a_broken_task_rule_closes_task_chats_only(self):
        broken_group = self.env['res.groups'].create({'name': 'Broken task rule'})
        self.staff2.groups_id = [Command.link(broken_group.id)]
        group_chat = self.env['discuss.channel'].with_user(self.staff2).create(
            {'name': 'mine', 'channel_type': 'group'})
        internal = self.open_tab(self.staff, 'internal')
        # valid when it is saved, but it cannot be evaluated for that user
        self.env['ir.rule'].create({
            'name': 'broken', 'model_id': self.env.ref('project.model_project_task').id,
            'groups': [Command.link(broken_group.id)],
            'domain_force': "[('id', '!=', 0 if user.id != %d else 1 / 0)]" % self.staff2.id})
        self.env.registry.clear_cache()
        Channel = self.env['discuss.channel'].with_user(self.staff2)
        with mute_logger('odoo.addons.woow_ai_task.models.discuss_channel'):
            self.assertEqual(Channel.search([('id', '=', group_chat.id)]), group_chat)
            self.assertFalse(Channel.search([('id', '=', internal.id)]))

    # --- 12 / 24: the assistant's description on the project page ---

    def test_12_24_description_shown_to_project_managers(self):
        self.internal_ai.description = 'Knows our suppliers'
        project = self.project.with_user(self.manager)
        self.assertEqual(project.woow_project_ai_internal_assistant_description, 'Knows our suppliers')
        arch = self.env['project.project'].with_user(self.manager).get_views([(False, 'form')])['views']['form']['arch']
        self.assertIn('woow_project_ai_internal_assistant_description', arch)
        listed = self.env['woow.project.ai.assistant'].with_user(self.manager).get_views([(False, 'list')])
        self.assertIn('name="description"', listed['views']['list']['arch'])

    # --- 17 / 41: zh_TW terms ---

    def test_17_41_traditional_chinese_terms(self):
        po = (Path(__file__).parent.parent / 'i18n' / 'zh_TW.po').read_text(encoding='utf-8')
        msgstrs = re.findall(r'^msgstr "(.*)"$', po, re.M)
        for simplified in ('消息', '渠道', '设置', '用户'):
            self.assertFalse([m for m in msgstrs if simplified in m], simplified)
        # names of models this module does not own are left to their own module
        for block in po.split('\n\n'):
            if 'model:ir.model,name:woow_ai_task.model_' in block and 'woow_project_ai' not in block:
                self.assertRegex(block, r'msgstr ""\s*$', block)
        self.assertNotIn('尚未就緒', po)
        self.assertNotIn('端點網址', po)

    # --- 20: usage totals and deleted projects ---

    def test_20_usage_totals(self):
        Usage = self.env['woow.project.ai.usage.month']
        Usage._add(self.internal_ai, self.internal_ai.connection_id, surface_type='project_task',
                   scope_model='project.project', scope_res_id=self.project.id, prompt_tokens=10, completion_tokens=5,
                   surface_tab='internal')
        Usage._add(self.internal_ai, self.internal_ai.connection_id, surface_type='project_task',
                   scope_model='project.project', scope_res_id=self.project.id, prompt_tokens=1, completion_tokens=2,
                   surface_tab='internal')
        row = Usage.search([('scope_res_id', '=', self.project.id), ('surface_tab', '=', 'internal')])
        self.assertEqual((row.total_tokens, row.project_id), (18, self.project))
        groups = Usage.read_group([('id', '=', row.id)], ['total_tokens:sum'], ['surface_tab'])
        self.assertEqual(groups[0]['total_tokens'], 18)
        self.assertIn('sum="Total"', Usage.get_views([(False, 'list')])['views']['list']['arch'].split(
            'total_tokens')[1][:40])
        # a deleted project: a readable label, not a raw reference
        row.scope_res_id = 999999
        row.invalidate_recordset(['scope_name'])
        self.assertIn('999999', row.scope_name)
        self.assertNotIn('project.project,', row.scope_name)

    # --- 21: the access-loss triggers ---

    def test_21_collaborators(self):
        collaborator_user = new_test_user(self.env, 'aitask_collab', name='Collaborator Cleo',
                                          groups='base.group_portal')
        # a project collaborator who does not follow the task reads it, and opens the customer chat
        collaborator = self.env['project.collaborator'].create({'project_id': self.project.id,
                                                                 'partner_id': collaborator_user.partner_id.id})
        self.assertTrue(self.task.with_user(collaborator_user).has_access('read'))
        chat = self.open_tab(collaborator_user, 'customer')
        self.assertIn(collaborator_user.partner_id, self.members(chat))
        # limited access (followed tasks only): out at once
        collaborator.limited_access = True
        self.assertNotIn(collaborator_user.partner_id, self.members(chat))
        # back, then the collaborator removed: out at once
        collaborator.limited_access = False
        self.open_tab(collaborator_user, 'customer')
        collaborator.unlink()
        self.assertNotIn(collaborator_user.partner_id, self.members(chat))

    def test_21_groups_users_and_companies(self):
        internal = self.open_tab(self.staff2, 'internal')
        # taken out of Project User through the group's users
        self.env.ref('project.group_project_user').write({'users': [Command.unlink(self.staff2.id)]})
        self.staff2.invalidate_recordset(['groups_id'])
        self.assertNotIn(self.staff2.partner_id, self.members(internal))
        # a deleted user
        internal = self.open_tab(self.staff, 'internal')
        customer_chat = self.open_tab(self.customer2, 'customer')
        partner = self.customer2.partner_id
        self.customer2.unlink()
        self.assertNotIn(partner, self.members(customer_chat))
        # a company change: the task in a company the colleague has no access to
        other = self.env['res.company'].create({'name': 'Other Kitchens'})
        self.assertIn(self.staff.partner_id, self.members(internal))
        self.project.company_id = other
        self.task.company_id = other
        self.assertNotIn(self.staff.partner_id, self.members(internal))

    # --- 22: archived tasks are read only ---

    def test_22_archived_task_chats_take_no_pins_reactions_or_renames(self):
        internal = self.open_tab(self.staff, 'internal')
        message = self.post(internal, self.staff, 'before archiving')
        self.task.active = False
        chat = internal.with_user(self.staff)
        with self.assertRaises(UserError), self.env.cr.savepoint():
            chat.set_message_pin(message.id, True)
        self.assertFalse(message.pinned_at)
        with self.assertRaises(UserError), self.env.cr.savepoint():
            message.with_user(self.staff)._message_reaction('👍', 'add', self.staff.partner_id,
                                                            self.env['mail.guest'])
        with self.assertRaises(UserError), self.env.cr.savepoint():
            chat.write({'name': 'renamed'})
        # restored: all of it again
        self.task.active = True
        chat.set_message_pin(message.id, True)
        self.assertTrue(message.pinned_at)

    # --- 26 / 29 / 4: live messages ---

    def test_26_29_removed_members_get_no_live_message(self):
        chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.customer2, 'customer')
        Bus = self.env['bus.bus']
        db = self.env.cr.dbname
        channels = [(db, 'discuss.channel', chat.id), (db, 'discuss.channel', chat.id, 'members')]

        def last_id():
            self.env.flush_all()
            self.env.cr.precommit.run()
            return Bus.sudo().search([], order='id desc', limit=1).id or 0

        def polled(user, since):
            self.env.flush_all()
            self.env.cr.precommit.run()
            return Bus.with_user(user)._poll(channels, last=since)

        start = last_id()
        self.post(chat, self.customer2, 'first question')
        self.assertTrue(polled(self.customer, start), 'a member gets the chat live')
        # the customer loses the access unseen; the next post removes them first, in its own transaction
        self.lose_access_silently(self.customer)
        start = last_id()
        self.post(chat, self.customer2, 'second question')
        self.assertNotIn(self.customer.partner_id, self.members(chat))
        self.assertFalse(polled(self.customer, start), 'not even the message whose post removed them')
        self.assertTrue(polled(self.customer2, start))
        # an AI reply or a push later: still nothing for them
        start = last_id()
        self.customer_ai.sudo()._post_text(chat, 'an AI answer')
        self.assertFalse(polled(self.customer, start))
        # other channels: unchanged
        group = self.env['discuss.channel'].with_user(self.staff).create({'name': 'g', 'channel_type': 'group'})
        group.with_user(self.staff).add_members(partner_ids=self.customer.partner_id.ids)
        start = last_id()
        group.with_user(self.staff).message_post(body='hi', message_type='comment', subtype_xmlid='mail.mt_comment')
        self.env.flush_all()
        self.env.cr.precommit.run()
        self.assertTrue(Bus.with_user(self.customer)._poll([(db, 'discuss.channel', group.id)], last=start))
        # public pollers (guests) never get a task chat
        self.assertFalse(Bus.with_user(self.env.ref('base.public_user'))._poll(channels, last=0))

    # --- 30 / 31: when the tabs are offered ---

    def test_30_unsaved_project_change_does_not_switch_the_tabs(self):
        other = self.env['project.project'].create({'name': 'No AI'})
        draft = self.task.new({'project_id': other.id}, origin=self.task)
        self.assertTrue(draft.woow_project_ai_internal_available, 'the saved project decides')
        self.task.project_id = other
        self.assertFalse(self.task.woow_project_ai_internal_available)

    def test_31_customer_tab_needs_a_portal_project(self):
        self.assertTrue(self.task.woow_project_ai_customer_available)
        self.assertEqual(self.project.woow_project_ai_customer_state, 'ok')
        self.project.privacy_visibility = 'employees'
        self.project.invalidate_recordset()
        self.task.invalidate_recordset()
        self.assertFalse(self.task.woow_project_ai_customer_available)
        self.assertEqual(self.project.woow_project_ai_customer_state, 'blocked')
        self.assertIn('not shared with portal users', self.project.woow_project_ai_customer_state_reason)
        self.assertIn('not_shared', self.project._woow_project_ai_tab_problems('customer'))

    # --- 34: the internal project text needs the project: since the fix-delta review (item 7) the internal
    # chat itself needs the project (test_delta_review.test_07_internal_chat_needs_the_project) ---

    # --- 39: the sweep resumes ---

    def test_39_sweep_resumes_where_it_stopped(self):
        chats = self.open_tab(self.customer, 'customer') | self.open_tab(self.staff, 'internal')
        task2 = self.env['project.task'].create({'name': 'Second', 'project_id': self.project.id})
        chats |= self.open_tab(self.staff, 'internal', task=task2)
        Channel = self.env['discuss.channel']
        progress = []
        with patch.object(type(self.env['ir.cron']), '_notify_progress',
                          lambda cron, done, remaining, deactivate=False: progress.append((done, remaining))):
            self.assertEqual(Channel._woow_project_ai_cron_sync(batch_size=1, time_slice=0), 1)
            self.assertEqual(Channel._woow_project_ai_sweep_cursor(), min(chats.ids))
            self.assertEqual(progress[-1], (1, 2))
            self.assertEqual(Channel._woow_project_ai_cron_sync(batch_size=1, time_slice=0), 1)
            self.assertEqual(Channel._woow_project_ai_cron_sync(batch_size=1, time_slice=0), 1)
            self.assertEqual(progress[-1], (1, 0))
            self.assertEqual(Channel._woow_project_ai_sweep_cursor(), 0, 'a full round: the next one starts over')
            self.assertEqual(Channel._woow_project_ai_cron_sync(batch_size=10), 3)
        self.env.cr.execute("SELECT value FROM ir_config_parameter WHERE key = %s", [SWEEP_CURSOR_PARAM])
        self.assertEqual(self.env.cr.fetchone(), ('0',))

    # --- 40: what "Ready" means per tab ---

    def test_40_ready_text_per_tab(self):
        self.assertNotEqual(self.project.woow_project_ai_internal_state_reason,
                            self.project.woow_project_ai_customer_state_reason)
        self.assertIn('pauses', self.project.woow_project_ai_customer_state_reason)


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestTeamReviewHttp(ReviewHelpers, AiTaskFixtures, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_ai_task()

    def call_kw(self, model, method, args, kwargs=None):
        params = {'model': model, 'method': method, 'args': args, 'kwargs': dict(kwargs or {})}
        return self.make_jsonrpc_request('/web/dataset/call_kw/%s/%s' % (model, method), params)

    def refused_route(self, route, params):
        with mute_logger('odoo.http'), self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request(route, params)

    def test_01_forged_parent_reads_nothing(self):
        customer_chat = self.open_tab(self.customer, 'customer')
        internal = self.open_tab(self.staff, 'internal')
        secret = self.post(internal, self.staff, 'INTERNAL SECRET about another customer')
        self.authenticate('aitask_customer', 'aitask_customer')
        with mute_logger('odoo.http'), self.assertRaises(JsonRpcException):
            self.call_kw('mail.message', 'create', [{
                'model': 'discuss.channel', 'res_id': customer_chat.id, 'message_type': 'comment',
                'subtype_id': self.comment(), 'author_id': self.customer.partner_id.id, 'body': '.',
                'parent_id': secret.id}])
        self.make_jsonrpc_request('/mail/message/post', {
            'thread_model': 'discuss.channel', 'thread_id': customer_chat.id,
            'post_data': {'body': 'a reply', 'message_type': 'comment', 'subtype_xmlid': 'mail.mt_comment',
                          'parent_id': secret.id}})
        data = self.make_jsonrpc_request('/discuss/channel/messages', {'channel_id': customer_chat.id})
        self.assertNotIn('INTERNAL SECRET', json.dumps(data, ensure_ascii=False))

    def test_25_customers_cannot_take_a_colleagues_file(self):
        chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.staff, 'customer')
        quote = self.file_of(chat, self.staff, 'quotation.pdf', 'our quotation')
        own = self.post(chat, self.customer, 'my question')
        self.authenticate('aitask_customer', 'aitask_customer')
        # a direct message linking it
        with mute_logger('odoo.http'), self.assertRaises(JsonRpcException):
            self.call_kw('mail.message', 'create', [{
                'model': 'discuss.channel', 'res_id': chat.id, 'message_type': 'comment',
                'subtype_id': self.comment(), 'body': '.', 'attachment_ids': [Command.link(quote.id)]}])
        # a post or an edit with its id
        self.refused_route('/mail/message/post', {
            'thread_model': 'discuss.channel', 'thread_id': chat.id,
            'post_data': {'body': 'mine now', 'message_type': 'comment', 'subtype_xmlid': 'mail.mt_comment',
                          'attachment_ids': [quote.id]}})
        self.refused_route('/mail/message/update_content', {
            'message_id': own.id, 'body': 'my question', 'attachment_ids': [quote.id]})
        # and so neither delete route can reach it
        self.refused_route('/mail/attachment/delete', {'attachment_id': quote.id})
        self.make_jsonrpc_request('/mail/message/update_content', {'message_id': own.id, 'body': '',
                                                                   'attachment_ids': []})
        self.assertTrue(quote.exists())
        self.assertEqual(quote.sudo().res_id, chat.id)
        self.assertEqual(self.find(chat, 'our quotation').attachment_ids, quote)

    def test_05_employees_cannot_delete_others_files(self):
        chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.staff, 'customer')
        contract = self.file_of(chat, self.customer, 'contract.pdf', 'signed')
        self.authenticate('aitask_staff', 'aitask_staff')
        self.refused_route('/mail/attachment/delete', {'attachment_id': contract.id})
        self.assertTrue(contract.exists())
        # the customer deletes their own message: its file goes with it
        message = self.find(chat, 'signed')
        self.authenticate('aitask_customer', 'aitask_customer')
        self.make_jsonrpc_request('/mail/message/update_content', {'message_id': message.id, 'body': '',
                                                                   'attachment_ids': []})
        self.assertFalse(contract.exists())

    def test_18_stale_open_token_reloads_the_task_page(self):
        self.authenticate('aitask_customer', 'aitask_customer')
        base = '/my/tasks/%s/woow_project_ai?token=stale' % self.task.id
        framed = self.url_open(base, allow_redirects=False)
        self.assertEqual(framed.status_code, 200)
        self.assertIn('target="_top"', framed.text)
        self.assertNotIn('o_woow_project_ai_portal_frame', framed.text, 'not the whole task page in the frame')
        full = self.url_open(base + '&full=1', allow_redirects=False)
        self.assertIn(full.status_code, (301, 302, 303))
        self.assertIn('/my/tasks/%s' % self.task.id, full.headers['Location'])

    def test_08_push_counted_after_the_commit(self):
        chat = self.open_tab(self.staff, 'internal')
        key = self.internal_ai.action_generate_push_key()['context']['default_push_key']
        Usage = self.env['woow.project.ai.usage.month']
        seen = {}

        def push():
            response = self.url_open('/woow_project_ai/v1/push', data=json.dumps(
                {'to': {'channel_id': chat.id}, 'text': 'pushed later'}),
                headers={'Content-Type': 'application/json', 'Authorization': 'Bearer %s' % key})
            Usage.invalidate_model()
            seen['before_commit'] = len(Usage.search([('assistant_id', '=', self.internal_ai.id)]))
            return response

        response, counts = run_after_commit_usage(self, push)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(counts), 1, 'counted after the commit, on a cursor of its own')
        self.assertEqual(seen['before_commit'], 0, 'not in the request transaction')
        row = Usage.search([('assistant_id', '=', self.internal_ai.id)])
        self.assertEqual((row.replies_ok, row.surface_tab), (1, 'internal'))
