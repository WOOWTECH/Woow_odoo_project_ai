# -*- coding: utf-8 -*-
"""AI assistants of project AI: the persona that answers the task AI chats,
and the bot partner it posts as.

The assistant holds the persona (name, avatar, bot partner, system prompt,
error message, history length); where its brain is and how it is called is
its AI connection (``woow.project.ai.connection``), readable by system
administrators only. Project managers pick assistants on their projects (the
internal and the customer AI chat); they read names only. The push key is
stored as a SHA-256 hash only and shown once when generated.

The bot partner stays archived: it is never offered in "Start a conversation"
nor in mention suggestions, yet it posts and is read in the task chats (Odoo
reads message authors and channel members with sudo).

Ported from woow_ai_contact 18.0.2.0.0 (``woow.ai.contact``), without direct
chats, onboarding and sharing. Nothing is shared with that module.
"""

import hashlib
import hmac
import logging
import secrets

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)

DEFAULT_MAX_HISTORY = 50
MAX_MAX_HISTORY = 200
# Push keys of project AI: their own prefix (woow_ai_contact's are "wpk_"), so
# a key of one module is never taken for one of the other
PUSH_KEY_PREFIX = 'wpai_'
BOT_CATEGORY_XMLID = 'woow_ai_task.partner_category_project_ai_assistant'


def hash_push_key(key):
    return hashlib.sha256((key or '').encode()).hexdigest()


