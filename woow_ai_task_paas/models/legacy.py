# -*- coding: utf-8 -*-
"""The switch of the old platform task AI, and the names this bridge keeps.

The old task AI is woow_paas_platform's own: a per-project agent pod spawned
from the /woow chat stream, its WoowBot chat, the "Task Chat: ..." channels
of `project.task.chat_enabled`, its status pills and its crons. Project AI
(woow_ai_task) replaces it, so this bridge switches it off without deleting
anything (code, data, menus and tests stay).

The switch is the system parameter ``woow_ai_task_paas.legacy_task_ai``: the
old task AI is on only while it is ``true`` or ``1`` (read with sudo,
stripped, lowercased) AND the task-chat guard of controllers/legacy.py
loaded (``TASK_CHAT_GUARD_LOADED``): without that guard the switch fails
closed (the old routes could otherwise serve Project AI's task chats). It is
not in Settings: it exists to run the platform's own tests of the old task
AI, or to bring the old task AI back on purpose (then also unpark its crons,
docs/PAAS.md); never as a rollback. neutralize removes it, uninstall deletes
it.

Every platform touchpoint of this module fails soft (docs/PAAS.md): a
platform field or method is used only after an existence check, data
functions, hooks and syncs log an unexpected error (ids, its type and code
lines, never its text) and never raise, and a superuser write is corrected
and logged instead of refused. A missing platform symbol means that part of
the old feature is gone, so the bridge does nothing there; where doing
nothing would be unsafe (counting live agent sessions), it does nothing
risky and logs CRITICAL.
"""

import json
import logging

_logger = logging.getLogger(__name__)

PARAM_PREFIX = 'woow_ai_task_paas.'
LEGACY_PARAM = 'woow_ai_task_paas.legacy_task_ai'
# {channel id: [active, group_public_id]} of the old task chats this module archived and restricted (the undo
# restores exactly these values); kept after an uninstall, so that the undo still works
RESTRICTED_PARAM = 'woow_ai_task_paas.restricted_chats'
# [channel id, ...] of the old task chats an administrator restored with the undo: never restricted again (the
# restriction runs at every upgrade); kept after an uninstall, as the record above
RELEASED_PARAM = 'woow_ai_task_paas.released_chats'
# [xmlid, ...] of the old task AI's crons this module parked (deactivated); unparked at uninstall
PARKED_PARAM = 'woow_ai_task_paas.parked_crons'
# kept after an uninstall (data an administrator may still need)
KEPT_PARAMS = (RESTRICTED_PARAM, 'woow_ai_task_paas.released_chats')

# the four crons of the old task AI (noupdate="1" in the platform: a platform upgrade never re-activates them).
# They act only on agent sessions that have a namespace hash, so they do nothing once no session is live; while
# one is, they are the shrink path (idle reaper, GC) and stay.
LEGACY_CRONS = (
    'woow_paas_platform.ir_cron_agent_session_drift',
    'woow_paas_platform.ir_cron_agent_session_idle_reaper',
    'woow_paas_platform.ir_cron_agent_session_gc',
    'woow_paas_platform.ir_cron_reach_reconciler',
)

# the name project.task._create_chat_channel gives the platform's own task chats
LEGACY_CHAT_PREFIX = 'Task Chat: '

# the old task AI's /woow routes (woow_paas_platform controllers/ai_assistant.py): refused while the switch is
# off. The upload and the stream are type='http' (HTTP 410), the others JSON-RPC (an error with the refusal).
# Every route under LEGACY_PREFIX is refused too: a new old-AI route is never served before a bridge release.
LEGACY_PREFIX = '/api/ai/'
LEGACY_ROUTES = frozenset({
    '/api/ai/agents',
    '/api/ai/chat/history',
    '/api/ai/chat/post',
    '/api/ai/chat/upload',
    '/api/ai/stream/<int:channel_id>',
    '/api/ai/connection-status',
    '/api/ai/agent-status',
    '/api/ai/agent-pod-status',
})
# the same routes by their controller methods: a request is refused when its URL rule OR its endpoint is one of
# these (another URL added to one of these methods is refused too, and the contract test reports it)
LEGACY_ENDPOINT_MODULE = 'odoo.addons.woow_paas_platform.controllers.ai_assistant'
LEGACY_ENDPOINTS = frozenset('AiAssistantController.%s' % name for name in (
    'api_ai_agents', 'api_ai_chat_history', 'api_ai_chat_post', 'api_ai_chat_upload', 'api_ai_stream',
    'api_ai_connection_status', 'api_ai_agent_status', 'api_ai_agent_pod_status'))


def legacy_endpoint(endpoint):
    """Whether a routing endpoint is one of the old task AI's 8 methods."""
    function = getattr(endpoint, 'original_endpoint', None)
    return bool(function) and getattr(function, '__module__', None) == LEGACY_ENDPOINT_MODULE \
        and getattr(function, '__qualname__', None) in LEGACY_ENDPOINTS


def legacy_rule(rule):
    """Whether a routing rule belongs to the old task AI: any URL under
    LEGACY_PREFIX, or any URL bound to one of its 8 methods."""
    return rule.rule.startswith(LEGACY_PREFIX) or legacy_endpoint(rule.endpoint)


def legacy_task_ai_enabled(env):
    """True only while the old platform task AI is switched on (see the
    module docstring): the parameter says so and the task-chat guard loaded."""
    from ..controllers import legacy as guard  # noqa: PLC0415 - the controllers import the models
    if not guard.TASK_CHAT_GUARD_LOADED:
        return False
    value = env['ir.config_parameter'].sudo().get_param(LEGACY_PARAM) or ''
    return str(value).strip().lower() in ('true', '1')


def refusal(env):
    """What the old routes answer while the switch is off (the user's language)."""
    return env._('The old task AI is turned off. Customers: please use the "Customer AI Chat" on the task page; '
                 'colleagues: the "Internal AI Chat" on the task form in the backend.')


def read_json_param(env, key, kind=dict):
    """A JSON system parameter of this module (a dict or a list); empty when
    it is missing or broken (logged: its key only)."""
    raw = env['ir.config_parameter'].sudo().get_param(key) or ''
    if not raw:
        return kind()
    try:
        value = json.loads(raw)
    except ValueError:
        _logger.error("woow_ai_task_paas: the system parameter %s is not valid JSON: treated as empty", key)
        return kind()
    if not isinstance(value, kind):
        _logger.error("woow_ai_task_paas: the system parameter %s is not a JSON %s: treated as empty", key,
                      kind.__name__)
        return kind()
    return value


def write_json_param(env, key, value):
    """Store `value` as JSON (an empty value removes the parameter)."""
    env['ir.config_parameter'].sudo().set_param(key, json.dumps(value, sort_keys=True) if value else False)
