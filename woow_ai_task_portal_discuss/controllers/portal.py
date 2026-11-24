# -*- coding: utf-8 -*-
"""/my/discussions without the task AI chats: a subclass of woow_odoo_portal_discuss's
controller (whose own domain method does not call super), so Python's method order
puts this filter first in the combined portal controller, whatever the order in which
the modules were imported."""

from odoo.osv import expression

from odoo.addons.woow_odoo_portal_discuss.controllers.portal import PortalDiscussions

NOT_A_TASK_CHAT = [('woow_project_ai_task_id', '=', False)]


class WoowProjectAiPortalDiscussions(PortalDiscussions):

    def _get_portal_discussion_domain(self):
        return expression.AND([super()._get_portal_discussion_domain(), NOT_A_TASK_CHAT])
