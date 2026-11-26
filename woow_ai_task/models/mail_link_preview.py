# -*- coding: utf-8 -*-
"""Link previews of the task AI conversations' messages: shown under the
message to every member. Odoo lets Access Rights administrators hide any
preview (/mail/link_preview/hide) and create, change or delete them through
their access; for a message of a task chat only its author does (Odoo's own
preview flows run as superuser)."""

from odoo import SUPERUSER_ID, api, models
from odoo.exceptions import AccessError

from ..tools.ids import real_ids


class MailLinkPreview(models.Model):
    _inherit = 'mail.link.preview'

    def _woow_project_ai_check_author(self):
        """Refuse when these previews (as stored) belong to a task-chat message
        the real user (also under sudo) did not write."""
        if self.env.uid == SUPERUSER_ID:
            return
        ids = real_ids(self)
        if not ids:
            return
        self.flush_model(['message_id'])
        self.env.cr.execute("""
            SELECT 1 FROM mail_link_preview lp
              JOIN mail_message m ON m.id = lp.message_id AND m.model = 'discuss.channel'
              JOIN discuss_channel c ON c.id = m.res_id AND c.woow_project_ai_task_id IS NOT NULL
             WHERE lp.id = ANY(%s) AND m.author_id IS DISTINCT FROM %s
             LIMIT 1""", [ids, self.env.user.partner_id.id or None])  # (no user: nobody is the author)
        if self.env.cr.fetchone():
            raise AccessError(self.env._('Only its author changes the link previews of a message of a task AI '
                                         'conversation.'))

    @api.model_create_multi
    def create(self, vals_list):
        previews = super().create(vals_list)
        if not self.env.su:
            previews._woow_project_ai_check_author()
        return previews

    def write(self, vals):
        if not self.env.su:
            self._woow_project_ai_check_author()
        res = super().write(vals)
        if not self.env.su and 'message_id' in vals:
            self._woow_project_ai_check_author()
        return res

    def unlink(self):
        if not self.env.su:
            self._woow_project_ai_check_author()
        return super().unlink()

    def _hide_and_notify(self):
        # the hide route runs as superuser for the real user (author or Access Rights administrator)
        self._woow_project_ai_check_author()
        return super()._hide_and_notify()
