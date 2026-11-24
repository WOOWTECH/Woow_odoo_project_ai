# -*- coding: utf-8 -*-
"""Live messages of the task AI conversations reach only people who may read
them now. Odoo keeps a websocket's channels from its last subscribe and
polls them without any access check; the stock client does not re-subscribe
when it is removed from a conversation. So a member removed by the sync (or
at post time: the message whose post removes them is sent in the same
transaction as the removal) would keep receiving the conversation's new
messages in a page left open. Every poll (websocket dispatch, peek) is made
with the session user's environment, after the commit: the task chats among
the polled channels that this user cannot read now (the global rule, and
membership) are left out of the poll."""

from odoo import api, models


def _channel_id(channel):
    """The discuss.channel id a bus channel is about, or None: a record, a
    (record, subchannel) tuple, or their (db, model, id[, subchannel]) form."""
    if isinstance(channel, models.BaseModel):
        return channel.id if channel._name == 'discuss.channel' and len(channel) == 1 else None
    if isinstance(channel, (tuple, list)):
        if len(channel) == 2 and isinstance(channel[0], models.BaseModel):
            return _channel_id(channel[0])
        if len(channel) in (3, 4) and channel[1] == 'discuss.channel' and isinstance(channel[2], int):
            return channel[2]
    return None


class BusBus(models.Model):
    _inherit = 'bus.bus'

    @api.model
    def _poll(self, channels, last=0, ignore_ids=None):
        if not self.env.su:
            channels = self._woow_project_ai_readable_channels(channels)
        return super()._poll(channels, last=last, ignore_ids=ignore_ids)

    @api.model
    def _woow_project_ai_readable_channels(self, channels):
        channels = list(channels or ())
        ids = {cid for cid in map(_channel_id, channels) if cid}
        if not ids:
            return channels
        self.env.cr.execute("SELECT id FROM discuss_channel WHERE id = ANY(%s) AND woow_project_ai_task_id IS NOT NULL",
                            [list(ids)])
        task_chats = {row[0] for row in self.env.cr.fetchall()}
        if not task_chats:
            return channels
        readable = set()
        if self.env.uid and not self.env.user._is_public():
            # the poller's own rights: Odoo's membership rule and this module's task access rule
            readable = set(self.env['discuss.channel'].with_context(active_test=False).search(
                [('id', 'in', list(task_chats))]).ids)
        dropped = task_chats - readable
        if not dropped:
            return channels
        return [channel for channel in channels if _channel_id(channel) not in dropped]
