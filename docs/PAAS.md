# woow_ai_task_paas：專案 AI 在 PaaS 平台上（橋接模組）

`woow_ai_task_paas`（`depends: woow_ai_task, woow_paas_platform`，`auto_install`）是專案 AI 在 PaaS 平台（`woow_paas_platform`）上的橋接模組。兩邊都裝了才會自動安裝；只裝平台、或沒有平台的資料庫都不會裝到它。**它不改平台的任何程式**：全部用繼承做。

它做四件事：

1. `/woow` 的任務頁多一個「客戶 AI 對話」分頁（Odoo 自己的 Discuss 頁面嵌在頁面裡，下方有「在新分頁開啟」）；`/my/tasks/<id>` 保留專案 AI 自己的區塊。
2. 停用平台舊的任務 AI（每個專案一個 agent pod、WoowBot 對話、「Task Chat: …」頻道），**不刪任何東西**。
3. 任務 AI 對話的成員跟著 PaaS 才有的權限變動走（workspace 成員、專案和雲端服務的綁定）。
4. 只有專案經理和系統管理員能設定專案的 AI 助理和專案說明（平台讓每個員工都能寫專案）。

## 誰在哪裡看到什麼

| 頁面 | 誰 | 看到什麼 |
|---|---|---|
| `/woow` 任務頁 | 讀得到任務、專案有客戶用 AI 助理、而且專案分享給 portal 使用者的客戶和同事 | 「客戶 AI 對話」分頁：按下去才載入（建立或加入對話），框架裡是 Odoo 的 Discuss 頁面；下方「在新分頁開啟」。客戶看到的說明和 `/my` 一樣；同事看到「客戶看得到這個對話…」；封存的任務只能閱讀（同事才看得到） |
| `/woow` 任務頁 | 所有人 | 舊的「聊天」分頁不見了（開關打開時才有） |
| `/woow` 專案看板 | 所有人 | 舊的「AI」「Agent」狀態膠囊不見了，也不再查它們的狀態 |
| `/my/tasks/<id>` | 讀得到任務、專案有客戶用 AI 助理的客戶和同事（這裡不要求專案分享給 portal 使用者：同事在沒分享的專案也看得到這個區塊） | 專案 AI 自己的「客戶 AI 對話」區塊（`woow_ai_task`），不變 |
| 後台任務表單 | 有專案權限的同事 | 「內部 AI 對話」「客戶 AI 對話」兩頁（`woow_ai_task`）；平台舊的 AI Chat 頁改名「關聯雲端服務」，只留服務欄位 |

- **內部 AI 對話只在後台**：`/woow` 只放客戶 AI 對話。
- **沒有自助入口**：`/woow` 沒有分享按鈕。客戶要由同事在後台加成任務的追蹤者或專案協作者。
- **`/woow` 的「專案」區由平台的設定決定**：系統參數 `woow_paas_platform.show_projects`（設定裡的「Show Projects (/woow)」）沒打開的網站，客戶看不到 `/woow` 的專案和任務頁，就從 `/my/tasks/<id>` 使用客戶 AI 對話；這個模組不改那個設定，程式兩種情況都一樣。
- **`/my` 是平台的例外**：平台的前台功能原則上都放 `/woow`。專案 AI 的 `/my` 區塊是平台接受的例外：對 portal 使用者，它不會多露出任何 `/woow` 看不到的任務（區塊要 Odoo 自己的任務讀取權限，平台 `/woow` 的判斷也是照同一條 Odoo portal 規則寫的）。
- **誰能用客戶 AI 對話**：讀得到任務的人（加上上面的條件），也就是今天後台的可見範圍。專案經理靠 Odoo 原生規則讀得到所有專案的任務，專案使用者讀得到所有分享給 portal 使用者的專案的任務：他們也就看得到、加得進這些任務的客戶 AI 對話，不管是不是那個 workspace 的成員。只讓服務所屬 workspace 的成員使用，是之後版本的計畫，這一版沒有。

### 嵌入的對話怎麼開

