# -*- coding: utf-8 -*-
"""Outgoing mails (``mail.mail``) delegate their content to a message
(``_inherits``) and the mail cron sends them as superuser: a mail created or
changed outside superuser (system administrators: Odoo's ACL) on a message of
a task AI conversation needs read access to that chat under the module's
rule. Odoo's own notification mails are created as superuser for recipients
the chat's post check allowed (they reuse the posted message; a mail that
would create its own message in a task chat, as the mass-mail composer's, is
refused by the message's own check, superuser included). Lists leave out the
mails of task-chat messages the user cannot read."""

from odoo import api, models
from odoo.exceptions import AccessError
from odoo.osv import expression

from ..tools.ids import real_ids


class MailMail(models.Model):
    _inherit = 'mail.mail'

    @api.model
    def _search(self, domain, offset=0, limit=None, order=None):
        # Settings > Technical > Emails (system administrators): mails of task-chat messages the user cannot read
        # are left out of lists (their subject and author are the message's, read in the user's environment, where
        # the message read check refuses them); a direct read keeps the refusal; the mail queue runs as superuser
        if not self.env.su:
            Message = self.env['mail.message'].sudo()
            unreadable = Message._search([('model', '=', 'discuss.channel'),
                                          ('res_id', 'in', self.env['discuss.channel']._woow_project_ai_unreadable_sql())])
            domain = expression.AND([domain, [('mail_message_id', 'not in', unreadable)]])
        return super()._search(domain, offset, limit, order)

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
