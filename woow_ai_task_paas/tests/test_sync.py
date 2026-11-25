# -*- coding: utf-8 -*-
"""Task AI chat members follow the PaaS-only access changes, without the
hourly sweep: workspace membership rows (create, move, delete) and the
project - service binding (from either side, and a deleted service).

Who it affects: staff without manager rights. B (an employee) reads the
task only as a workspace member (the platform's rule); U (a project user)
still reads the task of a portal project through Odoo's own rule, but the
internal chat needs the project, which U reads only as a member; M (a
project manager) keeps everything through the stock rules. Portal users
follow the task's sharing, not the workspace."""

import uuid
from unittest.mock import MagicMock, patch

import psycopg2

from odoo import Command
from odoo.tests.common import HttpCase, TransactionCase, tagged

from odoo.addons.woow_ai_task_paas.models.cloud_service import CloudService as BridgeCloudService
from odoo.addons.woow_ai_task_paas.models.sync import quietly

from .common import PaasBridgeFixtures, RpcMixin

OPERATOR = 'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'


@tagged('post_install', '-at_install')
class TestSync(PaasBridgeFixtures, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_paas()

    def setUp(self):
        super().setUp()
        self.customer_chat = self.open_tab(self.user_b, 'customer')
        self.open_tab(self.user_c, 'customer')
        self.open_tab(self.user_m, 'customer')
        self.internal_chat = self.open_tab(self.user_u, 'internal')
        self.open_tab(self.user_m, 'internal')
        self.assertIn(self.user_b.partner_id, self.members(self.customer_chat))
        self.assertIn(self.user_u.partner_id, self.members(self.internal_chat))

    def assert_b_and_u_removed(self):
        self.assertNotIn(self.user_b.partner_id, self.members(self.customer_chat))
        self.assertNotIn(self.user_u.partner_id, self.members(self.internal_chat))
        # M and the customer stay
        self.assertIn(self.user_m.partner_id, self.members(self.customer_chat))
        self.assertIn(self.user_m.partner_id, self.members(self.internal_chat))
        self.assertIn(self.user_c.partner_id, self.members(self.customer_chat))

    def test_access_rows_deleted(self):
        (self.access['b'] | self.access['u']).unlink()
        self.assert_b_and_u_removed()

    def test_access_rows_moved(self):
        elsewhere = self.env['woow_paas_platform.workspace'].sudo().create({'name': 'Elsewhere',
                                                                            'owner_id': self.owner.id})
        self.access['b'].sudo().write({'workspace_id': elsewhere.id})
        self.access['u'].sudo().write({'user_id': self.user_e.id})
        self.assert_b_and_u_removed()
        self.assertNotIn(self.user_e.partner_id, self.members(self.internal_chat))

    def test_project_unbound(self):
        self.project.sudo().write({'cloud_service_ids': [Command.clear()]})
        self.assert_b_and_u_removed()

    def test_service_side_unbound(self):
        self.service.sudo().write({'project_ids': [Command.clear()]})
        self.assert_b_and_u_removed()

    def test_service_moved_to_another_workspace(self):
        # the platform fixes a deployed service's workspace (its namespace): a service not deployed yet moves
        draft = self.make_service(self.workspace, 'S not deployed', helm_namespace=False)
        self.project.sudo().write({'cloud_service_ids': [Command.set(draft.ids)]})
        self.assertIn(self.user_b.partner_id, self.members(self.customer_chat))  # still a member through it
        elsewhere = self.env['woow_paas_platform.workspace'].sudo().create({'name': 'Elsewhere',
                                                                            'owner_id': self.owner.id})
        draft.sudo().write({'workspace_id': elsewhere.id})
        self.assert_b_and_u_removed()

    def test_service_deleted(self):
        self.service.sudo().unlink()
        self.assert_b_and_u_removed()

    def test_a_service_created_with_a_project(self):
        """A project bound when a service is created: its followers (who read it as an unbound project) lose it."""
        unbound = self.env['project.project'].sudo().create({
            'name': 'Unbound', 'privacy_visibility': 'portal',
            'woow_project_ai_customer_assistant_id': self.customer_ai.id})
        task = self.env['project.task'].sudo().create({'name': 'Unbound task', 'project_id': unbound.id})
        unbound.message_subscribe(partner_ids=self.user_e.partner_id.ids)
        chat = self.open_tab(self.user_e, 'customer', task=task)
        self.assertIn(self.user_e.partner_id, self.members(chat))
        self.make_service(self.workspace, 'S created bound', project_ids=[Command.set(unbound.ids)])
        self.assertNotIn(self.user_e.partner_id, self.members(chat))

    def test_a_service_created_with_a_project_from_a_generator(self):
        """create() reads its values again after super(): a one-shot iterable must still sync."""
        unbound = self.env['project.project'].sudo().create({
            'name': 'Unbound gen', 'privacy_visibility': 'portal',
            'woow_project_ai_customer_assistant_id': self.customer_ai.id})
        task = self.env['project.task'].sudo().create({'name': 'Unbound gen task', 'project_id': unbound.id})
        unbound.message_subscribe(partner_ids=self.user_e.partner_id.ids)
        chat = self.open_tab(self.user_e, 'customer', task=task)
        from odoo.addons.woow_paas_platform.models.cloud_service import _internal_provision  # noqa: PLC0415
        vals = {'name': 'S generator', 'workspace_id': self.workspace.id, 'template_id': self.template.id,
                'state': 'running', 'helm_namespace': 'paas-ws-test-bridge',
                'helm_release_name': 'svc-bridge-gen-%s' % uuid.uuid4().hex[:6],
                'helm_values': '{"config": {"nonSensitive": {}}}', 'project_ids': [Command.set(unbound.ids)]}
        services = _internal_provision(self.env['woow_paas_platform.cloud_service'].sudo()).create(
            item for item in [vals])
        self.assertEqual(len(services), 1)
        self.assertEqual(services.project_ids, unbound)
        self.assertNotIn(self.user_e.partner_id, self.members(chat))

    def test_a_grant_adds_nobody(self):
        before = (self.members(self.customer_chat), self.members(self.internal_chat))
        self.env['woow_paas_platform.workspace_access'].sudo().create({
            'workspace_id': self.workspace.id, 'user_id': self.user_e.id, 'role': 'user'})
        self.assertEqual((self.members(self.customer_chat), self.members(self.internal_chat)), before)
        # E may now open the customer chat (the rule follows at once)
        self.assertTrue(self.task._woow_project_ai_user_allowed(self.user_e, 'customer'))

    def test_a_state_write_costs_no_query_and_no_sync(self):
        twin = self.make_service(self.workspace, 'S twin')
        (self.service | twin).flush_recordset()
        with patch.object(type(self.env['discuss.channel']), '_woow_project_ai_sync_projects') as sync:
            self.env.invalidate_all()
            start = self.env.cr.sql_log_count
            super(BridgeCloudService, twin.sudo()).write({'state': 'running'})  # the platform's write alone
            self.env.flush_all()
            without_bridge = self.env.cr.sql_log_count - start
            self.env.invalidate_all()
            start = self.env.cr.sql_log_count
            self.service.sudo().write({'state': 'running'})  # through this module's override
            self.env.flush_all()
            with_bridge = self.env.cr.sql_log_count - start
        sync.assert_not_called()
        self.assertEqual(with_bridge, without_bridge)


@tagged('post_install', '-at_install')
class TestSyncKeepsTheAccessChecks(PaasBridgeFixtures, RpcMixin, HttpCase):
    """The sync hooks read the rows before the platform's own write and unlink
    in SQL, never with a sudo read, so that the platform's own access checks
    on these rows run first and unchanged: a denied call changes nothing and
    reaches no operator."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_paas()

    def test_a_denied_unlink_or_binding_write_reaches_no_operator(self):
        # a service bound to no project, and a bound one: a denied call reaches no operator
        alone = self.make_service(self.workspace, 'S alone')
        self.authenticate(self.user_c.login, self.user_c.login)  # a portal member: no PaaS ACL at all
        with patch(OPERATOR) as operator:
            for service in (alone, self.service):
                for method, args in (('unlink', [service.ids]),
                                     ('write', [service.ids, {'project_ids': [Command.clear()]}]),
                                     ('write', [service.ids, {'workspace_id': self.workspace.id}])):
                    result = self.call_kw('woow_paas_platform.cloud_service', method, args)
                    self.assertIn('AccessError', self.error_of(result), (service.name, method, result))
        operator.assert_not_called()
        self.assertTrue((alone | self.service).exists() == (alone | self.service))
        self.assertEqual(self.service.sudo().project_ids, self.project)

    def test_a_denied_membership_change_keeps_the_members(self):
        chat = self.open_tab(self.user_b, 'customer')
        self.authenticate(self.user_c.login, self.user_c.login)
        for method, args in (('unlink', [self.access['b'].ids]),
                             ('write', [self.access['b'].ids, {'user_id': self.user_e.id}])):
            result = self.call_kw('woow_paas_platform.workspace_access', method, args)
            self.assertIn('AccessError', self.error_of(result), (method, result))
        self.assertTrue(self.access['b'].exists())
        self.assertIn(self.user_b.partner_id, self.members(chat))


@tagged('post_install', '-at_install')
class TestSyncFailsSoft(PaasBridgeFixtures, TransactionCase):
    """A sync that fails after the platform's own write or delete is logged (its type and code lines, never its
    text) and swallowed: the platform's change stands, the hourly sweep syncs the chats later. A concurrency error
    propagates (Odoo retries the whole request)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_paas()

    def failing(self, method, error):
        return patch.object(type(self.env['discuss.channel']), method, side_effect=error)

    def assert_logged(self, logs):
        output = '\n'.join(logs.output)
        self.assertIn('RuntimeError', output)
        self.assertNotIn('secret detail', output)

    def test_a_service_delete_stands(self):
        with patch(OPERATOR), self.failing('_woow_project_ai_sync_projects', RuntimeError('secret detail')), \
                self.assertLogs('odoo.addons.woow_ai_task_paas.models.sync', 'ERROR') as logs:
            self.service.sudo().unlink()
        self.assert_logged(logs)
        self.assertFalse(self.service.exists())
        self.assertTrue(self.env['project.project'].sudo().create({'name': 'After the failure'}))

    def test_a_service_binding_write_stands(self):
        with self.failing('_woow_project_ai_sync_projects', RuntimeError('secret detail')), \
                self.assertLogs('odoo.addons.woow_ai_task_paas.models.sync', 'ERROR') as logs:
            self.service.sudo().write({'project_ids': [Command.clear()]})
        self.assert_logged(logs)
        self.assertFalse(self.service.sudo().project_ids)

    def test_a_project_binding_write_stands(self):
        with self.failing('_woow_project_ai_sync_projects', RuntimeError('secret detail')), \
                self.assertLogs('odoo.addons.woow_ai_task_paas.models.sync', 'ERROR') as logs:
            self.project.sudo().write({'cloud_service_ids': [Command.clear()]})
        self.assert_logged(logs)
        self.assertFalse(self.project.sudo().cloud_service_ids)

    def test_membership_changes_stand(self):
        with self.failing('_woow_project_ai_sync_partners', RuntimeError('secret detail')), \
                self.assertLogs('odoo.addons.woow_ai_task_paas.models.sync', 'ERROR') as logs:
            self.access['b'].sudo().write({'user_id': self.user_e.id})
            self.access['u'].sudo().unlink()
        self.assert_logged(logs)
        self.assertEqual(self.access['b'].user_id, self.user_e)
        self.assertFalse(self.access['u'].exists())
        with self.failing('_woow_project_ai_forget_readable', RuntimeError('secret detail')), \
                self.assertLogs('odoo.addons.woow_ai_task_paas.models.sync', 'ERROR') as logs:
            row = self.env['woow_paas_platform.workspace_access'].sudo().create({
                'workspace_id': self.workspace.id, 'user_id': self.user_x.id, 'role': 'user'})
        self.assert_logged(logs)
        self.assertTrue(row.exists())

    def test_a_concurrency_error_propagates(self):
        with self.failing('_woow_project_ai_sync_projects', psycopg2.errors.SerializationFailure('retry me')), \
                self.assertRaises(psycopg2.errors.SerializationFailure):
            self.project.sudo().write({'cloud_service_ids': [Command.clear()]})

    def test_a_callers_precommit_error_stays_the_callers(self):
        """The guard flushes the caller's pending work, precommit callbacks included, before its guarded block: a
        callback's error is raised as the caller's, never logged and swallowed as a failed sync."""
        def callback():
            raise RuntimeError('the caller')

        guarded = MagicMock(return_value='synced')
        with patch('odoo.addons.woow_ai_task_paas.models.sync.log_unexpected') as logged, \
                self.assertRaises(RuntimeError):
            # added inside assertRaises: Odoo's assertRaises opens (and so flushes) a savepoint of its own first
            self.env.cr.precommit.add(callback)
            quietly(self.env, 'a sync', [1], guarded)
        logged.assert_not_called()
        guarded.assert_not_called()
