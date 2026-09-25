# Plan: SEC Filing Research Agent

## 1. 文件狀態

- 規格名稱：`sec-filing-research-agent`
- 需求依據：[requirements.md](requirements.md)
- 設計依據：[design.md](design.md)
- 階段：固定 Demo 實作與驗證完成；提交、合併請求及部署待另行授權
- 能力順序：`sec-data-pipeline → filing-rag → sec-mcp-server → research-agent → demo-experience`

## 2. 實作邊界

- 僅實作 AAPL、MSFT、NVDA 最近五個已完成會計年度的 10-K／10-Q；8-K 僅即時查詢 metadata。
- 採 Python 3.12、`uv`、PostgreSQL 18 + pgvector、OpenRouter Responses API `openai/gpt-6-luna` 與 `openai/text-embedding-3-small`、官方 MCP Python SDK 2.x、Streamlit 與 Docker Compose。
- 不加入 Elasticsearch、專用向量資料庫、訊息佇列、Kubernetes、cross-encoder、獨立 router LLM、公開網路部署或 OAuth。
- 每個任務完成後才可進入其相依任務；各檢查點未通過時不得繼續。
- 本文件原為實作規劃；已授權的實作與正式資料操作完成，提交、合併請求及部署仍須另行授權。

## 3. 依賴順序

```text
專案基礎與設定
  → PostgreSQL schema
  → SEC client／ingestion／parser／XBRL
  → embedding／hybrid RAG
  → MCP tools／resources
  → OpenAI research agent
  → Streamlit Demo
  → Compose、完整 corpus 與端到端驗證
```

## Tasks

- [x] 1. 建立最小 Python 專案骨架與鎖定依賴
  - [x] 1.1 建立單一 `sec_research` package、Python 3.12 約束、`uv` lockfile 與 pytest 測試入口。
  - [x] 1.2 僅加入設計已核准的直接依賴；不得預先建立 repository、service、adapter 或 factory 抽象層。
  - [x] 1.3 建立 `.gitignore`，排除 `.env`、虛擬環境、cache、原始 SEC 資料、資料庫 volume 與測試輸出。
  - [x] 1.4 驗證：`uv sync --frozen`、`uv run python -m compileall src`、`uv run pytest --collect-only` 均成功。
  - [x] 1.5 依賴：無；預計檔案：`pyproject.toml`、`uv.lock`、`.gitignore`、`src/sec_research/__init__.py`。
  - [x] 1.6 需求追蹤：NFR-01、NFR-05、NFR-06。

- [x] 2. 建立型別化設定與秘密邊界
  - [x] 2.1 驗證 SEC、資料庫、MCP、OpenAI、RAG 與 Agent 的必要設定及安全預設值。
  - [x] 2.2 `OPENROUTER_API_KEY` 與資料庫密碼只能由環境或 secret store 注入；`.env.example` 僅保留 placeholder。
  - [x] 2.3 固定 `OPENROUTER_ANSWER_MODEL=openai/gpt-6-luna`、`OPENROUTER_EMBEDDING_MODEL=openai/text-embedding-3-small` 與 1,536 維索引不變量。
  - [x] 2.4 驗證：設定缺漏、非法限制值、秘密遮罩與安全預設值單元測試通過。
  - [x] 2.5 依賴：1；預計檔案：`src/sec_research/config.py`、`.env.example`、`tests/unit/test_config.py`。
  - [x] 2.6 需求追蹤：AC-12.2、AC-12.3、NFR-03。

- [x] 3. 建立 PostgreSQL schema、migration 與最小權限角色
  - [x] 3.1 建立 companies、filings、filing_sections、chunks、index_builds、chunk_embeddings、xbrl_facts、ingestion_runs、ingestion_items。
  - [x] 3.2 落實唯一鍵、外鍵、active index 單一性、`vector(1536)`、全文索引與 ingestion/query 分離角色。
  - [x] 3.3 migration 必須由明確命令執行，重跑不得破壞或重複 schema 物件。
  - [x] 3.4 驗證：乾淨資料庫 migrate、重跑 migrate、唯讀角色拒絕寫入、所有必要約束整合測試通過。
  - [x] 3.5 依賴：1、2；預計檔案：`migrations/001_initial.sql`、`src/sec_research/db.py`、`tests/integration/test_db_schema.py`。
  - [x] 3.6 需求追蹤：AC-03.1、NFR-02、NFR-04。

