# -*- coding: utf-8 -*-
"""Coexistence on one database with the Discuss AI modules (woow_ai_contact
2.x, woow_ai_contact_portal_discuss with woow_odoo_portal_discuss) and Live
Chat AI (im_livechat_ai). Each class runs only when those modules are
installed (the sandbox's coexistence database); elsewhere it is skipped.

- task chats never appear in /my/discussions;
- the global task-chat rule is a strict no-op for every other channel, for
  internal, portal, public and guest users, a Live Chat visitor session
  included;
- no double replies: a task chat is answered by project AI only, a Discuss AI
  direct chat by woow_ai_contact only;
- nothing is shared: models, crons, routes and push keys differ."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError
from odoo.modules.registry import Registry
from odoo.tests.common import HttpCase, get_db_name, new_test_user, tagged

from odoo.addons.woow_ai_task.tools import media

from .common import BACKEND_POST, TEST_KEY, AiTaskFixtures, openai_ok

try:  # woow_document_management (woow_prod): on the addons path of the coexistence database only
    from odoo.addons.woow_document_management.tests import test_reg_notification_label as document_labels
except ImportError:
    document_labels = None

RULE = 'woow_ai_task.rule_discuss_channel_woow_project_ai'


def installed(env, *modules):
    return all(module in env.registry._init_modules for module in modules)


@tagged('post_install', '-at_install', 'woow_ai_task', 'woow_ai_task_coexistence')
class TestCoexistence(AiTaskFixtures, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_ai_task()

    def setUp(self):
        super().setUp()
        for patcher in (patch.object(type(self.env['woow.project.ai.job']), '_start_after_commit'),):
            patcher.start()
            self.addCleanup(patcher.stop)
        if 'woow.ai.job' in self.env:
            patcher = patch.object(type(self.env['woow.ai.job']), '_start_after_commit')
            patcher.start()
            self.addCleanup(patcher.stop)

    # --- helpers ---

    def discuss_ai_assistant(self):
        """A Discuss AI (woow_ai_contact) assistant for every employee and portal user, direct chats on."""
        connection = self.env['woow.ai.connection'].create({
            'name': 'Discuss LiteLLM', 'backend_type': 'openai', 'endpoint_url': 'https://discuss-llm.example.com/v1',
            'api_key': TEST_KEY, 'openai_stream': False})
        return self.env['woow.ai.contact'].create({
            'name': 'Discuss Advisor', 'connection_id': connection.id,
            'allowed_group_ids': [Command.set([self.env.ref('base.group_user').id,
                                               self.env.ref('base.group_portal').id])]})

    def visible_channels(self, env):
        """Every channel `env`'s user may read, task chats left out (sudo listing, then the user's search and
        read access check), or 'no access right' when Odoo's ACL refuses the model itself."""
        others = self.env['discuss.channel'].sudo().with_context(active_test=False).search(
            [('woow_project_ai_task_id', '=', False)])
        try:
            found = env['discuss.channel'].with_context(active_test=False).search([('id', 'in', others.ids)])
        except AccessError:
            return 'no access right'
        readable = set()
        for channel in others:
            try:
                env['discuss.channel'].browse(channel.id).check_access('read')
            except AccessError:
                continue
            readable.add(channel.id)
        return set(found.ids), readable

    def livechat_session(self):
        """A Live Chat visitor session over HTTP (a guest, not logged in): returns the channel."""
        operator = new_test_user(self.env, 'aitask_operator', groups='base.group_user,im_livechat.im_livechat_group_user',
                                 name='Operator Olga')
        self.env['bus.presence'].create({'user_id': operator.id, 'status': 'online'})
        livechat = self.env['im_livechat.channel'].create({'name': 'Site chat', 'user_ids': [operator.id]})
        self.authenticate(None, None)
        data = self.make_jsonrpc_request('/im_livechat/get_session', {
            'anonymous_name': 'Visitor', 'channel_id': livechat.id, 'persisted': True})
        return self.env['discuss.channel'].browse(data['discuss.channel'][0]['id']), operator

    # --- /my/discussions ---

    def test_task_chats_never_in_portal_discussions(self):
        if not installed(self.env, 'woow_odoo_portal_discuss'):
            self.skipTest('woow_odoo_portal_discuss is not installed')
        # the bridge is what holds in any load order (delta review 10): installed with both sides
        self.assertTrue(installed(self.env, 'woow_ai_task_portal_discuss'), 'the bridge must be installed')
        customer_chat = self.open_tab(self.customer, 'customer')
        self.post(customer_chat, self.customer, 'a question in the task chat')
        # a plain group chat of the customer (with a colleague) is listed: the filter hides task chats only
        group = self.env['discuss.channel'].with_user(self.staff).create({'name': 'Kitchen planning group',
                                                                         'channel_type': 'group'})
        group.with_user(self.staff).add_members(partner_ids=self.customer.partner_id.ids)
        group.with_user(self.staff).message_post(body='hello customer', message_type='comment')
        self.authenticate('aitask_customer', 'aitask_customer')
        for url in ('/my/discussions', '/my/discussions?filterby=group', '/my/discussions?filterby=all'):
            with self.subTest(url=url):
                page = self.url_open(url)
                self.assertEqual(page.status_code, 200)
                self.assertNotIn('Install cabinets | Customer AI', page.text)
                self.assertNotIn('/discuss/channel/%s?' % customer_chat.id, page.text)
        self.assertIn('Kitchen planning group', self.url_open('/my/discussions').text)
        # the home card count leaves them out too
        self.assertIn('Kitchen planning group', self.url_open('/my/discussions?filterby=group').text)
        counters = self.make_jsonrpc_request('/my/counters', {'counters': ['portal_discussion_count']})
        self.assertEqual(counters['portal_discussion_count'], 1)
        if installed(self.env, 'woow_ai_contact_portal_discuss'):
            self.assertEqual(self.url_open('/my/discussions?filterby=ai').status_code, 200)

    def test_notification_centre_counts_what_the_list_shows(self):
        # team review 3 / 38: the centre's "unread conversations" (the /my card and the /my/notifications
        # notice, both linking to /my/discussions) leaves out the task chats the list leaves out
        if not installed(self.env, 'woow_portal_notification_discuss'):
            self.skipTest('woow_portal_notification_discuss is not installed')
        self.assertTrue(installed(self.env, 'woow_ai_task_portal_notification'), 'the bridge must be installed')
        customer_chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.staff, 'customer')
        self.post(customer_chat, self.staff, 'a colleague answers in the task chat')
        group = self.env['discuss.channel'].with_user(self.staff).create({'name': 'Kitchen planning group',
                                                                         'channel_type': 'group'})
        group.with_user(self.staff).add_members(partner_ids=self.customer.partner_id.ids)
        group.with_user(self.staff).message_post(body='hello customer', message_type='comment',
                                                 subtype_xmlid='mail.mt_comment')
        member = customer_chat.sudo().channel_member_ids.filtered(lambda m: m.partner_id == self.customer.partner_id)
        self.assertGreater(member.message_unread_counter, 0, 'the task chat has unread messages')
        self.authenticate('aitask_customer', 'aitask_customer')
        counters = self.make_jsonrpc_request('/my/counters', {
            'counters': ['wpn_unread_conversation_count', 'portal_discussion_count']})
        self.assertEqual(counters['portal_discussion_count'], 1)
        self.assertEqual(counters['wpn_unread_conversation_count'], 1, 'only the group chat the list shows')
        self.assertNotIn('Install cabinets', self.url_open('/my/discussions').text)

    def test_task_chat_unread_in_the_notification_centre(self):
        # delta review 8: the customer's unread task chat messages have their own notice and /my card, which
        # open the task page (one task) or the customer's tasks, never /my/discussions
        if not installed(self.env, 'woow_portal_notification_discuss'):
            self.skipTest('woow_portal_notification_discuss is not installed')
        self.assertTrue(installed(self.env, 'woow_ai_task_portal_notification'), 'the bridge must be installed')
        chat = self.open_tab(self.customer, 'customer')
        self.open_tab(self.staff, 'customer')
        self.post(chat, self.staff, 'a colleague answers later')
        member = chat.sudo().channel_member_ids.filtered(lambda m: m.partner_id == self.customer.partner_id)
        unread = member.message_unread_counter
        self.assertGreater(unread, 0)
        task_url = '/my/tasks/%s#woow_project_ai_chat' % self.task.id
        self.authenticate('aitask_customer', 'aitask_customer')
        counters = self.make_jsonrpc_request('/my/counters', {
            'counters': ['woow_project_ai_unread_chat_count', 'wpn_unread_conversation_count']})
        self.assertEqual(counters['woow_project_ai_unread_chat_count'], unread)
        self.assertEqual(counters['wpn_unread_conversation_count'], 0, 'not in the Discussions count')
        page = self.url_open('/my/notifications')
        self.assertEqual(page.status_code, 200)
        self.assertIn('%s unread messages in your task AI chats' % unread, page.text)
        self.assertIn(task_url, page.text)
        self.assertIn('/my/woow_project_ai/unread', self.url_open('/my').text, 'the /my card')
        opened = self.url_open('/my/woow_project_ai/unread', allow_redirects=False)
        self.assertIn(opened.status_code, (301, 302, 303))
        self.assertTrue(opened.headers['Location'].endswith(task_url))
        # unread in two tasks: the customer's tasks
        task2 = self.env['project.task'].create({'name': 'Paint walls', 'project_id': self.project.id})
        task2.message_subscribe(partner_ids=self.customer.partner_id.ids)
        chat2 = self.open_tab(self.customer, 'customer', task=task2)
        self.open_tab(self.staff, 'customer', task=task2)
        self.post(chat2, self.staff, 'another answer')
        opened = self.url_open('/my/woow_project_ai/unread', allow_redirects=False)
        self.assertTrue(opened.headers['Location'].endswith('/my/tasks'))
        # a task the customer cannot read any more: not counted (their own rights)
        self.env.flush_all()
        self.env.cr.execute("DELETE FROM mail_followers WHERE res_model = 'project.task' AND res_id IN %s "
                            "AND partner_id = %s", ((self.task.id, task2.id), self.customer.partner_id.id))
        self.env.invalidate_all()
        counters = self.make_jsonrpc_request('/my/counters', {'counters': ['woow_project_ai_unread_chat_count']})
        self.assertEqual(counters['woow_project_ai_unread_chat_count'], 0)
        self.assertNotIn('unread messages in your task AI chats', self.url_open('/my/notifications').text)

    def test_bridges_filter_whatever_the_import_order(self):
        # delta review 10: the order after a late import (a portal module installed into a running server)
        # puts that module's leaf controller first: the bridge's filter holds there, the module's own
        # override does not (why the bridge exists)
        if not installed(self.env, 'woow_odoo_portal_discuss'):
            self.skipTest('woow_odoo_portal_discuss is not installed')
        self.assertTrue(installed(self.env, 'woow_ai_task_portal_discuss'), 'the bridge must be installed')
        from odoo.addons.woow_ai_task.controllers.portal import WoowProjectAiPortal  # noqa: PLC0415
        from odoo.addons.woow_ai_task_portal_discuss.controllers.portal import (  # noqa: PLC0415
            NOT_A_TASK_CHAT, WoowProjectAiPortalDiscussions)
        from odoo.addons.woow_odoo_portal_discuss.controllers import portal as portal_discuss  # noqa: PLC0415

        def combined(*bases):
            # outside odoo.addons: never picked up by Odoo's routing (http.py keeps addon classes only)
            return type('LateImport', bases, {'__module__': 'woow_ai_task_test_late_import'})()

        with patch.object(portal_discuss, 'request', SimpleNamespace(env=self.env(user=self.customer))):
            late = combined(WoowProjectAiPortalDiscussions, WoowProjectAiPortal)._get_portal_discussion_domain()
            bare = combined(portal_discuss.PortalDiscussions, WoowProjectAiPortal)._get_portal_discussion_domain()
        self.assertIn(NOT_A_TASK_CHAT[0], late)
        self.assertNotIn(NOT_A_TASK_CHAT[0], bare)

    # --- the global rule: a strict no-op elsewhere ---

    def test_rule_is_a_no_op_for_other_channels(self):
        rule = self.env.ref(RULE)
        # one of every kind: group, direct chat, public channel, portal channel, the task chats
        internal = self.open_tab(self.staff, 'internal')
        customer_chat = self.open_tab(self.customer, 'customer')
        group = self.env['discuss.channel'].with_user(self.staff).create({'name': 'team', 'channel_type': 'group'})
        group.with_user(self.staff).add_members(partner_ids=self.customer.partner_id.ids)
        self.env['discuss.channel'].with_user(self.staff).channel_get([self.staff2.partner_id.id])
        self.env['discuss.channel'].create({'name': 'Everyone', 'channel_type': 'channel'})
        self.env['discuss.channel'].create({'name': 'Portal news', 'channel_type': 'channel',
                                            'group_public_id': self.env.ref('base.group_portal').id})
        users = {
            'employee': self.employee, 'project user': self.staff, 'portal': self.customer,
            'portal outsider': self.outsider, 'public': self.env.ref('base.public_user'),
        }
        if installed(self.env, 'im_livechat'):
            session, operator = self.livechat_session()
            users['livechat operator'] = operator
        guest = self.env['mail.guest'].create({'name': 'Guest Gina'})
        public = self.env.ref('base.public_user')
        envs = {label: self.env(user=user) for label, user in users.items()}
        envs['guest'] = self.env(user=public, context={'guest': guest})
        with_rule = {label: self.visible_channels(env) for label, env in envs.items()}
        rule.active = False
        self.env.registry.clear_cache()
        without_rule = {label: self.visible_channels(env) for label, env in envs.items()}
        rule.active = True
        self.env.registry.clear_cache()
        self.assertEqual(with_rule, without_rule)
        # and the task chats follow the task access
        self.assertIn(internal, self.env['discuss.channel'].with_user(self.staff).search([('id', '=', internal.id)]))
        self.assertFalse(self.env['discuss.channel'].with_user(self.staff2).search([('id', '=', internal.id)]))
        self.assertIn(customer_chat, self.env['discuss.channel'].with_user(self.customer).search(
            [('id', '=', customer_chat.id)]))

    def test_livechat_visitor_session(self):
        if not installed(self.env, 'im_livechat'):
            self.skipTest('im_livechat is not installed')
        session, _operator = self.livechat_session()
        self.assertEqual(session.channel_type, 'livechat')
        self.assertFalse(session.woow_project_ai_task_id)
        # the visitor (a guest) posts and reads with the rule active, as the Live Chat client does
        message = self.make_jsonrpc_request('/mail/message/post', {
            'thread_model': 'discuss.channel', 'thread_id': session.id,
            'post_data': {'body': 'Hello, I need help', 'message_type': 'comment',
                          'subtype_xmlid': 'mail.mt_comment'}})
        self.assertTrue(message)
        with_rule = self.make_jsonrpc_request('/discuss/channel/messages', {'channel_id': session.id})
        self.assertIn('Hello, I need help', json.dumps(with_rule, ensure_ascii=False))
        # exactly what the visitor gets without the rule
        rule = self.env.ref(RULE)
        rule.active = False
        without_rule = self.make_jsonrpc_request('/discuss/channel/messages', {'channel_id': session.id})
        rule.active = True
        self.assertEqual(with_rule['messages'], without_rule['messages'])
        # nobody of project AI answers there
        self.assertFalse(self.env['woow.project.ai.job'].search([('channel_id', '=', session.id)]))

    def test_livechat_operator_and_chatbot_posts(self):
        """Odoo's own Live Chat posts (an operator's answer, a chatbot's message, both through the channel's
        message_post) as before: only task chats need the module's post marker."""
        if not installed(self.env, 'im_livechat'):
            self.skipTest('im_livechat is not installed')
        session, operator = self.livechat_session()
        self.authenticate(operator.login, operator.login)
        self.make_jsonrpc_request('/mail/message/post', {
            'thread_model': 'discuss.channel', 'thread_id': session.id,
            'post_data': {'body': 'Operator here', 'message_type': 'comment', 'subtype_xmlid': 'mail.mt_comment'}})
        script = self.env['chatbot.script'].create({'title': 'Helper Bot'})
        message = session._chatbot_post_message(script, 'Chatbot here')
        self.assertEqual(message.author_id, script.operator_partner_id)
        bodies = self.env['mail.message'].sudo().search(
            [('model', '=', 'discuss.channel'), ('res_id', '=', session.id)]).mapped('body')
        self.assertTrue(any('Operator here' in body for body in bodies))
        self.assertTrue(any('Chatbot here' in body for body in bodies))

    # --- no double replies ---

    def test_no_double_replies(self):
        if not installed(self.env, 'woow_ai_contact'):
            self.skipTest('woow_ai_contact is not installed')
        advisor = self.discuss_ai_assistant()
        DiscussJob, ProjectJob = self.env['woow.ai.job'], self.env['woow.project.ai.job']
        # a task chat: one project AI job, no Discuss AI job
        internal = self.open_tab(self.staff, 'internal')
        self.post(internal, self.staff, 'task question')
        self.assertEqual(len(ProjectJob.search([('channel_id', '=', internal.id)])), 1)
        self.assertFalse(DiscussJob.search([('channel_id', '=', internal.id)]))
        # a Discuss AI direct chat: one Discuss AI job, no project AI job
        dm, _created = advisor._get_or_create_chat(self.staff)
        dm.with_user(self.staff).message_post(body='direct question', message_type='comment',
                                              subtype_xmlid='mail.mt_comment')
        self.assertEqual(len(DiscussJob.search([('channel_id', '=', dm.id)])), 1)
        self.assertFalse(ProjectJob.search([('channel_id', '=', dm.id)]))
        # each answered once, by its own module
        with patch(BACKEND_POST, return_value=openai_ok('project answer')) as post:
            self.run_due(internal)
            DiscussJob.search([('channel_id', '=', dm.id)])._run_job(commit=False)
        # (both modules call requests.post: one mock sees one call per module)
        self.assertEqual(post.call_count, 2)
        urls = sorted(call.args[0] for call in post.call_args_list)
        self.assertEqual(urls, ['https://discuss-llm.example.com/v1/chat/completions',
                                'https://llm.example.com/v1/chat/completions'])
        self.assertEqual(len(self.bot_messages(internal, self.internal_ai)), 1)
        replies = self.env['mail.message'].sudo().search([('model', '=', 'discuss.channel'), ('res_id', '=', dm.id),
                                                          ('author_id', '=', advisor.partner_id.id)])
        self.assertEqual(len(replies), 1)
        self.assertFalse(self.env['mail.message'].sudo().search([
            ('model', '=', 'discuss.channel'), ('res_id', '=', internal.id),
            ('author_id', '=', advisor.partner_id.id)]))
        # the Discuss AI bot never joins a task chat, the project bot never a Discuss AI chat
        self.assertNotIn(advisor.partner_id, self.members(internal))
        self.assertNotIn(self.internal_ai.partner_id, self.members(dm))

    def test_nothing_shared(self):
        if not installed(self.env, 'woow_ai_contact'):
            self.skipTest('woow_ai_contact is not installed')
        names = set(self.env.registry.models)
        ours = {name for name in names if name.startswith('woow.project.ai.')}
        theirs = {name for name in names if name.startswith('woow.ai.')}
        self.assertTrue(ours and theirs)
        self.assertFalse({self.env[name]._table for name in ours} & {self.env[name]._table for name in theirs})
        for xmlid in ('woow_ai_task.ir_cron_project_ai_run_jobs', 'woow_ai_task.ir_cron_project_ai_cleanup_logs',
                      'woow_ai_task.ir_cron_project_ai_sync_members'):
            cron = self.env.ref(xmlid)
            self.assertTrue(cron.name.startswith('Project AI:'))
            self.assertIn(cron.model_id.model, ('woow.project.ai.job', 'woow.project.ai.call.log',
                                                'discuss.channel'))
        # each push API takes its own keys only
        advisor = self.discuss_ai_assistant()
        discuss_key = advisor.action_generate_push_key()['context']['default_push_key']
        project_key = self.internal_ai.action_generate_push_key()['context']['default_push_key']
        internal = self.open_tab(self.staff, 'internal')
        for url, key in (('/woow_project_ai/v1/push', discuss_key), ('/woow_ai/v1/push', project_key)):
            with self.subTest(url=url):
                response = self.url_open(url, data=json.dumps({'to': {'channel_id': internal.id}, 'text': 'x'}),
                                         headers={'Content-Type': 'application/json',
                                                  'Authorization': 'Bearer %s' % key})
                self.assertEqual(response.status_code, 401)
        # Discuss AI's push into a task chat by id: refused (not one of its conversations)
        response = self.url_open('/woow_ai/v1/push', data=json.dumps({'to': {'channel_id': internal.id},
                                                                      'text': 'x'}),
                                 headers={'Content-Type': 'application/json',
                                          'Authorization': 'Bearer %s' % discuss_key})
        self.assertNotEqual(response.status_code, 200)
        self.assertFalse(self.bot_messages(internal, advisor))

    def test_signed_attachment_links_stay_apart(self):
        if not installed(self.env, 'woow_ai_contact'):
            self.skipTest('woow_ai_contact is not installed')
        from odoo.addons.woow_ai_contact.tools import media as discuss_media  # noqa: PLC0415
        internal = self.open_tab(self.staff, 'internal')
        attachment = self.pending_attachment(self.staff, name='plan.pdf')
        self.post(internal, self.staff, 'see the plan', attachments=attachment)
        secret = self.env['ir.config_parameter'].sudo().get_param('database.secret')
        ours, _expires = media.make_token(secret, attachment.id, attachment.checksum)
        theirs, _expires = discuss_media.make_token(secret, attachment.id, attachment.checksum)
        self.assertNotEqual(ours, theirs)
        cases = (('/woow_project_ai/attachment/%s/%s' % (attachment.id, ours), 200),
                 ('/woow_project_ai/attachment/%s/%s' % (attachment.id, theirs), 404),
                 ('/woow_ai/attachment/%s/%s' % (attachment.id, ours), 404),
                 ('/woow_ai/attachment/%s/%s' % (attachment.id, theirs), 404))
        for url, status in cases:
            with self.subTest(url=url.split('/')[1]):
                self.assertEqual(self.url_open(url).status_code, status)


if document_labels:
    @tagged('post_install', '-at_install', 'woow_ai_task', 'woow_ai_task_coexistence')
    class TestDocumentLabelsWithProjectAi(document_labels.TestNotificationLabelFollowsTheReader):
        """woow_document_management's own test of its message labels ("Restricted folder" / "Restricted
        document" for whoever cannot read them), run after this module is installed (its at_install run comes
        before): the task-chat message names extend Odoo's, so that module's override still runs."""
        allow_inherited_tests_method = True  # Odoo's loader collects a class's own test methods only

        @classmethod
        def setUpClass(cls):
            if 'woow_document_management' not in Registry(get_db_name())._init_modules:
                raise unittest.SkipTest('woow_document_management is not installed')
            super().setUpClass()
