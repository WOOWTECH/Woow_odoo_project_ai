# Woow Odoo Project AI

Odoo 18 addon for **project AI**: every task gets two AI conversations, an
internal one for the colleagues who can read the task and a customer one for
the portal users who can read it (also on the portal task page). The project
picks one **AI assistant** per conversation; each assistant uses an
**AI connection** to an n8n webhook or a LiteLLM (OpenAI-compatible) server.

The module ships its own runtime core (connections, assistants, reply queue,
call logs, monthly usage, push API). It depends on standard Odoo modules only
and installs next to the Discuss AI modules (`woow_ai_contact`) without
sharing anything with them.

[繁體中文說明](README_zh-TW.md)

| Module | Status | What it does |
|---|---|---|
| `woow_ai_task` | in development | Project AI: internal and customer AI chats on every task |

## Repository

Gitea `woow-paas/Woow_odoo_project_ai` is the source of truth. GitHub
`WOOWTECH/Woow_odoo_project_ai` is a push mirror of its `main` branch and
tags (`.gitea/workflows/mirror-to-github.yml`). Send changes as Gitea pull
requests.

## License

LGPL-3.0 — see [LICENSE](LICENSE).
