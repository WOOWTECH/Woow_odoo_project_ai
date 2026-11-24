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
  for archived tasks here), pins in SQL (checked by the channel);
* a file of a task chat is never linked to a message elsewhere (created,
  written or copied; see ir_attachment.py);
* Odoo reads messages with sudo (``mail.message.fetch``) wherever it does
  not call ``read``: a copy or an export of a task chat message needs read
  access to it (onchange: see base.py)."""

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

    @api.model
    def _woow_project_ai_task_chat_of(self, ids):
        """{message id: task chat id} for those of `ids` that are messages of a
        task AI chat (one query, no message row read)."""
        ids = [i for i in ids or () if isinstance(i, int) and not isinstance(i, bool)]
        if not ids:
            return {}
        self.env.cr.execute("""
            SELECT m.id, m.res_id FROM mail_message m
              JOIN discuss_channel c ON c.id = m.res_id AND c.woow_project_ai_task_id IS NOT NULL
             WHERE m.model = 'discuss.channel' AND m.id = ANY(%s)""", [ids])
        return dict(self.env.cr.fetchall())

    def _woow_project_ai_refuse_parent(self):
        raise AccessError(self.env._('A message of a task AI conversation can only be answered in that '
                                     'conversation.'))

    def _woow_project_ai_check_parents(self):
        """Refuse a parent message that is in a task AI chat while this message
        is not in that same chat (Odoo serializes the parent of a channel
        message with sudo: a reply would show any task chat message to the
        reply's readers)."""
        self.flush_model(['parent_id', 'model', 'res_id'])
        self.env.cr.execute("SELECT id, parent_id, model, res_id FROM mail_message WHERE id = ANY(%s) "
                            "AND parent_id IS NOT NULL", [self.ids])
        rows = self.env.cr.fetchall()
        chats = self._woow_project_ai_task_chat_of([row[1] for row in rows])
        for _id, parent_id, model, res_id in rows:
            if parent_id in chats and (model, res_id) != ('discuss.channel', chats[parent_id]):
                self._woow_project_ai_refuse_parent()

    def _woow_project_ai_check_files(self):
        """Refuse files of a task AI chat linked to these messages when a message
        is not in that file's chat (ir_attachment.py)."""
        Attachment = self.env['ir.attachment']
        for message in self.sudo():
            if message.attachment_ids:
                Attachment._woow_project_ai_check_file_target(message.attachment_ids.ids, message.model,
                                                              message.res_id)

    @api.model_create_multi
    def create(self, vals_list):
        messages = super().create(vals_list)
        if not self.env.su:
            # checked on the created messages, whatever way model / res_id came in (values, default_*
            # context, a string id, a copy): only the chat's own message_post (which checked the post)
            # creates them
            chats = messages._woow_project_ai_chat_ids()
            for message in messages.sudo():
                if message.model == 'discuss.channel' and message.res_id in chats \
                        and not posted_by_this_module(self.env, message.res_id):
                    self._woow_project_ai_refuse()
            messages._woow_project_ai_check_parents()
            messages._woow_project_ai_check_files()
        return messages

    def write(self, vals):
        if not self.env.su and vals.get('parent_id'):
            chats = self._woow_project_ai_task_chat_of([vals['parent_id']])
            if chats and any((message.model, message.res_id) != ('discuss.channel', chats[vals['parent_id']])
                             for message in self.sudo()):
                self._woow_project_ai_refuse_parent()
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
        res = super().write(vals)
        if not self.env.su and ({'attachment_ids', 'model', 'res_id'} & set(vals)):
            # a file of a task chat linked to (or moved with) a message of another thread
            self._woow_project_ai_check_files()
        return res

    @api.model
    def _woow_project_ai_check_readable(self, ids):
        """Refuse the messages of a task AI chat among `ids` that the current
        user cannot read (for the paths that read a message with sudo)."""
        if self.env.su:
            return
        chats = self._woow_project_ai_task_chat_of(ids)
        if chats:
            self.browse(list(chats)).check_access('read')

    def copy_data(self, default=None):
        # Odoo 18's copy reads the copied fields with sudo (mail.message.fetch) and never checks read: a copy
        # would show any task chat message to its creator. Only someone who reads it copies it.
        self._woow_project_ai_check_readable(self.ids)
        return super().copy_data(default=default)

    def export_data(self, fields_to_export):
        # the same sudo read (Odoo lets administrators export any message)
        self._woow_project_ai_check_readable(self.ids)
        return super().export_data(fields_to_export)

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
