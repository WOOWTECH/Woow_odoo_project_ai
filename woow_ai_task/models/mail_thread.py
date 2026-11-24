# -*- coding: utf-8 -*-
"""Every thread (records, channels): a file of a task AI conversation is never
linked to a message of another thread by a post or an edit, and an edit that
empties a message never deletes such a file of another thread (see
ir_attachment.py)."""

from odoo import models


class MailThread(models.AbstractModel):
    _inherit = 'mail.thread'

    def _process_attachments_for_post(self, attachments, attachment_ids, message_values):
        # posts and edits (message_post, _message_update_content, the composers) link existing files here, as
        # superuser: Odoo keeps any file id for internal users
        if attachment_ids:
            if 'res_id' in message_values:
                model, res_id = message_values['model'], message_values['res_id']
            else:
                model, res_id = self._name, self.id if len(self) == 1 else False
            self.env['ir.attachment']._woow_project_ai_check_file_target(list(attachment_ids), model, res_id)
        return super()._process_attachments_for_post(attachments, attachment_ids, message_values)

    def _message_update_content(self, message, body, attachment_ids=None, partner_ids=None, strict=True,
                                **kwargs):
        if attachment_ids is not None and not attachment_ids:
            # [] deletes every file of the message (as superuser): never a task chat file of another thread
            message_sudo = message.sudo()
            self.env['ir.attachment']._woow_project_ai_check_file_target(
                message_sudo.attachment_ids.ids, message_sudo.model, message_sudo.res_id, action='delete')
        return super()._message_update_content(message, body, attachment_ids=attachment_ids,
                                               partner_ids=partner_ids, strict=strict, **kwargs)
