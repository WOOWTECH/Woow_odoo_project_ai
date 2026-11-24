# -*- coding: utf-8 -*-
{
    'name': 'Project AI - Portal Notification Centre',
    'version': '18.0.1.0.0',
    'category': 'Services/Project',
    'summary': 'The unread-conversation count of the portal notification centre follows the discussion list',
    'description': """
Bridge between Project AI (woow_ai_task, through its portal discussion bridge)
and the Discussions bridge of the portal notification centre
(woow_portal_notification_discuss), installed automatically with both.

The centre's "N unread messages in Discussions" (the /my card and the
/my/notifications notice) links to /my/discussions; it now counts the unread
messages of the conversations that list shows, so the task AI chats (opened
from the portal task page) never make a count the list cannot clear.
    """,
    'author': 'WOOWTECH',
    'website': 'https://github.com/WOOWTECH/Woow_odoo_project_ai',
    'license': 'LGPL-3',
    'depends': ['woow_ai_task_portal_discuss', 'woow_portal_notification_discuss'],
    'data': [],
    'installable': True,
    'auto_install': True,
    'application': False,
}
