# -*- coding: utf-8 -*-
"""The two AI conversations of a task: opening a tab (lazy creation, joining),
who may be in each, the task summary the AI gets, the To-Do that alerts the
people in charge, and the membership triggers of the task's own changes.

A tab's chat is always found through the chat's own link
(``discuss.channel.woow_project_ai_task_id`` / ``woow_project_ai_tab``, which
only this module writes): the task-side fields are computed from it, so no
task write can point a tab at another conversation."""

import logging

from lxml import etree

from markupsafe import Markup

from odoo import Command, api, fields, models
from odoo.exceptions import AccessError, MissingError, UserError
from odoo.tools import SQL

from ..tools.ids import as_id, defaults_set, real_ids, require_int_ids
from ..tools.record_text import cap, clean_data_text, field_text, json_line, mask_contacts, unique_labels
from .consult_mixin import (
    NOTES_CONTEXT, PROFILE, ROLE_PRIMARY, consult_keys, consult_state, is_consult_model, model_tabs, readable_ids)
from .discuss_channel import deleting, is_sync, trusted, with_sync
from .project_project import DEFAULT_PAUSE_MINUTES, PROJECT_USER_GROUP, TAB_CUSTOMER, TAB_INTERNAL, TABS

_logger = logging.getLogger(__name__)

# (db, field) of link specs that are not stored links to a consulted model, logged once per process
_spec_errors_logged = set()

# Task summary sent to the AI (system prompt), read with the asker's own rights.
# Internal chat: these fields; customer chat: those of them in Odoo's portal
# readable set of project.task (SELF_READABLE_FIELDS), without the related
# tasks (parent, subtasks): the customers of one chat may see different ones,
# and the answer is posted to all of them. Never the chatter.
CONTEXT_FIELDS = ('name', 'stage_id', 'date_deadline', 'user_ids', 'tag_ids', 'priority', 'parent_id',
                  'child_ids', 'description')
CUSTOMER_EXCLUDED_FIELDS = ('parent_id', 'child_ids')
# model-facing labels (Traditional Chinese, the Odoo zh_TW terms)
CONTEXT_LABELS = {
    'name': '任務', 'stage_id': '階段', 'date_deadline': '截止日期', 'user_ids': '受指派人', 'tag_ids': '標籤',
    'priority': '優先級別', 'parent_id': '上級任務', 'child_ids': '子任務', 'description': '說明',
}
CONTEXT_HEADER = '這張任務的資料（依提問者本人的權限讀取；不含留言與內部記錄）：'
MAX_CONTEXT_CHARS = 8000
MAX_RELATED_NAMES = 50
TRUNCATED = '…（以下省略）'
ACCESS_FIELDS = {'project_id', 'active', 'user_ids', 'company_id', 'parent_id'}
# written by this module only (refused in create / write otherwise, default_* context keys included)
GUARDED_FIELDS = {'woow_project_ai_alert_activity_id'}
CHANNEL_FIELDS = {'woow_project_ai_internal_channel_id', 'woow_project_ai_customer_channel_id'}
# what the To-Do of a task says (one open at a time)
ALERT_HANDOFF = 'handoff'
ALERT_BLOCKED = 'blocked'
ALERT_FAILED = 'failed'

# the AI consultant (models/consult_mixin.py): the flag of a consultation task, set by 「詢問 AI 顧問」 only
CONSULT_ENTRY_FIELD = 'woow_project_ai_consult_entry'
# the consulted records sent with a question: at most 5 per model and 8 in all; caps of one value, of one long
# (detail) value and of the whole block, then the trim ladder (_woow_project_ai_consult_lines)
CONSULT_MAX_PER_MODEL = 5
CONSULT_MAX_RECORDS = 8
CONSULT_MAX_VALUE = 300
CONSULT_MAX_DETAIL = 1500
CONSULT_MAX_CHARS = 6000
# the title of the consulted records' section carries the request's tag (record_text.CONSULT_DATA_NOTE): text
# imitating the section (a task description) cannot know it
CONSULT_HEADER = 'AI 顧問對象（每行一筆紀錄；標記 %s）：'
CONSULT_ROLE_LABELS = {ROLE_PRIMARY: '主要', 'extra': '其他'}


