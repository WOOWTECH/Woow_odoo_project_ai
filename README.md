# Woow Odoo Project AI

Odoo 18 addon for **project AI**: each task gets up to two AI conversations,
one per AI assistant its project chose: an internal one for the colleagues
with Project rights who can read the task and its project, and a customer
one for the portal users who can read it (also on the portal task page; in a
project shared with portal users). Each assistant uses an **AI connection**
to an n8n webhook or a LiteLLM (OpenAI-compatible) server.

The module ships its own runtime core (connections, assistants, reply queue,
call logs, monthly usage, push API). It depends on standard Odoo modules only
and installs next to the Discuss AI modules (`woow_ai_contact`) and Live Chat
AI (`im_livechat_ai`) without sharing anything with them.

[繁體中文說明](README_zh-TW.md)

| Module | Status | What it does |
|---|---|---|
| `woow_ai_task` | 18.0.1.0.0 | Project AI: internal and customer AI chats on every task |
| `woow_ai_task_portal_discuss` | 18.0.1.0.0 | Bridge, installed automatically with `woow_odoo_portal_discuss`: task chats stay out of `/my/discussions`, its tabs and its card |
| `woow_ai_task_portal_notification` | 18.0.1.0.0 | Bridge, installed automatically with `woow_portal_notification_discuss`: the notification centre's unread conversations count what `/my/discussions` shows; the unread messages of the customer's task AI chats get their own notice and `/my` card, which open the task page |
| `woow_ai_task_snailmail` | 18.0.1.0.0 | Bridge, installed automatically with `snailmail` (which Odoo installs with `mail`): a file of a task AI chat is never put in a letter |
| `woow_ai_task_document_management` | 18.0.1.0.0 | Bridge, installed automatically with `woow_document_management`: a filing rule never copies a file of a task AI chat into a document folder |

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
  chats), system prompt, error message, history length, push key; the
  description is shown to project managers under each picker of the project's
  "AI Assistants" page.
- **Projects** (project managers): an internal and a customer AI assistant
  (the customer one must use a connection safe for customers), the project
  text of each tab, a welcome message for the customer chat, the pause after
  a colleague writes; readiness and this month's AI replies per tab.
- **Up to two private conversations per task** (one per assistant the
  project chose), created when someone opens the tab:
  - *Internal AI chat*: colleagues with Project rights who can read the task
    and its project, and every project whose internal text an answer used
    (a task moved to another project keeps its earlier answers with the
    readers of the old one);
  - *Customer AI chat*: portal users who can read the task (also on the
    portal task page), in a project shared with portal users; colleagues may
    read and write there and join muted.
