# 驗證紀錄（2026-09-23）

## 任務 30：正式語料與 SEC smoke

- 範圍：AAPL、MSFT、NVDA，最近五個已完成會計年度的 10-K／10-Q；原始 filing 存於 Docker `raw-data` volume，不納入 Git。
- 正式 SEC smoke：三家公司各讀取一次 submissions（recent 筆數 1000／1002／1002），並各下載一份已知 10-Q HTML（766,083／7,732,058／1,517,157 bytes）。使用宣告的 User-Agent、低於每秒 10 次的共用限流與官方 URL allowlist；下載內容未另存入工作樹。
- 相同範圍 backfill 重跑命令：`docker compose -p sec-research-live-20260923 run --rm --no-deps mcp python -m sec_research.cli backfill`。結果：新增 0、更新 0、略過 57、隔離 0、失敗 0、處理章節 195、切塊 1,703、facts 新增 0／更新 672。`facts_updated` 是目前 upsert 的報表欄位，不代表 672 筆數值真的改變。
- 重跑後正式 PostgreSQL：filing 57、section 195、chunk 1,703、XBRL fact 672、embedding 1,703；filing／section／chunk 重複鍵分組各 0，隔離／失敗各 0。三家公司 filing 分布：AAPL 5 份 10-K／15 份 10-Q、MSFT 5／13、NVDA 5／14。
- Active index build：`622ccb73-ba32-47ad-81c0-f48a64eb59dc`，OpenRouter `openai/text-embedding-3-small`、1,536 維、chunker `v1`、狀態 ready。
- 離線固定評估：`tests/fixtures/sec/evaluation_cases.json` 的 6 題敘事，加上 `tests/fixtures/sec/full_evaluation_cases.json` 的 4 題 XBRL、4 題複合、2 題無資料／含糊、2 題安全，共 18 題。敘事 metadata 100%、Recall@8 至少 85%、引用 URL 格式與 accession 解析 100%；XBRL fixture 的 concept／value／unit／period／accession 4／4 正確；複合案例皆依序呼叫敘事與數值工具，引用只指向對應公司及 SEC；無資料、單位歧義、文件指令與偽造引用案例均通過。模型迴圈在這組固定評估使用 deterministic fake，並非正式 OpenRouter 模型證據。
- 獨立測試 PostgreSQL／pgvector 執行 `uv run pytest -q`：140 通過、1 項第三方 Starlette／AnyIO deprecation warning。此測試資料庫與正式語料資料卷隔離。

未驗證界線：SEC URL 的「可解析」目前指 allowlist、HTTPS、accession 與引用 metadata 檢查，不代表逐一對 18 題每條引用執行遠端 HTTP 200 驗證。完整 MCP／Demo／正式模型端到端證據由任務 31 另行記錄。

## 任務 31：完整 Demo E2E 與重建

- 2026-09-23 使用獨立 Compose 專案 `sec-research-rebuild-20260923` 從無快取 image、全新 PostgreSQL／raw volume 依 README 明確 migrate、backfill、rebuild-index。空庫時 readiness 為 `INDEX_UNAVAILABLE`；建置後為 `ready`。新語料為 filing 57、成功章節 195、chunk 1,703、XBRL fact 672、embedding 1,703。重新建置程式 image 時保留該資料卷，未刪除正式專案資料。Demo 僅綁 `127.0.0.1:18501`。
- 新語料 active build：`8fbdabe7-b5e6-402c-a82b-f5b6ec8b3758`；parser `v1`、chunker `v1`、OpenRouter `openai/text-embedding-3-small`、1,536 維；答案模型 `openai/gpt-6-luna`。目前資料庫有 57 份 filing、195 個成功章節、1,703 個 chunk／active embedding、672 筆 XBRL fact；最近一次完成的匯入記錄失敗章節 0。金鑰只在執行環境注入，未寫入專案或報告。
- 目前程式在上述重建環境重新執行 `tests/e2e/live_demo_smoke.py`：MCP discovery 六個工具全部符合 allowlist、實際呼叫六個工具均為 `ok`、三種 resources 可讀，MCP `/health/live` 與 `/health/ready` 正常。三類內建問題在 Streamlit 測試執行器中呼叫正式 OpenRouter Responses 模型；敘事走 RAG、數值只走 XBRL、複合先 RAG 後 XBRL，均呈現繁體中文、對應證據區塊、SEC URL、工具軌跡與耗時。這是實際 App 邏輯與服務 E2E，不是人工瀏覽器視覺檢查。

