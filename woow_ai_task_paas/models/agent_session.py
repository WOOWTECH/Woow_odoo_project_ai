# -*- coding: utf-8 -*-
"""The old task AI's agent sessions: its four crons parked lock-safely, and
the teardown helper of the staging clean-up.

**Why not a plain cron write.** ``ir.cron.write`` first locks its rows with
NOWAIT and, when one is held, rolls back the WHOLE transaction before it
raises (Odoo's ``ir.cron._try_lock``); the cron runner holds a job's row
while the job runs. So this module never writes ``ir.cron`` rows it has not
locked itself first: each row with ``SELECT ... FOR NO KEY UPDATE SKIP
LOCKED`` in its own savepoint, then ``active=False`` only on the rows locked
(this transaction holds their locks, so ``_try_lock`` succeeds). A row held
by a running job is skipped, and a row the runner updated after this
transaction began raises a serialization failure (Odoo's cursors are
REPEATABLE READ, as in ``ir.cron._acquire_one_job``): both are reported
"retry", never waited for and never a failure.

The crons act only on sessions that have a namespace hash: with no live
session they do nothing, and with one they are its shrink path (idle reaper,
GC), so they are parked only once no session is live. They are
``noupdate="1"`` in the platform: a platform upgrade never re-activates them.
Whether a session is live is read from two platform fields: if either is
gone, nothing is parked (unknown counts as live) and the registry load logs
it as CRITICAL.
"""

import logging

import psycopg2

from odoo import api, models

from odoo.addons.woow_ai_task.tools.logs import log_unexpected

from .legacy import LEGACY_CRONS, PARKED_PARAM, read_json_param, write_json_param

_logger = logging.getLogger(__name__)

SESSION_FIELDS = ('project_namespace_hash', 'state')


