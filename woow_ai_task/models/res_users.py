# -*- coding: utf-8 -*-
"""Membership triggers of users: groups (portal access revoked included) and the active flag;
and the task AI chats a user may read, kept for the transaction."""

from odoo import fields, models

USER_ACCESS_FIELDS = {'groups_id', 'active', 'company_ids', 'company_id', 'share'}


class ResUsers(models.Model):
    _inherit = 'res.users'

    # the module's rule for this user (discuss.channel._woow_project_ai_rule_of): for a system administrator the
    # compiled SQL of the task AI chats they may read (the data read at each use) and the fields it reads; for
    # anyone else the list of their own member task chats they may read. Never stored: computed once per
    # transaction and kept in the field cache (the global channel rule, the message read check and the
    # searches use it), computed again after _woow_project_ai_forget_readable
    woow_project_ai_readable_rule = fields.Json(
        string='Task AI Chats Rule', compute='_compute_woow_project_ai_readable_rule', groups='base.group_system',
        prefetch=False)

    def _compute_woow_project_ai_readable_rule(self):
        Channel = self.env['discuss.channel'].sudo()
        for user in self:
            user.woow_project_ai_readable_rule = Channel._woow_project_ai_rule_of(user)

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
        if {'users', 'implied_ids'} & set(vals):
            self.env['discuss.channel']._woow_project_ai_forget_readable()  # someone may gain access too
        if before:
            self.env['discuss.channel']._woow_project_ai_sync_partners((before - self.sudo().users).partner_id)
        return res
