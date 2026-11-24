# -*- coding: utf-8 -*-
"""Files of the task AI conversations (attachments of their messages: res_model
``discuss.channel``, res_id the chat).

* Only the person who uploaded a file changes or deletes it (and system
  administrators): Odoo checks a file's write and unlink as write access on
  its channel, which every member employee has, and its delete route lets
  internal users delete any file they may unlink.
* A file of a task chat stays in its chat: it is never moved to another
  document (res_model / res_id), never linked to a message of another
  thread (a post, an edit, a message created, written or copied elsewhere),
  and never deleted "through" such a message (Odoo deletes the files of a
  message emptied by its author, or deleted by its delete route, as
  superuser; see mail_thread.py). Odoo's own message actions run as
  superuser: an author deleting their own message in the chat deletes its
  files."""

from odoo import SUPERUSER_ID, api, models
from odoo.exceptions import AccessError

# what a member may not change on someone else's file (its content and what it belongs to)
GUARDED_FIELDS = {'raw', 'datas', 'db_datas', 'url', 'type', 'name', 'mimetype', 'res_model', 'res_id',
                  'res_field', 'store_fname', 'checksum', 'file_size', 'public'}


def attachment_ids_of(value):
    """Ids of existing files in an x2many value (ids, or link / set / update commands)."""
    ids = []
    for item in value or []:
        if isinstance(item, int):
            ids.append(item)
        elif isinstance(item, (list, tuple)) and item:
            if item[0] in (1, 4) and len(item) > 1:
                ids.append(item[1])
            elif item[0] == 6 and len(item) > 2:
                ids.extend(item[2] or [])
    return [i for i in ids if isinstance(i, int) and not isinstance(i, bool)]


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    @api.model
    def _woow_project_ai_file_chats(self, ids):
        """{file id: task chat id} for those of `ids` that are files of a task AI chat."""
        ids = [i for i in ids or () if isinstance(i, int)]
        if not ids:
            return {}
        self.env.cr.execute("""
            SELECT a.id, a.res_id FROM ir_attachment a
              JOIN discuss_channel c ON c.id = a.res_id AND c.woow_project_ai_task_id IS NOT NULL
             WHERE a.res_model = 'discuss.channel' AND a.id = ANY(%s)""", [ids])
        return dict(self.env.cr.fetchall())

    @api.model
    def _woow_project_ai_refuse_elsewhere(self, action):
        if action == 'delete':
            raise AccessError(self.env._('A file of a task AI conversation stays in that conversation: it cannot '
                                         'be deleted through a message elsewhere.'))
        if action == 'move':
            raise AccessError(self.env._('A file of a task AI conversation stays in that conversation: it cannot '
                                         'be moved to another document.'))
        raise AccessError(self.env._('A file of a task AI conversation stays in that conversation: it cannot be '
                                     'added to a message elsewhere.'))

    @api.model
    def _woow_project_ai_check_file_target(self, ids, model, res_id, action='link'):
        """Refuse files of a task AI chat that would be linked to (or deleted
        through) a message of another thread (`model`, `res_id`); our own code
        (superuser) excepted: Odoo's routes run these steps with sudo, so the
        real user is checked."""
        if self.env.uid == SUPERUSER_ID:
            return
        for chat_id in self._woow_project_ai_file_chats(ids).values():
            if (model, res_id) != ('discuss.channel', chat_id):
                self._woow_project_ai_refuse_elsewhere(action)

    def _woow_project_ai_foreign_files(self):
        """Those of these files that are in a task AI chat and that the current
        user did not upload (empty for superuser and system administrators)."""
        if self.env.su or not self or self.env.is_system():
            return self.browse()
        chats = self._woow_project_ai_file_chats(self.ids)
        if not chats:
            return self.browse()
        return self.sudo().browse(list(chats)).filtered(lambda a: a.create_uid.id != self.env.uid)

    def _woow_project_ai_refuse_file(self):
        raise AccessError(self.env._('Only the person who uploaded a file of a task AI conversation can change or '
                                     'delete it.'))

    def write(self, vals):
        if not self.env.su:
            if GUARDED_FIELDS & set(vals) and self._woow_project_ai_foreign_files():
                self._woow_project_ai_refuse_file()
            if 'res_model' in vals or 'res_id' in vals:
                # nobody moves a file out of its task chat (its uploader and administrators neither)
                chats = self._woow_project_ai_file_chats(self.ids)
                for attachment in self.sudo().browse(list(chats)):
                    target = (vals.get('res_model', attachment.res_model), vals.get('res_id', attachment.res_id))
                    if target != ('discuss.channel', chats[attachment.id]):
                        self._woow_project_ai_refuse_elsewhere('move')
        return super().write(vals)

    def unlink(self):
        if not self.env.su and self._woow_project_ai_foreign_files():
            self._woow_project_ai_refuse_file()
        return super().unlink()

    def _delete_and_notify(self, message=None):
        # deleted "through" a message (Odoo's delete route, with sudo for portal users): only through a
        # message of the file's own chat (where only its author may delete it)
        if message:
            self._woow_project_ai_check_file_target(self.ids, message.sudo().model, message.sudo().res_id,
                                                    action='delete')
        return super()._delete_and_notify(message=message)
