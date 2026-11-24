# Backend protocol (project AI)

How `woow_ai_task` talks to an AI backend, and how a backend talks back
(push API). Placeholders only: `odoo.example.com`, `llm.example.com`,
`n8n.example.com`, `sk-REPLACE-ME`, `whsec-REPLACE-ME`, `wpai_REPLACE-ME`.

The module has its own runtime core. It shares no model, route, cron or key
with the Discuss AI module (`woow_ai_contact`); the wire format follows the
same conventions, so one n8n workflow can serve both and tell them apart by
`surface`.

Each **AI assistant** (`woow.project.ai.assistant`) uses one **AI connection**
(`woow.project.ai.connection`, Project > Configuration > Project AI > AI
Connections, system administrators only). A connection has one backend:

| `backend_type` | Endpoint URL | Backend |
|---|---|---|
| `openai` (shown as *LiteLLM*) | base URL ending in `/v1`; `/chat/completions` is appended | LiteLLM (any OpenAI compatible chat-completions server). Chat-only: Odoo never sends tools |
| `n8n` | full production webhook URL (a Chat Trigger: `<n8n>/webhook/<webhookId>/chat`) | an n8n workflow (Webhook node or Chat Trigger) |

The endpoint must be `https://`; plain `http://` only for `localhost`,
`127.0.0.1`, `::1` and hosts ending in `.svc.cluster.local`.

**Safe for Customers** (`customer_safe`): only connections with it answer the
customer AI chats. LiteLLM connections always have it (chat-only). An n8n
connection gets it only when a system administrator ticks it, after checking
that the workflow looks up no company data, calls no tools and keeps no
memory; it is cleared when the endpoint URL or the backend type changes. It
is checked when a project chooses its customer assistant and again before
every answer and every push.

## Keys

| Secret | Direction | Where | Use |
|---|---|---|---|
| API key (LiteLLM) | Odoo → backend | AI connection, *API Key* | `Authorization: Bearer <key>`; a LiteLLM virtual key carries the limits and budgets |
| Signing secret (n8n, optional) | Odoo → n8n | AI connection, *Signing Secret* | HMAC key of `X-Woow-Signature`, **never sent**; empty: requests are not signed |
| Push key | backend → Odoo | AI assistant, *Generate Push Key* (shown once, `wpai_…`) | `Authorization: Bearer wpai_…` on `POST /woow_project_ai/v1/push` |

Keys are write-only (not readable through any form, export or RPC). Push
keys are stored as SHA-256 only. The Discuss AI module's keys (`wpk_…`) are
not accepted here, and project AI keys are not accepted there.

## Common context

Every call is about one task AI chat (a private Discuss group conversation
of one task and one tab) and carries:

- **history**: the most recent `max_history` comment messages (default 50),
  oldest first, up to the message being answered (plus the assistant's own
  later answers to other people). The AI's messages are `assistant`, people's
  `user`. Each person's turn starts with a label set by the system:
  - internal chat: `[<name>] <text>`;
  - customer chat: `[客戶 <name>] ` for a portal user, `[負責同事 <name>] ` for an
    employee, `[其他 <name>] ` otherwise. Only the label's first word tells
    the kind of account; the system prompt says so.
  Names: `[` `]` become `(` `)`, at most 80 characters, never an email. Text
  is cut at 16,000 characters (`…[truncated]`).
- **system prompt**: the assistant's prompt, the project text of this tab
  (`專案說明（專案經理提供）：` + text; the customer chat never gets the internal
  one), a task summary read with the asker's own access rights (internal
  chat: name, description as text, stage, deadline, assignees, tags,
  priority, parent and subtask names; customer chat: those of them in Odoo's
  portal-readable field set, without the related tasks; never the chatter;
  at most 8,000 characters), and in the customer chat the notes about the
  labels. LiteLLM gets it as the `system` message (then the identity block);
  n8n as `system_prompt`.
