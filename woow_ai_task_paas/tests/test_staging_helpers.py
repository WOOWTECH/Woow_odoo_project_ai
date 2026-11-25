# -*- coding: utf-8 -*-
"""The teardown helper of the old agent runtimes: every session with a
namespace hash in any state but absent, the platform's own
_teardown_runtime() per session, absent and a commit per session, failures
reported (never raised), then the crons parked lock-safely (kept while a
session is still live)."""

import uuid
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from .common import SESSION, PaasBridgeFixtures

LIVE_STATES = ('spawning', 'active', 'reaped', 'degraded', 'spawn_failed')


@tagged('post_install', '-at_install')
class TestStagingHelpers(PaasBridgeFixtures, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_paas()
        Session = cls.env[SESSION].sudo()
        Project = cls.env['project.project'].sudo()
        cls.sessions = Session.browse()
        with patch.object(type(Session), '_get_namespace_secret', return_value=b'bridge-test-secret'):
            for state in LIVE_STATES + ('absent',):
                project = Project.create({'name': 'Runtime %s' % state})
                cls.sessions |= Session.create({'project_id': project.id, 'state': state,
                                                'project_namespace_hash': 'h%s' % uuid.uuid4().hex[:15]})
        cls.live = cls.sessions.filtered(lambda session: session.state != 'absent')

    def test_dry_run_plans_every_live_session(self):
        report = self.env[SESSION]._woow_project_ai_paas_teardown_all()
        self.assertEqual([row['id'] for row in report['plan']], self.live.ids)
        self.assertEqual({row['state'] for row in report['plan']}, set(LIVE_STATES))
        for row, session in zip(report['plan'], self.live):
            self.assertEqual(row['hash4'], session.project_namespace_hash[:4])
            self.assertEqual(row['project_id'], session.project_id.id)
        self.assertNotIn('done', report)
        self.assertEqual(set(self.live.mapped('state')), set(LIVE_STATES))

    def test_apply_commits_per_session_and_reports_a_failure(self):
        failing = self.live.filtered(lambda session: session.state == 'degraded')
        torn_down = []

        def teardown(session):
            if session == failing:
                raise RuntimeError('operator said no')
            torn_down.append(session.id)

        Session = type(self.env[SESSION])
        with patch.object(Session, '_teardown_runtime', autospec=True, side_effect=teardown), \
                patch.object(self.env.cr, 'commit') as commit:
            report = self.env[SESSION]._woow_project_ai_paas_teardown_all(dry_run=False)
        self.assertEqual(sorted(report['done']), sorted((self.live - failing).ids))
        self.assertEqual(sorted(torn_down), sorted((self.live - failing).ids))
        self.assertEqual(report['failed'], [{'id': failing.id, 'error': 'RuntimeError'}])
        self.assertEqual(commit.call_count, len(self.live) - 1 + 1)  # one per torn-down session, one for the parking
        self.live.invalidate_recordset(['state'])
        self.assertEqual(set((self.live - failing).mapped('state')), {'absent'})
        self.assertEqual(failing.state, 'degraded')
        # one session is still live: the crons are kept
        self.assertEqual(report['crons']['live'], 1)
        self.assertEqual(report['crons']['parked'], [])