- [x] 4. 檢查點 A：專案與資料基礎
  - [x] 4.1 驗證：`uv sync --frozen`、compileall、設定測試與資料庫 schema 測試全部通過。
  - [x] 4.2 確認 repository 不含真實 secret、`.env`、raw filing 或資料庫資料。
  - [x] 4.3 確認 ingestion 與 query 角色權限分離，且 migration 可重跑。
  - [x] 4.4 通過後才可進入 SEC 資料管線；依賴：1～3。

- [x] 5. 實作 SEC allowlist client 與公司識別
  - [x] 5.1 僅允許 `www.sec.gov`、`data.sec.gov`，由 ticker／CIK／accession 組合 URL，不接受任意 URL 或 redirect 目標。
  - [x] 5.2 取得並驗證 ticker／CIK mapping 與 submissions schema；CIK 正規化為補零後 10 位字串。
  - [x] 5.3 實作必要 User-Agent、預設每秒 5 次且硬上限低於 10 次的共用限流、timeout 與有限重試分類。
  - [x] 5.4 驗證：fixture 測試涵蓋合法 mapping、非唯一 ticker、429／5xx、一般 4xx、非法 host、redirect 與敏感 header 遮罩。
  - [x] 5.5 依賴：2；預計檔案：`src/sec_research/sec_client.py`、`tests/unit/test_sec_client.py`、`tests/fixtures/sec/submissions.json`。
  - [x] 5.6 需求追蹤：AC-01.3、AC-02.1、AC-02.4～AC-02.6。

- [x] 6. 實作 ingestion run 狀態與原始資料保存
  - [x] 6.1 依 canonical scope 與 pipeline version 產生 intent hash；相同 scope 同時只允許一個執行。
  - [x] 6.2 落實 discovered、downloaded、parsed、indexed、failed、quarantined 狀態與可續跑的 item checkpoint。
  - [x] 6.3 原始檔依 CIK／accession 原子寫入，保存 SHA-256；相同 accession 與 hash 不重做下游工作。
  - [x] 6.4 驗證：中斷續跑、重複啟動 conflict、摘要計數與原子寫入整合測試通過。
  - [x] 6.5 依賴：3、5；預計檔案：`src/sec_research/ingest.py`、`src/sec_research/db.py`、`tests/integration/test_ingestion_state.py`。
  - [x] 6.6 需求追蹤：AC-01.1～AC-01.4、AC-03.1～AC-03.4、AC-12.1。

- [x] 7. 實作 filing 發現、下載與 identity 驗證切片
  - [x] 7.1 篩選預設三家公司、最近五個已完成會計年度、10-K／10-Q，並排除不在規格內的表單與修正版。
  - [x] 7.2 下載 primary HTML 與必要 metadata，驗證 content type、大小、CIK、accession、primary document 與來源 URL。
  - [x] 7.3 identity 或內容驗證失敗時隔離，不建立可檢索資料；暫時性失敗保留續跑位置。
  - [x] 7.4 驗證：固定 submissions／HTML fixture 的新增、略過、隔離、失敗與重跑不重複案例通過。
  - [x] 7.5 依賴：5、6；預計檔案：`src/sec_research/ingest.py`、`src/sec_research/sec_client.py`、`tests/integration/test_filing_download.py`、`tests/fixtures/sec/filing.html`。
  - [x] 7.6 需求追蹤：AC-02.2、AC-02.5～AC-02.7、AC-03.2～AC-03.4。

