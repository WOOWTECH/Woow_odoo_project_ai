# -*- coding: utf-8 -*-
{
    'name': 'Project AI - Portal Notification Centre',
    'version': '18.0.1.0.0',
    'category': 'Services/Project',
    'summary': 'The portal notification centre counts the task AI chats apart, with a link to the task',
    'description': """
Bridge between Project AI (woow_ai_task, through its portal discussion bridge)
and the Discussions bridge of the portal notification centre
(woow_portal_notification_discuss), installed automatically with both.

* The centre's "N unread messages in Discussions" (the /my card and the
  /my/notifications notice) links to /my/discussions; it counts the unread
  messages of the conversations that list shows, so the task AI chats
  (opened from the portal task page) never make a count the list cannot
  clear.
* The unread messages of the customer's task AI chats have their own notice
  and /my card, which open the task page (one task) or the customer's tasks.
    """,
    'author': 'WOOWTECH',
    'website': 'https://github.com/WOOWTECH/Woow_odoo_project_ai',
    'license': 'LGPL-3',
    'depends': ['woow_ai_task_portal_discuss', 'woow_portal_notification_discuss'],
    'data': ['views/portal_templates.xml'],
    'installable': True,
    'auto_install': True,
    'application': False,
}
