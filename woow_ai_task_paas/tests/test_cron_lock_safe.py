# -*- coding: utf-8 -*-
"""Cron parking is lock-safe: a cron row held by another transaction (a
running job) is skipped and reported "retry", the other crons are parked, and
nothing of the caller's transaction is rolled back (a plain ir.cron write would
roll the whole transaction back). The same in reverse; and nothing is parked
while an agent session is live."""

import json
from contextlib import contextmanager

from odoo.modules.module import get_manifest
from odoo.sql_db import db_connect
from odoo.tests.common import TransactionCase, tagged

from odoo.addons import woow_ai_task_paas
from odoo.addons.woow_ai_task_paas import hooks

from .common import LEGACY_CRONS, PARKED_PARAM, SESSION, PaasBridgeFixtures


@tagged('post_install', '-at_install')
class TestCronLockSafe(PaasBridgeFixtures, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_paas()
        cls.Session = cls.env[SESSION]
        cls.crons = {xmlid: cls.env.ref(xmlid) for xmlid in LEGACY_CRONS}

    @contextmanager
    def held(self, cron):
        """`cron`'s row locked by another connection (as the cron runner holds a running job's row)."""
        cursor = db_connect(self.env.cr.dbname).cursor()
        try:
            cursor.execute("SELECT id FROM ir_cron WHERE id = %s FOR NO KEY UPDATE NOWAIT", [cron.id])
            yield
        finally:
            cursor.rollback()
            cursor.close()

    def set_active(self, crons, active):
        self.env.cr.execute("UPDATE ir_cron SET active = %s WHERE id IN %s", [active, tuple(crons.ids)])
        self.env['ir.cron'].invalidate_model(['active'])

    def active_of(self, xmlid):
        cron = self.crons[xmlid].with_context(active_test=False)
        cron.invalidate_recordset(['active'])
        return cron.active

    def parked(self):
        return json.loads(self.env['ir.config_parameter'].sudo().get_param(PARKED_PARAM) or '[]')

    def test_park_skips_a_held_cron_and_rolls_nothing_back(self):
        held_xmlid, others = LEGACY_CRONS[0], LEGACY_CRONS[1:]
        self.env['ir.config_parameter'].sudo().set_param(PARKED_PARAM, False)
        with self.held(self.crons[held_xmlid]):
            # the other three active again (this transaction's own rows: not skipped by SKIP LOCKED)
            self.set_active(self.env['ir.cron'].browse([self.crons[x].id for x in others]), True)
            marker = self.env['project.project'].sudo().create({'name': 'Written before parking'})
            report = self.Session._woow_project_ai_paas_park_crons()
        self.assertFalse(report['error'])
        self.assertEqual(report['live'], 0)
        self.assertEqual(set(report['parked']), set(others))
        self.assertEqual(report['retry'], [held_xmlid])
        for xmlid in others:
            self.assertFalse(self.active_of(xmlid), xmlid)
        self.assertEqual(set(self.parked()), set(others))
        # nothing of this transaction was rolled back
        self.assertTrue(marker.exists())
        self.assertEqual(marker.name, 'Written before parking')
        # again, nobody holding a row: nothing more to park (the held one was not active), nothing recorded twice
        again = self.Session._woow_project_ai_paas_park_crons()
        self.assertEqual(again['parked'], [])
        self.assertEqual(set(self.parked()), set(others))

    def test_unpark_skips_a_held_cron(self):
        held_xmlid = LEGACY_CRONS[2]
        # its committed state (parked at install, or kept active if a session was live then): this transaction must
        # not write the held row before the other connection locks it
        held_before = self.active_of(held_xmlid)
        others = self.env['ir.cron'].browse([cron.id for xmlid, cron in self.crons.items() if xmlid != held_xmlid])
        self.set_active(others, False)
        self.env['ir.config_parameter'].sudo().set_param(PARKED_PARAM, json.dumps(sorted(LEGACY_CRONS)))
        with self.held(self.crons[held_xmlid]):
            marker = self.env['project.project'].sudo().create({'name': 'Written before unparking'})
            report = self.Session._woow_project_ai_paas_unpark_crons()
        self.assertFalse(report['error'])
        self.assertEqual(report['retry'], [held_xmlid])
        self.assertEqual(set(report['unparked']), set(LEGACY_CRONS) - {held_xmlid})
        for xmlid in set(LEGACY_CRONS) - {held_xmlid}:
            self.assertTrue(self.active_of(xmlid), xmlid)
        self.assertEqual(self.active_of(held_xmlid), held_before)
        self.assertEqual(self.parked(), [held_xmlid])
        self.assertTrue(marker.exists())

    def test_unpark_touches_only_what_it_parked(self):
        all_crons = self.env['ir.cron'].browse([cron.id for cron in self.crons.values()])
        self.set_active(all_crons, False)
        self.env['ir.config_parameter'].sudo().set_param(PARKED_PARAM, json.dumps([LEGACY_CRONS[1]]))
        report = self.Session._woow_project_ai_paas_unpark_crons()
        self.assertEqual(report['unparked'], [LEGACY_CRONS[1]])
        self.assertTrue(self.active_of(LEGACY_CRONS[1]))
        for xmlid in (LEGACY_CRONS[0], LEGACY_CRONS[2], LEGACY_CRONS[3]):
            self.assertFalse(self.active_of(xmlid), xmlid)
        self.assertFalse(self.parked())

    def test_nothing_is_parked_while_a_session_is_live(self):
        all_crons = self.env['ir.cron'].browse([cron.id for cron in self.crons.values()])
        self.set_active(all_crons, True)
        self.env['ir.config_parameter'].sudo().set_param(PARKED_PARAM, False)
        self.setup_legacy_seed()  # a reaped session with a hash
        report = self.Session._woow_project_ai_paas_park_crons()
        self.assertEqual(report['live'], 1)
        self.assertEqual(report['parked'], [])
        for xmlid in LEGACY_CRONS:
            self.assertTrue(self.active_of(xmlid), xmlid)
        self.assertFalse(self.parked())

    def test_the_install_hook_parks_the_crons(self):
        """The manifest names the hook, and the hook parks every active old cron when no session is live (this
        class marked the database's own sessions absent in its transaction)."""
        # what Odoo calls at install: the function the manifest names, looked up on the module's package
        self.assertEqual(get_manifest('woow_ai_task_paas').get('post_init_hook'), 'post_init_hook')
        self.assertIs(woow_ai_task_paas.post_init_hook, hooks.post_init_hook)
        self.env['ir.config_parameter'].sudo().set_param(PARKED_PARAM, False)
        self.set_active(self.env['ir.cron'].browse([cron.id for cron in self.crons.values()]), True)
        hooks.post_init_hook(self.env)
        for xmlid in LEGACY_CRONS:
            self.assertFalse(self.active_of(xmlid), xmlid)
        self.assertEqual(set(self.parked()), set(LEGACY_CRONS))


@tagged('post_install', '-at_install')
class TestCronLockSafeSerialization(PaasBridgeFixtures, TransactionCase):
    """Alone in its class (its own transaction): the other connection commits a change to a cron row, which any
    later test of the same transaction could not lock either."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_paas()
        cls.Session = cls.env[SESSION]
        cls.crons = {xmlid: cls.env.ref(xmlid) for xmlid in LEGACY_CRONS}

    set_active = TestCronLockSafe.set_active
    active_of = TestCronLockSafe.active_of

    def test_a_row_updated_since_the_snapshot_is_retried(self):
        """Odoo's cursors are REPEATABLE READ: a cron row the runner updated (and committed) after this
        transaction began cannot be locked here (a serialization failure). That cron is "retry"; the others are
        parked and nothing of this transaction is rolled back."""
        changed_xmlid, others = LEGACY_CRONS[1], LEGACY_CRONS[:1] + LEGACY_CRONS[2:]
        self.env['ir.config_parameter'].sudo().set_param(PARKED_PARAM, False)
        self.Session.search_count([])  # this transaction's snapshot exists before the other one commits
        cursor = db_connect(self.env.cr.dbname).cursor()
        try:
            cursor.execute("UPDATE ir_cron SET lastcall = lastcall WHERE id = %s", [self.crons[changed_xmlid].id])
            cursor.commit()
        finally:
            cursor.close()
        self.set_active(self.env['ir.cron'].browse([self.crons[x].id for x in others]), True)
        marker = self.env['project.project'].sudo().create({'name': 'Written before parking'})
        # the handled serialization failure logs no ERROR line (an install's acceptance is "0 ERROR lines")
        with self.assertNoLogs('odoo.sql_db', 'ERROR'):
            report = self.Session._woow_project_ai_paas_park_crons()
        self.assertFalse(report['error'])
        self.assertEqual(report['retry'], [changed_xmlid])
        self.assertEqual(set(report['parked']), set(others))
        self.assertTrue(marker.exists())
