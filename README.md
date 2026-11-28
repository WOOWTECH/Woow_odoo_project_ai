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

## Three separate AI products

Since 2026-11-24 discuss AI, project AI and Live Chat AI are three separate
products. Each has its own runtime core and is downloaded and deployed on
its own; they share no code and no data.

| Product | Modules | Repository |
|---|---|---|
| Discuss AI | `woow_ai_contact`, `woow_ai_contact_portal_discuss` | Gitea `woow-paas/Woow_odoo_discuss_ai` (GitHub mirror `WOOWTECH/Woow_odoo_discuss_ai`) |
| Project AI (this repository) | `woow_ai_task` | Gitea `woow-paas/Woow_odoo_project_ai` |
| Live Chat AI | `im_livechat_ai` | GitHub `WOOWTECH/Woow_odoo_ai_livechat` |

This repository's modules (`woow_ai_task`, its five bridges and the CRM template of the AI form consultant):

| Module | Status | What it does |
|---|---|---|
| `woow_ai_task` | 18.0.1.2.0 | Project AI: internal and customer AI chats on every task; the AI form consultant's mechanism |
| `woow_ai_task_portal_discuss` | 18.0.1.0.0 | Bridge, installed automatically with `woow_odoo_portal_discuss`: task chats stay out of `/my/discussions`, its tabs and its card |
| `woow_ai_task_portal_notification` | 18.0.1.0.0 | Bridge, installed automatically with `woow_portal_notification_discuss`: the notification centre's unread conversations count what `/my/discussions` shows; the unread messages of the customer's task AI chats get their own notice and `/my` card, which open the task page |
| `woow_ai_task_snailmail` | 18.0.1.0.0 | Bridge, installed automatically with `snailmail` (which Odoo installs with `mail`): a file of a task AI chat is never put in a letter |
| `woow_ai_task_document_management` | 18.0.1.0.0 | Bridge, installed automatically with `woow_document_management`: a filing rule never copies a file of a task AI chat into a document folder |
| `woow_ai_task_paas` | 18.0.1.0.0 | Bridge, installed automatically with the PaaS platform (`woow_paas_platform`): the customer AI chat on the `/woow` task page, the platform's old task AI switched off (not deleted), task chat members following workspace membership and service bindings, the project AI settings for project managers only ([docs/PAAS.md](docs/PAAS.md)) |
| `woow_ai_task_crm` | 18.0.1.0.0 | AI form consultant for CRM leads (a template): installed only on purpose, switched off until a system administrator enables it |

## Alignment (contract v1.3)

Discuss AI, Project AI and Live Chat AI are configured and run the same
way through one written contract: connection and assistant fields, roles
and menus, how a turn runs, the n8n and LiteLLM wire formats, what people
see, logs and usage, `neutralize.sql`, the zh_TW glossary and the upgrade
rules. **This release (18.0.1.2.0) implements contract v1.3.** Its package
is in this repository, copied byte for byte: `docs/CONTRACT.md` and
`woow_ai_task/tests/contract/` (never edited here; a new contract version
replaces both). `woow_ai_task/tests/test_contract.py` runs its fifteen
checks against this module (`--test-tags woow_ai_contract`). Project AI's
values (contract 3.1): LiteLLM answers are streamed, the system message
has an identity block, and the person's text carries the system's speaker
label: `[<name>] ` in the internal chat, where every speaker is an
employee, and `[客戶 <name>] ` / `[負責同事 <name>] ` / `[其他 <name>] ` (a
person who is neither a colleague nor a portal user) in the customer chat (a
name with an `@` counts as no name: `[客戶] `, or no label in the internal
chat).

What contract v1.3 added, after a test bench found three defects on
2026-11-28 (a file the model could not read broke every later turn of a
chat, a model told a customer the shop was open on a Saturday, a Live Chat
answer started with the operator's label):

- **The current time.** Every request carries it, computed when the
  request is built: n8n gets `now` (`{utc, local, tz, weekday}`), LiteLLM
  one system line after the identity block,
  `Current time: 2026-11-28 10:20 (Saturday), time zone Asia/Taipei (UTC+08:00).`
  The time zone is the first one set of: the company's working schedule's
  (the schedule of the company that runs the replies, the database's main
  company: Settings > Technical > Resource > Working Schedules in developer
  mode, or Employees > Configuration > Working Schedules where Employees is
  installed), the time zone of the company's contact (no standard form
  shows it), the asker's own, UTC. A value that is not a known time zone
  counts as none. A working schedule's time zone is a required field, so
  where the company has a schedule (Odoo gives every company one) it wins
  over the asker's; a new database's default schedule says UTC: set it to
  the time zone of the business (see *Upgrading*).