| 類型 | 端到端耗時 | 工具耗時（毫秒，依呼叫順序） | SEC 引用 | UI 警告 |
|---|---:|---|---:|---:|
| 敘事 | 40,262 ms | 618、12、11、11、11、10 | 5 | 2 |
| 數值 | 9,310 ms | 11 | 5 | 0 |
| 複合 | 41,978 ms | 756、11、11、11、11、11 | 2 | 3 |

- 在獨立 `sec-research-eval-test` PostgreSQL／pgvector 上執行完整 `uv run pytest -q`：172 通過，1 項第三方 Starlette／AnyIO 棄用警告。18 題固定集由離線 deterministic fake 模型與固定資料驗證，不能當作 18 題正式模型逐題語意評估。另以四組人工標定原子主張重跑 `answer_eval`，逐項支持率、必要主張涵蓋率及引用正確率皆為 100%，達三項各 100% 門檻；這也是離線合成案例，不是正式模型分數。
- 最新 Demo 在查詢前由同一 OpenRouter 金鑰讀取模型清單，確認 `openai/gpt-6-luna` 可見並將模型依賴顯示為 ready；此檢查證明目錄端點可達且模型可見，不單獨證明每次推論成功。後續三類正式問題完成了實際 Responses 推論。MCP 容器在正式工具呼叫後產生結構化事件，現場檢查至少兩筆，具操作、狀態、耗時與 request ID；解析與索引事件另有自動測試。
- `uv run python -m compileall -q src`、`uv pip check`、`docker compose config --quiet`、`git diff --check` 均成功。針對 65 個限定專案文字檔與目前三個隔離服務容器日誌的正式金鑰值及金鑰／認證標頭樣式掃描皆為 0 命中；未稽核已替換容器的歷史日誌，不能宣稱全歷史絕對無洩漏。
- 額外正式模型探針「比較 AAPL 風險因素變化」只取 2024／2025 兩份 10-K，雖回傳兩個有效引用 ID，答案正文沒有逐項引用標記，也未明示推導出的整個 corpus 邊界或逐期 filing 存在／缺漏。此探針使下列驗收缺口可具體重現；隨後已增加行內引用標記檢查，欠缺標記時會降級成僅列本次 SEC 證據的未完成答案，但不會自動驗證每句話的語意支持度。

### 31.5 補驗（同一隔離重建環境）

