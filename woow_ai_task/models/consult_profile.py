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

from ..tools.urls import split_url
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
PROJECT_MANAGER_GROUP = 'project.group_project_manager'
# Odoo 18 (base_setup): while this system parameter is on, the default user template's groups reach new and
# existing employees (Settings > Users > Default Access Rights)
DEFAULT_RIGHTS_PARAM = 'base_setup.default_user_rights'


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
    send_notes = fields.Boolean(
        string='Send the Notes', default=False, copy=False,
        help="Send these records' long free texts (for leads: the Notes) to the AI. They hold whatever was typed "
             "into them, by visitors too: contact details, a whole Live Chat conversation, a web form's extra "
             "fields. E-mail addresses, phone numbers and IP addresses are masked; names and postal addresses are "
             "not.")
    no_data_tools = fields.Boolean(
        string='This connection has no tools that read company data', compute='_compute_no_data_tools',
        inverse='_inverse_no_data_tools',
        help='Confirm that the AI behind the consultation project (its n8n workflow, its LiteLLM proxy or model) '
             'has no tool that reads Odoo or other company data with credentials of its own: such a tool is not '
             'limited to what the asker may read, and the records\' text, which visitors may have typed, can steer '
             'it. Required to enable. Ticking it confirms where the data goes now (the assistant, its connection '
             'and its backend host); when that changes, it is cleared and the AI consultant pauses for these '
             'records until a system administrator ticks it again.')
    destination_fingerprint = fields.Char(
        string='Confirmed Destination', readonly=True, copy=False,
        help='Where the data went when a system administrator last ticked "This connection has no tools that read '
             'company data": the AI assistant, its connection, its kind and its backend host.')
    destination_changed = fields.Boolean(
        string='Destination Changed', compute='_compute_destination_changed',
        help='Where the record data would go now differs from what a system administrator confirmed: the AI '
             'consultant is paused for these records until one confirms again.')
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

    @api.depends('project_id', 'model_id', 'send_notes', 'project_id.woow_project_ai_internal_assistant_id')
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
            text = '%s (%s)' % (assistant.name, backend) if backend else assistant.name
            if profile._woow_project_ai_consult_has_notes():
                text = '%s; %s' % (text, _('Notes: sent, contact details masked') if profile.send_notes
                                   else _('Notes: not sent'))
            profile.data_destination = text

    @api.depends('project_id', 'model_id', 'enabled', 'send_notes', 'destination_fingerprint',
                 'project_id.privacy_visibility',
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
            if profile.enabled and profile._woow_project_ai_consult_destination_changed():
                lines.append(_('Where the record data goes changed after a system administrator confirmed it (now: '
                               '%(now)s). The AI consultant is paused for these records until a system administrator '
                               'checks that AI and ticks "This connection has no tools that read company data" '
                               'again.', now=profile.data_destination or '-'))
            if project or profile.project_from_record:
                # product decision: the task names carry the record's name (consult_mixin, _consult_task_name)
                lines.append(_("The names of consultation tasks carry the record's name (renaming the record later "
                               "does not rename them; deleting it does): everyone who can read the consultation "
                               "project sees them, names typed by website visitors included. Project Administrators "
                               "read every task whatever the project's visibility: an invited-users project hides "
                               "the names only from the other Project users."))
                lines += profile._woow_project_ai_consult_rights_lines()
            if project:
                assistant = project._woow_project_ai_tab_assistant(TAB_INTERNAL)
                if not assistant or project._woow_project_ai_tab_problems(TAB_INTERNAL, assistant):
                    lines.append(_('The internal AI of the consultation project cannot answer now: see the "AI '
                                   'Assistants" page of the project.'))
                if project.privacy_visibility == 'portal':
                    lines.append(_('The consultation project is shared with portal users: customers added to it '
                                   'see the names (with the record names) and assignees of its consultation tasks.'))
                backend = assistant.connection_id.backend_type if assistant else None
                if backend == 'n8n':
                    lines.append(_('The internal AI connection is n8n: make sure the workflow keeps no memory across '
                                   'conversations and never lets the AI decide to fetch URLs, and note that n8n '
                                   'keeps the data of every execution it receives. A workflow tool that reads Odoo '
                                   'or other company data with credentials of its own is not limited to what the '
                                   'asker may read, and the records\' text, which visitors may have typed, can '
                                   'steer it.'))
                elif backend == 'openai':
                    lines.append(_('The internal AI connection is LiteLLM: Odoo sends no tools, but a proxy or a '
                                   'model with tools of its own that read Odoo or other company data is not limited '
                                   'to what the asker may read, and the records\' text, which visitors may have '
                                   'typed, can steer it.'))
                if consulted and TAB_CUSTOMER not in model_tabs(self.env, model) \
                        and project._woow_project_ai_tab_assistant(TAB_CUSTOMER):
                    lines.append(_('These records have no fields for customers: no record field goes to the '
                                   "customer tab, but the consultation task's name, which carries the record's "
                                   'name, does.'))
            if profile._woow_project_ai_consult_has_notes():
                if profile.send_notes:
                    lines.append(_("The Notes are sent: they hold whatever was typed into them, by visitors too "
                                   "(contact details, a whole Live Chat conversation, a web form's extra fields and "
                                   "metadata). E-mail addresses, phone numbers and IP addresses are masked before "
                                   "they reach the AI; names and postal addresses are not."))
                else:
                    lines.append(_('The Notes are not sent ("Send the Notes" is off).'))
            if consulted:
                lines += [str(line) for line in self.env[model]._woow_project_ai_consult_profile_warnings(profile)
                          or () if line]
            profile.warning = '\n'.join(lines)

    def _woow_project_ai_consult_rights_lines(self):
        """Who reads every consultation task, counted now: the Project
        Administrators among the active employees, and whether Default Access
        Rights is on (Odoo 18: then the default user template's groups reach
        new employees too; with Odoo's template they include Project
        Administrator)."""
        _ = self.env._
        Users = self.env['res.users'].sudo()
        employees = Users.search([('share', '=', False)])
        group = self.env.ref(PROJECT_MANAGER_GROUP, raise_if_not_found=False)
        managers = employees.filtered(lambda user: group in user.groups_id) if group else Users
        lines = [_('Employees who are Project Administrators (they read every consultation task): %(managers)s of '
                   '%(employees)s.', managers=len(managers), employees=len(employees))]
        if self._woow_project_ai_default_rights_on():
            lines.append(_('Default Access Rights (Settings > Users) is on: new employees get the groups of the '
                           'default user template, with Odoo\'s template Project Administrator too.'))
        return lines

    @api.model
    def _woow_project_ai_default_rights_on(self):
        """Odoo 18: whether the default user template's groups reach new and
        existing employees (Settings > Users > Default Access Rights; on by
        default since base_setup's install; without base_setup they always do)."""
        if 'base_setup' not in self.env.registry._init_modules:
            return True
        value = self.env['ir.config_parameter'].sudo().get_param(DEFAULT_RIGHTS_PARAM)
        return str(value or '').strip().lower() in ('1', 'true', 'yes', 'on')

    def _woow_project_ai_consult_has_notes(self):
        """Whether this kind of record has long free texts (detail fields)."""
        self.ensure_one()
        model = self.model_id.model
        if not is_consult_model(self.env, model):
            return False
        Model = self.env[model]
        return bool(Model._woow_project_ai_consult_detail_fields(TAB_INTERNAL)
                    or Model._woow_project_ai_consult_detail_fields(TAB_CUSTOMER))

    # --- Where the data goes: confirmed by a system administrator ---

    def _woow_project_ai_consult_destination(self):
        """The fingerprint of where the record data goes now: the
        consultation project's internal AI assistant, its connection, the
        connection's kind and its backend host ('none' without an assistant;
        'project-from-record' when each record chooses its project: that
        module answers for its projects' assistants)."""
        self.ensure_one()
        if self.project_from_record:
            return 'project-from-record'
        project = self.sudo().project_id
        assistant = project._woow_project_ai_tab_assistant(TAB_INTERNAL) if project else None
        if not assistant:
            return 'none'
        connection = assistant.sudo().connection_id
        parts = split_url(connection.endpoint_url or '')
        host = (parts.hostname or '').lower() if parts else ''
        return 'assistant:%s;connection:%s;kind:%s;host:%s' % (
            assistant.id, connection.id, connection.backend_type or '', host)

    def _woow_project_ai_consult_destination_changed(self):
        """Whether where the data goes now differs from what a system
        administrator confirmed (ticking no_data_tools, required to enable)."""
        self.ensure_one()
        profile = self.sudo()
        return (profile.destination_fingerprint or '') != profile._woow_project_ai_consult_destination()

    @api.depends('enabled', 'destination_fingerprint', 'project_id', 'project_id.woow_project_ai_internal_assistant_id')
    def _compute_destination_changed(self):
        for profile in self:
            profile.destination_changed = bool(profile.enabled) \
                and profile._woow_project_ai_consult_destination_changed()

    @api.depends('destination_fingerprint', 'project_id', 'project_id.woow_project_ai_internal_assistant_id')
    def _compute_no_data_tools(self):
        # ticked only while it confirmed the destination of now: any change of the assistant, its connection or
        # its host clears it
        for profile in self:
            profile.no_data_tools = not profile._woow_project_ai_consult_destination_changed()

    def _inverse_no_data_tools(self):
        for profile in self:
            profile.destination_fingerprint = profile._woow_project_ai_consult_destination() \
                if profile.no_data_tools else False

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

    def _check_woow_project_ai_no_data_tools(self):
        """Enabled only with the confirmation that the AI behind the
        consultation project has no company-data tools, for where the data
        goes now. Called by create and write once no_data_tools' inverse has
        run (an @api.constrains on a stored field runs before it)."""
        for profile in self:
            if profile.enabled and profile._woow_project_ai_consult_destination_changed():
                raise ValidationError(self.env._('Before enabling the AI consultant, confirm that the AI behind '
                                                 'the consultation project has no tools that read company data '
                                                 '("This connection has no tools that read company data").'))

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
        profiles._check_woow_project_ai_no_data_tools()
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
        if {'enabled', 'no_data_tools', 'project_id', 'model_id'} & set(vals):
            self._check_woow_project_ai_no_data_tools()
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
        self.flush_model(['model', 'enabled', 'was_enabled', 'project_id', 'send_notes'])
        self.env.cr.execute("""
            SELECT ARRAY(SELECT DISTINCT model FROM woow_project_ai_consult_profile
                          WHERE enabled AND model IS NOT NULL),
                   EXISTS(SELECT 1 FROM woow_project_ai_consult_profile WHERE was_enabled)
                     OR EXISTS(SELECT 1 FROM woow_project_ai_consult_sent),
                   ARRAY(SELECT DISTINCT res_model FROM woow_project_ai_consult_sent),
                   ARRAY(SELECT DISTINCT project_id FROM woow_project_ai_consult_profile
                          WHERE project_id IS NOT NULL),
                   ARRAY(SELECT DISTINCT model FROM woow_project_ai_consult_profile
                          WHERE send_notes AND model IS NOT NULL)""")
        enabled, used, ledger_models, projects, notes = self.env.cr.fetchone()
        mixin = consult_models(self.env)
        return State(
            enabled_models=frozenset(name for name in enabled if name in mixin),
            used=bool(used),
            ledger_other_models=frozenset(name for name in ledger_models if name not in mixin),
            profile_project_ids=frozenset(projects),
            notes_models=frozenset(name for name in notes if name in mixin),
        )

    # --- Preview ---

    def action_woow_project_ai_consult_preview(self):
        """「預覽影響」: who would leave how many task AI chats if this kind of
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
                if chats == 1:
                    lines.append(_('- %(name)s: 1 chat', name=partner.name or '?'))
                else:
                    lines.append(_('- %(name)s: %(count)s chats', name=partner.name or '?', count=chats))
            more = len(people) - PREVIEW_MAX_NAMES
            if more == 1:
                lines.append(_('… and 1 more person.'))
            elif more > 1:
                lines.append(_('… and %(count)s more people.', count=more))
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
