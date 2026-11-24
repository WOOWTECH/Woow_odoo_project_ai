# woow_ai_task：專案 AI（任務上的兩個 AI 對話）

`woow_ai_task`（`depends: project, mail, portal, bus, web`）讓每個專案選兩個 AI 助理：**內部用 AI** 和 **客戶用 AI**。每張任務因此最多有兩個對話分頁（選了哪個助理才有哪個）：「內部 AI 對話」（有專案權限、讀得到任務和它的專案的同事和 AI）和「客戶 AI 對話」（客戶和 AI，也顯示在 portal 任務頁；專案要分享給 portal 使用者才有）。

模組自帶執行核心（AI 連線、AI 助理、回覆佇列、呼叫紀錄、每月用量、推送 API），從 `woow_ai_contact` 18.0.2.0.0 移植、只留專案 AI 需要的部分。和 Discuss AI（`woow_ai_contact`）、Live Chat AI（`im_livechat_ai`）可以裝在同一個資料庫，彼此不共用任何東西：模型 `woow.project.ai.*`、網址 `/woow_project_ai/...`、排程「Project AI: …」、推送金鑰 `wpai_…`、系統參數 `woow_ai_task.*`、Discuss 頻道上的欄位與方法 `woow_project_ai_*`。只處理自己的頻道（`woow_project_ai_task_id` 有值），其他頻道（私訊、Live Chat、一般群組、Discuss AI 的對話）完全照舊。不依賴 PaaS，沒有收費邏輯。

## 設定

**系統管理員**（專案 › 配置 › 專案 AI）：

- **AI 連線**：類型只有兩種：**LiteLLM**（OpenAI 相容的 chat completions；Odoo 從不送工具，只對話）和 **n8n webhook**。網址、只寫不讀的金鑰（LiteLLM：Bearer 虛擬金鑰，限額在 LiteLLM 設；n8n：選填的簽章金鑰）、模型、逾時、重試、串流、文件處理方式、回覆檔案的網域白名單、內容紀錄、上線前檢查、「測試連線」。
  - **可給客戶使用**：LiteLLM 一律可以（只對話）；n8n 要系統管理員確認流程不查公司資料、不呼叫工具、不跨對話記憶後才勾。改了網址或類型就自動取消，要重新確認。
- **AI 助理**：名稱、頭像、機器人聯絡人（一直保持封存：不會出現在「開始對話」和 @ 建議，但照樣能在任務對話發言、被讀到）、系統提示詞、錯誤訊息、歷史訊息數、推送金鑰；選一條 AI 連線。說明會顯示在專案「AI 助理」頁每個選項的下方（也在「搜尋更多」清單裡），讓專案經理知道該選哪一個。
- 另有「回覆工作」「呼叫紀錄」「AI 用量」。

**專案經理**（專案表單 ›「AI 助理」頁；其他專案使用者只能看）：

| 欄位 | 說明 |
|---|---|
| 內部用 AI 助理 `woow_project_ai_internal_assistant_id` | 有效的 AI 助理都可以選 |
| 客戶用 AI 助理 `woow_project_ai_customer_assistant_id` | 另外要求它的 AI 連線「可給客戶使用」（選單篩選，存檔時再用 constraint 檢查，錯誤訊息說明要請系統管理員改什麼）。留空：portal 任務頁沒有 AI |
| 給內部 AI 的專案說明 `woow_project_ai_internal_brief` | 每次提問接在助理的系統提示詞後面。內部對話只給也讀得到這個專案的同事（跟著任務的人可能讀得到任務、卻讀不到它的專案），所以讀得到對話（包括先前的回答）的人都讀得到這段說明；送出前仍再確認對話裡每個人都讀得到專案 |
| 給客戶 AI 的專案說明 `woow_project_ai_customer_brief` | 客戶分頁**唯一**會送出的專案文字 |
| 客戶分頁歡迎詞 `woow_project_ai_customer_welcome` | 選填；客戶對話第一次建立時由 AI 張貼一次 |
| 同事發言後 AI 暫停（分鐘）`woow_project_ai_pause_minutes` | 預設 10，1–1440 |

頁面也顯示兩個分頁的狀態（未設定／可以使用／未就緒，加原因；「可以使用」的說明分頁不同：客戶對話的 AI 回答客戶、同事發言後暫停）和本月 AI 回覆次數（內部對話／客戶對話）。專案沒有分享給 portal 使用者時，客戶分頁是「未就緒」（沒有客戶開得了客戶 AI 對話），任務上也不顯示客戶分頁。狀態和次數用 sudo 算，專案使用者只看到結果：原因是固定的句子，不含網址、金鑰或連線名稱；專案經理永遠讀不到 AI 連線。系統管理員另有「這個專案的 AI 用量」按鈕。所有欄位和這一頁都只給 `project.group_project_user`：portal 使用者和沒有專案權限的員工都讀不到。

