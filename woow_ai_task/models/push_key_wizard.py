# -*- coding: utf-8 -*-
from odoo import fields, models


class WoowProjectAiPushKeyWizard(models.TransientModel):
    """Shows a newly generated push key once. Never saved: the key only exists
    in the action context of the response that generated it."""
    _name = 'woow.project.ai.push.key.wizard'
    _description = 'Project AI Assistant Push Key'

    assistant_id = fields.Many2one('woow.project.ai.assistant', string='AI Assistant', readonly=True)
    push_key = fields.Char(string='Push Key', readonly=True)
