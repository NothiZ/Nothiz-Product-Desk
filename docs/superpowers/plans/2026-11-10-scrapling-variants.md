# Scrapling product variant repair implementation plan

**Goal:** Keep the current AI writer and async pipeline, use Scrapling for browser fetching, and verify one previously failed Alibaba product.

**Architecture:** Preserve the async Scraper context manager, scrape(url) return schema, Excel columns, and existing AI modules. Use reusable Scrapling browser sessions with platform login states. Extract real SKU dimensions from embedded product data and current/legacy SKU markup; expand collapsed options when necessary. Do not infer variant values with an LLM.

**Tech stack:** Python, Scrapling browser fetchers, BeautifulSoup, unittest, pandas/openpyxl.

## Tasks

- [x] Establish a runnable local virtual environment; read one failed URL from products.xlsx using its debug product ID. Do not call the AI API or overwrite products_output.xlsx.
- [x] Capture the failed product with Scrapling and inspect actual SKU HTML and embedded data before selecting parsing rules.
- [x] Add small offline regression fixtures/tests proving current SKU extraction, legacy JSON fallback, and exclusion of non-product option data; run them against the existing parser to see the failure.
- [x] Replace browser lifecycle with reusable Scrapling async sessions and adapt login.py. Keep the main async pipeline and AI writer behavior unchanged.
- [x] Implement the proven SKU rules and expansion waits. Preserve the current output schema and add dependency/install instructions.
- [x] Verify offline tests, imports, existing async caller compatibility, and one live failed URL. Save a separate debug result with actual dimensions/counts and report limitations accurately.

## Acceptance

- ai_writer.py and llm_client.py remain unchanged.
- run.py retains existing AI generation/concurrency behavior; do not run the full workbook.
- A previously failed product yields actual selectable variant values, verified against its browser data.
- Existing login state files are reused without printing cookies or secrets.
- Failed/missing SKU extraction produces a useful diagnostic and never fabricated options.

## Verification results (2026-11-10)

- Reproduced the failure on product 1601683518137: existing parser returned {}, while the same page DOM and detailData contained 颜色: 其它, 粉色.
- Added a sanitized HTML fixture from that product. Initial tests reproduced the missing current DOM/embedded SKU support and legacy browser use.
- Offline suite: 13 tests pass, including malformed embedded data fallback, delayed expansion, callback retry recovery, and Taobao/Tmall state isolation.
- Live Scrapling single-product check: HTTP 200, 颜色 = 其它, 粉色 (2 options), ~10 seconds. Price and description were also populated.
- AI writer, LLM client and run.py remain unchanged; no AI request or batch workbook write was made.
- Read-only code review completed and the three reproduced findings were fixed with regression tests.
- Fresh login flows and live Taobao/Tmall fetching were not exercised, because they require user account interaction and are outside the requested single-product check.
