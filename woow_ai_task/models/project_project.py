# -*- coding: utf-8 -*-
"""Project settings of the task AI conversations: the two AI assistants, their
project texts, the customer welcome message and the pause after a colleague
writes; their readiness and this month's replies (read with administrator
rights, shown as plain values).

Project managers edit these fields (``project.project`` write access is for
project managers only); project users who can read the project read them;
other employees and portal users do not (field groups: an employee outside the
Project app opening a project form never reads an AI assistant).
"""

from odoo import api, fields, models
from odoo.exceptions import ValidationError

PROJECT_USER_GROUP = 'project.group_project_user'

TAB_INTERNAL = 'internal'
TAB_CUSTOMER = 'customer'
TABS = (TAB_INTERNAL, TAB_CUSTOMER)

DEFAULT_PAUSE_MINUTES = 10
MAX_PAUSE_MINUTES = 1440

STATE_NONE = 'none'
STATE_OK = 'ok'
STATE_BLOCKED = 'blocked'
STATE_SELECTION = [(STATE_NONE, 'Not Set'), (STATE_OK, 'Ready'), (STATE_BLOCKED, 'Blocked')]

USAGE_SURFACE = 'project_task'
ASSISTANT_DOMAIN = [('active', '=', True)]
CUSTOMER_ASSISTANT_DOMAIN = ASSISTANT_DOMAIN + [('customer_safe', '=', True)]


