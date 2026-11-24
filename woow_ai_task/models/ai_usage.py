# -*- coding: utf-8 -*-
"""Monthly project AI usage: how many replies each AI assistant gave, per
connection, surface tab (the internal or the customer AI chat of a task) and
scope (the project), and the tokens the backends reported.

A reference for invoicing by hand ("回覆次數給人工開發票參考"), never a billing
gate: nothing is limited here (limits live in the LiteLLM keys). Rows are
written only by the reply jobs and by pushes into a task chat, with one SQL
upsert in the transaction that posts the reply, and are never deleted
automatically (the call logs are kept 30 days only). System administrators
can read them; nobody edits them through the ORM.

Months are UTC calendar months (``month`` = their first day). n8n calls report
no tokens: 0 is recorded, never an estimate. The two tabs of a project are
counted apart even when they use the same AI assistant (``surface_tab`` is
part of the key).

Ported from woow_ai_contact 18.0.2.0.0 (``woow.ai.usage.month``), with the tab
in the key.
"""

from odoo import api, fields, models
from odoo.tools import sql

KEY_INDEX = 'woow_project_ai_usage_month_key_uniq'
KEY_EXPRESSIONS = ('assistant_id', 'connection_id', "COALESCE(scope_model, '')", 'COALESCE(scope_res_id, 0)',
                   "COALESCE(surface_type, '')", "COALESCE(surface_tab, '')", 'month')


