# -*- coding: utf-8 -*-
"""Messages of the task AI conversations.

Reading. Odoo reads a message with sudo (``mail.message.fetch``) wherever it
does not call ``read``: onchange origins and computes (im_livechat's
``parent_body``, the full composer's parent-derived fields), exports through
any model (``mail.notification``, starred messages, channel members), copies,
delegated fields (``mail.mail``), template rendering of ``object.body``.
``fetch`` is the one place all of them go through: outside superuser, a
message of a task chat is fetched only for someone who can read that chat
under the module's rule (``woow_project_ai_readable``: its task now, and for
the internal chat the projects of its texts). The rows are classified from
the cache with the registry cache of task-chat channel ids (no query; only
channels that exist are remembered), and the user's readable task chats are
computed once per transaction. ``_search`` leaves those messages out of the
user's searches too: Odoo lets their authors and notified partners find them
(inbox, starred, history, read_group).

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

from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.models import NewId
from odoo.osv import expression
from odoo.tools import SQL

from ..tools.ids import as_id, real_ids, require_int_ids
from .discuss_channel import posted_by_this_module

# what Odoo itself changes on a message without superuser rights (stars, pins)
MESSAGE_WRITABLE_FIELDS = {'starred_partner_ids', 'pinned_at'}
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

    def fetch(self, field_names):
        if self.env.su or not field_names or not self._ids:
            return super().fetch(field_names)
        names = list(field_names)
        # (as superuser, as mail's own fetch: no field-group check here)
        to_fetch = self.sudo()._determine_fields_to_fetch(names, ignore_when_in_cache=True)
        if not any(field.column_type for field in to_fetch):
            if to_fetch:
                # only x2many lines (who starred, the replies...): checked first (model / res_id are usually in
                # the cache already: no query then)
                self._woow_project_ai_check_records()
            return super().fetch(names)
        # model / res_id come with the asked fields (same query): the rows are then classified from the cache
        names += [name for name in ('model', 'res_id') if name not in names]
        result = super().fetch(names)
        self._woow_project_ai_check_fetched()
        return result

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

    def _woow_project_ai_check_fetched(self):
        """Refuse the fetched task-chat messages the current user cannot read
        under the module's rule; their cached values are dropped first (the
        ORM then fetches the record asked for alone)."""
        channel_of = self._woow_project_ai_chats_in_cache()
        if not channel_of:
            return
        Channel = self.env['discuss.channel']
        chats = Channel._woow_project_ai_known_task_chats(set(channel_of.values()))
        if not chats:
            return
        unreadable = chats - Channel._woow_project_ai_readable_cached(chats)
        if unreadable:
            self.browse([id_ for id_, chat in channel_of.items() if chat in unreadable]).invalidate_recordset()
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
        user's readable task chats of this transaction; otherwise a sub-query
        of the task chats that are not among them (a task chat created later
        is left out too)."""
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
        # PostgreSQL evaluates the user's compiled rule for messages of task chats only: a message of another
        # model, or of an ordinary channel, matches one of the first two leaves
        Channel.flush_model(['woow_project_ai_task_id'])
        return ['|', '|', ('model', '!=', 'discuss.channel'),
                ('res_id', 'not in', SQL("(SELECT id FROM discuss_channel WHERE woow_project_ai_task_id IS NOT NULL)")),
                ('res_id', 'in', SQL("(%s)", Channel._woow_project_ai_readable_query()))]

    @api.depends('record_name', 'model', 'res_id')
    @api.depends_context('uid', 'lang')
    def _compute_display_name(self):
        """The name of a task-chat message is the chat's ("<task> | Internal
        AI"), read in SQL (so that a name shown with sudo loads nothing else of
        the message), and shown with sudo wherever a record links it
        (mail.notification, readable by every employee): "Task AI chat" for
        people who cannot read the chat. Every other message: Odoo's name, and
        any other module's (super)."""
        self.flush_recordset(['record_name', 'model', 'res_id'])
        # a new record with an origin (an onchange) shows its origin's name
        keys = {message: as_id(message.id) or as_id(message._origin.id) for message in self}
        ids = sorted({key for key in keys.values() if key})
        rows = {}
        if ids:
            self.env['discuss.channel'].flush_model(['woow_project_ai_task_id'])
            self.env.cr.execute("""
                SELECT m.id, m.record_name, c.id
                  FROM mail_message m
                  JOIN discuss_channel c ON m.model = 'discuss.channel' AND c.id = m.res_id
                                        AND c.woow_project_ai_task_id IS NOT NULL
                 WHERE m.id = ANY(%s)""", [ids])
            rows = {row[0]: row[1:] for row in self.env.cr.fetchall()}
        chat_messages = self.filtered(lambda message: keys[message] in rows)
        super(MailMessage, self - chat_messages)._compute_display_name()
        if not chat_messages:
            return
        chats = {chat for _name, chat in rows.values()}
        unreadable = set()
        if self.env.uid != SUPERUSER_ID:
            unreadable = chats - self.env['discuss.channel']._woow_project_ai_readable_cached(chats)
        convert = self._fields['record_name'].convert_to_display_name
        label = self.env._('Task AI chat')
        for message in chat_messages:
            name, chat = rows[keys[message]]
            message.display_name = label if chat in unreadable else convert(name, message)

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
        """The links of these messages as stored (see the module docstring):
        one small query (each message's task chat, its parent's task chat,
        whether it has files, joined on the stored rows), and the files' query
        only for messages that have files."""
        ids = real_ids(self)
        if not ids:
            return
        self.flush_recordset()
        self.env['discuss.channel'].flush_model(['woow_project_ai_task_id'])
        self.env.cr.execute("""
            SELECT m.id, mc.id, pc.id,
                   EXISTS (SELECT 1 FROM message_attachment_rel r WHERE r.message_id = m.id)
              FROM mail_message m
              LEFT JOIN discuss_channel mc ON m.model = 'discuss.channel' AND mc.id = m.res_id
                                          AND mc.woow_project_ai_task_id IS NOT NULL
              LEFT JOIN mail_message p ON p.id = m.parent_id
              LEFT JOIN discuss_channel pc ON p.model = 'discuss.channel' AND pc.id = p.res_id
                                          AND pc.woow_project_ai_task_id IS NOT NULL
             WHERE m.id = ANY(%s)""", [ids])
        rows = self.env.cr.fetchall()
        # a new task-chat message: only through the chat's own message_post, superuser included; a move into
        # one: never outside superuser
        check_chat = created or (moved and not self.env.su)
        with_files = []
        for message_id, here, parent_chat, has_files in rows:
            if parent_chat and here != parent_chat:
                raise AccessError(self.env._('A message of a task AI conversation can only be answered in that '
                                             'conversation.'))
            if here and check_chat and (moved or not posted_by_this_module(self.env, here)):
                self._woow_project_ai_refuse()
            if has_files:
                with_files.append(message_id)
        if with_files:
            self._woow_project_ai_check_stored_files(with_files)

    @api.model
    def _woow_project_ai_check_stored_files(self, message_ids):
        """A task-chat file only on messages of its chat; a task-chat message
        only with files of its chat that its author uploaded (the AI's replies:
        files the module creates as superuser)."""
        self.env['ir.attachment'].flush_model(['res_model', 'res_id', 'create_uid'])
        self.env['discuss.channel'].flush_model(['woow_project_ai_task_id'])
        self.env['woow.project.ai.assistant'].flush_model(['partner_id'])
        self.env.cr.execute("""
            SELECT 1
              FROM message_attachment_rel r
              JOIN mail_message m ON m.id = r.message_id
              JOIN ir_attachment a ON a.id = r.attachment_id
              LEFT JOIN discuss_channel ac ON a.res_model = 'discuss.channel' AND ac.id = a.res_id
                                          AND ac.woow_project_ai_task_id IS NOT NULL
              LEFT JOIN discuss_channel mc ON m.model = 'discuss.channel' AND mc.id = m.res_id
                                          AND mc.woow_project_ai_task_id IS NOT NULL
              LEFT JOIN res_users u ON u.id = a.create_uid
             WHERE r.message_id = ANY(%(ids)s)
               AND ((ac.id IS NOT NULL
                     AND (m.model IS DISTINCT FROM 'discuss.channel' OR m.res_id IS DISTINCT FROM a.res_id))
                    OR (mc.id IS NOT NULL
                        AND NOT COALESCE(
                            a.res_model = 'discuss.channel' AND a.res_id = m.res_id
                            AND (u.partner_id = m.author_id
                                 OR (a.create_uid = %(su)s AND m.author_id IN (
                                     SELECT partner_id FROM woow_project_ai_assistant
                                      WHERE partner_id IS NOT NULL))), FALSE)))
             LIMIT 1""", {'ids': message_ids, 'su': SUPERUSER_ID})
        if self.env.cr.fetchone():
            raise AccessError(self.env._('A file of a task AI conversation stays in that conversation, and a '
                                         'message there carries only files its author uploaded there.'))

    # --- Writing ---

    @api.model_create_multi
    def create(self, vals_list):
        messages = super().create(vals_list)
        messages._woow_project_ai_check_stored(created=True)
        return messages

    def write(self, vals):
        if not self.env.su:
            require_int_ids(self)
            if set(vals) - MESSAGE_WRITABLE_FIELDS and self._woow_project_ai_chat_ids():
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
        return super().unlink()

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
