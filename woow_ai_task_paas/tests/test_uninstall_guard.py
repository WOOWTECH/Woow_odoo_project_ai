# -*- coding: utf-8 -*-
"""Never uninstall the bridge alone (the old task AI would come back); with
Project AI (or with the platform) it goes along."""

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestUninstallGuard(TransactionCase):

    def module(self, name):
        return self.env['ir.module.module'].search([('name', '=', name)])

    def test_the_bridge_alone_is_refused(self):
        bridge = self.module('woow_ai_task_paas')
        with self.assertRaises(UserError) as caught:
            bridge.button_uninstall()
        self.assertIn('cannot be uninstalled alone', str(caught.exception))
        self.assertEqual(bridge.state, 'installed')

    def test_with_project_ai_it_goes_along(self):
        core, bridge = self.module('woow_ai_task'), self.module('woow_ai_task_paas')
        core.button_uninstall()
        self.assertEqual(core.state, 'to remove')
        self.assertEqual(bridge.state, 'to remove')
        self.assertEqual(self.module('woow_paas_platform').state, 'installed')

    def test_with_the_platform_it_goes_along(self):
        bridge, platform = self.module('woow_ai_task_paas'), self.module('woow_paas_platform')
        (bridge | platform).button_uninstall()
        self.assertEqual(bridge.state, 'to remove')
        self.assertEqual(self.module('woow_ai_task').state, 'installed')
