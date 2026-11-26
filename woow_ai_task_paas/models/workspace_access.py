# -*- coding: utf-8 -*-
"""Workspace membership rows decide who reads the tasks of a bound project
(the platform's project and task rules), so the task AI chats follow them.
woow_ai_task's own rule cuts reading at once; this keeps the member lists in
line without waiting for its hourly sweep. Only the keys in the values are
looked at before anything is read; the users before and after are read in
SQL, never with a sudo read, so that the platform's own checks on these rows
run first and unchanged. The sync is fail-soft (models/sync.py)."""

from odoo import api, models
from odoo.tools import SQL

from .sync import quietly

MEMBERSHIP_KEYS = {'user_id', 'workspace_id'}


class WorkspaceAccess(models.Model):
    _inherit = 'woow_paas_platform.workspace_access'

    def _woow_project_ai_paas_partners(self):
        """The partners of the users of these rows, as stored."""
        ids = [record_id for record_id in self.ids if isinstance(record_id, int)]
        if 'user_id' not in self._fields or not ids:
            return self.env['res.partner']
        self.flush_recordset(['user_id'])
        self.env.cr.execute(SQL("SELECT DISTINCT u.partner_id FROM %s a JOIN res_users u ON u.id = a.user_id "
                                "WHERE a.id IN %s", SQL.identifier(self._table), tuple(ids)))
        return self.env['res.partner'].browse(row[0] for row in self.env.cr.fetchall())

    @api.model_create_multi
    def create(self, vals_list):
        rows = super().create(vals_list)
        # a new row only adds access: nobody to remove, the readable task chats are computed again
        quietly(self.env, 'forgetting the readable task chats after a membership create', rows.ids,
                self.env['discuss.channel']._woow_project_ai_forget_readable)
        return rows

    def write(self, vals):
        if not MEMBERSHIP_KEYS & set(vals) or 'user_id' not in self._fields:
            return super().write(vals)
        # only administrators or sudo move a row to another user or workspace (the platform's own guard)
        Partner = self.env['res.partner']
        before = quietly(self.env, 'reading the members before a membership write', self.ids,
                         self._woow_project_ai_paas_partners, Partner)
        result = super().write(vals)
        quietly(self.env, 'the task chat sync after a membership write', self.ids,
                lambda: self.env['discuss.channel']._woow_project_ai_sync_partners(
                    before | self._woow_project_ai_paas_partners()))
        return result

    def unlink(self):
        ids = self.ids
        partners = quietly(self.env, 'reading the members before a membership delete', ids,
                           self._woow_project_ai_paas_partners, self.env['res.partner'])
        result = super().unlink()
        if partners:
            quietly(self.env, 'the task chat sync after a membership delete', ids,
                    lambda: self.env['discuss.channel']._woow_project_ai_sync_partners(partners))
        return result
