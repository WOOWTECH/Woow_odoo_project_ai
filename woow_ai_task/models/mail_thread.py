# -*- coding: utf-8 -*-
"""Every thread (records, channels): an edit that empties a message never
deletes a file of a task AI conversation through a message of another thread
(Odoo deletes the files of an emptied message as superuser). The links
themselves (a task-chat file on a message elsewhere) are refused on the
stored rows (mail_message.py), whatever route made them."""

from odoo import models


class MailThread(models.AbstractModel):
    _inherit = 'mail.thread'

    def _message_update_content(self, message, body, attachment_ids=None, partner_ids=None, strict=True,
                                **kwargs):
        if attachment_ids is not None and not attachment_ids:
            # [] deletes every file of the message (as superuser): never a task chat file of another thread
            message_sudo = message.sudo()
            self.env['ir.attachment']._woow_project_ai_check_file_target(
                message_sudo.attachment_ids.ids, message_sudo.model, message_sudo.res_id, action='delete')
        return super()._message_update_content(message, body, attachment_ids=attachment_ids,
                                               partner_ids=partner_ids, strict=strict, **kwargs)
