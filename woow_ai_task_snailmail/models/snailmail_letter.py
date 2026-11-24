# -*- coding: utf-8 -*-
from odoo import api, models
from odoo.exceptions import AccessError

from odoo.addons.woow_ai_task.tools.ids import real_ids


class SnailmailLetter(models.Model):
    _inherit = 'snailmail.letter'

    def _woow_project_ai_check_files(self):
        """The letters as stored: never with a file of a task AI chat (for
        everyone: snailmail sends the document's own report)."""
        ids = real_ids(self)
        if not ids:
            return
        self.flush_model(['attachment_id'])
        self.env['ir.attachment'].flush_model(['res_model', 'res_id'])
        self.env.cr.execute("""
            SELECT 1 FROM snailmail_letter l
              JOIN ir_attachment a ON a.id = l.attachment_id AND a.res_model = 'discuss.channel'
              JOIN discuss_channel c ON c.id = a.res_id AND c.woow_project_ai_task_id IS NOT NULL
             WHERE l.id = ANY(%s)
             LIMIT 1""", [ids])
        if self.env.cr.fetchone():
            raise AccessError(self.env._('A file of a task AI conversation cannot be sent by post.'))

    @api.model_create_multi
    def create(self, vals_list):
        letters = super().create(vals_list)
        letters._woow_project_ai_check_files()
        return letters

    def write(self, vals):
        res = super().write(vals)
        if 'attachment_id' in vals:
            self._woow_project_ai_check_files()
        return res