複製專案（或 sale_project 用專案範本建立專案）時，只保留目前仍然可選的助理：已封存的、或客戶分頁的連線已經不可給客戶使用的，就不帶過去，所以複製不會因為原專案的分頁「未就緒」而失敗。

## 兩個對話

- 第一次有人打開分頁時才建立（後台任務表單的分頁、或 portal 任務頁按「開啟 AI 對話」）；安裝時不會替所有任務建立。
- `channel_type='group'`、沒有 `group_public_id`：成員讀得到；讀得到任務的系統管理員也讀得到（Odoo 給管理員的頻道規則），但不會列在成員裡。
- **對話只認對話自己的連結**：頻道上的 `woow_project_ai_task_id`／`woow_project_ai_tab` 只有本模組會寫（建立時用 context 帶不進來、寫入也被拒絕，連 `default_*`、`install_mode` 都一樣），(任務, 分頁) 有唯一限制。任務上的 `woow_project_ai_internal_channel_id`／`woow_project_ai_customer_channel_id` 是從對話那邊算出來的，不能寫，所以沒有人能把任務的分頁指到別的對話（別人的群組、Live Chat、LINE 對話）。加入成員也只限任務對話。
- 建立對話用乾淨的 context（只留語言），用戶端帶的 `default_*` 等值（例如永久暫停、額外成員、上層頻道）不會進到這個受信任的建立。
- 兩個人同時第一次打開時，先用一次不改值的 UPDATE 鎖任務列：後到的請求遇到序列化衝突，由 Odoo 重試，加入同一個頻道（只鎖不寫的話，後到的會建第二個頻道、撞唯一限制而失敗）。
- 名稱「<任務名稱>｜內部 AI」「<任務名稱>｜客戶 AI」（公司語言），任務改名時只改它自己的對話。
- 分頁的 AI 助理機器人是成員；專案換助理時換機器人成員（同步時處理）。
- 客戶對話開放 portal 上傳檔案（`allow_public_upload`）。

## 誰可以是成員

| 對話 | 成員 |
|---|---|
| 內部 | 有專案權限（`project.group_project_user`）、現在讀得到任務、也讀得到任務的專案的內部使用者（Odoo 自己的規則：`task.with_user(user)` 和 `project.with_user(user)` 的讀取權限，涵蓋使用者所有**有效**的公司；沒有專案的任務只看任務）。給內部 AI 的專案說明是專案使用者才看得到的欄位，所以內部對話也只給他們；讀不到專案的人（例如只追蹤任務的人）進不來，因為之前的回答可能用到專案說明。回答用過哪些專案的內部說明，記在對話上（`woow_project_ai_brief_project_ids`）：任務換到別的專案後，內部對話（加入、同步、發言、全域規則）也要讀得到這些舊專案，先前的回答只留給讀得到舊專案的人 |
| 客戶 | 現在讀得到任務的 portal 使用者（Odoo 18 portal 規則：專案隱私是 portal、任務有效、追蹤者或專案協作者），以及讀得到任務的內部使用者（可以看、也可以發言；**靜音加入**：成員通知設定「直到我重新開啟」、`custom_notifications = 'no_notif'`，不會每則客戶訊息都通知；只通知開著的頁面，不另排解除靜音的排程） |

**只有這個模組會加成員，而且只在有人打開分頁時**（@ 不會讓任何人加入：提及只留對話的成員和 AI 機器人，發言、修改訊息、通知收件人都一樣；@ 到不是成員的人時訊息照樣送出，但不通知他、也不讓他讀到。同一個人同時打開兩次時，先用一次不改值的 UPDATE 鎖頻道列，後到的請求遇到序列化衝突，由 Odoo 用新的快照重試，看到已經加入的成員）： `add_members`、`channel_join`、`/discuss/channel/join`、`/discuss/settings/mute`、邀請連結 `/chat/<id>/<uuid>`、直接建立 `discuss.channel.member`、用 `channel_member_ids` 寫頻道，全部拒絕，系統管理員也一樣；只有本模組自己的程式（context 帶一個物件 `SYNC`，JSON-RPC 的 context 偽造不出來）可以加。成員可以自己退出（Odoo 的「離開」）：退出後就讀不到這個對話，他們在這裡未讀的提及會標成已讀。任務對話不顯示邀請、通話和更換頭像的按鈕。

