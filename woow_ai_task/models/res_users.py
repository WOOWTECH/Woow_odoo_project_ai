# -*- coding: utf-8 -*-
"""Membership triggers of users: groups (portal access revoked included) and the active flag."""

from odoo import models

USER_ACCESS_FIELDS = {'groups_id', 'active', 'company_ids', 'company_id', 'share'}


class ResUsers(models.Model):
    _inherit = 'res.users'

    def write(self, vals):
        res = super().write(vals)
        if USER_ACCESS_FIELDS & set(vals) or any(key.startswith(('in_group_', 'sel_groups_')) for key in vals):
            self.env['discuss.channel']._woow_project_ai_sync_partners(self.sudo().partner_id)
        return res

    def unlink(self):
        partners = self.sudo().partner_id
        res = super().unlink()
        self.env['discuss.channel']._woow_project_ai_sync_partners(partners)
        return res


class ResGroups(models.Model):
    _inherit = 'res.groups'

    def write(self, vals):
        # only the users taken out of a group can lose access (Odoo adds implied
        # groups to users, it never takes them away when implied_ids change)
        before = self.sudo().users if 'users' in vals else None
        res = super().write(vals)
        if before:
            self.env['discuss.channel']._woow_project_ai_sync_partners((before - self.sudo().users).partner_id)
        return res
