# -*- coding: utf-8 -*-
"""「AI 顧問設定」: one profile per consulted model (system administrators
only), off by default: whether staff may ask the AI consultant about records
of that model, the consultation project, a role prompt for the internal AI.

The profiles also hold the consultation state of the database
(``_woow_project_ai_consult_state``), which every reader of a task chat
consults: an ormcache cleared only by profile writes (and again after their
commit; other processes clear theirs at their next request or cron callback,
as for Odoo's record rules). A job never writes a profile.
"""

import logging

from odoo import api, fields, models, tools
from odoo.addons.base.models.ir_model import MODULE_UNINSTALL_FLAG
from odoo.exceptions import UserError, ValidationError
from odoo.osv import expression

from .consult_mixin import (
    PROFILE, UNUSED, State, company_ids, consult_models, consult_state, is_consult_model, model_tabs)
from .project_project import TAB_CUSTOMER, TAB_INTERNAL

_logger = logging.getLogger(__name__)

MAX_ROLE_PROMPT = 1000
PREVIEW_MAX_CHATS = 2000
PREVIEW_MAX_NAMES = 200
# set on the registry once both tables exist (they never go away while this code is loaded)
TABLES_READY_KEY = '_woow_project_ai_consult_tables_ready'
SYNC_CRON = 'woow_ai_task.ir_cron_project_ai_sync_members'


