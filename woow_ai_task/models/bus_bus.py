# -*- coding: utf-8 -*-
"""Live messages of the task AI conversations reach only people who may read
them now. Odoo keeps a websocket's channels from its last subscribe and
polls them without any access check; the stock client does not re-subscribe
when it is removed from a conversation. So a member removed by the sync (or
at post time: the message whose post removes them is sent in the same
transaction as the removal) would keep receiving the conversation's new
messages in a page left open. Every poll (websocket dispatch, peek) is made
with the session user's environment, after the commit: the task chats with
notifications pending in this poll that this user cannot read now (the
global rule, and membership) are left out of it.

Cost: which channel ids are task chats is kept per process, on the registry
(discuss.channel._woow_project_ai_known_task_chats, shared with the message
read check), so a poll without any task chat costs nothing more; with task
chats, one query on bus_bus tells whether this poll carries anything for
them, and only then the user's readable search runs, for those chats only
(never cached: the access is checked at each poll)."""

import datetime

from odoo import api, fields, models

from odoo.addons.bus.models.bus import TIMEOUT, channel_with_db, json_dump


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
            channels = self._woow_project_ai_readable_channels(channels, last, ignore_ids)
        return super()._poll(channels, last=last, ignore_ids=ignore_ids)

    @api.model
    def _woow_project_ai_readable_channels(self, channels, last=0, ignore_ids=None):
        channels = list(channels or ())
        ids = {cid for cid in map(_channel_id, channels) if cid}
        task_chats = self.env['discuss.channel']._woow_project_ai_known_task_chats(ids) if ids else set()
        if not task_chats:
            return channels
        # anything pending for them in this poll? (the same window as Odoo's _poll)
        db_name = self.env.cr.dbname
        names = {}
        for channel in channels:
            cid = _channel_id(channel)
            if cid in task_chats:
                names[json_dump(channel_with_db(db_name, channel))] = cid
        if last == 0:
            condition = "create_date > %s"
            value = fields.Datetime.now() - datetime.timedelta(seconds=TIMEOUT)
        else:
            condition = "id > %s"
            value = last
        query = "SELECT DISTINCT channel FROM bus_bus WHERE channel = ANY(%s) AND " + condition
        params = [list(names), value]
        if ignore_ids:
            query += " AND NOT (id = ANY(%s))"
            params.append(list(ignore_ids))
        self.env.cr.execute(query, params)
        pending = {names[row[0]] for row in self.env.cr.fetchall() if row[0] in names}
        if not pending:
            return channels
        readable = set()
        if self.env.uid and not self.env.user._is_public():
            # the poller's own rights: Odoo's membership rule and this module's task access rule
            readable = set(self.env['discuss.channel'].with_context(active_test=False).search(
                [('id', 'in', list(pending))]).ids)
        dropped = pending - readable
        if not dropped:
            return channels
        return [channel for channel in channels if _channel_id(channel) not in dropped]
