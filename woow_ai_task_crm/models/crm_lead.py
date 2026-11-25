# -*- coding: utf-8 -*-
"""CRM leads as AI consultant records (the template of a consultant bridge):
「詢問 AI 顧問」 on a lead opens the internal AI chat of a consultation task
linked to it (project.task.woow_project_ai_consult_lead_id).

The AI gets the fields below, read with the asker's own rights (Odoo's own
lead rules: a salesperson reads their own leads and unassigned ones, a sales
manager all of them), never the contact details, the address, the chatter
and its log notes, the files or the activities (the lead's own Notes field,
description, is sent). Leads have no portal access, so
nothing goes to the customer tab.
"""

from lxml import etree

from odoo import api, models

from odoo.addons.woow_ai_task.models.consult_mixin import LEDGER, MIXIN
from odoo.addons.woow_ai_task.models.discuss_channel import with_sync
from odoo.addons.woow_ai_task.models.project_project import TAB_INTERNAL

LINK_FIELD = 'woow_project_ai_consult_lead_id'
# what the internal AI gets (every related model is readable by every user who can read a lead: the tests check
# it); the long one is trimmed first
INTERNAL_FIELDS = ('type', 'stage_id', 'priority', 'partner_name', 'user_id', 'team_id', 'expected_revenue',
                   'probability', 'date_deadline', 'tag_ids', 'source_id', 'medium_id', 'campaign_id',
                   'lost_reason_id', 'description')
DETAIL_FIELDS = ('description',)
# never sent (contact details, address, language, properties, referral, meetings): documented and tested
NEVER_SENT = ('email_from', 'phone', 'mobile', 'contact_name', 'partner_id', 'function', 'title', 'street', 'street2',
              'zip', 'city', 'state_id', 'country_id', 'website', 'lang_id', 'lead_properties', 'referred',
              'calendar_event_ids')
# who may read a lead: the fields of Odoo's lead rules (salesperson, company) and the sales team (the company is
# a stored compute of it; sites change the noupdate rules)
ACCESS_FIELDS = frozenset(('user_id', 'team_id', 'company_id'))


class CrmLead(models.Model):
    _name = 'crm.lead'
    _inherit = ['crm.lead', MIXIN]

    @api.model
    def _get_view(self, view_id=None, view_type='form', **options):
        """「詢問 AI 顧問」 and 「開新議題」 in the header of a lead form, for employees
        while the CRM consultant is enabled (woow_project_ai_consult_enabled),
        and the 「AI 顧問」 count of the consultation tasks they can read. Added
        here, not by an inherited view: an image without this module (a
        rollback image) shows the lead form without them instead of failing
        on fields its registry does not have."""
        arch, view = super()._get_view(view_id, view_type, **options)
        if view_type != 'form' or arch.xpath("//button[@name='action_woow_project_ai_consult']"):
            return arch, view
        _ = self.env._
        header = arch.find('.//header')
        if header is not None:
            etree.SubElement(header, 'field', {'name': 'woow_project_ai_consult_enabled', 'invisible': '1',
                                               'groups': 'base.group_user'})
            etree.SubElement(header, 'button', {
                'name': 'action_woow_project_ai_consult', 'type': 'object', 'string': _('Ask the AI Consultant'),
                'class': 'oe_highlight', 'invisible': 'not woow_project_ai_consult_enabled',
                'groups': 'base.group_user'})
            etree.SubElement(header, 'button', {
                'name': 'action_woow_project_ai_consult_new', 'type': 'object', 'string': _('New Topic'),
                'invisible': 'not woow_project_ai_consult_enabled', 'groups': 'base.group_user'})
        for box in arch.xpath("//div[@name='button_box']")[:1]:
            button = etree.SubElement(box, 'button', {
                'name': 'action_woow_project_ai_consult_tasks', 'type': 'object', 'class': 'oe_stat_button',
                'icon': 'fa-comments', 'invisible': 'not woow_project_ai_consult_task_count',
                'groups': 'base.group_user'})
            etree.SubElement(button, 'field', {'name': 'woow_project_ai_consult_task_count', 'widget': 'statinfo',
                                               'string': _('AI Consultations')})
        return arch, view

    @api.model
    def _woow_project_ai_consult_fields(self, tab):
        return INTERNAL_FIELDS if tab == TAB_INTERNAL else ()

    @api.model
    def _woow_project_ai_consult_detail_fields(self, tab):
        return DETAIL_FIELDS if tab == TAB_INTERNAL else ()

    @api.model
    def _woow_project_ai_consult_access_fields(self):
        return set(ACCESS_FIELDS)

    def _woow_project_ai_consult_kind(self, lang):
        """「潛在商機」 or 「商機」: the label of the lead's type in `lang` (Odoo's own terms)."""
        self.ensure_one()
        labels = dict(self._fields['type']._description_selection(self.with_context(lang=lang).env))
        return labels.get(self.type) or super()._woow_project_ai_consult_kind(lang)

    def _woow_project_ai_consult_task_values(self):
        self.ensure_one()
        return {LINK_FIELD: self.id}

    def _woow_project_ai_consult_task_domain(self):
        self.ensure_one()
        return [(LINK_FIELD, '=', self.id)]

    def _merge_dependences(self, opportunities):
        """A merge (merge_opportunity, the convert wizard's merge, team
        assignment) keeps this lead and deletes the others later: what was
        sent to the AI about them, and their consultation tasks, move to this
        lead, as Odoo moves their chatter. From then on access to this lead
        decides."""
        merged = opportunities - self
        Channel = self.env['discuss.channel']
        chat_ids = []
        tasks = self.env['project.task']
        if merged:
            chat_ids = self.env[LEDGER].sudo()._woow_project_ai_move(self._name, merged.ids, self.id)
            tasks = self.env['project.task'].sudo().with_context(active_test=False).search(
                [(LINK_FIELD, 'in', merged.ids)])
            if tasks:
                with_sync(tasks).write({LINK_FIELD: self.id})
        res = super()._merge_dependences(opportunities)
        chats = Channel.sudo().browse(chat_ids) | Channel._woow_project_ai_chats_of(tasks)
        if chats:
            Channel._woow_project_ai_forget_readable()
            chats.exists()._woow_project_ai_sync()
        return res
