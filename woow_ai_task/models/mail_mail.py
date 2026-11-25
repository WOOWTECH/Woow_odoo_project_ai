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
from odoo.tools import SQL

from ..tools.ids import defaults_set, real_ids, stores_other_than


class MailMail(models.Model):
    _inherit = 'mail.mail'

    @api.model
    def _search(self, domain, offset=0, limit=None, order=None):
        # Settings > Technical > Emails (system administrators): mails of task-chat messages the user cannot read
        # are left out of lists (their subject and author are the message's, read in the user's environment, where
        # the message read check refuses them); a direct read keeps the refusal; the mail queue runs as superuser
        if not self.env.su:
            # per mail, on its message's columns (Odoo LEFT JOINs mail_message for them): a mail of another model's
            # message matches the first leaf, one of an ordinary channel the second; no set of messages is built,
            # and the sets of task chats only when a channel message's mail is met
            Channel = self.env['discuss.channel']
            Channel.flush_model(['woow_project_ai_task_id'])
            exclusion = ['|', ('model', '!=', 'discuss.channel'), ('res_id', 'not in', SQL(
                "(SELECT id FROM discuss_channel WHERE woow_project_ai_task_id IS NOT NULL)"))]
            # (the queue is small: a system administrator's compiled sub-query of all the task chats they may read
            # stays, so that the Emails list shows every mail they may read)
            exclusion = expression.OR([exclusion] + [[('res_id', 'in', value)]
                                                     for value in Channel._woow_project_ai_readable_values()])
            domain = expression.AND([domain, exclusion])
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
        vals_list = list(vals_list)  # any iterable (a one-shot generator too): read again below
        mails = super().create(vals_list)
        if not self.env.su and not self._woow_project_ai_new_messages_elsewhere(vals_list):
            mails._woow_project_ai_check_messages()
        return mails

    @api.model
    def _woow_project_ai_new_messages_elsewhere(self, vals_list):
        """Whether each of these mails creates its own message of another model
        than discuss.channel (no query; its message's own create checks it):
        no given message, a model other than discuss.channel (a plain string,
        or none), and no default for either."""
        if defaults_set(self, ('mail_message_id', 'model')):
            return False
        return all(not vals.get('mail_message_id') and stores_other_than(vals.get('model'), 'discuss.channel')
                   for vals in vals_list)

    def write(self, vals):
        res = super().write(vals)
        if not self.env.su and 'mail_message_id' in vals:
            self._woow_project_ai_check_messages()
        return res
