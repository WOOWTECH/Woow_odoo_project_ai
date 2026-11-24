# -*- coding: utf-8 -*-
"""Membership trigger: project collaborators (portal access to the project's
tasks). A new collaborator only gains access: nothing to sync."""

from odoo import models


class ProjectCollaborator(models.Model):
    _inherit = 'project.collaborator'

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