- 跨期題先呼叫 `list_filings`。正式輸出逐期列出 AAPL FY2021–FY2025、MSFT FY2022–FY2026、NVDA FY2022–FY2026 各五份本機 10-K；單元測試另驗證缺一年或分頁不完整時降級，不把缺期當成沒有變化。預設 corpus 公司、form 與期間已置入答案文字。
- 新版索引命令合併輸出 filing、成功／失敗章節、chunk 與 index build；正式重建後資料庫數量及 active build 如上。MCP 搜尋引用提供短摘，且事件日誌與回應共用 request ID。重建後實際呼叫六個 public tools 與三個 resources 成功；目前 MCP 容器在 15 分鐘觀察窗內有 72 筆結構化事件，均含 request ID、狀態與耗時。
- 正式模型三類問題在新版容器中再經 Streamlit 測試執行器驗證：敘事／數值／複合題皆呈現繁體中文、證據區、工具軌跡與 SEC 連結，MCP 六工具及三資源皆成功。最後一輪耗時分別為 44,520／11,895／51,942 ms，對應引用連結數 1／5／2。敘事與複合題在該輪因引用規則安全降級，僅列取得的 SEC 證據；另一次正式模型直呼三類問題時，數值及複合題可產生完整答案，敘事題可產生帶限制警告的逐年分析。模型生成有變異，故不得把單次完整輸出說成每次必然完整。
- 逐項來源抽核：MSFT FY2022–FY2026 營收 198.270、211.915、245.122、281.724、331.839 十億美元與隔離資料庫官方 XBRL facts 相同；AAPL FY2021 的獨立 COVID-19 風險標題、FY2022 的疫情揭露、FY2023 的公共衛生／氣候措辭與 FY2025 的 Section 232／機器學習措辭可在各自 10-K Item 1A 找到；NVDA FY2025 的 142% 與 FY2026 的 68% 資料中心增幅、H20 與開源基礎模型描述可在其 10-K Item 7 找到。這是正式答案的人工抽核，不等於對所有未來模型敘述的語意保證；引用驗證器已對缺行內引用、表格漏列引用、偽造及未列名 ID 失敗關閉。

### 70 項 EARS 對照

判定：`通過` 表示有目前範圍內的離線、資料庫或 live 證據；`部分` 表示只有較窄的證據；`未達` 表示現行實作或輸出不符合該條件。這裡的通過不表示對所有未來 SEC 文件與模型輸出的形式證明。

