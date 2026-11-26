# -*- coding: utf-8 -*-
"""Every platform touchpoint fails soft (every platform upgrade upgrades this
module too): with a platform field or method gone, the data function and the
hooks log and never raise, and projects and tasks are still created; where
doing nothing would be unsafe (counting live agent sessions), nothing is
parked and the registry load logs CRITICAL."""

from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_ai_task_paas import hooks

from .common import LEGACY_CRONS, SESSION, PaasBridgeFixtures


@tagged('post_install', '-at_install')
class TestFailSoft(PaasBridgeFixtures, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_paas()
        cls.setup_legacy_seed()

    def test_the_data_function_logs_and_does_not_raise(self):
        Task = self.env['project.task']
        with patch.object(type(Task), '_woow_project_ai_paas_restrict_legacy_chats_now',
                          side_effect=RuntimeError('secret detail')), \
                self.assertLogs('odoo.addons.woow_ai_task_paas.models.project_task', 'ERROR') as logs:
            result = Task._woow_project_ai_paas_restrict_legacy_chats()
        self.assertEqual(result['error'], 'RuntimeError')
        output = '\n'.join(logs.output)
        self.assertIn('RuntimeError', output)
        self.assertNotIn('secret detail', output)  # its type and code lines, never its text
        self.assertTrue(self.legacy_chat.active)
        # the transaction is still usable
        self.assertTrue(self.env['project.project'].sudo().create({'name': 'After the failure'}))

    def test_without_the_old_task_fields(self):
        Task = type(self.env['project.task'])
        with patch.dict(Task._fields):
            for name in ('channel_id', 'chat_enabled'):
                del Task._fields[name]
            self.assertEqual(self.env['project.task']._woow_project_ai_paas_restrict_legacy_chats(),
                             {'restricted': [], 'others': []})
            task = self.env['project.task'].sudo().create({'name': 'No old fields', 'project_id': self.project.id})
            task.write({'name': 'Renamed'})
            self.assertEqual(task._create_chat_channel(), self.env['discuss.channel'])
        self.assertEqual(task.name, 'Renamed')

    def test_without_the_runtime_field(self):
        Project = type(self.env['project.project'])
        with patch.dict(Project._fields):
            del Project._fields['use_agent_runtime']
            project = self.env['project.project'].sudo().create({'name': 'No runtime field'})
            project.sudo().write({'name': 'Renamed'})
        self.assertEqual(project.name, 'Renamed')

    def test_hooks_log_and_never_raise(self):
        Session = type(self.env[SESSION])
        with patch.object(Session, '_woow_project_ai_paas_park_crons', side_effect=RuntimeError('boom')), \
                self.assertLogs('odoo.addons.woow_ai_task_paas.hooks', 'ERROR'):
            hooks.post_init_hook(self.env)
        with patch.object(Session, '_woow_project_ai_paas_unpark_crons', side_effect=RuntimeError('boom')), \
                self.assertLogs('odoo.addons.woow_ai_task_paas.hooks', 'ERROR'):
            hooks.uninstall_hook(self.env)

    def test_cron_parking_survives_a_failure_and_missing_crons(self):
        Session = self.env[SESSION]
        self.legacy_session.sudo().state = 'absent'
        with patch.object(type(Session), '_woow_project_ai_paas_lock_crons', side_effect=RuntimeError('boom')):
            report = Session._woow_project_ai_paas_park_crons()
        self.assertEqual(report['error'], 'RuntimeError')
        # a cron xmlid that no longer exists is reported, the others are parked
        missing = self.env.ref(LEGACY_CRONS[0])
        self.env['ir.model.data'].sudo().search([('module', '=', 'woow_paas_platform'),
                                                  ('name', '=', LEGACY_CRONS[0].split('.')[1])]).unlink()
        self.env.cr.execute("UPDATE ir_cron SET active = TRUE WHERE id IN %s",
                            [tuple(self.env.ref(xmlid).id for xmlid in LEGACY_CRONS[1:])])
        self.env['ir.cron'].invalidate_model()
        report = Session._woow_project_ai_paas_park_crons()
        self.assertEqual(report['missing'], [LEGACY_CRONS[0]])
        self.assertEqual(set(report['parked']), set(LEGACY_CRONS[1:]))
        self.assertTrue(missing.exists())

    def test_the_uninstall_hook_keeps_the_record_of_crons_it_could_not_unpark(self):
        Session = type(self.env[SESSION])
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('woow_ai_task_paas.parked_crons', '["%s"]' % LEGACY_CRONS[0])
        ICP.set_param('woow_ai_task_paas.legacy_task_ai', 'True')
        retry = {'unparked': [], 'retry': [LEGACY_CRONS[0]], 'missing': [], 'error': False}
        # a cron held by a running job is expected: a warning (a person activates it), not an error
        with patch.object(Session, '_woow_project_ai_paas_unpark_crons', return_value=retry), \
                self.assertLogs('odoo.addons.woow_ai_task_paas.hooks', 'WARNING') as logs:
            hooks.uninstall_hook(self.env)
        self.assertEqual([record.levelname for record in logs.records], ['WARNING'])
        self.assertTrue(ICP.get_param('woow_ai_task_paas.parked_crons'))
        self.assertFalse(ICP.get_param('woow_ai_task_paas.legacy_task_ai'))

    def test_unknown_sessions_park_nothing(self):
        """If the platform renames the fields that tell a live session, nothing is parked (unknown counts as
        live) and it is logged CRITICAL, at registry load too."""
        Session = self.env[SESSION]
        crons = self.env['ir.cron'].browse([self.env.ref(xmlid).id for xmlid in LEGACY_CRONS])
        self.env.cr.execute("UPDATE ir_cron SET active = TRUE WHERE id IN %s", [tuple(crons.ids)])
        self.env['ir.cron'].invalidate_model()
        with patch.dict(type(Session)._fields):
            del type(Session)._fields['project_namespace_hash']
            with self.assertLogs('odoo.addons.woow_ai_task_paas.models.agent_session', 'CRITICAL'):
                report = Session._woow_project_ai_paas_park_crons()
            with self.assertLogs('odoo.addons.woow_ai_task_paas.models.agent_session', 'CRITICAL'):
                Session._register_hook()
        self.assertEqual(report['error'], 'unknown sessions')
        self.assertEqual(report['parked'], [])
        self.assertTrue(all(crons.with_context(active_test=False).mapped('active')))
