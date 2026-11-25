# -*- coding: utf-8 -*-
"""Nothing this module stores in the database needs its code: no view,
field, record rule, filter, menu, action, server action, cron or asset of
its own (its form changes are made in code, ``_get_view``), and the
platform's code reads none of its system parameters. Its only rows are the
``ir.model`` names Odoo records for the models it extends. So a database
where it is installed stays sound when Odoo runs without its code (the
platform's own deployments refuse such an image anyway: docs/PAAS.md,
"回復用的映像"). What it changes in platform rows (parked crons, restricted
old chats, the two flags left False) are plain values that the platform's
own code reads as before."""

import os

from odoo.modules.module import get_module_path
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import SQL

MODULE = 'woow_ai_task_paas'
OWN_NAMES = ('woow_ai_task_paas', 'woow_project_ai_paas')
# every stored text Odoo evaluates or renders at run time: table -> columns (constants)
STORED_CODE = {
    'ir_ui_view': ('arch_db::text', 'arch_fs', 'key'),
    'ir_rule': ('domain_force',),
    'ir_filters': ('domain', 'context'),
    'ir_act_server': ('code',),
    'ir_act_window': ('domain', 'context'),
    'ir_asset': ('path',),
    'ir_model_fields': ('name', 'related', 'depends'),
}


@tagged('post_install', '-at_install')
class TestRollbackImage(TransactionCase):

    def test_no_record_of_its_own_but_the_model_names(self):
        data = self.env['ir.model.data'].sudo().search([('module', '=', MODULE)])
        self.assertEqual(set(data.mapped('model')) - {'ir.model'}, set(),
                         sorted((row.model, row.name) for row in data))
        names = self.env['ir.model'].sudo().browse(data.mapped('res_id')).mapped('model')
        self.assertTrue(all(name in self.env for name in names), names)

    def test_no_stored_row_names_its_code(self):
        for table, columns in STORED_CODE.items():
            for name in OWN_NAMES:
                pattern = '%' + name + '%'
                condition = SQL(' OR ').join(SQL('%s ILIKE %s', SQL(column), pattern) for column in columns)
                rows = self.env.execute_query(SQL('SELECT id FROM %s WHERE %s', SQL.identifier(table), condition))
                self.assertFalse(rows, (table, name))

    def test_the_platform_names_none_of_it(self):
        """Its system parameters (woow_ai_task_paas.*) are read by its own code only: an image without it
        changes nothing the platform reads."""
        path = get_module_path('woow_paas_platform')
        self.assertTrue(path)
        for root, _dirs, files in os.walk(path):
            for name in files:
                if not name.endswith(('.py', '.xml', '.js', '.csv', '.json')):
                    continue
                with open(os.path.join(root, name), encoding='utf-8', errors='replace') as handle:
                    self.assertNotIn(MODULE, handle.read(), os.path.join(root, name))
