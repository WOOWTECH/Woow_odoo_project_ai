# -*- coding: utf-8 -*-
"""Install / uninstall hooks: they log and never raise."""

import logging

from odoo.addons.woow_ai_task.tools.logs import log_unexpected

from .models.legacy import KEPT_PARAMS, PARAM_PREFIX, PARKED_PARAM

_logger = logging.getLogger(__name__)

SESSION_MODEL = 'woow_paas_platform.agent_session'


def post_init_hook(env):
    """Park the old task AI's crons (lock-safely, and only once no session is
    live: a database with live sessions parks them after their teardown)."""
    try:
        if SESSION_MODEL in env:
            env[SESSION_MODEL]._woow_project_ai_paas_park_crons()
        else:
            _logger.info("woow_ai_task_paas: no %s model: no old task AI cron to park", SESSION_MODEL)
    except Exception as error:  # noqa: BLE001 - an install must never fail here
        log_unexpected(_logger, "woow_ai_task_paas: parking the old task AI's crons at install failed", (), error)


def uninstall_hook(env):
    """Unpark the crons this module parked, then delete its system parameters,
    except the records of the old task chats (restricted, and restored by an
    administrator): the chats stay as they are (they are data), and the
    records keep the undo possible after the uninstall (docs/PAAS.md). The
    record of parked crons stays too when some could not be unparked (held by
    a running job, or an error): those crons are logged."""
    keep = set(KEPT_PARAMS)
    try:
        report = env[SESSION_MODEL]._woow_project_ai_paas_unpark_crons() if SESSION_MODEL in env else {}
        if report.get('retry') or report.get('error'):
            keep.add(PARKED_PARAM)
            # a person activates them (a running job held one: expected, so a warning; an error: logged as such)
            _logger.log(logging.ERROR if report.get('error') else logging.WARNING,
                        "woow_ai_task_paas: old task AI crons still parked after the uninstall: %s (%s); activate "
                        "them in Settings > Technical > Scheduled Actions", report.get('retry'),
                        report.get('error') or 'held by a running job')
    except Exception as error:  # noqa: BLE001 - an uninstall must never fail here
        keep.add(PARKED_PARAM)
        log_unexpected(_logger, "woow_ai_task_paas: unparking the old task AI's crons failed", (), error)
    try:
        with env.cr.savepoint():
            params = env['ir.config_parameter'].sudo().search([('key', '=like', PARAM_PREFIX + '%')])
            params = params.filtered(lambda param: param.key.startswith(PARAM_PREFIX) and param.key not in keep)
            if params:
                params.unlink()
    except Exception as error:  # noqa: BLE001 - an uninstall must never fail here
        log_unexpected(_logger, "woow_ai_task_paas: deleting the module's system parameters failed", (), error)
