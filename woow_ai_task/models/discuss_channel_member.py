# -*- coding: utf-8 -*-
"""The one gate every way of joining a channel goes through."""

from odoo import api, models
from odoo.exceptions import AccessError

from .discuss_channel import is_sync


class DiscussChannelMember(models.Model):
    _inherit = 'discuss.channel.member'

    @api.model_create_multi
    def create(self, vals_list):
        # add_members, channel_join, /discuss/channel/join, /discuss/settings/mute,
        # invitation links, a direct RPC create, a channel write with member
        # commands: refused for task AI conversations unless this module's own
        # code adds the member (checked before, and on the created members)
        if is_sync(self.env):
            return super().create(vals_list)
        channel_ids = {vals.get('channel_id') for vals in vals_list if isinstance(vals.get('channel_id'), int)}
        if channel_ids and self.env['discuss.channel'].sudo().with_context(active_test=False).search_count(
                [('id', 'in', list(channel_ids)), ('woow_project_ai_task_id', '!=', False)], limit=1):
            self._woow_project_ai_refuse_member()
        members = super().create(vals_list)
        if members.sudo().channel_id.filtered('woow_project_ai_task_id'):
            self._woow_project_ai_refuse_member()
        return members

    def _woow_project_ai_notify_settings(self):
        """Tell the member's open clients their notification settings (as
        Odoo's _notify_mute and set_custom_notifications do), without
        scheduling the unmute cron: a 9999-12-31 trigger would never be
        cleaned up."""
        for member in self:
            member._bus_send_store(member.channel_id, {'mute_until_dt': member.mute_until_dt,
                                                       'custom_notifications': member.custom_notifications})

    def _woow_project_ai_refuse_member(self):
        raise AccessError(self.env._('Nobody can be added to a task AI conversation: people with access to the task '
                                     'join from the task\'s AI tab.'))