- [x] 8. 實作 SEC HTML 清理與章節辨識
  - [x] 8.1 移除 script、style、noscript、導覽、inline-XBRL hidden/header metadata，保留可見文字、數字與單位。
  - [x] 8.2 可靠辨識 10-K Item 1／1A／1C／7／8 與 10-Q Part I Item 1／2、Part II Item 1A，排除目錄假命中。
  - [x] 8.3 將表格轉成保留列順序的文字；缺章、順序衝突或低信心時標記失敗，不自動猜測。
  - [x] 8.4 驗證：多種固定 filing fixture 涵蓋成功、目錄假命中、缺章、異常順序與大型表格案例。
  - [x] 8.5 依賴：7；預計檔案：`src/sec_research/parser.py`、`tests/unit/test_parser.py`、`tests/fixtures/sec/10k.html`、`tests/fixtures/sec/10q.html`。
  - [x] 8.6 需求追蹤：AC-04.1～AC-04.4。

- [x] 9. 實作章節切塊與可追溯 metadata
  - [x] 9.1 以章節內 600～900 tokens、約 100 tokens overlap 為目標，優先使用段落與 table row 邊界。
  - [x] 9.2 每個 chunk 保存公司、CIK、ticker、form、日期、accession、section、順序、來源 URL 與相鄰 chunk 關聯。
  - [x] 9.3 產生穩定內容 hash；同一 section 重跑不得新增重複 chunk。
  - [x] 9.4 驗證：chunk 不跨 section、metadata 完整、順序可回溯、hash 穩定與重跑計數不變。
  - [x] 9.5 依賴：3、8；預計檔案：`src/sec_research/rag.py`、`tests/unit/test_chunking.py`、`tests/integration/test_chunk_persistence.py`。
  - [x] 9.6 需求追蹤：AC-04.5～AC-04.7、AC-11.2。

- [x] 10. 實作 XBRL facts 下載、正規化與去重
  - [x] 10.1 由 SEC company facts 取得支援 concept，保存 taxonomy、concept、value、unit、期間、fy、fp、form、filed 與 accession。
  - [x] 10.2 依設計組合唯一鍵去重；不同單位、期間與 custom taxonomy 不得靜默合併。
  - [x] 10.3 將 XBRL 納入同一 ingestion 摘要與可續跑狀態。
  - [x] 10.4 驗證：重複 facts、多單位、custom taxonomy、缺 accession 與暫時性 SEC 失敗 fixture 通過。
  - [x] 10.5 依賴：3、5、6；預計檔案：`src/sec_research/ingest.py`、`src/sec_research/db.py`、`tests/integration/test_xbrl_ingestion.py`、`tests/fixtures/sec/companyfacts.json`。
  - [x] 10.6 需求追蹤：AC-02.3、AC-07.1～AC-07.4。

- [x] 11. 檢查點 B：SEC 資料管線
  - [x] 11.1 驗證：fixture pipeline 可由 discovery 完成至 filing、section、chunk 與 XBRL facts 持久化。
  - [x] 11.2 相同範圍連續執行兩次後，duplicate filing／section／chunk 為 0。
  - [x] 11.3 隔離內容不得出現在 corpus；失敗摘要須含 accession、stage 與非敏感 error code。
  - [x] 11.4 通過後才可建立 embedding 與檢索；依賴：5～10。

- [x] 12. 實作 OpenRouter embedding 與版本化 index build
  - [x] 12.1 批次呼叫 `openai/text-embedding-3-small`，驗證每筆向量為 1,536 維並記錄 provider、model、dimension、chunker version。
  - [x] 12.2 以 `chunk_text_sha256 + index_build_id` 保證同 build 不重複；失敗批次不得切換 active index。
  - [x] 12.3 候選 build 完整且通過基本檢查後才原子切換 active pointer，舊 build 保留可回復性。
  - [x] 12.4 驗證：fake embedding boundary 涵蓋成功、部分失敗、錯誤維度、重跑與 active build 原子切換。
  - [x] 12.5 依賴：2、3、9；預計檔案：`src/sec_research/rag.py`、`src/sec_research/db.py`、`tests/integration/test_index_build.py`。
  - [x] 12.6 需求追蹤：AC-04.7、NFR-02～NFR-04。

