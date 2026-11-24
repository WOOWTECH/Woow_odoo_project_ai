# -*- coding: utf-8 -*-
{
    'name': 'Project AI - Snailmail',
    'version': '18.0.1.0.0',
    'category': 'Services/Project',
    'summary': 'A file of a task AI conversation is never sent by post',
    'description': """
Bridge between Project AI (woow_ai_task) and Snailmail (snailmail, installed
automatically with mail), installed automatically with both.

A letter shows its file to every employee (its related attachment_datas and
attachment_fname are read as superuser) and the mail cron posts it: a letter
is never created or changed with a file of a task AI conversation, whatever
id the file was given as (Odoo stores a string or float id while its own
check reads none).
    """,
    'author': 'WOOWTECH',
    'website': 'https://github.com/WOOWTECH/Woow_odoo_project_ai',
    'license': 'LGPL-3',
    'depends': ['woow_ai_task', 'snailmail'],
    'data': [],
    'installable': True,
    'auto_install': True,
    'application': False,
}
