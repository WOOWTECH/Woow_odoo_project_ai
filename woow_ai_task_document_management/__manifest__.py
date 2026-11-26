# -*- coding: utf-8 -*-
{
    'name': 'Project AI - Document Management',
    'version': '18.0.1.0.0',
    'category': 'Services/Project',
    'summary': 'Filing rules never copy a file of a task AI conversation into a document folder',
    'description': """
Bridge between Project AI (woow_ai_task) and Woow Document Management
(woow_document_management), installed automatically with both.

A filing rule copies every file attached to records of its kind into a
document folder, as superuser, for everyone who may read the folder. A file
of a task AI conversation stays in its conversation: a rule on Discussion
Channel files the other channels' files, never a task chat's (new files,
files moved onto a chat, and the backfill of existing files).
    """,
    'author': 'WOOWTECH',
    'website': 'https://github.com/WOOWTECH/Woow_odoo_project_ai',
    'license': 'LGPL-3',
    'depends': ['woow_ai_task', 'woow_document_management'],
    'data': [],
    'installable': True,
    'auto_install': True,
    'application': False,
}
