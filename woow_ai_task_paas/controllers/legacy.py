# -*- coding: utf-8 -*-
"""Guarded subclasses of two platform controllers (woow_paas_platform).

* ``AiAssistantController._check_channel_access``: a task AI chat of
  Project AI is never one of the old /api/ai/ chat routes' channels, even
  while the old task AI is switched on: Project AI alone decides who reads
  its chats (a member who lost the task access is refused at once, before
  the next membership sync). This guard is what ``TASK_CHAT_GUARD_LOADED``
  reports: without it the switch of the old task AI fails closed
  (models/legacy.py). With the switch on, the platform's own old chats are
  served by the old routes to their members as before, the archived and
  restricted ones included (docs/PAAS.md).
* ``PaasProjectCustomerPortal``: the old agent card of /my/tasks/<id> and
  /my/projects/<id> creates the old "Task Chat: ..." channel and joins the
  visitor. While the switch is off its values say "disabled": no chat is
  created and nobody joins.

Controller subclasses take effect only where this module is installed
(Odoo builds the routing of each database from its installed modules'
leaf controllers). Each platform class is imported inside try/except
ImportError: a missing class is logged (ERROR) and its guard fails closed.
"""

import logging

from odoo.http import request

from ..models.legacy import legacy_task_ai_enabled

_logger = logging.getLogger(__name__)

TASK_CHAT_GUARD_LOADED = False

try:
    from odoo.addons.woow_paas_platform.controllers.ai_assistant import AiAssistantController
except ImportError:
    AiAssistantController = None
    _logger.error("woow_ai_task_paas: woow_paas_platform's AiAssistantController is missing: the old task AI "
                  "stays switched off whatever its system parameter says")

try:
    from odoo.addons.woow_paas_platform.controllers.portal import PaasProjectCustomerPortal
except ImportError:
    PaasProjectCustomerPortal = None
    _logger.error("woow_ai_task_paas: woow_paas_platform's PaasProjectCustomerPortal is missing: its old agent "
                  "card is not guarded (nothing to guard if it is gone)")


if AiAssistantController is not None and hasattr(AiAssistantController, '_check_channel_access'):

    class WoowProjectAiPaasAiAssistant(AiAssistantController):

        def _check_channel_access(self, channel):
            # sudo: whether a channel is a task AI chat is not sensitive (its content is the channel rules' business)
            if channel.sudo().exists().woow_project_ai_task_id:
                return False
            return super()._check_channel_access(channel)

    TASK_CHAT_GUARD_LOADED = True
elif AiAssistantController is not None:
    _logger.error("woow_ai_task_paas: AiAssistantController._check_channel_access is missing: the old task AI stays "
                  "switched off whatever its system parameter says")


if PaasProjectCustomerPortal is not None:

    class WoowProjectAiPaasPortal(PaasProjectCustomerPortal):

        def _paas_agent_values_for_task(self, task):
            parent = getattr(super(), '_paas_agent_values_for_task', None)
            if parent is not None and legacy_task_ai_enabled(request.env):
                return parent(task)
            project = task.sudo().project_id
            # the old card's values, disabled: no chat is created and nobody joins
            return {'paas_agent_enabled': False, 'paas_project_id': project.id or False, 'paas_channel_id': False}

        def _project_get_page_view_values(self, project, access_token, *args, **kwargs):
            values = super()._project_get_page_view_values(project, access_token, *args, **kwargs)
            if not legacy_task_ai_enabled(request.env):
                values['paas_agent_enabled'] = False
            return values
