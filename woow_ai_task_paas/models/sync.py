# -*- coding: utf-8 -*-
"""The task chat membership syncs this module adds to the platform's own
writes (cloud services, workspace membership rows, project bindings) are
fail-soft. The platform's write or delete has happened and stays: the sync
runs in a savepoint, and an unexpected error there is logged (the ids, the
exception's type and code lines, never its text) and swallowed.
woow_ai_task's hourly sweep then syncs what was missed, and its rule cuts
reading at once. Concurrency errors (serialization failure, deadlock, lock
not available) propagate, so that Odoo retries the whole request, as
woow_ai_task's own triggers do."""

import logging

from odoo.service.model import PG_CONCURRENCY_EXCEPTIONS_TO_RETRY

from odoo.addons.woow_ai_task.tools.logs import log_unexpected

_logger = logging.getLogger(__name__)


def quietly(env, what, ids, function, default=None):
    """Return ``function()``, run in a savepoint; on an unexpected error, log
    it with `what` and `ids` and return `default`. The caller's pending work
    (ORM writes and precommit callbacks) is flushed first, outside: an error
    of theirs stays theirs."""
    env.cr.flush()
    try:
        with env.cr.savepoint():
            return function()
    except PG_CONCURRENCY_EXCEPTIONS_TO_RETRY:
        raise
    except Exception as error:  # noqa: BLE001 - logged; the hourly sweep covers a missed sync
        log_unexpected(_logger, "woow_ai_task_paas: %s failed for %s; woow_ai_task's hourly sweep will sync the task "
                       "chats", (what, ids), error)
        return default