**頻道本身也由任務管理：** 成員只能改名稱和說明，封存、允許上傳、頭像、暫停狀態等都拒絕。

**失去權限時移除：**

- 觸發同步：專案隱私、專案／任務取消追蹤（新增追蹤者只會多出權限，不同步）、專案協作者（修改、刪除）、任務換專案或換上層任務（連同跟著搬的子任務）、封存／取消封存、受指派人、公司；使用者的群組（含 portal 撤銷、拿掉專案權限）、啟用、公司；被移出群組的使用者。
- 每小時排程「Project AI: Sync Task Chat Members」再掃一次所有任務對話（每 200 個一批、每批連同停在哪裡一起 commit；掃描中被刪掉的對話會略過，不會讓整批失敗）。每次至少跑一批、最多 120 秒：用 Odoo 的進度 API 告訴排程還剩多少，排程會在同一輪立刻再跑、從停下的地方接著掃。同一輪的時間上限（`limit_time_real_cron`，和派送排程一樣算法）快到時（留 30 秒），改回報「沒有剩下」讓 Odoo 結束這一輪，並在 60 秒後觸發排程、在新的一輪接著掃，所以對話再多也掃得完，不會被時間上限砍掉。停在哪裡記在系統參數 `woow_ai_task.sweep_cursor`，用 SQL 讀寫（用 ORM 寫每次都會清掉所有程序的快取）。這個排程和其他排程一樣是 noupdate：升級不會改寫正在跑的排程（Odoo 會直接拒絕），也不會把關掉的排程重新打開；之後要改它就用 migration。
- **發言當下檢查**：在任務對話發言（或修改訊息）的人，必須是現在讀得到任務的成員；修改或清空訊息只限訊息的作者（Odoo 也讓管理員修改任何人的訊息，那會把客戶或 AI 的話和檔案拿掉）；不是的話丟 AccessError，並在**另一個交易**立刻把他移除（等鎖最多 3 秒，失敗就留給下次同步）。發文、上傳等 mail 路由先查頻道（`_get_thread_with_access`），失去權限的成員在那裡就被拒絕並移除。人和 AI 只寫一般留言：不收 email、不收人自己發的通知。封存的任務一律拒絕（修改、刪除訊息、釘選、表情回應、改名也一樣）。有人發言、AI 回覆或推送時，都先把已經失去權限的成員移除，訊息才送出。客戶對話不收記事（log note）：客戶讀得到客戶對話裡的一切，所以記事或 `is_internal` 的訊息一律拒絕；沒有指定類型的張貼一律當成一般訊息（Odoo 預設是記事）。客戶 AI 的歷史也只取客戶讀得到的訊息。
- **不能用別人的名義**：JSON-RPC 的 `message_post` 只能用自己的 `author_id`；任務對話的 `mail.message` 只能用發言建立（直接建立一律拒絕，自己的名字也一樣），非 superuser 也不能直接修改（釘選、加星號也一樣：Odoo 自己的釘選用 SQL、加星號寫在聯絡人那邊、修改訊息以 superuser 處理）、刪除；發言和編輯時 @ 到的人只留對話的成員。任務對話的發言不收評分（Odoo 的公開評分路由 `/rate/<token>/submit_feedback` 會以 superuser、用評分的聯絡人當作者發一則）。AI 助理以 superuser 張貼，只有這種張貼才當成機器人。
- **讀取只有一個檢查點**：Odoo 18 讀訊息時用 sudo（`mail.message.fetch`），只有 `read` 會檢查權限；複製、經由任何模型的匯出（`mail.notification`、加星號的訊息、頻道成員）、onchange 的來源記錄與計算、委派欄位（`mail.mail`）、範本渲染的 `object.body` 都用 sudo 讀；其他模型的 related 欄位與儲存的計算欄位也以 superuser 讀（mail_group 的 `body`／`subject`／`author_id`／`email_from`／`attachment_ids`、完整編輯器由上層訊息算出的欄位、im_livechat 的 `parent_body`），onchange 裡也一樣。這些讀取最後都到同一個地方：從資料庫讀出欄位（`_fetch_query`）。本模組在那裡檢查，而且只要環境是替一位真實使用者在做（`env.uid` 不是 superuser 帳號、也不是沒有使用者的請求），superuser 也一樣：一批裡有任務對話的訊息時，確認這位使用者依本模組規則讀得到那些對話（現在讀得到任務；內部對話還有專案和用過說明的專案），讀不到就清掉它們剛讀到的欄位、丟 AccessError（ORM 的預先讀取會退回只讀被要求的那一筆）。Odoo 的 record rule 和存取檢查以 superuser 讀的欄位（`model`、`res_id`、`message_type`、`subtype_id`、`is_internal`、`parent_id`、建立／修改的人和時間、`date`）不擋：先讀到這些欄位時（例如 `filtered_domain`），一起預先讀到的內容欄位會被清掉，不留在快取、也不讓規則檢查失敗。哪些訊息屬於任務對話用快取裡的 `model`／`res_id` 和每個程序記著的任務對話 id 判斷（只記住已經存在的頻道：查一個還沒有資料列的 id 不會把未來的任務對話記成一般頻道），沒有任務對話時不多任何查詢。（`message_post` 回傳的訊息記錄帶著發言時的 context，所以發言的標記不算讀取理由。）完整編輯器另外檢查上層訊息（`model`／`res_ids` 是規則欄位）：讀不到上層訊息的人不能用它開編輯器。Odoo 給作者看的寄送失敗（`/mail/data` 的 failures、bus）略過作者現在讀不到的任務對話訊息。Odoo 對訊息的讀取權限檢查（`_check_access`：`read`、`has_access`、`_get_with_access`）也套用本模組規則：有任何訊息的 record rule 時（例如 woow_document_management 加的），Odoo 會以 superuser 的 `filtered_domain` 檢查，之後的讀取可能直接用快取。`_search`（非 superuser）也略過讀不到的任務對話訊息：Odoo 讓作者和被通知的人搜尋得到它們（收件匣、星號、歷史、read_group）。條件只鎖在其他模型時不加任何東西；鎖在特定頻道時先看快取；其他情況每則訊息看它自己的頻道（用主鍵查 `discuss_channel`，不建所有任務對話的集合），是任務對話時才看使用者讀得到的清單。
- **使用者讀得到哪些任務對話**（`res.users.woow_project_ai_readable_rule`，欄位快取，每個交易算一次；加入、離開、群組、追蹤者、專案、協作者、使用者變動或建立任務對話後重新計算）：系統管理員（Odoo 讓他們讀所有頻道）是一個編譯好的子查詢，資料每次使用時才讀，PostgreSQL 每個語句只算一次、而且只在碰到任務對話時才算；用過內部說明的專案那個條件不和外層關聯（`id NOT IN (SELECT channel_id FROM woow_project_ai_channel_brief_project_rel WHERE project_id NOT IN (讀得到的專案))`），只算一次，它的關聯表也在要先寫入資料庫的欄位裡。其他人（Odoo 只讓成員讀群組對話，任務對話是群組對話）是他們自己所在、而且讀得到的任務對話 id 清單：一個有索引的成員查詢，有任務對話時再一個查詢，之後頻道的全域規則、訊息的搜尋條件、`mail.mail` 的條件都用這個清單，沒有東西要規劃。不在任何任務對話的人，規則只是 `woow_project_ai_task_id = False`。
- 讀不到的人看到任務對話訊息的名稱（記錄連到它時 Odoo 用 sudo 顯示，例如每個員工都讀得到的 `mail.notification`）是「任務 AI 對話」，不是含任務名稱的對話名稱；其他訊息的名稱交給 Odoo 和其他模組（例如 woow_document_management 的「Restricted folder」），名稱、模型、res_id 用同一個查詢讀出、放進快取，每批一個查詢（和 Odoo 一樣）。失去對話的人（成員與否）未讀的提及通知，同步與每小時排程會標成已讀並更新收件匣計數。設定 > 技術 > 郵件（系統管理員）不列出讀不到的任務對話訊息的郵件：每封郵件看它的訊息的欄位（不建訊息的集合）。
- **檔案內容與名稱**：使用者自己的環境裡，Odoo 的 `ir.attachment` 檢查本來就有效；其他模型以 superuser 讀連結檔案時（snailmail 信件的 `attachment_datas`／`attachment_fname`，橋接模組改成依真實使用者計算；hr_expense 的 `message_main_attachment_checksum`；任何連到檔案的 many2one 標籤，Odoo 以 sudo 組出，onchange 裡也一樣），讀不到對話的真實使用者（`env.uid`）拿到的 `datas` 是空的（沒有使用者的請求 `auth='none'` 也算讀不到）、顯示名稱是「任務 AI 對話檔案」（都先用 SQL 判斷，不讀檔案的名稱和內容），其他從資料庫讀出的欄位（名稱、checksum、大小、類型、token、儲存位置……）一律拒絕；檔案屬於哪裡（`res_model`／`res_id`／`res_field`）、`public`、`type`、公司、建立／修改的人和時間照樣讀得到（superuser 和系統管理員除外：排程、本模組的工作、`force_storage`）。
- **連結一律檢查存進資料庫的結果**（SQL，不看輸入值或快取：Odoo 把字串或浮點數的 many2one id 直接寫進 INSERT，快取卻是空的；x2many 的指令也一樣），不管 id 型別和指令形式：
  - **上層訊息**：任何地方的訊息都不能拿任務對話的訊息當上層訊息，除非它就在那個對話裡（superuser 也一樣：沒有 Odoo 流程需要）。（Odoo 18 本身：一般群組的成員可以拿任何訊息的 id 當上層訊息；這是核心的問題，使用者決定不修也不回報，本模組只保護任務對話。）
  - **訊息的檔案**：任務對話的檔案不能放在別處的訊息（superuser 也一樣：Odoo 的發言路由以 superuser 執行，正是繞過的地方）；任務對話的訊息只能帶這個對話裡、由訊息作者上傳的檔案（例外：AI 的回覆，作者是 AI 助理、檔案由本模組以 superuser 建立）。
  - **訊息的位置**：新的任務對話訊息只能經由對話自己的 `message_post` 建立，superuser 也一樣（Odoo 自己的離開、釘選通知，AI 的回答與推送，客戶對話的歡迎詞都走 `message_post`；大量寄信精靈以 superuser 建立的 `mail.mail` 會被拒絕）；非 superuser 不能把訊息移進任務對話。
  - **表情回應**：任務對話訊息的表情回應只給讀得到對話的成員（Odoo 的 'create' 檢查也放行被上層訊息提及的人；路由以 superuser 執行，所以看 `env.uid`），封存任務的對話不能回應。
  - **評分**：`rating.rating` 非 superuser 時不能針對任務對話（存進資料庫的 `res_model`／`res_model_id` 和 `res_id`）或任務對話的訊息（`message_id`），建立和修改這些欄位時都檢查；任務對話的發言也不收評分（見上）。
  - **主要附件**（`message_main_attachment_id`：發票、費用等）：非 superuser 時不能是任務對話的檔案（存進資料庫的值）。Odoo 自己設定時是 superuser、只用那筆記錄自己的檔案，不受影響。
  - **文件歸檔規則**（`woow_document_management`）：規則把某種記錄的每個檔案以 superuser 複製到文件資料夾，資料夾的讀者都看得到；任務對話的檔案永遠不歸檔（新檔案、移到對話上的檔案、補歸檔都一樣）——橋接模組 `woow_ai_task_document_management`。
  - **子頻道**：`parent_channel_id` 不能是任務對話、`from_message_id` 不能是任務對話的訊息（superuser 也一樣：任務對話沒有討論串；Odoo 自己的限制讀快取，字串 id 時是空的）。
  - **檔案的位置**：任務對話檔案的 `res_model`／`res_id`／`res_field` 永遠不變（所有人）；非 superuser 不能直接在任務對話建立或移入檔案（上傳以 superuser 執行；這也擋住用 `res_field` 改對話頭像、在封存任務的對話加檔案；模型名稱用 bytes 或 XML-RPC 的 base64 值給、或放在 default 裡也一樣：Odoo 存成文字 `discuss.channel`，它自己的建立檢查卻跳過不認得的模型名稱）；只有上傳的人能改或刪（系統管理員除外：批次刪除、`force_storage`）；id 依 SQL 的方式換成整數，其他型別在非 superuser 時拒絕。
  - **外寄郵件**（`mail.mail`，委派給訊息、由排程以 superuser 寄出）：非 superuser 只能連到自己讀得到的任務對話訊息（例外：Odoo 自己的通知信）。
  - **郵寄信件**（`snailmail.letter`，相關欄位以 sudo 讀檔案、由排程寄出）：永遠不能帶任務對話的檔案（所有人）——橋接模組 `woow_ai_task_snailmail`。
  - **連結預覽**：任務對話訊息的連結預覽只有作者能建立、修改、刪除或隱藏（Odoo 讓存取權限管理員動任何預覽）。
  - 這些檢查裡的「是不是任務對話」都在 SQL 裡 join `discuss_channel`，不用程序快取。
  - **成員**：建立的成員列依存進資料庫的 `channel_id` 檢查，不能在任務對話（本模組自己的同步除外）。（Odoo 自己的成員建立遇到浮點數或字串的頻道 id 本來就失敗；`res.partner`／`mail.guest` 的 `channel_ids` 是成員表上的 many2many，寫不進列：Odoo 的 `new_message_separator` 是 NOT NULL。）
