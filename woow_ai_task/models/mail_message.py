# -*- coding: utf-8 -*-
"""Messages of the task AI conversations.

Reading. Odoo reads a message with sudo (``mail.message.fetch``) wherever it
does not call ``read``: copies, exports through any model
(``mail.notification``, starred messages, channel members), delegated fields
(``mail.mail``), template rendering of ``object.body``; and other models read
it as superuser through their related and stored computed fields (mail_group's
message fields, the full composer's parent-derived fields, im_livechat's
``parent_body``), in an onchange too. All of them read the database in
``_fetch_query``: there, whenever the environment acts for a real user
(``env.uid``: not the superuser account, not a request without a user), also
as superuser, the content of a task-chat message is read only for someone who
can read that chat under the module's rule (``woow_project_ai_readable``: its
task now, and for the internal chat the projects of its texts). The fields
Odoo's record rules and access checks read as superuser (RULE_FIELDS: where a
message belongs, its type, who and when) stay readable, and a rule field read
first never leaves the prefetched content of an unreadable message in the
cache. The rows are classified from the cache with the registry cache of
task-chat channel ids (no query; only channels that exist are remembered), and
the user's readable task chats are computed once per transaction.
``_search`` leaves those messages out of the user's searches too: Odoo lets
their authors and notified partners find them (inbox, starred, history,
read_group).

Links, checked on the rows as stored (any id type, any command shape):

* no message gets a parent in a task chat unless it is in that same chat
  (Odoo serializes a reply's parent with sudo);
* a file of a task chat is never on a message elsewhere, and a message of a
  task chat carries only files of that chat uploaded by its author (the
  AI's replies: files the module creates as superuser);
* a message is created in a task chat only by the chat's own
  ``message_post``, which checked the post, superuser included (Odoo's own
  notices, the AI's answers and the welcome text all go through it; the
  mass-mail composer's superuser ``mail.mail`` would otherwise put a message
  under anyone's name there); outside superuser, none is moved into one, and
  task chat messages are never written or deleted directly (Odoo's own
  message actions go through other paths: ``_message_update_content``,
  checked by the channel; reactions, checked here; pins in SQL).

The task chats in these checks come from SQL joins on the stored rows (not
the registry cache)."""

import logging

from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.models import NewId
from odoo.osv import expression
from odoo.tools import SQL

from ..tools.ids import as_id, defaults_set, real_ids, require_int_ids, stores_other_than
from .discuss_channel import for_a_user, posted_by_this_module

_logger = logging.getLogger(__name__)

# what Odoo's record rules and access checks read of a message as superuser (rule domains evaluated with
# filtered_domain, routing): read for anyone. The rest of a task-chat message (body, subject, files, recipients,
# author, name...) is read, also as superuser, only for someone who may read the chat (env.uid)
RULE_FIELDS = frozenset({'id', 'model', 'res_id', 'message_type', 'subtype_id', 'is_internal', 'parent_id',
                         'create_uid', 'create_date', 'write_uid', 'write_date', 'date'})
# set by _fetch_field around a rule field's lazy read as superuser (an object no client can send): the other
# fields Odoo prefetches with it are dropped for unreadable task-chat messages instead of failing the check
RULE_READ = object()
RULE_READ_KEY = 'woow_project_ai_rule_read'
# set by unlink around Odoo's own unlink of messages (an object no client can send)
DELETING = object()
DELETING_KEY = 'woow_project_ai_deleting'
# what a message's stored links depend on
LINK_FIELDS = {'parent_id', 'attachment_ids', 'model', 'res_id', 'author_id'}
# a domain that cannot match a channel message
NO_CHANNEL = object()


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _domain_scope(domain):
    """What a mail.message search domain says about the channels it can match:
    NO_CHANNEL (only other models), a set of discuss.channel ids, or None (any
    message). Only plain conjunctions of leaves are read; anything else is None."""
    if not isinstance(domain, (list, tuple)):
        return None
    models_, res_ids = None, None
    for item in domain:
        if isinstance(item, str):
            if item != '&':
                return None
            continue
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            return None
        name, operator, value = item
        if name == 'model':
            if operator == '=' and isinstance(value, str):
                values = {value}
            elif operator == 'in' and isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
                values = set(value)
            else:
                continue
            models_ = values if models_ is None else models_ & values
        elif name == 'res_id':
            if operator == '=' and _is_int(value):
                values = {value}
            elif operator == 'in' and isinstance(value, (list, tuple)) and all(_is_int(v) for v in value):
                values = set(value)
            else:
                continue
            res_ids = values if res_ids is None else res_ids & values
    if models_ is not None and 'discuss.channel' not in models_:
        return NO_CHANNEL
    if models_ == {'discuss.channel'} and res_ids is not None:
        return res_ids
    return None