class WoowProjectAiAssistant(models.Model):
    _name = 'woow.project.ai.assistant'
    _description = 'Project AI Assistant'
    _inherit = ['image.mixin']
    _order = 'sequence, id'

    name = fields.Char(string='Name', required=True, translate=False)
    description = fields.Text(string='Description',
                              help='Shown to project managers when they choose the AI assistants of a project.')
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    partner_id = fields.Many2one(
        'res.partner', string='Bot Partner', readonly=True, copy=False,
        ondelete='restrict', index=True,
        help='Partner the AI posts as. Created automatically and kept archived; name and avatar follow the AI '
             'assistant.',
    )
    connection_id = fields.Many2one(
        'woow.project.ai.connection', string='AI Connection', required=True, ondelete='restrict', index=True,
        groups='base.group_system',
        help='Where the brain of this assistant is and how it is called: backend type, URL, key, model, '
             'timeouts. Several assistants may share one connection.',
    )
    customer_safe = fields.Boolean(
        string='Connection Safe for Customers', compute='_compute_customer_safe',
        search='_search_customer_safe',
        help='Its AI connection is safe for customers: it may answer the customer AI chat of a project. Read '
             'with administrator rights; only the yes / no is shown.')

    # --- Persona (system administrators only) ---
    system_prompt = fields.Text(string='System Prompt', groups='base.group_system',
                                help='Optional. Leave empty to let the backend decide the role.')
    max_history = fields.Integer(string='History Messages', default=DEFAULT_MAX_HISTORY,
                                 groups='base.group_system',
                                 help='Most recent messages sent as context (1-200).')
    error_message = fields.Text(string='Error Message', groups='base.group_system',
                                help='Posted when the backend fails. Empty: a default message.')
    push_key_hash = fields.Char(string='Push Key Hash', readonly=True, copy=False, index=True,
                                groups='base.group_system')
    push_key_hint = fields.Char(string='Push Key', readonly=True, copy=False,
                                groups='base.group_system')
    call_log_count = fields.Integer(compute='_compute_call_log_count', groups='base.group_system')
    project_ids = fields.One2many(
        'project.project', compute='_compute_project_ids', string='Projects', groups='base.group_system',
        help='Projects that chose this AI assistant (internal or customer AI chat).')
    job_runner_info = fields.Text(string='Reply Processing', compute='_compute_job_runner_info',
                                  groups='base.group_system',
                                  help='Where the AI replies are processed (docs/PROTOCOL.md, reply queue).')
    job_runner_warning = fields.Boolean(compute='_compute_job_runner_info', groups='base.group_system')

    _sql_constraints = [
        ('partner_uniq', 'unique(partner_id)', 'Each bot partner can belong to one AI assistant only.'),
    ]

    def _check_admin(self):
        if not self.env.su and not self.env.user.has_group('base.group_system'):
            raise AccessError(self.env._('Only system administrators can change AI assistants.'))

    def action_test_connection(self):
        """Test this assistant's AI connection (the n8n ping names the assistant)."""
        self.ensure_one()
        self._check_admin()
        return self.sudo().connection_id._test_connection(assistant=self.sudo())

    # --- Customer chats (read with administrator rights) ---

    def _compute_customer_safe(self):
        # sudo: woow.project.ai.connection - only system administrators read connections; the yes / no is not
        # sensitive
        for assistant in self:
            assistant.customer_safe = bool(assistant.sudo().connection_id.customer_safe)

    def _search_customer_safe(self, operator, value):
        if operator not in ('=', '!=') or not isinstance(value, bool):
            raise UserError(self.env._('This field can only be searched as equal to True or False.'))
        # sudo: woow.project.ai.connection - evaluated as administrator, only ids come back
        safe = self.sudo().with_context(active_test=False).search([('connection_id.customer_safe', '=', True)])
        positive = (operator == '=') == value
        return [('id', 'in' if positive else 'not in', safe.ids)]

    def _compute_project_ids(self):
        Project = self.env['project.project'].sudo().with_context(active_test=False)
        for assistant in self:
            assistant.project_ids = Project.search([
                '|', ('woow_project_ai_internal_assistant_id', '=', assistant.id),
                ('woow_project_ai_customer_assistant_id', '=', assistant.id)]) \
                if isinstance(assistant.id, int) else Project

    # --- Constraints ---

    @api.constrains('max_history')
    def _check_limits(self):
        for assistant in self.sudo():
            if not 1 <= assistant.max_history <= MAX_MAX_HISTORY:
                raise ValidationError(self.env._('History messages must be between 1 and 200.'))

    @api.constrains('partner_id')
    def _check_partner(self):
        for assistant in self.sudo():
            if assistant.partner_id and assistant.partner_id.with_context(active_test=False).user_ids:
                raise ValidationError(self.env._('The bot partner of an AI assistant cannot be a user.'))

    # --- Bot partner ---

    def _partner_vals(self):
        self.ensure_one()
        category = self.env.ref(BOT_CATEGORY_XMLID, raise_if_not_found=False)
        vals = {
            'name': self.name,
            # archived: no chat search hit, no mention suggestion; still posts in the task chats
            'active': False,
            'is_company': False,
            'type': 'contact',
        }
        if self.image_1920:
            vals['image_1920'] = self.image_1920
        if category:
            vals['category_id'] = [(4, category.id)]
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        assistants = super().create(vals_list)
        for assistant in assistants:
            if not assistant.partner_id:
                partner = self.env['res.partner'].sudo().create(assistant._partner_vals())
                assistant.sudo().partner_id = partner
        return assistants

    def write(self, vals):
        res = super().write(vals)
        if {'name', 'image_1920'} & set(vals):
            for assistant in self.sudo():
                if assistant.partner_id:
                    partner_vals = {'name': assistant.name}
                    if 'image_1920' in vals:
                        partner_vals['image_1920'] = assistant.image_1920 or False
                    assistant.partner_id.with_context(active_test=False).write(partner_vals)
        if {'active', 'connection_id'} & set(vals):
            self.env['woow.project.ai.job']._runner_cache_clear()
        return res

    def copy_data(self, default=None):
        vals_list = super().copy_data(default=default)
        for assistant, vals in zip(self, vals_list):
            vals['name'] = self.env._('%s (copy)', assistant.name)
            vals.pop('partner_id', None)
        return vals_list

    # --- Push key ---

    def action_generate_push_key(self):
        """Generate a new push key, store its hash, show it once."""
        self.ensure_one()
        self._check_admin()
        key = PUSH_KEY_PREFIX + secrets.token_urlsafe(32)
        self.sudo().write({
            'push_key_hash': hash_push_key(key),
            'push_key_hint': key[:9] + '…' + key[-4:],
        })
        _logger.info("Project AI assistant %s: push key regenerated by user %s", self.id, self.env.uid)
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Push Key'),
            'res_model': 'woow.project.ai.push.key.wizard',
            'view_mode': 'form',
            'target': 'new',
            # The key travels only in this response: the wizard is never saved.
            'context': {'default_assistant_id': self.id, 'default_push_key': key},
        }

    def action_revoke_push_key(self):
        self._check_admin()
        self.sudo().write({'push_key_hash': False, 'push_key_hint': False})
        _logger.info("Project AI assistant(s) %s: push key revoked by user %s", self.ids, self.env.uid)
        return True

    @api.model
    def _find_by_push_key(self, key):
        """Active AI assistant whose push key is `key`, else an empty recordset."""
        if not key or not key.startswith(PUSH_KEY_PREFIX):
            return self.browse()
        digest = hash_push_key(key)
        assistant = self.sudo().search([('push_key_hash', '=', digest)], limit=1)
        if assistant and hmac.compare_digest(assistant.push_key_hash or '', digest):
            return assistant
        return self.browse()

    # --- Posting ---

    def _post_text(self, channel, text):
        """Post a markdown text as the AI (no attachments)."""
        self.ensure_one()
        prepared = self._prepare_reply({'text': text}, channel=channel)
        return self._post_prepared(channel, prepared)

    # --- Languages and reply processing ---

    @api.model
    def _installed_lang(self, *codes):
        """First of `codes` that is an installed language, else the current
        language or en_US. Translating with an uninstalled code raises
        "Invalid language code" (UserError), which would abort _give_up and
        leave the job 'running'."""
        installed = {code for code, _name in self.env['res.lang'].get_installed()}
        for code in codes + (self.env.context.get('lang'),):
            if code and code in installed:
                return code
        return 'en_US'

    @api.depends_context('uid')
    def _compute_job_runner_info(self):
        from .ai_job import CONCURRENCY_PARAM, DEFAULT_CONCURRENCY  # noqa: PLC0415 - ai_job imports this module
        info = self.env['woow.project.ai.job']._job_runner()
        limit = info['cron_limit']
        env = self.env
        lines = []
        if info['mode'] == 'cron':
            lines.append(env._('Replies are processed in the background by the scheduled action '
                               '"Project AI: Run AI Replies", outside the web workers (at most %(n)s '
                               'conversations at a time).', n=env['ir.config_parameter'].sudo().get_param(
                                   CONCURRENCY_PARAM) or DEFAULT_CONCURRENCY))
        else:
            lines.append(env._('Replies are processed in a background thread of the web worker that received '
                               'the message (fallback mode): a reply still running when that worker restarts '
                               'is cut and processed again.'))
        reasons = {
            'cron_runner': env._('A cron runner is available (%(where)s).',
                                 where=env._('this server') if info['cron_runner'] == 'local'
                                 else env._('another process')),
            'no_cron_runner': env._('No cron runner was found: max_cron_threads is 0 in this server, or it '
                                    'runs with --stop-after-init, and no other process ran the scheduled action '
                                    'recently.'),
            'cron_limit_too_low': env._('The cron time limit (%(limit)s s) is shorter than the longest AI connection '
                                        'timeout plus margins (%(required)s s). Set limit_time_real_cron to at least '
                                        '%(required)s to process replies in the cron runner.',
                                        limit=limit, required=info['required']),
            'forced_thread': env._('Forced by the system parameter woow_ai_task.job_runner = thread.'),
            'forced_cron': env._('Forced by the system parameter woow_ai_task.job_runner = cron.'),
            'forced_cron_limit_too_low': env._(
                'Forced by the system parameter woow_ai_task.job_runner = cron, but the cron time limit '
                '(%(limit)s s) is shorter than the longest timeout plus margins (%(required)s s): a long reply '
                'is cut when the cron worker is stopped, and processed again.',
                limit=limit, required=info['required']),
        }
        lines.append(reasons.get(info['reason'], info['reason']))
        if limit:
            lines.append(env._('Cron time limit: %(limit)s s (%(server)s server); longest timeout: %(timeout)s s.',
                               limit=limit, server=info['server'], timeout=info['max_timeout']))
        if info['mode'] == 'cron' and info.get('shared'):
            lines.append(env._('Warning: the cron runner has %(threads)s cron thread(s) for %(dbs)s database(s), so '
                               'other scheduled actions (and databases) wait while replies run; each pass therefore '
                               'starts new replies for at most 60 seconds. max_cron_threads = 2 or more, and one '
                               'cron process per database with AI assistants, is recommended.',
                               threads=info.get('cron_threads'), dbs=info.get('dbs')))
        text = '\n'.join(lines)
        warning = info['reason'] in ('no_cron_runner', 'cron_limit_too_low', 'forced_cron_limit_too_low')
        for assistant in self:
            assistant.job_runner_info = text
            assistant.job_runner_warning = warning

    # --- Call logs ---

    def _compute_call_log_count(self):
        data = self.env['woow.project.ai.call.log'].sudo()._read_group(
            [('assistant_id', 'in', self.ids)], ['assistant_id'], ['__count'])
        counts = {assistant.id: count for assistant, count in data}
        for assistant in self:
            assistant.call_log_count = counts.get(assistant.id, 0)

    def action_view_call_logs(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('woow_ai_task.action_woow_project_ai_call_log')
        action['domain'] = [('assistant_id', '=', self.id)]
        action['context'] = {'default_assistant_id': self.id}
        return action

    def action_open_bot_partner(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'res.partner',
            'res_id': self.partner_id.id,
            'view_mode': 'form',
            'context': {'active_test': False},
        }
