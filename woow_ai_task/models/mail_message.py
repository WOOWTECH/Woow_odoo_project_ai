# -*- coding: utf-8 -*-
"""Messages of the task AI conversations created, changed or deleted directly
(JSON-RPC ``mail.message`` create / write / unlink): the same rules as a post.
Odoo lets anyone who can read a channel create a message in it with any
author, the author of a message change it, and members who can write the
channel delete any message of it: in a task chat that would put words in the
AI's or a colleague's mouth (and in the AI's history), or take them out.
Odoo's own message actions (edit, delete, react, pin, star) go through other
paths: ``_message_update_content`` as superuser, reactions, pins in SQL."""

from odoo import api, models
from odoo.exceptions import AccessError

# what Odoo itself changes on a message without superuser rights (stars, pins)
MESSAGE_WRITABLE_FIELDS = {'starred_partner_ids', 'pinned_at'}


class MailMessage(models.Model):
    _inherit = 'mail.message'

    def _woow_project_ai_chat_ids(self, extra_ids=()):
        """Ids of the task AI chats among the channels of these messages (and `extra_ids`)."""
        ids = {message.res_id for message in self.sudo() if message.model == 'discuss.channel' and message.res_id}
        ids |= set(extra_ids)
        if not ids:
            return set()
        return set(self.env['discuss.channel'].sudo().with_context(active_test=False).search(
            [('id', 'in', list(ids)), ('woow_project_ai_task_id', '!=', False)]).ids)

    def _woow_project_ai_refuse(self):
        raise AccessError(self.env._('Messages of a task AI conversation can only be written, edited and deleted '
                                     'in the conversation itself.'))

    @api.model_create_multi
    def create(self, vals_list):
        messages = super().create(vals_list)
        if not self.env.su:
            # checked on the created messages, whatever way model / res_id came in (values, default_*
            # context, a string id); message_post's own create passes here too: then the author is the
            # user's and the post was checked already (no second sync)
            chats = messages._woow_project_ai_chat_ids()
            for message in messages.sudo():
                if message.model == 'discuss.channel' and message.res_id in chats:
                    if message.author_guest_id:
                        self._woow_project_ai_refuse()  # shown with the guest's name, whatever the author
                    self.env['discuss.channel'].browse(message.res_id)._woow_project_ai_check_post(
                        message.message_type, {'author_id': message.author_id.id or False,
                                               'subtype_id': message.subtype_id.id or False,
                                               'is_internal': message.is_internal}, sync=False)
        return messages

    def write(self, vals):
        if not self.env.su and set(vals) - MESSAGE_WRITABLE_FIELDS:
            target = []
            moved_to_channel = vals.get('model') == 'discuss.channel' or (
                'model' not in vals and any(message.model == 'discuss.channel' for message in self.sudo()))
            if vals.get('res_id') and moved_to_channel:
                # moved into a channel (Odoo lets administrators only change model / res_id)
                try:
                    target = [int(vals['res_id'])]
                except (TypeError, ValueError):
                    target = []
            if self._woow_project_ai_chat_ids(target):
                self._woow_project_ai_refuse()
        return super().write(vals)

    def unlink(self):
        if not self.env.su and self._woow_project_ai_chat_ids():
            # deleting a message in the chat empties it as superuser (_message_update_content)
            self._woow_project_ai_refuse()
        return super().unlink()