- **能讀一切的角色**：系統管理員（程式、規則）、郵件範本編輯者（範本可以呼叫 `.sudo()`；不呼叫時 `object.body` 一樣經過 `fetch` 的檢查）；存取權限管理員可以給自己任何權限。
- **讀取的限制**：同步之前，失去權限的人還會出現在成員名單，但已經**讀不到任何東西**：一條全域 record rule（`rule_discuss_channel_woow_project_ai`）只讓「現在讀得到任務的人」讀任務對話（內部對話只限有專案權限、也讀得到專案的內部使用者），依目前使用者計算。其他頻道完全不受影響：規則對它們永遠成立（公開使用者、訪客、portal、員工，不管公司設定如何；任務規則出錯時也只是任務對話讀不到）。
- **通知**：一則訊息的通知（成員的 web push、被 @ 的成員的收件匣／email）只送給現在讀得到任務的成員。
- **即時訊息**：Odoo 會沿用 websocket 上次訂閱的頻道、送出時也不檢查權限，而它的用戶端在成員被移除時不會重新訂閱。所以本模組在每次 bus poll（websocket、peek）時，用 poll 那位使用者自己的權限略過他現在讀不到、而這次有通知的任務對話（哪些頻道是任務對話每個程序記著；有任務對話時先用一個 bus_bus 查詢看這次有沒有它們的通知，有才查權限）：被移除的人開著的頁面（官方用戶端或腳本都一樣）收不到這個對話的即時訊息，連移除他們的那一則（同一個交易裡送出）也收不到。
- 任務對話不會出現在 portal 的 `/my/discussions`，也不算進它的卡片、分頁和通知中心的「未讀對話」：那些清單用 sudo 列出成員的對話和最後一則訊息，任務對話只從任務頁進入，每次都檢查權限。這是兩個自動安裝的橋接模組：`woow_ai_task_portal_discuss`（裝了 woow_odoo_portal_discuss 時）的控制器是它控制器的子類別，所以不管模組載入順序（包括之後才在執行中的伺服器裝 woow_odoo_portal_discuss）都排在前面；`woow_ai_task_portal_notification`（裝了 woow_portal_notification_discuss 時）讓通知中心的未讀數只算 `/my/discussions` 列出的對話。
- **客戶的任務 AI 對話未讀訊息**另外算（`woow_ai_task_portal_notification`）：通知中心（`/my/notifications`）有一則「您的任務 AI 對話中有 N 則未讀訊息」，`/my` 有一張卡片；只有一張任務有未讀時連到那張任務的 AI 對話（`/my/tasks/<id>#woow_project_ai_chat`），不然連到「我的任務」。只算客戶現在讀得到的任務的對話（用客戶自己的權限查）。這樣 AI 回不了、同事晚一點才在對話裡回答時，客戶也看得到。