class ProjectProject(models.Model):
    _inherit = 'project.project'

    woow_project_ai_internal_assistant_id = fields.Many2one(
        'woow.project.ai.assistant', string='Internal AI Assistant', ondelete='restrict', index='btree_not_null',
        groups=PROJECT_USER_GROUP, domain=ASSISTANT_DOMAIN,
        help='Answers the "Internal AI chat" of every task of this project (colleagues with Project rights who '
             'can read the task and this project). Its backend may look up company data, read only: a property '
             'of its AI connection, set up by a system administrator.')
    woow_project_ai_customer_assistant_id = fields.Many2one(
        'woow.project.ai.assistant', string='Customer AI Assistant', ondelete='restrict', index='btree_not_null',
        groups=PROJECT_USER_GROUP, domain=CUSTOMER_ASSISTANT_DOMAIN,
        help='Answers the "Customer AI chat" of every task (portal users who can read the task, also on the '
             'portal task page). Its AI connection must be safe for customers. Empty: no AI on the portal task '
             'page.')
    woow_project_ai_internal_assistant_description = fields.Text(
        related='woow_project_ai_internal_assistant_id.description', string='About the Internal AI Assistant',
        groups=PROJECT_USER_GROUP)
    woow_project_ai_customer_assistant_description = fields.Text(
        related='woow_project_ai_customer_assistant_id.description', string='About the Customer AI Assistant',
        groups=PROJECT_USER_GROUP)
    woow_project_ai_internal_brief = fields.Text(
        string='Project Text for the Internal AI', groups=PROJECT_USER_GROUP,
        help='Sent to the internal AI with every question, after its own prompt. The internal chat of a task is '
             'only for colleagues who can read this project too (a task follower may read a task of a project '
             'hidden to them), also after the task moves to another project: whoever reads the chat, earlier '
             'answers included, may read this text.')
    woow_project_ai_customer_brief = fields.Text(
        string='Project Text for the Customer AI', groups=PROJECT_USER_GROUP,
        help='Sent to the customer AI with every question, after its own prompt. The only project text the '
             'customer tab ever sends: write nothing internal here.')
    woow_project_ai_customer_welcome = fields.Text(
        string='Customer Tab Welcome Message', groups=PROJECT_USER_GROUP,
        help='Optional. Posted once by the customer AI when the customer chat of a task is opened for the first '
             'time (markdown).')
    woow_project_ai_pause_minutes = fields.Integer(
        string='AI Pause After a Colleague Writes (minutes)', default=DEFAULT_PAUSE_MINUTES,
        groups=PROJECT_USER_GROUP,
        help='When an employee writes in the customer chat of a task, the AI stays silent there for this many '
             'minutes (1-1440), counted again from each further message of an employee. Customer messages '
             'written meanwhile are not answered later.')

    woow_project_ai_internal_state = fields.Selection(
        STATE_SELECTION, string='Internal AI Status', compute='_compute_woow_project_ai_state',
        groups=PROJECT_USER_GROUP)
    woow_project_ai_internal_state_reason = fields.Text(
        string='Internal AI Status Details', compute='_compute_woow_project_ai_state', groups=PROJECT_USER_GROUP)
    woow_project_ai_customer_state = fields.Selection(
        STATE_SELECTION, string='Customer AI Status', compute='_compute_woow_project_ai_state',
        groups=PROJECT_USER_GROUP)
    woow_project_ai_customer_state_reason = fields.Text(
        string='Customer AI Status Details', compute='_compute_woow_project_ai_state', groups=PROJECT_USER_GROUP)
    woow_project_ai_usage_internal_month = fields.Integer(
        string='AI Replies This Month (Internal Chats)', compute='_compute_woow_project_ai_usage_month',
        groups=PROJECT_USER_GROUP,
        help='Replies of the AI in the internal chats of this project\'s tasks this month (UTC).')
    woow_project_ai_usage_customer_month = fields.Integer(
        string='AI Replies This Month (Customer Chats)', compute='_compute_woow_project_ai_usage_month',
        groups=PROJECT_USER_GROUP,
        help='Replies of the AI in the customer chats of this project\'s tasks this month (UTC): a reference for '
             'invoicing by hand.')

    # --- Constraints ---

    @api.constrains('woow_project_ai_pause_minutes')
    def _check_woow_project_ai_pause_minutes(self):
        for project in self.sudo():
            if not 1 <= (project.woow_project_ai_pause_minutes or 0) <= MAX_PAUSE_MINUTES:
                raise ValidationError(self.env._('The AI pause must be between 1 and 1440 minutes.'))

    @api.constrains('woow_project_ai_internal_assistant_id')
    def _check_woow_project_ai_internal_assistant(self):
        self._check_woow_project_ai_assistants(TAB_INTERNAL)

    @api.constrains('woow_project_ai_customer_assistant_id')
    def _check_woow_project_ai_customer_assistant(self):
        # also before every answer (the connection may change later)
        self._check_woow_project_ai_assistants(TAB_CUSTOMER)

    def _check_woow_project_ai_assistants(self, *tabs):
        """The chosen assistant of each tab of `tabs`: active, and (customer
        tab) its AI connection safe for customers. Checked when the tab's
        field is set (one constraint per field: clearing one tab never fails
        because of the other)."""
        # sudo: woow.project.ai.assistant / connection - the check needs the connection's flag
        for project in self.sudo():
            for tab in tabs or TABS:
                assistant = project['woow_project_ai_%s_assistant_id' % tab]
                if not assistant:
                    continue
                name = assistant.name
                if not assistant.active:
                    raise ValidationError(self.env._(
                        'The AI assistant "%(name)s" is archived. A system administrator must restore it, or '
                        'choose another AI assistant.', name=name))
                if tab == TAB_CUSTOMER and not assistant.connection_id.customer_safe:
                    raise ValidationError(self.env._(
                        'The AI connection of "%(name)s" is not safe for customers. A system administrator must '
                        'check that n8n workflow and tick "Safe for Customers" on its AI connection (Project > '
                        'Configuration > Project AI > AI Connections) only if it looks up no company data, calls '
                        'no tools and keeps no memory; or choose another AI assistant for the customer tab.',
                        name=name))

    def copy_data(self, default=None):
        # a duplicate (or a project created from a template, e.g. by sale_project) keeps a tab's assistant
        # only while it may still be chosen there; one that became archived or (customer tab) unsafe would
        # make the copy fail on the constraints
        vals_list = super().copy_data(default=default)
        default = default or {}
        for project, vals in zip(self.sudo(), vals_list):
            for tab in TABS:
                field = 'woow_project_ai_%s_assistant_id' % tab
                if field in default:
                    continue
                assistant = project[field]
                if assistant and (not assistant.active
                                  or (tab == TAB_CUSTOMER and not assistant.connection_id.customer_safe)):
                    vals[field] = False
        return vals_list

    # --- Readiness (administrator rights, plain values) ---

    def _woow_project_ai_tab_assistant(self, tab):
        """The AI assistant chosen for `tab` (sudo; archived ones too), or empty."""
        self.ensure_one()
        return self.sudo()['woow_project_ai_%s_assistant_id' % tab]

    def _woow_project_ai_tab_problems(self, tab, assistant=None):
        """Why the AI of `tab` cannot answer now (codes; [] = it answers): the
        assistant is archived, its AI connection is not ready
        (``woow.project.ai.connection.readiness``), or (customer tab) the
        connection is not safe for customers. Checked again before every
        answer (discuss.channel._woow_project_ai_assistant)."""
        self.ensure_one()
        assistant = (assistant if assistant is not None else self._woow_project_ai_tab_assistant(tab)).sudo()
        if not assistant:
            return []
        problems = []
        if not assistant.active:
            problems.append('assistant_archived')
        # sudo: woow.project.ai.connection - readable by system administrators only; only codes come out
        connection = assistant.connection_id.sudo()
        if connection.readiness != 'ok':
            details = []
            if not connection.active:
                details.append('connection_archived')
            else:
                if not (connection.endpoint_url or '').strip():
                    details.append('connection_no_url')
                if connection.backend_type == 'openai' and not connection.api_key_set:
                    details.append('connection_no_key')
            problems += details or ['connection_not_ready']
        if tab == TAB_CUSTOMER and not connection.customer_safe:
            problems.append('not_customer_safe')
        if tab == TAB_CUSTOMER and self.sudo().privacy_visibility != 'portal':
            problems.append('not_shared')
        return problems

    def _woow_project_ai_tab_blocked_reasons(self, tab, assistant=None):
        """The problems of `tab` as sentences for the project form: fixed
        texts, never a URL, a key or a connection's name; [] = ready."""
        _ = self.env._
        texts = {
            'assistant_archived': _('The AI assistant is archived.'),
            'connection_archived': _('Its AI connection is archived.'),
            'connection_no_url': _('Its AI connection has no endpoint URL.'),
            'connection_no_key': _('Its AI connection has no API key.'),
            'connection_not_ready': _('Its AI connection is not ready.'),
            'not_customer_safe': _('Its AI connection is not marked "Safe for Customers": the customer chats get a '
                                   'short notice instead of an answer.'),
            'not_shared': _('This project is not shared with portal users: no customer can open a customer AI '
                            'chat.'),
        }
        problems = self._woow_project_ai_tab_problems(tab, assistant)
        reasons = [texts[code] for code in problems if code in texts]
        if set(problems) - {'not_shared'}:
            reasons.append(_('A system administrator must fix this in Project > Configuration > Project AI.'))
        elif problems:
            reasons.append(_('A project manager can share the project with portal users (Visibility, Settings '
                             'page).'))
        return reasons

    def _woow_project_ai_pause_minutes_value(self):
        self.ensure_one()
        minutes = self.sudo().woow_project_ai_pause_minutes or DEFAULT_PAUSE_MINUTES
        return max(1, min(minutes, MAX_PAUSE_MINUTES))

    @api.depends('woow_project_ai_internal_assistant_id', 'woow_project_ai_customer_assistant_id',
                 'privacy_visibility')
    def _compute_woow_project_ai_state(self):
        _ = self.env._
        ready = {
            TAB_INTERNAL: _('Ready: the AI answers every message of the colleagues.'),
            TAB_CUSTOMER: _("Ready: the AI answers every customer message; a colleague's message pauses it."),
        }
        for project in self:
            for tab in TABS:
                assistant = project.sudo()['woow_project_ai_%s_assistant_id' % tab]
                if not assistant:
                    state, reason = STATE_NONE, _('No AI assistant chosen: the task has no such tab.')
                else:
                    reasons = project._woow_project_ai_tab_blocked_reasons(tab, assistant)
                    state = STATE_BLOCKED if reasons else STATE_OK
                    reason = '\n'.join(reasons) if reasons else ready[tab]
                project['woow_project_ai_%s_state' % tab] = state
                project['woow_project_ai_%s_state_reason' % tab] = reason

    # --- Usage (administrator rights, numbers only) ---

    def _compute_woow_project_ai_usage_month(self):
        # the usage months are UTC calendar months (woow.project.ai.usage.month)
        month = fields.Datetime.now().date().replace(day=1)
        ids = [i for i in self.ids if isinstance(i, int)]
        counts = {}
        if ids:
            # sudo: woow.project.ai.usage.month - readable by administrators only; project users see the sums
            rows = self.env['woow.project.ai.usage.month'].sudo().search([
                ('scope_model', '=', 'project.project'), ('scope_res_id', 'in', ids),
                ('surface_type', '=', USAGE_SURFACE), ('month', '=', month)])
            for row in rows:
                key = (row.scope_res_id, row.surface_tab)
                counts[key] = counts.get(key, 0) + row.replies_ok
        for project in self:
            project.woow_project_ai_usage_internal_month = counts.get((project.id, TAB_INTERNAL), 0)
            project.woow_project_ai_usage_customer_month = counts.get((project.id, TAB_CUSTOMER), 0)

    def action_woow_project_ai_view_usage(self):
        """Monthly usage rows of this project (system administrators: the action is theirs)."""
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('woow_ai_task.action_woow_project_ai_usage_month')
        action['domain'] = [('scope_model', '=', 'project.project'), ('scope_res_id', '=', self.id)]
        action['context'] = {'search_default_group_tab': 1}
        return action

    # --- Membership triggers ---

    def write(self, vals):
        res = super().write(vals)
        if {'privacy_visibility', 'company_id', 'woow_project_ai_internal_assistant_id',
                'woow_project_ai_customer_assistant_id'} & set(vals):
            # who may read the tasks, or which bot answers a tab, changed
            self.env['discuss.channel']._woow_project_ai_sync_projects(self)
        return res

    # (no sync on message_subscribe: a new follower only gains access, nobody loses it)

    def message_unsubscribe(self, partner_ids=None):
        res = super().message_unsubscribe(partner_ids=partner_ids)
        if partner_ids:
            self.env['discuss.channel']._woow_project_ai_sync_projects(self)
        return res
