# SEC Filing Research Agent

以 SEC 官方 filing 與 XBRL facts 為唯一研究來源的本機 Demo。系統把敘事問題交給 PostgreSQL 全文搜尋加 pgvector 的混合檢索，把數值問題交給結構化 XBRL 查詢；向量由 OpenRouter 的 `openai/text-embedding-3-small` 建立，研究答案由 OpenRouter Responses API 的 `openai/gpt-6-luna` 經 MCP 循序呼叫工具並驗證引用。

> 本專案只提供研究資訊，不是投資建議、價格預測或交易指令。

## 架構與邊界

- PostgreSQL 保存 filing metadata、章節、chunks、embeddings、XBRL facts 與 ingestion checkpoints。
- MCP Server 只發布六個唯讀研究工具及三個唯讀 resources；ingestion、migration 與索引重建不透過 MCP 發布。
- Streamlit Demo 是唯一發布到主機的服務，固定綁定 `127.0.0.1`；MCP 與 PostgreSQL 不發布連接埠。
- SEC 文件、工具輸出與模型輸出一律視為不可信資料。引用必須屬於本次 request，且 URL 必須通過 SEC HTTPS allowlist。
- 嵌入輸入、查詢文字、研究問題與取回的 SEC 公開片段都會送至 OpenRouter，並可能由其轉送給所選 OpenAI 模型供應端。不得把私密資料、秘密或未公開文件放入問題或 corpus。

Docker 官方說明指出，未指定主機位址的 published port 可能對外開放；綁定 `127.0.0.1` 時只有 Docker 主機可存取。因此 Compose 只有 Demo 使用 loopback mapping。[Docker port publishing](https://docs.docker.com/engine/network/port-publishing/#publishing-ports)

## Windows 前置需求

1. Windows 11、PowerShell 7、Docker Desktop（Linux containers）。
2. 本機直接執行時另需 Python 3.12 與 `uv`。
3. 可聯絡的 `SEC_USER_AGENT`，格式為產品名稱加聯絡 email。
4. OpenRouter API key（嵌入及研究答案）；不得寫入 Git、映像或 `compose.yaml`。

複製 `.env.example` 為 `.env`，至少替換：

```dotenv
SEC_USER_AGENT=sec-research-demo your-email@example.com
OPENROUTER_API_KEY=replace-with-secret
POSTGRES_PASSWORD=replace-with-long-random-password
```

`.env` 已被 Git 忽略。正式秘密應由環境或部署 secret store 注入；不要放在命令列歷史、原始碼、log 或測試 fixture。

## Docker Compose 快速開始

先驗證設定並啟動資料庫：

```powershell
docker compose config --quiet
docker compose up -d --build postgres
```

明確套用 migration，再啟動 MCP 與 Demo：

```powershell
docker compose run --rm mcp python -m sec_research.cli migrate
docker compose up -d mcp demo
```

開啟 <http://127.0.0.1:8501>。乾淨資料庫在尚未建立 corpus 與 active index 時，MCP `/health/live` 會正常，但 `/health/ready` 會回報 not ready，Demo 會阻止研究查詢。

Compose 以 PostgreSQL healthcheck 搭配 `depends_on.condition: service_healthy`，因為 Compose 只等待容器進入 running，不會自動等待服務可用。[Docker startup order](https://docs.docker.com/compose/how-tos/startup-order/#control-startup)

## 資料與索引命令

Ingestion 都是操作者明確執行的一次性命令；沒有 scheduler。原始 filing 存在 `raw-data` volume，PostgreSQL 存在 `postgres-data` volume。

```powershell
# 最近五個已完成會計年度：AAPL、MSFT、NVDA 的 10-K／10-Q 與 XBRL
docker compose run --rm mcp python -m sec_research.cli backfill

# 以相同範圍進行可重跑的增量同步
docker compose run --rm mcp python -m sec_research.cli sync

# 建立完整候選 embedding index，成功後才原子切換 active build
docker compose run --rm mcp python -m sec_research.cli rebuild-index

# 檢查服務與 readiness
docker compose exec mcp python -c "import httpx; print(httpx.get('http://127.0.0.1:8000/health/live').json()); print(httpx.get('http://127.0.0.1:8000/health/ready').json())"
```

SEC 自動化請求會使用 allowlisted 官方主機、宣告 User-Agent、禁止 redirect，並將總速率限制在每秒 10 次以下。正式 backfill 會下載 SEC 公開資料；`rebuild-index` 與向量查詢會呼叫 OpenRouter embeddings，執行前應確認資料傳送與費用邊界。

## 本機開發命令

```powershell
uv sync --frozen
$env:DATABASE_URL='postgresql://postgres:replace-me@127.0.0.1:5432/sec_research'
$env:SEC_USER_AGENT='sec-research-demo your-email@example.com'
$env:OPENROUTER_API_KEY='replace-with-secret'

uv run python -m sec_research.cli migrate
uv run python -m sec_research.cli backfill
uv run python -m sec_research.cli sync
uv run python -m sec_research.cli rebuild-index
uv run python -m sec_research.mcp_server
uv run streamlit run src/sec_research/app.py
```

## 驗證

```powershell
$env:TEST_DATABASE_URL='postgresql://postgres:replace-me@127.0.0.1:5432/sec_research'
uv run pytest
uv run python -m compileall -q src
uv pip check
docker compose config --quiet
```

單元、fixture E2E 與 MCP contract 測試不等於正式 SEC、OpenRouter 模型或完整 corpus 的 live 證據；這些結果必須分開報告。

### 固定案例答案支持度評分

```powershell
uv run python -m sec_research.answer_eval tests/fixtures/sec/answer_support_gold.json tests/fixtures/sec/answer_support_answers.json
```

四組離線合成案例涵蓋敘事、XBRL 數值、複合與跨期主張。標準檔逐項列出必要主張、來源 metadata 與**必須同時出現**的引用 ID；答案檔每行放一項主張，行末放 `[citation_id]`，並附 `citation_ids`、`citations`。評分器以 NFKC 與空白正規化比對標定主張及來源欄位，不接受額外或未受支持的主張。

通過門檻固定為主張支持率 `supported_claims / observed_claims = 100%`、必要主張涵蓋率 `supported_claims / expected_claims = 100%`、引用正確率 `valid_citations / used_citations = 100%`，且不得有任何案例或結構錯誤。命令輸出 JSON 報表；通過時結束碼 0，否則為 1。相同檔案重跑應得到相同結果。

隨附答案是人工撰寫的正向控制，SEC 形式的網址及數值均沿用合成測試資料；分數只證明評分器對**這組固定標定文字**的判定可重現，不證明網址實際可讀、真實 SEC 數值正確，也不是 OpenRouter 模型端到端成績。自由改寫的正確答案可能因未符合標定原子主張而失敗；使用於模型輸出時須另行標定其案例與答案，不能把此分數解讀為通用語意支持率。

## 停止服務

```powershell
docker compose stop
docker compose down
```

`down` 不會刪除 named volumes。只有在明確決定刪除本機 corpus 與資料庫後，才使用 `docker compose down --volumes`；該動作不可復原。