class ProjectTask(models.Model):
    _inherit = 'project.task'

    woow_project_ai_internal_channel_id = fields.Many2one(
        'discuss.channel', string='Internal AI Chat', compute='_compute_woow_project_ai_channels',
        groups='base.group_user')
    woow_project_ai_customer_channel_id = fields.Many2one(
        'discuss.channel', string='Customer AI Chat', compute='_compute_woow_project_ai_channels',
        groups='base.group_user')
    woow_project_ai_internal_available = fields.Boolean(
        string='Internal AI Chat Available', compute='_compute_woow_project_ai_available', groups='base.group_user')
    woow_project_ai_customer_available = fields.Boolean(
        string='Customer AI Chat Available', compute='_compute_woow_project_ai_available', groups='base.group_user')
    woow_project_ai_alert_activity_id = fields.Many2one(
        'mail.activity', string='AI To-Do', readonly=True, copy=False, ondelete='set null',
        groups='base.group_user',
        help='The open To-Do of the last time the customer AI chat needed a person (the customer asked for one, '
             'or the AI could not answer a customer): at most one at a time.')
    # the AI consultant (no default: adding the column does not rewrite the table)
    woow_project_ai_consult_entry = fields.Boolean(
        string='AI Consultation Topic', readonly=True, copy=False, groups='base.group_user',
        help='Created by "Ask the AI Consultant" on a record: the button continues the latest open one of these.')
    woow_project_ai_consult_summary = fields.Text(
        string='AI Consultant Records', compute='_compute_woow_project_ai_consult_summary',
        groups=PROJECT_USER_GROUP)
    woow_project_ai_consult_show = fields.Boolean(
        string='Show the AI Consultant Records', compute='_compute_woow_project_ai_consult_show',
        groups=PROJECT_USER_GROUP)

    @api.depends('project_id', 'project_id.woow_project_ai_internal_assistant_id',
                 'project_id.woow_project_ai_customer_assistant_id', 'project_id.privacy_visibility')
    def _compute_woow_project_ai_available(self):
        # sudo: project.project - only whether the project chose an assistant for the tab. The saved project:
        # a project changed in the form but not saved yet does not decide (its tab would refuse to open); the
        # customer tab only in a project shared with portal users (else no customer can ever join)
        for task in self:
            project = (task._origin or task).sudo().project_id
            task.woow_project_ai_internal_available = bool(project.woow_project_ai_internal_assistant_id)
            task.woow_project_ai_customer_available = bool(project.woow_project_ai_customer_assistant_id) \
                and project.privacy_visibility == 'portal'

    def _compute_woow_project_ai_channels(self):
        # through the chats' own link only (sudo: whether a chat exists is not sensitive; reading it is the
        # channel's own rules' business)
        chats = self.env['discuss.channel']._woow_project_ai_chats_of(self)
        by_key = {(chat.woow_project_ai_task_id.id, chat.woow_project_ai_tab): chat for chat in chats}
        empty = self.env['discuss.channel']
        for task in self:
            task.woow_project_ai_internal_channel_id = by_key.get((task.id, TAB_INTERNAL), empty)
            task.woow_project_ai_customer_channel_id = by_key.get((task.id, TAB_CUSTOMER), empty)

    def _woow_project_ai_tab_channel(self, tab):
        """(sudo) The AI conversation of this task's `tab`, found through the
        conversation's own link, or an empty recordset."""
        self.ensure_one()
        task_id = as_id(self.id)
        if not task_id:
            return self.env['discuss.channel'].sudo()
        return self.env['discuss.channel'].sudo().with_context(active_test=False).search([
            ('woow_project_ai_task_id', '=', task_id), ('woow_project_ai_tab', '=', tab)], limit=1)

    # --- Guards ---

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = list(vals_list)  # any iterable (a one-shot generator too): read again below
        sync = is_sync(self.env)
        if not sync and any((GUARDED_FIELDS | CHANNEL_FIELDS) & set(vals) for vals in vals_list):
            raise AccessError(self.env._('The AI conversations and the AI To-Do of a task are managed by the task '
                                         'itself.'))
        if not sync and any(CONSULT_ENTRY_FIELD in vals for vals in vals_list):
            raise AccessError(self._woow_project_ai_consult_entry_refusal())
        # consulted-record links: checked as stored (a link given as a string or a float id, a command, a
        # default_* key, an ir.default, a stored compute), only when something may have set one
        guarded = [] if sync or self.env.su else [
            spec for spec in self._woow_project_ai_consult_specs() if spec.guard and (
                any(spec.field in vals for vals in vals_list) or defaults_set(self, (spec.field,))
                or self._fields[spec.field].compute)]
        tasks = super().create(vals_list)
        if not sync:
            if tasks.sudo().filtered('woow_project_ai_alert_activity_id'):
                # set by a default (context, ir.default): the cache holds the stored value here, as Odoo's
                # default_get turns a many2one default into an int or False (a float or a string id gives False)
                raise AccessError(self.env._('The AI conversations and the AI To-Do of a task are managed by the '
                                             'task itself.'))
            if defaults_set(self, (CONSULT_ENTRY_FIELD,)) and tasks._woow_project_ai_consult_entry_stored():
                # a default_* key or an ir.default set the flag: checked as stored
                raise AccessError(self._woow_project_ai_consult_entry_refusal())
            if guarded:
                tasks._woow_project_ai_consult_check_links(guarded)
        return tasks

    # --- Who may be in a tab ---

    @staticmethod
    def _woow_project_ai_company_ids(user):
        """The companies `user` may work in (Odoo's env.companies accepts no
        other: an archived company still in their Allowed Companies would
        raise an access error)."""
        return list(user.sudo()._get_company_ids())

    def _woow_project_ai_user_allowed(self, user, tab, consult=True, gate=None):
        """True when `user` may be in the AI conversation `tab` of this task now:
        an active user of the tab's audience (internal chat: employees with
        Project rights who can read the task's project too, as the internal
        project text the AI gets, and quotes, is the project's, and every
        project whose text an earlier answer used; customer chat: portal users
        and employees) who can read the task by Odoo's own rules (portal: the
        portal task rules), over all of their active companies, and (the AI
        consultant, `consult`) every record of the chat's gate set: what was
        sent to its AI and the task's current links to records of enabled
        kinds (_woow_project_ai_consult_user_ok; `gate`: the set when the
        caller computed it already for this chat). Never sudo."""
        self.ensure_one()
        if not user or not user.active or user._is_public():
            return False
        if tab == TAB_INTERNAL and not (user._is_internal() and user.has_group(PROJECT_USER_GROUP)):
            return False
        if tab == TAB_CUSTOMER and not (user._is_internal() or user._is_portal()):
            return False
        companies = self._woow_project_ai_company_ids(user)
        task = self.with_user(user).with_context(active_test=False, allowed_company_ids=companies)
        try:
            if not task.has_access('read'):
                return False
            # the internal chat carries the project's internal text (in the AI's answers too): only for
            # people who can read the project, not only the task (a follower of a task in a project hidden
            # to them), and every earlier project whose text it used (a task moved to another project)
            if tab == TAB_INTERNAL:
                projects = task.sudo().project_id
                chat = task.sudo()._woow_project_ai_tab_channel(TAB_INTERNAL)
                if chat:
                    projects |= chat.woow_project_ai_brief_project_ids
                if not all(project.with_env(task.env).has_access('read') for project in projects):
                    return False
            # the consulted records (nothing to check while consultation is unused: no query)
            if consult and consult_state(self.env).used:
                return task.sudo()._woow_project_ai_consult_user_ok(user, tab, companies, gate=gate)
            return True
        except AccessError:
            return False

    # --- Opening a tab ---

    def woow_project_ai_tab_info(self, tab):
        """Backend task form, without any side effect: whether the current
        user is already a member of this task's AI conversation `tab`. The
        form pager shows that conversation at once; otherwise an "Open"
        button, so that paging through tasks never creates or joins one.

        Returns:
            dict: member (bool)
        """
        self.ensure_one()
        if tab not in TABS:
            raise UserError(self.env._('Unknown AI tab.'))
        self.check_access('read')  # the current user's own rights (an RPC)
        channel = self._woow_project_ai_tab_channel(tab)
        return {'member': bool(channel) and self.env.user.partner_id in channel.channel_member_ids.partner_id}

    def woow_project_ai_open_tab(self, tab):
        """Backend task form (current user): open this task's AI conversation
        `tab` ('internal' or 'customer'): created the first time, joined (an
        employee joins the customer chat muted).

        Returns:
            dict: channel_id, readonly (archived task: the tab can only be read)
        """
        self.ensure_one()
        require_int_ids(self)
        if tab not in TABS:
            raise UserError(self.env._('Unknown AI tab.'))
        self.check_access('read')  # the current user's own rights (an RPC: never sudo here)
        channel = self.sudo()._woow_project_ai_open_tab_for(self.env.user, tab)
        return {'channel_id': channel.id, 'readonly': not self.sudo().active}

    def _woow_project_ai_open_tab_for(self, user, tab):
        """Open the AI conversation `tab` of this task for `user` (callers check
        their own route; access is checked here again): find (through the
        conversation's own link) or create the channel (task row locked: two
        people opening at once get one), join. Returns the channel (sudo)."""
        self.ensure_one()
        task = self.sudo()
        assistant = task.project_id._woow_project_ai_tab_assistant(tab) if task.project_id \
            else task.env['woow.project.ai.assistant']
        if not assistant:
            raise UserError(self.env._('The project of this task has no AI assistant for this tab.'))
        if not task._woow_project_ai_user_allowed(user, tab):
            if task._woow_project_ai_user_allowed(user, tab, consult=False):
                # the task, but not every record its AI consulted (never named here)
                raise AccessError(self.env._('You cannot read the records this task consulted, so you cannot use '
                                             'its AI conversation.'))
            raise AccessError(self.env._('You cannot open this AI conversation: you need access to the task.'))
        channel = task._woow_project_ai_tab_channel(tab)
        if not channel:
            task._woow_project_ai_lock_for_create()
            channel = task._woow_project_ai_tab_channel(tab)
        created = False
        if not channel:
            if not task.active:
                raise UserError(self.env._('This task is archived: its AI conversations can only be read.'))
            channel = task._woow_project_ai_create_chat(tab, assistant, user)
            created = True
        else:
            channel._woow_project_ai_sync()  # the bot of the current assistant, nobody without access
        channel._woow_project_ai_join(user, created=created)
        if created and tab == TAB_CUSTOMER:
            welcome = (task.project_id.woow_project_ai_customer_welcome or '').strip()
            if welcome:
                assistant._post_text(channel, welcome)
        return channel

    def _woow_project_ai_lock_for_create(self):
        """Before creating a tab's conversation: lock the task row by updating
        it (a no-op update). Two people opening a tab for the first time at
        once: the second waits here, then fails with a serialization failure
        (its snapshot predates the first one's update), and Odoo retries its
        request, which then finds the conversation and joins it. A plain row
        lock (SELECT ... FOR UPDATE) does not fail after a commit that only
        locked the row: the second request would create a second conversation
        and hit the unique constraint instead (not retried)."""
        self.ensure_one()
        self.env.cr.execute("UPDATE project_task SET write_date = write_date WHERE id = %s", (self.id,))

    def _woow_project_ai_chat_name(self, tab):
        self.ensure_one()
        task = self.sudo()
        lang = self.env['woow.project.ai.assistant']._installed_lang(task.company_id.partner_id.lang, self.env.lang)
        env = self.with_context(lang=lang).env
        if tab == TAB_INTERNAL:
            return env._('%(task)s | Internal AI', task=task.name or '')
        return env._('%(task)s | Customer AI', task=task.name or '')

    def _woow_project_ai_create_chat(self, tab, assistant, user):
        """New private conversation of this task's `tab`: a group channel (no
        authorized group: only members read it) with the assistant's bot and
        `user` as members; portal users may upload files in the customer chat.
        Created from a clean context (the language only): nothing the client
        sent (default_* keys, install_mode...) reaches this trusted create."""
        self.ensure_one()
        task = self.sudo()
        context = {key: value for key, value in self.env.context.items() if key in ('lang', 'tz')}
        # created as `user` (discuss.channel.create always adds the current user)
        Channel = with_sync(self.env['discuss.channel'].with_user(user).with_context(context).sudo())
        channel = Channel.create({
            'name': task._woow_project_ai_chat_name(tab),
            'channel_type': 'group',
            'woow_project_ai_task_id': task.id,
            'woow_project_ai_tab': tab,
            'woow_project_ai_paused_until': False,
            'woow_project_ai_notice_sent': False,
            'parent_channel_id': False,
            'allow_public_upload': tab == TAB_CUSTOMER,
            'channel_member_ids': [Command.create({'partner_id': assistant.partner_id.id})],
        })
        _logger.info("Task %s: project AI conversation %s (%s) created by user %s", task.id, channel.id, tab, user.id)
        task.invalidate_recordset(list(CHANNEL_FIELDS))
        return self.env['discuss.channel'].sudo().browse(channel.id)

    # --- What the AI gets about the task ---

    def _woow_project_ai_context_block(self, user, tab, audience=None):
        """Short task summary for the AI, read as `user` (with_user, never
        sudo): what they can read themselves. Customer chat: only the fields
        of Odoo's portal readable set of project.task, without the related
        tasks. ``audience`` (res.users: the people who read the answer, the
        chat's members): of the related records (other tasks, tags, people...)
        only those every one of them can read too. At most 8,000 characters;
        never the chatter or log notes. '' when `user` cannot read the task."""
        self.ensure_one()
        if not user:
            return ''
        task = self.with_user(user).with_context(
            active_test=False, allowed_company_ids=self._woow_project_ai_company_ids(user),
            lang=user.lang or self.env.lang, tz=user.tz or 'UTC')
        try:
            if not task.has_access('read'):
                return ''
        except AccessError:
            return ''
        names = [name for name in CONTEXT_FIELDS if name in task._fields]
        if tab == TAB_CUSTOMER:
            readable = task.SELF_READABLE_FIELDS
            names = [name for name in names if name in readable and name not in CUSTOMER_EXCLUDED_FIELDS]
        lines, description = [], ''
        for name in names:
            try:
                text = task._woow_project_ai_field_text(name, audience)
            except (AccessError, MissingError):
                continue
            # a value of the data section: no line breaks but the description's, no marker look-alikes
            text = clean_data_text(text, one_line=name != 'description')
            if not text:
                continue
            if name == 'description':
                description = text
            else:
                lines.append('- %s：%s' % (CONTEXT_LABELS[name], text))
        block = '\n'.join([CONTEXT_HEADER] + lines)
        if description:
            head = '\n- %s：\n' % CONTEXT_LABELS['description']
            room = MAX_CONTEXT_CHARS - len(block) - len(head)
            if room > len(TRUNCATED):
                if len(description) > room:
                    description = description[:room - len(TRUNCATED)] + TRUNCATED
                block += head + description
        return block[:MAX_CONTEXT_CHARS]

    def _woow_project_ai_field_text(self, name, audience=None):
        """One field of this task as text, read with this record's user
        (tools/record_text.field_text: field access checked; related records
        filtered by the user's own access, whatever the shared cache holds,
        and by the access of every user of ``audience``; at most 50 names;
        people's names by the name rule, record_text.one_line_name)."""
        self.ensure_one()
        return field_text(self, name, audience_filter=lambda records: self._woow_project_ai_shared_with(
            records, audience), max_names=MAX_RELATED_NAMES)

    def _woow_project_ai_shared_with(self, records, audience):
        """Those of `records` that every user of `audience` can read too (an
        answer is read by all the members of the chat)."""
        for member in audience or ():
            if not records:
                break
            readable = records.with_user(member).with_context(
                allowed_company_ids=self._woow_project_ai_company_ids(member))._filtered_access('read')
            records = records.browse(readable.ids)
        return records

    # --- The AI consultant: links, guards, gate (models/consult_mixin.py) ---

    @api.model
    def _woow_project_ai_consult_link_specs(self):
        """The task fields that link consulted records (LinkSpec). None in the
        core: each bridge returns ``super() + [LinkSpec(...)]``."""
        return []

    @api.model
    def _woow_project_ai_consult_specs(self):
        """The link specs naming a stored many2one or many2many of project.task
        to a mixin model, primary first, then by sequence (others are logged
        once and ignored)."""
        specs = []
        for spec in self._woow_project_ai_consult_link_specs() or ():
            field = self._fields.get(spec.field)
            if field and field.store and field.type in ('many2one', 'many2many') \
                    and is_consult_model(self.env, field.comodel_name):
                specs.append(spec)
            else:
                key = (self.env.cr.dbname, spec.field)
                if key not in _spec_errors_logged:
                    _spec_errors_logged.add(key)
                    _logger.warning("Project AI consultant: link %s of project.task ignored (not a stored link to a "
                                    "consulted model)", spec.field)
        return sorted(specs, key=lambda spec: (spec.role != ROLE_PRIMARY, spec.sequence, spec.field))

    def _woow_project_ai_consult_stored(self, specs):
        """{field: {task id: {linked ids}}} of these tasks as stored (SQL after
        a flush: never the values given, nor the cache)."""
        result = {spec.field: {} for spec in specs}
        ids = real_ids(self)
        if not ids or not specs:
            return result
        self.flush_model([spec.field for spec in specs])
        for spec in specs:
            field = self._fields[spec.field]
            if field.type == 'many2one':
                column = SQL.identifier(field.name)
                query = SQL("SELECT id, %s FROM project_task WHERE id = ANY(%s) AND %s IS NOT NULL", column, ids,
                            column)
            else:
                query = SQL("SELECT %s, %s FROM %s WHERE %s = ANY(%s)", SQL.identifier(field.column1),
                            SQL.identifier(field.column2), SQL.identifier(field.relation),
                            SQL.identifier(field.column1), ids)
            for task_id, value in self.env.execute_query(query):
                result[spec.field].setdefault(task_id, set()).add(value)
        return result

    def _woow_project_ai_consult_check_links(self, specs, before=None):
        """Every record these tasks now link through `specs` and did not link
        `before` (_woow_project_ai_consult_stored) must be readable by the
        current user in the internal sense, and the user must have the link
        field's own groups (Odoo 18 checks them on write, not on create); the
        error never names a record."""
        stored = self._woow_project_ai_consult_stored(specs)
        user = self.env.user
        companies = self._woow_project_ai_company_ids(user) if user else []
        for spec in specs:
            old = (before or {}).get(spec.field) or {}
            added = set()
            for task_id, linked in stored[spec.field].items():
                added |= linked - (old.get(task_id) or set())
            if not added:
                continue
            self.check_field_access_rights('write', [spec.field])
            comodel = self._fields[spec.field].comodel_name
            if not user or added - readable_ids(self.env, comodel, added, user, TAB_INTERNAL, companies):
                raise AccessError(self.env._('You cannot read the record you are adding, so you cannot add it to '
                                             'the AI consultant records.'))

    def _woow_project_ai_consult_entry_refusal(self):
        return self.env._('Only "Ask the AI Consultant" marks a task as an AI consultation topic.')

    def _woow_project_ai_consult_entry_stored(self):
        """Whether one of these tasks is stored with the consultation flag set."""
        ids = real_ids(self)
        if not ids:
            return False
        self.flush_model([CONSULT_ENTRY_FIELD])
        rows = self.env.execute_query(SQL("SELECT bool_or(woow_project_ai_consult_entry) FROM project_task "
                                          "WHERE id = ANY(%s)", ids))
        return bool(rows and rows[0][0])

    def _woow_project_ai_consult_gate(self, tab, state=None):
        """The gate set of this task's AI chat `tab` (as stored; not cached
        across calls): {model: frozenset of ids} of the chat's ledger rows
        and of the current links of the enabled models whose records go to
        `tab`. Links are not capped (the caps apply to what is sent). Empty
        while consultation is unused."""
        self.ensure_one()
        state = state or consult_state(self.env)
        task_id = as_id(self.id)
        if not state.used or not task_id:
            return {}
        self.env['discuss.channel'].flush_model(['woow_project_ai_task_id', 'woow_project_ai_tab'])
        parts = [SQL("""
            SELECT s.res_model, s.res_id FROM woow_project_ai_consult_sent s
              JOIN discuss_channel c ON c.id = s.channel_id
             WHERE c.woow_project_ai_task_id = %s AND c.woow_project_ai_tab = %s""", task_id, tab)]
        specs = [spec for spec in self._woow_project_ai_consult_specs()
                 if self._fields[spec.field].comodel_name in state.enabled_models
                 and tab in model_tabs(self.env, self._fields[spec.field].comodel_name)]
        if specs:
            self.flush_model([spec.field for spec in specs])
        for spec in specs:
            field = self._fields[spec.field]
            if field.type == 'many2one':
                column = SQL.identifier(field.name)
                parts.append(SQL("SELECT %s::varchar, t.%s FROM project_task t WHERE t.id = %s AND t.%s IS NOT NULL",
                                 field.comodel_name, column, task_id, column))
            else:
                parts.append(SQL("SELECT %s::varchar, r.%s FROM %s r WHERE r.%s = %s", field.comodel_name,
                                 SQL.identifier(field.column2), SQL.identifier(field.relation),
                                 SQL.identifier(field.column1), task_id))
        gate = {}
        for model, res_id in self.env.execute_query(SQL(" UNION ALL ").join(parts)):
            gate.setdefault(model, set()).add(res_id)
        return {model: frozenset(ids) for model, ids in gate.items()}

    def _woow_project_ai_consult_user_ok(self, user, tab, companies, gate=None):
        """Whether `user` can read every record of the gate set of this task's
        AI chat `tab` (``readable_ids``, the consulted model's one
        readability source): a missing model or record fails, and so does a
        ledger row of a (model, tab) the registry has no key for."""
        self.ensure_one()
        state = consult_state(self.env)
        if gate is None:
            gate = self.sudo()._woow_project_ai_consult_gate(tab, state)
        if not gate:
            return True
        keys = consult_keys(self.env, state)
        for model, ids in gate.items():
            if (model, tab) not in keys:
                return False
            if ids - readable_ids(self.env, model, ids, user, tab, companies):
                return False
        return True

    def _woow_project_ai_consult_ledger_pairs(self):
        """(model, id) recorded in the AI chats of this task (both tabs)."""
        self.ensure_one()
        return self._woow_project_ai_consult_ledger_pairs_by_task().get(as_id(self.id), set())

    def _woow_project_ai_consult_ledger_pairs_by_task(self):
        """{task id: {(model, id)}} recorded in the AI chats of these tasks
        (both tabs): one query for the recordset."""
        task_ids = [task_id for task_id in (as_id(task.id) for task in self) if task_id]
        if not task_ids:
            return {}
        self.env['discuss.channel'].flush_model(['woow_project_ai_task_id'])
        rows = self.env.execute_query(SQL("""
            SELECT DISTINCT c.woow_project_ai_task_id, s.res_model, s.res_id FROM woow_project_ai_consult_sent s
              JOIN discuss_channel c ON c.id = s.channel_id
             WHERE c.woow_project_ai_task_id = ANY(%s)""", task_ids))
        pairs = {}
        for task_id, model, res_id in rows:
            pairs.setdefault(task_id, set()).add((model, res_id))
        return pairs

    # --- The AI consultant: the task page 「AI 顧問對象」 ---

    @api.depends_context('uid', 'lang')
    def _compute_woow_project_ai_consult_show(self):
        # with sudo: whether the page has anything to say (links, chats that consulted records, a consultation
        # project); no query unless consultation is configured
        state = consult_state(self.env)
        specs = self._woow_project_ai_consult_specs()
        ledger = self.sudo()._woow_project_ai_consult_ledger_pairs_by_task() if state.used else {}
        for task in self:
            show = False
            task_id = as_id(task.id)
            if task_id:
                task_sudo = task.sudo()
                show = any(task_sudo[spec.field] for spec in specs) \
                    or task_sudo.project_id.id in state.profile_project_ids or bool(ledger.get(task_id))
            task.woow_project_ai_consult_show = show

    @api.depends_context('uid', 'lang')
    def _compute_woow_project_ai_consult_summary(self):
        """The consulted records of the task for the current user: the links
        (read with sudo), the names only of what the user can read, the rest
        as a count."""
        _ = self.env._
        user = self.env.user
        state = consult_state(self.env)
        specs = self._woow_project_ai_consult_specs()
        tasks = self.filtered(lambda task: as_id(task.id)) if specs or state.used else self.browse()
        ledger = tasks.sudo()._woow_project_ai_consult_ledger_pairs_by_task() if state.used else {}
        # the links and earlier records of every task first, then one readability check per model for all
        plans, by_model = {}, {}
        for task in tasks:
            linked = []
            for spec in specs:
                for record in task.sudo()[spec.field]:
                    if (record._name, record.id) not in {(model, res_id) for model, res_id, _role in linked}:
                        linked.append((record._name, record.id, spec.role))
            earlier = sorted(ledger.get(as_id(task.id), set()) - {(model, res_id) for model, res_id, _role in linked})
            plans[task.id] = (linked, earlier)
            for model, res_id in [(model, res_id) for model, res_id, _role in linked] + earlier:
                by_model.setdefault(model, set()).add(res_id)
        companies = self._woow_project_ai_company_ids(user) if user and by_model else []
        readable = {model: readable_ids(self.env, model, ids, user, TAB_INTERNAL, companies)
                    for model, ids in by_model.items()}
        for task in self:
            if task.id not in plans:
                task.woow_project_ai_consult_summary = False
                continue
            linked, earlier = plans[task.id]
            lines, hidden = [], 0
            for model, res_id, role in linked:
                label = task._woow_project_ai_consult_label(model, res_id, companies) \
                    if res_id in readable[model] else None
                if not label:
                    hidden += 1
                    continue
                kind, name = label
                if role == ROLE_PRIMARY:
                    line = _('%(kind)s: %(name)s (primary)', kind=kind, name=name)
                else:
                    line = _('%(kind)s: %(name)s (other)', kind=kind, name=name)
                if model not in state.enabled_models:
                    line += ' ' + _('(not enabled: the AI does not read it)')
                lines.append(line)
            for model, res_id in earlier:
                label = task._woow_project_ai_consult_label(model, res_id, companies) \
                    if res_id in readable.get(model, ()) else None
                if not label:
                    hidden += 1
                    continue
                kind, name = label
                lines.append(_('Consulted before: %(kind)s: %(name)s', kind=kind, name=name))
            if hidden:
                lines.append(_('Records you cannot read: %(count)s', count=hidden))
            task.woow_project_ai_consult_summary = '\n'.join(lines) or False

    def _woow_project_ai_consult_label(self, model, res_id, companies):
        """(kind, name) of a record the current user can read, in their
        language, read over all of their active `companies` (as the
        readability check, whatever the company switcher selects); None when
        it cannot be read after all."""
        record = self.env[model].with_context(allowed_company_ids=companies, active_test=False).browse(res_id)
        lang = self.env.lang or self.env.user.lang
        try:
            if is_consult_model(self.env, model):
                kind = record._woow_project_ai_consult_kind(lang)
            else:
                kind = self.env['ir.model'].sudo().with_context(lang=lang)._get(model).name or model
            return kind, record.display_name or ''
        except (AccessError, MissingError):
            return None

    # --- The AI consultant: names ---

    @api.model
    def _woow_project_ai_consult_rename(self, renames):
        """{task id: name}: consultation tasks renamed (their AI chats follow,
        no tracking line keeps the old name), and the record names Odoo stored
        for them rewritten: on the messages of the tasks and of their chats
        (mail.message.record_name, written at post time) and on the tasks'
        activities (mail.activity.res_name, computed only when the activity is
        created). A deleted record's name stays nowhere the module wrote it
        (call logs with request content kept excepted, up to 30 days); the
        tracking lines of renames people made by hand are Odoo's history."""
        tasks = self.sudo().with_context(active_test=False).browse(list(renames)).exists()
        if not tasks:
            return
        for task in tasks:
            with_sync(task).with_context(tracking_disable=True, mail_notrack=True).write(
                {'name': renames[task.id]})
        self.env.flush_all()
        self.env.cr.execute("""
            UPDATE mail_message m SET record_name = c.name FROM discuss_channel c
             WHERE m.model = 'discuss.channel' AND m.res_id = c.id AND c.woow_project_ai_task_id = ANY(%s)""",
                            [tasks.ids])
        self.env.cr.execute("""
            UPDATE mail_message m SET record_name = t.name FROM project_task t
             WHERE m.model = 'project.task' AND m.res_id = t.id AND t.id = ANY(%s)""", [tasks.ids])
        self.env.cr.execute("""
            UPDATE mail_activity a SET res_name = t.name FROM project_task t
             WHERE a.res_model = 'project.task' AND a.res_id = t.id AND t.id = ANY(%s)""", [tasks.ids])
        self.env['mail.message'].invalidate_model(['record_name'])
        self.env['mail.activity'].invalidate_model(['res_name'])

    # --- The AI consultant: the task form ---

    @api.model
    def _get_view(self, view_id=None, view_type='form', **options):
        arch, view = super()._get_view(view_id, view_type, **options)
        if view_type == 'form':
            self._woow_project_ai_consult_view_page(arch)
        return arch, view

    @api.model
    def _woow_project_ai_consult_view_page(self, arch):
        """The 「AI 顧問對象」 page of the task form, added here and not by an
        inherited view: an image without this code (an older release, a
        rollback image) shows the task form without it, instead of failing on
        fields its registry does not have. After the customer AI chat's page
        (this module's own task form); consultant modules put their link
        fields into its group named woow_project_ai_consult_targets."""
        anchors = arch.xpath("//page[@name='woow_project_ai_customer_chat']")
        if not anchors or arch.xpath("//page[@name='woow_project_ai_consult']"):
            return
        _ = self.env._
        page = etree.Element('page', {
            'string': _('AI Consultant Records'), 'name': 'woow_project_ai_consult',
            'groups': 'project.group_project_user', 'invisible': 'not woow_project_ai_consult_show'})
        etree.SubElement(page, 'field', {'name': 'woow_project_ai_consult_show', 'invisible': '1'})
        note = etree.SubElement(page, 'div', {'class': 'text-muted mb-2'})
        note.text = _("When the AI answers, it reads some fields of the records below with the asker's own rights. "
                      "Only colleagues who can read these records (including the ones this task consulted before) "
                      "can use this task's AI chats.")
        etree.SubElement(page, 'field', {'name': 'woow_project_ai_consult_summary', 'nolabel': '1',
                                         'readonly': '1'})
        etree.SubElement(page, 'group', {'name': 'woow_project_ai_consult_targets'})
        anchors[0].addnext(page)

    # --- The AI consultant: what the AI gets ---

    def _woow_project_ai_consult_in_project(self, model, project):
        """Whether records of `model` may go to the AI of a task of
        `project`: only in the consultation project its setting chose (the
        AI the administrator reviewed under 「資料會送到」), or in the project
        the record itself chooses (_woow_project_ai_consult_project_from_record).
        A task linked to a record in any other project gets no record data,
        whoever created or moved it; its links and the records already sent
        still decide who may use its chats."""
        if not project:
            return False
        if is_consult_model(self.env, model) and self.env[model]._woow_project_ai_consult_project_from_record():
            return True
        profile = self.env[PROFILE].sudo().search([('model', '=', model)], limit=1)
        if profile.project_id != project:
            return False
        # the AI behind the project changed after a system administrator confirmed it: no record data until one
        # confirms again (the button refuses too)
        return not profile._woow_project_ai_consult_destination_changed()

    def _woow_project_ai_consult_targets(self, tab, state):
        """[(record (sudo), role)]: the current links of the enabled models
        whose records go to `tab`, primary first, then by spec sequence; at
        most 5 per model and 8 in all."""
        self.ensure_one()
        task = self.sudo()
        targets, seen, per_model = [], set(), {}
        for spec in self._woow_project_ai_consult_specs():
            model = self._fields[spec.field].comodel_name
            if model not in state.enabled_models or tab not in model_tabs(self.env, model):
                continue
            if not self._woow_project_ai_consult_in_project(model, task.project_id):
                continue  # a task created or moved elsewhere: that project's AI gets no record data
            for record in task[spec.field]:
                if len(targets) >= CONSULT_MAX_RECORDS:
                    return targets
                if (model, record.id) in seen or per_model.get(model, 0) >= CONSULT_MAX_PER_MODEL:
                    continue
                seen.add((model, record.id))
                per_model[model] = per_model.get(model, 0) + 1
                targets.append((record, spec.role))
        return targets

    def _woow_project_ai_consult_block(self, requester, tab, audience, tag):
        """The consulted records for the AI of `tab`, read as `requester`
        (with_user, never sudo; only what they can read): (block text, the
        (model, id) of the records that have a line in it, their models in
        order). The block's title carries `tag`, the request's delimiter tag.
        One JSON line per record, every value cleaned and capped, e-mail
        addresses, phone numbers and IP addresses in the name and in free text
        masked; long (detail) values only while the setting sends the Notes;
        related names only those every user of `audience` (the other people of
        the chat) can read too. Never the chatter, log notes, files, activities
        or properties. Which kinds are enabled and send their Notes is read
        from the settings now, not from the cached state: a switch-off reaches
        the next request also in a process whose cache is older."""
        self.ensure_one()
        state = consult_state(self.env)
        if not requester or not state.enabled_models:
            return '', [], []
        enabled, notes = self.env[PROFILE].sudo()._woow_project_ai_consult_live_models()
        state = state._replace(enabled_models=enabled, notes_models=notes)
        targets = self._woow_project_ai_consult_targets(tab, state)
        if not targets:
            return '', [], []
        companies = self._woow_project_ai_company_ids(requester)
        lang = self.env['woow.project.ai.assistant']._installed_lang(requester.lang, self.env.lang)
        readable = {}
        for model in {record._name for record, _role in targets}:
            ids = [record.id for record, _role in targets if record._name == model]
            readable[model] = readable_ids(self.env, model, ids, requester, tab, companies)
        entries = []
        for record, role in targets:
            if record.id not in readable[record._name]:
                continue
            # (NOTES_CONTEXT: the live decision of _values; always set here, whatever the caller's context had)
            mine = record.with_user(requester).with_context(
                active_test=False, allowed_company_ids=companies, lang=lang, tz=requester.tz or 'UTC',
                **{NOTES_CONTEXT: record._name in state.notes_models})
            try:
                kind = mine._woow_project_ai_consult_kind(lang)
                name = mine.display_name
                values = mine._woow_project_ai_consult_values(requester, tab, audience) or []
            except (AccessError, MissingError):
                continue
            notes = record._name in state.notes_models
            entries.append({
                'pair': (record._name, record.id),
                'primary': role == ROLE_PRIMARY,
                'kind': cap(clean_data_text(kind), CONSULT_MAX_VALUE),
                'role': CONSULT_ROLE_LABELS.get(role, CONSULT_ROLE_LABELS['extra']),
                'name': cap(mask_contacts(clean_data_text(name)), CONSULT_MAX_VALUE),
                'values': [
                    (cap(clean_data_text(label), CONSULT_MAX_VALUE),
                     cap(clean_data_text(text, one_line=not is_detail),
                         CONSULT_MAX_DETAIL if is_detail else CONSULT_MAX_VALUE),
                     bool(is_detail))
                    # (a module's own _values may return long values: still only while the Notes are sent)
                    for label, text, is_detail in values if text and (notes or not is_detail)],
            })
        header = CONSULT_HEADER % tag
        lines, sent = self._woow_project_ai_consult_lines(entries, header)
        if not lines:
            return '', [], []
        models_ = []
        for model, _res_id in sent:
            if model not in models_:
                models_.append(model)
        return '\n'.join([header] + lines), sent, models_

    @staticmethod
    def _woow_project_ai_consult_lines(entries, header):
        """(JSON lines, sent pairs) within CONSULT_MAX_CHARS, by the trim
        ladder: 1. no long values of extra records; 2. no long values at all;
        3. extra records: kind, role and name only; 4. whole lines cut, then
        「…（以下省略）」."""
        def render(level):
            lines = []
            for entry in entries:
                values = entry['values']
                if level >= 2 or (level >= 1 and not entry['primary']):
                    values = [value for value in values if not value[2]]
                if level >= 3 and not entry['primary']:
                    values = None
                elif values is not None:
                    values = unique_labels(values)
                lines.append(json_line(entry['kind'], entry['role'], entry['name'], values))
            return lines

        def size(lines):
            return len(header) + sum(len(line) + 1 for line in lines)

        lines = []
        for level in range(4):
            lines = render(level)
            if size(lines) <= CONSULT_MAX_CHARS:
                return lines, [entry['pair'] for entry in entries]
        kept = []
        for line in lines:
            if size(kept + [line]) + len(TRUNCATED) + 1 > CONSULT_MAX_CHARS:
                break
            kept.append(line)
        return kept + [TRUNCATED], [entry['pair'] for entry in entries[:len(kept)]]

    # --- The To-Do of the people in charge ---

    def _woow_project_ai_pause_minutes(self):
        self.ensure_one()
        project = self.sudo().project_id
        return project._woow_project_ai_pause_minutes_value() if project else DEFAULT_PAUSE_MINUTES

    def _woow_project_ai_alert_activity(self, kind, reason=None, minutes=0):
        """The customer AI chat needs a person: the customer asked for one
        (``kind`` handoff), the AI cannot answer a customer (``blocked``) or
        could not (``failed``: the backend failed, or the question waited too
        long). Called for every such customer message: one To-Do for the
        first assignee (else the project manager), unless one is still open on
        this task (it is reused, never doubled; once done, the next message
        schedules a new one). From a trusted context: in a customer's post,
        nothing the client sent (default_user_id, default_date_deadline,
        mail_activity_automation_skip...) reaches the To-Do."""
        self.ensure_one()
        task = trusted(self.sudo())
        current = task.woow_project_ai_alert_activity_id.with_context(active_test=False).exists()
        if current and current.active and current.res_model == 'project.task' and current.res_id == task.id:
            return current
        user = task.user_ids.filtered('active')[:1] or task.project_id.user_id
        if not user:
            _logger.warning("Task %s: the customer AI chat needs a person (%s), but the task has no assignee and the "
                            "project no manager: no To-Do", task.id, kind)
            return self.env['mail.activity']
        env = task.with_context(lang=self.env['woow.project.ai.assistant']._installed_lang(user.lang)).env
        names = ', '.join(task.user_ids.mapped('name'))
        if kind == ALERT_HANDOFF:
            summary = env._('Customer asks for a person')
            note = env._('The customer asked for a person in the customer AI chat of this task. The AI is paused for '
                         '%(minutes)s minutes.', minutes=minutes)
            if reason:
                note += ' ' + env._('Reason given by the AI: %(reason)s', reason=reason)
        elif kind == ALERT_FAILED:
            summary = env._('Customer waits for an answer')
            note = env._('A customer wrote in the customer AI chat of this task, but the AI could not answer (the AI '
                         'backend failed or did not answer in time; the customer got the error message). Please '
                         'answer the customer.')
        else:
            summary = env._('Customer waits for an answer')
            note = env._('A customer wrote in the customer AI chat of this task, but the AI cannot answer now (see '
                         'the "AI Assistants" page of the project). Please answer the customer.')
        if names:
            note += ' ' + env._('Assignees: %(names)s', names=names)
        activity = task.activity_schedule(
            'mail.mail_activity_data_todo', summary=summary, note=Markup('<p>%s</p>') % note, user_id=user.id)
        with_sync(task).woow_project_ai_alert_activity_id = activity
        return activity

    # --- Membership triggers ---

    def write(self, vals):
        sync = is_sync(self.env)
        if not sync and (GUARDED_FIELDS | CHANNEL_FIELDS) & set(vals):
            raise AccessError(self.env._('The AI conversations and the AI To-Do of a task are managed by the task '
                                         'itself.'))
        if not sync and CONSULT_ENTRY_FIELD in vals:
            raise AccessError(self._woow_project_ai_consult_entry_refusal())
        specs = self._woow_project_ai_consult_specs()
        links = {spec.field for spec in specs} & set(vals)
        # consulted-record links: the records added are checked as stored, before and after the write
        guarded = [spec for spec in specs if spec.guard and spec.field in vals] if links and not (
            sync or self.env.su) else []
        before = self._woow_project_ai_consult_stored(guarded) if guarded else None
        moved = self.browse()
        if {'project_id', 'parent_id'} & set(vals):
            # subtasks follow their parent's project without a write of their own
            moved = self.sudo().with_context(active_test=False)._get_all_subtasks()
        res = super().write(vals)
        if guarded:
            self._woow_project_ai_consult_check_links(guarded, before)
        keys = set(vals)
        if 'name' in keys:
            for chat in self.env['discuss.channel']._woow_project_ai_chats_of(self):
                with_sync(chat).name = chat.woow_project_ai_task_id._woow_project_ai_chat_name(chat.woow_project_ai_tab)
        if ACCESS_FIELDS & keys:
            # the task's project, archiving, assignees or company: who may read it
            self.env['discuss.channel']._woow_project_ai_sync_tasks(self | moved)
        elif links and consult_state(self.env).used:
            # a consulted record linked or unlinked: who may use the task's AI chats may change
            self.env['discuss.channel']._woow_project_ai_sync_tasks(self)
        return res

    def message_subscribe(self, partner_ids=None, subtype_ids=None):
        res = super().message_subscribe(partner_ids=partner_ids, subtype_ids=subtype_ids)
        if partner_ids:
            # a new follower only gains access (nobody to remove): the readable task chats of this transaction
            # are computed again
            self.env['discuss.channel']._woow_project_ai_forget_readable()
        return res

    def message_unsubscribe(self, partner_ids=None):
        res = super().message_unsubscribe(partner_ids=partner_ids)
        if partner_ids:
            self.env['discuss.channel']._woow_project_ai_sync_tasks(self)
        return res

    def unlink(self):
        # a deleted task deletes its AI conversations (messages and files with them), and so do the subtasks Odoo
        # deletes with it (project.task.unlink adds them, as found here: same context): their chats would
        # otherwise go with the database's cascade, which leaves their messages and files behind. The module's
        # own cascade (DELETING): Odoo's reads of the deleted messages and files as superuser are not checked
        # against the deleting user, who may not read the chats (the task's own access is checked by super())
        # (Odoo's own list is read in SQL after the chats are deleted, which flushes: the same values here)
        self.env['project.task'].flush_model(['active', 'parent_id'])
        tasks = self | self._get_all_subtasks()
        chats = self.env['discuss.channel']._woow_project_ai_chats_of(tasks)
        if chats:
            deleting(with_sync(chats)).unlink()
        return super().unlink()
