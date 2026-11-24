# -*- coding: utf-8 -*-
"""Backend call logs of project AI: status and timing only, readable by system
administrators.

Request/response content is stored only while the AI connection's "Log
Request Content" is on (debugging); turning it off erases it. A daily cron
deletes logs older than LOG_RETENTION_DAYS (the monthly usage,
woow.project.ai.usage.month, is kept).

Ported from woow_ai_contact 18.0.2.0.0 (``woow.ai.call.log``).
"""

import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

LOG_RETENTION_DAYS = 30


class WoowProjectAiCallLog(models.Model):
    _name = 'woow.project.ai.call.log'
    _description = 'Project AI Call Log'
    _order = 'id desc'

    assistant_id = fields.Many2one('woow.project.ai.assistant', string='AI Assistant', ondelete='cascade',
                                   index=True, readonly=True)
    connection_id = fields.Many2one('woow.project.ai.connection', string='AI Connection', ondelete='set null',
                                    index=True, readonly=True)
    channel_id = fields.Many2one('discuss.channel', string='Conversation', ondelete='set null',
                                 index=True, readonly=True)
    surface_type = fields.Char(string='Used In', readonly=True,
                               help='Where the AI assistant was used: project_task (a task AI chat).')
    surface_tab = fields.Selection(
        [('internal', 'Internal AI Chat'), ('customer', 'Customer AI Chat')], string='Tab', readonly=True,
        help='The task AI chat of the call.')
    scope_model = fields.Char(string='Scope Model', readonly=True)
    scope_res_id = fields.Many2oneReference(string='Scope Record', model_field='scope_model', readonly=True,
                                            help='What the call is counted for: the project of the task.')
    job_id = fields.Many2one('woow.project.ai.job', string='Job', ondelete='set null', readonly=True)
    backend_type = fields.Selection([('openai', 'LiteLLM (OpenAI compatible)'), ('n8n', 'n8n webhook'),
                                     ('push', 'Push API')], string='Backend', readonly=True)
    status = fields.Selection([
        ('ok', 'OK'),
        ('http_error', 'HTTP Error'),
        ('timeout', 'Timeout'),
        ('invalid_response', 'Invalid Response'),
        ('error', 'Error'),
    ], string='Status', required=True, index=True, readonly=True)
    http_status = fields.Integer(string='HTTP Status', readonly=True)
    duration_ms = fields.Integer(string='Duration (ms)', readonly=True)
    error = fields.Char(string='Error', readonly=True)
    prompt_tokens = fields.Integer(string='Prompt Tokens', readonly=True,
                                   help='From the "usage" of a LiteLLM answer (0 when not given).')
    completion_tokens = fields.Integer(string='Completion Tokens', readonly=True)
    total_tokens = fields.Integer(string='Total Tokens', readonly=True)
    request_payload = fields.Text(string='Request Content', readonly=True)
    response_payload = fields.Text(string='Response Content', readonly=True)

    @api.depends('assistant_id')
    def _compute_display_name(self):
        for log in self:
            log.display_name = '%s #%s' % (log.assistant_id.display_name or '?', log.id)

    @api.model
    def _erase_payloads(self, assistant_ids=None, connection_ids=None):
        domain = ['|', ('request_payload', '!=', False), ('response_payload', '!=', False)]
        if assistant_ids is not None:
            domain = [('assistant_id', 'in', list(assistant_ids))] + domain
        if connection_ids is not None:
            domain = [('connection_id', 'in', list(connection_ids))] + domain
        logs = self.sudo().search(domain)
        if logs:
            logs.write({'request_payload': False, 'response_payload': False})
        return len(logs)

    @api.model
    def _cleanup_old_logs(self, days=LOG_RETENTION_DAYS):
        cutoff = fields.Datetime.now() - timedelta(days=days)
        old = self.sudo().search([('create_date', '<', cutoff)])
        count = len(old)
        if count:
            old.unlink()
            _logger.info("Deleted %s project AI call logs older than %s days", count, days)
        self.env['woow.project.ai.job'].sudo()._cleanup_old_jobs(days)
        return count
