# -*- coding: utf-8 -*-
"""The old task chats of the platform, switched off.

While the switch is off (models/legacy.py):

* ``chat_enabled`` never turns on: ``create`` writes it False (an explicit
  key also beats ``default_chat_enabled`` and ``ir.default``), ``write`` drops
  a truthy value (logged), and ``_create_chat_channel`` returns the task's
  existing channel (or none) and never creates one. That covers the /woow
  task create and update routes, which pass ``chat_enabled``. No value is
  cleared: the old data stays.
* the backend task form hides the two old chat groups and calls the page
  "Linked Cloud Services" (it keeps the service fields), in code
  (``_get_view``): no ``ir.ui.view`` record inherits a platform view.
* the platform's own old task chats ("Task Chat: ..." channels of type
  ``channel``, every employee can read them) are archived and restricted to
  system administrators, their old values recorded for an exact undo (also
  those no task points to any more).
"""

import logging

from odoo import api, models

from odoo.addons.woow_ai_task.tools.logs import log_unexpected

from .legacy import (LEGACY_CHAT_PREFIX, RELEASED_PARAM, RESTRICTED_PARAM, legacy_task_ai_enabled, read_json_param,
                     write_json_param)

_logger = logging.getLogger(__name__)

# backend task form (woow_paas_platform views/project_task_views.xml): hidden groups, relabelled page
TASK_FORM_HIDDEN = ("//group[field[@name='chat_enabled']]", "//group[field[@name='channel_id']]")
TASK_FORM_PAGE = "//page[@name='ai_chat']"