- **Labels in answers.** A model sees the speaker labels of the history and
  may start its answer with one. Before an answer is posted, every label
  at its start goes, with the white space around it (any white space,
  no-break and zero-width spaces included): `[客戶 …]`, `[負責同事 …]`,
  `[真人客服 …]` (or the kind alone) in every chat, `[其他 …]` in the
  customer chat, and in the internal chat `[<name>]` when it is the label
  of a person of that chat (a member, or the author of a person's message
  in the history); the same for a text an n8n workflow pushes. So an AI
  answer never shows up in the customer chat as
  「[負責同事 王小明] 好的，我們同意退款」. What stays: text in brackets
  elsewhere, a leading bracket that is no such label (「[注意] 截止日改了」),
  a label directly followed by `(` (a Markdown link: `[王小明](https://…)`),
  and, in the internal chat, an answer where a later line also starts with
  a person's label (a list of who does what). An answer that held only
  labels is an empty answer: the person gets the error message; a pushed
  text of labels only is refused like an empty one.
- **When the model cannot read a file.** LiteLLM only: when a request that
  carries files (inline images, documents sent as files) gets HTTP 400, 413,
  415, 422 or 500, the same turn is sent again at once (no wait, not
  counted as a retry, within the same timeout, never with less than one
  second left): first without the files of earlier messages, then, when the
  question's own files are left, without any. Before each such request the
  checks of a reply run again (the assistant, the connection's readiness,
  the asker's access, the pause): when one fails, nothing more is sent. Each
  request is built again (its own current time) with the same files minus
  the ones it leaves out; a file left out is its `[附件]` download line
  followed by `[附件說明] <name>：模型無法讀取這個檔案，只附下載連結。` Each request
  has its own call log row. When an answer comes that way, the chat
  remembers those files (*Files Sent as Links Only* on its reply jobs):
  every later request of that chat sends them, where they would be files of
  the request, as their download line and the note, so no later turn fails
  first. When no request succeeds, the person gets the error message as
  before and nothing new is remembered. The memory lasts as long as the
  chat: changing the connection or the model does not clear it, and the
  daily cleanup of jobs keeps the newest job that carries it. A system
  administrator clears it: *Reply Jobs*, filter *Files Sent as Links Only*,
  open a job of that chat, *Forget Files Sent as Links* (for example after
  switching to a model that reads those files). n8n is unchanged (it gets
  links).

## Features

- **AI connections** (system administrators): an n8n webhook (requests
  optionally signed with HMAC-SHA256) or a LiteLLM server (OpenAI-compatible
  chat completions; Odoo sends the conversation and never any tool, so these
  are chat-only). Write-only key, model (required for LiteLLM: it picks the
  deployment by its name), timeouts, retries, streaming, documents, files in
  replies, content logging, a readiness check and *Test Connection*. **Safe for Customers**: always for LiteLLM; for n8n only once
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
- **AI form consultant** (「詢問 AI 顧問」, off by default): staff with Project
  rights press *Ask the AI Consultant* on a record of another module (a CRM
  lead with `woow_ai_task_crm`; PaaS cloud services later, through the PaaS
  bridge) and land in the internal AI chat of a consultation task, in a chat
  window; *New Topic* always starts a new task. The AI gets a capped summary
  of the fields the consultant module lists, read with the asker's own
  rights, as data, and only reads; long free texts (a lead's Notes) only
  while the setting sends them, and e-mail addresses, phone numbers and IP
  addresses in the free text sent are masked by patterns (not a guarantee).
  Every record sent to a chat's
  AI is recorded; from then on only people who can read every one of them
  (and the task's current consulted records) use that chat. One *AI
  Consultant Settings* row per kind of record (system administrators): its
  consultation project, a role prompt, where the data goes (confirmed by a
  system administrator, and paused when it changes), warnings, and a
  preview of how many task AI chats each person would leave (by name)
  before it is enabled.
- **What the AI gets**: the assistant's prompt, the project text of its tab
  (the customer chat never gets the internal one; the internal one only
  while every person of the chat can read the project, as the internal chat
  requires) and a short task summary read with the asker's own rights
  (customer chat: the portal-readable task fields, without related tasks;
  internal chat: related records every member can read); never the chatter.
  Instructions come first, then one data section between two markers tagged
  with 8 random characters per request; every value of it is cleaned, so no
  value can close it. Then the identity block and the current time (the
  time zone of the company's working schedule, else of the company's
  contact, else the asker's, else UTC). The customer chat takes no log note
  (its customers read everything in it).
- **Durable reply queue** in the cron process, typing indicator, files both
  ways (images inline, documents as file parts or signed links; the files of
  one answer: 45 seconds and 100 MB together, a failed download stays a
  link), Markdown replies, push API for n8n "skip, then push", call logs
  (30 days) and a permanent monthly usage per project and tab (a reference
  for invoicing by hand, no billing: limits live in the LiteLLM keys).

## Install

1. Put this whole repository on the addons path: the five bridges must be
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

The install runs `ANALYZE discuss_channel` (the new task link column gets
its statistics at once: PostgreSQL plans the channel rule with them).

**Large databases (PostgreSQL JIT).** For system administrators the task
chat rule keeps an arm for the chats they are not a member of, and
PostgreSQL adds its whole estimated cost to every Discuss statement of
theirs (it runs only when a statement meets a task chat they are not a
member of). That estimate grows with the tasks that have a task chat (one
or two chats each): measured with column statistics on Odoo's own task
rules, about 17.9 per such task for an administrator who is also a Project
administrator (93,959 at 5,250 tasks; 281,699 at 15,750) and 13.6 for one
who is not. It passes `jit_above_cost` (100,000 by default) from about
5,600 such tasks (7,400 for the second kind). Other task rules (the PaaS
platform's own) give other figures, so read the estimate on the database
itself, in `odoo shell`:

```python
from odoo.tools import SQL
query = env['discuss.channel'].with_user(env.ref('base.user_admin'))._search([])
env.cr.execute(SQL('EXPLAIN %s', query.select()))
print(env.cr.fetchone()[0])  # cost=<startup>..<total>: compare <total> with jit_above_cost
```

Above the threshold PostgreSQL compiles those statements with JIT, about
60 ms each: at 15,750 tasks an administrator's `/mail/data` took 226 ms
(36 ms with JIT off) and opening a chat 164 ms (49 ms). Other users stay
far below (5,240 at most at 15,750 tasks; no JIT). When the
administrator's estimate is above `jit_above_cost`, turn JIT off for that
database, as the database owner: `ALTER DATABASE <db> SET jit = off;` (new
connections take it: restart Odoo). The module never changes it.

## Upgrading

The first release (18.0.1.0.0) needed no migration, and neither does
18.0.1.1.0 (below). What can keep an assistant from answering, and what to
check first (the *Readiness* column of *AI Connections* and the project's
*AI Assistants* page say why):

- a LiteLLM connection without a model name is not ready (LiteLLM picks the
  deployment by it);
- a connection without an endpoint URL, or a LiteLLM one without a key;
- the customer chat needs a connection that is safe for customers;
- the cron needs the time above (otherwise replies run in a web worker's
  thread, slower but answered).

Translations: the module's zh_TW strings are loaded at install; an
administrator's own translations of this module's strings are overwritten
when it is upgraded.

18.0.1.1.0 adds the AI form consultant, switched off: from 18.0.1.0.0,
`-u woow_ai_task` adds three tables (one of them is the impact preview's
transient model) and one task column, and nothing stops answering; every
value of the task summary the AI gets is now cleaned like the consulted
records' (see *AI form consultant* below for that, for enabling it and for
the rollback).

18.0.1.2.0 (contract v1.3, above) needs no migration either: from
18.0.1.1.0, `-u woow_ai_task` adds one column to the reply jobs (*Files Sent
as Links Only*, empty on the existing jobs) and shows it on the job form
with its *Forget Files Sent as Links* button (system administrators);
nothing stops answering. What changes for the people:

- every request carries the current time in the time zone of the company's
  working schedule (above). Check it first, with this read-only query (a
  new database's default schedule says UTC; set the schedule's *Timezone*
  to the business's own):

  ```sql
  SELECT c.id, c.name, rc.tz AS schedule_tz, p.tz AS partner_tz
    FROM res_company c
    LEFT JOIN resource_calendar rc ON rc.id = c.resource_calendar_id
    JOIN res_partner p ON p.id = c.partner_id;
  ```

  `schedule_tz` wins; `partner_tz` counts only for a company without a
  schedule, and the asker's own time zone only when neither is set;
- an answer that starts with a speaker label loses it (an answer of labels
  only now gets the error message instead of being posted);
- a LiteLLM turn whose file the model refuses is answered without the file
  (more call log rows with `http_error` 400, 413, 415, 422 or 500, each
  followed by the request without the file), and the chat keeps sending
  that file as a link until a system administrator clears it (above).

No other module of this repository changes: the bridges and the CRM
template keep their versions. Rollback: the 18.0.1.1.0 image with the
database snapshot taken before the upgrade, or the 18.0.1.1.0 image with
`-u woow_ai_task` (Odoo drops the new column, and what the chats remembered
with it, and loads the old job form again).

### Cron workers next to Discuss AI

Each AI module (this one and `woow_ai_contact`) has a priority-1 dispatcher
cron that keeps one cron worker for its pass: up to its window, plus the
longest connection timeout, plus 60 s.

- Plan one cron thread per AI dispatcher plus one for every other cron:
  `max_cron_threads` >= 3 with both modules on one database.
- When the AI dispatchers are as many as the cron threads (for example
  2 and 2), both use short passes (a 60 s window, a 5 s wait), so the other
  crons (deploys, the e-mail queue) get a turn between passes.
- With a single cron thread, `limit_time_real_cron` must cover both passes
  (Discuss AI's runs first): otherwise this module's replies run in thread
  mode. A pass that finds too little time left starts no reply and runs
  again shortly.
- A threaded cron process needs `db_maxconn` >= both modules' concurrency + 6.

### On the PaaS platform

Installing `woow_ai_task` where `woow_paas_platform` is installed brings the
bridge `woow_ai_task_paas` in the same operation; nothing else is needed. It
puts the customer AI chat on the `/woow` task page (the internal chat stays
in the backend task form), switches the platform's old task AI off without
deleting anything, keeps the task chat members in line with the PaaS access
changes, and lets only project managers and administrators set a project's
AI assistants and texts. The `/woow` Projects area shows only where the
platform's `Show Projects (/woow)` setting is on (the bridge never changes
it); elsewhere customers use the customer AI chat on the `/my/tasks/<id>`
page. Who may use a task's customer AI chat follows who may read the task
(the backend's visibility as it is today: project managers and project
users read the tasks of every workspace's portal projects); limiting it to
the members of the service's workspace is planned for a later version.
Details, the old task AI's switch, the platform contract and rollback:
[docs/PAAS.md](docs/PAAS.md).

## Once installed: keep the modules in every image

The module adds a **global record rule on `discuss.channel`** whose domain
uses one of its computed fields, plus views and crons. The rule applies
only where the module's code is loaded (its domain checks that the
module's field is in the registry). Without that check, every
non-superuser channel access raised on the unknown field (Discuss, Live
Chat and portal chats site-wide) wherever the rule's row is in the
database but the code is not loaded: in an image that lacks the module (a
rollback image), and while the modules that load before it run (Odoo runs
their at_install tests during a registry load; a migration script that
acts as a user). Now the rule is empty there, and silently: the task chats
keep only Odoo's own channel rules (members, and system administrators),
without the task check, the membership sync and the read check of this
module. The module's three crons fail in such an image; Odoo switches a
cron off after 5 failures over at least 7 days, and (their data is
noupdate) a later roll-forward does not switch them on again: do it by
hand (Settings > Technical > Scheduled Actions). So every image that
serves the database must still contain `woow_ai_task` as long as it is
installed.

The document bridge extends two private methods of
`woow_document_management` (18.0.2.0.0):
`_woow_auto_file_checked(attachments, unfiled)` and
`_woow_backfill_domain()`, and passes through any argument they gain. It
refuses to load when either is gone or can no longer be called the way
`woow_document_management` calls it (the registry does not start; a
compatible change, such as an added optional parameter or another module's
`*args, **kwargs` wrapper, passes). Release tooling must start a registry
with both modules installed (for example `odoo -d <tmp> -i
woow_document_management,woow_ai_task --stop-after-init`: the bridge
installs itself), so an incompatible release of either fails there, before
it reaches an image.

The notification-centre bridge (`woow_ai_task_portal_notification`)
extends `woow_portal_notification`'s private
`res.users._wpn_public_message_domain`. If a release renames it, the
registry load logs it as CRITICAL (never a refusal: the centre is a page,
not a reason to stop the database) and customers who lost a chat they were
mentioned in may get an error page on `/my/notifications` again: update
the bridge with `woow_portal_notification`. The centre's fix needs both
`woow_portal_notification` and the `/my` Discussions bridge
(`woow_odoo_portal_discuss` with `woow_portal_notification_discuss`), as
the bridge depends on them.

The PaaS bridge keeps nothing in the database that needs its own code
(no view, field, rule, filter, action, cron or asset: its form changes are
made in code, and only its own code reads its system parameters). Odoo
started by hand without it serves the database, but the platform's old
task AI is held off no more. The PaaS platform's own deployments do not get
that far: they refuse to start an image that lacks the code of any
installed module, so the site stays down until an image that carries the
modules is used again. Once project AI is installed there, an image without
the six modules is therefore not a rollback target; before installing,
make sure that the image running now and the image a rollback would go
back to both carry the six modules. Details: [docs/PAAS.md](docs/PAAS.md),
"回復用的映像".

Ship the modules together in every image that serves the database
(rollback images included): the six (`woow_ai_task` and its five bridges)
where the PaaS platform is installed, the other five elsewhere (the PaaS
bridge has no use there: it installs only with the platform). A module of
this repository that a database installs later, such as the CRM template
(`woow_ai_task_crm`, never installed automatically), is then in every image
that serves that database too. Without the PaaS bridge the platform's old
task AI would answer again. Build images from a
release tag `v<woow_ai_task version>` (`-N` when only bridges change), never
from an untagged merge.

Before installing the CRM template, upgrade `woow_ai_task` to 18.0.1.1.0
then run `-i woow_ai_task_crm`, or use both in one command
(`-u woow_ai_task -i woow_ai_task_crm`). An earlier standalone install is
refused, but only after Odoo installs its dependencies (including CRM if
it was absent). Once the CRM template is installed, keep its source in
every serving and rollback image too: seven modules on PaaS, six elsewhere.
Odoo
installs a bridge by itself only while one of its dependencies is being
installed; `-u`, `-u all` and *Update Apps List* never install a new module.
So when a bridge reaches a database after `woow_ai_task` and the portal
module it bridges are both installed, install it explicitly (it is
harmless): `-i woow_ai_task_portal_discuss` where `woow_odoo_portal_discuss`
is installed, `-i woow_ai_task_portal_notification` where
`woow_portal_notification_discuss` is, `-i woow_ai_task_snailmail` where
`snailmail` is, `-i woow_ai_task_document_management` where
`woow_document_management` is, `-i woow_ai_task_paas` where
`woow_paas_platform` is (or from Apps). Never install a bridge where its
other side is missing: that would install the other side too.

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
first) and only after that its fields and tables: the AI connections with
their keys, the assistants, reply jobs, call logs and monthly usage are
gone (back them up first if needed). The archived chats keep their
messages but lose their task link (Odoo drops the module's columns):
installing the module again starts new, empty chats. The people leave all
the chats in two statements: with 10,000 task chats and 30,030 people in
them the hook took 3.5 s and the whole uninstall 6.5 s (member by member,
as before, 371 s: longer than a web request may run). Uninstalling it also
uninstalls its five bridges (`woow_ai_task_portal_discuss`,
`woow_ai_task_portal_notification`, `woow_ai_task_snailmail`,
`woow_ai_task_document_management`, `woow_ai_task_paas`); the PaaS platform
stays installed. The first four hold nothing but their code and one portal
card, so there is nothing to restore. The CRM template is also uninstalled:
its task-to-lead links go with it, while consultant settings and records-sent
rows go with `woow_ai_task`. Only a module that depends on it and
must stay installed (for example the PaaS platform, should it come to
depend on it) blocks the uninstall: then forward-fix, or restore the
database and filestore snapshot taken before the upgrade. On the PaaS
platform this is not a routine rollback step (forward-fix, or an image that
still carries the six modules, or the snapshot taken before the install:
docs/PAAS.md), and `woow_ai_task_paas` can never be uninstalled alone
(refused: the old task AI would come back).

`odoo-bin neutralize` (copies for testing): the AI connections lose their
URL and key and are archived, n8n "Safe for Customers" is cleared, push keys
are revoked; each table is checked first (`to_regclass`), so a database where
the module's tables are missing can still be neutralized.

## AI form consultant (optional)

- **Off by default.** Until a system administrator enables a kind of record
  in *AI Consultant Settings* (Project > Configuration > Project AI), nothing
  changes in membership, reading, buttons, reply links or downloads. The
  upgrade to 18.0.1.1.0 itself changes the system prompt's data section
  only: its framing, and every value of the task summary is now cleaned like
  the consulted records' (NFKC, so full-width punctuation and digits reach
  the AI as ASCII; control and format characters dropped; runs of three or
  more angle brackets collapsed).
- **CRM** (`woow_ai_task_crm`): install it on purpose, from the command line
  (`-i woow_ai_task_crm`) or when no cron is running, after `-u woow_ai_task`
  has finished, or together with it (`-u woow_ai_task -i woow_ai_task_crm`).
  Run alone before the update it refuses, but only once Odoo has installed
  its dependencies: on a database without `crm`, CRM is then installed
  anyway (with the effect on the Sales rights described below). It installs
  `crm` too, with
  Odoo's own CRM bridges of the apps already there (`website_crm`,
  `crm_livechat`, …). Once installed, keep it in every image that serves the
  database. Its setting is created switched off at install (a setting for
  leads that exists already, such as one an administrator made, is adopted
  and switched off). **Where `crm` is not installed yet, installing it can
  make every existing employee a Sales Administrator** (who reads every
  lead): while *Default Access Rights* (Settings > Users; the system
  parameter `base_setup.default_user_rights`) is on, which is Odoo's default
  on a database created on Odoo 17 or 18, Odoo gives every group added to
  the *Default User Template* to all existing internal users, and the Sales
  app adds that group to the template. Check first whether `crm` is
  installed and whether Default Access Rights is on (Settings > Users, or
  read the parameter in Settings > Technical > System Parameters); if `crm`
  comes with this module, review the Sales rights before enabling the
  consultant (otherwise everyone reads every lead). The setting's warnings
  count the employees who read every lead.
- **Enabling** (once per site, by a system administrator, with the site
  owner's agreement): choose the consultation project (its internal AI
  assistant answers), read the warnings and *Data Goes To*, check the AI
  behind that project and tick *This connection has no tools that read
  company data* (required), decide on *Send the Notes* (off by default),
  give the salespeople who will use it the Project user rights, press
  *Preview the Impact* (how many task AI chats each person would leave, by
  name), then tick *Enable the AI Consultant*. Check *Default Access Rights*
  and who is a Project Administrator first: the warnings count them.
- **Task names carry the record's name**, e.g. 「AI 顧問：大安區廚房翻新」 (read
  with the asker's own rights, cleaned, contact details masked, at most 80
  characters; set once, a later rename of the record does not rename the
  task). Everyone who can read the consultation project sees them,
  colleagues who cannot read the record included, and names typed by
  website visitors (a web form or Live Chat lead) appear: an accepted
  trade-off. Project Administrators read every task whatever the project's
  visibility (Odoo's own rules), and while *Default Access Rights* is on,
  Odoo's *Default User Template* makes new employees Project Administrators
  (the Project app adds that group to it). To keep the names from some
  employees, remove the Project Administrator group from them; an
  invited-users consultation project then also hides them from the other
  Project users. Deleting a record renames its consultation tasks (and the
  names stored on their messages and activities) to 「AI 顧問：<kind> #<id>」;
  merging leads renames the tasks of the merged ones after the lead that
  stays. The tracking lines of renames people made by hand are Odoo's
  history and stay.
- **Notes and other free text**: long free texts (a lead's Notes) go to the
  AI only while *Send the Notes* is on (off by default). They hold whatever
  was typed: on Live Chat leads (`/lead`, the chatbot) the whole
  conversation, the visitor's e-mail and phone first; on web-form leads the
  form's extra fields, and the visitor's IP address and browser when the
  form's metadata is on. In every free text sent (the record's name, a lead's
  company name, campaign, source and medium names too), e-mail addresses,
  phone numbers and IP addresses (IPv4, IPv6) are masked (`[email]`,
  `[phone]`, `[IP]`), also right next to Chinese words. A phone number is a
  run of 8 to 15 digits that starts with 0, `+` or `(` or has spaces,
  dashes, dots or parentheses in it; a plain run such as an amount or a tax
  number (`20000000`), an amount with decimals and year-first or year-last
  dates stay. Spaced amounts (`12 345 678`) and hyphenated references
  (`PO-2026-1125-0042`) can still look like phone numbers and be masked.
  E-mail matching covers ASCII and accented Latin letters; other scripts
  and obfuscated addresses can be missed. IPv6 matching covers full and
  compressed forms with a group before `::`; `::1` is left alone. Names and
  postal addresses are not masked: pattern based, not a guarantee. Switching
  the Notes or the setting off applies to the next question, also in a
  running AI worker (both are read from the settings at each request).
- **Multi-company**: the consultation project's company must be one of the
  allowed companies of every person who will use the consultant (otherwise
  they are told to ask a project manager). With *Project From the Record* the
  setting cannot show the warnings that depend on one project.
- **Who may use a consultation chat**: people who can read the task, its
  project (internal chat) and every record the chat's AI got or the task now
  links (records of enabled kinds). A reassigned, merged or deleted record
  updates the members at once; a record-rule change, at the hourly sweep
  (reading is cut at once anyway). A deleted record closes its chats for
  everyone; their history stays. The record a button press is about is
  recorded for the chat at once (not only when its AI gets it), so unlinking
  or deleting it, or disabling the setting, never opens the chat.
- **Where the data goes**: a record's data goes only to the AI of its
  consultation project (the one *Data Goes To* shows), or of the project the
  record itself chooses; a task linked to it in another project (created or
  moved there) gets none, while its links still decide who may use its chats.
  A system administrator confirms it: saving a tick in *This connection has
  no tools that read company data* (required to enable) records where the
  data goes at that moment (check *Data Goes To* again after saving): the
  project's internal assistant, its connection and kind, the backend host
  and port, and a hash of the n8n workflow URL or of the LiteLLM model (no
  URL, model or key is stored). When any of them changes (a project manager
  chooses another internal assistant; an administrator points the connection
  at another workflow, host or model), the box clears, the setting shows a
  red banner, the buttons refuse and the consulted-record section is omitted
  until a system administrator checks the new AI and ticks it again. Existing
  chats still send their ordinary task summary and history, including the
  task names that carry the masked record names. A new key alone does not
  pause it. Only system
  administrators set the links, on create too. A setting that was ever
  enabled cannot be deleted, only disabled.
- **n8n and LiteLLM**: when the internal AI connection is n8n, make sure the
  workflow keeps no memory across conversations and never lets the AI decide
  to fetch URLs; n8n stores the data of every execution, the record summaries
  included. With *Log Request Content* on, the call logs keep the whole
  request (system administrators, 30 days). Odoo sends LiteLLM no tools, but
  an n8n workflow tool, or a LiteLLM proxy or model with tools of its own,
  that reads Odoo or other company data with credentials of its own is not
  limited to what the asker may read, and the records' text, which visitors
  may have typed, can steer it: the consultation project's AI must have none
  (that is what the box above confirms).
- **Consultant modules** (bridges): `readable_query` answers must be
  uncorrelated SQL without NULLs (a NULL never makes a record readable, the
  rule embeds `NOT COALESCE(x IN R, FALSE)`); `candidates` always arrives as an
  SQL sub-select; a module that overrides the values hook instead of declaring
  customer fields overrides the tabs hook too. The record's name is read as
  the asker (Odoo's own rules): the CRM template and the PaaS bridge give
  their records to the internal tab only, whose askers are employees, so
  they never need a portal asker to read their models. See
  「穩定介面（橋接 API）」.
- **Cost**: the gate's SQL grows with the consulted chats. A member list or a
  point check compiles its terms for the chats it asks about only (their
  records-sent rows, the records linked to their tasks); a system
  administrator's compiled rule takes every records-sent row and every
  linked record. Measured with 2,100 task chats (median of 20 calls per
  route, the same query counts, no JIT): a Project user's SQL time per route
  +2 to +5 ms with 100 consulted chats, +5 to +8.5 ms with 1,000; a system
  administrator's +3 to +7.5 and +6.5 to +15.5 ms (docs/PROJECT.md 「效能」).
- **Reinstalling the CRM template**: installing it adopts the setting for
  leads that exists (an administrator's own setting survives an uninstall of
  the template) and switches it off; without one it creates one, switched
  off.
- **Upgrade and rollback**: `-u woow_ai_task` from 18.0.1.0.0 adds three
  tables (one is the impact preview's transient model), one task column (no
  table rewrite), views, a menu and access rows; nothing is removed, no
  migration script. The 18.0.1.1.0 image may serve the database only once
  `-u woow_ai_task` has finished (a task read needs the new column). Roll
  back by restoring the database and filestore snapshot; never with an older
  image and `-u` (it drops the ledger, the settings and the column). An image
  older than 18.0.1.1.0 has no consultant gate: colleagues who cannot read a
  consulted record could read and join those chats again, so once a kind of
  record was enabled, roll back only by restoring the snapshot. To take the
  CRM template away, uninstall `woow_ai_task_crm` on an image that has it:
  the records already sent keep deciding through Odoo's own lead rules.
- **An image without the code still opens tasks and leads**: the task form's
  「AI 顧問對象」 page, the lead form's buttons and smart button and the
  task's lead field are added in Python (`_get_view`), not by stored views,
  so an older release or a rollback image that lacks the consultant (or the
  CRM template) shows those forms without them instead of failing on unknown
  fields. The settings menu stays visible to system administrators there
  (its access rows stay in the database): it opens an error, and every fill
  of the menu cache logs one `Missing model woow.project.ai.consult.profile`
  ERROR. The consultant's gate and data need its code, like the rest of
  Project AI: keep the modules in every image that serves the database.

Details (Traditional Chinese): [docs/PROJECT.md](docs/PROJECT.md) 「AI 顧問」; the
API for consultant modules: 「穩定介面（橋接 API）」 there.

## Protocol and details

- Wire format of both backends, the push API, signed attachment URLs, reply
  rendering, usage: [docs/PROTOCOL.md](docs/PROTOCOL.md).
- Behaviour in detail (Traditional Chinese): [docs/PROJECT.md](docs/PROJECT.md).
- On the PaaS platform (Traditional Chinese): [docs/PAAS.md](docs/PAAS.md).

## Privacy and security

- AI form consultant: a record's data reaches the AI only inside the cleaned,
  tagged data section, read with the asker's own rights, only the fields its
  consultant module lists (never the contact fields such as e-mail, phone,
  address and contact name, nor the chatter and its log notes, files or
  activities), names of related records only those every other person of the
  chat can read. Long free texts (a lead's Notes, which can hold a whole Live
  Chat conversation) go only while the setting sends them; e-mail addresses,
  phone numbers and IP addresses in the free text sent are masked by
  patterns (not a guarantee), names and postal addresses are not. The AI
  only reads. Links in its replies
  stay clickable, and files are downloaded, only for this site's hosts and
  the hosts the consulted records allow (and, for files, the connection's
  download domains too).

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
- Odoo's post route (`/mail/message/post`) applies the client's `context`
  to the request. In a task chat its `default_*` keys reach nothing the
  post creates, and the rows the module creates in a client's request (the
  reply job, the colleagues' To-Do, the AI's own posts and files, call
  logs) are made from the language and time zone only: a client cannot
  queue a job that looks running or used up, or move, hide or attach files
  to a To-Do.
- Connections (URLs, keys) and prompts are for system administrators only;
  keys are never readable and never sent to a browser; push keys are stored
  hashed. Project managers read assistant names only.
- The task summary is read with the asker's own access rights, never sudo;
  the customer chat gets only portal-readable task fields.
- Backend replies are untrusted: HTML is escaped, links are limited to
  http/https/mailto, files are downloaded only from allowlisted hosts (over
  https; plain http only from an allowlisted `localhost` or
  `*.svc.cluster.local` host) with a size and time limit (a host that
  trickles its bytes is cut at the deadline), and their type is detected
  from the content. A URL whose host the module and the HTTP client could
  read differently (a backslash, a user name before the host, a host
  `urlsplit` and requests parse apart) is never linked, never downloaded
  (at every redirect too) and never accepted as an endpoint URL.
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
  author and the delivery status notices the mail queue sends them, and the
  portal notification centre (`woow_portal_notification`, through the
  notification bridge: a customer mentioned in a chat they later left or
  lost; the bridge comes with `woow_portal_notification_discuss`, which needs
  `woow_odoo_portal_discuss`: where the centre is installed, install both). The lists of messages (Inbox, History, Starred, Settings >
  Messages) show a system administrator the task chats they are a member
  of; they open any other chat they may read from its task. Deleting a task
  (its active subtasks with it) or a project deletes their AI conversations,
  messages and files included, whoever deletes it (Odoo's cascade reads the
  deleted messages and files as superuser for that person: not checked
  there). For other people a task chat message's name (shown with sudo
  where a record links it) is "Task AI chat", a task chat file's name is
  "Task AI chat file", and its content read through another model is empty,
  also in a request without a user. Settings > Technical > Emails leaves out
  the mails of messages its reader cannot read. The task chats a user may
  read: the list of their own member task chats they may read, computed
  once per transaction (one indexed query, one more when they are in task
  chats; again after a join, a leave, a group or an access change); for a
  system administrator (who reads chats without being a member) also, after
  that list, a sub-query compiled once per transaction that PostgreSQL
  evaluates once per statement, and only when it meets a task chat not in
  the list (in the channel rule and the Emails list; the lists of messages
  use the list alone, and a check of a few chats is compiled for those
  chats only). Which channels are task chats is kept per process for channels
  that exist only.
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
- Known limits: the read check trusts the superuser account itself (crons,
  the reply runner, the signed download routes), so Odoo routes that switch
  a user's request to it are not checked: `/portal/attachment/remove`
  answers an employee the name of any file in its error (as it does for
  every file of the database), and the HTML editor's image change
  (`/html_editor/modify_image`) can add a picture to a task chat its user
  may write in (in an archived task's chat too). Translations of task chat
  messages (Discuss's *Translate*) are stored by Odoo and readable by system
  administrators.
- Call logs hold status and timing only, unless *Log Request Content* is on
  for a connection (turning it off erases the stored content).
- The current time a request carries names a time zone (the company's, else
  the asker's) and is never put in the LiteLLM `metadata`. When the model
  refuses a file, the next requests send its signed download link instead
  (valid one hour, as for every link); the reply job keeps the attachment
  ids only.

## Repository

Gitea `woow-paas/Woow_odoo_project_ai` is the source of truth. GitHub
`WOOWTECH/Woow_odoo_project_ai` is a push mirror of its `main` branch and
tags (`.gitea/workflows/mirror-to-github.yml`). Send changes as Gitea pull
requests.

## License

LGPL-3.0 — see [LICENSE](LICENSE).
