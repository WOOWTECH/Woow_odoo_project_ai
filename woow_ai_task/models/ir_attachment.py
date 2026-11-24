# -*- coding: utf-8 -*-
"""Files of the task AI conversations (attachments of their messages: res_model
``discuss.channel``, res_id the chat). Odoo checks a file's write and unlink
as write access on its channel, which every member employee has, and its
delete route lets internal users delete any file they may unlink: a member
could replace or delete a customer's upload, a colleague's file or the AI's,
under their name. Only the person who uploaded a file changes or deletes it
(Odoo's own message actions run as superuser: an author deleting their own
message deletes its files)."""

from odoo import models
from odoo.exceptions import AccessError

# what a member may not change on someone else's file (its content and what it belongs to)
GUARDED_FIELDS = {'raw', 'datas', 'db_datas', 'url', 'type', 'name', 'mimetype', 'res_model', 'res_id',
                  'res_field', 'store_fname', 'checksum', 'file_size', 'public'}


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    def _woow_project_ai_foreign_files(self):
        """Those of these files that are in a task AI chat and that the current
        user did not upload (empty for superuser)."""
        if self.env.su or not self:
            return self.browse()
        files = self.sudo().filtered(lambda a: a.res_model == 'discuss.channel' and a.res_id)
        if not files:
            return self.browse()
        chats = set(self.env['discuss.channel'].sudo().with_context(active_test=False).search(
            [('id', 'in', list(set(files.mapped('res_id')))), ('woow_project_ai_task_id', '!=', False)]).ids)
        return files.filtered(lambda a: a.res_id in chats and a.create_uid.id != self.env.uid)

    def _woow_project_ai_refuse_file(self):
        raise AccessError(self.env._('Only the person who uploaded a file of a task AI conversation can change or '
                                     'delete it.'))

    def write(self, vals):
        if not self.env.su and GUARDED_FIELDS & set(vals) and self._woow_project_ai_foreign_files():
            self._woow_project_ai_refuse_file()
        return super().write(vals)

    def unlink(self):
        if not self.env.su and self._woow_project_ai_foreign_files():
            self._woow_project_ai_refuse_file()
        return super().unlink()
