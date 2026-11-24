# -*- coding: utf-8 -*-
{
    'name': 'Project AI',
    'version': '18.0.1.0.0',
    'category': 'Services/Project',
    'summary': 'Two AI conversations on every task (internal, customer on the portal), answered by n8n or LiteLLM',
    'description': """
Project AI
==========

Every task gets two private AI conversations, answered by the AI assistants
its project chose. Standalone: the module carries its own runtime core and
depends on standard Odoo modules only; it installs next to the Discuss AI
modules (woow_ai_contact) without sharing anything with them.

* AI connections (system administrators): an n8n webhook (signed requests)
  or a LiteLLM server (OpenAI compatible chat completions, chat-only);
  write-only key, model, timeouts, retries, streaming, documents, files in
  replies, content logging, readiness check, Test Connection. "Safe for
  Customers": always for LiteLLM, for n8n only once an administrator ticked
  it (cleared when the workflow URL changes)
* AI assistants (system administrators): name, avatar, bot partner, system
  prompt, error message, history length, push key
* Projects (project managers): an internal and a customer AI assistant, the
  project text of each tab, the customer welcome message, the pause after a
  colleague writes; readiness and this month's replies per tab
* "Internal AI chat" (colleagues with Project rights who can read the task)
  and "Customer AI chat" (portal users who can read the task, also on the
  portal task page; colleagues join muted). Membership follows the task
  access: only this module adds members, synced on every access-relevant
  change and every hour; a post by someone who lost the access is refused at
  once, and every message (a person's, an AI reply, a push) first removes
  the members who lost the access; they read nothing any more, even before
  the sync. A tab's chat is found through the chat's own link only
* Every message is answered (no @AI): a burst of one person gets one answer,
  two people asking at once each get theirs. A colleague's message in the
  customer chat pauses the AI (and unmutes that colleague); an n8n handoff
  pauses it, and a customer message the AI cannot answer is flagged: one
  To-Do for the assignees. The customer chat never gets the internal brief,
  and takes no log note (its customers read everything in it)
* The AI gets the assistant's prompt, the project text of its tab and a short
  task summary read with the asker's own access rights (customer tab: the
  portal-readable task fields only); never the chatter
* Durable reply queue in the cron process, typing indicator, files both ways,
  Markdown replies, push API (n8n "skip, then push"), call logs and a
  permanent monthly usage per project and tab (a reference for invoicing by
  hand, no billing)
* Settings under Project > Configuration > Project AI

See README.md and docs/PROTOCOL.md in the repository.
    """,
    'author': 'WOOWTECH',
    'website': 'https://github.com/WOOWTECH/Woow_odoo_project_ai',
    'license': 'LGPL-3',
    'external_dependencies': {'python': ['requests']},
    'depends': ['project', 'mail', 'portal', 'bus', 'web'],
    'data': [
        'security/ir.model.access.csv',
        'security/security.xml',
        'data/data.xml',
        'views/ai_connection_views.xml',
        'views/ai_assistant_views.xml',
        'views/ai_job_views.xml',
        'views/ai_call_log_views.xml',
        'views/ai_usage_views.xml',
        'views/menus.xml',
        'views/project_project_views.xml',
        'views/project_task_views.xml',
        'views/portal_templates.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'woow_ai_task/static/src/common/**/*',
            'woow_ai_task/static/src/backend/**/*',
        ],
        # the portal task page embeds Odoo's Discuss page (/discuss/channel/<id>)
        'mail.assets_public': [
            'woow_ai_task/static/src/common/**/*',
        ],
        'web.assets_frontend': [
            'woow_ai_task/static/src/portal/**/*',
        ],
    },
    'uninstall_hook': 'uninstall_hook',
    'installable': True,
    'auto_install': False,
    'application': False,
}