class WoowProjectAiConsultProfile(models.Model):
    _name = PROFILE
    _description = 'AI Consultant Settings'
    _order = 'model, id'
    _rec_name = 'model_id'

    model_id = fields.Many2one(
        'ir.model', string='Record Type', required=True, ondelete='cascade',
        domain=lambda self: [('model', 'in', sorted(consult_models(self.env)))],
        help='The kind of record staff may ask the AI consultant about. Only kinds with an AI consultant module '
             '(for example "Project AI - CRM") can be chosen.')
    model = fields.Char(related='model_id.model', store=True, index=True, string='Model')
    enabled = fields.Boolean(
        string='Enable the AI Consultant', default=False, copy=False,
        help='Shows "Ask the AI Consultant" on these records to colleagues with Project rights, and lets only the '
             'people who can read the consulted records use the task AI chats linked to them.')
    was_enabled = fields.Boolean(
        string='Was Enabled Once', readonly=True, copy=False,
        help='Set the first time this setting is enabled and never reset: from then on the records sent to the '
             'AI keep deciding who may read those chats, also after the setting is disabled.')
    project_id = fields.Many2one(
        'project.project', string='Consultation Project', ondelete='restrict',
        help='The consultation tasks are created in this project; its internal AI assistant answers them.')
    role_prompt = fields.Text(
        string='Consultant Role',
        help='A trusted instruction for the internal AI (at most 1,000 characters), sent with every consultation '
             'that includes these records. Never sent to the customer tab.')
    project_from_record = fields.Boolean(
        string='Project From the Record', compute='_compute_project_from_record',
        help="The record's own module chooses the consultation project of each record.")
    data_destination = fields.Char(
        string='Data Goes To', compute='_compute_data_destination',
        help='The AI assistant (and its kind of AI connection) that receives the record data of a consultation.')
    warning = fields.Text(string='Warnings', compute='_compute_warning')

    _sql_constraints = [
        ('model_uniq', 'unique(model_id)', 'Each kind of record has one AI consultant setting.'),
    ]

    # --- Computes (system administrators read the profile; the values come with sudo, as fixed texts) ---

    @api.depends('model_id')
    def _compute_project_from_record(self):
        for profile in self:
            model = profile.model_id.model
            profile.project_from_record = bool(is_consult_model(self.env, model)
                                               and self.env[model]._woow_project_ai_consult_project_from_record())

    @api.depends('project_id', 'model_id', 'project_id.woow_project_ai_internal_assistant_id')
    def _compute_data_destination(self):
        _ = self.env._
        Connection = self.env['woow.project.ai.connection']
        backends = dict(Connection._fields['backend_type']._description_selection(self.env))
        for profile in self:
            if profile.project_from_record:
                profile.data_destination = _('The internal AI assistant of the project each record names')
                continue
            project = profile.sudo().project_id
            assistant = project._woow_project_ai_tab_assistant(TAB_INTERNAL) if project else None
            if not assistant:
                profile.data_destination = _('Nowhere yet: no internal AI assistant in the consultation project')
                continue
            backend = backends.get(assistant.connection_id.backend_type) or ''
            profile.data_destination = '%s (%s)' % (assistant.name, backend) if backend else assistant.name

    @api.depends('project_id', 'model_id', 'enabled', 'project_id.privacy_visibility',
                 'project_id.woow_project_ai_internal_assistant_id', 'project_id.woow_project_ai_customer_assistant_id')
    def _compute_warning(self):
        _ = self.env._
        for profile in self:
            lines = []
            model = profile.model_id.model
            consulted = is_consult_model(self.env, model)
            project = profile.sudo().project_id
            if not project and not profile.project_from_record:
                lines.append(_('No consultation project is chosen yet.'))
            if project or profile.project_from_record:
                # N6 B: the task names carry the record's name (consult_mixin._woow_project_ai_consult_task_name)
                lines.append(_("The names of consultation tasks carry the record's name (renaming the record later "
                               "does not rename them): everyone who can read the consultation project sees them, "
                               "names typed by website visitors included. With Odoo's default user template every "
                               "employee is a Project Administrator and reads every task."))
            if project:
                assistant = project._woow_project_ai_tab_assistant(TAB_INTERNAL)
                if not assistant or project._woow_project_ai_tab_problems(TAB_INTERNAL, assistant):
                    lines.append(_('The internal AI of the consultation project cannot answer now: see the "AI '
                                   'Assistants" page of the project.'))
                if project.privacy_visibility == 'portal':
                    lines.append(_('The consultation project is shared with portal users: customers added to it '
                                   'see the names (with the record names) and assignees of its consultation tasks.'))
                if assistant and assistant.connection_id.backend_type == 'n8n':
                    lines.append(_('The internal AI connection is n8n: make sure the workflow keeps no memory across '
                                   'conversations and never lets the AI decide to fetch URLs, and note that n8n '
                                   'keeps the data of every execution it receives.'))
                if consulted and TAB_CUSTOMER not in model_tabs(self.env, model) \
                        and project._woow_project_ai_tab_assistant(TAB_CUSTOMER):
                    lines.append(_('These records have no fields for customers: the customer tab gets no record '
                                   'data.'))
            if consulted:
                lines += [str(line) for line in self.env[model]._woow_project_ai_consult_profile_warnings(profile)
                          or () if line]
            profile.warning = '\n'.join(lines)

    # --- Constraints ---

    @api.constrains('model_id')
    def _check_woow_project_ai_model(self):
        for profile in self:
            if not is_consult_model(self.env, profile.model_id.model):
                raise ValidationError(self.env._('This kind of record has no AI consultant: install its AI '
                                                 'consultant module first.'))

    @api.constrains('enabled', 'project_id', 'model_id')
    def _check_woow_project_ai_project(self):
        for profile in self:
            if profile.enabled and not profile.project_id and not profile.project_from_record:
                raise ValidationError(self.env._('Choose a consultation project before enabling the AI '
                                                 'consultant.'))

    @api.constrains('role_prompt')
    def _check_woow_project_ai_role_prompt(self):
        for profile in self:
            if len(profile.role_prompt or '') > MAX_ROLE_PROMPT:
                raise ValidationError(self.env._('The consultant role can have at most %(max)s characters.',
                                                 max=MAX_ROLE_PROMPT))

    # --- Writes: every one clears the state ---

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = [dict(vals, was_enabled=True) if vals.get('enabled') else vals for vals in vals_list]
        profiles = super().create(vals_list)
        self._woow_project_ai_consult_changed(any(profiles.mapped('enabled')))
        return profiles

    def write(self, vals):
        if 'was_enabled' in vals and not vals['was_enabled']:
            raise UserError(self.env._('"Was Enabled Once" is never reset: the records sent to the AI keep '
                                       'deciding who may read those chats.'))
        before = {profile.id: profile.enabled for profile in self}
        if vals.get('enabled'):
            vals = dict(vals, was_enabled=True)
        res = super().write(vals)
        changed = 'enabled' in vals and any(before[profile.id] != profile.enabled for profile in self)
        self._woow_project_ai_consult_changed(changed)
        return res

    def unlink(self):
        if not self.env.context.get(MODULE_UNINSTALL_FLAG) and any(self.mapped('was_enabled')):
            raise UserError(self.env._('A setting that was ever enabled cannot be deleted: disable it instead (the '
                                       'records sent to the AI keep deciding who may read those chats).'))
        enabled = any(self.mapped('enabled'))
        res = super().unlink()
        self._woow_project_ai_consult_changed(enabled)
        return res

    def _woow_project_ai_consult_changed(self, enabled_changed):
        """A profile changed: the state is read again (this transaction at once;
        after the commit again, so that no transaction of this process that
        read the old state meanwhile keeps it; other processes at their next
        request). Enabling or disabling a model: the readable task chats of
        this transaction are computed again, and the member sweep runs soon."""
        registry = self.env.registry
        registry.clear_cache()
        self.env.cr.postcommit.add(registry.clear_cache)
        if enabled_changed:
            self.env['discuss.channel']._woow_project_ai_forget_readable()
            cron = self.env.ref(SYNC_CRON, raise_if_not_found=False)
            if cron:
                cron.sudo()._trigger()

    # --- The state ---

    @api.model
    def _woow_project_ai_consult_state(self):
        """State: enabled models, used (a profile was ever enabled, or the
        ledger has a row), ledger models without a mixin in this registry, the
        profiles' projects. "Unused" while the tables are not there yet (only
        while ``-u`` creates them)."""
        if not self._woow_project_ai_consult_tables_ready():
            return UNUSED
        return self._woow_project_ai_consult_state_cached()

    @api.model
    def _woow_project_ai_consult_tables_ready(self):
        registry = self.env.registry
        if getattr(registry, TABLES_READY_KEY, False):
            return True
        self.env.cr.execute("SELECT to_regclass('woow_project_ai_consult_profile') IS NOT NULL "
                            "AND to_regclass('woow_project_ai_consult_sent') IS NOT NULL")
        ready = bool(self.env.cr.fetchone()[0])
        if ready:
            setattr(registry, TABLES_READY_KEY, True)
        return ready

    @api.model
    @tools.ormcache()
    def _woow_project_ai_consult_state_cached(self):
        self.flush_model(['model', 'enabled', 'was_enabled', 'project_id'])
        self.env.cr.execute("""
            SELECT ARRAY(SELECT DISTINCT model FROM woow_project_ai_consult_profile
                          WHERE enabled AND model IS NOT NULL),
                   EXISTS(SELECT 1 FROM woow_project_ai_consult_profile WHERE was_enabled)
                     OR EXISTS(SELECT 1 FROM woow_project_ai_consult_sent),
                   ARRAY(SELECT DISTINCT res_model FROM woow_project_ai_consult_sent),
                   ARRAY(SELECT DISTINCT project_id FROM woow_project_ai_consult_profile
                          WHERE project_id IS NOT NULL)""")
        enabled, used, ledger_models, projects = self.env.cr.fetchone()
        mixin = consult_models(self.env)
        return State(
            enabled_models=frozenset(name for name in enabled if name in mixin),
            used=bool(used),
            ledger_other_models=frozenset(name for name in ledger_models if name not in mixin),
            profile_project_ids=frozenset(projects),
        )

    # --- Preview ---

    def action_woow_project_ai_consult_preview(self):
        """「預覽影響」: who would leave which task AI chats if this kind of
        record were enabled (the chats whose task links such records; at most
        2,000 chats per press), by name, in a dialog. Nothing changes."""
        self.ensure_one()
        preview = self.env['woow.project.ai.consult.preview'].create({
            'profile_id': self.id,
            'result': self._woow_project_ai_consult_preview_text(),
        })
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Preview the Impact'),
            'res_model': 'woow.project.ai.consult.preview',
            'res_id': preview.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'new',
        }

    def _woow_project_ai_consult_preview_counts(self):
        """(chats looked at, capped, {partner: number of chats they would leave}):
        the members who may be in a chat now but would fail its gate with this
        model enabled."""
        self.ensure_one()
        model = self.model_id.model
        Task = self.env['project.task'].sudo().with_context(active_test=False)
        Channel = self.env['discuss.channel'].sudo().with_context(active_test=False)
        specs = [spec for spec in Task._woow_project_ai_consult_specs()
                 if Task._fields[spec.field].comodel_name == model]
        if not specs:
            return 0, False, {}
        tasks = Task._search(expression.OR([[(spec.field, '!=', False)] for spec in specs]))
        chats = Channel.search([('woow_project_ai_task_id', 'in', tasks)], order='id', limit=PREVIEW_MAX_CHATS + 1)
        capped = len(chats) > PREVIEW_MAX_CHATS
        chats = chats[:PREVIEW_MAX_CHATS]
        state = consult_state(self.env)
        enabled = state._replace(enabled_models=state.enabled_models | {model}, used=True)
        bots = Channel._woow_project_ai_bot_partner_ids()
        leaving = {}
        for chat in chats:
            task, tab = chat.woow_project_ai_task_id, chat.woow_project_ai_tab
            now = task._woow_project_ai_consult_gate(tab, state)
            then = task._woow_project_ai_consult_gate(tab, enabled)
            if then == now:
                continue
            for partner in chat.channel_member_ids.partner_id:
                if partner.id in bots:
                    continue
                users = partner.with_context(active_test=False).user_ids.filtered(
                    lambda user: task._woow_project_ai_user_allowed(user, tab))
                stays_now = any(task._woow_project_ai_consult_user_ok(user, tab, company_ids(user), gate=now)
                                for user in users)
                stays_then = any(task._woow_project_ai_consult_user_ok(user, tab, company_ids(user), gate=then)
                                 for user in users)
                if stays_now and not stays_then:
                    leaving[partner] = leaving.get(partner, 0) + 1
        return len(chats), capped, leaving

    def _woow_project_ai_consult_preview_text(self):
        _ = self.env._
        count, capped, leaving = self._woow_project_ai_consult_preview_counts()
        lines = [_('Task AI chats linked to records of this kind: %(count)s.', count=count)]
        if capped:
            lines.append(_('Only the first %(limit)s chats were checked.', limit=PREVIEW_MAX_CHATS))
        if leaving:
            lines.append(_('If you enable it, these people leave task AI chats (they cannot read a record linked '
                           'to the task, or one its AI already got):'))
            people = sorted(leaving.items(), key=lambda item: (-item[1], item[0].name or '', item[0].id))
            for partner, chats in people[:PREVIEW_MAX_NAMES]:
                lines.append(_('- %(name)s: %(count)s chats', name=partner.name or '?', count=chats))
            if len(people) > PREVIEW_MAX_NAMES:
                lines.append(_('… and %(count)s more people.', count=len(people) - PREVIEW_MAX_NAMES))
        else:
            lines.append(_('If you enable it, nobody leaves a task AI chat.'))
        if self.was_enabled:
            lines.append(_('The records already sent to the AI keep deciding in any case.'))
        _logger.info("Project AI consultant: impact preview of %s: %s chats, %s people would leave", self.model,
                     count, len(leaving))
        return '\n'.join(lines)


class WoowProjectAiConsultPreview(models.TransientModel):
    _name = 'woow.project.ai.consult.preview'
    _description = 'AI Consultant: Impact Preview'

    profile_id = fields.Many2one(PROFILE, string='AI Consultant Setting', readonly=True, ondelete='cascade')
    result = fields.Text(string='Impact', readonly=True)
