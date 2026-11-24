# Woow Odoo Project AI

Odoo 18 模組：**專案 AI**。每張任務有兩個 AI 對話：

- **內部 AI 對話**：看得到這張任務的同事。
- **客戶 AI 對話**：看得到這張任務的 portal 用戶，也顯示在 portal 的任務頁。

專案替每個對話選一個 **AI 助理**；每個助理用一條 **AI 連線**，接 n8n webhook 或 LiteLLM（OpenAI 相容）。

模組自帶執行核心（AI 連線、AI 助理、回覆佇列、呼叫紀錄、每月用量、推送 API），只相依 Odoo 原生模組；可以和 Discuss AI（`woow_ai_contact`）裝在同一個資料庫，彼此不共用任何東西。

[English](README.md)

| 模組 | 狀態 | 內容 |
|---|---|---|
| `woow_ai_task` | 開發中 | 專案 AI：每張任務的內部／客戶 AI 對話 |

## Repo

主 repo 是 Gitea `woow-paas/Woow_odoo_project_ai`。GitHub `WOOWTECH/Woow_odoo_project_ai` 只是鏡像，接收 `main` 與標籤（`.gitea/workflows/mirror-to-github.yml`）。修改請送 Gitea PR。

## 授權

LGPL-3.0，見 [LICENSE](LICENSE)。