`/woow` 的分頁先問 `POST /woow_project_ai_paas/task_chat`（`type='json'`、`auth='user'`、參數 `task_id`），回答 `{'available', 'url', 'staff', 'readonly'}`；不能用就只回 `{'available': False}`，不說原因（不洩漏任務存在與否），這個路由沒有任何副作用。判斷和 `/my` 一樣（`woow_ai_task` 的 `_woow_project_ai_user_allowed`，用使用者自己的權限），另外要專案分享給 portal 使用者；封存的任務要已經有對話（不然打開會 404）。`url` 是 `woow_ai_task` 的 `/my/tasks/<id>/woow_project_ai?token=…`（token 只對這張任務和這次登入有效），它建立或加入對話，再轉到 `/discuss/channel/<id>`，那一頁用使用者自己的權限找頻道。同一個網址開新分頁；不帶 `full=1`（那個版本的返回列只接受 `/my`）。

已知限制：同一個瀏覽器登出再登入後，還開著的 `/woow` 分頁拿的是舊 token，框架裡會顯示「這個頁面已經過期，請重新載入任務頁。」和「重新載入任務頁」按鈕，按下去回到 `/my` 的任務頁。

## 舊的任務 AI：停用、不刪

開關關著（預設）時：

| 入口 | 橋接模組 |
|---|---|
| 舊的 `/api/ai/` 路由：`/api/ai/agents`、`/api/ai/chat/history`、`/api/ai/chat/post`、`/api/ai/chat/upload`、`/api/ai/stream/<int:channel_id>`、`/api/ai/connection-status`、`/api/ai/agent-status`、`/api/ai/agent-pod-status`，以及 `/api/ai/` 底下之後新增的任何路由、綁到這 8 個方法的任何網址 | `ir.http._pre_dispatch` 在端點執行前拒絕：JSON 路由回錯誤「舊版任務 AI 已停用。客戶請在任務頁使用「客戶 AI 對話」；同事請在後台任務表單使用「內部 AI 對話」。」，上傳和串流回 HTTP 410。什麼都不讀、不寫、不開 pod（串流是唯一會開 pod 的路由） |
| 專案 AI 的任務對話（開關打開時也一樣） | 舊路由的頻道檢查一律拒絕專案 AI 的任務對話：誰讀得到這些對話只由專案 AI 決定（失去任務權限、還沒被同步移出的成員也進不去） |
| 建專案時自動開 agent runtime | `create` 一律寫 `use_agent_runtime` False（明寫的值也蓋過 `default_*` 和 `ir.default`；複製也一樣） |
| 手動打開 `use_agent_runtime` | 從 False 變 True：非 superuser 模式一律拒絕（「舊版任務 AI 已停用，不能替專案開啟 Agent Runtime。」）；superuser 模式（平台程式、排程）改寫成 False 並記 WARNING（含專案 id），不會拒絕。True→True 可以；關掉由平台自己的守衛決定；**不清任何值**（平台的 reconciler 依這個欄位）。平台的 migration 在升級時比橋接模組先載入，不會被改正 |
| 任務的 `chat_enabled` → `_create_chat_channel` | `create` 寫 False；`write` 拿掉 True（記 id）；`_create_chat_channel` 只回原本的頻道（或沒有），永遠不建立 |
| `/my` 舊的 agent 卡片和自動加入 | 回「停用」的值：不建立舊對話、不加入任何人 |
| 後台任務表單、專案表單 | 在程式裡（`_get_view`）隱藏兩組舊對話欄位、AI Chat 頁改名「關聯雲端服務」；專案表單隱藏 `use_agent_runtime`。開關是畫面快取鍵的一部分 |
| `/woow` 任務頁「聊天」分頁、看板的「AI」「Agent」膠囊與 3 個狀態查詢 | OWL 樣板擴充和 `patch`：隱藏、不查 |
| `/woow` 舊的 AI 對話頁 `#/ai-assistant/chat/<id>` | 保留；它查歷史的路由被拒絕，頁面就顯示那句錯誤 |
| 排程：Drift Detection、Idle Reaper、Long-term GC、Reach Reconciler | 還有活著的 session 時保留（縮掉 pod 靠它們）；沒有時**安全地停用**（見下面） |
| Webhook、skill bundle、WoowBot、Agent Templates／Skills／MCP Servers 選單 | 保留不動 |

