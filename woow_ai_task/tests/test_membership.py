# -*- coding: utf-8 -*-
"""Membership security of the task AI conversations, through every route: only
the task's AI tab adds members; access loss removes them (sync, post time);
a member who lost the access reads nothing even before the sync."""

import json
import re
from unittest.mock import patch

import odoo
from odoo.exceptions import AccessError
from odoo.tests.common import HttpCase, JsonRpcException, new_test_user, tagged
from odoo.tools import mute_logger

from .common import BACKEND_POST, TINY_PDF, AiTaskFixtures, openai_ok


@tagged('post_install', '-at_install', 'woow_ai_task')
class TestMembershipSecurity(AiTaskFixtures, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_ai_task()

    def setUp(self):
        super().setUp()
        # never start real worker threads from HTTP requests in tests
        patcher = patch.object(type(self.env['woow.project.ai.job']), '_start_after_commit')
        patcher.start()
        self.addCleanup(patcher.stop)

    def call_kw(self, model, method, args, kwargs=None, context=None):
        params = {'model': model, 'method': method, 'args': args, 'kwargs': dict(kwargs or {})}
        if context:
            params['kwargs']['context'] = context
        return self.make_jsonrpc_request('/web/dataset/call_kw/%s/%s' % (model, method), params)

    def refused(self, *args, **kwargs):
        with mute_logger('odoo.http'), self.assertRaises(JsonRpcException):
            self.call_kw(*args, **kwargs)

    def lose_access_silently(self, user):
        """`user` stops following the task without any trigger (as a change of a
        record rule or of a commercial partner would): only the sweep sees it."""
        self.env.flush_all()
        self.env.cr.execute("DELETE FROM mail_followers WHERE res_model = 'project.task' AND res_id = %s "
                            "AND partner_id = %s", (self.task.id, user.partner_id.id))
        self.env.invalidate_all()
        self.assertFalse(self.task._woow_project_ai_user_allowed(user, 'customer'))

    def http_post(self, channel, body, context=None, refused=False):
        params = {'thread_model': 'discuss.channel', 'thread_id': channel.id,
                  'post_data': {'body': body, 'message_type': 'comment', 'subtype_xmlid': 'mail.mt_comment'}}
        if context:
            params['context'] = context
        if refused:
            with mute_logger('odoo.http'), self.assertRaises(JsonRpcException):
                self.make_jsonrpc_request('/mail/message/post', params)
            return None
        return self.make_jsonrpc_request('/mail/message/post', params)

    def find(self, channel, body):
        return self.env['mail.message'].sudo().search(
            [('model', '=', 'discuss.channel'), ('res_id', '=', channel.id), ('body', 'ilike', body)])

    def chat_url(self, page):
        """The open link of the portal task page (with its CSRF token)."""
        return re.search(r'data-src="(/my/tasks/\d+/woow_project_ai\?[^"]+)"', page.text).group(1).replace('&amp;', '&')

    # --- nobody joins by another route ---

    def test_portal_user_without_access_refused_everywhere(self):
        channel = self.open_tab(self.customer, 'customer')
        self.post(channel, self.customer, 'my question')
        self.assertFalse(self.task._woow_project_ai_user_allowed(self.outsider, 'customer'))
        self.authenticate('aitask_outsider', 'aitask_outsider')
        # the portal task page: no task, no section
        page = self.url_open('/my/tasks/%s' % self.task.id)
        self.assertNotIn('o_woow_project_ai_portal', page.text)
        self.assertEqual(self.url_open('/my/tasks/%s/woow_project_ai' % self.task.id, allow_redirects=False).status_code,
                         404)
        # Odoo's Discuss page and its JSON routes
        self.assertEqual(self.url_open('/discuss/channel/%s' % channel.id).status_code, 404)
        with mute_logger('odoo.http'), self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request('/discuss/channel/messages', {'channel_id': channel.id})
        with mute_logger('odoo.http'), self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request('/discuss/channel/join', {'channel_id': channel.id})
        # the invitation link (whoever leaked it)
        invitation = self.url_open('/chat/%s/%s' % (channel.id, channel.sudo().uuid))
        self.assertEqual(invitation.status_code, 404)
        self.assertNotIn(self.outsider.partner_id, self.members(channel))
        # a member who tries to add them: add_members, a direct member create, a channel write
        self.authenticate('aitask_customer', 'aitask_customer')
        self.refused('discuss.channel', 'add_members', [[channel.id]], {'partner_ids': self.outsider.partner_id.ids})
        self.open_tab(self.staff, 'customer')
        self.authenticate('aitask_staff', 'aitask_staff')
        for context in (None, {'woow_project_ai_sync': True}):  # the sync flag cannot be forged
            self.refused('discuss.channel', 'add_members', [[channel.id]],
                         {'partner_ids': self.outsider.partner_id.ids}, context=context)
            self.refused('discuss.channel.member', 'create',
                         [{'channel_id': channel.id, 'partner_id': self.outsider.partner_id.id}], context=context)
        self.refused('discuss.channel', 'write', [[channel.id], {
            'channel_member_ids': [odoo.Command.create({'partner_id': self.outsider.partner_id.id})]}])
        self.refused('discuss.channel', 'channel_join', [[channel.id]])
        self.assertNotIn(self.outsider.partner_id, self.members(channel))
        # nor is anybody with access added by those routes: only by opening the tab
        self.assertTrue(self.task._woow_project_ai_user_allowed(self.staff2, 'customer'))
        self.refused('discuss.channel', 'add_members', [[channel.id]], {'partner_ids': self.staff2.partner_id.ids})
        self.authenticate('aitask_staff2', 'aitask_staff2')
        self.assertEqual(self.url_open('/chat/%s/%s' % (channel.id, channel.sudo().uuid)).status_code, 404)
        self.assertNotIn(self.staff2.partner_id, self.members(channel))

    def test_internal_tab_only_for_employees_with_access(self):
        self.project.privacy_visibility = 'followers'
        self.assertFalse(self.task._woow_project_ai_user_allowed(self.employee, 'internal'))
        with self.assertRaises(AccessError):
            self.open_tab(self.employee, 'internal')
        with self.assertRaises(AccessError):
            self.open_tab(self.staff2, 'internal')  # project user, but not following this private project
        # a customer who can read the task never opens the internal tab
        # (back to portal: Odoo unsubscribed the portal followers when the project became private)
        self.project.privacy_visibility = 'portal'
        self.task.message_subscribe(partner_ids=self.customer.partner_id.ids)
        self.assertTrue(self.task.with_user(self.customer).has_access('read'))
        with self.assertRaises(AccessError):
            self.open_tab(self.customer, 'internal')
        internal = self.open_tab(self.staff, 'internal')
        self.assertNotIn(self.customer.partner_id, self.members(internal))

    # --- access loss ---

    def test_access_loss_removes_members(self):
        customer_chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.customer2, 'customer')
        internal = self.open_tab(self.staff, 'internal')
        self.open_tab(self.staff2, 'internal')
        # a follower removed
        self.task.message_unsubscribe(partner_ids=self.customer.partner_id.ids)
        self.assertNotIn(self.customer.partner_id, self.members(customer_chat))
        self.assertIn(self.customer2.partner_id, self.members(customer_chat))
        # an archived (or revoked) portal user
        wizard = self.env['portal.wizard'].create({'partner_ids': self.customer2.partner_id.ids})
        wizard.user_ids.filtered(lambda w: w.partner_id == self.customer2.partner_id).action_revoke_access()
        self.assertNotIn(self.customer2.partner_id, self.members(customer_chat))
        # the project privacy: only invited employees (the internal chat: those who can read the project too)
        self.open_tab(self.staff2, 'customer')
        self.project.message_subscribe(partner_ids=self.staff.partner_id.ids)
        self.project.privacy_visibility = 'followers'
        self.assertNotIn(self.staff2.partner_id, self.members(internal))
        self.assertNotIn(self.staff2.partner_id, self.members(customer_chat))
        self.assertIn(self.staff.partner_id, self.members(internal), 'the assignee following the project stays')
        # an archived employee
        self.staff.active = False
        self.assertNotIn(self.staff.partner_id, self.members(internal))
        self.assertEqual(self.members(internal), self.internal_ai.partner_id, 'the bot stays')

    def test_groups_and_project_changes(self):
        internal = self.open_tab(self.staff2, 'internal')
        customer_chat = self.open_tab(self.customer, 'customer')
        # an employee who becomes a portal user leaves the internal chat
        self.staff2.write({'groups_id': [odoo.Command.set([self.env.ref('base.group_portal').id])]})
        self.assertNotIn(self.staff2.partner_id, self.members(internal))
        # the task moved to a project the customer cannot see
        other = self.env['project.project'].create({
            'name': 'Internal Only', 'privacy_visibility': 'employees',
            'woow_project_ai_internal_assistant_id': self.internal_ai.id,
            'woow_project_ai_customer_assistant_id': self.customer_ai.id})
        self.task.project_id = other
        self.assertNotIn(self.customer.partner_id, self.members(customer_chat))

    def test_post_before_the_sync_is_refused(self):
        channel = self.open_tab(self.customer, 'customer')
        self.lose_access_silently(self.customer)
        self.assertIn(self.customer.partner_id, self.members(channel), 'no trigger saw it')
        # (no assertRaises: Odoo's wraps the call in a savepoint, which would also undo
        # the removal the check commits in its own transaction)
        try:
            self.post(channel, self.customer, 'still here?')
        except AccessError as error:
            refusal = error
        else:
            refusal = None
        self.assertIsInstance(refusal, AccessError)
        self.assertNotIn(self.customer.partner_id, self.members(channel), 'removed at once')
        self.assertFalse(self.jobs(channel))
        # over HTTP too (a member who was never synced)
        channel2 = self.open_tab(self.customer2, 'customer')
        self.env.flush_all()
        self.env.cr.execute("DELETE FROM mail_followers WHERE res_model = 'project.task' AND res_id = %s "
                            "AND partner_id = %s", (self.task.id, self.customer2.partner_id.id))
        self.env.invalidate_all()
        self.authenticate('aitask_customer2', 'aitask_customer2')
        with mute_logger('odoo.http'), self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request('/mail/message/post', {
                'thread_model': 'discuss.channel', 'thread_id': channel2.id,
                'post_data': {'body': 'hello?', 'message_type': 'comment', 'subtype_xmlid': 'mail.mt_comment'}})
        self.assertFalse(self.env['mail.message'].sudo().search(
            [('model', '=', 'discuss.channel'), ('res_id', '=', channel2.id), ('body', 'ilike', 'hello?')]))

    def test_reading_is_limited_before_the_sync(self):
        channel = self.open_tab(self.customer, 'customer')
        self.post(channel, self.customer, 'first question')
        message = self.env['mail.message'].sudo().search(
            [('model', '=', 'discuss.channel'), ('res_id', '=', channel.id)], order='id desc', limit=1)
        self.open_tab(self.staff, 'customer')
        self.post(channel, self.staff, 'answer from a colleague')
        staff_message = self.env['mail.message'].sudo().search(
            [('model', '=', 'discuss.channel'), ('res_id', '=', channel.id)], order='id desc', limit=1)
        self.lose_access_silently(self.customer)
        # still listed until the sync...
        self.assertIn(self.customer.partner_id, self.members(channel))
        # ...but nothing to read: not the channel, not the colleague's message
        self.assertFalse(self.env['discuss.channel'].with_user(self.customer).search([('id', '=', channel.id)]))
        with self.assertRaises(AccessError):
            channel.with_user(self.customer).read(['name'])
        with self.assertRaises(AccessError):
            staff_message.with_user(self.customer).read(['body'])
        # their own message neither (Odoo lets an author read it; the task chat follows the task access)
        with self.assertRaises(AccessError):
            message.with_user(self.customer).read(['author_id'])
        self.authenticate('aitask_customer', 'aitask_customer')
        self.assertEqual(self.url_open('/discuss/channel/%s' % channel.id).status_code, 404)
        with mute_logger('odoo.http'), self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request('/discuss/channel/messages', {'channel_id': channel.id})
        data = self.make_jsonrpc_request('/mail/data', {'channels_as_member': True})
        self.assertNotIn(channel.id, [c['id'] for c in data.get('discuss.channel', [])])
        self.assertEqual(self.url_open('/my/tasks/%s/woow_project_ai' % self.task.id, allow_redirects=False).status_code,
                         404)
        # the hourly sweep removes the member (in batches)
        internal = self.open_tab(self.staff, 'internal')
        self.assertEqual(self.env['discuss.channel']._woow_project_ai_cron_sync(batch_size=1), 2)
        self.assertNotIn(self.customer.partner_id, self.members(channel))
        self.assertIn(self.staff.partner_id, self.members(channel))
        self.assertIn(self.staff.partner_id, self.members(internal))

    def test_reading_rule_is_evaluated_per_user(self):
        # Odoo checks a record's rules on cached values (filtered_domain): the
        # value computed for one user must never be reused for another one
        channel = self.open_tab(self.customer, 'customer')
        self.open_tab(self.staff, 'customer')
        self.lose_access_silently(self.customer)
        channel.with_user(self.staff).check_access('read')
        # no assertRaises: Odoo's clears the record cache first (so that cached values do not hide
        # access errors), which would hide exactly this case
        try:
            channel.with_user(self.customer).check_access('read')
        except AccessError:
            refused = True
        else:
            refused = False
        self.assertTrue(refused, 'the value computed for the colleague is not reused for the customer')
        self.assertTrue(channel.with_user(self.staff).has_access('read'))

    def test_members_may_leave(self):
        channel = self.open_tab(self.customer, 'customer')
        channel.with_user(self.customer).sudo().action_unfollow()
        self.assertNotIn(self.customer.partner_id, self.members(channel))
        self.assertEqual(self.open_tab(self.customer, 'customer'), channel, 'back through the tab')

    # --- nobody writes under another name, whatever the client sends ---

    def test_rpc_posts_only_under_ones_own_name(self):
        customer_chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.staff, 'customer')
        bot = self.customer_ai.partner_id
        # a JSON-RPC message_post (not superuser): only under the user's own name, whatever the type
        self.authenticate('aitask_customer', 'aitask_customer')
        self.refused('discuss.channel', 'message_post', [[customer_chat.id]], {
            'body': 'FAKE AI: your refund is approved', 'message_type': 'comment', 'author_id': bot.id,
            'subtype_xmlid': 'mail.mt_comment'})
        self.refused('discuss.channel', 'message_post', [[customer_chat.id]], {
            'body': 'FAKE STAFF: free installation', 'message_type': 'comment', 'email_from': self.staff.email,
            'subtype_xmlid': 'mail.mt_comment'})
        self.refused('discuss.channel', 'message_post', [[customer_chat.id]], {
            'body': 'FAKE NOTICE', 'message_type': 'notification', 'author_id': self.staff.partner_id.id,
            'subtype_xmlid': 'mail.mt_comment'})
        self.assertFalse(self.find(customer_chat, 'FAKE'))
        # a member's own JSON-RPC post and Odoo's pin notice still work
        message_id = self.call_kw('discuss.channel', 'message_post', [[customer_chat.id]], {
            'body': 'posted by RPC', 'message_type': 'comment', 'subtype_xmlid': 'mail.mt_comment'})
        own_message = self.env['mail.message'].browse(message_id)
        self.assertEqual(own_message.author_id, self.customer.partner_id)
        self.call_kw('discuss.channel', 'set_message_pin', [[customer_chat.id]],
                     {'message_id': own_message.id, 'pinned': True})
        own_message.invalidate_recordset(['pinned_at'])
        self.assertTrue(own_message.pinned_at)
        self.assertTrue(self.env['mail.message'].sudo().search([
            ('model', '=', 'discuss.channel'), ('res_id', '=', customer_chat.id),
            ('message_type', '=', 'notification')]), "Odoo's pin notice")
        # a notice of one's own (the post route takes a message type): refused, it would pass for Odoo's
        with mute_logger('odoo.http'), self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request('/mail/message/post', {
                'thread_model': 'discuss.channel', 'thread_id': customer_chat.id,
                'post_data': {'body': 'FAKE: the AI is switched off', 'message_type': 'notification',
                              'subtype_xmlid': 'mail.mt_comment'}})
        self.assertFalse(self.find(customer_chat, 'FAKE'))

    def test_messages_are_not_created_or_changed_directly(self):
        customer_chat = self.open_tab(self.customer, 'customer')
        bot = self.customer_ai.partner_id
        self.authenticate('aitask_customer', 'aitask_customer')
        # Odoo lets whoever reads a channel create a message in it with any author...
        comment = self.env.ref('mail.mt_comment').id
        self.refused('mail.message', 'create', [{
            'model': 'discuss.channel', 'res_id': customer_chat.id, 'body': 'FAKE AI', 'message_type': 'comment',
            'author_id': bot.id, 'subtype_id': comment}])
        self.refused('mail.message', 'create', [{
            'model': 'discuss.channel', 'res_id': str(customer_chat.id), 'body': 'FAKE AI 3', 'message_type': 'comment',
            'author_id': bot.id, 'subtype_id': comment}])
        self.refused('mail.message', 'create', [{
            'body': 'FAKE AI 4', 'message_type': 'comment', 'author_id': bot.id, 'subtype_id': comment}],
            context={'default_model': 'discuss.channel', 'default_res_id': customer_chat.id})
        guest = self.env['mail.guest'].create({'name': 'Staff Sam'})
        self.refused('mail.message', 'create', [{
            'model': 'discuss.channel', 'res_id': customer_chat.id, 'body': 'FAKE GUEST', 'message_type': 'comment',
            'subtype_id': comment, 'author_guest_id': guest.id}])
        self.assertFalse(self.find(customer_chat, 'FAKE'))
        # not even under their own name: a task chat message is created by the chat's own post only (team
        # review 1 / 25: a direct create could also carry a parent or files of other messages)
        self.refused('mail.message', 'create', [{
            'model': 'discuss.channel', 'res_id': customer_chat.id, 'body': 'NO AUTHOR GIVEN', 'message_type': 'comment',
            'subtype_id': comment}])
        self.assertFalse(self.find(customer_chat, 'NO AUTHOR GIVEN'))
        # ...and the author change their message (its author included): not in a task chat
        self.http_post(customer_chat, 'my real question')
        own_message = self.find(customer_chat, 'my real question')
        self.assertEqual(own_message.author_id, self.customer.partner_id)
        self.refused('mail.message', 'write', [own_message.ids, {'author_id': bot.id}])
        self.refused('mail.message', 'write', [own_message.ids, {'body': '<p>edited behind the chat</p>'}])
        own_message.invalidate_recordset()
        self.assertEqual(own_message.author_id, self.customer.partner_id)
        self.assertIn('my real question', own_message.body)
        # an edit in the chat that mentions someone outside it: not made a recipient (who could read it)
        self.make_jsonrpc_request('/mail/message/update_content', {
            'message_id': own_message.id, 'body': 'my real question, @Otto', 'attachment_ids': [],
            'partner_ids': self.outsider.partner_id.ids})
        own_message.invalidate_recordset()
        self.assertIn('@Otto', own_message.body)
        self.assertNotIn(self.outsider.partner_id, own_message.partner_ids)
        # members who may write the channel cannot delete its messages directly either
        internal = self.open_tab(self.staff, 'internal')
        answer = self.customer_ai.sudo()._post_text(customer_chat, 'an AI answer')
        self.internal_ai.sudo()._post_text(internal, 'an internal AI answer')
        ai_message = self.find(internal, 'an internal AI answer')
        self.authenticate('aitask_staff', 'aitask_staff')
        self.refused('mail.message', 'unlink', [ai_message.ids])
        self.assertTrue(ai_message.exists() and answer.exists())

    def test_bots_of_the_context_cannot_be_forged(self):
        # the routes post as superuser with the client's context (JSON): a list of "bots" in it changes nothing
        customer_chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.customer2, 'customer')
        self.open_tab(self.staff, 'customer')
        internal = self.open_tab(self.staff, 'internal')
        self.authenticate('aitask_staff', 'aitask_staff')
        forged = {'woow_project_ai_bots': [self.staff.partner_id.id, self.customer2.partner_id.id]}
        # a colleague stays a colleague (the AI pauses)...
        self.http_post(customer_chat, 'a colleague writes', context=forged)
        self.assertTrue(customer_chat.sudo().woow_project_ai_paused_until, 'a colleague pauses the AI')
        # ...nobody is removed as a former bot by a sync (an unsubscription syncs the task's chats)...
        self.call_kw('project.task', 'message_unsubscribe', [self.task.ids],
                     {'partner_ids': self.staff2.partner_id.ids}, context=forged)
        self.assertIn(self.customer2.partner_id, self.members(customer_chat))
        # ...and an archived task stays read only
        self.task.active = False
        self.http_post(internal, 'archived?', context=forged, refused=True)
        self.assertFalse(self.find(internal, 'archived'))

    def test_comments_only(self):
        # no message type but comments from people (an incoming email's sender can be forged)
        internal = self.open_tab(self.staff, 'internal')
        self.authenticate('aitask_staff', 'aitask_staff')
        for message_type in ('email', 'email_outgoing', 'auto_comment'):
            self.refused('discuss.channel', 'message_post', [[internal.id]], {
                'body': 'typed %s' % message_type, 'message_type': message_type,
                'subtype_xmlid': 'mail.mt_comment'})
        self.assertFalse(self.find(internal, 'typed'))
        # nor by the mail gateway (superuser), even with the AI's or a colleague's address as sender
        with self.assertRaises(AccessError):
            internal.sudo().message_post(body='forged email', message_type='email',
                                         author_id=self.internal_ai.partner_id.id)
        self.assertFalse(self.find(internal, 'forged email'))

    def test_administrators_join_only_through_the_tab(self):
        admin = new_test_user(self.env, 'aitask_admin', name='Admin Ada', email='ada@example.com',
                              groups='base.group_user,base.group_system')
        channel = self.open_tab(self.customer, 'customer')
        self.assertTrue(self.task._woow_project_ai_user_allowed(admin, 'customer'), 'an administrator reads the task')
        self.authenticate('aitask_admin', 'aitask_admin')
        # administrators read every group channel: the join routes reach the member creation
        with mute_logger('odoo.http'), self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request('/discuss/channel/join', {'channel_id': channel.id})
        with mute_logger('odoo.http'), self.assertRaises(JsonRpcException):
            self.make_jsonrpc_request('/discuss/settings/mute', {'minutes': -1, 'channel_id': channel.id})
        self.http_post(channel, 'admin without opening the tab', refused=True)
        self.assertNotIn(admin.partner_id, self.members(channel))
        self.assertFalse(self.find(channel, 'admin without opening the tab'))
        # through the tab: a muted member
        self.open_tab(admin, 'customer')
        self.assertIn(admin.partner_id, self.members(channel))

    def test_outsider_and_archived_posts_over_http(self):
        channel = self.open_tab(self.customer, 'customer')
        internal = self.open_tab(self.staff, 'internal')
        self.authenticate('aitask_outsider', 'aitask_outsider')
        self.http_post(channel, 'outsider here', refused=True)
        self.authenticate('aitask_staff', 'aitask_staff')
        self.http_post(internal, 'before archiving')
        # employees still read an archived task (and its chats), but write in neither
        self.task.active = False
        self.assertTrue(internal.with_user(self.staff).has_access('read'))
        self.http_post(internal, 'after archiving', refused=True)
        self.assertTrue(self.find(internal, 'before archiving'))
        self.assertFalse(self.find(channel, 'outsider here') | self.find(internal, 'after archiving'))

    def test_notifications_only_reach_people_with_access(self):
        customer_chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.customer2, 'customer')
        internal = self.open_tab(self.staff, 'internal')
        Channel = type(self.env['discuss.channel'])
        web_push = Channel._notify_thread_by_web_push
        pushed = []

        def capture(channel, message, recipients_data, msg_vals=False, **kwargs):
            pushed.extend(recipient['id'] for recipient in recipients_data)
            return web_push(channel, message, recipients_data, msg_vals=msg_vals, **kwargs)

        for user in (self.customer, self.customer2):
            # Odoo pushes only to users with Discuss settings (made when they first open the web client)
            self.env['res.users.settings']._find_or_create_for_user(user)
        self.lose_access_silently(self.customer2)
        # still listed (no trigger saw it), but left out of the notifications of any message
        self.assertIn(self.customer2.partner_id, self.members(customer_chat), 'still listed')
        welcome = self.bot_messages(customer_chat)[:1]
        recipients = [recipient['id'] for recipient in customer_chat.sudo()._notify_get_recipients(welcome, {})]
        self.assertIn(self.customer.partner_id.id, recipients)
        self.assertNotIn(self.customer2.partner_id.id, recipients)
        with patch.object(Channel, '_notify_thread_by_web_push', autospec=True, side_effect=capture):
            # the AI's answer first removes the member who lost the access (independent review F5): not pushed
            self.customer_ai._post_text(customer_chat, 'AI answer')
        self.assertIn(self.customer.partner_id.id, pushed)
        self.assertNotIn(self.customer2.partner_id.id, pushed)
        self.assertNotIn(self.customer2.partner_id, self.members(customer_chat))
        # a person's post does the same (here a colleague's)
        self.open_tab(self.staff, 'customer')
        self.lose_access_silently(self.customer)
        self.assertIn(self.customer.partner_id, self.members(customer_chat), 'still listed')
        self.post(customer_chat, self.staff, 'next answer')
        self.assertNotIn(self.customer.partner_id, self.members(customer_chat))
        # a customer mentioned in the internal chat is not notified (no email with internal words)
        message = internal.with_user(self.staff).sudo().message_post(
            body='internal: @Customer Carl margin 30%', message_type='comment', subtype_xmlid='mail.mt_comment',
            partner_ids=self.customer.partner_id.ids)
        self.assertNotIn(self.customer.partner_id, message.sudo().partner_ids, 'nor can they read it')
        self.assertNotIn(self.customer.partner_id, message.sudo().notification_ids.res_partner_id)
        self.assertFalse(self.env['mail.mail'].sudo().search([('mail_message_id', '=', message.id)]))

    # --- the portal task page ---

    def test_portal_task_page(self):
        self.authenticate('aitask_customer', 'aitask_customer')
        page = self.url_open('/my/tasks/%s' % self.task.id)
        self.assertEqual(page.status_code, 200)
        self.assertIn('o_woow_project_ai_portal', page.text)
        self.assertIn('data-src="/my/tasks/%s/woow_project_ai?token=' % self.task.id, page.text)
        self.assertFalse(self.task.woow_project_ai_customer_channel_id, 'not created by viewing the page')
        # another site cannot make the customer join (a GET without the page's token): back to the task page
        csrf = odoo.http.Request.csrf_token(self)  # the session's CSRF token is not the open token either
        for forged in ('', '?token=0123456789abcdef', '?token=%s' % csrf, '?csrf_token=%s' % csrf):
            # the embedded frame: a short page that reloads the task page itself (team review 18)
            response = self.url_open('/my/tasks/%s/woow_project_ai%s' % (self.task.id, forged), allow_redirects=False)
            self.assertEqual(response.status_code, 200)
            self.assertIn('href="/my/tasks/%s#woow_project_ai_chat"' % self.task.id, response.text)
            self.assertIn('target="_top"', response.text)
            # the full page: back to the task page
            joiner = '&' if forged else '?'
            response = self.url_open('/my/tasks/%s/woow_project_ai%s%sfull=1' % (self.task.id, forged, joiner),
                                     allow_redirects=False)
            self.assertIn(response.status_code, (302, 303))
            self.assertIn('/my/tasks/%s#woow_project_ai_chat' % self.task.id, response.headers['Location'])
            self.assertFalse(self.task.woow_project_ai_customer_channel_id)
        response = self.url_open(self.chat_url(page), allow_redirects=False)
        self.assertIn(response.status_code, (302, 303))
        channel = self.task.woow_project_ai_customer_channel_id
        self.assertTrue(channel)
        self.assertTrue(response.headers['Location'].endswith('/discuss/channel/%s' % channel.id))
        self.assertIn(self.customer.partner_id, self.members(channel))
        self.assertEqual(self.url_open('/discuss/channel/%s' % channel.id).status_code, 200)
        self.assertNotIn(' src="/my/tasks/', page.text, 'loaded on demand')
        # never with the page, even for a member (its composer would scroll the page): the section knows the
        # member and loads at once only when the page is opened at its anchor (review 35)
        again = self.url_open('/my/tasks/%s' % self.task.id).text
        self.assertNotIn(' src="/my/tasks/', again)
        self.assertIn('data-member="1"', again)
        self.assertIn('data-member="0"', page.text)
        # the full page, back to the task (portal back bar of woow_odoo_portal_discuss when installed)
        url = self.chat_url(page)
        full = self.url_open('%s&full=1&back=/my/tasks/%s' % (url, self.task.id), allow_redirects=False)
        self.assertIn('discussions=1&back=/my/tasks/%s' % self.task.id, full.headers['Location'])
        evil = self.url_open('%s&full=1&back=//evil.example.com' % url, allow_redirects=False)
        self.assertNotIn('evil', evil.headers['Location'])
        # the project-scoped task route shows it too
        self.project.message_subscribe(partner_ids=self.customer.partner_id.ids)
        page = self.url_open('/my/projects/%s/task/%s' % (self.project.id, self.task.id))
        self.assertIn('o_woow_project_ai_portal', page.text)
        # task chats are not listed among the portal discussions (they have their own page): test_coexistence
        # a project without customer assistant: no section
        self.project.woow_project_ai_customer_assistant_id = False
        self.assertNotIn('o_woow_project_ai_portal', self.url_open('/my/tasks/%s' % self.task.id).text)

    def test_portal_post_and_upload(self):
        channel = self.open_tab(self.customer, 'customer')
        self.authenticate('aitask_customer', 'aitask_customer')
        with mute_logger('odoo.http'):
            response = self.url_open('/mail/attachment/upload', data={
                'csrf_token': odoo.http.Request.csrf_token(self), 'is_pending': True,
                'thread_id': channel.id, 'thread_model': 'discuss.channel'},
                files={'ufile': ('報價.pdf', TINY_PDF, 'application/pdf')})
        self.assertEqual(response.status_code, 200, response.text[:300])
        attachment = json.loads(response.content)['data']['ir.attachment'][0]
        self.make_jsonrpc_request('/mail/message/post', {
            'thread_model': 'discuss.channel', 'thread_id': channel.id,
            'post_data': {'body': '請看報價', 'attachment_ids': [attachment['id']], 'message_type': 'comment',
                          'subtype_xmlid': 'mail.mt_comment'},
            'attachment_tokens': [attachment['access_token']]})
        job = self.jobs(channel)
        self.assertEqual(len(job), 1)
        self.assertEqual(job.message_id.attachment_ids.name, '報價.pdf')
        # the connection's document mode applies: "file" sends the PDF itself
        with patch(BACKEND_POST, return_value=openai_ok('收到')) as post:
            self.run_job(job)
        self.assertEqual(job.state, 'done', job.error)
        user_turn = self.sent(post)['messages'][-1]['content']
        self.assertIn('file', [part.get('type') for part in user_turn])
        # "link": a signed download link instead
        self.customer_ai.connection_id.document_mode = 'link'
        self.post(channel, self.customer, '再看一次', attachments=self.pending_attachment(self.customer))
        with patch(BACKEND_POST, return_value=openai_ok('好')) as post:
            self.run_job(self.jobs(channel)[-1])
        text = json.dumps(self.sent(post)['messages'][-1], ensure_ascii=False)
        self.assertIn('/woow_project_ai/attachment/', text)
        self.assertNotIn('"type": "file"', text)
