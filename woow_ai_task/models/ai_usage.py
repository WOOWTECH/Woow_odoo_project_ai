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

import logging

from odoo import SUPERUSER_ID, api, fields, models
from odoo.tools import sql

from ..tools.logs import log_unexpected

_logger = logging.getLogger(__name__)

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
    project_id = fields.Many2one('project.project', string='Project', readonly=True, index=True,
                                 ondelete='set null',
                                 help='The project of the task chats (set with the row; empty once the project is '
                                      'deleted, the scope keeps its id).')
    replies_ok = fields.Integer(string='Replies', readonly=True,
                                help='Replies of the backend posted in the conversation.')
    replies_failed = fields.Integer(string='Failed Replies', readonly=True,
                                    help='Replies that failed: the user got the error message.')
    prompt_tokens = fields.Integer(string='Prompt Tokens', readonly=True)
    completion_tokens = fields.Integer(string='Completion Tokens', readonly=True)
    # kept by the same upsert as the other counters (stored: summed in the list and its groups)
    total_tokens = fields.Integer(string='Total Tokens', readonly=True)

    def init(self):
        # one row per key; NULL scopes and tabs count as the same key (COALESCE)
        sql.create_unique_index(self.env.cr, KEY_INDEX, self._table, list(KEY_EXPRESSIONS))

    @api.depends('scope_model', 'scope_res_id')
    def _compute_scope_name(self):
        for row in self:
            name = ''
            if row.scope_model and row.scope_res_id and row.scope_model in self.env:
                record = self.env[row.scope_model].sudo().with_context(active_test=False).browse(
                    row.scope_res_id).exists()
                name = record.display_name if record else self.env._('Deleted project (#%(id)s)',
                                                                      id=row.scope_res_id)
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
        # grouped by in the usage list (the scope itself is a reference, not a relation)
        project = self._woow_project_ai_scope_project(scope_model, scope_res_id)
        self.env.cr.execute("""
            INSERT INTO woow_project_ai_usage_month
                   (assistant_id, connection_id, surface_type, surface_tab, scope_model, scope_res_id, project_id,
                    month, replies_ok, replies_failed, prompt_tokens, completion_tokens, total_tokens,
                    create_uid, create_date, write_uid, write_date)
            VALUES (%(assistant)s, %(connection)s, %(surface)s, %(tab)s, %(scope_model)s, %(scope_res_id)s,
                    %(project)s, date_trunc('month', statement_timestamp() AT TIME ZONE 'UTC')::date,
                    %(ok)s, %(failed)s, %(prompt)s, %(completion)s, %(prompt)s + %(completion)s,
                    %(uid)s, statement_timestamp() AT TIME ZONE 'UTC',
                    %(uid)s, statement_timestamp() AT TIME ZONE 'UTC')
            ON CONFLICT (assistant_id, connection_id, (COALESCE(scope_model, '')), (COALESCE(scope_res_id, 0)),
                         (COALESCE(surface_type, '')), (COALESCE(surface_tab, '')), month)
            DO UPDATE SET replies_ok = woow_project_ai_usage_month.replies_ok + EXCLUDED.replies_ok,
                          replies_failed = woow_project_ai_usage_month.replies_failed + EXCLUDED.replies_failed,
                          prompt_tokens = woow_project_ai_usage_month.prompt_tokens + EXCLUDED.prompt_tokens,
                          completion_tokens = woow_project_ai_usage_month.completion_tokens
                                              + EXCLUDED.completion_tokens,
                          total_tokens = woow_project_ai_usage_month.total_tokens + EXCLUDED.total_tokens,
                          project_id = COALESCE(EXCLUDED.project_id, woow_project_ai_usage_month.project_id),
                          write_uid = EXCLUDED.write_uid, write_date = EXCLUDED.write_date
        """, {
            'assistant': assistant.id,
            'connection': connection.id,
            'surface': surface_type or None,
            'tab': surface_tab or None,
            'scope_model': scope_model or None,
            'scope_res_id': scope_res_id or None,
            'project': project.id or None,
            'ok': 1 if ok else 0,
            'failed': 0 if ok else 1,
            'prompt': max(0, int(prompt_tokens or 0)),
            'completion': max(0, int(completion_tokens or 0)),
            'uid': self.env.uid,
        })
        self.invalidate_model()

    @api.model
    def _add_for_channel_after_commit(self, channel, assistant, connection=None):
        """Count one reply posted in the current transaction (a push) once it
        is committed, on a READ COMMITTED cursor of its own: in the request's
        REPEATABLE READ transaction the upsert fails (a serialization failure)
        whenever a reply of the same month and tab was counted meanwhile.
        Failures are logged."""
        db_name = self.env.cr.dbname
        ids = (channel.id, assistant.id, connection.id if connection else False)

        def count_push():
            from .ai_job import _job_cursor  # noqa: PLC0415 - ai_job imports this module
            try:
                with _job_cursor(db_name) as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    env['woow.project.ai.usage.month']._add_for_channel(
                        env['discuss.channel'].browse(ids[0]), env['woow.project.ai.assistant'].browse(ids[1]),
                        env['woow.project.ai.connection'].browse(ids[2]) if ids[2] else None)
            except Exception as error:  # noqa: BLE001 - the pushed message stays
                log_unexpected(_logger, "Project AI assistant %s: could not record the usage of a push", (ids[1],),
                               error)

        count_push.woow_project_ai_usage = ids
        self.env.cr.postcommit.add(count_push)

    @api.model
    def _woow_project_ai_scope_project(self, scope_model, scope_res_id):
        if scope_model != 'project.project' or not scope_res_id:
            return self.env['project.project']
        return self.env['project.project'].sudo().with_context(active_test=False).browse(scope_res_id).exists()

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
