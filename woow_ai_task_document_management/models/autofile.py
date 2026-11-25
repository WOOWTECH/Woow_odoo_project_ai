# -*- coding: utf-8 -*-
import inspect

from odoo import api, models
from odoo.osv import expression
from odoo.tools import SQL

# the task AI chats, as stored (a chat's task link is set when it is created and never changes)
TASK_CHATS = "(SELECT id FROM discuss_channel WHERE woow_project_ai_task_id IS NOT NULL)"
# the two private hooks of woow_document_management (18.0.2.0.0) this bridge extends, and how the base module
# calls them (models/autofile.py there: `self._woow_auto_file_checked(attachments, unfiled)`,
# `self._woow_backfill_domain()`): if either is gone, or can no longer be called that way, the bridge's
# overrides would be dead code and filing rules would copy task-chat files again, or every filing would fail
# silently in the base module's catch-all; so the registry refuses to load instead. A compatible change (an
# added optional parameter, another module's `*args, **kwargs` wrapper, renamed parameters) passes: the test is
# the call's shape, not the parameter names. A release's checks start a registry with both modules installed
# (README, "Once installed")
REQUIRED_HOOKS = {
    '_woow_auto_file_checked': (None, 'attachments', 'unfiled'),
    '_woow_backfill_domain': (None,),
}


def missing_hooks(model_class, bridge_class):
    """The REQUIRED_HOOKS that the classes below `bridge_class` in the MRO of
    `model_class` (woow_document_management and anything else under the
    bridge) do not define, or define so that the base module's call cannot
    bind (inspect.signature(...).bind with self and the call's arguments)."""
    mro = model_class.__mro__
    below = mro[mro.index(bridge_class) + 1:]
    missing = []
    for name, call in REQUIRED_HOOKS.items():
        owner = next((klass for klass in below if name in vars(klass)), None)
        if owner is None:
            missing.append(name)
            continue
        try:
            inspect.signature(vars(owner)[name]).bind(*call)
        except (TypeError, ValueError):
            missing.append(name)
    return missing


class WoowDocumentAutoRule(models.Model):
    _inherit = 'woow.document.auto.rule'

    def _register_hook(self):
        super()._register_hook()
        missing = missing_hooks(type(self), WoowDocumentAutoRule)
        if missing:
            raise RuntimeError(
                'woow_ai_task_document_management: woow_document_management no longer has %s, or it cannot be '
                'called the way woow_document_management calls it; filing rules could copy files of task AI '
                'conversations. Update the bridge together with woow_document_management.' % ', '.join(missing))

    @api.model
    def _woow_auto_file_checked(self, attachments, *args, **kwargs):
        # new files and files moved onto a record: never those of a task AI chat (one query, only when a
        # channel's file is among them)
        if any(attachment.res_model == 'discuss.channel' for attachment in attachments.sudo()):
            chat_files = self.env['ir.attachment']._woow_project_ai_file_chats(attachments.ids)
            if chat_files:
                attachments = attachments - attachments.browse(list(chat_files))
        return super()._woow_auto_file_checked(attachments, *args, **kwargs)

    def _woow_backfill_domain(self, *args, **kwargs):
        # the existing files of the rule's kind: never those of a task AI chat (pass-through: whatever the base
        # module passes reaches it)
        domain = super()._woow_backfill_domain(*args, **kwargs)
        if self.res_model == 'discuss.channel':
            self.env['discuss.channel'].flush_model(['woow_project_ai_task_id'])
            domain = expression.AND([domain, [('res_id', 'not in', SQL(TASK_CHATS))]])
        return domain