## 封存與刪除

- 任務封存：AI 不回答，分頁只能閱讀（不能發言、編輯、刪除、釘選、表情回應、改名）；portal 使用者讀不到封存的任務，同步時移除。取消封存後照常。在表單上封存或取消封存，開著的分頁馬上跟著變。
- 任務刪除：刪除它的 AI 對話（訊息、檔案一起）。
- 沒有專案的任務（變成私人任務）：AI 不回答，同事發言時用預設暫停分鐘數，不會出錯。

## 回覆

- **每則都回**，不需要 @AI：內部對話回所有內部成員的留言，客戶對話回所有 portal 成員的留言。機器人訊息和通知永遠不回。
- 同一個人連續發幾則：一個回答涵蓋全部；兩個人同時問：各自得到回答（由舊到新）。歷史帶發言者名字：內部對話 `[名字] 內容`；客戶對話由系統先標身分 `[客戶 名字]`、`[負責同事 名字]`，系統訊息說明身分只看標籤第一個詞。
- **暫停**：內部使用者在客戶對話留言，AI 暫停專案設定的分鐘數，同事每多一則就重新計算，時間到自動恢復。暫停期間客戶的訊息之後不會補回答。**發言的同事會被取消靜音**，之後客戶的回覆會通知他（像 Live Chat 的真人客服）。後台客戶分頁的輸入框上方有一行提示：訊息會給客戶看、AI 會暫停。
- **轉真人**（只在客戶對話）：後端回 n8n `"handoff": true` 時：有回覆文字就張貼、AI 暫停、在任務上替第一位受指派人（沒有就專案經理）排一個待辦事項；同一張任務同時只會有一個未完成的（完成後下一次再排新的）。`[[handoff]]` 標記在任務對話不算（會留在文字裡），LiteLLM 沒有轉真人訊號。內部對話忽略 handoff。
- **助理不能回答時**（助理封存、連線未就緒；客戶對話另外：連線**此刻**不可給客戶使用、專案沒分享給 portal）：不呼叫後端，每段未就緒期間張貼一則固定通知（客戶：「AI 暫時無法回覆，負責同事會再跟您聯絡。」），專案表單顯示原因。**客戶對話裡每一則這樣的客戶訊息都替負責的同事排待辦事項**（和轉真人共用：未完成的會沿用，完成後下一則再排新的），因為同事在客戶對話是靜音的。
- **回覆失敗時**（後端錯誤、試了三次、問題等超過 30 分鐘才輪到）：提問的人收到助理的錯誤訊息；客戶對話另外排同一個待辦事項。
- 提問的人在回答前失去權限：不回答。