- [x] 13. 實作 metadata-first hybrid retrieval 與 cursor
  - [x] 13.1 先驗證 ticker、form、日期、section、`top_k`，再套用 metadata SQL 篩選。
  - [x] 13.2 PostgreSQL FTS 與 pgvector 各取最多 50 候選，以 RRF `k=60` 合併並回傳前 8 筆。
  - [x] 13.3 回傳全文排名、向量排名、融合排名、metadata、chunk text、相鄰識別與 opaque cursor。
  - [x] 13.4 驗證：公司／期間／form／section 篩選 100% 正確、cursor 穩定、非法 filter 被拒絕、active build 不混用。
  - [x] 13.5 依賴：3、12；預計檔案：`src/sec_research/rag.py`、`src/sec_research/db.py`、`tests/integration/test_hybrid_retrieval.py`。
  - [x] 13.6 需求追蹤：AC-05.1～AC-05.5、NFR-04。

- [x] 14. 建立 RAG 固定評估集與 citation resolver
  - [x] 14.1 建立至少 6 題敘事檢索 fixture，涵蓋公司、期間、form、section、無結果與跨期比較。
  - [x] 14.2 citation 必須解析為公司、form、period end、filing date、accession、section 與 SEC 官方 URL。
  - [x] 14.3 計算 metadata 正確率、Recall@8 與 URL 可解析率；門檻分別為 100%、至少 85%、100%。
  - [x] 14.4 驗證：評估命令對固定 corpus 可重現，且刻意錯誤的範圍與引用會使案例失敗。
  - [x] 14.5 依賴：13；預計檔案：`tests/e2e/test_rag_evaluation.py`、`tests/fixtures/sec/evaluation_cases.json`、`src/sec_research/rag.py`。
  - [x] 14.6 需求追蹤：AC-09.1、AC-09.2、AC-11.1、AC-11.4、AC-11.5。

- [x] 15. 檢查點 C：Filing RAG
  - [x] 15.1 index build 重跑無重複向量，失敗 build 不會污染 active index。
  - [x] 15.2 驗證：固定評估集達成 metadata 100%、Recall@8 至少 85%、citation URL 100%。
  - [x] 15.3 確認尚未加入 reranker、query rewrite、multi-query 或第二套向量服務。
  - [x] 15.4 通過後才可公開 MCP 檢索能力；依賴：12～14。

- [x] 16. 建立 MCP 共用 envelope、錯誤與 citation 契約
  - [x] 16.1 定義 `ok`、`partial`、`not_found` envelope，以及 request ID、data、citations、warnings、page。
  - [x] 16.2 統一 INVALID_ARGUMENT、DEPENDENCY_UNAVAILABLE、RATE_LIMITED、INDEX_UNAVAILABLE、INTERNAL_ERROR 語意與 retryability。
  - [x] 16.3 所有輸入在 MCP boundary 驗證；所有錯誤不得洩漏秘密、主機路徑或未清理堆疊。
  - [x] 16.4 驗證：成功、找不到、非法輸入、暫時性依賴與內部錯誤 contract vectors 通過。
  - [x] 16.5 依賴：3；預計檔案：`src/sec_research/mcp_server.py`、`tests/contract/test_mcp_envelope.py`。
  - [x] 16.6 需求追蹤：AC-06.3～AC-06.6、AC-12.3。

- [x] 17. 實作 `list_filings` 與 `get_corpus_status`
  - [x] 17.1 `list_filings` 支援設計限定的 ticker、form、日期、limit 與 opaque cursor。
  - [x] 17.2 `get_corpus_status` 回傳 scope、計數、失敗／隔離數、active build 與最後同步時間，且不接受參數。
  - [x] 17.3 所有查詢使用唯讀角色、參數化 SQL 與 statement timeout。
  - [x] 17.4 驗證：合法分頁、空結果、非法日期／ticker／form／limit 與唯讀權限 contract 測試通過。
  - [x] 17.5 依賴：3、11、15、16；預計檔案：`src/sec_research/mcp_server.py`、`tests/contract/test_mcp_catalog_tools.py`。
  - [x] 17.6 需求追蹤：AC-06.1～AC-06.4、AC-06.6。

