# -*- coding: utf-8 -*-
from odoo.exceptions import UserError

from . import models

PROFILE_XMLID = 'consult_profile_crm_lead'
ROLE_PROMPT = '你是業務顧問：根據商機資料幫業務整理重點與下一步；不要承諾價格、折扣或交期。'


def pre_init_hook(env):
    """Project AI's AI form consultant (woow_ai_task 18.0.1.1.0) must be in the
    database first: an image with its code serves a database still at
    18.0.1.0.0 without the consultant's tables until `-u woow_ai_task` ran.
    Odoo runs this hook only after this module's dependencies are installed:
    where crm was missing, it is installed (and stays) even when this
    refuses."""
    env.cr.execute("SELECT to_regclass('woow_project_ai_consult_profile') IS NOT NULL")
    if not env.cr.fetchone()[0]:
        raise UserError('Update Project AI first (-u woow_ai_task, to 18.0.1.1.0), then install Project AI - CRM.')


def post_init_hook(env):
    """The setting for leads, switched off: the one that exists already (an
    administrator's own, which survives uninstalling this module) adopted and
    switched off, else a new one with the sales consultant's role. Either way it
    carries this module's XML id, so installing again never meets the
    one-setting-per-kind rule."""
    Data = env['ir.model.data'].sudo()
    if Data._xmlid_to_res_id('woow_ai_task_crm.%s' % PROFILE_XMLID, raise_if_not_found=False):
        return
    Profile = env['woow.project.ai.consult.profile'].sudo()
    profile = Profile.search([('model', '=', 'crm.lead')], limit=1)
    if profile:
        if profile.enabled:
            profile.enabled = False
    else:
        profile = Profile.create({
            'model_id': env['ir.model']._get_id('crm.lead'),
            'enabled': False,
            'role_prompt': ROLE_PROMPT,
        })
    Data.create({'module': 'woow_ai_task_crm', 'name': PROFILE_XMLID, 'model': profile._name, 'res_id': profile.id,
                 'noupdate': True})
