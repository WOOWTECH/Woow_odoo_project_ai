# -*- coding: utf-8 -*-
{
    'name': 'Project AI - Portal Discussions',
    'version': '18.0.1.0.0',
    'category': 'Services/Project',
    'summary': 'Keeps the task AI chats of Project AI out of the portal discussion list and its counts',
    'description': """
Bridge between Project AI (woow_ai_task) and the portal discussions of
woow_odoo_portal_discuss (/my/discussions, its home card and tabs), installed
automatically with both. The task AI chats have their own place, the portal
task page, which checks the task access every time; the list reads with sudo
and shows the last message, so it leaves them out.

Its controller is a subclass of the portal discussion controller itself, so
the filter applies whatever the order in which the modules were loaded (also
when woow_odoo_portal_discuss is installed later into a running server).
    """,
    'author': 'WOOWTECH',
    'website': 'https://github.com/WOOWTECH/Woow_odoo_project_ai',
    'license': 'LGPL-3',
    'depends': ['woow_ai_task', 'woow_odoo_portal_discuss'],
    'data': [],
    'installable': True,
    'auto_install': True,
    'application': False,
}
