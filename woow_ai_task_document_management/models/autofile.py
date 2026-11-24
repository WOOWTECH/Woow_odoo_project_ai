# -*- coding: utf-8 -*-
from odoo import api, models
from odoo.osv import expression
from odoo.tools import SQL

# the task AI chats, as stored (a chat's task link is set when it is created and never changes)
TASK_CHATS = "(SELECT id FROM discuss_channel WHERE woow_project_ai_task_id IS NOT NULL)"


class WoowDocumentAutoRule(models.Model):
    _inherit = 'woow.document.auto.rule'

    @api.model
    def _woow_auto_file_checked(self, attachments, unfiled):
        # new files and files moved onto a record: never those of a task AI chat (one query, only when a
        # channel's file is among them)
        if any(attachment.res_model == 'discuss.channel' for attachment in attachments.sudo()):
            chat_files = self.env['ir.attachment']._woow_project_ai_file_chats(attachments.ids)
            if chat_files:
                attachments = attachments - attachments.browse(list(chat_files))
        return super()._woow_auto_file_checked(attachments, unfiled)

    def _woow_backfill_domain(self):
        # the existing files of the rule's kind: never those of a task AI chat
        domain = super()._woow_backfill_domain()
        if self.res_model == 'discuss.channel':
            self.env['discuss.channel'].flush_model(['woow_project_ai_task_id'])
            domain = expression.AND([domain, [('res_id', 'not in', SQL(TASK_CHATS))]])
        return domain