class WoowProjectAiUsageMonth(models.Model):
    _name = 'woow.project.ai.usage.month'
    _description = 'Project AI Usage per Month'
    _order = 'month desc, assistant_id, id'

    month = fields.Date(string='Month', required=True, readonly=True, index=True,
                        help='First day of the month (UTC).')
    assistant_id = fields.Many2one('woow.project.ai.assistant', string='AI Assistant', required=True,
                                   readonly=True, ondelete='restrict', index=True)
    connection_id = fields.Many2one('woow.project.ai.connection', string='AI Connection', required=True,
                                    readonly=True, ondelete='restrict', index=True)
    surface_type = fields.Char(string='Used In', readonly=True,
                               help='project_task: the AI chats of the tasks.')
    surface_tab = fields.Selection(
        [('internal', 'Internal AI Chat'), ('customer', 'Customer AI Chat')], string='Tab', readonly=True,
        help='The task AI chat the replies were given in. Counted separately, even with the same AI assistant.')
    scope_model = fields.Char(string='Scope Model', readonly=True)
    scope_res_id = fields.Many2oneReference(string='Scope Record', model_field='scope_model', readonly=True)
    scope_name = fields.Char(string='Scope', compute='_compute_scope_name')
    replies_ok = fields.Integer(string='Replies', readonly=True,
                                help='Replies of the backend posted in the conversation.')
    replies_failed = fields.Integer(string='Failed Replies', readonly=True,
                                    help='Replies that failed: the user got the error message.')
    prompt_tokens = fields.Integer(string='Prompt Tokens', readonly=True)
    completion_tokens = fields.Integer(string='Completion Tokens', readonly=True)
    total_tokens = fields.Integer(string='Total Tokens', compute='_compute_total_tokens')

    def init(self):
        # one row per key; NULL scopes and tabs count as the same key (COALESCE)
        sql.create_unique_index(self.env.cr, KEY_INDEX, self._table, list(KEY_EXPRESSIONS))

    @api.depends('prompt_tokens', 'completion_tokens')
    def _compute_total_tokens(self):
        for row in self:
            row.total_tokens = (row.prompt_tokens or 0) + (row.completion_tokens or 0)

    @api.depends('scope_model', 'scope_res_id')
    def _compute_scope_name(self):
        for row in self:
            name = ''
            if row.scope_model and row.scope_res_id and row.scope_model in self.env:
                record = self.env[row.scope_model].sudo().browse(row.scope_res_id).exists()
                name = record.display_name if record else '%s,%s' % (row.scope_model, row.scope_res_id)
            row.scope_name = name

    @api.depends('assistant_id', 'month')
    def _compute_display_name(self):
        for row in self:
            row.display_name = '%s %s' % (row.assistant_id.display_name or '?',
                                          row.month.strftime('%Y-%m') if row.month else '')

    @api.model
    def _add(self, assistant, connection, surface_type=None, scope_model=None, scope_res_id=None, ok=True,
             prompt_tokens=0, completion_tokens=0, surface_tab=None):
        """Count one reply (``ok``) or one failed reply in the current UTC
        month, in the current transaction (one statement, safe against
        concurrent replies: the conflicting row is locked and incremented)."""
        if not assistant or not connection:
            return
        self.env.cr.execute("""
            INSERT INTO woow_project_ai_usage_month
                   (assistant_id, connection_id, surface_type, surface_tab, scope_model, scope_res_id, month,
                    replies_ok, replies_failed, prompt_tokens, completion_tokens,
                    create_uid, create_date, write_uid, write_date)
            VALUES (%(assistant)s, %(connection)s, %(surface)s, %(tab)s, %(scope_model)s, %(scope_res_id)s,
                    date_trunc('month', statement_timestamp() AT TIME ZONE 'UTC')::date,
                    %(ok)s, %(failed)s, %(prompt)s, %(completion)s,
                    %(uid)s, statement_timestamp() AT TIME ZONE 'UTC',
                    %(uid)s, statement_timestamp() AT TIME ZONE 'UTC')
            ON CONFLICT (assistant_id, connection_id, (COALESCE(scope_model, '')), (COALESCE(scope_res_id, 0)),
                         (COALESCE(surface_type, '')), (COALESCE(surface_tab, '')), month)
            DO UPDATE SET replies_ok = woow_project_ai_usage_month.replies_ok + EXCLUDED.replies_ok,
                          replies_failed = woow_project_ai_usage_month.replies_failed + EXCLUDED.replies_failed,
                          prompt_tokens = woow_project_ai_usage_month.prompt_tokens + EXCLUDED.prompt_tokens,
                          completion_tokens = woow_project_ai_usage_month.completion_tokens
                                              + EXCLUDED.completion_tokens,
                          write_uid = EXCLUDED.write_uid, write_date = EXCLUDED.write_date
        """, {
            'assistant': assistant.id,
            'connection': connection.id,
            'surface': surface_type or None,
            'tab': surface_tab or None,
            'scope_model': scope_model or None,
            'scope_res_id': scope_res_id or None,
            'ok': 1 if ok else 0,
            'failed': 0 if ok else 1,
            'prompt': max(0, int(prompt_tokens or 0)),
            'completion': max(0, int(completion_tokens or 0)),
            'uid': self.env.uid,
        })
        self.invalidate_model()

    @api.model
    def _add_for_channel(self, channel, assistant, connection=None, ok=True, prompt_tokens=0, completion_tokens=0):
        """Count one reply posted in `channel` by `assistant`: the surface, its
        tab and the scope come from the channel
        (``discuss.channel._woow_project_ai_surface_info`` /
        ``_woow_project_ai_usage_scope``). ``connection``: the one the reply
        was asked from (default: the assistant's current one). The caller
        handles failures."""
        channel = channel.sudo()
        assistant = assistant.sudo()
        surface = channel._woow_project_ai_surface_info() or {}
        scope_model, scope_res_id = channel._woow_project_ai_usage_scope() or (False, False)
        tab = surface.get('tab')
        self._add(assistant, connection or assistant.connection_id, surface_type=surface.get('type'),
                  scope_model=scope_model, scope_res_id=scope_res_id, ok=ok, prompt_tokens=prompt_tokens,
                  completion_tokens=completion_tokens, surface_tab=tab if isinstance(tab, str) else None)
