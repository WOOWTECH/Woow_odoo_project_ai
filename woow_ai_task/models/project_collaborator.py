# -*- coding: utf-8 -*-
"""Membership trigger: project collaborators (portal access to the project's
tasks). A new collaborator only gains access: nothing to sync (the readable
task chats of the transaction are computed again)."""

from odoo import api, models


class ProjectCollaborator(models.Model):
    _inherit = 'project.collaborator'

    @api.model_create_multi
    def create(self, vals_list):
        collaborators = super().create(vals_list)
        self.env['discuss.channel']._woow_project_ai_forget_readable()
        return collaborators

    def write(self, vals):
        projects = self.sudo().project_id
        res = super().write(vals)
        self.env['discuss.channel']._woow_project_ai_sync_projects(projects | self.sudo().project_id)
        return res

    def unlink(self):
        projects = self.sudo().project_id
        res = super().unlink()
        self.env['discuss.channel']._woow_project_ai_sync_projects(projects.exists())
        return res
