# -*- coding: utf-8 -*-
"""The consult ledger: a row says that this record's data was sent to the AI
in this task chat. It lives as long as its chat, whatever happens to the
task's links (a task's deletion deletes its chats); from then on everyone who
uses the chat must be able to read the record (a deleted record: nobody).

Only this module writes it, with SQL (``_woow_project_ai_record`` while the
system prompt is built, ``_woow_project_ai_move`` for merged records): the ORM
refuses create, write and unlink for everyone, the superuser included, outside
the module's own sync mode. Inserting rows never clears the consultation
state: the ledger keys come from the registry (consult_mixin.consult_keys).
"""

from odoo import api, fields, models, tools
from odoo.exceptions import AccessError
from odoo.tools import SQL

from ..tools.ids import as_id
from .consult_mixin import LEDGER
from .discuss_channel import is_sync

INDEX_NAME = 'woow_project_ai_consult_sent_res_idx'


class WoowProjectAiConsultSent(models.Model):
    _name = LEDGER
    _description = 'AI Consultant: Records Sent to the AI'
    _log_access = False
    _order = 'id'

    channel_id = fields.Many2one('discuss.channel', string='Task AI Chat', required=True, ondelete='cascade',
                                 index=True, readonly=True)
    res_model = fields.Char(string='Record Model', required=True, readonly=True)
    res_id = fields.Integer(string='Record ID', required=True, readonly=True)

    _sql_constraints = [
        ('channel_record_uniq', 'unique(channel_id, res_model, res_id)', 'A record is listed once per chat.'),
    ]

    def _auto_init(self):
        res = super()._auto_init()
        tools.create_index(self.env.cr, INDEX_NAME, self._table, ['res_model', 'res_id'])
        return res

    @api.depends('res_model', 'res_id')
    def _compute_display_name(self):
        for row in self:
            row.display_name = '%s,%s' % (row.res_model or '', row.res_id or 0)

    # --- Only this module, with SQL ---

    def _woow_project_ai_refuse(self):
        if not is_sync(self.env):
            raise AccessError(self.env._('Only the AI consultant itself records what was sent to the AI.'))

    @api.model_create_multi
    def create(self, vals_list):
        self._woow_project_ai_refuse()
        return super().create(vals_list)

    def write(self, vals):
        self._woow_project_ai_refuse()
        return super().write(vals)

    def unlink(self):
        self._woow_project_ai_refuse()
        return super().unlink()

    @api.model
    def _woow_project_ai_record(self, channel, pairs):
        """Record that the data of these (model, id) `pairs` was sent to the AI
        in `channel`: only a task chat takes rows (the insert joins on its task
        link), each pair once. Returns the models of the new rows; when there
        are new rows the readable task chats of this transaction are computed
        again (never a cache clear)."""
        pairs = sorted({(model, res_id) for model, res_id in ((m, as_id(i)) for m, i in pairs or ())
                        if model and res_id})
        channel_id = as_id(channel.id) if channel else None
        if not pairs or not channel_id:
            return []
        self.env['discuss.channel'].flush_model(['woow_project_ai_task_id'])
        rows = self.env.execute_query(SQL("""
            INSERT INTO woow_project_ai_consult_sent (channel_id, res_model, res_id)
            SELECT c.id, v.m, v.i FROM discuss_channel c, (VALUES %s) AS v(m, i)
             WHERE c.id = %s AND c.woow_project_ai_task_id IS NOT NULL
            ON CONFLICT DO NOTHING
            RETURNING res_model""", SQL(", ").join(SQL("(%s, %s)", model, res_id) for model, res_id in pairs),
            channel_id))
        if rows:
            self.env['discuss.channel']._woow_project_ai_forget_readable()
        return [row[0] for row in rows]

    @api.model
    def _woow_project_ai_move(self, model, from_ids, to_id):
        """Records `from_ids` of `model` were merged into `to_id` (a CRM merge):
        their rows move to it (one per chat), the old rows go. Returns the ids
        of the chats concerned."""
        to_id = as_id(to_id)
        from_ids = sorted({value for value in (as_id(i) for i in from_ids or ()) if value} - {to_id})
        if not model or not to_id or not from_ids:
            return []
        moved = self.env.execute_query(SQL("""
            INSERT INTO woow_project_ai_consult_sent (channel_id, res_model, res_id)
            SELECT DISTINCT channel_id, %s, %s FROM woow_project_ai_consult_sent
             WHERE res_model = %s AND res_id = ANY(%s)
            ON CONFLICT DO NOTHING
            RETURNING channel_id""", model, to_id, model, from_ids))
        removed = self.env.execute_query(SQL("""
            DELETE FROM woow_project_ai_consult_sent WHERE res_model = %s AND res_id = ANY(%s)
            RETURNING channel_id""", model, from_ids))
        chat_ids = sorted({row[0] for row in moved} | {row[0] for row in removed})
        if chat_ids:
            self.env['discuss.channel']._woow_project_ai_forget_readable()
        return chat_ids