- [x] 18. 實作 `get_latest_filings`
  - [x] 18.1 只由 SEC submissions API 即時查詢 10-K、10-Q、8-K metadata；不得下載或索引 8-K 全文。
  - [x] 18.2 回傳本機 corpus 是否已有該 accession、SEC URL 與取得時間。
  - [x] 18.3 套用 SEC allowlist、User-Agent、限流、timeout、依賴錯誤與最多 20 筆限制。
  - [x] 18.4 驗證：fake SEC boundary 涵蓋三種 forms、limit、corpus 命中、429 與 dependency unavailable。
  - [x] 18.5 依賴：5、16、17；預計檔案：`src/sec_research/mcp_server.py`、`src/sec_research/sec_client.py`、`tests/contract/test_mcp_latest_filings.py`。
  - [x] 18.6 需求追蹤：AC-06.2～AC-06.5。

- [x] 19. 實作 `search_filing_sections` 與 `read_filing_section`
  - [x] 19.1 search 工具完整映射 metadata-first hybrid retrieval，限制 query、tickers、forms、sections 與 `top_k<=12`。
  - [x] 19.2 read 工具以 accession／section 精確分頁讀取，單次最多 20,000 字元並回傳引用。
  - [x] 19.3 找不到資料回傳 `not_found`；非法 cursor／section／accession 不得執行含糊查詢。
  - [x] 19.4 驗證：排序證據、相鄰識別、分頁、引用、無資料與非法參數 contract 測試通過。
  - [x] 19.5 依賴：13、14、16；預計檔案：`src/sec_research/mcp_server.py`、`src/sec_research/rag.py`、`tests/contract/test_mcp_rag_tools.py`。
  - [x] 19.6 需求追蹤：AC-05.1～AC-05.5、AC-06.2～AC-06.5、AC-09.1、AC-09.2。

- [x] 20. 實作 `get_financial_metric` 與 MCP Resources
  - [x] 20.1 財務工具驗證 ticker、1～5 個 concepts、期間、forms 與 unit，直接查詢 XBRL facts，不走向量檢索。
  - [x] 20.2 多單位、custom taxonomy 或無法安全正規化時回傳 warning／partial／not_found，不自行推測。
  - [x] 20.3 提供 filing、section 與 corpus status 三種唯讀 `sec://` resources；不得代理任意 URL。
  - [x] 20.4 驗證：value、unit、period、accession 100% 正確，resources 與對應 tools 資料一致。
  - [x] 20.5 依賴：10、16、17；預計檔案：`src/sec_research/mcp_server.py`、`tests/contract/test_mcp_financial_tools.py`、`tests/contract/test_mcp_resources.py`。
  - [x] 20.6 需求追蹤：AC-06.2～AC-06.5、AC-07.1～AC-07.4、AC-09.3。

- [x] 21. 完成 MCP Streamable HTTP、discovery 與健康端點
  - [x] 21.1 以官方 Python SDK 2.x 提供 `/mcp`、server discovery、`tools/list`、`resources/list`；正式傳輸使用 Streamable HTTP。
  - [x] 21.2 `/health/live` 僅判斷程序；`/health/ready` 驗證設定、DB schema、唯讀角色與 active index。
  - [x] 21.3 MCP 與 PostgreSQL 預設只允許 loopback／Docker internal network，不綁定 LAN 或 Internet。
  - [x] 21.4 驗證：MCP Inspector／client smoke、六個 tools、三個 resources、schema、錯誤語意與 health tests 通過。
  - [x] 21.5 依賴：16～20；預計檔案：`src/sec_research/mcp_server.py`、`tests/contract/test_mcp_discovery.py`、`tests/integration/test_mcp_health.py`。
  - [x] 21.6 需求追蹤：AC-06.1～AC-06.6、AC-12.4。

