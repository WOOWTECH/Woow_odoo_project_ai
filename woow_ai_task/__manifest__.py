# -*- coding: utf-8 -*-
{
    'name': 'Project AI',
    'version': '18.0.1.0.0',
    'category': 'Services/Project',
    'summary': 'Two AI conversations on every task (internal, customer on the portal), answered by n8n or LiteLLM',
    'description': """
Project AI
==========

Each task gets up to two private AI conversations, one per AI assistant its
project chose (the customer one in a project shared with portal users).
Standalone: the module carries its own runtime core and depends on standard
Odoo modules only; it installs next to the Discuss AI modules
(woow_ai_contact) without sharing anything with them.

* AI connections (system administrators): an n8n webhook (signed requests)
  or a LiteLLM server (OpenAI compatible chat completions, chat-only);
  write-only key, model, timeouts, retries, streaming, documents, files in
  replies, content logging, readiness check, Test Connection. "Safe for
  Customers": always for LiteLLM, for n8n only once an administrator ticked
  it (cleared when the workflow URL changes)
* AI assistants (system administrators): name, avatar, bot partner, system
  prompt, error message, history length, push key; the description is shown
  to project managers on the project's "AI Assistants" page
* Projects (project managers): an internal and a customer AI assistant, the
  project text of each tab, the customer welcome message, the pause after a
  colleague writes; readiness and this month's replies per tab
* "Internal AI chat" (colleagues with Project rights who can read the task
  and its project) and "Customer AI chat" (portal users who can read the
  task, also on the portal task page; colleagues join muted). Membership follows the task
  access: only this module adds members, synced on every access-relevant
  change and by an hourly sweep that resumes where it stopped; a post by
  someone who lost the access is refused at once, and every message (a
  person's, an AI reply, a push) first removes the members who lost the
  access. Until then they read nothing (a global rule), and no poll of
  their open pages carries the chat any more, the message that removed them
  included. System administrators who can read the task read its chats too,
  without being members. A tab's chat is found through the chat's own link
  only
* Every message is answered (no @AI): a burst of one person gets one answer,
  two people asking at once each get theirs. A colleague's message in the
  customer chat pauses the AI (and unmutes that colleague); an n8n handoff
  pauses it, and every customer message the AI cannot answer (blocked, failed,
  waited too long) is flagged: one To-Do for the first assignee (else the
  project manager), reused while open. The customer chat never gets the
  internal brief, and takes no log note (its customers read everything in it)
* The AI gets the assistant's prompt, the project text of its tab (the
  internal one while every person of the chat can read the project, as the
  internal chat requires) and a short task summary read with the asker's own access rights
  (customer tab: the portal-readable task fields only); never the chatter
* Durable reply queue in the cron process, typing indicator, files both ways
  (45 s and 100 MB per answer), Markdown replies, push API (n8n "skip, then
  push"), call logs and a permanent monthly usage per project and tab (a
  reference for invoicing by hand, no billing)
* Settings under Project > Configuration > Project AI. With
  woow_odoo_portal_discuss, the bridge woow_ai_task_portal_discuss (and
  woow_ai_task_portal_notification with the portal notification centre) is
  installed automatically: task chats stay out of /my/discussions and its
  counts, and the centre counts their unread messages apart (a link to the
  task); with snailmail, woow_ai_task_snailmail keeps their files out of
  letters. Keep the four modules together on the addons path and in every
  image (README.md)

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
