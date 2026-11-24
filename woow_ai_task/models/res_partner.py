# -*- coding: utf-8 -*-
"""Bot partners of project AI assistants: "bot" presence (as OdooBot) in the
task AI chats' member lists. A partner's company decides what its portal user
reads (Odoo's portal task rules): the readable task chats of the transaction
are computed again when it changes."""

from odoo import models

# what Odoo's portal rules read of a partner (commercial_partner_id follows parent_id / is_company)
PORTAL_ACCESS_FIELDS = {'parent_id', 'is_company'}


class ResPartner(models.Model):
    _inherit = 'res.partner'

    def write(self, vals):
        res = super().write(vals)
        if PORTAL_ACCESS_FIELDS & set(vals):
            self.env['discuss.channel']._woow_project_ai_forget_readable()
        return res

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
