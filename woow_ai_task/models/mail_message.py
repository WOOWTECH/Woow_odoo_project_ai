# -*- coding: utf-8 -*-
"""Messages of the task AI conversations created, changed or deleted directly
(JSON-RPC ``mail.message`` create / write / unlink). Odoo lets anyone who can
read a channel create a message in it with any author, parent and files, the
author of a message change it, and members who can write the channel delete
any message of it: in a task chat that would put words in the AI's or a
colleague's mouth (and in the AI's history), take them out, take someone
else's file (and then delete it as the "author"), or quote any message of
the site as a reply's parent (Odoo serializes the parent with sudo). So:

* a task chat message is created only by its chat's own ``message_post``
  (which checks the post), never directly;
* no message anywhere gets a parent in a task chat unless it is in that same
  chat (created or written);
* task chat messages are never written or deleted directly; Odoo's own
  message actions (edit, delete, react, pin, star) go through other paths:
  ``_message_update_content`` (checked by the channel), reactions (refused
  for archived tasks here), pins in SQL (checked by the channel)."""

from odoo import api, models
from odoo.exceptions import AccessError, UserError

from .discuss_channel import posted_by_this_module

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

    def _woow_project_ai_check_parents(self):
        """Refuse a parent message that is in a task AI chat while this message
        is not in that same chat (Odoo serializes the parent of a channel
        message with sudo: a reply would show any task chat message to the
        reply's readers)."""
        messages = self.sudo().filtered('parent_id')
        if not messages:
            return
        chats = messages.parent_id._woow_project_ai_chat_ids()
        for message in messages:
            parent = message.parent_id
            if parent.model == 'discuss.channel' and parent.res_id in chats \
                    and (message.model, message.res_id) != (parent.model, parent.res_id):
                raise AccessError(self.env._('A message of a task AI conversation can only be answered in that '
                                             'conversation.'))

    @api.model_create_multi
    def create(self, vals_list):
        messages = super().create(vals_list)
        if not self.env.su:
            # checked on the created messages, whatever way model / res_id came in (values, default_*
            # context, a string id): only the chat's own message_post (which checked the post) creates them
            chats = messages._woow_project_ai_chat_ids()
            for message in messages.sudo():
                if message.model == 'discuss.channel' and message.res_id in chats \
                        and not posted_by_this_module(self.env, message.res_id):
                    self._woow_project_ai_refuse()
            messages._woow_project_ai_check_parents()
        return messages

    def write(self, vals):
        if not self.env.su and 'parent_id' in vals and vals['parent_id']:
            parent = self.browse(vals['parent_id']).sudo().exists()
            chats = parent._woow_project_ai_chat_ids()
            if parent and parent.res_id in chats and parent.model == 'discuss.channel' and any(
                    (message.model, message.res_id) != (parent.model, parent.res_id) for message in self.sudo()):
                raise AccessError(self.env._('A message of a task AI conversation can only be answered in that '
                                             'conversation.'))
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

    def _message_reaction(self, *args, **kwargs):
        # an archived task's chats can only be read: no reactions either
        chats = self._woow_project_ai_chat_ids()
        if chats and self.env['discuss.channel'].sudo().with_context(active_test=False).browse(list(chats)).filtered(
                lambda chat: not chat.woow_project_ai_task_id.active):
            raise UserError(self.env._('This task is archived: its AI conversations can only be read.'))
        return super()._message_reaction(*args, **kwargs)
