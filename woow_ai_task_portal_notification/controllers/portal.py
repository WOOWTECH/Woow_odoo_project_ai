# -*- coding: utf-8 -*-
"""The notification centre's unread conversations: those of the /my/discussions list
it links to (its own domain, which leaves the task AI chats out), not every
membership of the customer. A subclass of the centre's Discussions controller:
first in the combined portal controller whatever the import order."""

from odoo.http import request

from odoo.addons.woow_portal_notification_discuss.controllers.portal import PortalNotificationDiscuss


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