**平台自己的舊對話**：`data/legacy_task_ai.xml`（`noupdate="0"`，安裝和每次升級都跑）挑出平台自己的舊任務對話：`channel` 型、不是討論串（子頻道）、名稱以「Task Chat: 」開頭的頻道（討論串的權限跟著上層頻道，Odoo 也不讓它們設群組），不管有一張任務、好幾張任務（平台複製任務或專案時會一起複製 `channel_id`，任務改名後頻道名稱不會跟著改）、或已經沒有任務（平台刪任務時不刪它的頻道）指到它；還沒記錄過、也沒被管理員還原過的才處理。先把它們原本的值 `{id: [active, group_public_id]}` 記在系統參數 `woow_ai_task_paas.restricted_chats`，再封存並設 `group_public_id = base.group_system`。每個對話各自在一個 savepoint 裡記錄和限制：寫不進去的那一個記 ERROR（id），維持原狀、不記錄（下次升級再試），其他照常。訊息、成員、任務的 `channel_id` 都保留；再跑一次不會改任何東西。兩件事都要做：`channel` 型頻道的規則不看 `active`，只封存的話員工照樣讀得到；系統管理員有自己的頻道規則，仍然讀得到。其他被任務指到的頻道（不是這種名稱或型別）只記 id（WARNING），留給人看。這支函式出任何錯都只記 ERROR（例外的類型和程式行，不記內容），不會讓升級失敗。

**排程怎麼停（不能直接寫 `ir.cron`）**：Odoo 的 `ir.cron.write` 先用 NOWAIT 鎖列，鎖不到就把**整個交易**回滾再丟錯；排程執行中會一直鎖著自己那一列。所以橋接模組每一列各用一個 savepoint `SELECT … FOR NO KEY UPDATE SKIP LOCKED` 先鎖，只把鎖到的停用（這個交易已經持有鎖，`ir.cron.write` 的 NOWAIT 一定成功）；正在執行的排程（列被鎖著）、以及這個交易開始之後剛被排程程序更新過的列（Odoo 的交易是 REPEATABLE READ，鎖它會得到序列化錯誤，這是預期中的情況，不記 ERROR）都回報「retry」，停了哪些記在 `woow_ai_task_paas.parked_crons`。只停原本開著的；還有 session 有 hash 而且不是 `absent` 時什麼都不動（回報「N 個活著的 session，排程保留」）。判斷 session 活著與否要靠平台的兩個欄位（`project_namespace_hash`、`state`）：任何一個不見了就什麼都不停（不知道就當作活著），並記 CRITICAL（registry 載入時也會記）。這四個排程在平台是 `noupdate="1"`，平台升級不會再打開。誰會呼叫：安裝時（`post_init_hook`）、清完舊 pod 之後、解除安裝時（`uninstall_hook` 反過來重新啟用，只動自己停的；有排程沒能恢復時，記錄留著並記 WARNING，請人手動啟用）。回報 retry 的排程，稍後在 `odoo shell` 再執行一次 `env['woow_paas_platform.agent_session']._woow_project_ai_paas_park_crons()` 並 `env.cr.commit()`。每個呼叫都只記錄、不會丟錯。

## 開關

- 系統參數 `woow_ai_task_paas.legacy_task_ai`：值（sudo 讀、去空白、轉小寫）是 `true` 或 `1`，**而且** `controllers/legacy.py` 的任務對話守衛有載入（`TASK_CHAT_GUARD_LOADED`）時，舊的任務 AI 才打開。守衛沒載入（平台的控制器類別不見了）就當作關著。
- 不在「設定」裡。它只有兩個用途：跑平台自己的舊任務 AI 測試，或有人刻意要讓舊的任務 AI 回來。**不要拿它當回復手段**：打開等於把舊功能放回來。
- 要讓舊的任務 AI 完整回來，除了把參數設成 `True`，還要恢復安裝時停掉的排程；關回去時把參數刪掉（設成 `False` 就是刪掉），再停排程（還有活著的 session 時它不會動排程）。在 `odoo shell` 執行，每次都要 `env.cr.commit()`（`odoo shell` 結束時會回滾沒有 commit 的變更），再 `env.registry.signal_changes()`，讓執行中的 Odoo 程序也讀到新的參數值（`odoo shell` 不會自己通知它們；它們把系統參數快取在記憶體裡）：

  ```python
  # 打開
  env['ir.config_parameter'].sudo().set_param('woow_ai_task_paas.legacy_task_ai', 'True')
  env['woow_paas_platform.agent_session']._woow_project_ai_paas_unpark_crons()
  env.cr.commit()
  env.registry.signal_changes()

  # 關回去
  env['ir.config_parameter'].sudo().set_param('woow_ai_task_paas.legacy_task_ai', False)
  env['woow_paas_platform.agent_session']._woow_project_ai_paas_park_crons()
  env.cr.commit()
  env.registry.signal_changes()
  ```
