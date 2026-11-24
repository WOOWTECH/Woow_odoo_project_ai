# -*- coding: utf-8 -*-
"""The two AI conversations of a task: opening a tab (lazy creation, joining),
who may be in each, the task summary the AI gets, the To-Do that alerts the
people in charge, and the membership triggers of the task's own changes.

A tab's chat is always found through the chat's own link
(``discuss.channel.woow_project_ai_task_id`` / ``woow_project_ai_tab``, which
only this module writes): the task-side fields are computed from it, so no
task write can point a tab at another conversation."""

import logging

from markupsafe import Markup

from odoo import Command, api, fields, models
from odoo.exceptions import AccessError, MissingError, UserError

from .discuss_channel import html_to_text, is_sync, with_sync
from .project_project import DEFAULT_PAUSE_MINUTES, PROJECT_USER_GROUP, TAB_CUSTOMER, TAB_INTERNAL, TABS

_logger = logging.getLogger(__name__)

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
        if not isinstance(self.id, int):
            return self.env['discuss.channel'].sudo()
        return self.env['discuss.channel'].sudo().with_context(active_test=False).search([
            ('woow_project_ai_task_id', '=', self.id), ('woow_project_ai_tab', '=', tab)], limit=1)

    # --- Guards ---

    @api.model_create_multi
    def create(self, vals_list):
        if not is_sync(self.env) and any((GUARDED_FIELDS | CHANNEL_FIELDS) & set(vals) for vals in vals_list):
            raise AccessError(self.env._('The AI conversations and the AI To-Do of a task are managed by the task '
                                         'itself.'))
        tasks = super().create(vals_list)
        if not is_sync(self.env) and tasks.sudo().filtered('woow_project_ai_alert_activity_id'):
            # set by a default_* context key
            raise AccessError(self.env._('The AI conversations and the AI To-Do of a task are managed by the task '
                                         'itself.'))
        return tasks

    # --- Who may be in a tab ---

    @staticmethod
    def _woow_project_ai_company_ids(user):
        """The companies `user` may work in (Odoo's env.companies accepts no
        other: an archived company still in their Allowed Companies would
        raise an access error)."""
        return list(user.sudo()._get_company_ids())

    def _woow_project_ai_user_allowed(self, user, tab):
        """True when `user` may be in the AI conversation `tab` of this task now:
        an active user of the tab's audience (internal chat: employees with
        Project rights, as the internal project text the AI gets is theirs;
        customer chat: portal users and employees) who can read the task by
        Odoo's own rules (portal: the portal task rules), over all of their
        active companies. Never sudo."""
        self.ensure_one()
        if not user or not user.active or user._is_public():
            return False
        if tab == TAB_INTERNAL and not (user._is_internal() and user.has_group(PROJECT_USER_GROUP)):
            return False
        if tab == TAB_CUSTOMER and not (user._is_internal() or user._is_portal()):
            return False
        task = self.with_user(user).with_context(active_test=False,
                                                 allowed_company_ids=self._woow_project_ai_company_ids(user))
        try:
            return task.has_access('read')
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
        (field access checked; related records filtered by the user's own
        access, whatever the shared cache holds, and by the access of every
        user of ``audience``)."""
        self.ensure_one()
        self.check_field_access_rights('read', [name])
        field = self._fields[name]
        if field.type in ('one2many', 'many2many'):
            # read in this user's environment (record rules applied to the lines),
            # never from a list another environment left in the shared cache,
            # and not left there for others either
            self.invalidate_recordset([name])
            try:
                records = self._woow_project_ai_shared_with(self[name]._filtered_access('read'), audience)
                names = records[:MAX_RELATED_NAMES].mapped('display_name')
            finally:
                self.invalidate_recordset([name])
            return '、'.join(name for name in names if name)
        value = self[name]
        if field.type == 'many2one':
            return self._woow_project_ai_shared_with(value._filtered_access('read'), audience).display_name or ''
        if field.type == 'html':
            return html_to_text(value)
        if field.type == 'datetime':
            return fields.Datetime.context_timestamp(self, value).strftime('%Y-%m-%d %H:%M') if value else ''
        if field.type == 'date':
            return fields.Date.to_string(value) if value else ''
        if field.type == 'selection':
            return dict(field._description_selection(self.env)).get(value) or ''
        return ' '.join(str(value or '').split())

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
        schedules a new one)."""
        self.ensure_one()
        task = self.sudo()
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
        if not is_sync(self.env) and (GUARDED_FIELDS | CHANNEL_FIELDS) & set(vals):
            raise AccessError(self.env._('The AI conversations and the AI To-Do of a task are managed by the task '
                                         'itself.'))
        moved = self.browse()
        if {'project_id', 'parent_id'} & set(vals):
            # subtasks follow their parent's project without a write of their own
            moved = self.sudo().with_context(active_test=False)._get_all_subtasks()
        res = super().write(vals)
        keys = set(vals)
        if 'name' in keys:
            for chat in self.env['discuss.channel']._woow_project_ai_chats_of(self):
                with_sync(chat).name = chat.woow_project_ai_task_id._woow_project_ai_chat_name(chat.woow_project_ai_tab)
        if ACCESS_FIELDS & keys:
            # the task's project, archiving, assignees or company: who may read it
            self.env['discuss.channel']._woow_project_ai_sync_tasks(self | moved)
        return res

    # (no sync on message_subscribe: a new follower only gains access, nobody loses it)

    def message_unsubscribe(self, partner_ids=None):
        res = super().message_unsubscribe(partner_ids=partner_ids)
        if partner_ids:
            self.env['discuss.channel']._woow_project_ai_sync_tasks(self)
        return res

    def unlink(self):
        # a deleted task deletes its AI conversations (messages and files with them)
        chats = self.env['discuss.channel']._woow_project_ai_chats_of(self)
        if chats:
            with_sync(chats).unlink()
        return super().unlink()