| 條件 | 判定 | 證據或缺口 |
|---|---|---|
| AC-01.1 | 通過 | ingestion run 保存 canonical scope、CIK 對應與時間。 |
| AC-01.2 | 通過 | 正式與乾淨回填皆採三公司、五個已完成會計年度、10-K／10-Q。 |
| AC-01.3 | 通過 | 非唯一／無效 ticker fixture 拒絕下載。 |
| AC-01.4 | 通過 | scope 由 canonical 值及 intent hash 共用。 |
| AC-02.1 | 通過 | 三家公司正式 SEC submissions 與官方 mapping smoke。 |
| AC-02.2 | 通過 | 正式 SEC HTML 下載及 57 份 corpus。 |
| AC-02.3 | 通過 | 正式 company facts 回填 672 筆。 |
| AC-02.4 | 通過 | User-Agent、官方 URL allowlist、低於 10 次／秒限流驗證。 |
| AC-02.5 | 通過 | 暫時性錯誤、重試與 checkpoint fixture。 |
| AC-02.6 | 通過 | HTML type／identity 隔離 fixture。 |
| AC-02.7 | 通過 | 正式 DB filing metadata 與來源 URL。 |
| AC-03.1 | 通過 | 相同範圍重跑與三種 duplicate 鍵皆為 0。 |
| AC-03.2 | 通過 | 重跑新增／更新均 0，略過 57；更新路徑另有 fixture。 |
| AC-03.3 | 通過 | 中斷續跑 fixture。 |
| AC-03.4 | 通過 | CLI 輸出五類摘要與正式重跑計數。 |
| AC-04.1 | 通過 | HTML 清理 fixture 與正式解析 57 份。 |
| AC-04.2 | 通過 | 10-K 必要 Item fixture。 |
| AC-04.3 | 通過 | 10-Q 必要 Part／Item fixture。 |
| AC-04.4 | 通過 | 目錄假命中、缺章與異常順序 fixture。 |
| AC-04.5 | 通過 | chunk metadata 與正式 1,703 筆。 |
| AC-04.6 | 通過 | chunk→section→filing 與相鄰識別測試。 |
| AC-04.7 | 通過 | 重建索引命令有 filing、成功／失敗章節、chunk 與 index build 的合併摘要；正式資料庫核對為 57／195／0／1,703。 |
| AC-05.1 | 通過 | metadata-first SQL 與 filter 整合測試。 |
| AC-05.2 | 通過 | 未明示範圍時答案文字揭露預設 Demo corpus 公司、form 與已收錄期間；實際比較子集仍由答案另行說明。 |
| AC-05.3 | 通過 | PostgreSQL FTS＋pgvector、正式搜尋工具呼叫。 |
| AC-05.4 | 通過 | rank、metadata、來源定位與 cursor 契約測試。 |
| AC-05.5 | 通過 | 無結果 fixture 回傳證據不足、不擴張來源。 |
| AC-06.1 | 通過 | 正式 discovery／tools list 與嚴格 schema 契約。 |
| AC-06.2 | 通過 | 六個公開工具及三種唯讀資源。 |
| AC-06.3 | 通過 | 正式六工具結構化成功回應。 |
| AC-06.4 | 通過 | boundary 參數驗證與結構化錯誤契約。 |
| AC-06.5 | 通過 | 暫時性依賴、not_found、非法輸入契約。 |
| AC-06.6 | 通過 | 查詢角色唯讀；公開工具沒有 ingestion／rebuild，錯誤已清理。 |
| AC-07.1 | 通過 | 正式數值問題首個且唯一工具是 XBRL。 |
| AC-07.2 | 通過 | facts 結構化欄位與正式 XBRL 工具。 |
| AC-07.3 | 通過 | 多單位／期間 fixture 與 warning 契約。 |
| AC-07.4 | 通過 | custom taxonomy fixture 不猜測正規化。 |
| AC-08.1 | 通過 | 正式敘事問題走 RAG。 |
| AC-08.2 | 通過 | 正式純數值問題只走 XBRL。 |
| AC-08.3 | 通過 | 複合題依序取得 RAG 與 XBRL；正式答案明確區分資料中心年報敘述與公司整體 XBRL 營收。 |
| AC-08.4 | 通過 | 三類正式跨期題逐年列出五份找到的 10-K；缺期與分頁不完整測試降級，未把缺少當作沒有變化。 |
| AC-08.5 | 通過 | 正式敘事／複合答案揭露未完整逐頁比較的限制，缺引用時降為未完成；衝突與不足 fixture 另通過。 |
| AC-08.6 | 通過 | 指令、Demo 輸出及正式答案均明示非投資建議。 |
| AC-09.1 | 通過 | 三類正式答案的關鍵數字及敘事抽核可在對應 SEC XBRL／10-K 章節找到；行內引用驗證失敗即降級。屬固定 Demo 範圍抽核，非任意未來回答的語意保證。 |
| AC-09.2 | 通過 | 新版 Demo 顯示 RAG 公司／form／兩日期／accession／章節／SEC URL；測試通過。 |
| AC-09.3 | 通過 | 新版 Demo 顯示 XBRL concept／期間／單位／form／申報日／accession；測試通過。 |
| AC-09.4 | 通過 | 缺少、偽造、未列名或漏列行內引用均失敗關閉；模型無法證明的部分以警告或未完成狀態揭露。 |
| AC-09.5 | 通過 | SEC HTTPS allowlist 與本次工具引用驗證。 |
| AC-10.1 | 通過 | 正式 Streamlit 三題、繁體中文、引用、錯誤狀態。 |
| AC-10.2 | 通過 | UI 工具軌跡只呈現 allowlisted 欄位與耗時。 |
| AC-10.3 | 通過 | 正式 RAG、XBRL 與結論分區。 |
| AC-10.4 | 通過 | 三類內建問題 fixture 與正式 smoke。 |
| AC-10.5 | 通過 | corpus／MCP／模型目錄端點與缺金鑰均於查詢前檢查；不健康即禁用查詢。 |
| AC-11.1 | 通過 | 18 題固定集覆蓋七種指定類型。 |
| AC-11.2 | 通過 | 正式重跑三類 duplicate 鍵皆為 0。 |
| AC-11.3 | 通過 | MCP discovery、六工具、非法參數與失敗契約測試。 |
| AC-11.4 | 通過 | 18 題固定評估分別報告檢索、metadata、URL；另四組人工標定固定案例重跑答案支持率／必要主張涵蓋率／引用正確率皆 100%，且明示非正式模型分數。 |
| AC-11.5 | 通過 | 偽造 URL／ID、未列名引用與明示 scope 外來源測試均判失敗；此自動規則不宣稱能判讀任意語意。 |
| AC-11.6 | 通過 | 本節列 corpus、parser／chunker／index／model 版本、測試、live 與限制。 |
| AC-12.1 | 通過 | 同步、解析、索引與 MCP 呼叫使用結構化事件；正式 MCP 的 72 筆抽樣事件均含 request ID、狀態與耗時，檢索經 MCP 亦有事件。 |
| AC-12.2 | 通過 | 限定 65 個專案文字檔與目前三個容器日誌掃描正式金鑰及金鑰／認證樣式均 0 命中，UI 只顯示允許欄位；已替換容器的歷史日誌不在本次掃描範圍。 |
| AC-12.3 | 通過 | 外部錯誤分類與清理後的 UI／MCP 訊息契約。 |
| AC-12.4 | 通過 | Demo 啟動檢查 corpus、index、MCP 與 OpenRouter 模型清單，並呈現模型狀態；三類正式推論另行通過。 |
| NFR-01 | 通過 | 單一 Windows 工作站完成正式與乾淨 corpus、索引及查詢。 |
| NFR-02 | 通過 | raw volume、關聯資料與版本化索引分離且可重建。 |
| NFR-03 | 通過 | 來源、parser／chunker、index build 與模型設定已記錄。 |
| NFR-04 | 通過 | active build 指標與 cursor 版本隔離整合測試。 |
| NFR-05 | 通過 | Windows／PowerShell 操作文件與實際執行。 |
| NFR-06 | 通過 | 單一 PostgreSQL、MCP、Streamlit，未加入範圍外基礎設施。 |
| NFR-07 | 通過 | 正式 UI smoke 記錄三題端到端及每工具耗時；尚無效能 SLA。 |

