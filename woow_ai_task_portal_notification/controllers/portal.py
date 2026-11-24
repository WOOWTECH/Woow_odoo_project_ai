# -*- coding: utf-8 -*-
"""The notification centre and the task AI chats.

* Its unread conversations are those of the /my/discussions list it links to
  (its own domain, which leaves the task AI chats out), not every membership
  of the customer: a count the list cannot clear is never shown.
* The unread messages of the customer's task AI chats get their own notice
  (/my/notifications) and /my card, linking to the task page (one task) or
  to the customer's tasks: a later answer in a chat (a colleague's, after
  the AI could not answer) is not missed.

A subclass of the centre's Discussions controller: first in the combined
portal controller whatever the import order."""

from odoo import _, http
from odoo.http import request

from odoo.addons.woow_portal_notification.controllers.portal import PARAM_HOME_SUMMARY, _param_enabled
from odoo.addons.woow_portal_notification_discuss.controllers.portal import PortalNotificationDiscuss

TASKS_URL = '/my/tasks'
TASK_CHAT_ANCHOR = 'woow_project_ai_chat'
UNREAD_URL = '/my/woow_project_ai/unread'
UNREAD_COUNTER = 'woow_project_ai_unread_chat_count'


class WoowProjectAiPortalNotification(PortalNotificationDiscuss):

    def _wpn_unread_conversations(self):
        channels = request.env['discuss.channel'].sudo().search(self._get_portal_discussion_domain())
        if not channels:
            return 0
        members = request.env['discuss.channel.member'].sudo().search([
            ('partner_id', '=', request.env.user.partner_id.id),
            ('channel_id', 'in', channels.ids),
        ])
        return sum(members.mapped('message_unread_counter'))

    def _woow_project_ai_unread_by_task(self):
        """{task id: unread messages} in the task AI chats the portal user is
        a member of and can read now (searched as the user: the global rule
        keeps the chats of the tasks they can read)."""
        if not request.env.user._is_portal():
            return {}
        partner = request.env.user.partner_id
        chats = request.env['discuss.channel'].search([
            ('woow_project_ai_task_id', '!=', False),
            ('channel_member_ids', 'any', [('partner_id', '=', partner.id)]),
        ])
        unread = {}
        if chats:
            members = request.env['discuss.channel.member'].sudo().search([
                ('partner_id', '=', partner.id), ('channel_id', 'in', chats.ids)])
            for member in members:
                if member.message_unread_counter:
                    task_id = member.channel_id.woow_project_ai_task_id.id
                    unread[task_id] = unread.get(task_id, 0) + member.message_unread_counter
        return unread

    @staticmethod
    def _woow_project_ai_unread_url(unread):
        if len(unread) == 1:
            return '%s/%s#%s' % (TASKS_URL, next(iter(unread)), TASK_CHAT_ANCHOR)
        return TASKS_URL

    def _wpn_extra_entries(self):
        entries = super()._wpn_extra_entries()
        unread = self._woow_project_ai_unread_by_task()
        if unread:
            entries.append({
                'icon': 'fa-commenting-o',
                'text': _('%(count)s unread messages in your task AI chats', count=sum(unread.values())),
                'url': self._woow_project_ai_unread_url(unread),
                'link_label': _('Open the task') if len(unread) == 1 else _('Open your tasks'),
            })
        return entries

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if UNREAD_COUNTER in (counters or ()):
            shown = request.env.user._is_portal() and _param_enabled(PARAM_HOME_SUMMARY)
            values[UNREAD_COUNTER] = sum(self._woow_project_ai_unread_by_task().values()) if shown else 0
        return values

    @http.route(UNREAD_URL, type='http', auth='user', website=True, methods=['GET'])
    def woow_project_ai_unread(self, **_kw):
        """The /my card: the task page when one task has unread messages, else the tasks."""
        return request.redirect(self._woow_project_ai_unread_url(self._woow_project_ai_unread_by_task()))