class AgentSession(models.Model):
    _inherit = 'woow_paas_platform.agent_session'

    def _register_hook(self):
        super()._register_hook()
        missing = self._woow_project_ai_paas_missing_fields()
        if missing:
            _logger.critical("woow_ai_task_paas: woow_paas_platform.agent_session has no field %s: live agent "
                             "sessions cannot be counted, so the old task AI's crons are never parked; a release of "
                             "this module for the platform's new fields is needed", missing)

    @api.model
    def _woow_project_ai_paas_missing_fields(self):
        return [name for name in SESSION_FIELDS if name not in self._fields]

    @api.model
    def _woow_project_ai_paas_live_sessions(self):
        """Sessions with a namespace hash in any state but ``absent`` (sudo).
        Empty when the platform's fields are gone: the teardown plan has
        nothing to show then; parking checks the fields itself."""
        if self._woow_project_ai_paas_missing_fields():
            return self.browse()
        return self.sudo().with_context(active_test=False).search(
            [('project_namespace_hash', '!=', False), ('state', '!=', 'absent')], order='id')

    @api.model
    def _woow_project_ai_paas_cron_ids(self):
        """{xmlid: ir.cron id} of the old task AI's crons that exist."""
        crons = {}
        for xmlid in LEGACY_CRONS:
            cron = self.env.ref(xmlid, raise_if_not_found=False)
            if cron and cron._name == 'ir.cron':
                crons[xmlid] = cron.id
        return crons

    @api.model
    def _woow_project_ai_paas_lock_crons(self, cron_ids):
        """Lock those rows of `cron_ids` that can be locked now, each in its own
        savepoint: a row held by a running job is skipped (SKIP LOCKED), a row
        the cron runner updated after this transaction began raises a
        serialization failure and is skipped too. Returns the ids locked."""
        locked = set()
        for cron_id in cron_ids:
            try:
                with self.env.cr.savepoint(flush=False):
                    # log_exceptions=False: the serialization failure is expected and handled ("retry"), as Odoo's
                    # own ir.cron._try_lock does; the cursor would log it at ERROR level
                    self.env.cr.execute("SELECT id FROM ir_cron WHERE id = %s FOR NO KEY UPDATE SKIP LOCKED",
                                        [cron_id], log_exceptions=False)
                    if self.env.cr.fetchone():
                        locked.add(cron_id)
            except psycopg2.errors.SerializationFailure:
                continue
        return locked

    @api.model
    def _woow_project_ai_paas_park_crons(self):
        """Park (deactivate) the old task AI's active crons, lock-safely, once
        no session is live; record the parked xmlids. Never raises.

        Returns:
            dict: live (number of live sessions: nothing changed when > 0),
            parked (xmlids parked now), retry (xmlids held by a running job:
            call again later), missing (xmlids that do not exist), error
        """
        report = {'live': 0, 'parked': [], 'retry': [], 'missing': [], 'error': False}
        self.env.flush_all()  # pending writes of the caller are not this function's to hide
        try:
            # all the work in a savepoint: an error (its final flush included) rolls back to it, clears the caches
            # and leaves the caller's transaction usable
            with self.env.cr.savepoint():
                missing = self._woow_project_ai_paas_missing_fields()
                if missing:
                    # unknown counts as live: parking would stop the shrink path of sessions that may exist
                    report['error'] = 'unknown sessions'
                    _logger.critical("woow_ai_task_paas: the old task AI's crons are kept: live agent sessions "
                                     "cannot be counted (woow_paas_platform.agent_session has no field %s)", missing)
                    return report
                live = len(self._woow_project_ai_paas_live_sessions())
                if live:
                    report['live'] = live
                    _logger.info("woow_ai_task_paas: %s live agent session(s): the old task AI's crons are kept",
                                 live)
                    return report
                crons = self._woow_project_ai_paas_cron_ids()
                report['missing'] = [xmlid for xmlid in LEGACY_CRONS if xmlid not in crons]
                locked = self._woow_project_ai_paas_lock_crons(crons.values())
                Cron = self.env['ir.cron'].sudo().with_context(active_test=False)
                to_park = [xmlid for xmlid, cron_id in crons.items()
                           if cron_id in locked and Cron.browse(cron_id).active]
                report['retry'] = [xmlid for xmlid, cron_id in crons.items() if cron_id not in locked]
                if to_park:
                    Cron.browse([crons[xmlid] for xmlid in to_park]).write({'active': False})
                    parked = read_json_param(self.env, PARKED_PARAM, list)
                    write_json_param(self.env, PARKED_PARAM, sorted(set(parked) | set(to_park)))
                report['parked'] = to_park
        except Exception as error:  # noqa: BLE001 - rolled back to the savepoint; callers must never fail here
            report.update(parked=[], retry=[], error=type(error).__name__)
            log_unexpected(_logger, "woow_ai_task_paas: parking the old task AI's crons failed, nothing was changed",
                           (), error)
            return report
        _logger.info("woow_ai_task_paas: old task AI crons parked: %s; held by a running job (retry): %s; missing: %s",
                     report['parked'], report['retry'], report['missing'])
        return report

    @api.model
    def _woow_project_ai_paas_unpark_crons(self):
        """Re-activate the crons this module parked (and only those),
        lock-safely; held ones are reported "retry" and stay recorded. Never
        raises.

        Returns:
            dict: unparked, retry, missing (recorded xmlids that no longer exist), error
        """
        report = {'unparked': [], 'retry': [], 'missing': [], 'error': False}
        self.env.flush_all()  # pending writes of the caller are not this function's to hide
        try:
            with self.env.cr.savepoint():
                parked = read_json_param(self.env, PARKED_PARAM, list)
                if not parked:
                    return report
                crons = {}
                for xmlid in parked:
                    cron = self.env.ref(xmlid, raise_if_not_found=False) if isinstance(xmlid, str) else None
                    if cron and cron._name == 'ir.cron':
                        crons[xmlid] = cron.id
                    else:
                        report['missing'].append(xmlid)
                locked = self._woow_project_ai_paas_lock_crons(crons.values())
                unpark = [xmlid for xmlid, cron_id in crons.items() if cron_id in locked]
                report['retry'] = [xmlid for xmlid, cron_id in crons.items() if cron_id not in locked]
                if unpark:
                    self.env['ir.cron'].sudo().browse([crons[xmlid] for xmlid in unpark]).write({'active': True})
                # still parked: the ones held by a running job (call again later)
                write_json_param(self.env, PARKED_PARAM, sorted(report['retry']))
                report['unparked'] = unpark
        except Exception as error:  # noqa: BLE001 - rolled back to the savepoint; the uninstall hook must never fail
            report.update(unparked=[], retry=[], error=type(error).__name__)
            log_unexpected(_logger, "woow_ai_task_paas: unparking the old task AI's crons failed, nothing was changed",
                           (), error)
            return report
        _logger.info("woow_ai_task_paas: old task AI crons unparked: %s; held by a running job (retry): %s; "
                     "missing: %s", report['unparked'], report['retry'], report['missing'])
        return report

    @api.model
    def _woow_project_ai_paas_teardown_all(self, dry_run=True):
        """Clean-up of the old agent runtimes, run by an administrator in
        ``odoo shell`` on purpose (never on its own; it commits). Covers every
        session with a namespace hash in any state but ``absent``
        (``degraded``, ``spawning`` and ``spawn_failed`` included).

        ``dry_run`` (default): only the plan. Otherwise, per session: the
        platform's own ``_teardown_runtime()``, then ``state='absent'``,
        committed for that session; a failure is reported, never raised. At
        the end the crons are parked (lock-safely) and that is committed too.

        Returns:
            dict: plan [{id, project_id, state, hash4}]; and when not a dry
            run: done (ids), failed [{id, error}], crons (the park report)
        """
        sessions = self._woow_project_ai_paas_live_sessions()
        plan = [{'id': session.id, 'project_id': session.project_id.id or False, 'state': session.state,
                 'hash4': (session.project_namespace_hash or '')[:4]} for session in sessions]
        report = {'plan': plan}
        if dry_run:
            return report
        if not hasattr(type(self), '_teardown_runtime'):
            report.update(done=[], failed=[{'id': s['id'], 'error': 'no _teardown_runtime'} for s in plan], crons={})
            _logger.error("woow_ai_task_paas: the platform has no _teardown_runtime: nothing was torn down")
            return report
        done, failed = [], []
        for session in sessions:
            try:
                with self.env.cr.savepoint():
                    session._teardown_runtime()
                    session.write({'state': 'absent'})
                self.env.cr.commit()  # one session at a time: a later failure never undoes an earlier teardown
                done.append(session.id)
            except Exception as error:  # noqa: BLE001 - reported per session, the others go on
                failed.append({'id': session.id, 'error': type(error).__name__})
                log_unexpected(_logger, "woow_ai_task_paas: teardown of agent session %s failed", (session.id,), error)
        report.update(done=done, failed=failed, crons=self._woow_project_ai_paas_park_crons())
        self.env.cr.commit()  # the parking too (odoo shell rolls back what is not committed)
        return report