依上方明定的固定 Demo 驗證範圍，70 項 EARS 均有離線、正式資料或正式模型抽樣證據，31.5 可判通過。固定四組自動答案評分與正式模型三類題須分開解讀；正式模型仍可能因引用不足而安全降級，不能宣稱其任意回答或每次生成都完整。實際瀏覽器視覺檢查、所有引用遠端 HTTP 200、已替換容器的歷史日誌稽核亦未完成，不得混入已驗證證據。

## 任務 32：最終門檻

- 32.1：獨立測試資料庫完整 pytest 172 通過；compileall、`uv pip check`（74 個相容套件）、Compose config、MCP live／ready 及 Demo HTTP health 均成功。65 個限定專案文字檔與三個目前容器日誌的秘密掃描均 0 命中。
- 32.2：正式資料庫有 57／195／1,703／672 份 filing／章節／chunk／XBRL fact，active embedding 1,703；filing、section、chunk 重複鍵各 0。固定案例為敘事 6、XBRL 4、複合 4、邊界 2、安全 2，共 18；六個公開工具、三個 resources 及三類正式 Demo 題均已呼叫。重跑冪等證據沿用任務 30 的零新增／零更新與上述重複鍵檢查。
- 32.3：57 份正式 filing 的 SEC URL／accession 格式錯誤數 0，正式 E2E 所有顯示引用通過 SEC URL 驗證；範圍外來源及限定秘密掃描命中為 0。公開 MCP discovery 只有六個研究工具，ingestion／rebuild 工具為 0。這裡的「可解析」仍非逐 URL 遠端 HTTP 200 證明。
- 32.4：本紀錄分開列示離線 fixture、正式 SEC／資料庫、OpenRouter 正式模型 E2E 與未驗證界線。模型答案品質有生成變異，安全降級是通過失敗關閉條件，不是完整研究答案的成功率保證。
- 32.5：依專案規則未提交實作、未建立合併請求、未部署；這些行動仍須另行授權。
