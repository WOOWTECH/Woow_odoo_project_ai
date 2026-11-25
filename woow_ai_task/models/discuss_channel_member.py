# -*- coding: utf-8 -*-
"""The one gate every way of joining a channel goes through."""

from odoo import api, models
from odoo.exceptions import AccessError

from ..tools.ids import as_id, defaults_set, real_ids
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
        vals_list = list(vals_list)  # any iterable (a one-shot generator too): read again below
        members = super().create(vals_list)
        if not is_sync(self.env) and not self._woow_project_ai_ordinary_channels(vals_list):
            members._woow_project_ai_check_stored_channels()
        return members

    @api.model
    def _woow_project_ai_ordinary_channels(self, vals_list):
        """Whether these members are created in channels known not to be task
        chats (plain int ids, the registry cache of task chats: a query only
        for channels it never saw) and no default channel."""
        if defaults_set(self, ('channel_id',)):
            return False
        ids = [vals.get('channel_id') for vals in vals_list]
        if not all(isinstance(i, int) and not isinstance(i, bool) and i > 0 for i in ids):
            return False
        return not self.env['discuss.channel']._woow_project_ai_known_task_chats(set(ids))

    def unlink(self):
        # a member leaves a task chat (Odoo's Leave), or the module removes them: the readable task chats of this
        # transaction are computed again (Odoo lets a non-administrator read a group chat only as a member), and
        # a leaver's unread mentions there are marked read (the Inbox no longer lists those messages to them;
        # the module's own removal does the same itself)
        channel_of = {member.id: as_id(member.sudo().channel_id.id) for member in self}
        Channel = self.env['discuss.channel']
        chats = Channel._woow_project_ai_known_task_chats({i for i in channel_of.values() if i})
        leavers = {}
        if chats and not is_sync(self.env):
            for member in self.sudo():
                if channel_of.get(member.id) in chats and member.partner_id:
                    leavers.setdefault(channel_of[member.id], set()).add(member.partner_id.id)
        res = super().unlink()
        if chats:
            Channel._woow_project_ai_forget_readable()
            for chat_id, partner_ids in leavers.items():
                Channel.sudo().browse(chat_id)._woow_project_ai_clear_lost_notifications(
                    self.env['res.partner'].sudo().browse(sorted(partner_ids)))
        return res

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
