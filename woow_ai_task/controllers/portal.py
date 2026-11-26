# -*- coding: utf-8 -*-
"""Customer AI chat on the portal task page (/my/tasks/<id> and
/my/projects/<project>/task/<id>): shown only to a logged-in user who can read
the task, when the project has a customer AI assistant. The conversation is
Odoo's own Discuss page, embedded (an iframe loaded on demand) or opened in a
new tab.

Task AI chats are left out of the portal discussion list of
woow_odoo_portal_discuss (/my/discussions, when that module is installed):
they are opened from the task page, which checks the task access every time,
while the list reads with sudo and shows the last message. The filter that
holds whatever the load order is the bridge woow_ai_task_portal_discuss
(installed automatically with both modules); the override here is defence in
depth (see _get_portal_discussion_domain)."""

import re
from urllib.parse import quote

from werkzeug.exceptions import NotFound

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request
from odoo.osv import expression
from odoo.tools import consteq
from odoo.tools.misc import hmac as hmac_sign

from odoo.addons.project.controllers.portal import ProjectCustomerPortal

from ..models.project_project import TAB_CUSTOMER

# a "back" target must be a portal page of this site (as woow_odoo_portal_discuss checks it)
SAFE_BACK_URL = re.compile(r'^/my(/[A-Za-z0-9_\-]+)*/?$')
OPEN_TOKEN_SCOPE = 'woow_project_ai.portal_open'
NOT_A_TASK_CHAT = [('woow_project_ai_task_id', '=', False)]


class WoowProjectAiPortal(ProjectCustomerPortal):

    def _task_get_page_view_values(self, task, access_token, **kwargs):
        values = super()._task_get_page_view_values(task, access_token, **kwargs)
        values.update(self._woow_project_ai_portal_values(task))
        return values

    def _woow_project_ai_portal_values(self, task):
        """The customer AI chat section: only for a logged-in user (never an
        access token alone) who may be in the task's customer chat."""
        user = request.env.user
        task_sudo = task.sudo()
        shown = bool(
            not user._is_public() and task_sudo.project_id
            and task_sudo.project_id._woow_project_ai_tab_assistant(TAB_CUSTOMER)
            and task_sudo._woow_project_ai_user_allowed(user, TAB_CUSTOMER))
        if not shown:
            return {'woow_project_ai_customer_chat': False}
        # through the chat's own link (never a task-side pointer)
        channel = task_sudo._woow_project_ai_tab_channel(TAB_CUSTOMER)
        member = bool(channel) and user.partner_id in channel.channel_member_ids.partner_id
        back = request.httprequest.path if SAFE_BACK_URL.match(request.httprequest.path or '') else '/my/tasks'
        # opening creates or joins the conversation: only from this page (a token of this task and session)
        url = '/my/tasks/%s/woow_project_ai?token=%s' % (task_sudo.id, self._woow_project_ai_open_token(task_sudo.id))
        return {
            'woow_project_ai_customer_chat': True,
            # a colleague on the portal page: the copy says the customers read the chat (and the AI pauses)
            'woow_project_ai_chat_staff': user._is_internal(),
            'woow_project_ai_chat_url': url,
            # never loaded with the page (its composer would take the focus and scroll the page): on demand,
            # or at once for a member who came through the section's anchor (static/src/portal)
            'woow_project_ai_chat_member': member,
            'woow_project_ai_chat_full_url': '%s&full=1&back=%s' % (url, quote(back, safe='/')),
        }

    @staticmethod
    def _woow_project_ai_open_token(task_id):
        """Token of the open link: this task and this session only (not the
        session's CSRF token, which a URL would leak to logs and history)."""
        return hmac_sign(request.env(su=True), OPEN_TOKEN_SCOPE, (task_id, request.session.sid))

    @http.route('/my/tasks/<int:task_id>/woow_project_ai', type='http', auth='user', website=True)
    def portal_my_task_woow_project_ai(self, task_id, full=None, back=None, token=None, **_kw):
        """Open the customer AI chat of a task (create or join it), then show
        Odoo's Discuss page of it: embedded (default) or as a full page with the
        portal back bar of woow_odoo_portal_discuss, when installed. A GET with
        a side effect: only with the token the task page gives (another site
        cannot make a user join), else back to the task page."""
        user = request.env.user
        task = request.env['project.task'].sudo().browse(task_id).exists()
        if not task or not task.project_id or not task.project_id._woow_project_ai_tab_assistant(TAB_CUSTOMER) \
                or not task._woow_project_ai_user_allowed(user, TAB_CUSTOMER):
            raise NotFound()
        if not consteq(str(token or ''), self._woow_project_ai_open_token(task.id)):
            task_url = '/my/tasks/%s#woow_project_ai_chat' % task.id
            if full:
                return request.redirect(task_url)
            # the embedded frame (an older page, or another session): a short page that reloads the task
            # page itself, never the whole portal page inside the frame
            return request.render('woow_ai_task.portal_open_expired', {'task_url': task_url})
        try:
            channel = task._woow_project_ai_open_tab_for(user, TAB_CUSTOMER)
        except (AccessError, UserError):
            raise NotFound()
        url = '/discuss/channel/%s' % channel.id
        if full:
            back = back if back and SAFE_BACK_URL.match(back) else '/my/tasks/%s' % task.id
            url += '?discussions=1&back=%s' % quote(back, safe='/')
        return request.redirect(url)

    # --- woow_odoo_portal_discuss (optional): no task AI chat in /my/discussions ---

    def _get_portal_discussion_domain(self):
        """Defined by woow_odoo_portal_discuss (the /my/discussions list and its
        home card, read with sudo), whose own version does not call super.

        The order-independent filter is the bridge woow_ai_task_portal_discuss:
        a subclass of that module's controller, so it comes first in the
        combined portal controller whatever the import order (installed
        automatically with both modules). This override is defence in depth:
        it only matters while the bridge is not installed, and only in graph
        order (Odoo combines the leaf portal controllers walking
        CustomerPortal's subclasses in the order they were defined; project,
        and so this module, loads after woow_odoo_portal_discuss, which needs
        portal and mail only, so this class comes first). A portal discussion
        module imported later (installed into a running server) would come
        first instead and drop it: that is what the bridge is for, and what
        tests/test_coexistence.py checks. Without that module there is
        nothing to filter."""
        parent = getattr(super(), '_get_portal_discussion_domain', None)
        if parent is None:
            return NOT_A_TASK_CHAT
        return expression.AND([parent(), NOT_A_TASK_CHAT])
