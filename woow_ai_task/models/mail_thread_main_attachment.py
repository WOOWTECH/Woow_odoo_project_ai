# -*- coding: utf-8 -*-
"""The main attachment of a record (``message_main_attachment_id``: invoices,
expenses...) is a link any writer of the record can set to any file id:
outside superuser it is never a file of a task AI conversation (a file of a
task chat stays in its chat; hr_expense would show its checksum and copy its
content into a vendor bill). Odoo's own setter runs as superuser with the
record's own attachments, which are never task-chat files."""

from odoo import api, models

from ..tools.ids import real_ids


class MailThreadMainAttachment(models.AbstractModel):
    _inherit = 'mail.thread.main.attachment'

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if not self.env.su and any(vals.get('message_main_attachment_id') for vals in vals_list):
            records._woow_project_ai_check_main_attachment()
        return records

    def write(self, vals):
        res = super().write(vals)
        if not self.env.su and vals.get('message_main_attachment_id'):
            self._woow_project_ai_check_main_attachment()
        return res

    def _woow_project_ai_check_main_attachment(self):
        """The main attachments of these records as stored (one query)."""
        ids = real_ids(self)
        if not ids:
            return
        self.flush_recordset(['message_main_attachment_id'])
        self.env.cr.execute(f'SELECT message_main_attachment_id FROM "{self._table}" '  # noqa: S608 - the model's table
                            'WHERE id = ANY(%s) AND message_main_attachment_id IS NOT NULL', [ids])
        files = [row[0] for row in self.env.cr.fetchall()]
        if files and self.env['ir.attachment']._woow_project_ai_file_chats(files):
            self.env['ir.attachment']._woow_project_ai_refuse_elsewhere('link')