class ProjectTask(models.Model):
    _inherit = 'project.task'

    # --- the old task chat never turns on ---

    @api.model_create_multi
    def create(self, vals_list):
        if 'chat_enabled' in self._fields and not legacy_task_ai_enabled(self.env):
            vals_list = [dict(vals, chat_enabled=False) for vals in vals_list]
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('chat_enabled') and 'chat_enabled' in self._fields and not legacy_task_ai_enabled(self.env):
            vals = {key: value for key, value in vals.items() if key != 'chat_enabled'}
            _logger.info("woow_ai_task_paas: chat_enabled left out of a write on task(s) %s (the old task AI is off)",
                         self.ids)
            if not vals:
                return True
        return super().write(vals)

    def _create_chat_channel(self):
        parent = getattr(super(), '_create_chat_channel', None)
        if parent is not None and legacy_task_ai_enabled(self.env):
            return parent()
        self.ensure_one()
        # the existing channel or none: never a new one
        return self.channel_id if 'channel_id' in self._fields else self.env['discuss.channel']

    # --- backend form, in code (the switch is part of the view cache key) ---

    @api.model
    def _get_view(self, view_id=None, view_type='form', **options):
        arch, view = super()._get_view(view_id, view_type, **options)
        if view_type == 'form' and not legacy_task_ai_enabled(self.env):
            for xpath in TASK_FORM_HIDDEN:
                nodes = arch.xpath(xpath)
                if not nodes:
                    _logger.debug("woow_ai_task_paas: task form view %s has no %s", view.id, xpath)
                for node in nodes:
                    node.set('invisible', '1')
            pages = arch.xpath(TASK_FORM_PAGE)
            if not pages:
                _logger.debug("woow_ai_task_paas: task form view %s has no %s", view.id, TASK_FORM_PAGE)
            for page in pages:
                page.set('string', self.env._('Linked Cloud Services'))
        return arch, view

    @api.model
    def _get_view_cache_key(self, view_id=None, view_type='form', **options):
        key = super()._get_view_cache_key(view_id, view_type, **options)
        return key + (legacy_task_ai_enabled(self.env),)

    # --- the platform's old task chats: archived and restricted, recorded ---

    @api.model
    def _woow_project_ai_paas_restrict_legacy_chats(self):
        """Archive and restrict to system administrators the platform's own old
        task chats not recorded yet: every top-level channel of type
        ``channel`` named "Task Chat: ...", whether tasks point to it through ``channel_id``
        (archived tasks included; one chat can be shared by several tasks,
        since the platform copies ``channel_id`` with a task, and keeps its
        first name when a task is renamed) or no task does any more (the
        platform does not delete a chat with its task). Chats an
        administrator restored with the undo stay as they are. Other channels
        that tasks point to are only logged (ids), for a person to look at.
        The old values of each chat (active, group_public_id) are recorded in
        ``woow_ai_task_paas.restricted_chats`` first; messages, members and
        ``task.channel_id`` stay. Both writes are needed: the channel rule
        ignores ``active``, and administrators keep their access. Threads
        (sub-channels) are left out: Odoo forbids a group on them, and their
        access follows their parent's. Each chat is recorded and restricted in
        its own savepoint: one that cannot be written is logged (its id) and
        left as it is, recorded nowhere, and the others go on.

        Called by data/legacy_task_ai.xml at install and at every upgrade:
        all its work runs in a savepoint and any error there is logged (its
        type and code lines, never its text) and rolled back, never raised (a
        platform upgrade upgrades this module too). A second call changes
        nothing.

        Returns:
            dict: restricted (ids restricted now), others (ids only logged)
        """
        if legacy_task_ai_enabled(self.env):
            return {'restricted': [], 'others': []}
        self.env.flush_all()  # pending writes of the caller are not this function's to hide
        try:
            # the savepoint flushes this function's own writes before it is released, and on any error rolls back to
            # it and clears the caches: the transaction of the upgrade stays usable
            with self.env.cr.savepoint():
                return self._woow_project_ai_paas_restrict_legacy_chats_now()
        except Exception as error:  # noqa: BLE001 - rolled back to the savepoint: log and go on, never abort an upgrade
            log_unexpected(_logger, "woow_ai_task_paas: restricting the old task chats failed, nothing was changed",
                           (), error)
            return {'restricted': [], 'others': [], 'error': type(error).__name__}

    @api.model
    def _woow_project_ai_paas_restrict_legacy_chats_now(self):
        if 'channel_id' not in self._fields:
            _logger.info("woow_ai_task_paas: project.task has no channel_id: no old task chat to restrict")
            return {'restricted': [], 'others': []}
        Task = self.sudo().with_context(active_test=False)
        pointed = {channel.id for channel, _count in Task._read_group(
            [('channel_id', '!=', False)], ['channel_id'], ['__count'])}
        Channel = self.env['discuss.channel'].sudo().with_context(active_test=False)
        chats = Channel.search([('channel_type', '=', 'channel'), ('parent_channel_id', '=', False),
                                ('name', '=like', LEGACY_CHAT_PREFIX + '%')], order='id')
        others = sorted(pointed - set(chats.ids))
        if others:
            _logger.warning("woow_ai_task_paas: tasks point to %s channel(s) that are not the platform's own task "
                            "chats, left as they are (for a person to look at): %s", len(others), others)
        record = read_json_param(self.env, RESTRICTED_PARAM)
        released = set(read_json_param(self.env, RELEASED_PARAM, list))
        selected = chats.filtered(lambda chat: str(chat.id) not in record and chat.id not in released)
        if not selected:
            return {'restricted': [], 'others': others}
        administrators = self.env.ref('base.group_system')
        restricted, failed = [], []
        for channel in selected:
            entry = dict(record, **{str(channel.id): [bool(channel.active), channel.group_public_id.id or False]})
            try:
                # the record entry and the write together: a chat is recorded only when it is restricted
                with self.env.cr.savepoint():
                    write_json_param(self.env, RESTRICTED_PARAM, entry)
                    channel.write({'active': False, 'group_public_id': administrators.id})
            except Exception as error:  # noqa: BLE001 - this chat stays as it is; the others go on
                failed.append(channel.id)
                log_unexpected(_logger, "woow_ai_task_paas: old task chat %s could not be restricted, left as it is",
                               (channel.id,), error)
                continue
            record = entry
            restricted.append(channel.id)
        orphans = [chat_id for chat_id in restricted if chat_id not in pointed]
        _logger.info("woow_ai_task_paas: %s old task chat(s) archived and restricted to system administrators: %s "
                     "(no task points to %s of them: %s)", len(restricted), restricted, len(orphans), orphans)
        report = {'restricted': restricted, 'others': others}
        if failed:
            report['failed'] = failed
        return report

    @api.model
    def _woow_project_ai_paas_unrestrict_legacy_chats(self, channel_ids=None):
        """Undo: restore exactly the recorded values (active, group_public_id)
        of the recorded old task chats (all of them, or `channel_ids`), remove
        their entries and remember them as released (the restriction, which
        runs at every upgrade, leaves them alone from then on). A chat whose
        recorded group no longer exists stays restricted (and recorded):
        restoring "no group" would open it to every employee. Returns the ids
        restored."""
        record = read_json_param(self.env, RESTRICTED_PARAM)
        released = set(read_json_param(self.env, RELEASED_PARAM, list))
        keys = list(record) if channel_ids is None else [str(int(i)) for i in channel_ids]
        Channel = self.env['discuss.channel'].sudo().with_context(active_test=False)
        Groups = self.env['res.groups'].sudo()
        restored, kept = [], []
        for key in keys:
            if key not in record:
                continue
            values = record[key]
            try:
                active, group_id = bool(values[0]), int(values[1] or 0)
                channel = Channel.browse(int(key)).exists()
            except (TypeError, ValueError, IndexError):
                _logger.error("woow_ai_task_paas: the recorded values of old task chat %s are broken: kept", key)
                kept.append(key)
                continue
            if not channel:
                del record[key]  # deleted since: nothing to restore
                continue
            group = Groups.browse(group_id).exists() if group_id else Groups
            if group_id and not group:
                _logger.error("woow_ai_task_paas: old task chat %s: its recorded group %s no longer exists: it stays "
                              "restricted", channel.id, group_id)
                kept.append(key)
                continue
            channel.write({'active': active, 'group_public_id': group.id or False})
            restored.append(channel.id)
            released.add(channel.id)
            del record[key]
        write_json_param(self.env, RESTRICTED_PARAM, record)
        write_json_param(self.env, RELEASED_PARAM, sorted(released))
        _logger.info("woow_ai_task_paas: %s old task chat(s) restored to their recorded values: %s", len(restored),
                     restored)
        return restored