- 開關打開時，平台的舊路由照它自己的規則服務舊對話的成員：**已封存、限制給系統管理員的舊對話也一樣**（舊路由不看封存和群組）。要讓成員看不到，就保持開關關著；要讓它們回來，用下面的還原。
- 開關打開（含恢復排程）時，平台自己的 post_install 測試和沒有專案 AI 的資料庫失敗集合相同；關著時，平台的舊任務 AI 測試會失敗（預期）。平台的 at_install 測試在升級平台時比這個模組先跑，不會遇到它。
- `odoo-bin neutralize` 會刪掉它（`data/neutralize.sql`），解除安裝也會刪。
- `/woow` 讀 session 裡的 `woow_project_ai_paas_legacy`（`ir.http.session_info()` 加的一個布林值）決定要不要顯示舊分頁和膠囊；讀不到就當作關著。

## PaaS 上誰會在對話裡

`woow_ai_task` 用 Odoo 自己的規則判斷（平台的規則也算在內），所以讀取權限的變動立刻生效（它的全域規則讓失去權限的人讀不到）；成員名單則由同步更新。PaaS 才有的兩種變動由橋接模組觸發同步，不必等每小時的掃描：

- `woow_paas_platform.workspace_access`：新增一列只會多出權限（重新計算可讀的對話，不移除任何人）；改 `user_id` 或 `workspace_id`（只有系統管理員或 sudo 能改）同步前後兩個使用者；刪除同步被刪的使用者。
- 綁定：專案改 `cloud_service_ids`（或舊的 `cloud_service_id`）、服務改 `project_ids` 或 `workspace_id`、刪除服務：同步相關專案的任務對話。服務的 `write` 每次背景排程寫狀態都會跑，所以先看值裡有沒有這幾個鍵才讀任何東西：寫 `state` 不多任何查詢。
- 變動前後的成員和專案一律用 SQL 讀（綁定的關聯表、`workspace_access` 加 `res_users`），**不用 sudo 讀記錄**：平台自己對這些記錄的權限檢查才會照原樣先跑。
- 同步失敗不會擋住平台的變動：每次同步都在 savepoint 裡跑，出錯只記 ERROR（id、例外的類型和程式行，不記內容），平台的寫入或刪除照樣完成，漏掉的由 `woow_ai_task` 每小時的掃描補上；併發錯誤（序列化失敗、死結、鎖不到）照常往上丟，Odoo 會重試整個請求。
- 刪除整個 workspace 不觸發（資料庫直接連帶刪除）：每小時的掃描會處理，讀取權限則立刻沒有了。

會受影響的只有**沒有專案經理權限的同事**：

- 一般員工（只有 `base.group_user`）讀綁定專案的任務靠 workspace 成員資格（平台規則）：兩個對話都跟著它。
- 專案使用者（`project.group_project_user`）在 portal 可見的專案裡，靠 Odoo 自己的規則讀得到每張任務，但內部對話還要讀得到專案，綁定專案的專案只給 workspace 成員：內部對話跟著成員資格。
- 專案經理靠 Odoo 原生規則讀得到所有專案和任務：不受影響。新建的內部使用者預設就是專案經理（Odoo 的預設使用者範本），所以實際上大部分同事都不受影響。
- portal 客戶跟著任務的分享（追蹤者、協作者），和 workspace 無關。

這就是今天後台的可見範圍（見上面「誰能用客戶 AI 對話」）。

## 只有專案經理能設定專案 AI

平台讓每個員工都能建立、修改專案，`woow_ai_task` 卻假設只有專案經理會寫專案。所以在 PaaS 上由橋接模組把關這六個欄位：內部用／客戶用 AI 助理、兩段專案說明、客戶分頁歡迎訊息、同事發言後 AI 暫停分鐘數。

