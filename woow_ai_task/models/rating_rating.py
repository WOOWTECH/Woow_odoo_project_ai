# -*- coding: utf-8 -*-
"""Ratings (``rating.rating``, a dependency of project). Odoo never rates a
task AI conversation: outside superuser (customers rate through Odoo's
routes, which run as superuser), a rating is never, as stored:

* linked to a message of a task AI conversation (it becomes that message's
  rating, which Odoo shows with sudo to every reader of the message: the
  messaging menu shows it in place of the text of a chat's last message);
* about a task AI conversation (``res_model`` / ``res_id``): Odoo's public
  rating route (/rate/<token>/submit_feedback) posts the rating's notice as
  superuser, under the rating's partner, in the rated record. The chat's own
  post refuses ratings as well (discuss_channel.py)."""

from odoo import api, models
from odoo.exceptions import AccessError

from ..tools.ids import real_ids


class RatingRating(models.Model):
    _inherit = 'rating.rating'

    def _woow_project_ai_check_messages(self):
        ids = real_ids(self)
        if not ids:
            return
        self.flush_model(['message_id', 'res_model_id', 'res_model', 'res_id'])
        self.env['mail.message'].flush_model(['model', 'res_id'])
        self.env['discuss.channel'].flush_model(['woow_project_ai_task_id'])
        self.env.cr.execute("""
            SELECT 1 FROM rating_rating r
              LEFT JOIN ir_model im ON im.id = r.res_model_id
              LEFT JOIN mail_message m ON m.id = r.message_id AND m.model = 'discuss.channel'
             WHERE r.id = ANY(%s)
               AND (EXISTS (SELECT 1 FROM discuss_channel c
                             WHERE c.id = m.res_id AND c.woow_project_ai_task_id IS NOT NULL)
                    OR ((im.model = 'discuss.channel' OR r.res_model = 'discuss.channel')
                        AND EXISTS (SELECT 1 FROM discuss_channel c
                                     WHERE c.id = r.res_id AND c.woow_project_ai_task_id IS NOT NULL)))
             LIMIT 1""", [ids])
        if self.env.cr.fetchone():
            raise AccessError(self.env._('A rating cannot be about a task AI conversation or one of its '
                                         'messages.'))

    @api.model_create_multi
    def create(self, vals_list):
        ratings = super().create(vals_list)
        if not self.env.su:
            ratings._woow_project_ai_check_messages()
        return ratings

    def write(self, vals):
        res = super().write(vals)
        if not self.env.su and {'message_id', 'res_model_id', 'res_model', 'res_id'} & set(vals):
            self._woow_project_ai_check_messages()
        return res
