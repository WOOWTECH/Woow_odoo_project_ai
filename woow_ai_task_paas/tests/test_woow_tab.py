# -*- coding: utf-8 -*-
"""The /woow task page's customer AI chat route: who gets it, how it opens,
and lockstep with /my's own section on portal projects."""

from odoo import Command
from odoo.tests.common import HttpCase, tagged

from .common import PaasBridgeFixtures, RpcMixin

ROUTE = '/woow_project_ai_paas/task_chat'


@tagged('post_install', '-at_install')
class TestWoowTab(PaasBridgeFixtures, RpcMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_paas()

    def route(self, user, task_id):
        self.authenticate(user.login, user.login)
        response = self.json_rpc(ROUTE, {'task_id': task_id}).json()
        self.assertNotIn('error', response, response)
        return response['result']

    def test_who_gets_the_tab(self):
        for user, staff in ((self.user_c, False), (self.user_c2, False), (self.user_m, True), (self.user_a, True),
                            (self.user_b, True), (self.user_u, True)):
            info = self.route(user, self.task.id)
            self.assertTrue(info['available'], user.login)
            self.assertIs(info['staff'], staff, user.login)
            self.assertIs(info['readonly'], False, user.login)
            self.assertTrue(info['url'].startswith('/my/tasks/%s/woow_project_ai?token=' % self.task.id))
        for user in (self.user_x, self.user_e):
            self.assertEqual(self.route(user, self.task.id), {'available': False}, user.login)

    def test_not_available(self):
        Project, Task = self.env['project.project'].sudo(), self.env['project.task'].sudo()
        # no customer assistant
        no_ai = Project.create({'name': 'No customer AI', 'privacy_visibility': 'portal',
                                'woow_project_ai_internal_assistant_id': self.internal_ai.id})
        # a project not shared with portal users (the backend tab's own rule)
        internal = Project.create({'name': 'Employees only', 'privacy_visibility': 'employees',
                                   'woow_project_ai_customer_assistant_id': self.customer_ai.id})
        for project in (no_ai, internal):
            task = Task.create({'name': 'In %s' % project.name, 'project_id': project.id})
            task.message_subscribe(partner_ids=self.user_c.partner_id.ids)
            for user in (self.user_c, self.user_m):
                self.assertEqual(self.route(user, task.id), {'available': False}, (project.name, user.login))
        # a deleted task, ids that are not integers
        gone = Task.create({'name': 'Deleted', 'project_id': self.project.id})
        gone_id = gone.id
        gone.unlink()
        for task_id in (gone_id, 'abc', None, True, -1, 0, 1.5, str(self.task.id), [self.task.id]):
            self.assertEqual(self.route(self.user_m, task_id), {'available': False}, task_id)
        # a task without a project
        private = Task.create({'name': 'Private'})
        self.assertEqual(self.route(self.user_a, private.id), {'available': False})

    def test_archived_task(self):
        Task = self.env['project.task'].sudo()
        without_chat = Task.create({'name': 'Archived, no chat', 'project_id': self.project.id})
        with_chat = Task.create({'name': 'Archived, chat', 'project_id': self.project.id})
        with_chat.message_subscribe(partner_ids=self.user_c.partner_id.ids)
        self.open_tab(self.user_m, 'customer', task=with_chat)
        (without_chat | with_chat).write({'active': False})
        self.assertEqual(self.route(self.user_m, without_chat.id), {'available': False})
        info = self.route(self.user_m, with_chat.id)
        self.assertTrue(info['available'])
        self.assertTrue(info['readonly'])
        # portal users read no archived task
        self.assertEqual(self.route(self.user_c, with_chat.id), {'available': False})

    def test_opening_creates_or_joins_the_chat(self):
        info = self.route(self.user_c, self.task.id)
        self.assertFalse(self.task._woow_project_ai_tab_channel('customer'))  # the route has no side effect
        response = self.url_open(info['url'], allow_redirects=False, timeout=30)
        self.assertIn(response.status_code, (301, 302, 303))
        chat = self.task._woow_project_ai_tab_channel('customer')
        self.assertTrue(chat)
        self.assertTrue(response.headers['Location'].endswith('/discuss/channel/%s' % chat.id))
        self.assertIn(self.user_c.partner_id, self.members(chat))
        self.assertEqual(self.url_open('/discuss/channel/%s' % chat.id, timeout=30).status_code, 200)
        # a stranger: not found
        self.authenticate(self.user_x.login, self.user_x.login)
        self.assertEqual(self.url_open('/discuss/channel/%s' % chat.id, timeout=30).status_code, 404)

    def test_a_wrong_token_shows_the_reload_page(self):
        self.authenticate(self.user_c.login, self.user_c.login)
        response = self.url_open('/my/tasks/%s/woow_project_ai?token=wrong' % self.task.id, timeout=30)
        self.assertEqual(response.status_code, 200)
        self.assertIn('target="_top"', response.text)
        self.assertFalse(self.task._woow_project_ai_tab_channel('customer'))

    def test_another_sessions_token_is_refused(self):
        info = self.route(self.user_c, self.task.id)
        self.authenticate(self.user_c.login, self.user_c.login)  # a new session (a new login)
        response = self.url_open(info['url'], allow_redirects=False, timeout=30)
        self.assertEqual(response.status_code, 200)
        self.assertIn('target="_top"', response.text)
        self.assertFalse(self.task._woow_project_ai_tab_channel('customer'))

    def test_lockstep_with_my(self):
        """On a portal project, the route agrees with /my's own section for every user."""
        other = self.env['project.task'].sudo().create({'name': 'Followed by C2 only', 'project_id': self.project.id,
                                                        'user_ids': [Command.set(self.user_e.ids)]})
        other.message_subscribe(partner_ids=self.user_c2.partner_id.ids)
        users = (self.user_b, self.user_u, self.user_m, self.user_a, self.user_c, self.user_c2, self.user_x,
                 self.user_e)
        for task in (self.task, other):
            for user in users:
                available = self.route(user, task.id)['available']
                response = self.url_open('/my/tasks/%s' % task.id, timeout=30)
                section = response.status_code == 200 and 'id="woow_project_ai_chat"' in response.text
                self.assertEqual(available, section, (task.name, user.login, response.status_code))