- 可以設定的人：superuser 模式、系統管理員、專案經理。
- 其他人：建立時（看 Odoo 實際會寫的值：值本身、`default_*`、`ir.default`）任一欄位不是預設值（沒有助理、空白、10 分鐘）就拒絕；修改時任一欄位**真的改變**就拒絕（照原值送回來的沒關係，畫面整批送出時會這樣）。比的是 Odoo 實際會存的值：例如多對一欄位只把整數當成記錄（數字字串、小數、布林值會被存成「沒有」），文字欄位把 `0`、`[]`、`{}` 存成那段文字；不確定的值一律當成改變。錯誤：「只有專案經理可以設定專案的 AI 助理與專案說明。」
- 對不能寫這個專案、或沒有這些欄位權限的人，先回 Odoo 自己的權限錯誤，和猜的值對不對無關。
- 他們複製專案時，複本不帶這六個欄位（複製仍然可以，只是沒有 AI 設定；這六個欄位的預設值直接放進複製的 `default`，所以沒有專案權限、讀不到這些欄位的員工也複製得了）；專案經理和系統管理員的複本照常帶過去。
- 專案表單上這六個欄位對其他人是唯讀的：在程式裡（`project.project._get_view`）設 `readonly`，使用者能不能設定是畫面快取鍵的一部分；沒有存在資料庫的畫面（見「回復用的映像」）。
- **不要替這六個欄位設任何 `ir.default`**：設了以後，非專案經理的同事就建不了專案。

## 五條規則：平台改版時不會壞

每次平台發版升級 `woow_paas_platform`，Odoo 也會升級這個橋接模組（重跑它的資料、驗它的畫面）。所以：

1. **不存任何 `ir.ui.view`**：平台的後台畫面和 `woow_ai_task` 的專案表單都在程式裡（`_get_view`）改；找不到目標就跳過並記 DEBUG。存在資料庫的畫面會在升級時被驗證，也會在少了這個模組的程式時被套用（見「回復用的映像」）。
2. **平台的類別在 `try/except ImportError` 裡 import**：失敗就記 ERROR，該項照上面說的「失敗時關閉」。
3. **其他平台接點都要「失敗也沒事」**：用平台欄位前先確認 `name in self._fields`、用方法前先 `hasattr`；資料函式、hook 和成員同步吃掉非預期的例外，只記 ERROR（id、例外的類型和程式行，不記內容），不丟出；superuser 模式的寫入改正並記錄，不拒絕。平台的東西不見了，表示那部分舊功能也不在了，橋接模組就不做事；什麼都不做反而危險的地方（判斷 session 活著與否），就不做有風險的事並記 CRITICAL。
4. **`/woow` 的 OWL 擴充是唯一的硬相依**：樣板錨點對不上時，那一頁第一次顯示就會出錯（Odoo 在瀏覽器裡套用樣板擴充，`-u` 看不到）。所以只用 5 個錨點，由契約測試檢查；樣板裡的式子在 JS patch 沒載入時也不會出錯（新分頁不出現、舊「聊天」分頁維持隱藏）。另外，橋接模組繼承的平台模型（`agent_session`、`workspace_access`、`cloud_service`）被刪除時，模組會載入失敗：同樣由契約測試把關。
5. **契約測試** `tests/test_platform_contract.py` 檢查用到的每個平台符號、網址、模型、xmlid、資料旗標、樣板錨點和 `/woow` 資產包（下一節），也證明這些檢查在名稱不見時真的會失敗。它和橋接模組的其他測試一起跑，需要兩個模組都裝著的資料庫：平台每個新版本在服務裝了這個模組的資料庫之前，都要在新的沙盒或正式資料的副本（沒有舊任務對話和活著的 session）上跑**整個橋接模組的測試**（約 100 項、幾分鐘），不只契約：契約只看符號還在，平台改了呼叫方式要靠其他測試（舊路由、`/my` 卡片、成員同步）抓到。

## 平台介面清單（契約）

