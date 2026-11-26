# -*- coding: utf-8 -*-
"""The notification centre lists a customer's notifications of every public
message, whatever its document, and reads their messages as superuser for
the customer (``mail.notification`` with sudo). A notification of a task AI
chat message the customer cannot read now (they were mentioned as a member,
then left the chat or lost the task) is left out: Project AI refuses its
content to them, and the centre would show an error page instead of the
list. The list, its pager, the unread count, the open and read routes and
the /my cards all build on this domain, so they agree.

``_wpn_public_message_domain`` is a private hook of woow_portal_notification:
if a release renames it, this override is dead code and the error page comes
back, so the registry load logs it as CRITICAL (never a refusal: the centre is
a page, not a reason to stop the database). The override passes through any
argument a later release may add."""

import logging

from odoo import api, models
from odoo.osv import expression

_logger = logging.getLogger(__name__)
HOOK = '_wpn_public_message_domain'


def hook_missing(model_class, bridge_class):
    """Whether no class below `bridge_class` in the MRO of `model_class`
    (woow_portal_notification and anything else under the bridge) defines the
    hook this bridge extends."""
    mro = model_class.__mro__
    return not any(HOOK in vars(klass) for klass in mro[mro.index(bridge_class) + 1:])


class ResUsers(models.Model):
    _inherit = 'res.users'

    def _register_hook(self):
        super()._register_hook()
        if hook_missing(type(self), ResUsers):
            _logger.critical(
                "woow_ai_task_portal_notification: woow_portal_notification no longer has %s; the notification "
                "centre may fail for customers who lost a task AI chat they were mentioned in. Update the bridge "
                "together with woow_portal_notification.", HOOK)

    @api.model
    def _wpn_public_message_domain(self, *args, **kwargs):
        domain = super()._wpn_public_message_domain(*args, **kwargs)
        # the user whose notifications these are (the centre calls it on the signed-in user); their readable
        # task chats (the centre searches as superuser, where mail.message's own search adds nothing)
        user = self if len(self) == 1 else self.env.user
        exclusion = self.env['mail.message'].with_user(user)._woow_project_ai_search_exclusion([])
        return expression.AND([domain, exclusion]) if exclusion else domain
