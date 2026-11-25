# -*- coding: utf-8 -*-
"""The notification centre lists a customer's notifications of every public
message, whatever its document, and reads their messages as superuser for
the customer (``mail.notification`` with sudo). A notification of a task AI
chat message the customer cannot read now (they were mentioned as a member,
then left the chat or lost the task) is left out: Project AI refuses its
content to them, and the centre would show an error page instead of the
list. The list, its pager, the unread count, the open and read routes and
the /my cards all build on this domain, so they agree."""

from odoo import api, models
from odoo.osv import expression


class ResUsers(models.Model):
    _inherit = 'res.users'

    @api.model
    def _wpn_public_message_domain(self):
        domain = super()._wpn_public_message_domain()
        # the user whose notifications these are (the centre calls it on the signed-in user); their readable
        # task chats (the centre searches as superuser, where mail.message's own search adds nothing)
        user = self if len(self) == 1 else self.env.user
        exclusion = self.env['mail.message'].with_user(user)._woow_project_ai_search_exclusion([])
        return expression.AND([domain, exclusion]) if exclusion else domain