| 種類 | 內容 |
|---|---|
| 模型 | `woow_paas_platform.workspace_access`、`.cloud_service`、`.agent_session` |
| 控制器 | `controllers/ai_assistant.py` 的 `AiAssistantController._check_channel_access`；`controllers/portal.py` 的 `PaasProjectCustomerPortal._paas_agent_values_for_task`、`_project_get_page_view_values` |
| 路由 | 上面 8 個 `/api/ai/` 路由；`/api/ai/` 底下**剛好**就是這 8 個；`/woow` |
| 欄位、方法 | `project.task`：`chat_enabled`、`channel_id`、`_create_chat_channel`；`project.project`：`use_agent_runtime`、`cloud_service_ids`、`cloud_service_id`；`agent_session`：`state`、`project_namespace_hash`、`project_id`、`_teardown_runtime`；`workspace_access`：`user_id`、`workspace_id`；`cloud_service`：`project_ids`、`workspace_id` |
| 排程 xmlid | `woow_paas_platform.ir_cron_agent_session_drift`、`ir_cron_agent_session_idle_reaper`、`ir_cron_agent_session_gc`、`ir_cron_reach_reconciler`，每個都要是 `noupdate` |
| 後台畫面目標 | 任務表單：`//group[field[@name='chat_enabled']]`、`//group[field[@name='channel_id']]`、`//page[@name='ai_chat']`；專案表單：`//field[@name='use_agent_runtime']` |
| `/woow` 樣板錨點 | `TaskDetailPage.xml`：舊「聊天」分頁按鈕、`//t[@t-if="state.activeTab === 'chat'"]`、`o_woow_td_tabs`、`o_woow_td_content`；`ProjectKanbanPage.xml`：`o_woow_kanban__status_pills`（每個都要剛好對到一個）；分頁的 class `o_woow_td_tab`、`o_woow_td_tab--active` |
| `/woow` JS | `TaskDetailPage`（`loadTask`、`setTab`、`static components` 裡的 `WoowIcon`、`props.taskId`、`state.task`、`state.activeTab`）、`ProjectKanbanPage`（`_loadStatuses`）、`services/rpc.js` 的 `jsonRpc`、`@web/session` 的讀法（`core/platform_config.js`）；資產包 `woow_paas_platform.assets_paas` |
| 頁面 | 以 portal 使用者 `GET /woow`，頁面裡有 `woow_project_ai_paas_legacy` |

清單只列橋接模組的程式用到的東西。橋接模組的測試另外用平台的 `woow_paas_platform.workspace`、`woow_paas_platform.cloud_app_template` 建測試資料，並檢查改名後的「關聯雲端服務」頁還有 `primary_cloud_service_id`：平台改這些時，要跟著改的是測試，不是程式。

## 舊對話的還原（不用 SQL）

封存並限制的舊對話，原本的值記在 `woow_ai_task_paas.restricted_chats`（解除安裝也會留著）。要還原時，系統管理員在 `odoo shell` 執行：

```python
env['project.task']._woow_project_ai_paas_unrestrict_legacy_chats()          # 全部
env['project.task']._woow_project_ai_paas_unrestrict_legacy_chats([42, 43])  # 只還原這幾個頻道
env.cr.commit()
```

它把每個頻道的 `active` 和 `group_public_id` 改回記錄的值，刪掉那幾筆記錄，並把頻道記在 `woow_ai_task_paas.released_chats`：之後每次升級的限制都會跳過它們（管理員的決定會留著）。記錄的群組已經不存在的頻道維持限制（還原成「沒有群組」會讓所有員工讀得到），留給人處理。橋接模組已經解除安裝時，這支方法不在了：用記錄裡的值，在後台（設定 › 技術 › Discuss 頻道，開發者模式）把頻道的「啟用」和「授權群組」改回去，再刪掉這個系統參數。

清掉舊的 agent runtime（只在還有活著的 session 時需要；它會 commit）：`env['woow_paas_platform.agent_session']._woow_project_ai_paas_teardown_all()` 只列出計畫（`{'plan': [{id, project_id, state, hash4}, …]}`）；`_woow_project_ai_paas_teardown_all(dry_run=False)` 逐一用平台自己的 `_teardown_runtime()` 拆掉、設成 `absent`、每個 session 各自 commit（失敗只回報不丟錯），最後停排程並 commit。

## 映像規則與回復

