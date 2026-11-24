# -*- coding: utf-8 -*-
"""Files of the task AI conversations (attachments of their messages: res_model
``discuss.channel``, res_id the chat). Checked on the rows as stored, with the
ids as Odoo matches them in SQL (4242, 4242.0 and '4242' are the same file;
any other id is refused outside superuser):

* Only the person who uploaded a file changes or deletes it (and system
  administrators: bulk deletes, force_storage): Odoo checks a file's write
  and unlink as write access on its channel, which every member employee
  has, and its delete route lets internal users delete any file they may
  unlink.
* A file of a task chat stays in its chat: its res_model / res_id /
  res_field never change (for nobody); outside superuser no file is created
  in a task chat, nor moved into one, directly (uploads run as superuser;
  this keeps a member from setting the chat's avatar through a res_field
  file, or adding files to an archived task's chat). Its messages: only
  messages of its chat (mail_message.py), and it is never deleted "through" a
  message elsewhere (mail_thread.py, the delete route below).
* Its content and its name: other models read a linked file as superuser
  (related and stored computed fields: snailmail's ``attachment_datas`` /
  ``attachment_fname``, hr_expense's ``message_main_attachment_checksum``,
  also in an onchange; the label of any many2one to a file, which Odoo
  builds with sudo). For a task-chat file whose chat the real user
  (``env.uid``) cannot read under the module's rule (superuser and system
  administrators excepted): ``datas`` is empty, the display name a generic
  label, and every other field but where it belongs (res_model, res_id,
  res_field, public, type, company, who / when) is refused when it is read
  from the database as superuser for that user. In a user environment
  Odoo's own attachment checks already apply."""

import logging

from odoo import SUPERUSER_ID, api, models
from odoo.exceptions import AccessError

from ..tools.ids import as_id, defaults_set, real_ids, require_int_ids, stores_other_than
from .discuss_channel import for_a_user
from .mail_message import RULE_READ, RULE_READ_KEY

_logger = logging.getLogger(__name__)

# what a member may not change on someone else's file (its content and what it belongs to)
GUARDED_FIELDS = {'raw', 'datas', 'db_datas', 'url', 'type', 'name', 'mimetype', 'res_model', 'res_id',
                  'res_field', 'store_fname', 'checksum', 'file_size', 'public'}
