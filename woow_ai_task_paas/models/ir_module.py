# -*- coding: utf-8 -*-
"""Never uninstall this module alone: without it the platform's old task AI
would answer again. Uninstall Project AI (woow_ai_task) instead, which
removes it too; uninstalling the platform also removes it, with the old task
AI.

The guard sits in ``button_uninstall`` (Apps, the uninstall wizard,
``button_immediate_uninstall``): an uninstall hook is too late, Odoo has
committed the "to remove" state before it runs, and a raise there would
break every later registry load."""

from odoo import models
from odoo.exceptions import UserError

BRIDGE = 'woow_ai_task_paas'
# selecting one of these with the bridge is fine: the bridge goes with them
SIDES = {'woow_ai_task', 'woow_paas_platform'}


class IrModuleModule(models.Model):
    _inherit = 'ir.module.module'

    def button_uninstall(self):
        names = set(self.mapped('name'))
        if BRIDGE in names and not names & SIDES and self.search_count(
                [('name', '=', 'woow_ai_task'), ('state', 'in', ('installed', 'to upgrade'))]):
            raise UserError(self.env._(
                '"Project AI - PaaS" cannot be uninstalled alone: uninstall "Project AI" (woow_ai_task), which '
                'removes it too. Removing it alone would bring the old task AI back.'))
        return super().button_uninstall()
