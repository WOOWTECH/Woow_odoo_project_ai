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
* Its content and its name: other models read a linked file with sudo
  (snailmail's ``attachment_datas`` / ``attachment_fname``, in an onchange
  as well; the label of any many2one to a file, which Odoo builds with
  sudo): ``datas`` is empty, and the file's display name a generic label,
  for a task-chat file whose chat the real user (``env.uid``; none in a
  request without a user) cannot read under the module's rule (superuser
  and system administrators excepted). In a user environment Odoo's own
  attachment checks already apply."""

from odoo import SUPERUSER_ID, api, models
from odoo.exceptions import AccessError

from ..tools.ids import as_id, real_ids, require_int_ids

# what a member may not change on someone else's file (its content and what it belongs to)
GUARDED_FIELDS = {'raw', 'datas', 'db_datas', 'url', 'type', 'name', 'mimetype', 'res_model', 'res_id',
                  'res_field', 'store_fname', 'checksum', 'file_size', 'public'}
PLACE_FIELDS = ('res_model', 'res_id', 'res_field')


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

    def _woow_project_ai_hidden_file_ids(self):
        """Ids (as given) of those of these files that are task-chat files whose
        chat the real user (env.uid, also under sudo) cannot read now (a new
        record, an onchange: its origin's)."""
        if not self._ids or self._woow_project_ai_sees_every_file():
            return set()
        chats = self._woow_project_ai_file_chats([attachment._origin.id for attachment in self])
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
        if not self.env.su and any(place[3] for place in attachments._woow_project_ai_places().values()):
            # as stored, whatever put it there: a value, a default, a model name given as bytes or as
            # XML-RPC's base64 value (Odoo stores the text 'discuss.channel', and its own create check skips
            # a model name it does not know): a file goes into a task chat only through an upload or a post
            # (superuser)
            self._woow_project_ai_refuse_elsewhere('add')
        return attachments

    def write(self, vals):
        before = None
        if not self.env.su:
            require_int_ids(self)
            if GUARDED_FIELDS & set(vals) and self._woow_project_ai_foreign_files():
                self._woow_project_ai_refuse_file()
        if set(PLACE_FIELDS) & set(vals):
            before = self._woow_project_ai_places()
        res = super().write(vals)
        if before is not None:
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
        super()._compute_datas()
        if self.env.context.get('bin_size'):
            return
        # a new record (an onchange) reads its content from its origin: the origin's chat decides
        hidden = self._woow_project_ai_hidden_file_ids()
        for attachment in self:
            if attachment.id in hidden:
                attachment.datas = False

    @api.depends('name')
    @api.depends_context('uid', 'lang')
    def _compute_display_name(self):
        # Odoo shows the label of a many2one to a file with sudo (web_read), also in an onchange: the name of a
        # task-chat file only to whoever may read its chat
        super()._compute_display_name()
        hidden = self._woow_project_ai_hidden_file_ids()
        if hidden:
            label = self.env._('Task AI chat file')
            for attachment in self:
                if attachment.id in hidden:
                    attachment.display_name = label
