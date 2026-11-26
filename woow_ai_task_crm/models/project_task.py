# -*- coding: utf-8 -*-
"""The task side of the CRM consultant: the link of a consultation task to
its lead. Only system administrators read or write it (web_read shows a
many2one's name with sudo); everyone else sees the 「AI 顧問對象」 summary,
which names only the leads the reader can read."""

from lxml import etree

from odoo import api, fields, models

from odoo.addons.woow_ai_task.models.consult_mixin import ROLE_PRIMARY, LinkSpec

LINK_SEQUENCE = 30


class ProjectTask(models.Model):
    _inherit = 'project.task'

    woow_project_ai_consult_lead_id = fields.Many2one(
        'crm.lead', string='Opportunity', ondelete='set null', index='btree_not_null', copy=False,
        groups='base.group_system',
        help='The lead or opportunity this consultation task is about (set by "Ask the AI Consultant"). Only people '
             'who can read it may use the task\'s AI chats once the CRM consultant is enabled.')

    @api.model
    def _woow_project_ai_consult_link_specs(self):
        return super()._woow_project_ai_consult_link_specs() + [
            LinkSpec('woow_project_ai_consult_lead_id', ROLE_PRIMARY, LINK_SEQUENCE, True)]

    @api.model
    def _get_view(self, view_id=None, view_type='form', **options):
        """The lead, in the 「AI 顧問對象」 page's group of link fields (system
        administrators only). Added here, not by an inherited view: an image
        without this module shows the task form without it."""
        arch, view = super()._get_view(view_id, view_type, **options)
        if view_type == 'form' and not arch.xpath("//field[@name='woow_project_ai_consult_lead_id']"):
            for group in arch.xpath("//group[@name='woow_project_ai_consult_targets']")[:1]:
                etree.SubElement(group, 'field', {'name': 'woow_project_ai_consult_lead_id',
                                                  'groups': 'base.group_system', 'options': "{'no_create': True}"})
        return arch, view