- [x] 22. 檢查點 D：完整 MCP Server
  - [x] 22.1 驗證：六個工具、三個 resources、discovery、input／output schema 與所有錯誤向量通過契約測試。
  - [x] 22.2 ingestion／rebuild 未透過 MCP 暴露，所有 public tools 使用唯讀角色。
  - [x] 22.3 未發布 MCP／PostgreSQL 至 LAN 或 Internet，且 health 語意與 readiness 相符。
  - [x] 22.4 通過後才可接 OpenAI Research Agent；依賴：16～21。

- [x] 23. 建立 MCP 到 OpenAI strict function schema 轉換
  - [x] 23.1 啟動時由 discovery allowlist 產生六個同名 function tools，不另寫會漂移的 domain schema。
  - [x] 23.2 每層 object 設 `additionalProperties:false`、所有 properties 列為 required，概念選填欄位使用 nullable。
  - [x] 23.3 MCP 呼叫前移除 `null` 以套用 server defaults；任一 schema 不相容時 readiness fail closed。
  - [x] 23.4 驗證：逐工具比較名稱、欄位、enum、format、limit、optional-to-null 與無參數工具 schema。
  - [x] 23.5 依賴：21、22；預計檔案：`src/sec_research/agent.py`、`tests/contract/test_openai_tool_schemas.py`。
  - [x] 23.6 需求追蹤：AC-06.1、AC-06.4、AC-12.4。

- [x] 24. 實作 Responses API 循序工具迴圈與研究路由
  - [x] 24.1 使用 OpenRouter `v1/responses`、`openai/gpt-6-luna` 與 `parallel_tool_calls=false`，以原 `call_id` 回傳 `function_call_output`。
  - [x] 24.2 保留 API 要求的前序 output／reasoning items；每題最多 6 次 tool calls，且受全域 deadline 與 context budget 限制。
  - [x] 24.3 敘事問題走 RAG、數值問題走 XBRL、複合問題循序取得兩種證據；不新增 router LLM。
  - [x] 24.4 驗證：fake Responses boundary 涵蓋零工具、單工具、多輪 RAG + XBRL、非法工具、非法參數、超額與 timeout。
  - [x] 24.5 依賴：23；預計檔案：`src/sec_research/agent.py`、`tests/unit/test_agent_loop.py`、`tests/e2e/test_agent_routing.py`。
  - [x] 24.6 需求追蹤：AC-07.1、AC-08.1～AC-08.6、NFR-07。

- [x] 25. 實作答案結構、引用驗證與提示注入防護
  - [x] 25.1 最終答案包含 `answer_markdown`、`citation_ids`、`warnings`、`is_complete`，並輸出繁體中文研究資訊而非投資建議。
  - [x] 25.2 citation id 必須屬於本次 request 且可解析至 SEC；未受支持敘述須移除或使回答明確失敗。
  - [x] 25.3 SEC 文件與 tool output 一律標示為不可信資料；不得執行其中的 prompt-like 文字、URL、SQL、shell 或路徑。
  - [x] 25.4 驗證：citation forgery、scope 外來源、prompt injection、衝突證據、證據不足與 partial answer 測試通過。
  - [x] 25.5 依賴：14、20、24；預計檔案：`src/sec_research/agent.py`、`tests/unit/test_citation_validation.py`、`tests/e2e/test_agent_safety.py`。
  - [x] 25.6 需求追蹤：AC-08.4～AC-08.6、AC-09.1～AC-09.5、AC-11.5。