- **session id**: `woow-project-ai-<first 8 hex of the database uuid>-<channel id>`.
- **surface**: `{"type": "project_task", "project_id": 7, "task_id": 42, "tab": "internal" | "customer"}`
  (n8n `surface`; LiteLLM `metadata.surface_*` string keys).
- **identity** of the person who asked (the author of the message being
  answered; no login is delegated):

```json
{"user_id": 7, "partner_id": 15, "name": "王小明", "email": "user@example.com",
 "lang": "zh_TW", "tz": "Asia/Taipei", "is_portal": true, "company_id": 1}
```

  n8n gets it as `user`. LiteLLM's model sees an identity block in the system
  message without the email; `metadata` repeats the identity (`odoo_*`).
- **attachments** as `ATT` objects with a signed download link valid one hour:

```json
{"id": 12, "name": "報價.xlsx", "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
 "size": 12345, "url": "https://odoo.example.com/woow_project_ai/attachment/12/1793433600.4f1c…",
 "expires_at": "2026-11-24T09:00:00Z"}
```

  The link is built from `web.base.url`, which must be the public address of
  Odoo (set `web.base.url.freeze`).

Who is answered: every comment (text or files) of the tab's own people:
employees with Project rights in the internal chat, portal users in the
customer chat. Bots and notices never. A burst of one person gets one answer
covering it; two people asking at once each get theirs (one answer at a time
per chat, oldest question first). An employee's comment in the customer chat
pauses the AI for the project's minutes (default 10), counted again from each
further comment; customer messages written meanwhile are not answered later.

### Timeouts and retries

- Connect timeout 10 s. LiteLLM with *Stream Replies* (default): fails when
  no byte arrives for 30 s, or when the whole answer takes longer than the
  connection's *Timeout* (default 300 s). n8n: at most 90 s per call (behind
  Cloudflare, which cuts at about 100 s): long workflows answer
  `{"skip": true}` and push the reply.
- *Retries* (0–2, default 1) only when the connection could not be opened or
  the backend answered HTTP 429 / 503, after 3 s and 10 s. Never after the
  request was sent (read timeout, reset, 5xx): the backend may already be
  working on it. Redirects are never followed (a 3xx is an error).
- A failure posts the assistant's error message and is kept in the call log
  (status, HTTP code, duration; never the upstream body).
- Replies run in the cron process (*Project AI: Run AI Replies*), never in the
  web worker that received the message; `limit_time_real_cron` must allow the
  longest timeout + 60 s (+ 70 s with prefork workers), otherwise a thread of
  the web worker is used and the AI assistant form says so. At most
  `woow_ai_task.max_concurrency` (default 4) chats are answered at once.

## LiteLLM (`openai`) backend

```http
POST /v1/chat/completions HTTP/1.1
Host: llm.example.com
Authorization: Bearer sk-REPLACE-ME
Content-Type: application/json
Accept: text/event-stream
```

```json
{
  "model": "gpt-example",
  "messages": [
    {"role": "system", "content": "你是裝修公司的客服。\n\n專案說明（專案經理提供）：\n我們在台北安裝廚具。\n\n這張任務的資料（依提問者本人的權限讀取；不含留言與內部記錄）：\n- 任務：安裝吊櫃\n- 階段：施工中\n\n這是客戶對話。…\n\nYou are chatting in Odoo with this person (information only, no login is delegated):\n- name: 王小明\n- account: portal user (customer)\n- user_id: 7\n- partner_id: 15"},
    {"role": "user", "content": "[客戶 王小明] 吊櫃什麼時候裝？"}
  ],
  "user": "woow-project-ai-0edfec80-42",
  "metadata": {"session_id": "woow-project-ai-0edfec80-42", "surface_type": "project_task",
               "surface_project_id": "7", "surface_task_id": "42", "surface_tab": "customer",
               "odoo_user_id": "7", "odoo_partner_id": "15", "odoo_name": "王小明", "odoo_email": "user@example.com",
               "odoo_lang": "zh_TW", "odoo_tz": "Asia/Taipei", "odoo_is_portal": "true", "odoo_company_id": "1"},
  "stream": true,
  "stream_options": {"include_usage": true}
}
```

