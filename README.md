# Woow Odoo Project AI

Odoo 18 addon for **project AI**: every task gets two AI conversations, an
internal one for the colleagues who can read the task and a customer one for
the portal users who can read it (also on the portal task page). The project
picks one **AI assistant** per conversation; each assistant uses an
**AI connection** to an n8n webhook or a LiteLLM (OpenAI-compatible) server.

The module ships its own runtime core (connections, assistants, reply queue,
call logs, monthly usage, push API). It depends on standard Odoo modules only
and installs next to the Discuss AI modules (`woow_ai_contact`) and Live Chat
AI (`im_livechat_ai`) without sharing anything with them.

[繁體中文說明](README_zh-TW.md)

| Module | Status | What it does |
|---|---|---|
| `woow_ai_task` | 18.0.1.0.0 | Project AI: internal and customer AI chats on every task |

## Features

- **AI connections** (system administrators): an n8n webhook (requests
  optionally signed with HMAC-SHA256) or a LiteLLM server (OpenAI-compatible
  chat completions; Odoo sends the conversation and never any tool, so these
  are chat-only). Write-only key, model, timeouts, retries, streaming,
  documents, files in replies, content logging, a readiness check and *Test
  Connection*. **Safe for Customers**: always for LiteLLM; for n8n only once
  an administrator ticked it, and cleared when the workflow URL or the
  backend type changes.
- **AI assistants** (system administrators): name, avatar, an archived bot
  partner (never offered in "Start a conversation", still posts in the task
  chats), system prompt, error message, history length, push key.
- **Projects** (project managers): an internal and a customer AI assistant
  (the customer one must use a connection safe for customers), the project
  text of each tab, a welcome message for the customer chat, the pause after
  a colleague writes; readiness and this month's AI replies per tab.
- **Two private conversations per task**, created when someone opens the tab:
  - *Internal AI chat*: colleagues with Project rights who can read the task;
  - *Customer AI chat*: portal users who can read the task (also on the
    portal task page); colleagues may read and write there and join muted.
- **Membership follows the task access**, by Odoo's own rules: only this
  module adds members; synced on every access-relevant change and every hour;
  a post by someone who lost the access is refused and they are removed at
  once; every message (a person's, an AI reply, a push) first removes the
  members who lost the access; until the sync, members who lost the access
  read nothing (a global rule that changes nothing for any other channel). A tab's chat is found
  through the chat's own link only, so no task write can point a tab at
  another conversation.
- **Every message is answered** (no @AI): a burst of one person gets one
  answer, two people asking at once each get theirs. A colleague's message
  in the customer chat pauses the AI and unmutes that colleague; an n8n
  handoff pauses it and schedules one To-Do for the assignees; a customer
  message the AI cannot answer schedules that To-Do too.
- **What the AI gets**: the assistant's prompt, the project text of its tab
  (the customer chat never gets the internal one) and a short task summary
  read with the asker's own rights (customer chat: the portal-readable task
  fields, without related tasks; internal chat: related records every member
  can read); never the chatter. The customer chat takes no log note (its
  customers read everything in it).
- **Durable reply queue** in the cron process, typing indicator, files both
  ways (images inline, documents as file parts or signed links), Markdown
  replies, push API for n8n "skip, then push", call logs (30 days) and a
  permanent monthly usage per project and tab (a reference for invoicing by
  hand, no billing: limits live in the LiteLLM keys).

## Install

1. Put this repository (or the `woow_ai_task` folder) on the addons path.
2. Install *Project AI* (`woow_ai_task`). It depends on `project`, `mail`,
   `portal`, `bus` and `web`; zh_TW translations included.
3. Settings: Project > Configuration > Project AI (system administrators):
   *AI Connections*, *AI Assistants*, *Reply Jobs*, *Call Logs*, *AI Usage*.
   Then, on each project, the *AI Assistants* page.

Set `web.base.url` to the public address of Odoo (and
`web.base.url.freeze`): the signed download links sent to the backends are
built from it. Give the cron enough time: `limit_time_real_cron` at least the
longest connection timeout + 60 s (+ 70 s with prefork workers); otherwise
replies run in a thread of the web worker (the assistant form says so).

## Once installed: keep the module in every image

The module adds a **global record rule on `discuss.channel`** whose domain
uses one of its computed fields, plus views and crons. Every image that
serves the database must therefore contain `woow_ai_task` as long as it is
installed: without its code, every non-superuser channel read fails (Discuss,
Live Chat and portal chats break site-wide).

To remove it, **uninstall it on an image that contains it**. The uninstall
hook first switches the global rule off, removes the people from the task
chats (the AI bots stay) and archives the chats (history kept, readable by
system administrators); Odoo then deletes the module's records (the rule
first) and only after that its fields and tables. Where another installed
module depends on it (for example a future PaaS-only bridge), it cannot be
uninstalled alone: forward-fix, or restore the database and filestore
snapshot taken before the upgrade.

`odoo-bin neutralize` (copies for testing): the AI connections lose their
URL and key and are archived, n8n "Safe for Customers" is cleared, push keys
are revoked; each table is checked first (`to_regclass`), so a database where
the module's tables are missing can still be neutralized.

## Protocol and details

- Wire format of both backends, the push API, signed attachment URLs, reply
  rendering, usage: [docs/PROTOCOL.md](docs/PROTOCOL.md).
- Behaviour in detail (Traditional Chinese): [docs/PROJECT.md](docs/PROJECT.md).

## Privacy and security

- Task chats are private group conversations: only members read them, and
  only while they can read the task now. Notifications (web push, mentions)
  only reach people who can read the task. Odoo keeps an already open
  websocket subscription until the client re-subscribes: a client that
  ignores the leave notice keeps receiving the chat's live messages until it
  reconnects.
- Connections (URLs, keys) and prompts are for system administrators only;
  keys are never readable and never sent to a browser; push keys are stored
  hashed. Project managers read assistant names only.
- The task summary is read with the asker's own access rights, never sudo;
  the customer chat gets only portal-readable task fields.
- Backend replies are untrusted: HTML is escaped, links are limited to
  http/https/mailto, files are downloaded only from allowlisted hosts with a
  size limit, and their type is detected from the content.
- Call logs hold status and timing only, unless *Log Request Content* is on
  for a connection (turning it off erases the stored content).

## Repository

Gitea `woow-paas/Woow_odoo_project_ai` is the source of truth. GitHub
`WOOWTECH/Woow_odoo_project_ai` is a push mirror of its `main` branch and
tags (`.gitea/workflows/mirror-to-github.yml`). Send changes as Gitea pull
requests.

## License

LGPL-3.0 — see [LICENSE](LICENSE).