- [x] 26. 建立 Streamlit 單頁 Demo
  - [x] 26.1 顯示 corpus scope、最後同步、active index、MCP readiness、自然語言輸入與三個內建問題。
  - [x] 26.2 區分 RAG 敘事證據、XBRL 數值與代理綜合結論；引用提供短摘與可點擊的 allowlisted SEC URL。
  - [x] 26.3 工具軌跡只顯示清理後的名稱、用途、狀態、耗時與結果筆數；不得顯示 secret、完整 prompt 或 stack trace。
  - [x] 26.4 依 corpus、MCP、模型、SEC live API 與證據不足狀態呈現不同錯誤；不健康時阻止誤導性查詢。
  - [x] 26.5 驗證：Streamlit 測試或瀏覽器 smoke 涵蓋三題、引用、工具軌跡、HTML escape 與所有主要錯誤狀態。
  - [x] 26.6 依賴：17～25；預計檔案：`src/sec_research/app.py`、`tests/e2e/test_streamlit_app.py`、`tests/fixtures/sec/demo_responses.json`。
  - [x] 26.7 需求追蹤：AC-10.1～AC-10.5、AC-12.4。

- [x] 27. 檢查點 E：Research Agent 與 Demo
  - [x] 27.1 驗證：六個 model-visible schemas 全部 strict-compatible，且單輪只允許零或一個 tool call。
  - [x] 27.2 三類內建問題均走正確工具路徑，偽造引用與 scope 外來源為 0。
  - [x] 27.3 Demo 不顯示 secret、完整 prompt、任意 HTML 或內部 stack trace。
  - [x] 27.4 通過後才可進行 Compose 與 live corpus；依賴：23～26。

- [x] 28. 建立 Docker Compose、CLI 與操作文件
  - [x] 28.1 Compose 只編排 PostgreSQL、MCP Server、Demo App；ingestion 使用一次性明確命令，不建立 scheduler。
  - [x] 28.2 只發布 Demo App loopback port；MCP／PostgreSQL 維持 internal network，raw store 與 DB 使用持久 volume。
  - [x] 28.3 提供 migrate、backfill、sync、rebuild-index、MCP、Demo、測試與關閉命令，以及 Windows 執行前提。
  - [x] 28.4 文件明示 SEC User-Agent、OpenRouter 與模型供應端資料傳送邊界、秘密注入、研究用途與非投資建議限制。
  - [x] 28.5 驗證：`docker compose config --quiet`、乾淨啟動、health/readiness、明確 migration 與 graceful stop 成功。
  - [x] 28.6 依賴：3、11、15、21、26；預計檔案：`compose.yaml`、`Dockerfile`、`README.md`、`.env.example`。
  - [x] 28.7 需求追蹤：AC-10.5、AC-12.2～AC-12.4、NFR-01、NFR-05、NFR-06。

- [x] 29. 完成離線自動化與安全驗證
  - [x] 29.1 執行 unit、integration、MCP contract、Responses contract、E2E 與安全測試，不以 mocks 取代後續 live 證據。
  - [x] 29.2 安全案例涵蓋任意 URL、private IP、非法 redirect、超長 query、SQL metacharacter、未知 tool、prompt injection 與 citation forgery。
  - [x] 29.3 執行 compileall、依賴一致性與 secrets scan；掃描結果不得包含真實 API key、password、token 或 SEC/OpenAI headers。
  - [x] 29.4 將 `openai/gpt-6-luna` 答案生成改走既有 OpenRouter API 金鑰與 Responses 端點；以自動測試驗證設定與模型路由，並與正式模型呼叫證據分開記錄。
  - [x] 29.5 驗證：`uv run pytest`、`uv run python -m compileall src`、`uv pip check` 與限定檔案 secrets scan 全部通過。
  - [x] 29.6 依賴：27、28；預計檔案：既有測試、`pyproject.toml`、`README.md`，不得為通過檢查而刪除或弱化測試。
  - [x] 29.7 需求追蹤：AC-11.1～AC-11.5、AC-12.1～AC-12.4。

