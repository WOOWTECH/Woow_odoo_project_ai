# -*- coding: utf-8 -*-
"""The one gate every way of joining a channel goes through."""

from odoo import api, models
from odoo.exceptions import AccessError

from ..tools.ids import real_ids
from .discuss_channel import is_sync


class DiscussChannelMember(models.Model):
    _inherit = 'discuss.channel.member'

    @api.model_create_multi
    def create(self, vals_list):
        # add_members, channel_join, /discuss/channel/join, /discuss/settings/mute,
        # invitation links, a direct RPC create, a channel write with member
        # commands: refused for task AI conversations unless this module's own
        # code adds the member (checked on the created members as stored).
        # (res.partner / mail.guest channel_ids, many2many on the member table,
        # insert no row: Odoo's NOT NULL new_message_separator refuses it.)
        members = super().create(vals_list)
        if not is_sync(self.env):
            members._woow_project_ai_check_stored_channels()
        return members

    def _woow_project_ai_check_stored_channels(self):
        """The created members as stored (one query, whatever type the
        channel id was given as): none in a task AI conversation."""
        ids = real_ids(self)
        if not ids:
            return
        self.flush_recordset(['channel_id'])
        self.env.cr.execute("""
            SELECT 1 FROM discuss_channel_member m
              JOIN discuss_channel c ON c.id = m.channel_id AND c.woow_project_ai_task_id IS NOT NULL
             WHERE m.id = ANY(%s) LIMIT 1""", [ids])
        if self.env.cr.fetchone():
            self._woow_project_ai_refuse_member()

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
