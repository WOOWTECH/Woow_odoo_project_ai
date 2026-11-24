# -*- coding: utf-8 -*-
"""Outgoing mails (``mail.mail``) delegate their content to a message
(``_inherits``) and the mail cron sends them as superuser: a mail created or
changed outside superuser (system administrators: Odoo's ACL) on a message of
a task AI conversation needs read access to that chat under the module's
rule. Odoo's own notification mails are created as superuser for recipients
the chat's post check allowed."""

from odoo import api, models
from odoo.exceptions import AccessError

from ..tools.ids import real_ids


class MailMail(models.Model):
    _inherit = 'mail.mail'

    def _woow_project_ai_check_messages(self):
        ids = real_ids(self)
        if not ids:
            return
        self.flush_model(['mail_message_id'])
        self.env.cr.execute("""
            SELECT DISTINCT m.res_id FROM mail_mail mm
              JOIN mail_message m ON m.id = mm.mail_message_id AND m.model = 'discuss.channel'
              JOIN discuss_channel c ON c.id = m.res_id AND c.woow_project_ai_task_id IS NOT NULL
             WHERE mm.id = ANY(%s)""", [ids])
        chats = {row[0] for row in self.env.cr.fetchall()}
        if chats and chats - self.env['discuss.channel']._woow_project_ai_readable_cached(chats):
            raise AccessError(self.env._('This message is in a task AI conversation you cannot read.'))

    @api.model_create_multi
    def create(self, vals_list):
        mails = super().create(vals_list)
        if not self.env.su:
            mails._woow_project_ai_check_messages()
        return mails

    def write(self, vals):
        res = super().write(vals)
        if not self.env.su and 'mail_message_id' in vals:
            self._woow_project_ai_check_messages()
        return res
