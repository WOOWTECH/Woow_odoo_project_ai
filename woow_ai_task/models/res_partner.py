# -*- coding: utf-8 -*-
"""Bot partners of project AI assistants: "bot" presence (as OdooBot) in the
task AI chats' member lists."""

from odoo import models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    def _compute_im_status(self):
        super()._compute_im_status()
        ids = [pid for pid in self.ids if isinstance(pid, int)]
        if not ids:
            return
        # which partners are AI bots is not sensitive (archived assistants too)
        self.env['woow.project.ai.assistant'].flush_model(['partner_id'])
        self.env.cr.execute("SELECT partner_id FROM woow_project_ai_assistant WHERE partner_id IN %s", (tuple(ids),))
        bots = {row[0] for row in self.env.cr.fetchall()}
        for partner in self:
            if partner.id in bots:
                partner.im_status = 'bot'
