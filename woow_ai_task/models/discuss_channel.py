# -*- coding: utf-8 -*-
"""Task AI conversations: one private group channel per task and tab.

Only these channels (``woow_project_ai_task_id`` set) are touched: every
other channel (direct chats, groups, Live Chat, the Discuss AI chats of
woow_ai_contact) goes to ``super()`` unchanged. All names added here start
with ``woow_project_ai`` so that this module and woow_ai_contact install on
one database without sharing anything.

Membership is managed by this module only:

* people join when they open a tab (``project.task._woow_project_ai_open_tab_for``);
  the bot of the project's assistant for the tab is a member too;
* every other way to add a member (``add_members``, invitations and invitation
  links, ``/discuss/channel/join``, a direct ``discuss.channel.member``
  create, a channel write) is refused unless this module's own code does it
  (``SYNC``: a context value no JSON-RPC context can carry);
* members who lost the task access are removed by the sync (triggers on the
  access-relevant changes, and an hourly sweep), at once when they post, and
  when anybody else posts or the AI posts (in the same transaction as that
  message);
* until then they are still listed, but cannot read the conversation any more:
  the global rule ``rule_discuss_channel_woow_project_ai`` lets a user read a
  task chat only while they can read its task (``woow_project_ai_readable``),
  and the notifications of a message (web push, mentions) only reach people
  who can read the task now;
* live messages: Odoo keeps a websocket's channels from its last subscribe
  and polls them unchecked, and its client does not re-subscribe when it is
  removed; so every poll leaves out the task chats the polling user cannot
  read now (``bus_bus.py``): the message whose post removed them, and every
  later one, never reach their open pages.
* system administrators who can read the task read its chats too (Odoo's
  administrator rule on channels) without being members.

Nobody writes under another name: a non-superuser post (a JSON-RPC
``message_post``) or message (``mail.message``) must be the user's own; AI
assistants post as superuser.

Answering: every comment of the tab's audience (employees in the internal
chat, portal users in the customer chat) is answered, one question of each
person at a time (supersede policy "author"), author names in the history.
An employee's comment in the customer chat pauses the AI; staff turns are
labelled. A tab whose assistant cannot answer (see
``project.project._woow_project_ai_tab_problems``) calls no backend and posts
one short notice per blocked period.
"""

import html as html_lib
import logging
import re
import threading
import time
import unicodedata
from datetime import datetime, timedelta
from email.utils import parseaddr

import psycopg2
from markupsafe import Markup

from odoo import SUPERUSER_ID, Command, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.osv import expression
from odoo.service.model import PG_CONCURRENCY_EXCEPTIONS_TO_RETRY
from odoo.tools import SQL
from odoo.tools.misc import clean_context

from odoo.addons.mail.tools.discuss import Store

from ..tools.ids import as_id, defaults_set, real_ids
from .project_project import PROJECT_USER_GROUP, TAB_CUSTOMER, TAB_INTERNAL

_logger = logging.getLogger(__name__)

_BREAK_RE = re.compile(r'<\s*(br|/p|/div|/li|/tr|/h\d)\s*/?\s*>', re.IGNORECASE)
_CELL_RE = re.compile(r'<\s*/t[dh]\s*>', re.IGNORECASE)
_TAG_RE = re.compile(r'<[^>]+>')

MAX_MESSAGE_TEXT = 16000  # characters of one history message sent to a backend
TRUNCATED_MARKER = '\n…[truncated]'
MAX_AUTHOR_NAME = 80  # characters of an author name in the history
_ANGLE_RUN_RE = re.compile(r'<{3,}|>{3,}')

# The context value of this module's own membership changes. An object, so a
# client cannot forge it (JSON-RPC contexts only carry JSON values), like
# mail's ``mail_create_bypass_create_check``.
SYNC = object()
SYNC_KEY = 'woow_project_ai_sync'
# set by set_message_pin around Odoo's own pin notice (same idea: an object no client can send)
PIN = object()
PIN_KEY = 'woow_project_ai_pin'
BOTS_KEY = 'woow_project_ai_bots'
TRIGGER_KEY = 'woow_project_ai_trigger_id'
# set by message_post around its own message create (mail_message.py refuses every other direct create of a
# task chat message): an instance no client can send, holding the chat it is for
POST_KEY = 'woow_project_ai_post'


class PostMarker:
    __slots__ = ('channel_id',)

    def __init__(self, channel_id):
        self.channel_id = channel_id


def posted_by_this_module(env, channel_id):
    marker = env.context.get(POST_KEY)
    return isinstance(marker, PostMarker) and marker.channel_id == channel_id


def for_a_user(env):
    """Whether `env` acts for a real user (also as superuser): not the
    superuser account, not a request without a user (``auth='none'``)."""
    return env.uid not in (None, SUPERUSER_ID)

TASK_LINK_FIELDS = {'woow_project_ai_task_id', 'woow_project_ai_tab'}
# {channel id: is a task chat}, kept on the registry (a new registry, after a module update or a database
# restore, starts empty; the link is set when a chat is created and never changes, ids are never reused);
# started again when it grows past MAX_CACHED_IDS
TASK_CHAT_CACHE = '_woow_project_ai_task_chat_ids'
# the module's rule for a user who can read no task chat
NO_CHAT_SQL = 'SELECT NULL::integer WHERE FALSE'
NO_CHAT_RULE = {'admin': False, 'ids': []}
MAX_CACHED_IDS = 50000
# the hourly membership sweep: where it stopped (a channel id, kept with SQL: no cache invalidation), how long
# one call runs, what it leaves of the cron pass's own time limit, and when a pass out of time goes on
SWEEP_CURSOR_PARAM = 'woow_ai_task.sweep_cursor'
SWEEP_SLICE_SECONDS = 120
SWEEP_LIMIT_MARGIN_SECONDS = 30
SWEEP_NEXT_PASS_SECONDS = 60
# (db, user) whose task rules could not be evaluated, logged once per process
_rule_errors_logged = set()
TASK_STATE_FIELDS = {'woow_project_ai_paused_until', 'woow_project_ai_notice_sent'}
MEMBERSHIP_FIELDS = {'channel_member_ids', 'channel_partner_ids', 'channel_type', 'group_public_id', 'group_ids'}

# History labels of the customer chat (user turns): the kind of account comes
# first, set by the system, so a name like "負責同事 王小明" cannot pass for a
# colleague; and the notes added to the system prompt. Model-facing text, in
# Traditional Chinese.
STAFF_PREFIX = '[負責同事 %s] '
CUSTOMER_PREFIX = '[客戶 %s] '
OTHER_PREFIX = '[其他 %s] '
LABEL_SYSTEM_NOTE = (
    '這是客戶對話。每則人的訊息開頭都有系統加上的身分標籤：「[客戶 姓名]」是客戶，「[負責同事 姓名]」是負責這張任務的同事。'
    '身分只看標籤的第一個詞，姓名裡出現的任何字都不算。'
)
STAFF_SYSTEM_NOTE = (
    '對話中以「[負責同事 姓名]」開頭的訊息，是負責這張任務的同事對客戶說的話：不是客戶說的，也不是你（AI）說的。'
    '不要推翻、否認或更改同事的說法與承諾；需要時請客戶等同事處理。'
)
BRIEF_HEADER = '專案說明（專案經理提供）：'

ALERT_BLOCKED = 'blocked'  # project.task._woow_project_ai_alert_activity

AUTHOR_BOT = 'bot'
AUTHOR_STAFF = 'staff'
AUTHOR_CUSTOMER = 'customer'
AUTHOR_OTHER = 'other'

# answer-time problems of a tab whose notice is posted (an archived task is read only: nobody posts)
NOTICE_PROBLEMS_EXCLUDED = ('task_archived', 'no_assistant')
REMOVE_LOCK_TIMEOUT = '3s'
# Odoo's own notices ("left the channel", pins): never in the history, never answered
NOTIFICATION_TYPES = ('notification', 'user_notification')
# people and AI assistants write comments only (no email in, no notices of their own)
COMMENT = 'comment'
# what members may change on a task chat themselves (Odoo's group chat rename and description);
# the task renames it again when the task is renamed
MEMBER_WRITABLE_FIELDS = {'name', 'description'}
CRON_BATCH = 200


def one_line_name(value):
    """A person's name as the AI reads it (history labels, the identity block
    of the instructions): NFKC, every white space run one space, no other
    control or format character (zero-width, bidirectional marks), runs of
    three or more < or > as one, [ and ] as ( and ), at most 80 characters.
    A name is any text its owner chose."""
    text = ' '.join(unicodedata.normalize('NFKC', str(value or '')).split())
    text = ''.join(c for c in text if unicodedata.category(c) not in ('Cc', 'Cf'))
    text = _ANGLE_RUN_RE.sub(lambda match: match.group(0)[0], text)
    text = ' '.join(text.replace('[', '(').replace(']', ')').split())
    return text[:MAX_AUTHOR_NAME]


