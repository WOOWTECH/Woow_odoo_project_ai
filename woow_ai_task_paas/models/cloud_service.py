# -*- coding: utf-8 -*-
"""A service's bound projects and its workspace decide who reads the tasks of
those projects (the platform's rules), so their task AI chats are synced
when either changes from the service side (the project side is
models/project_project.py). ``cloud_service.write`` runs on every state
change the background crons write: the keys in the values are checked
before anything is read, so such a write costs no query here.

The projects before and after are read in SQL from the binding's relation
table, never with a sudo read, so that the platform's own access checks
on these services run first and unchanged. The sync itself is fail-soft
(models/sync.py): the platform's write or delete always stands.

Not hooked: deleting a workspace cascades in the database; woow_ai_task's
hourly sweep covers it, and its rule cuts reading at once."""

from odoo import api, models
from odoo.tools import SQL

from .sync import quietly

BINDING_KEYS = {'project_ids', 'workspace_id'}


class CloudService(models.Model):
    _inherit = 'woow_paas_platform.cloud_service'

    def _woow_project_ai_paas_project_ids(self):
        """The ids of the projects bound to these services, as stored."""
        field = self._fields.get('project_ids')
        ids = [record_id for record_id in self.ids if isinstance(record_id, int)]
        if field is None or field.type != 'many2many' or not ids:
            return set()
        self.flush_recordset(['project_ids'])
        self.env.cr.execute(SQL("SELECT %s FROM %s WHERE %s IN %s", SQL.identifier(field.column2),
                                SQL.identifier(field.relation), SQL.identifier(field.column1), tuple(ids)))
        return {row[0] for row in self.env.cr.fetchall()}

    def _woow_project_ai_paas_sync(self, project_ids):
        self.env['discuss.channel']._woow_project_ai_sync_projects(
            self.env['project.project'].browse(project_ids).exists())

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = list(vals_list)  # any iterable (a one-shot generator too): read again after super()
        services = super().create(vals_list)
        if 'project_ids' in self._fields and any('project_ids' in vals for vals in vals_list):
            # a project bound at the service's creation: its tasks are read by the service's workspace from now on
            quietly(self.env, 'the task chat sync after a service create', services.ids,
                    lambda: self._woow_project_ai_paas_sync(services._woow_project_ai_paas_project_ids()))
        return services

    def write(self, vals):
        if not BINDING_KEYS & set(vals) or 'project_ids' not in self._fields:
            return super().write(vals)
        before = quietly(self.env, 'reading the bound projects before a service write', self.ids,
                         self._woow_project_ai_paas_project_ids, set())
        result = super().write(vals)
        quietly(self.env, 'the task chat sync after a service write', self.ids,
                lambda: self._woow_project_ai_paas_sync(before | self._woow_project_ai_paas_project_ids()))
        return result

    def unlink(self):
        ids = self.ids
        before = quietly(self.env, 'reading the bound projects before a service delete', ids,
                         self._woow_project_ai_paas_project_ids, set())
        result = super().unlink()
        if before:
            quietly(self.env, 'the task chat sync after a service delete', ids,
                    lambda: self._woow_project_ai_paas_sync(before))
        return result
