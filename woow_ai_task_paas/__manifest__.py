# -*- coding: utf-8 -*-
{
    'name': 'Project AI - PaaS',
    'version': '18.0.1.0.0',
    'category': 'Services/Project',
    'summary': 'Project AI on the PaaS platform: the customer AI chat on the /woow task page, the old platform task AI '
               'switched off',
    'description': """
Project AI - PaaS
=================

Bridge between Project AI (woow_ai_task) and the PaaS platform
(woow_paas_platform), installed automatically with both. It changes no
platform code: everything is done by inheritance, and every platform
touchpoint fails soft (see docs/PAAS.md in the repository).

* /woow task page: a "Customer AI Chat" tab, Odoo's own Discuss page of the
  task's customer AI chat embedded, with "Open this chat in a new tab"; the
  internal AI chat stays in the backend task form. /my/tasks keeps Project
  AI's own section.
* The old platform task AI is switched off, not deleted: its /api/ai/
  routes are refused, no project turns its agent runtime on, no task enables
  the old chat, the platform's own "Task Chat: ..." channels are archived and
  restricted to administrators (their old values recorded for an exact
  undo), its four crons are parked once no session is live, and its fields
  are hidden in the backend forms. A system parameter switches it back on,
  to run the platform's own tests of it or to bring it back on purpose.
* Task AI chat members follow the PaaS-only access changes: workspace
  membership rows and the project - cloud service binding.
* Only project managers and administrators set a project's AI assistants
  and texts (on the platform every employee may write projects).

It stores no view, field, rule, filter, action or cron of its own (the form
changes are made in code). Ship it with the other five modules in every
image that serves the platform's databases (README.md). Never uninstall
this module alone (it is refused): uninstall Project AI (woow_ai_task),
which removes it too.
    """,
    'author': 'WOOWTECH',
    'website': 'https://github.com/WOOWTECH/Woow_odoo_project_ai',
    'license': 'LGPL-3',
    'depends': ['woow_ai_task', 'woow_paas_platform'],
    'data': [
        'data/legacy_task_ai.xml',
    ],
    'assets': {
        # appended to the platform's own /woow bundle (other modules may add files to a named bundle)
        'woow_paas_platform.assets_paas': [
            'woow_ai_task_paas/static/src/paas/**/*',
        ],
    },
    'post_init_hook': 'post_init_hook',
    'uninstall_hook': 'uninstall_hook',
    'installable': True,
    'auto_install': True,
    'application': False,
}
