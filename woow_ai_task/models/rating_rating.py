# -*- coding: utf-8 -*-
"""Ratings (``rating.rating``, a dependency of project): a rating linked to a
message becomes that message's rating, which Odoo shows with sudo to every
reader of the message (the messaging menu shows it in place of the text of a
chat's last message). Outside superuser (customers rate through Odoo's
routes, which run as superuser), a rating is never linked to a message of a
task AI conversation, as stored."""

from odoo import api, models
from odoo.exceptions import AccessError

from ..tools.ids import real_ids


class RatingRating(models.Model):
    _inherit = 'rating.rating'

    def _woow_project_ai_check_messages(self):
        ids = real_ids(self)
        if not ids:
            return
        self.flush_model(['message_id'])
        self.env['mail.message'].flush_model(['model', 'res_id'])
        self.env['discuss.channel'].flush_model(['woow_project_ai_task_id'])
        self.env.cr.execute("""
            SELECT 1 FROM rating_rating r
              JOIN mail_message m ON m.id = r.message_id AND m.model = 'discuss.channel'
              JOIN discuss_channel c ON c.id = m.res_id AND c.woow_project_ai_task_id IS NOT NULL
             WHERE r.id = ANY(%s)
             LIMIT 1""", [ids])
        if self.env.cr.fetchone():
            raise AccessError(self.env._('A rating cannot be linked to a message of a task AI conversation.'))

    @api.model_create_multi
    def create(self, vals_list):
        ratings = super().create(vals_list)
        if not self.env.su:
            ratings._woow_project_ai_check_messages()
        return ratings

    def write(self, vals):
        res = super().write(vals)
        if not self.env.su and 'message_id' in vals:
            self._woow_project_ai_check_messages()
        return res