## AI 收到什麼

- **系統訊息**：助理的系統提示詞 ＋ 這個分頁的專案說明 ＋ 一小段任務資料（＋ 客戶對話的身分標籤說明），之後是身分區塊。
- **任務資料用提問者本人的權限讀**（`with_user(提問者)`，不用 sudo）。提問者是被回答那則訊息的作者，而且必須是這個分頁的對象（客戶對話：portal 使用者，絕不用同事的權限）：
  - 內部對話：名稱、說明（純文字）、階段、截止日期、受指派人、標籤、優先級別、上級任務、子任務名稱。關聯的記錄（上級任務、子任務、標籤、人…）只放**對話裡每個成員都讀得到**的：AI 的回答所有成員都看得到。
  - 客戶對話：上面這些裡，Odoo 18 portal 可讀欄位（`SELF_READABLE_FIELDS`）有的才放，而且**不放上級任務和子任務**：同一個客戶對話裡的客戶可能看得到不同的子任務，AI 的回答所有成員都看得到。
  - 兩者都不放任務的留言和內部記錄，最多 8,000 字。
- **使用位置**：`{"type": "project_task", "project_id": …, "task_id": …, "tab": "internal" | "customer"}`；`project_id` 只在對話裡每個人（包括提問者）都讀得到專案時才有，不然是 null：回答給對話裡每個人看，客戶也可能讀得到任務、卻讀不到它的專案。
- **檔案**：依 AI 連線的「使用者傳送的文件」（`file` 直接附檔、`link` 簽章連結）；兩個分頁都能上傳。