- `model` only when the connection has one. No `tools`, ever.
- `messages` start and end with a `user` turn: AI messages before the first
  question (the welcome message) are dropped; AI messages after the last
  question (pushes, errors) are moved before it.
- Images (png, jpeg, gif, webp; type from the content; ≤ 5 MB) are inlined
  as data URIs for the 3 most recent user messages, at most 4 images and
  about 7 MB per request. Documents (PDF, docx, xlsx, xls, csv, txt, md,
  json) follow the connection's *Documents Users Send*: `file` (default) sends
  each as an OpenAI `file` part with its own type (Odoo converts nothing;
  the 5 most recent user messages, at most 5 documents of ≤ 4 MB, sharing the
  7 MB budget); `link` sends the signed link line. Other files are a line
  `[附件] <name>（<mimetype>，<size>）：<signed url>`.
- Streamed answers: `: keepalive` comments and events other than `message`
  are ignored, `delta.content` is joined, `[DONE]` ends it (a stream without
  `[DONE]` and without `finish_reason` is invalid). `usage` is stored in the
  call log and the monthly usage. Non-streamed: `choices[0].message.content`.
- `<think>` / `<thinking>` blocks and special tokens are removed.
- LiteLLM has no handoff signal.

## n8n backend

```http
POST /webhook/project-ai HTTP/1.1
Host: n8n.example.com
Content-Type: application/json
X-Woow-Timestamp: 1793430000
X-Woow-Signature: sha256=5d2b…
```

(The two `X-Woow-*` headers only when a signing secret is set. No
`Authorization` header: the secret is never sent.)

```json
{
  "event": "message",
  "session_id": "woow-project-ai-0edfec80-42",
  "assistant": {"id": 3, "name": "裝修客服"},
  "user": {"user_id": 7, "partner_id": 15, "name": "王小明", "email": "user@example.com",
           "lang": "zh_TW", "tz": "Asia/Taipei", "is_portal": true, "company_id": 1},
  "channel": {"id": 42, "type": "group"},
  "surface": {"type": "project_task", "project_id": 7, "task_id": 42, "tab": "customer"},
  "system_prompt": "專案說明（專案經理提供）：…",
  "message": {"id": 991, "text": "[客戶 王小明] 幫我看這份報價", "attachments": [ATT]},
  "history": [{"role": "assistant", "text": "您好，我是客戶 AI 助理。", "attachments": []}],
  "action": "sendMessage",
  "sessionId": "woow-project-ai-0edfec80-42",
  "chatInput": "[客戶 王小明] 幫我看這份報價"
}
```

- `message` is the question being answered, `history` the earlier context.
  `action` / `sessionId` / `chatInput` are what an n8n Chat Trigger reads.
- Verify the signature on the **raw body**: `sha256=` + hex
  HMAC-SHA256(secret, `"<X-Woow-Timestamp>.<raw body>"`); reject timestamps
  more than 300 s away; compare in constant time; answer 401 when it fails.
- Answer HTTP 200 with JSON within 90 s:
  - `{"reply": {"text": "markdown", "attachments": [{"name": "…", "data_base64": "…"} | {"name": "…", "url": "https://…"}]}}`
  - `{"replies": [{"text": "…"}, …]}` (at most 10 messages)
  - `{"output": "markdown"}` (n8n AI Agent / Chat Trigger)
  - `{"skip": true}`: no message now (push it later)
  - n8n item lists `[{…}]` are accepted (first item).
  Anything else (non-200, not JSON, empty) is an error: the person gets the
  assistant's error message. Answer non-200 when the agent fails.