class MailMessage(models.Model):
    _inherit = 'mail.message'

    # the body in a prefetch group of its own: a read of the other fields with sudo (a related field, a name)
    # never puts it in the cache, where a read in the user's environment would find it without fetch
    body = fields.Html(prefetch='woow_project_ai_body')

    # --- Reading ---

    def _fetch_field(self, field):
        if (field.name in RULE_FIELDS and self.env.su and for_a_user(self.env)
                and self.env.context.get(RULE_READ_KEY) is not RULE_READ):
            # a rule field read as superuser for a user (a record rule check, Odoo's own routing): what Odoo
            # prefetches with it of an unreadable task-chat message is not kept, and the read works
            return super(MailMessage, self.with_context(**{RULE_READ_KEY: RULE_READ}))._fetch_field(field)
        return super()._fetch_field(field)

    def _fetch_query(self, query, fields):
        # every read of message fields from the database comes here as superuser (mail's fetch reads with sudo):
        # Odoo's reads for a user and the related or stored computed fields of other models (mail_group's
        # body, the composer's parent-derived fields, an onchange of either). For a real user (env.uid; not
        # superuser, not a request without a user) the content of a task-chat message they may not read is
        # refused; model / res_id come with it (same query) to classify the rows from the cache
        if not self.env.su or not for_a_user(self.env) or self.env.context.get(DELETING_KEY) is DELETING:
            return super()._fetch_query(query, fields)
        content = [field for field in fields if field.name not in RULE_FIELDS]
        if not content:
            return super()._fetch_query(query, fields)
        places = [self._fields[name] for name in ('model', 'res_id')]
        if any(field.column_type for field in fields):
            # model / res_id in the same SELECT as the asked columns (no query of their own)
            fields = list(fields) + [field for field in places if field not in fields]
        fetched = super()._fetch_query(query, fields)
        # x2many lines only (Odoo reads them one field at a time): model / res_id are usually in the cache already;
        # fetched for the rows that miss them (a rule field: no check, no recursion)
        missing = fetched.filtered(lambda message: not all(
            self.env.cache.contains(message, field) for field in places))
        if missing:
            missing.fetch(['model', 'res_id'])
        hidden = fetched._woow_project_ai_hidden_in_cache()
        if hidden:
            hidden.invalidate_recordset([field.name for field in content])
            if self.env.context.get(RULE_READ_KEY) is not RULE_READ:
                _logger.info("Project AI: content of task chat message(s) %s refused to user %s", hidden.ids,
                             self.env.uid)
                raise AccessError(self.env._('This message is in a task AI conversation you cannot read.'))
        return fetched

    def _check_access(self, operation):
        """Odoo's read access check of messages applies the module's rule too:
        Odoo lets the author and notified partners read a task-chat message,
        and its own rule check (``filtered_domain`` as superuser, whenever a
        record rule on messages exists) puts the message's columns in the
        cache, where a read then finds them without ``fetch``."""
        result = super()._check_access(operation)
        if operation != 'read' or self.env.su or not self._ids:
            return result
        messages = self - result[0] if result else self
        forbidden = messages._woow_project_ai_unreadable()
        if not forbidden:
            return result
        if result:
            return result[0] + forbidden, result[1]
        return forbidden, lambda: AccessError(self.env._('This message is in a task AI conversation you cannot read.'))

    def _woow_project_ai_unreadable(self):
        """Those of these messages that are in task chats the current user
        cannot read (model / res_id from the cache, fetched as superuser if
        needed; the registry cache of task chats; the readable list)."""
        real = self.browse([id_ for id_ in self._ids if id_ and not isinstance(id_, NewId)])
        if not real:
            return self.browse()
        real.sudo().fetch(['model', 'res_id'])  # usually in the cache already (no query)
        channel_of = real._woow_project_ai_chats_in_cache()
        if not channel_of:
            return self.browse()
        Channel = self.env['discuss.channel']
        chats = Channel._woow_project_ai_known_task_chats(set(channel_of.values()))
        unreadable = chats - Channel._woow_project_ai_readable_cached(chats) if chats else set()
        return self.browse([id_ for id_, chat in channel_of.items() if chat in unreadable])

    def _woow_project_ai_check_records(self):
        """Refuse these messages when some are task-chat messages the current
        user cannot read (for the paths that read them whatever the cache)."""
        if self.env.su or not self._ids:
            return
        self.sudo().fetch(['model', 'res_id'])  # usually in the cache already (no query)
        self._woow_project_ai_check_fetched()

    def _woow_project_ai_chats_in_cache(self):
        """{record id (as given): channel id} for the fetched real records of
        these that are channel messages, read from the cache."""
        cache = self.env.cache
        models_ = cache._get_field_cache(self, self._fields['model'])
        res_ids = cache._get_field_cache(self, self._fields['res_id'])
        chats = {}
        for id_ in self._ids:
            if id_ and models_.get(id_) == 'discuss.channel' and res_ids.get(id_):
                chats[id_] = res_ids[id_]
        return chats

    def _woow_project_ai_hidden_in_cache(self):
        """Those of these fetched messages that are in task chats the real user
        (env.uid, also as superuser) may not read under the module's rule,
        classified from the cache (model / res_id) and the registry cache of
        task chats: no query for other messages. (The post marker is not a
        reason to read: the message message_post returns keeps its context.)"""
        channel_of = self._woow_project_ai_chats_in_cache()
        if not channel_of:
            return self.browse()
        Channel = self.env['discuss.channel']
        chats = Channel._woow_project_ai_known_task_chats(set(channel_of.values()))
        if not chats:
            return self.browse()
        unreadable = chats - Channel._woow_project_ai_readable_cached(chats)
        return self.browse([id_ for id_, chat in channel_of.items() if chat in unreadable])

    def _woow_project_ai_check_fetched(self):
        """Refuse the fetched task-chat messages the current user cannot read
        under the module's rule; their cached values are dropped first (the
        ORM then fetches the record asked for alone)."""
        hidden = self._woow_project_ai_hidden_in_cache()
        if hidden:
            hidden.invalidate_recordset()
            raise AccessError(self.env._('This message is in a task AI conversation you cannot read.'))

    @api.model
    def _search(self, domain, offset=0, limit=None, order=None):
        if not self.env.su:
            exclusion = self._woow_project_ai_search_exclusion(domain)
            if exclusion:
                domain = expression.AND([domain, exclusion])
        return super()._search(domain, offset, limit, order)

    @api.model
    def _woow_project_ai_search_exclusion(self, domain):
        """The messages of the task chats the current user cannot read, left
        out: nothing for a domain about other models only; for given channels,
        the registry cache (one query only for channels it never saw) and the
        user's readable task chats of this transaction; otherwise a test of
        each message's own channel (a task chat created later is left out
        too)."""
        scope = _domain_scope(domain)
        if scope is NO_CHANNEL:
            return None
        Channel = self.env['discuss.channel']
        if isinstance(scope, set):
            chats = Channel._woow_project_ai_known_task_chats(scope)
            unreadable = chats - Channel._woow_project_ai_readable_cached(chats) if chats else set()
            if not unreadable:
                return None
            return ['|', ('model', '!=', 'discuss.channel'), ('res_id', 'not in', sorted(unreadable))]
        # a message of another model matches the first leaf; one of an ordinary channel the second: its own
        # channel looked up by primary key (correlated with the message row: no set of all task chats is
        # built); a task-chat message then needs the third: the user's readable task chats (their member chats,
        # a literal list; a system administrator's compiled sub-query, evaluated once per statement and only
        # when a task-chat message is met)
        Channel.flush_model(['woow_project_ai_task_id'])
        exclusion = ['|', ('model', '!=', 'discuss.channel'), ('res_id', 'not in', SQL(
            "(SELECT woow_c.id FROM discuss_channel woow_c WHERE woow_c.id = %s "
            "AND woow_c.woow_project_ai_task_id IS NOT NULL)", SQL.identifier(self._table, 'res_id')))]
        # (then their readable member chats; for a system administrator, last, all the task chats they may read)
        return expression.OR([exclusion] + [[('res_id', 'in', value)]
                                            for value in Channel._woow_project_ai_readable_values()])

    @api.depends('record_name', 'model', 'res_id')
    @api.depends_context('uid', 'lang')
    def _compute_display_name(self):
        """The name of a task-chat message is the chat's ("<task> | Internal
        AI"), read in SQL (so that a name shown with sudo loads nothing else of
        the message), and shown with sudo wherever a record links it
        (mail.notification, readable by every employee): "Task AI chat" for
        people who cannot read the chat. Every other message: Odoo's name, and
        any other module's (super), from the same query: its record_name,
        model and res_id are put in the cache when missing (one query per
        batch, as Odoo's own)."""
        self.flush_recordset(['record_name', 'model', 'res_id'])
        # a new record with an origin (an onchange) shows its origin's name
        keys = {message: as_id(message.id) or as_id(message._origin.id) for message in self}
        ids = sorted({key for key in keys.values() if key})
        chat_rows, others = {}, {}
        if ids:
            self.env['discuss.channel'].flush_model(['woow_project_ai_task_id'])
            self.env.cr.execute("""
                SELECT m.id, m.record_name, m.model, m.res_id, c.id
                  FROM mail_message m
                  LEFT JOIN discuss_channel c ON m.model = 'discuss.channel' AND c.id = m.res_id
                                             AND c.woow_project_ai_task_id IS NOT NULL
                 WHERE m.id = ANY(%s)""", [ids])
            for message_id, name, model, res_id, chat in self.env.cr.fetchall():
                if chat:
                    chat_rows[message_id] = (name, chat)
                else:
                    others[message_id] = (name, model, res_id)
        if others:
            known = self.browse(sorted(others))
            cache = self.env.cache
            for index, fname in enumerate(('record_name', 'model', 'res_id')):
                cache.insert_missing(known, self._fields[fname], [others[i][index] for i in known._ids])
        chat_messages = self.filtered(lambda message: keys[message] in chat_rows)
        super(MailMessage, self - chat_messages)._compute_display_name()
        if not chat_messages:
            return
        chats = {chat for _name, chat in chat_rows.values()}
        unreadable = set()
        if self.env.uid != SUPERUSER_ID:
            unreadable = chats - self.env['discuss.channel']._woow_project_ai_readable_cached(chats)
        convert = self._fields['record_name'].convert_to_display_name
        label = self.env._('Task AI chat')
        for message in chat_messages:
            name, chat = chat_rows[keys[message]]
            message.display_name = label if chat in unreadable else convert(name, message)

    def _message_notifications_to_store(self, store):
        # the failures Odoo shows the author of a message (read as superuser for them, /mail/data and the bus):
        # not those of task chats they may not read now
        messages = self - self._woow_project_ai_unreadable() if for_a_user(self.env) else self
        return super(MailMessage, messages)._message_notifications_to_store(store)

    # --- Helpers ---

    def _woow_project_ai_chat_ids(self):
        """Ids of the task AI chats of these messages, as stored (one query)."""
        ids = real_ids(self)
        if not ids:
            return set()
        self.flush_recordset(['model', 'res_id'])
        self.env['discuss.channel'].flush_model(['woow_project_ai_task_id'])
        self.env.cr.execute("""
            SELECT DISTINCT c.id
              FROM mail_message m
              JOIN discuss_channel c ON m.model = 'discuss.channel' AND c.id = m.res_id
                                    AND c.woow_project_ai_task_id IS NOT NULL
             WHERE m.id = ANY(%s)""", [ids])
        return {row[0] for row in self.env.cr.fetchall()}

    def _woow_project_ai_refuse(self):
        raise AccessError(self.env._('Messages of a task AI conversation can only be written, edited and deleted '
                                     'in the conversation itself.'))

    def _woow_project_ai_check_stored(self, created=False, moved=False):
        """The links of these messages as stored (see the module docstring), in
        one query: each message's task chat, its parent's task chat, and
        whether it carries a file it may not (a task chat's file on a message
        elsewhere; on a task chat's message, a file that is not of that chat
        or that its author did not upload there; the AI's replies: files the
        module creates as superuser), joined on the stored rows."""
        ids = real_ids(self)
        if not ids:
            return
        self.flush_recordset()
        self.env['discuss.channel'].flush_model(['woow_project_ai_task_id'])
        self.env['ir.attachment'].flush_model(['res_model', 'res_id', 'create_uid'])
        self.env['woow.project.ai.assistant'].flush_model(['partner_id'])
        self.env.cr.execute("""
            SELECT m.id, mc.id, pc.id,
                   EXISTS (
                       SELECT 1
                         FROM message_attachment_rel r
                         JOIN ir_attachment a ON a.id = r.attachment_id
                         LEFT JOIN discuss_channel ac ON a.res_model = 'discuss.channel' AND ac.id = a.res_id
                                                     AND ac.woow_project_ai_task_id IS NOT NULL
                         LEFT JOIN res_users u ON u.id = a.create_uid
                        WHERE r.message_id = m.id
                          AND ((ac.id IS NOT NULL
                                AND (m.model IS DISTINCT FROM 'discuss.channel' OR m.res_id IS DISTINCT FROM a.res_id))
                               OR (mc.id IS NOT NULL
                                   AND NOT COALESCE(
                                       a.res_model = 'discuss.channel' AND a.res_id = m.res_id
                                       AND (u.partner_id = m.author_id
                                            OR (a.create_uid = %(su)s AND m.author_id IN (
                                                SELECT partner_id FROM woow_project_ai_assistant
                                                 WHERE partner_id IS NOT NULL))), FALSE))))
              FROM mail_message m
              LEFT JOIN discuss_channel mc ON m.model = 'discuss.channel' AND mc.id = m.res_id
                                          AND mc.woow_project_ai_task_id IS NOT NULL
              LEFT JOIN mail_message p ON p.id = m.parent_id
              LEFT JOIN discuss_channel pc ON p.model = 'discuss.channel' AND pc.id = p.res_id
                                          AND pc.woow_project_ai_task_id IS NOT NULL
             WHERE m.id = ANY(%(ids)s)""", {'ids': ids, 'su': SUPERUSER_ID})
        rows = self.env.cr.fetchall()
        # a new task-chat message: only through the chat's own message_post, superuser included; a move into
        # one: never outside superuser
        check_chat = created or (moved and not self.env.su)
        for _message_id, here, parent_chat, bad_files in rows:
            if parent_chat and here != parent_chat:
                raise AccessError(self.env._('A message of a task AI conversation can only be answered in that '
                                             'conversation.'))
            if here and check_chat and (moved or not posted_by_this_module(self.env, here)):
                self._woow_project_ai_refuse()
            if bad_files:
                raise AccessError(self.env._('A file of a task AI conversation stays in that conversation, and a '
                                             'message there carries only files its author uploaded there.'))

    # --- Writing ---

    @api.model_create_multi
    def create(self, vals_list):
        messages = super().create(vals_list)
        if not self._woow_project_ai_links_nothing(vals_list):
            messages._woow_project_ai_check_stored(created=True)
        return messages

    @api.model
    def _woow_project_ai_links_nothing(self, vals_list):
        """Whether these create values can put nothing in a task chat nor link
        anything of one, without a query (most messages): no parent, no files,
        and a model other than discuss.channel (a plain string, or none), or a
        channel known not to be a task chat (a plain int id, the registry
        cache: a query only for channels it never saw); no default for any of
        those. Otherwise the stored rows are checked."""
        if defaults_set(self, ('model', 'parent_id', 'attachment_ids')):
            return False
        channel_ids = set()
        for vals in vals_list:
            if vals.get('parent_id') or vals.get('attachment_ids'):
                return False
            if stores_other_than(vals.get('model'), 'discuss.channel'):
                continue
            res_id = vals.get('res_id')
            if vals.get('model') != 'discuss.channel' or not isinstance(res_id, int) or isinstance(res_id, bool) \
                    or res_id <= 0:
                return False
            channel_ids.add(res_id)
        return not (channel_ids and self.env['discuss.channel']._woow_project_ai_known_task_chats(channel_ids))

    def write(self, vals):
        if not self.env.su:
            require_int_ids(self)
            if self._woow_project_ai_chat_ids():
                # Odoo itself never writes a task-chat message outside superuser: pins in SQL, stars on the
                # partner's side, edits and their cleanup as superuser
                self._woow_project_ai_refuse()
        res = super().write(vals)
        if LINK_FIELDS & set(vals):
            self._woow_project_ai_check_stored(moved=bool({'model', 'res_id'} & set(vals)))
        return res

    def copy_data(self, default=None):
        # Odoo 18's copy reads the copied fields (fetch, checked above, unless they are cached) and never checks
        # read: checked here whatever the cache
        require_int_ids(self)
        self._woow_project_ai_check_records()
        return super().copy_data(default=default)

    def unlink(self):
        if not self.env.su:
            require_int_ids(self)
            if self._woow_project_ai_chat_ids():
                # deleting a message in the chat empties it as superuser (_message_update_content)
                self._woow_project_ai_refuse()
        # what Odoo reads of the messages it deletes (their lines, for the cascade and the bus notice of the
        # deletion; ids only leave) is not checked: Odoo empties the whole cache after an unlink, and Odoo empties
        # it before each one when it deletes many messages one by one (a sent mailing), where checking would read
        # every message's place again
        return super(MailMessage, self.with_context(**{DELETING_KEY: DELETING})).unlink()

    def _message_reaction(self, content, action, partner, guest, store=None):
        """Reactions in a task chat (the route runs this as superuser, after
        Odoo's 'create' check, which also lets in a person the message's
        parent mentioned): only from a member who may read the chat now;
        never in an archived task's chats. Superuser itself (env.uid) is not
        checked."""
        chat_ids = self._woow_project_ai_chat_ids()
        if chat_ids:
            chats = self.env['discuss.channel'].sudo().with_context(active_test=False).browse(sorted(chat_ids))
            if chats.filtered(lambda chat: not chat.woow_project_ai_task_id.active):
                raise UserError(self.env._('This task is archived: its AI conversations can only be read.'))
            if self.env.uid != SUPERUSER_ID:
                if guest or not partner:
                    raise AccessError(self.env._('Guests cannot write in task AI conversations.'))
                for chat in chats:
                    if partner not in chat.channel_member_ids.partner_id or not chat._woow_project_ai_partner_allowed(
                            partner):
                        raise AccessError(self.env._('You cannot react in this task AI conversation: you need '
                                                     'access to the task.'))
        return super()._message_reaction(content, action, partner, guest, store=store)

    def _to_store(self, store, /, **kwargs):
        # the body has a prefetch group of its own: one fetch for the batch with Odoo's columns, as without
        # this module (it goes through the read check above as any fetch)
        if self._ids:
            self.fetch(['body'] + [name for name, field in self._fields.items()
                                   if field.prefetch is True and field.column_type])
        return super()._to_store(store, **kwargs)