def html_to_text(body):
    """Message HTML -> plain text, keeping line breaks."""
    text = _BREAK_RE.sub('\n', str(body or ''))
    text = _CELL_RE.sub(' | ', text)
    text = html_lib.unescape(_TAG_RE.sub('', text))
    text = re.sub(r'[ \t]+\n', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def with_sync(records):
    """`records` with the context of this module's own changes (members,
    links, pause): the caller's default_* keys are dropped, so nothing a
    client put in its context reaches these trusted writes."""
    return records.with_context(clean_context(records.env.context), **{SYNC_KEY: SYNC})


def is_sync(env):
    return env.context.get(SYNC_KEY) is SYNC


class DiscussChannel(models.Model):
    _inherit = 'discuss.channel'

    woow_project_ai_task_id = fields.Many2one(
        'project.task', string='Project AI Task', readonly=True, copy=False, ondelete='cascade',
        index='btree_not_null',
        help='Task this AI conversation belongs to (project AI). Its members are managed by the task.')
    woow_project_ai_tab = fields.Selection(
        [(TAB_INTERNAL, 'Internal AI Chat'), (TAB_CUSTOMER, 'Customer AI Chat')], string='Project AI Tab',
        readonly=True, copy=False)
    woow_project_ai_paused_until = fields.Datetime(
        string='Project AI Paused Until', readonly=True, copy=False,
        help='Customer AI chat: the AI does not answer before this time (an employee wrote, or the backend '
             'handed the customer over to a person).')
    woow_project_ai_notice_sent = fields.Boolean(
        string='Project AI Unavailable Notice Sent', readonly=True, copy=False,
        help='The short "the AI cannot answer" notice was posted for the current blocked period.')
    woow_project_ai_readable = fields.Boolean(
        string='Project AI Chat Readable', compute='_compute_woow_project_ai_readable',
        search='_search_woow_project_ai_readable',
        help='Not a task AI chat, or the current user can read its task now (internal chats: employees with '
             'Project rights who can read its project too).')
    woow_project_ai_brief_project_ids = fields.Many2many(
        'project.project', 'woow_project_ai_channel_brief_project_rel', 'channel_id', 'project_id',
        string='Projects of the Internal Texts Used', readonly=True, copy=False, groups='base.group_system',
        help='Internal chat: the projects whose internal text the AI got for an answer here. The chat stays '
             'with people who can read every one of them (a task moved to another project keeps its earlier '
             'answers away from readers of the new project only).')

    _sql_constraints = [
        ('woow_project_ai_tab_uniq', 'unique(woow_project_ai_task_id, woow_project_ai_tab)',
         'A task has one AI conversation per tab.'),
        ('woow_project_ai_tab_check',
         'CHECK((woow_project_ai_task_id IS NULL) = (woow_project_ai_tab IS NULL))',
         'A task AI conversation needs both its task and its tab.'),
    ]

    # --- Reading: the live rule ---

    @api.depends_context('uid')
    def _compute_woow_project_ai_readable(self):
        # any other channel: readable as far as this module goes, without a query (the registry cache of the
        # task chats); task chats: one query for the batch (the user's compiled rule); per user in the cache
        # (record rule checks read this field through filtered_domain)
        # ids as Odoo matches them in SQL (an id sent as 4242.0 is the channel 4242)
        ids = {channel: channel_id for channel in self if (channel_id := as_id(channel.id))}
        chats = self._woow_project_ai_known_task_chats(set(ids.values())) if ids else set()
        readable = self._woow_project_ai_readable_chat_ids(chats) if chats else set()
        for channel in self:
            channel_id = ids.get(channel)
            channel.woow_project_ai_readable = channel_id not in chats or channel_id in readable

    def _search_woow_project_ai_readable(self, operator, value):
        if operator not in ('=', '!=') or not isinstance(value, bool):
            raise UserError(self.env._('This field can only be searched as equal to True or False.'))
        # the global rule of every channel search
        leaves = [('id', 'in', value_) for value_ in self._woow_project_ai_readable_values()]
        if (operator == '=') == value:
            return expression.OR([[('woow_project_ai_task_id', '=', False)]] + [[leaf] for leaf in leaves])
        return expression.AND([[('woow_project_ai_task_id', '!=', False)]]
                              + [[('id', 'not in', leaf[2])] for leaf in leaves])

    @api.model
    def _woow_project_ai_rule(self):
        """The module's rule for the current user (``env.uid``, also under
        sudo: record rules are evaluated as superuser), for this transaction:
        kept in the field cache on the user
        (``res.users.woow_project_ai_readable_rule``, cleared at the end of the
        transaction and by ``_woow_project_ai_forget_readable``). A request
        without a user (``auth='none'``): no task chat."""
        if not self.env.uid:
            return dict(NO_CHAT_RULE)
        return self.env['res.users'].sudo().browse(self.env.uid).woow_project_ai_readable_rule or dict(NO_CHAT_RULE)

    @api.model
    def _woow_project_ai_readable_values(self):
        """The task chats the current user may read now, as values of ``in``
        domain leaves on a channel id, to be OR-ed in this order: the list of
        their own readable member task chats (nothing to plan; most reads,
        administrators' included, stop there: PostgreSQL evaluates the leaves
        of an OR in order), then, for a system administrator only (Odoo lets
        them read every channel, others read a group chat only as members, and
        task chats are group chats), the compiled sub-query of all the task
        chats they may read, which PostgreSQL evaluates once per statement and
        only when it meets a task chat that is not in the list."""
        rule = self._woow_project_ai_rule()
        values = []
        if rule.get('ids'):
            values.append(list(rule['ids']))
        if rule.get('admin'):
            to_flush = {}
            for model_name, field_name in rule.get('flush') or ():
                to_flush.setdefault(model_name, []).append(field_name)
            for model_name, field_names in to_flush.items():
                if model_name in self.env:
                    # the fields the compiled SQL reads, flushed as Odoo flushes a query's fields before running it
                    self.env[model_name].flush_model(field_names)
            values.append(SQL("(%s)", SQL((rule.get('code') or NO_CHAT_SQL).replace('%', '%%'))))
        return values

    @api.model
    def _woow_project_ai_forget_readable(self):
        """A task chat was created, someone joined or left one, or a task,
        project, follower, collaborator, user or group changed: the rule is
        computed again, and the per-chat answers of this transaction too, at
        their next use."""
        self.env['res.users'].invalidate_model(['woow_project_ai_readable_rule'], flush=False)
        self.invalidate_model(['woow_project_ai_readable'], flush=False)

    @api.model
    def _woow_project_ai_rule_of(self, user):
        """The module's rule for `user`: the task AI chats they may read now: a
        task chat while they can read its task, by Odoo's own rules over all of
        their active companies; the internal chat only for employees with
        Project rights who can read the task's project too, and every project
        whose internal text an answer of the chat used (archived ones too).
        No chat for public users, inactive users, users who cannot read tasks,
        or whenever the rules cannot be evaluated for them (task chats stay
        closed to them; every other channel is unchanged).

        {'ids': their member task chats they may read (one indexed query for
        their memberships, then, only if they are in a task chat, one for
        those), 'admin': False}. A system administrator (Odoo's own rule lets
        them read every channel) also gets 'admin': True, 'code': the SQL of
        all the task chats they may read, compiled (their rules in it, the data
        read at each use) and 'flush': [[model, field], ...] it reads."""
        try:
            if not user or not user.active or user._is_public():
                return dict(NO_CHAT_RULE)
            admin = user._is_system()
            self.flush_model(['woow_project_ai_task_id', 'woow_project_ai_tab', 'woow_project_ai_brief_project_ids'])
            self.env['discuss.channel.member'].flush_model(['channel_id', 'partner_id'])
            rows = self.env.execute_query(SQL("""
                SELECT c.id FROM discuss_channel_member m
                  JOIN discuss_channel c ON c.id = m.channel_id AND c.woow_project_ai_task_id IS NOT NULL
                 WHERE m.partner_id = %s""", user.partner_id.id))
            member_chats = sorted({row[0] for row in rows})
            if not member_chats and not admin:
                return dict(NO_CHAT_RULE)
            # the user's active companies only: an archived company still in their Allowed Companies would
            # make env.companies (the task rules) raise
            companies = list(user._get_company_ids())
            Task = self.env['project.task'].with_user(user).with_context(
                active_test=False, allowed_company_ids=companies)
            if not Task.has_access('read'):
                return dict(NO_CHAT_RULE)
            internal = user._is_internal() and user.has_group(PROJECT_USER_GROUP)
            projects = None
            if internal:
                projects = self.env['project.project'].with_user(user).with_context(
                    active_test=False, allowed_company_ids=companies)._search([])

            def readable_chats(chats_of_tasks, restrict_to=None):
                # the user's readable tasks among the tasks of those chats (their rules applied: not sudo)
                tasks = Task._search([('id', 'in', chats_of_tasks)])
                domain = ['&', ('woow_project_ai_tab', '=', TAB_CUSTOMER), ('woow_project_ai_task_id', 'in', tasks)]
                if internal:
                    # the internal chats: the task's project readable too (its internal text is in the chat): the
                    # chats' tasks with such a project, apart from `tasks` (a sub-query of `tasks` would be a
                    # semi-join of the task rules, which PostgreSQL may plan as a nested loop over all tasks)
                    project_tasks = Task.sudo()._search([
                        ('id', 'in', chats_of_tasks), '|', ('project_id', '=', False), ('project_id', 'in', projects)])
                    domain = ['|'] + domain + [
                        '&', '&', '&', ('woow_project_ai_tab', '=', TAB_INTERNAL),
                        ('woow_project_ai_task_id', 'in', tasks), ('woow_project_ai_task_id', 'in', project_tasks),
                        # ... and every project whose internal text an answer used, archived ones included (no
                        # active filter): one sub-query PostgreSQL evaluates once, not once per chat
                        ('id', 'not in', SQL("(SELECT channel_id FROM woow_project_ai_channel_brief_project_rel "
                                             "WHERE project_id NOT IN %s)", projects.subselect()))]
                channels = ['&', ('woow_project_ai_task_id', '!=', False)] + domain
                if restrict_to is not None:
                    channels = ['&', ('id', 'in', restrict_to)] + channels
                # sudo: the rules are in the sub-queries above
                return self.sudo().with_context(active_test=False)._search(channels).select()

            ids = []
            if member_chats:
                sql = readable_chats(SQL("(SELECT woow_project_ai_task_id FROM discuss_channel WHERE id = ANY(%s))",
                                         member_chats), restrict_to=member_chats)
                ids = sorted(row[0] for row in self.env.execute_query(sql))
            if not admin:
                return {'admin': False, 'ids': ids}
            sql = readable_chats(SQL("(SELECT woow_project_ai_task_id FROM discuss_channel "
                                     "WHERE woow_project_ai_task_id IS NOT NULL)"))
            flush = {(field.model_name, field.name) for field in sql.to_flush}
            flush.add(('discuss.channel', 'woow_project_ai_brief_project_ids'))  # raw SQL above: not in to_flush
            return {'admin': True, 'ids': ids, 'code': self.env.cr.mogrify(sql.code, sql.params).decode(),
                    'flush': sorted(flush)}
        except psycopg2.Error:
            raise  # the transaction is aborted: swallowing it would only move the failure to the next query
        except Exception:  # noqa: BLE001 - AccessError, a task rule that cannot be evaluated for this user...
            key = (self.env.cr.dbname, user.id)
            if key not in _rule_errors_logged:
                _rule_errors_logged.add(key)
                _logger.warning("Project AI: user %s cannot be checked against the task rules; no task AI chat "
                                "is readable for them", user.id, exc_info=True)
            return dict(NO_CHAT_RULE)

    @api.model
    def _woow_project_ai_known_task_chats(self, ids):
        """Those of the channel `ids` (ints) that are task AI chats, from a
        per-process cache on the registry: a query only for ids this process
        never saw. Only channels that exist are remembered (a channel's task
        link is set in its INSERT and never changes, ids are never reused): an
        id without a row (a channel not created yet, or not visible to this
        transaction yet) is asked again next time, so that no lookup can mark
        a future task chat as an ordinary channel."""
        ids = {i for i in ids if isinstance(i, int)}
        if not ids:
            return set()
        registry = self.env.registry
        known = getattr(registry, TASK_CHAT_CACHE, None)
        if known is None or len(known) > MAX_CACHED_IDS:
            known = {}
            setattr(registry, TASK_CHAT_CACHE, known)
        unknown = [i for i in ids if i not in known]
        if unknown:
            self.flush_model(['woow_project_ai_task_id'])
            self.env.cr.execute("SELECT id, woow_project_ai_task_id IS NOT NULL FROM discuss_channel "
                                "WHERE id = ANY(%s)", [unknown])
            for channel_id, is_chat in self.env.cr.fetchall():
                known[channel_id] = is_chat
        return {i for i in ids if known.get(i)}

    @api.model
    def _woow_project_ai_readable_cached(self, chat_ids):
        """_woow_project_ai_readable_chat_ids through the per-user computed
        field woow_project_ai_readable: computed once per batch and then kept
        in the cache for the transaction, as Odoo's own rule checks keep it."""
        chats = self.sudo().with_context(active_test=False).browse(sorted(chat_ids))
        return {chat.id for chat in chats if chat.woow_project_ai_readable}

    @api.model
    def _woow_project_ai_readable_chat_ids(self, chat_ids):
        """Those of the task chat ids `chat_ids` that the current user
        (env.uid, also under sudo) may read under the module's rule (a system
        administrator: one query for these chats; anyone else: their list)."""
        if not chat_ids:
            return set()
        values = self._woow_project_ai_readable_values()
        listed = set(values[0]) if values and isinstance(values[0], list) else set()
        readable = set(chat_ids) & listed
        others = sorted(set(chat_ids) - listed)
        compiled = [value for value in values if isinstance(value, SQL)]
        if others and compiled:
            rows = self.env.execute_query(SQL("SELECT id FROM discuss_channel WHERE id = ANY(%s) AND id IN %s",
                                              others, compiled[0]))
            readable |= {row[0] for row in rows}
        return readable

    # --- Helpers ---

    def _woow_project_ai_is_task_chat(self):
        return len(self) == 1 and bool(self.sudo().woow_project_ai_task_id)

    @api.model
    def _woow_project_ai_bot_partner_ids(self):
        """Partners that are bots: every project AI assistant (archived too) and OdooBot."""
        cached = self.env.context.get(BOTS_KEY)
        if isinstance(cached, frozenset):
            # set by _woow_project_ai_history; a client's context (JSON) can never carry a frozenset
            return cached
        ids = set(self.env['woow.project.ai.assistant'].sudo().with_context(active_test=False).search(
            []).partner_id.ids)
        root = self.env.ref('base.partner_root', raise_if_not_found=False)
        if root:
            ids.add(root.id)
        return frozenset(ids)

    def _woow_project_ai_tab_assistant(self):
        """(sudo) The AI assistant the task's project chose for this chat's tab
        (archived ones too), or an empty recordset."""
        self.ensure_one()
        channel = self.sudo()
        project = channel.woow_project_ai_task_id.project_id
        if not project:
            return self.env['woow.project.ai.assistant'].sudo()
        return project._woow_project_ai_tab_assistant(channel.woow_project_ai_tab)

    def _woow_project_ai_problems(self, assistant=None):
        """Why the AI must not answer this chat now: list of codes, [] = it answers."""
        self.ensure_one()
        channel = self.sudo()
        task = channel.woow_project_ai_task_id
        if not task.active:
            return ['task_archived']
        assistant = assistant if assistant is not None else channel._woow_project_ai_tab_assistant()
        if not assistant:
            return ['no_assistant']
        return task.project_id._woow_project_ai_tab_problems(channel.woow_project_ai_tab, assistant)

    def _woow_project_ai_partner_allowed(self, partner):
        """True when `partner` has a user who may be in this chat now (the
        task's ``_woow_project_ai_user_allowed``: active, the tab's audience,
        Odoo's read access to the task)."""
        self.ensure_one()
        channel = self.sudo()
        if not partner:
            return False
        users = partner.sudo().with_context(active_test=False).user_ids
        task = channel.woow_project_ai_task_id
        return any(task._woow_project_ai_user_allowed(user, channel.woow_project_ai_tab) for user in users)

    def _woow_project_ai_author_kind(self, message):
        """bot / staff (an employee) / customer (a portal user) / other."""
        author = message.sudo().author_id
        if not author or message.sudo().author_guest_id:
            return AUTHOR_OTHER
        if author.id in self._woow_project_ai_bot_partner_ids():
            return AUTHOR_BOT
        users = author.sudo().with_context(active_test=False).user_ids.filtered('active')
        if any(user._is_internal() for user in users):
            return AUTHOR_STAFF
        if any(user._is_portal() for user in users):
            return AUTHOR_CUSTOMER
        return AUTHOR_OTHER

    def _woow_project_ai_audience(self):
        """Author kind answered in this chat."""
        self.ensure_one()
        return AUTHOR_STAFF if self.sudo().woow_project_ai_tab == TAB_INTERNAL else AUTHOR_CUSTOMER

    # --- Guards: only this module adds members ---

    @api.model_create_multi
    def create(self, vals_list):
        if not is_sync(self.env) and any(TASK_LINK_FIELDS & set(vals) for vals in vals_list):
            raise AccessError(self.env._('Task AI conversations are created by their task only.'))
        channels = super().create(vals_list)
        if is_sync(self.env) or not self._woow_project_ai_plain_channels(vals_list):
            if channels._woow_project_ai_check_stored_channels(not is_sync(self.env)):
                # a new task chat: the readable task chats of this transaction are computed again
                self._woow_project_ai_forget_readable()
        return channels

    @api.model
    def _woow_project_ai_plain_channels(self, vals_list):
        """Whether these channels can be nothing of a task chat (no query: most
        channels): no task link (refused above), no parent channel, not made
        from a message, and no default for any of those."""
        if defaults_set(self, ('woow_project_ai_task_id', 'woow_project_ai_tab', 'parent_channel_id',
                               'from_message_id')):
            return False
        return all(not vals.get('parent_channel_id') and not vals.get('from_message_id') for vals in vals_list)

    def _woow_project_ai_check_stored_channels(self, refuse_links):
        """The created channels as stored (Odoo writes a many2one given as a
        string or a float straight into the INSERT while its cache, and so its
        own constraints, see none): no task link outside this module (default_*
        context, ir.default, string ids...), and, for everyone, no sub-channel
        of a task chat nor one made from a task-chat message (Odoo serializes
        from_message_id with sudo to the sub-channel's readers)."""
        ids = real_ids(self)
        if not ids:
            return
        self.flush_recordset()
        self.env.cr.execute("""
            SELECT bool_or(c.woow_project_ai_task_id IS NOT NULL OR c.woow_project_ai_tab IS NOT NULL),
                   bool_or(pc.id IS NOT NULL OR mc.id IS NOT NULL)
              FROM discuss_channel c
              LEFT JOIN discuss_channel pc ON pc.id = c.parent_channel_id AND pc.woow_project_ai_task_id IS NOT NULL
              LEFT JOIN mail_message m ON m.id = c.from_message_id AND m.model = 'discuss.channel'
              LEFT JOIN discuss_channel mc ON mc.id = m.res_id AND mc.woow_project_ai_task_id IS NOT NULL
             WHERE c.id = ANY(%s)""", [ids])
        linked, thread = self.env.cr.fetchone()
        if linked and refuse_links:
            raise AccessError(self.env._('Task AI conversations are created by their task only.'))
        if thread:
            raise AccessError(self.env._('A task AI conversation has no threads.'))
        return bool(linked)

    def write(self, vals):
        if not is_sync(self.env):
            keys = set(vals)
            if TASK_LINK_FIELDS & keys:
                raise AccessError(self.env._('Task AI conversations are created by their task only.'))
            membership = bool(MEMBERSHIP_FIELDS & keys)
            state = bool(TASK_STATE_FIELDS & keys) and not self.env.su
            # members (not superuser): only what Odoo lets them change in a group chat themselves;
            # not the archiving, the uploads, the avatar...: the task manages its chats
            other = not self.env.su and bool(keys - MEMBER_WRITABLE_FIELDS - MEMBERSHIP_FIELDS - TASK_STATE_FIELDS)
            chats = self.sudo().filtered('woow_project_ai_task_id')
            if chats and not self.env.su and keys & MEMBER_WRITABLE_FIELDS \
                    and chats.filtered(lambda chat: not chat.woow_project_ai_task_id.active):
                raise UserError(self.env._('This task is archived: its AI conversations can only be read.'))
            if (membership or state or other) and chats:
                if membership:
                    raise AccessError(self.env._('The members of a task AI conversation follow the task access: '
                                                 'people join from the task\'s AI tab.'))
                if state:
                    raise AccessError(self.env._('Only the AI task chat itself changes its pause.'))
                raise AccessError(self.env._('Only the name and the description of a task AI conversation can be '
                                             'changed: its task manages the rest.'))
        return super().write(vals)

    def _add_members(self, *, guests=None, partners=None, users=None, **kwargs):
        # add_members, channel_join, /discuss/channel/join, invitation links all come here
        if not is_sync(self.env) and any(channel.sudo().woow_project_ai_task_id for channel in self):
            raise AccessError(self.env._('Nobody can be added to a task AI conversation: people with access to the '
                                         'task join from the task\'s AI tab.'))
        return super()._add_members(guests=guests, partners=partners, users=users, **kwargs)

    def _channel_basic_info(self):
        # the clients hide calls and invitations in task AI chats (static/src/common)
        data = super()._channel_basic_info()
        tab = self.sudo().woow_project_ai_tab
        if tab:
            data['woow_project_ai_tab'] = tab
        return data

    # --- Post check ---

    @api.model
    def _get_thread_with_access(self, thread_id, mode='read', **kwargs):
        """The mail routes that post or upload (/mail/message/post,
        /mail/attachment/upload...) look the conversation up here first: a
        member who lost the task access (still listed until the sync, but
        unreadable by the rule) is refused with an AccessError and removed at
        once, as at post time, instead of a plain "not found"."""
        thread = super()._get_thread_with_access(thread_id, mode=mode, **kwargs)
        if thread or not isinstance(thread_id, int) or not self.env.user or self.env.user._is_public():
            return thread
        channel = self.sudo().with_context(active_test=False).browse(thread_id).exists()
        if channel.woow_project_ai_task_id:
            member = channel.channel_member_ids.filtered(lambda m: m.partner_id == self.env.user.partner_id)
            if member and not channel._woow_project_ai_partner_allowed(self.env.user.partner_id):
                channel._woow_project_ai_remove_now(member)
                raise AccessError(self.env._('You no longer have access to this task, so you were removed from its '
                                             'AI conversation.'))
        return thread

    @api.returns('mail.message', lambda value: value.id)
    def message_post(self, *, message_type='notification', **kwargs):
        if self._woow_project_ai_is_task_chat():
            if message_type == COMMENT and not kwargs.get('subtype_id') and not kwargs.get('subtype_xmlid'):
                # a message of a task chat is for its members: never a log note (Odoo's default subtype,
                # which portal users never read)
                kwargs['subtype_xmlid'] = 'mail.mt_comment'
            if kwargs.get('rating_id') or kwargs.get('rating_value'):
                # Odoo never rates a task chat: its public rating route posts the rating's notice as superuser,
                # under the rating's partner, in whatever record the rating points to
                raise AccessError(self.env._('A task AI conversation takes no ratings.'))
            self._woow_project_ai_check_post(message_type, kwargs)
            if kwargs.get('partner_ids'):
                # a mention notifies (and lets read the message) only members of this chat who may still be in it
                # (as Odoo's own client and post route keep mentions of a group chat to its members)
                kwargs['partner_ids'] = self._woow_project_ai_mentionable_partner_ids(kwargs['partner_ids'])
            # the files of the message are checked as stored (mail_message.py): the chat's own files, the
            # author's own uploads (the AI's replies: the module's files)
            self = self.with_context(**{POST_KEY: PostMarker(self.id)})  # noqa: PLW0642
        return super().message_post(message_type=message_type, **kwargs)

    def set_message_pin(self, message_id, pinned):
        # Odoo posts a "pinned a message" notice as the member (not superuser): the one notice a person may post
        if self._woow_project_ai_is_task_chat():
            if not self.sudo().woow_project_ai_task_id.active:
                raise UserError(self.env._('This task is archived: its AI conversations can only be read.'))
            self = self.with_context(**{PIN_KEY: PIN})  # noqa: PLW0642
        return super().set_message_pin(message_id, pinned)

    def _woow_project_ai_mentionable_partner_ids(self, partner_ids):
        """Those of `partner_ids` a mention in this chat may reach: its members
        who may still be in it, and the AI bots. Nobody else, so that a mention
        never makes anyone a recipient (who could read the message) or a member."""
        self.ensure_one()
        bots = self._woow_project_ai_bot_partner_ids()
        members = set(self.sudo().channel_member_ids.partner_id.ids)
        candidates = [partner_id for partner_id in self._woow_project_ai_allowed_partner_ids(partner_ids)
                      if partner_id in bots or partner_id in members]
        return candidates

    def _woow_project_ai_allowed_partner_ids(self, partner_ids):
        """Those of `partner_ids` who may be in this chat now (AI bots too)."""
        self.ensure_one()
        bots = self._woow_project_ai_bot_partner_ids()
        partners = self.env['res.partner'].sudo().browse(partner_ids).exists()
        return [partner.id for partner in partners
                if partner.id in bots or self._woow_project_ai_partner_allowed(partner)]

    def _message_update_content(self, message, body, attachment_ids=None, partner_ids=None, strict=True,
                                **kwargs):
        if self._woow_project_ai_is_task_chat():
            if not self.env.su:
                self._woow_project_ai_check_post(COMMENT, {})
                # only its author edits (or empties) a message of a task chat: Odoo also lets administrators
                # edit any message, which would take a customer's or the AI's words and files out of the chat
                if message.sudo().author_id != self.env.user.partner_id:
                    raise AccessError(self.env._('Only its author can edit or delete a message of a task AI '
                                                 'conversation.'))
                # the files it keeps or gets: checked as stored (mail_message.py)
            if partner_ids:
                # an edit that mentions someone: same as a post (Odoo makes them recipients)
                partner_ids = self._woow_project_ai_mentionable_partner_ids(partner_ids)
        return super()._message_update_content(message, body, attachment_ids=attachment_ids,
                                               partner_ids=partner_ids, strict=strict, **kwargs)

    def _woow_project_ai_check_post(self, message_type, values, sync=True):
        """A post (or an edit, or a ``mail.message`` created directly) in a
        task chat; `values`: its ``author_id`` / ``email_from``, if given.

        * Not superuser (a JSON-RPC ``message_post`` or ``mail.message``
          create; Odoo's own routes post as superuser): never under another
          name than the user's own.
        * Notices only from Odoo itself: a member leaving (superuser, in their
          name) and a pin (``set_message_pin``); the AI's notices are posted
          as superuser in the assistant's name.
        * Comments only otherwise: no incoming email (its sender can be
          forged), no other message type.
        * AI assistants post as superuser (``_post_prepared``).
        * A person: refused unless they are a member who can read the task
          now (one who lost the access is removed at once, in a transaction
          of its own: the refusal rolls this one back), and always in an
          archived task. Their post also removes the other members who lost
          the access meanwhile, before it reaches them (`sync`)."""
        self.ensure_one()
        channel = self.sudo()
        user = self.env.user
        author_id = values.get('author_id')
        if not self.env.su and (author_id not in (None, user.partner_id.id)
                                or (author_id is None and values.get('email_from'))):
            raise AccessError(self.env._('You can only write in a task AI conversation under your own name.'))
        if message_type in NOTIFICATION_TYPES:
            if self.env.context.get(PIN_KEY) is PIN and not channel.woow_project_ai_task_id.active:
                raise UserError(self.env._('This task is archived: its AI conversations can only be read.'))
            if (self.env.su and author_id) or self.env.context.get(PIN_KEY) is PIN:
                return
            raise AccessError(self.env._('Only Odoo itself posts notices in a task AI conversation.'))
        if message_type != COMMENT:
            raise AccessError(self.env._('A task AI conversation only takes messages written in it.'))
        if author_id:
            author = self.env['res.partner'].sudo().browse(author_id).exists()
        elif user and not user._is_public():
            author = user.partner_id
        else:
            raise AccessError(self.env._('Guests cannot write in task AI conversations.'))
        if self.env.su and author and author.id in self._woow_project_ai_bot_partner_ids():
            if sync:
                # an AI reply or a push: the members who lost the access meanwhile go first, as for a person's
                # post (their open pages: the poll leaves the chat out, bus_bus.py)
                channel._woow_project_ai_sync(notifications=False)
            return
        if channel.woow_project_ai_tab == TAB_CUSTOMER and self._woow_project_ai_hidden_from_customers(values):
            raise AccessError(self.env._('The customers read everything in a customer AI chat: internal notes '
                                         'cannot be posted there.'))
        if not channel.woow_project_ai_task_id.active:
            raise UserError(self.env._('This task is archived: its AI conversations can only be read.'))
        member = channel.channel_member_ids.filtered(lambda m: author and m.partner_id == author)
        if not channel._woow_project_ai_partner_allowed(author):
            if member:
                channel._woow_project_ai_remove_now(member)
            raise AccessError(self.env._('You no longer have access to this task, so you were removed from its AI '
                                         'conversation.'))
        if not member:
            raise AccessError(self.env._('Open the AI tab of the task to join this conversation first.'))
        if sync:
            # the others who lost the access meanwhile (no trigger saw it): removed in this transaction; the
            # message reaches neither their notifications nor their open pages (bus_bus.py)
            channel._woow_project_ai_sync(notifications=False)

    def _woow_project_ai_hidden_from_customers(self, values):
        """Whether a comment with these values (``is_internal``,
        ``subtype_id`` / ``subtype_xmlid``, as given to message_post or read
        from a created message) is one portal users never read: Odoo shows
        them only non-internal messages with a public subtype
        (mail.message._get_search_domain_share). False when the values say
        nothing about it (an edit)."""
        if not {'is_internal', 'subtype_id', 'subtype_xmlid'} & set(values):
            return False
        if values.get('is_internal'):
            return True
        Subtype = self.env['mail.message.subtype'].sudo()
        if values.get('subtype_id'):
            subtype = Subtype.browse(values['subtype_id']).exists()
        elif values.get('subtype_xmlid'):
            xmlid = values['subtype_xmlid']
            subtype = self.env.ref(xmlid if '.' in xmlid else 'mail.%s' % xmlid, raise_if_not_found=False)
            subtype = subtype.sudo() if subtype and subtype._name == 'mail.message.subtype' else Subtype
        else:
            subtype = Subtype  # a message without subtype
        return not subtype or subtype.internal

    def _notify_get_recipients(self, message, msg_vals=False, **kwargs):
        """Task chats: the notifications of a message (web push of the
        members, inbox / email of the mentioned partners) only reach members
        who can read the task now: not a member who lost the access before the
        sync, nor anyone outside the chat."""
        recipients = super()._notify_get_recipients(message, msg_vals, **kwargs)
        if not recipients or not self._woow_project_ai_is_task_chat():
            return recipients
        allowed = set(self._woow_project_ai_mentionable_partner_ids(list({recipient['id'] for recipient in recipients})))
        return [recipient for recipient in recipients if recipient['id'] in allowed]

    def _woow_project_ai_remove_now(self, members):
        """Remove `members` in a transaction of their own, committed at once
        (never waits long for a lock; on failure the next sync removes them)."""
        member_ids = members.ids
        try:
            with self.env.registry.cursor() as cr:
                cr.execute(SQL("SET LOCAL lock_timeout = %s", REMOVE_LOCK_TIMEOUT))
                env = api.Environment(cr, SUPERUSER_ID, {})
                found = env['discuss.channel.member'].browse(member_ids).exists()
                if found:
                    env['discuss.channel']._woow_project_ai_remove_members(found)
        except Exception:  # noqa: BLE001 - the refusal must happen anyway
            _logger.warning("Project AI task chat %s: could not remove members %s at once (the sync will)", self.id,
                            member_ids, exc_info=True)

    # --- Membership ---

    @api.model
    def _woow_project_ai_remove_members(self, members, reason='no access to the task any more'):
        """Remove members of task chats (sudo): like leaving (the client drops
        the conversation, the others see the member list change) but without
        a "left the channel" message."""
        removed = {}  # channel: partners removed
        for member in with_sync(members.sudo()):
            channel = member.channel_id
            target = member.partner_id or member.guest_id
            _logger.info("Project AI task chat %s (task %s): member %s removed (%s)", channel.id,
                         channel.woow_project_ai_task_id.id, target.display_name if target else member.id, reason)
            if member.partner_id:
                removed[channel] = removed.get(channel, self.env['res.partner'].sudo()) | member.partner_id
            member.unlink()
            if target:
                target._bus_send_store(Store(channel, {'is_pinned': False, 'isLocallyPinned': False}),
                                       notification_type='discuss.channel/leave')
            channel._bus_send_store(channel, {
                'channelMembers': Store.many(member, 'DELETE', only_id=True),
                'memberCount': channel.member_count,
            })
        for channel, partners in removed.items():
            channel._woow_project_ai_clear_lost_notifications(partners)
        if members:
            self._woow_project_ai_forget_readable()  # the readable task chats change with the members

    def _woow_project_ai_clear_lost_notifications(self, partners=None):
        """The unread notifications of these task chats' messages (mentions)
        for partners who may not read the chat now, members or not: marked
        read, and their Inbox counter updated. The Inbox lists only messages
        its reader may read (mail_message.py) while its counter counts every
        unread notification. `partners`: only those (removed members: no
        access check again); None: every notified partner (one query), each
        checked."""
        chats = self.sudo().with_context(active_test=False).filtered('woow_project_ai_task_id')
        if not chats or (partners is not None and not partners):
            return
        self.env['mail.notification'].flush_model(['is_read', 'res_partner_id', 'mail_message_id'])
        self.env['mail.message'].flush_model(['model', 'res_id'])
        query = """
            SELECT m.res_id, n.res_partner_id, array_agg(n.id)
              FROM mail_notification n
              JOIN mail_message m ON m.id = n.mail_message_id
             WHERE m.model = 'discuss.channel' AND m.res_id = ANY(%s)
               AND n.is_read IS NOT TRUE AND n.res_partner_id IS NOT NULL"""
        params = [chats.ids]
        if partners is not None:
            query += " AND n.res_partner_id = ANY(%s)"
            params.append(partners.ids)
        self.env.cr.execute(query + " GROUP BY m.res_id, n.res_partner_id", params)
        rows = self.env.cr.fetchall()
        if not rows:
            return
        bots = self._woow_project_ai_bot_partner_ids()
        Partner = self.env['res.partner'].sudo()
        lost = {}  # partner id: notification ids
        for chat_id, partner_id, notification_ids in rows:
            if partner_id in bots:
                continue
            if partners is None and chats.browse(chat_id)._woow_project_ai_partner_allowed(Partner.browse(partner_id)):
                continue
            lost.setdefault(partner_id, []).extend(notification_ids)
        for partner_id, notification_ids in lost.items():
            notifications = self.env['mail.notification'].sudo().browse(notification_ids)
            notifications.write({'is_read': True})
            partner = Partner.browse(partner_id)
            # as Odoo's mark_all_as_read tells the user's open clients
            partner._bus_send('mail.message/mark_as_read', {
                'message_ids': notifications.mail_message_id.ids,
                'needaction_inbox_counter': partner._get_needaction_count(),
            })
            _logger.info("Project AI task chats %s: %s unread notification(s) of partner %s marked read (no access "
                         "to the chat)", sorted({row[0] for row in rows if row[1] == partner_id}),
                         len(notification_ids), partner_id)

    def _woow_project_ai_join(self, user, created=False):
        """Add `user` (allowed: checked by the caller) as a member, once
        (``created``: the chat was just created with them as a member). An
        employee joins the customer chat muted (the member notification
        setting: muted until they turn it back on, nothing notified), so they
        are not pinged for every customer message."""
        self.ensure_one()
        channel = self.sudo()
        if not channel.woow_project_ai_task_id or not channel.woow_project_ai_tab:
            raise AccessError(self.env._('Only task AI conversations are joined from a task.'))
        member = channel.channel_member_ids.filtered(lambda m: m.partner_id == user.partner_id)
        if member and not created:
            return member
        if not member:
            # joins of one chat in turn: a concurrent join of the same person waits here, then fails with a
            # serialization failure that Odoo retries with a fresh snapshot (which sees the member), instead of a
            # unique-index error that it does not retry
            self.env.cr.execute("UPDATE discuss_channel SET write_date = write_date WHERE id = %s", [channel.id])
            member = with_sync(channel)._add_members(users=user, post_joined_message=False)
            self._woow_project_ai_forget_readable()  # a new member: the readable chats change
        if channel.woow_project_ai_tab == TAB_CUSTOMER and user._is_internal():
            # Odoo's "until I turn it back on" (discuss/settings/mute) is datetime.max; told to the open
            # clients only (Odoo's _notify_mute would also leave a 9999-12-31 unmute cron trigger per join)
            member.write({'mute_until_dt': datetime.max, 'custom_notifications': 'no_notif'})
            member._woow_project_ai_notify_settings()
        return member

    def _woow_project_ai_unmute(self, partner):
        """`partner` (an employee) took over the customer chat by writing in it:
        their membership is unmuted, so the customer's next messages reach
        them as a Live Chat operator's would (the AI is paused meanwhile)."""
        self.ensure_one()
        member = self.sudo().channel_member_ids.filtered(lambda m: m.partner_id == partner)
        if member and (member.mute_until_dt or member.custom_notifications == 'no_notif'):
            member.write({'mute_until_dt': False, 'custom_notifications': False})
            member._woow_project_ai_notify_settings()
        return member

    def _woow_project_ai_sync(self, notifications=True):
        """Bring the members of these task chats in line: the bot of the tab's
        current assistant (other AI bots removed), the people who can still
        read the task (the others removed). Nobody else is ever added here.
        `notifications`: also the unread notifications of the chats' messages
        of everyone who may not read them now, members or not (the triggers
        and the hourly sweep; a post only takes care of the members it
        removes)."""
        bot_ids = self._woow_project_ai_bot_partner_ids()
        Member = self.env['discuss.channel.member'].sudo()
        for channel in self.sudo().with_context(active_test=False).filtered('woow_project_ai_task_id'):
            keep_bot = channel._woow_project_ai_tab_assistant().partner_id
            to_remove = old_bots = Member
            for member in channel.channel_member_ids:
                partner = member.partner_id
                if not partner:
                    to_remove |= member  # guests are never members
                elif partner.id in bot_ids:
                    if partner != keep_bot:
                        old_bots |= member  # the tab's assistant changed
                elif not channel._woow_project_ai_partner_allowed(partner):
                    to_remove |= member
            if old_bots:
                self._woow_project_ai_remove_members(old_bots, reason="not the tab's AI assistant any more")
            if to_remove:
                self._woow_project_ai_remove_members(to_remove)
            if keep_bot and keep_bot not in channel.channel_member_ids.partner_id:
                with_sync(channel)._add_members(partners=keep_bot, post_joined_message=False)
        if notifications:
            self._woow_project_ai_clear_lost_notifications()

    @api.model
    def _woow_project_ai_chats_of(self, tasks):
        return self.sudo().with_context(active_test=False).search([('woow_project_ai_task_id', 'in', tasks.ids)]) \
            if tasks else self.sudo().browse()

    @api.model
    def _woow_project_ai_sync_tasks(self, tasks):
        self._woow_project_ai_forget_readable()
        self._woow_project_ai_chats_of(tasks)._woow_project_ai_sync()

    @api.model
    def _woow_project_ai_sync_projects(self, projects):
        self._woow_project_ai_forget_readable()
        if not projects:
            return
        self.sudo().with_context(active_test=False).search(
            [('woow_project_ai_task_id.project_id', 'in', projects.ids)])._woow_project_ai_sync()

    @api.model
    def _woow_project_ai_sync_partners(self, partners):
        """Task chats where these partners are members (their users' groups or active flag changed)."""
        self._woow_project_ai_forget_readable()
        partners = partners.exists() if partners else partners
        if not partners:
            return
        members = self.env['discuss.channel.member'].sudo().search([
            ('partner_id', 'in', partners.ids), ('channel_id.woow_project_ai_task_id', '!=', False)])
        members.channel_id._woow_project_ai_sync()

    @api.model
    def _woow_project_ai_sweep_cursor(self):
        """Where the sweep stopped (a channel id; 0: start of a round). Read and
        written with SQL, like the cron runner report (ai_job STATE_PARAM):
        ir.config_parameter's ORM write would clear the caches of every
        process at each batch."""
        self.env.cr.execute("SELECT value FROM ir_config_parameter WHERE key = %s", [SWEEP_CURSOR_PARAM])
        row = self.env.cr.fetchone()
        try:
            return max(0, int(row[0])) if row else 0
        except (TypeError, ValueError):
            return 0

    @api.model
    def _woow_project_ai_set_sweep_cursor(self, value):
        self.env.cr.execute("""
            INSERT INTO ir_config_parameter (key, value, create_uid, write_uid, create_date, write_date)
            VALUES (%s, %s, %s, %s, statement_timestamp() AT TIME ZONE 'UTC', statement_timestamp() AT TIME ZONE 'UTC')
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, write_date = EXCLUDED.write_date
        """, [SWEEP_CURSOR_PARAM, str(int(value or 0)), SUPERUSER_ID, SUPERUSER_ID])

    @api.model
    def _woow_project_ai_sweep_deadline(self, time_slice):
        """(deadline, pass_bound): when this call stops (time.monotonic()), and
        whether that is the cron pass's own time limit rather than the slice.
        In a cron pass (Odoo runs a callback that reports progress up to 10
        times in the same pass, under the same real-time limit) the call keeps
        SWEEP_LIMIT_MARGIN_SECONDS of what the pass has left (ai_job
        _cron_pass_left, as the dispatcher measures it)."""
        from . import ai_job  # noqa: PLC0415 - ai_job imports this module (through project_task)
        now = time.monotonic()
        if self.env.context.get('cron_id'):
            left = ai_job._cron_pass_left()
            if left is not None and left - SWEEP_LIMIT_MARGIN_SECONDS < time_slice:
                return now + max(0.0, left - SWEEP_LIMIT_MARGIN_SECONDS), True
        return now + time_slice, False

    @api.model
    def _woow_project_ai_cron_sync(self, batch_size=CRON_BATCH, time_slice=SWEEP_SLICE_SECONDS):
        """Hourly sweep: removes the members who lost the task access by a
        change no trigger saw (a record rule, a commercial partner...). In
        batches, each committed with the sweep's cursor (a failing batch is
        logged and skipped: the next sweep tries it again).

        One call runs at least one batch and at most ``time_slice`` seconds,
        then tells Odoo's cron how many chats are left
        (ir.cron._notify_progress): Odoo runs it again at once, in the same
        pass. A call never runs past what the cron pass has left of its time
        limit (_woow_project_ai_sweep_deadline): when the pass is out of time
        it reports nothing left, so that Odoo ends the pass, and triggers the
        cron again in SWEEP_NEXT_PASS_SECONDS, in a fresh pass. Every call
        goes on from the cursor, so a sweep that outgrows the cron's time
        limit still reaches every chat. Returns the number of chats synced by
        this call."""
        after = self._woow_project_ai_sweep_cursor()
        Channel = self.sudo().with_context(active_test=False)
        Cron = self.env['ir.cron']
        channels = Channel.search([('woow_project_ai_task_id', '!=', False), ('id', '>', after)], order='id')
        auto_commit = not getattr(threading.current_thread(), 'testing', False)
        deadline, pass_bound = self._woow_project_ai_sweep_deadline(time_slice)
        done = 0
        for start in range(0, len(channels), batch_size):
            if time.monotonic() >= deadline and (done or pass_bound):
                if pass_bound:
                    # out of the pass's time: end the pass, go on in a fresh one
                    Cron._notify_progress(done=done, remaining=0)
                    cron_id = self.env.context.get('cron_id')
                    if cron_id:
                        Cron.browse(cron_id)._trigger(
                            fields.Datetime.now() + timedelta(seconds=SWEEP_NEXT_PASS_SECONDS))
                else:
                    Cron._notify_progress(done=done, remaining=len(channels) - done)
                return done
            # a chat deleted since the search (its task was deleted) must not abort its whole batch
            ids = channels[start:start + batch_size].ids
            batch = Channel.browse(ids).exists()
            if batch:
                try:
                    with self.env.cr.savepoint():
                        batch._woow_project_ai_sync()
                except Exception:  # noqa: BLE001 - one bad chat must not stop the sweep
                    _logger.exception("Project AI task chats %s..%s: membership sweep failed", ids[0], ids[-1])
                    self.env.invalidate_all()
            done += len(ids)
            self._woow_project_ai_set_sweep_cursor(ids[-1] if len(channels) > done else 0)
            if auto_commit:
                self.env.cr.commit()  # pylint: disable=invalid-commit
        if after and not channels:
            self._woow_project_ai_set_sweep_cursor(0)  # a full round ended: the next sweep starts again
        Cron._notify_progress(done=done, remaining=0)
        return done

    # --- Pause ---

    def _woow_project_ai_pause(self, minutes=None):
        """Pause the AI of this (customer) chat: extends, never shortens."""
        self.ensure_one()
        channel = self.sudo()
        if minutes is None:
            minutes = channel.woow_project_ai_task_id._woow_project_ai_pause_minutes()
        until = fields.Datetime.now() + timedelta(minutes=minutes)
        if not channel.woow_project_ai_paused_until or channel.woow_project_ai_paused_until < until:
            channel.woow_project_ai_paused_until = until
        return until

    def _woow_project_ai_is_paused(self):
        """True while the AI must not answer this chat (a colleague wrote in the
        customer chat, or the backend handed the customer over to a person)."""
        self.ensure_one()
        channel = self.sudo()
        # reread: a running job must see an employee's post committed meanwhile
        channel.invalidate_recordset(['woow_project_ai_paused_until'])
        until = channel.woow_project_ai_paused_until
        return bool(until and until > fields.Datetime.now())

    # --- Notice when the AI cannot answer ---

    def _woow_project_ai_blocked_notice(self, message=None):
        """One short fixed notice per blocked period (reset by the next answer),
        posted as a notification: never in the history, never answered. In the
        customer chat, every blocked customer message also schedules the
        colleagues' To-Do (an open one is reused)."""
        self.ensure_one()
        channel = self.sudo()
        problems = channel._woow_project_ai_problems()
        if not problems or any(code in NOTICE_PROBLEMS_EXCLUDED for code in problems):
            return False
        if channel.woow_project_ai_tab == TAB_CUSTOMER:
            # every customer message the AI cannot answer tells the colleagues (muted there): the open To-Do is
            # reused, a new one comes once it is done
            try:
                with self.env.cr.savepoint():
                    channel.woow_project_ai_task_id._woow_project_ai_alert_activity(ALERT_BLOCKED)
            except Exception:  # noqa: BLE001 - the notice stays
                _logger.exception("Task %s: could not schedule the To-Do of an unanswered customer",
                                  channel.woow_project_ai_task_id.id)
        if channel.woow_project_ai_notice_sent:
            return False
        Assistant = self.env['woow.project.ai.assistant']
        lang = Assistant._installed_lang(message.sudo().author_id.lang if message else None)
        env = self.with_context(lang=lang).env
        if channel.woow_project_ai_tab == TAB_CUSTOMER:
            text = env._('The AI cannot answer right now. The colleague in charge will get back to you.')
        else:
            text = env._('The AI cannot answer right now: the internal AI assistant of this project is not '
                         'available. A project manager can see why on the "AI Assistants" page of the project.')
        author = channel._woow_project_ai_tab_assistant().partner_id or self.env.ref('base.partner_root')
        channel.woow_project_ai_notice_sent = True
        return channel.message_post(
            body=Markup('<div class="o_mail_notification">%s</div>') % text, author_id=author.id,
            message_type='notification', subtype_xmlid='mail.mt_comment')

    # --- Trigger ---

    def _notify_thread(self, message, msg_vals=False, **kwargs):
        rdata = super()._notify_thread(message, msg_vals=msg_vals, **kwargs)
        if self._woow_project_ai_is_task_chat():
            try:
                # Savepoint: a failure here must never undo the person's message.
                with self.env.cr.savepoint():
                    self.sudo()._woow_project_ai_handle_new_message(message.sudo())
            except PG_CONCURRENCY_EXCEPTIONS_TO_RETRY:
                # concurrent update (e.g. two people posting in the same chat at
                # once, raised when the savepoint flushes Odoo's own writes): the
                # transaction is aborted, Odoo retries the whole request
                raise
            except Exception:  # noqa: BLE001
                _logger.exception("Project AI: could not handle message %s in task chat %s", message.id, self.id)
        return rdata

    def _woow_project_ai_handle_new_message(self, message):
        """Queue an AI reply for a newly posted message of a task chat, or the
        tab's notice when its AI cannot answer.

        Returns the created ``woow.project.ai.job`` or False. Answered: every
        comment (text or files) of the tab's audience (employees in the
        internal chat, portal users in the customer chat) while the AI is not
        paused. An employee's comment in the customer chat pauses the AI
        (refreshed by each further one). Bots, notices and the others are
        never answered."""
        self.ensure_one()
        channel = self.sudo()
        comment = message.message_type == 'comment' and message.model == 'discuss.channel' \
            and bool(html_to_text(message.body) or message.attachment_ids)
        kind = channel._woow_project_ai_author_kind(message) if comment else None
        if kind == AUTHOR_STAFF and channel.woow_project_ai_tab == TAB_CUSTOMER:
            # a colleague writes to the customer: the AI steps back (refreshed by each further message), and
            # the colleague gets the customer's next messages (unmuted, as a Live Chat operator)
            channel._woow_project_ai_pause()
            channel._woow_project_ai_unmute(message.author_id)
        if kind != channel._woow_project_ai_audience() or channel._woow_project_ai_is_paused():
            return False
        assistant = channel._woow_project_ai_assistant()
        if not assistant:
            channel._woow_project_ai_blocked_notice(message)
            return False
        if channel.woow_project_ai_notice_sent:
            channel.woow_project_ai_notice_sent = False  # answering again: a later blocked period gets its notice
        return self.env['woow.project.ai.job']._create_for_message(channel, assistant, message)

    # --- Answering ---

    def _woow_project_ai_assistant(self):
        """The AI assistant answering this task chat now, or an empty
        recordset: the project's assistant for the tab, while it is active,
        its connection ready and (customer tab) still safe for customers, and
        the task active."""
        Assistant = self.env['woow.project.ai.assistant'].sudo()
        if not self._woow_project_ai_is_task_chat():
            return Assistant
        assistant = self._woow_project_ai_tab_assistant()
        if not assistant or self._woow_project_ai_problems(assistant):
            return Assistant
        return assistant

    def _woow_project_ai_requester(self, assistant, message=None):
        """Partner the AI is talking to (identity sent to the backend): the
        author of ``message`` (the message being answered) when it is a person
        other than the bot; without it (or a message of the bot), the first
        member who is not a bot."""
        self.ensure_one()
        bots = self._woow_project_ai_bot_partner_ids()
        author = message.sudo().author_id if message else self.env['res.partner']
        if author and author.id not in bots:
            return author
        members = self.sudo().channel_member_ids.filtered(lambda m: m.partner_id and m.partner_id.id not in bots)
        return members.sorted('id')[:1].partner_id

    def _woow_project_ai_identity(self, assistant, message=None):
        """Identity of the person the AI talks to (docs/PROTOCOL.md "identity"):
        the n8n ``user`` object, the openai ``metadata`` and system identity
        block, and the language of the default messages."""
        self.ensure_one()
        return assistant._identity(self._woow_project_ai_requester(assistant, message))

    def _woow_project_ai_link_hosts(self, assistant):
        """Hosts whose links in AI replies stay clickable: None (any http(s) link)."""
        return None

    def _woow_project_ai_channel_info(self, assistant):
        """``channel`` object of the n8n payload."""
        self.ensure_one()
        return {'id': self.id, 'type': self.channel_type}

    def _woow_project_ai_surface_info(self):
        """Where the AI assistant is used (docs/PROTOCOL.md "Surface"): the n8n
        ``surface`` object and the openai ``metadata`` keys ``surface_<key>``."""
        self.ensure_one()
        channel = self.sudo()
        project = channel.woow_project_ai_task_id.project_id
        # the project only when the asker of the message being answered (and, internal chat, every colleague of
        # the chat) can read it, as for the internal project text
        shared = bool(project) and channel._woow_project_ai_project_shared(
            channel._woow_project_ai_requester_user(None))
        return {'type': 'project_task', 'project_id': project.id if shared else None,
                'task_id': channel.woow_project_ai_task_id.id or None, 'tab': channel.woow_project_ai_tab or None}

    def _woow_project_ai_usage_scope(self):
        """(model name, record id) the replies of this channel are counted for
        in the monthly usage: the project of the task."""
        self.ensure_one()
        project = self.sudo().woow_project_ai_task_id.project_id
        return ('project.project', project.id) if project else (False, False)

    def _woow_project_ai_review_answer(self, assistant, result):
        """Look at the backend's answer before it is posted; True hands the
        customer to a person (``woow.project.ai.job._process_backend_handoff``):
        an n8n ``"handoff": true`` in the customer chat. The internal chat
        posts the answer and ignores the flag."""
        self.ensure_one()
        if self.sudo().woow_project_ai_tab != TAB_CUSTOMER:
            return False
        return bool(result.get('handoff'))

    # --- History ---

    def _woow_project_ai_context_messages(self, assistant, until=None):
        """The most recent ``max_history`` comment messages, oldest first.

        With ``until`` (a mail.message: a job answers the conversation up to
        its own message), only the messages up to it, plus this AI
        assistant's own later messages (its answers to other people's
        questions, moved before the last question by normalize_turns), so the
        model sees what was already answered."""
        self.ensure_one()
        limit = max(1, min(assistant.sudo().max_history or 50, 200))
        domain = [
            ('model', '=', 'discuss.channel'),
            ('res_id', '=', self.id),
            ('message_type', 'in', ('comment', 'email')),
        ]
        if self.sudo().woow_project_ai_tab == TAB_CUSTOMER:
            # only what the customers read themselves (no log note, whatever way it came in)
            domain += self.env['mail.message']._get_search_domain_share()
        Message = self.env['mail.message'].sudo()
        if not until:
            return Message.search(domain, order='id desc', limit=limit).sorted('id')
        bot = assistant.sudo().partner_id
        # the later answers take at most half of the history: the question itself is always in it
        later = Message.search(domain + [('id', '>', until.id), ('author_id', '=', bot.id)], order='id desc',
                               limit=limit // 2) if bot and limit >= 2 else Message
        earlier = Message.search(domain + [('id', '<=', until.id)], order='id desc', limit=limit - len(later))
        return (earlier | later).sorted('id')

    @staticmethod
    def _woow_project_ai_author_name(message):
        """Name of a message's author for the history (one line, no brackets).
        Never an email address: for a message without author, only the
        display name part of its sender."""
        message = message.sudo()
        name = message.author_id.name or message.author_guest_id.name
        if not name:
            name = parseaddr(message.email_from or '')[0]
            if '@' in name:  # a display name that is an address
                name = ''
        return one_line_name(name)

    def _woow_project_ai_author_role(self, assistant, message):
        """History role of a message: ('user'|'assistant', text prefix) or None to drop it."""
        kind = self._woow_project_ai_author_kind(message)
        if kind == AUTHOR_BOT:
            root = self.env.ref('base.partner_root', raise_if_not_found=False)
            if root and message.sudo().author_id == root:
                return None  # not the AI's words
            return 'assistant', ''  # this assistant, or the one the tab had before
        if self.sudo().woow_project_ai_tab == TAB_CUSTOMER:
            # the kind of account, set by the system, before the name (a name can say anything)
            prefix = {AUTHOR_STAFF: STAFF_PREFIX, AUTHOR_CUSTOMER: CUSTOMER_PREFIX}.get(kind, OTHER_PREFIX)
            return 'user', prefix % self._woow_project_ai_author_name(message)
        return 'user', ''

    def _woow_project_ai_history(self, assistant, messages):
        """Messages -> [{'role', 'text', 'attachments', 'message_id'}] (no empty
        entries). Several people share a tab: every turn written by a person
        (role ``user``) starts with ``[<author name>] `` (customer chat: the
        kind of account first, see _woow_project_ai_author_role). Customer
        chat entries of a colleague carry ``woow_project_ai_staff``."""
        self.ensure_one()
        channel = self.with_context(**{BOTS_KEY: self._woow_project_ai_bot_partner_ids()})
        customer_tab = self.sudo().woow_project_ai_tab == TAB_CUSTOMER
        history = []
        # one prefetch set for every attachment of the context: the job commits
        # (and clears the cache) between building the history and the request,
        # which then reads all of them in one query instead of one per message
        # (concrete ids: the x2many's own lazy prefetch set is empty once the cache is cleared)
        prefetch = tuple(messages.sudo().attachment_ids.ids)
        for message in messages:
            role = channel._woow_project_ai_author_role(assistant, message)
            if not role:
                continue
            role, prefix = role
            if role == 'user' and not prefix:
                name = self._woow_project_ai_author_name(message)
                prefix = '[%s] ' % name if name else ''
            text = html_to_text(message.body)
            if len(text) > MAX_MESSAGE_TEXT:
                text = text[:MAX_MESSAGE_TEXT] + TRUNCATED_MARKER
            # in upload order, as the chat shows them (ir.attachment's default order is id desc)
            attachments = message.sudo().attachment_ids.sorted('id').with_prefetch(prefetch)
            if not text and not attachments:
                continue
            entry = {
                'role': role,
                'text': (prefix + text) if text else prefix.strip(),
                'attachments': attachments,
                'message_id': message.id,
            }
            if customer_tab:
                entry['woow_project_ai_staff'] = channel._woow_project_ai_author_kind(message) == AUTHOR_STAFF
            history.append(entry)
        return history

    def _woow_project_ai_system_prompt(self, assistant, history):
        """System prompt sent to the backend: the assistant's prompt, the
        project text of this tab (the customer chat never gets the internal
        one), a task summary read with the asker's own rights, and (customer
        chat) the notes about the identity labels."""
        self.ensure_one()
        channel = self.sudo()
        task = channel.woow_project_ai_task_id
        tab = channel.woow_project_ai_tab
        parts = [(assistant.sudo().system_prompt or '').strip()]
        requester = channel._woow_project_ai_requester_user(history)
        brief = (task.project_id['woow_project_ai_%s_brief' % tab] or '').strip() if task.project_id else ''
        # the internal text is a project's: only while the asker and every colleague of the chat can read the
        # project (a task follower may read the task of a project hidden to them); the customer text is
        # written for the customers
        if brief and (tab == TAB_CUSTOMER or channel._woow_project_ai_project_shared(requester)):
            parts.append('%s\n%s' % (BRIEF_HEADER, brief))
            if tab == TAB_INTERNAL and task.project_id not in channel.woow_project_ai_brief_project_ids:
                # the answers will be built from this project's text: the chat stays with its readers
                channel.woow_project_ai_brief_project_ids = [Command.link(task.project_id.id)]
                self._woow_project_ai_forget_readable()
        if requester:
            audience = None
            if tab == TAB_INTERNAL:
                # every member reads the answer: related records only if all of them can read them
                audience = channel._woow_project_ai_member_users().filtered(
                    lambda member: member._is_internal()) - requester
            parts.append(task._woow_project_ai_context_block(requester, tab, audience))
        if tab == TAB_CUSTOMER:
            parts.append(LABEL_SYSTEM_NOTE)
            if any(entry.get('woow_project_ai_staff') for entry in history or []):
                parts.append(STAFF_SYSTEM_NOTE)
        return '\n\n'.join(part for part in parts if part)

    def _woow_project_ai_member_users(self):
        """The active users of the people (not the AI bots) of this chat."""
        self.ensure_one()
        channel = self.sudo()
        bots = set(channel._woow_project_ai_bot_partner_ids())
        people = channel.channel_member_ids.partner_id.filtered(lambda partner: partner.id not in bots)
        return people.with_context(active_test=False).user_ids.filtered('active')

    def _woow_project_ai_project_shared(self, requester):
        """Whether `requester` and every person of the chat can read the
        task's project now: then the project's id goes to the AI (and, in the
        internal chat, the internal project text)."""
        self.ensure_one()
        channel = self.sudo()
        task = channel.woow_project_ai_task_id
        project = task.project_id
        if not project or not requester:
            return False
        # every person of the chat reads the answer (a portal user may read a task without its project too)
        users = requester | channel._woow_project_ai_member_users()
        for user in users:
            readable = project.with_user(user).with_context(
                active_test=False, allowed_company_ids=task._woow_project_ai_company_ids(user))
            try:
                if not readable.has_access('read'):
                    return False
            except AccessError:
                return False
        return True

    def _woow_project_ai_requester_user(self, history):
        """The user who asked, of the tab's audience (customer chat: a portal
        user, never a colleague), who may be in this chat now: the author of
        the message being answered (set by the job's request:
        ``woow.project.ai.assistant._build_request``), else of the last turn
        of the audience. The task summary is read with their rights. Empty: no
        task summary."""
        self.ensure_one()
        channel = self.sudo()
        audience = channel._woow_project_ai_audience()
        Message = self.env['mail.message'].sudo()
        trigger_id = self.env.context.get(TRIGGER_KEY)
        message = Message.browse(trigger_id if isinstance(trigger_id, int) else []).exists()
        if not message or message.model != 'discuss.channel' or message.res_id != channel.id \
                or channel._woow_project_ai_author_kind(message) != audience:
            ids = sorted((entry['message_id'] for entry in history or [] if entry.get('role') == 'user'
                          and entry.get('message_id')), reverse=True)
            message = next((candidate for candidate in Message.browse(ids)
                            if channel._woow_project_ai_author_kind(candidate) == audience), Message)
        author = message.author_id
        if not author:
            return self.env['res.users']
        users = author.with_context(active_test=False).user_ids
        return next((user for user in users
                     if (user._is_internal() if audience == AUTHOR_STAFF else user._is_portal())
                     and channel.woow_project_ai_task_id._woow_project_ai_user_allowed(
                         user, channel.woow_project_ai_tab)),
                    self.env['res.users'])