- **Handoff** (customer chat only): `{"output": "好的，我請負責同事跟您聯絡。", "handoff": true, "handoff_reason": "客戶要找真人"}`.
  `handoff` must be the JSON boolean `true`. The reply text is posted, the AI
  pauses for the project's minutes, and the task gets one To-Do for its first
  assignee (else the project manager), never a second one while it is open.
  `{"handoff": true}` alone posts nothing and only hands over. Only this
  field counts: a `[[handoff]]` marker in the text is not recognised in task
  chats (it stays in the text), so a Chat Trigger workflow or a LiteLLM
  backend cannot hand off. The internal chat ignores `handoff` (the text is
  posted; without text it is an empty reply).
- *Test Connection* sends `{"event": "ping", "assistant": {…}, "connection": {…}, "action": "loadPreviousSession", "sessionId": "woow-project-ai-ping-<id>"}`
  (`assistant` only from an assistant's form); any HTTP 200 is a success.
- n8n stores every execution with its body (the conversation): set an
  execution retention period.

## Reply rendering

Markdown (paragraphs, bold / italic / strike, code, headings, lists, quotes,
GFM tables) rendered to safe HTML; HTML in the reply is shown as text; only
`http`, `https` and `mailto` links. Images and file links whose host is in
the connection's *Download Domains* are downloaded (https; size limit *Max
File Size*, default 20 MB; no redirect outside the list) and attached; other
links stay links. `data:image/…;base64` URIs in the text are attached without
the list. n8n `data_base64` attachments are decoded with the same limit. The
type is detected from the content; HTML, SVG, XML and scripts are stored as
`application/octet-stream`. At most 10 files per reply, 100 MB together, all
downloaded within 45 seconds (a host that sends its bytes slowly is cut off:
the file stays a link).

## Push API (a later reply)

```http
POST /woow_project_ai/v1/push HTTP/1.1
Host: odoo.example.com
Authorization: Bearer wpai_REPLACE-ME
Content-Type: application/json
```

```json
{"to": {"channel_id": 42}, "text": "**稍後回覆**：報價已更新。",
 "attachments": [{"name": "報價.pdf", "data_base64": "JVBERi0xLjQK…"}]}
```

Posts into a task AI chat as the assistant, for n8n "skip, then push" (take
`channel.id` from the webhook body). Only the assistant answering that chat
now may push (same rules as a reply: assistant active, connection ready,
customer chat safe for customers, task active), never while the AI is
paused. Counted in the monthly usage like a reply; never answered.

| Status | `error` | Meaning |
|---|---|---|
| 200 | — | `{"ok": true, "channel_id": 42, "message_id": 1001}` |
| 400 | `invalid_json`, `invalid_body`, `too_large` (80 MB), `empty_message`, `invalid_recipient` | bad request (`to` must be `{"channel_id": <int>}`) |
| 401 | `invalid_key` | missing or wrong key, or the assistant is archived |
| 403 | `not_allowed` | the chat is answered by another assistant, or cannot be answered now |
| 404 | `channel_not_found` | no such task AI chat (other conversations are never reachable) |
| 409 | `paused` | a colleague took over (or a handoff): the AI is paused there |

The route uses `auth='none'`: the database must be resolvable from the host.

## Signed attachment URLs

`GET /woow_project_ai/attachment/<attachment id>/<token>`,
`token = <expires unix>.<hex HMAC-SHA256(database.secret, "woow_ai_task.attachment|<id>|<checksum>|<expires>")>`:
one hour, only for files of messages in a task AI chat that an assistant
answers now, served as a download (`nosniff`, `Cache-Control: private,
no-store`); 404 otherwise. Links signed by the Discuss AI module (scope
`woow_ai`) never open here.

## Monthly usage

`woow.project.ai.usage.month`: one row per assistant, connection, project,
tab and UTC month: replies posted, failed replies (the person got the error
message), prompt / completion tokens (0 for n8n). A reference for invoicing
by hand: nothing is limited or deleted (limits live in the LiteLLM keys).
The project form shows this month's AI replies per tab.