- [x] 30. 建置正式 Demo corpus 並執行 live SEC smoke
  - [x] 30.1 以核准命令回填 AAPL、MSFT、NVDA 最近五個已完成會計年度的 10-K／10-Q 與 XBRL facts。
  - [x] 30.2 分別記錄新增、更新、略過、隔離、失敗、filing、section、chunk、fact 與 active index build。
  - [x] 30.3 各公司至少實際查一筆 submissions，下載一份已知 filing，驗證 User-Agent、限流、URL allowlist、schema 與 SEC URL。
  - [x] 30.4 執行完整 18 題評估集並達成 metadata 100%、Recall@8 至少 85%、XBRL fixture 100%、citation URL 100%、scope 外來源 0。
  - [x] 30.5 驗證：相同 backfill 再跑一次後 duplicate filing／section／chunk 為 0；live 與離線結果分開報告。
  - [x] 30.6 依賴：11、15、22、29；預計產物：本機 `data/raw/`、PostgreSQL volume、清理後驗證摘要；不得提交 raw corpus。
  - [x] 30.7 需求追蹤：AC-01.2、AC-02.1～AC-02.7、AC-03.1～AC-03.4、AC-11.2、AC-11.4～AC-11.6。

- [x] 31. 執行完整 Demo E2E 與可重現性收尾
  - [x] 31.1 實際呼叫 MCP discovery、六個 public tools、三個 resources 與三類內建 Demo 問題。
  - [x] 31.2 驗證每題工具路徑、繁體中文答案、證據分區、引用解析、警告、端到端耗時與各工具耗時。
  - [x] 31.3 輸出資料範圍、parser／chunker／index／model 版本、測試結果、live 證據與尚未驗證限制。
  - [x] 31.4 以乾淨環境依 README 重建服務；不得以本機殘留 cache、未記錄設定或手動 DB 修改才能成功。
  - [x] 31.5 驗證：完整 E2E 結果符合 requirements.md 的 63 項使用者故事驗收條件與 7 項非功能需求（共 70 項 EARS）。
  - [x] 31.6 依賴：28～30；預計產物：清理後驗證摘要與操作紀錄，不提交 secrets、raw filing 或完整 prompts。
  - [x] 31.7 需求追蹤：AC-08.1～AC-12.4、NFR-01～NFR-07。

- [x] 32. 檢查點 F：Demo 完成門檻
  - [x] 32.1 驗證：`uv run pytest`、compileall、依賴檢查、secrets scan、Compose config 與 health/readiness 全部通過。
  - [x] 32.2 正式 corpus、18 題評估、六工具／三 resources、三類 Demo 問題與重跑冪等證據全部具備。
  - [x] 32.3 所有引用可解析至 SEC、scope 外來源為 0、秘密洩漏為 0、公開 ingestion tool 為 0。
  - [x] 32.4 清楚區分離線測試、live SEC、OpenRouter 模型 E2E 與尚未驗證項目；任一缺口不得宣稱完成。
  - [x] 32.5 完成後才可依另行授權提交實作、建立合併請求或部署；依賴：29～31。

## Rules & Tips

- OpenRouter Responses API 不保存對話狀態；多輪工具呼叫須在每次請求附上完整前序 response items 與對應 `call_id` 的工具結果。
- 模型與 embedding 共用 `OPENROUTER_API_KEY`；不得回退至直接 OpenAI API 或將金鑰寫入專案檔案。
- 固定離線答案評分與正式模型抽樣結果須分開報告；引用 ID 合法不等於語意支持，正式答案仍須對 SEC 原文或 XBRL 逐項抽核，證據不足時降級。

## 4. 規劃完成定義

- [x] `requirements.md`、`design.md` 與本 `tasks.md` 互相一致。
- [x] 所有實作任務都有驗收、驗證、依賴與需求追蹤。
- [x] 任務依賴沒有循環，所有相依工作均排在使用者之前。
- [x] 六個檢查點覆蓋基礎、資料管線、RAG、MCP、Agent／Demo 與最終完成門檻。
- [x] 規劃完成當時尚未開始程式實作、資料下載、雲端呼叫或部署；目前進度以上方任務清單為準。