## 推送與用量

- n8n「先 skip、之後推送」：`POST /woow_project_ai/v1/push`，`{"to": {"channel_id": <頻道>}, "text": …}`。只有這個對話**現在**的回答助理可以推，AI 暫停中回 409；推送算一次回覆（推送 commit 之後，用 READ COMMITTED 的游標計數，不會因為同時有別的回覆計數而漏算）。只有任務對話可以推（其他對話一律 404）。見 [PROTOCOL.md](PROTOCOL.md#push-api-a-later-reply)。
- 每月用量（`woow.project.ai.usage.month`）：範圍＝專案（清單有專案欄、可依專案分組；總 token 數有合計；專案刪除後顯示「已刪除的專案（#編號）」），分頁分開記（顯示「內部 AI 對話」「客戶 AI 對話」），即使兩個分頁用同一個助理。給人工開發票參考，不自動刪除，也不擋任何東西；限額放在 LiteLLM 金鑰。

## 畫面

- **後台任務表單**：「內部 AI 對話」「客戶 AI 對話」兩頁，存檔的專案有那個分頁的助理、而且使用者可以進去時才顯示（表單上改了專案但還沒存檔時，照存檔的專案）。頁面直接嵌入 Odoo 18 的 Discuss 元件（`Thread`、`Composer`；誰在打字由輸入框顯示一次），打開頁面時建立或加入對話、標成已讀，不會讓輸入框搶焦點、把表單捲下去；嵌入中算「顯示中」，新訊息不會跳 web push 或聊天視窗。引用與釘選訊息的連結可以跳過去；回覆、表情符號、編輯後游標回到輸入框。封存的任務沒有輸入框，訊息也沒有操作（編輯、刪除、回覆、表情）。用上一筆／下一筆換任務時，已經是成員就直接顯示，不是的話顯示「開啟這張任務的 AI 對話」按鈕（翻任務不會建立或加入對話）。
- **portal 任務頁**（`/my/tasks/<id>`、`/my/projects/<專案>/task/<id>`）：「客戶 AI 對話」區塊，只給登入、讀得到任務的使用者（單靠分享連結的 access token 不顯示）。按「開啟 AI 對話」才載入（iframe 嵌入 Odoo 自己的 Discuss 頁）；不會跟著頁面自動載入（裡面的輸入框會搶焦點、把頁面捲下去），只有成員從 `#woow_project_ai_chat` 錨點進來時才直接打開。另有「在新分頁開啟」。開啟的網址 `/my/tasks/<id>/woow_project_ai` 要帶任務頁產生的 token（只對這張任務和這次登入有效；舊頁面或另一次登入的 token 在框架裡只顯示「請重新整理任務頁」，按下去在最外層重新載入任務頁）。說明文字寫明：我們的同事和其他看得到這張任務的人也看得到訊息和檔案；同事看這個區塊時，說明改成「客戶看得到這個對話，您發言後 AI 會暫停」。在 portal 頁面的小視窗裡，任務對話不能關掉（關掉會只剩 Odoo 的對話清單）。
- **客戶對話的提醒到處都有**：同事在客戶對話的輸入框（任務表單、Discuss、聊天視窗、portal 頁）看到的提示都是「傳給客戶的訊息：您發言後 AI 會暫停…」。
- **專案表單**：「AI 助理」頁（見上）。

## 安裝、解除安裝、回復

- 安裝：只相依 Odoo 原生模組；和 `woow_ai_contact`、`woow_ai_contact_portal_discuss`、`im_livechat_ai` 一起裝也可以（沙盒實測）。
- **裝了以後，每個服務這個資料庫的映像都必須帶著 `woow_ai_task`**：它的全域 `discuss.channel` record rule 讀的是本模組的計算欄位，它的畫面和排程也需要它的程式。映像少了這個模組、資料庫卻還裝著時，每個非 superuser 的頻道讀取都會失敗（Discuss、Live Chat、portal 對話全站壞掉）。
- **五個模組一起進映像**（回復用的映像也是）：`addons-sources.py sync --modules woow_ai_task,woow_ai_task_portal_discuss,woow_ai_task_portal_notification,woow_ai_task_snailmail,woow_ai_task_document_management`，`paas-addons.list`、`cloud-addons.list` 也寫這五個。Odoo 只在橋接模組的某個相依模組「正在安裝」時才自動裝它；`-u`、`-u all`、更新應用程式清單、對已安裝模組再 `-i` 都不會。橋接模組比兩邊都晚到資料庫時要明確安裝：裝了 woow_odoo_portal_discuss 的 `-i woow_ai_task_portal_discuss`，裝了 woow_portal_notification_discuss 的 `-i woow_ai_task_portal_notification`，裝了 snailmail 的 `-i woow_ai_task_snailmail`，裝了 woow_document_management 的 `-i woow_ai_task_document_management`（另一邊沒裝的資料庫不要裝，會連另一邊一起裝上）。之後若再加新的橋接模組，`woow_ai_task` 要在升級時（post-migrate）替已經有兩邊的資料庫安裝它。
- **模組操作和排程**：從「應用程式」或 `odoo shell` 的安裝、升級、解除安裝（`button_immediate_*`）在有排程正在執行時會被 Odoo 拒絕，而 AI 派送排程（本模組和 `woow_ai_contact` 的）和成員同步掃描每一輪都佔著自己的排程。請在空檔操作，或先把 `woow_ai_task.job_runner`（和 `woow_ai_contact.job_runner`）設成 `thread` 等這一輪結束，或停掉排程程序（`max_cron_threads=0`）再試；命令列的 `odoo -i`／`-u` 不受影響。
- **要拿掉**：在帶著這個模組的映像上解除安裝。解除安裝會先關掉那條全域規則，把任務對話裡的人都移出（機器人留下）、對話封存、歷史保留（只有系統管理員讀得到），再由 Odoo 刪掉本模組的資料（規則先刪，欄位和資料表後刪）。四個橋接模組會一起解除安裝：它們只有程式和一張 portal 卡片，沒有東西要還原。
- 只有必須留著、又相依它的模組（例如日後相依它的 PaaS 平台）才會擋住單獨解除安裝：那時只能往前修，或還原資料庫和檔案庫快照。
- `odoo-bin neutralize`：AI 連線清空網址和金鑰並封存，n8n 的「可給客戶使用」取消，推送金鑰撤銷；每個資料表都先用 `to_regclass` 檢查。

## 和 PaaS 舊任務 AI 的關係

這個模組不動 PaaS（`woow_paas_platform`）。/woow 前台與 PaaS 服務資料的讀取，之後由只在 PaaS 安裝的橋接模組處理。