- **六個模組一起放進服務 PaaS 平台資料庫的每個映像**（回復時要退回的映像也一樣）：`woow_ai_task` 和它的五個橋接模組（`woow_ai_task_portal_discuss`、`woow_ai_task_portal_notification`、`woow_ai_task_snailmail`、`woow_ai_task_document_management`、`woow_ai_task_paas`）。沒有 PaaS 平台的網站用的映像帶另外五個（這個橋接模組在那裡沒有用處：它只跟著平台安裝）。之後裝上的本 repo 模組（例如 CRM 範本 `woow_ai_task_crm`）也要在每個服務那個資料庫的映像裡。
- PaaS 資料庫裝了 `woow_ai_task` 卻沒有橋接模組時，立刻 `-i woow_ai_task_paas`：自動安裝只在「有相依模組正在安裝」時發生，`-u` 不會裝新模組。
- 回復的順序：往前修（新版本）；或退回一個**仍然帶著這六個模組**的映像；或還原安裝前的資料庫快照，和當時跑的映像一起。
- **解除安裝 `woow_ai_task` 不是例行的回復步驟**：它會刪掉 `woow_ai_task` 的資料表（連線和金鑰、助理、用量、呼叫紀錄、專案 AI 設定、對話連結），任務對話封存、只有系統管理員讀得到；連同五個橋接模組一起移除，平台保留。舊的 AI 會以平台自己的程式回來（`/woow` 的「聊天」分頁、Enable Chat 建立每個員工都讀得到的對話、舊路由）；沒有專案開著 runtime，被限制的舊對話維持限制，排程恢復。
- **橋接模組不能單獨解除安裝**（會被拒絕：「「專案 AI - PaaS」不能單獨解除安裝：請解除安裝「專案 AI」（woow_ai_task），它會一起移除。單獨移除會讓舊版任務 AI 回來。」；和平台一起解除安裝則可以，舊 AI 會跟著平台一起移除）。

## 回復用的映像（少了這個模組的映像）

**PaaS 平台的部署會拒絕啟動**少了任何已安裝模組程式的映像：那樣的映像根本不會服務資料庫，網站會停在那裡，直到換回帶著這些模組的映像。所以裝了專案 AI 之後，少了這六個模組任何一個的映像都**不是回復目標**；安裝之前，要先確定現在跑的映像和回復時會退回的上一個映像都帶著這六個模組。

另外，橋接模組在資料庫裡**不存任何需要它的程式才能用的東西**（`tests/test_rollback_image.py` 檢查），所以資料庫本身不會因為少了它而壞掉：

| 種類 | 橋接模組存了什麼 |
|---|---|
| 記錄規則、欄位、篩選、動作、伺服器動作、選單、資產 | 沒有（`data/legacy_task_ai.xml` 的 `<function>` 只在安裝和升級時執行，不留紀錄；`/woow` 的 JS 由 manifest 掛上，不存資料庫） |
| 畫面（`ir.ui.view`） | 沒有：專案表單的唯讀、隱藏 `use_agent_runtime`、任務表單隱藏舊欄位和改名，都在程式裡（`_get_view`）。存在資料庫、又用到這個模組自己欄位的畫面，在少了它的程式時會讓表單在瀏覽器出錯（`"project.project"."<欄位>" field is undefined`），所以不用 |
| 排程 | 沒有自己的；停用平台的四個（`active` 設 False，記在 `parked_crons`） |
| 系統參數 | `woow_ai_task_paas.legacy_task_ai`、`restricted_chats`、`released_chats`、`parked_crons`：只有橋接模組自己讀，平台的程式完全沒有提到這個模組 |
| 平台資料列的值 | 舊對話封存並只給系統管理員；新專案的 `use_agent_runtime`、新任務的 `chat_enabled` 寫 False；不清任何既有的值 |
| `ir.model.data` | 只有 Odoo 替它繼承的模型記的 `ir.model` 名稱 |

手動啟動 Odoo（不經過平台的部署）時的實測（平台 v18.0.2.0.34，用過舊 AI 的資料庫）：

- **少了 `woow_ai_task_paas`**（其他五個在）：Discuss、後台專案和任務表單、`/woow` 和 `/my` 頁面都正常；Odoo 每次啟動記一行 ERROR「Some modules are not loaded」。但**舊的任務 AI 會再回應**：`/woow` 回到舊的「聊天」分頁、`/my/tasks` 又出現舊的 agent 卡片（`use_agent_runtime` 還開著的專案）。在那個映像上升級平台會把橋接模組留在「待升級」；回到帶著它的映像時，一般啟動就會把它升級好。
- **少了六個模組**：Discuss 和頁面正常，但 `woow_ai_task` 自己的畫面讓後台的任務表單、以及專案使用者的專案表單打不開（缺的欄位沒有一個是橋接模組的）。

在 PaaS 平台的部署上，這兩種情況都到不了：映像會先被拒絕。