- **Membership follows the task access**, by Odoo's own rules: only this
  module adds members; synced on every access-relevant change and by an
  hourly sweep (it resumes where it stopped when a round outgrows the cron's
  time limit); a post by someone who lost the access is refused and they are
  removed at once; every message (a person's, an AI reply, a push) first
  removes the members who lost the access. Until the sync, members who lost
  the access read nothing (a global rule that changes nothing for any other
  channel), and their open pages get no live message of the chat any more,
  the message that removed them included (every bus poll leaves out the task
  chats the polling user cannot read now). A tab's chat is found through the
  chat's own link only, so no task write can point a tab at another
  conversation.
- **Every message is answered** (no @AI): a burst of one person gets one
  answer, two people asking at once each get theirs. A colleague's message
  in the customer chat pauses the AI and unmutes that colleague; an n8n
  handoff pauses it and schedules a To-Do for the first assignee (else the
  project manager); so does every customer message the AI cannot answer (the
  tab is blocked, the backend failed, or the question waited too long: the
  customer gets the error message). An open To-Do is reused; once it is
  done, the next such message schedules a new one.
- **What the AI gets**: the assistant's prompt, the project text of its tab
  (the customer chat never gets the internal one; the internal one only
  while every person of the chat can read the project, as the internal chat
  requires) and a short task summary read with the asker's own rights
  (customer chat: the portal-readable task fields, without related tasks;
  internal chat: related records every member can read); never the chatter.
  The customer chat takes no log note (its customers read everything in it).
- **Durable reply queue** in the cron process, typing indicator, files both
  ways (images inline, documents as file parts or signed links; the files of
  one answer: 45 seconds and 100 MB together, a failed download stays a
  link), Markdown replies, push API for n8n "skip, then push", call logs
  (30 days) and a permanent monthly usage per project and tab (a reference
  for invoicing by hand, no billing: limits live in the LiteLLM keys).

## Install

1. Put this whole repository on the addons path: the four bridges must be
   there too, so that Odoo installs them with the modules they bridge.
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

### Cron workers next to Discuss AI

Each AI module (this one and `woow_ai_contact`) has a priority-1 dispatcher
cron that keeps one cron worker for its pass: up to its window, plus the
longest connection timeout, plus 60 s.

- Plan one cron thread per AI dispatcher plus one for every other cron:
  `max_cron_threads` >= 3 with both modules on one database (woow_prod).
- When the AI dispatchers are as many as the cron threads (for example
  2 and 2), both use short passes (a 60 s window, a 5 s wait), so the other
  crons (deploys, the e-mail queue) get a turn between passes.
- With a single cron thread, `limit_time_real_cron` must cover both passes
  (Discuss AI's runs first): otherwise this module's replies run in thread
  mode. A pass that finds too little time left starts no reply and runs
  again shortly.
- A threaded cron process needs `db_maxconn` >= both modules' concurrency + 6.

## Once installed: keep the five modules in every image

The module adds a **global record rule on `discuss.channel`** whose domain
uses one of its computed fields, plus views and crons. Every image that
serves the database must therefore contain `woow_ai_task` as long as it is
installed: without its code, every non-superuser channel read fails (Discuss,
Live Chat and portal chats break site-wide).

Ship the five modules together, in every image (rollback images included):
for paas-odoo-ci, `addons-sources.py sync --modules
woow_ai_task,woow_ai_task_portal_discuss,woow_ai_task_portal_notification,woow_ai_task_snailmail,woow_ai_task_document_management`
and the same five names in `paas-addons.list` and `cloud-addons.list`. Odoo
installs a bridge by itself only while one of its dependencies is being
installed; `-u`, `-u all` and *Update Apps List* never install a new module.
So when a bridge reaches a database after `woow_ai_task` and the portal
module it bridges are both installed, install it explicitly (it is
harmless): `-i woow_ai_task_portal_discuss` where `woow_odoo_portal_discuss`
is installed, `-i woow_ai_task_portal_notification` where
`woow_portal_notification_discuss` is, `-i woow_ai_task_snailmail` where
`snailmail` is, `-i woow_ai_task_document_management` where
`woow_document_management` is (or from Apps). Never install a bridge
where its other side is missing: that would install the other side too.

Module operations from Apps or `odoo shell` (install, upgrade, uninstall:
`button_immediate_*`) are refused while any cron of the database runs
("Odoo is currently processing a scheduled action"), and the AI dispatchers
(this module's, `woow_ai_contact`'s) and the membership sweep hold their
cron for a whole pass. Run them in a quiet moment; or first set the system
parameters `woow_ai_task.job_runner` (and `woow_ai_contact.job_runner`) to
`thread` and wait for the current pass to end; or stop the cron process
(`max_cron_threads=0` on the cron pod) and try again. `odoo -i` / `-u` from
the command line does not take that lock.

To remove it, **uninstall it on an image that contains it**. The uninstall
hook first switches the global rule off, removes the people from the task
chats (the AI bots stay) and archives the chats (history kept, readable by
system administrators); Odoo then deletes the module's records (the rule
first) and only after that its fields and tables. Uninstalling it also
uninstalls its four bridges (`woow_ai_task_portal_discuss`,
`woow_ai_task_portal_notification`, `woow_ai_task_snailmail`,
`woow_ai_task_document_management`): they hold nothing but their code and
one portal card, so there is nothing to restore. Only a module that depends
on it and must stay installed (for example the PaaS platform, should it
come to depend on it) blocks the uninstall: then forward-fix, or restore
the database and filestore snapshot taken before the upgrade.

`odoo-bin neutralize` (copies for testing): the AI connections lose their
URL and key and are archived, n8n "Safe for Customers" is cleared, push keys
are revoked; each table is checked first (`to_regclass`), so a database where
the module's tables are missing can still be neutralized.

## Protocol and details

- Wire format of both backends, the push API, signed attachment URLs, reply
  rendering, usage: [docs/PROTOCOL.md](docs/PROTOCOL.md).
- Behaviour in detail (Traditional Chinese): [docs/PROJECT.md](docs/PROJECT.md).

## Privacy and security

- Task chats are private group conversations: members read them, only while
  they can read the task now (the internal chat: and its project); so do
  system administrators who can read the task (Odoo's administrator rule on
  channels), without being listed as members. Anyone else reads nothing of a
  chat they are not in (Odoo lets them read a group chat only as members): a
  member who leaves (Odoo's Leave) reads it no more, and their unread
  mentions there are marked read. Notifications (web push, mentions) only
  reach members who can read the task; a mention never adds anyone to a
  chat. Odoo keeps a
  websocket's channels from its last subscribe and its client does not
  re-subscribe when a member is removed, so every bus poll leaves out the
  task chats the polling user cannot read now: a removed member's open page
  (or script) gets no live message of the chat, not even the one whose post
  removed them.
- Connections (URLs, keys) and prompts are for system administrators only;
  keys are never readable and never sent to a browser; push keys are stored
  hashed. Project managers read assistant names only.
- The task summary is read with the asker's own access rights, never sudo;
  the customer chat gets only portal-readable task fields.
- Backend replies are untrusted: HTML is escaped, links are limited to
  http/https/mailto, files are downloaded only from allowlisted hosts with a
  size and time limit (a host that trickles its bytes is cut at the
  deadline), and their type is detected from the content.
- Reading a task chat message or file: Odoo reads messages with sudo
  wherever it does not call `read` (copies, exports through any model, the
  values an onchange computes, delegated fields, template rendering), and
  other models read them as superuser through their related and stored
  computed fields (mail_group's message fields, the full composer's
  parent-derived fields, hr_expense's main-attachment checksum, snailmail's
  letter), in an onchange too. The module's read check sits where all of
  them meet, the read from the database (`_fetch_query`), and it applies as
  superuser too whenever the environment acts for a real user (`env.uid`):
  the content of a task chat message (body, subject, files, recipients,
  author, name...) is read only for someone who can read that chat now (its
  task, and for the internal chat the projects whose internal text it
  used), whatever way its id arrived; so is the content of a task chat file
  (name, data, checksum, size, type, token). What Odoo's record rules and
  access checks read as superuser (where a message or a file belongs, its
  type, who and when) stays readable, and a rule check never brings the
  content into the cache. The full composer refuses a parent message its
  user cannot read. Searches leave those messages out too (Odoo lets their
  authors and notified partners find them, and the Inbox counter drops their
  unread mentions once they lose the chat); so do the failures Odoo shows an
  author. For other people a task chat message's name (shown with sudo
  where a record links it) is "Task AI chat", a task chat file's name is
  "Task AI chat file", and its content read through another model is empty,
  also in a request without a user. Settings > Technical > Emails leaves out
  the mails of messages its reader cannot read. The task chats a user may
  read: for a system administrator a sub-query compiled once per
  transaction that PostgreSQL evaluates once per statement, and only when a
  task chat is met; for anyone else the list of their own member task chats
  they may read, computed once per transaction (one indexed query; again
  after a join, a leave, a group or an access change). Which channels are
  task chats is kept per process for channels that exist only.
- Links, checked on the rows as stored (any id type, any command shape;
  superuser included unless Odoo itself needs it): no message anywhere gets a
  task chat message as its parent unless it is in that chat; a task chat's
  file is never on a message elsewhere, never moved to another document,
  never deleted through a message elsewhere, never put in a letter, and a
  task chat message carries only files of the chat its author uploaded (the
  AI's replies carry the module's files); no sub-channel is made of a task
  chat or from one of its messages; nobody but the module joins a task chat
  (member rows); outside superuser no file is created in a task chat
  directly (a model name given as bytes or as XML-RPC base64, or in a
  default, too), nobody changes or deletes another person's file (system
  administrators excepted), edits or empties another person's message, or
  changes another person's link previews; a message is created in a task
  chat only by posting it there, superuser included (Odoo's own notices
  and the AI's answers post; the mass-mail composer cannot put a message
  there); reactions come only from members who may read the chat; no
  rating is about a task chat or one of its messages, and a post in a task
  chat takes no rating (Odoo's public rating route would post one as
  superuser); outside superuser nobody writes a task chat message directly
  (Odoo pins in SQL and stars on the partner's side); a task chat file is
  never the main attachment of another document (invoices, expenses: Odoo's
  own setter runs as superuser with the record's own files), and a document
  filing rule (woow_document_management) never copies one into a folder,
  for new files nor in its backfill. A mention reaches the members of the
  chat only (and the AI bots): mentioning someone else posts the message
  without notifying them, adds nobody and lets nobody else read it.
- Roles that can read everything anyway: system administrators (code,
  rules), Mail Template Editors (a template may call `.sudo()`); Access
  Rights administrators can grant themselves anything.
- Call logs hold status and timing only, unless *Log Request Content* is on
  for a connection (turning it off erases the stored content).

## Repository

Gitea `woow-paas/Woow_odoo_project_ai` is the source of truth. GitHub
`WOOWTECH/Woow_odoo_project_ai` is a push mirror of its `main` branch and
tags (`.gitea/workflows/mirror-to-github.yml`). Send changes as Gitea pull
requests.

## License

LGPL-3.0 — see [LICENSE](LICENSE).