PLACE_FIELDS = ('res_model', 'res_id', 'res_field')
# what Odoo's attachment checks and routing read of a file as superuser: read for anyone (the rest of a hidden
# task-chat file is refused, as superuser too, for the real user)
RULE_FIELDS = frozenset({'id', 'res_model', 'res_id', 'res_field', 'public', 'type', 'company_id',
                         'create_uid', 'create_date', 'write_uid', 'write_date'})


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    @api.model
    def _woow_project_ai_file_chats(self, ids):
        """{file id: task chat id} for those of `ids` (coerced as SQL would)
        that are files of a task AI chat, as stored."""
        ids = [value for value in map(as_id, ids or ()) if value]
        if not ids:
            return {}
        self.flush_model(['res_model', 'res_id'])
        self.env.cr.execute("""
            SELECT a.id, a.res_id FROM ir_attachment a
              JOIN discuss_channel c ON c.id = a.res_id AND c.woow_project_ai_task_id IS NOT NULL
             WHERE a.res_model = 'discuss.channel' AND a.id = ANY(%s)""", [ids])
        return dict(self.env.cr.fetchall())

    @api.model
    def _woow_project_ai_refuse_elsewhere(self, action):
        if action == 'delete':
            raise AccessError(self.env._('A file of a task AI conversation stays in that conversation: it cannot '
                                         'be deleted through a message elsewhere.'))
        if action == 'move':
            raise AccessError(self.env._('A file of a task AI conversation stays in that conversation: it cannot '
                                         'be moved to another document.'))
        if action == 'link':
            raise AccessError(self.env._('A file of a task AI conversation stays in that conversation: it cannot '
                                         'be the main attachment of another document.'))
        raise AccessError(self.env._('Files are added to a task AI conversation by posting them there.'))

    @api.model
    def _woow_project_ai_check_file_target(self, ids, model, res_id, action='delete'):
        """Refuse files of a task AI chat deleted through a message of another
        thread (`model`, `res_id`); our own code (superuser) excepted: Odoo's
        routes run these steps with sudo, so the real user is checked."""
        if self.env.uid == SUPERUSER_ID:
            return
        for chat_id in self._woow_project_ai_file_chats(ids).values():
            if (model, res_id) != ('discuss.channel', chat_id):
                self._woow_project_ai_refuse_elsewhere(action)

    def _woow_project_ai_sees_every_file(self):
        """Superuser (env.uid) and system administrators: every file, as
        Odoo's own checks (crons, the module's jobs, force_storage). A
        request without a user (``auth='none'``, uid None) is nobody."""
        return self.env.uid == SUPERUSER_ID or bool(self.env.uid and self.env.user._is_system())

    def _fetch_field(self, field):
        if (field.name in RULE_FIELDS and self.env.su and for_a_user(self.env)
                and self.env.context.get(RULE_READ_KEY) is not RULE_READ):
            # where a file belongs, read as superuser for a user (Odoo's routing and access checks): what Odoo
            # prefetches with it of a hidden task-chat file is not kept, and the read works
            return super(IrAttachment, self.with_context(**{RULE_READ_KEY: RULE_READ}))._fetch_field(field)
        return super()._fetch_field(field)

    def _fetch_query(self, query, fields):
        # a file read from the database as superuser for a real user (a related or stored computed field of
        # another model, an onchange, Odoo's own sudo reads): the content and name of a task-chat file they may
        # not read are refused; res_model / res_id come with them (same query) to classify the rows from the
        # cache (no query for files of other records)
        if not self.env.su or not for_a_user(self.env):
            return super()._fetch_query(query, fields)
        content = [field for field in fields if field.name not in RULE_FIELDS]
        if not content:
            return super()._fetch_query(query, fields)
        places = [self._fields[name] for name in ('res_model', 'res_id')]
        if any(field.column_type for field in fields):
            # res_model / res_id in the same SELECT as the asked columns (no query of their own)
            fields = list(fields) + [field for field in places if field not in fields]
        fetched = super()._fetch_query(query, fields)
        missing = fetched.filtered(lambda attachment: not all(
            self.env.cache.contains(attachment, field) for field in places))
        if missing:
            missing.fetch(['res_model', 'res_id'])
        hidden = fetched._woow_project_ai_hidden_in_cache()
        if hidden:
            hidden.invalidate_recordset([field.name for field in content])
            if self.env.context.get(RULE_READ_KEY) is not RULE_READ:
                _logger.info("Project AI: task chat file(s) %s refused to user %s", hidden.ids, self.env.uid)
                raise AccessError(self.env._('This file is in a task AI conversation you cannot read.'))
        return fetched

    def _woow_project_ai_hidden_in_cache(self):
        """Those of these fetched files that are task-chat files hidden from
        the real user (see _woow_project_ai_hidden_file_ids), classified from
        the cache (res_model / res_id) and the registry cache of task chats."""
        if not self._ids or self._woow_project_ai_sees_every_file():
            return self.browse()
        cache = self.env.cache
        models_ = cache._get_field_cache(self, self._fields['res_model'])
        res_ids = cache._get_field_cache(self, self._fields['res_id'])
        channel_of = {id_: res_ids[id_] for id_ in self._ids
                      if id_ and models_.get(id_) == 'discuss.channel' and res_ids.get(id_)}
        if not channel_of:
            return self.browse()
        Channel = self.env['discuss.channel']
        chats = Channel._woow_project_ai_known_task_chats(set(channel_of.values()))
        if not chats:
            return self.browse()
        unreadable = chats - Channel._woow_project_ai_readable_cached(chats)
        return self.browse([id_ for id_, chat in channel_of.items() if chat in unreadable])

    def _woow_project_ai_hidden_file_ids(self):
        """Ids (as given) of those of these files that are task-chat files whose
        chat the real user (env.uid, also under sudo) cannot read now (a new
        record, an onchange: its origin's)."""
        if not self._ids or self._woow_project_ai_sees_every_file():
            return set()
        origins = [as_id(attachment._origin.id) for attachment in self]
        # a file whose place is in the cache and is not a channel needs no query (a task-chat file never leaves
        # its chat, and nothing outside superuser moves a file into one)
        places = self.env.cache._get_field_cache(self, self._fields['res_model'])
        candidates = [i for i in origins if i and (i not in places or places[i] == 'discuss.channel')]
        if not candidates:
            return set()
        chats = self._woow_project_ai_file_chats(candidates)
        if not chats:
            return set()
        unreadable = set(chats.values()) - self.env['discuss.channel']._woow_project_ai_readable_cached(
            set(chats.values()))
        return {attachment.id for attachment in self if chats.get(as_id(attachment._origin.id)) in unreadable}

    def _woow_project_ai_foreign_files(self):
        """Ids of those of these files that are in a task AI chat and that the
        current user did not upload (none for superuser and system
        administrators)."""
        if self.env.su or not self._ids or (self.env.uid and self.env.is_system()):
            return []
        chats = self._woow_project_ai_file_chats(real_ids(self))
        if not chats:
            return []
        self.flush_model(['create_uid'])
        self.env.cr.execute("SELECT id FROM ir_attachment WHERE id = ANY(%s) AND create_uid IS DISTINCT FROM %s",
                            [list(chats), self.env.uid])
        return [row[0] for row in self.env.cr.fetchall()]

    def _woow_project_ai_refuse_file(self):
        raise AccessError(self.env._('Only the person who uploaded a file of a task AI conversation can change or '
                                     'delete it.'))

    def _woow_project_ai_places(self):
        """{file id: (res_model, res_id, res_field, task chat or None)} as stored."""
        ids = real_ids(self)
        if not ids:
            return {}
        self.flush_model(['res_model', 'res_id', 'res_field'])
        self.env.cr.execute("""
            SELECT a.id, a.res_model, a.res_id, a.res_field, c.id
              FROM ir_attachment a
              LEFT JOIN discuss_channel c ON a.res_model = 'discuss.channel' AND c.id = a.res_id
                                         AND c.woow_project_ai_task_id IS NOT NULL
             WHERE a.id = ANY(%s)""", [ids])
        return {row[0]: row[1:] for row in self.env.cr.fetchall()}

    @api.model_create_multi
    def create(self, vals_list):
        attachments = super().create(vals_list)
        # (no query for files of another model than discuss.channel, given as a plain string or none, without a
        # default: most files)
        elsewhere = not defaults_set(self, ('res_model',)) and all(
            stores_other_than(vals.get('res_model'), 'discuss.channel') for vals in vals_list)
        if not self.env.su and not elsewhere and any(
                place[3] for place in attachments._woow_project_ai_places().values()):
            # as stored, whatever put it there: a value, a default, a model name given as bytes or as
            # XML-RPC's base64 value (Odoo stores the text 'discuss.channel', and its own create check skips
            # a model name it does not know): a file goes into a task chat only through an upload or a post
            # (superuser)
            self._woow_project_ai_refuse_elsewhere('add')
        return attachments

    def _woow_project_ai_outside_chats_cached(self):
        """Whether all of these files are known, from the cache, not to be in
        a channel (no query: a task-chat file never leaves its chat, and
        nothing outside superuser moves a file into one; a move into a channel
        is checked on the rows as stored after the write)."""
        places = self.env.cache._get_field_cache(self, self._fields['res_model'])
        return all(isinstance(id_, int) and id_ in places and places[id_] != 'discuss.channel' for id_ in self._ids)

    def write(self, vals):
        before = None
        outside = bool(self._ids) and self._woow_project_ai_outside_chats_cached()
        if not self.env.su:
            require_int_ids(self)
            if GUARDED_FIELDS & set(vals) and not outside and self._woow_project_ai_foreign_files():
                self._woow_project_ai_refuse_file()
        if set(PLACE_FIELDS) & set(vals):
            before = {} if outside else self._woow_project_ai_places()
        res = super().write(vals)
        if before is not None and not (outside and stores_other_than(vals.get('res_model'), 'discuss.channel')):
            # (files that were in no channel and stay out of one: nothing to check)
            after = self._woow_project_ai_places()
            for file_id, (res_model, res_id, res_field, chat) in after.items():
                old = before.get(file_id)
                if old and old[3] and (res_model, res_id, res_field) != old[:3]:
                    # a task-chat file keeps its place, for everyone
                    self._woow_project_ai_refuse_elsewhere('move')
                if chat and not self.env.su and (not old or old[3] != chat):
                    self._woow_project_ai_refuse_elsewhere('add')
        return res

    def unlink(self):
        if not self.env.su:
            require_int_ids(self)
            if self._woow_project_ai_foreign_files():
                self._woow_project_ai_refuse_file()
        return super().unlink()

    def _delete_and_notify(self, message=None):
        # deleted "through" a message (Odoo's delete route, with sudo for portal users): only through a
        # message of the file's own chat (where only its author may delete it)
        if message:
            self._woow_project_ai_check_file_target(real_ids(self), message.sudo().model, message.sudo().res_id)
        return super()._delete_and_notify(message=message)

    @api.depends('store_fname', 'db_datas', 'file_size')
    @api.depends_context('bin_size', 'uid')
    def _compute_datas(self):
        # a new record (an onchange) reads its content from its origin: the origin's chat decides. Hidden files
        # first (SQL), so that their content and size are never read
        hidden = self._woow_project_ai_hidden_file_ids()
        visible = self.filtered(lambda attachment: attachment.id not in hidden)
        super(IrAttachment, visible)._compute_datas()
        for attachment in self - visible:
            attachment.datas = False

    @api.depends('name')
    @api.depends_context('uid', 'lang')
    def _compute_display_name(self):
        # Odoo shows the label of a many2one to a file with sudo (web_read), also in an onchange: the name of a
        # task-chat file only to whoever may read its chat (hidden files first, in SQL: their name is never read)
        hidden = self._woow_project_ai_hidden_file_ids()
        visible = self.filtered(lambda attachment: attachment.id not in hidden)
        super(IrAttachment, visible)._compute_display_name()
        label = self.env._('Task AI chat file')
        for attachment in self - visible:
            attachment.display_name = label
