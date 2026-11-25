# -*- coding: utf-8 -*-
"""The /woow task page's customer AI chat: whether the current
user may open the task's customer AI chat, and the URL that opens it.

The chat itself is Project AI's own: the frame loads woow_ai_task's open
route ``/my/tasks/<id>/woow_project_ai?token=...`` (a token of this task and
this session), which creates or joins the chat, then redirects to Odoo's
Discuss page ``/discuss/channel/<id>``; that page finds the channel with the
user's own rights. This route has no side effect.

The decision is woow_ai_task's (``_woow_project_ai_user_allowed``, with the
user's own rights, never sudo): the /my section's rule plus the backend
tab's own condition (a project shared with portal users) and, for an
archived task, a chat that already exists (opening one would end in a 404).
Anything else answers ``{'available': False}``, without a reason, so a
task's existence never leaks.
"""

from odoo import http
from odoo.http import request

from odoo.addons.woow_ai_task.controllers.portal import WoowProjectAiPortal
from odoo.addons.woow_ai_task.models.project_project import TAB_CUSTOMER

UNAVAILABLE = {'available': False}


class WoowProjectAiPaasWoow(http.Controller):

    @http.route('/woow_project_ai_paas/task_chat', type='json', auth='user', methods=['POST'])
    def woow_project_ai_paas_task_chat(self, task_id=None, **_kw):
        if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0:
            return dict(UNAVAILABLE)
        user = request.env.user
        if user._is_public():
            return dict(UNAVAILABLE)
        # sudo: only to find the task and its project; the decision below uses the user's own rights
        task = request.env['project.task'].sudo().browse(task_id).exists()
        if not task or not task.project_id:
            return dict(UNAVAILABLE)
        project = task.project_id
        if not project._woow_project_ai_tab_assistant(TAB_CUSTOMER) or project.privacy_visibility != 'portal':
            return dict(UNAVAILABLE)
        if not task._woow_project_ai_user_allowed(user, TAB_CUSTOMER):
            return dict(UNAVAILABLE)
        if not task.active and not task._woow_project_ai_tab_channel(TAB_CUSTOMER):
            return dict(UNAVAILABLE)
        token = WoowProjectAiPortal._woow_project_ai_open_token(task.id)
        return {
            'available': True,
            'url': '/my/tasks/%d/woow_project_ai?token=%s' % (task.id, token),
            'staff': user._is_internal(),
            'readonly': not task.active,
        }
