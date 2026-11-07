# MEXC AI — persistent paper worker

Current implementation is **PAPER ONLY**. No live order execution. No measured profitability claim.

The worker reads the actual MEXC USDT futures account with read-only credentials. It seeds a separate simulation once from observed available USDT; restarts restore the simulation ledger. Subsequent real deposits are shown as real account changes, and never overwrite simulated PnL. Legacy `STARTING_CAPITAL_TRY` does not size worker trades. TRY conversion is not used by this worker.

## Local setup

Python 3.11+ is required (this checkout has a Python 3.12 `.venv`).

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# Fill secrets locally; never commit .env.
.venv/bin/pytest -q
.venv/bin/python -m apps.worker.main
```

In another terminal:

```sh
.venv/bin/uvicorn apps.api.main:app --host 127.0.0.1 --port 8080
```

`GET /status` exposes real account, simulated equity, positions, last reason, AI status and estimated spend. `GET /equity` separates real and simulated balances. `GET /ready` checks worker freshness; `/health` only checks the API process. Legacy equity deposit/floor/reset mutations return 409 to prevent fictitious account funding.

Required local environment: `MEXC_API_KEY`, `MEXC_API_SECRET` (futures account/trade **reading** permissions), `ANTHROPIC_API_KEY`. Missing or invalid account data blocks entries; Claude failures and missing candidates result in HOLD. No credentials are included in this checkout.

## Telegram

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, then send `/start` to your bot. Only the configured chat can request `/durum` or `/status`. Opens/closes include PAPER labeling, SL/TP, event equity, current observed real account and simulated balance. Hourly status is configurable. Failed deliveries remain queued. Delivery is at least once: a crash after Telegram accepts a message may cause a duplicate on restart. Disable any existing webhook before using polling. Bot token must remain private.

## Defaults and controls

- USDT linear contracts only, complete exchange metadata required; unsupported custom tiers are rejected.
- Maximum 3x leverage, 1% planned risk per trade including modeled stop slippage/fees; exchange minimum sizes round down or reject.
- Maximum two simultaneous simulated positions; duplicate closed-candle signals are persisted.
- Daily 2% equity-loss halt and 8% peak drawdown halt. A simulated liquidation halts further entries. Daily reset uses Europe/Istanbul.
- Touch `data/STOP` to block new entries; existing simulated positions continue monitoring. In Compose: `docker compose --env-file .env -f infrastructure/docker-compose.yml exec worker touch /app/data/STOP`.
- Claude evaluation at least 300 seconds apart, only on qualified candidates. Estimated spend/reservations persist across restarts, defaults $2/day and $24 total. Provider-side spending limits are still needed: prices are configurable estimates, not an account billing authority. Do not change model without updating prices.

## Hetzner paper deployment

```sh
docker compose --env-file .env -f infrastructure/docker-compose.yml config --quiet
docker compose --env-file .env -f infrastructure/docker-compose.yml up --build -d
docker compose --env-file .env -f infrastructure/docker-compose.yml ps
curl --fail http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/status
```

Compose forces paper mode and live=false, binds API to localhost, and persists the SQLite worker ledger in `mexc_ai_runtime`. Run exactly one worker for this database; a process lock rejects duplicates. Back up the runtime volume with SQLite backup facilities. Never use `down -v` on a funded simulation you want to retain. Keep API private behind SSH/VPN. Deployment has not been exercised on Hetzner in this session.

## Validation limits

This is a paper research implementation, **not live-ready**. Stops are observed simulated triggers, not exchange protective orders. Fleet mode separates price supervision from market scanning, Claude and Telegram; legacy single-account mode remains sequential. Intracycle stop/TP touches and outage paths cannot be reconstructed from endpoint snapshots. Liquidation is a conservative isolated approximation, not exact MEXC risk-tier parity; funding uses observed mark price rather than historical settlement price. Partial fills and depth-based execution are not modeled. Backtests, walk-forward results, a web dashboard and a 24-hour connected paper run remain outstanding. No return or liquidation-avoidance guarantee follows from unit tests.

The legacy `trading.pipeline.TradingPipeline` remains for scaffold compatibility/tests. The executable worker uses `trading.runtime.runner.PaperRunner`; do not run the legacy pipeline for operational paper evaluation.

Model selection and prices: https://platform.claude.com/docs/en/about-claude/pricing
MEXC contract auth and assets: https://mexcdevelop.github.io/apidocs/contract_v1_en/
Telegram API: https://core.telegram.org/bots/api

## Multi-account Telegram + text/VLM comparison

When `ACCOUNTS_FILE=secrets/accounts.json` exists, the worker runs Fleet mode. One shared Telegram bot routes private users to separate server-provisioned MEXC accounts; unauthorized users/callbacks cannot see balances or change settings. No website is required. **API keys/secrets are not collected through Telegram cloud chats.** Set them in server `.env` and register only references and user IDs. Never enable withdraw/transfer permissions for this application.

1. Set one `TELEGRAM_BOT_TOKEN`, one `ANTHROPIC_API_KEY`, `AHMET_MEXC_KEY`, `AHMET_MEXC_SECRET`; add corresponding friend variables later. Keep `.env` mode 600.
2. Each user sends `/start` to the bot. With the worker stopped, `python -m trading.control.setup --identities` prints pending private numeric IDs only. Verify whose ID is whose before registering; strangers messaging a public bot are not automatically authorized.
3. Register locally on server:

```sh
python -m trading.control.setup --account ahmet --telegram-id YOUR_NUMERIC_ID --key-env AHMET_MEXC_KEY --secret-env AHMET_MEXC_SECRET
```

Repeat with `--account friend`, their ID and `FRIEND_MEXC_KEY`/`FRIEND_MEXC_SECRET`. `accounts.example.json` shows the schema. Registry is mounted read-only into Compose. The bot must have no webhook and exactly one poller. Local shell uses `.venv/bin/python` if no activated environment.

Telegram buttons: start entries, pause entries, text/VLM version, risk 0.5%/1%, status and positions. `/version text` or `/version vlm` switches and pauses; `/run` starts the selected version. Pause does not close positions. Price supervision continues for both variants, and `/positions` includes inactive-variant open positions. Changing Telegram settings never enables real orders.

Each account **and version** owns separate paper ledger, deduplication and bounded per-symbol memory under `data/<account>/<version>/`. Switching preserves earlier results. A version first starts from the observed USDT available funds at that moment, so sequential runs can have different start conditions; this is not a synchronized causal A/B experiment. For fair comparisons use identical replay data/start conditions in a separate evaluation run. All versions are PAPER only.

Both variants use identical deterministic closed-candle PA features (confirmed swings, BOS, sweeps, historical FVGs) and the same decision schema. VLM adds the exact MEXC 4h/15m/5m PNGs; text receives numeric OHLCV/features. Charts are generated for both variants so evidence can be attached to Telegram entries; text does not send them to Claude. Open/close and delayed funding adjustment messages are queued. Open messages contain rationale and chart evidence; paths/hashes/timestamps are saved. Retain charts until delivery, monitor disk space; automated retention is not implemented.

Claude makes one decision call per qualified evaluation, at least 300 seconds apart per version; unchanged closed-candle bundles are skipped. A bounded last-six-decision journal, watch note, invalidation and latest outcome are re-supplied on every request. This is persistent working memory, not an infinite context window. Strict schema/unknown signal failures HOLD. Watch decisions do not force future entries.

Fleet uses one shared persistent budget database for all users/variants. Set `ANTHROPIC_DAILY_BUDGET_USD=3` and `ANTHROPIC_TOTAL_BUDGET_USD=24`; seven 24-hour calendar-day budgets correspond to $21 estimated calls, subject to timezone day boundaries and total cap. Provider prices/billing may differ; the API key's other uses are outside this tracker. Configure a provider-side spend limit too. Budget exhaustion pauses AI entries but not position supervision. Concurrent accounts share this pool; per-user quotas/fairness are not implemented.

Independent quote-protection threads continue during analysis, funding and Telegram calls. A fresh successful health check is required immediately before new risk; a new position must receive a supervision tick before another entry. Price polling is still not an exchange-held stop, and network outages remain observable limitations. Funding history is reconciled independently for open **and closed** positions; missed settlements use current observed fair price, so cost modeling is approximate. Inactive variants retain supervision and funding reconciliation.

The old API `/status` is for single-worker mode; Fleet is operated privately through Telegram and per-account state. No account-selection HTTP endpoint is exposed. Update existing `.env` explicitly: copying an example does not overwrite old settings.

## Telegram linking and manual close (updated)

Initial server registry may now contain empty credential environment references; it allowlists Telegram identities only. `ACCOUNT_VAULT_KEY` enables `/connect` and must be generated once, kept private and backed up. Example key generation (run only in your own server terminal, then place output in server `.env`):

```sh
.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

`/connect` pauses entries and accepts the next single JSON message within 180 seconds: `{"api_key":"...","api_secret":"..."}`. Only the account owner's private chat is accepted. Messages are best-effort deleted and never echoed/queued/passed to Claude; credentials are validated against futures account/positions reads and encrypted at rest. **Telegram bot chats are cloud chats, not end-to-end secret chats.** Local server provisioning remains an alternative. Message deletion does not guarantee erasure from Telegram or device copies. Use no withdrawal permissions.

`/disconnect` pauses entries and removes the local active vault record. Open positions must first be closed and active analysis/funding work must finish. It does not revoke keys on MEXC or securely erase backups/WAL. Reconnect creates a new isolated paper session; old records are retained for audit and never adopted as the new wallet's positions. Deleting a Telegram message alone does not disconnect.

`/stop` stops new entries, keeping price supervision and reporting alive. `/close text BTC_USDT` (or `vlm`) requests a confirmed manual paper close; `/closeall` confirms closure across both versions and pauses entries. `/positions` includes individual close buttons. Confirmations expire, bind to the exact position and are owner checked. A quote outage leaves the close pending and reports that it has not executed; it does not invent a fill. None of these controls place live exchange orders.

`POSITION_UPDATE_SECONDS=60` posts active position status periodically; `/positions` queries the latest supervised snapshot. These are timestamped observations, not tick-by-tick guarantees. Trade opens include chart album, entry/SL/TP, rationale, paper balance and latest real-account observation. Manual/SL/TP closes include net result. Slow model analysis cannot block price protection, but Telegram network delivery may be delayed.

Set `AI_BUDGET_ENABLED=false` to disable local daily/total caps while retaining spend accounting. Provider credit errors generate a Telegram warning and block AI entries; supervision continues. A provider-side limit still controls real spend, and other API users are outside this accounting. To restore local caps, set true. Updated `.env.example` leaves local caps off per operator preference; existing `.env` must be edited explicitly.

## Guided settings flow

Telegram now requires verified MEXC connection, explicit text/VLM selection, then a trader profile, a readable summary, and explicit Start. Existing accounts migrate paused until selections are made again. A linked account shows connected/disconnect rather than another connect prompt. Historical Telegram messages retain their old keyboards; stale Start callbacks are still checked against current settings. `/settings` pauses entries and reopens the version/profile sequence.

Profiles are effective paper risk presets, not promised returns:

- Capital preservation: 0.5% planned stop loss/trade, maximum 3x, two positions, 2% daily loss halt, 8% peak drawdown halt.
- Balanced: 1% planned stop loss/trade, maximum 5x, three positions, 3.5% daily loss halt, 12% peak drawdown halt.
- Aggressive: 3% planned stop loss/trade, maximum 10x, four positions, 5.5% daily loss halt, 22.5% peak drawdown halt.

Exchange contract limits, margin availability and stop-to-liquidation checks may reduce size or reject the setup. Profiles share the same candidate strategies and typed analysis pipeline; aggressive is a risk posture, not a new proven strategy. No settings enable live orders. Existing positions retain their recorded SL/TP/leverage when changing settings; newly proposed risk uses the new profile. A settings change while AI runs invalidates that in-flight entry.

Messages resembling API credentials sent without a live `/connect` session are best-effort deleted and explicitly reported as **not saved**. The bot no longer responds to such input with a misleading generic menu. Do not post keys in screenshots; revoke/replace any exposed key on MEXC.

## Demo capital, account allocation and management reports

Telegram now starts with a PAPER capital choice, before analysis version/trader profile:

- `/demo 1000`: create a 1,000 virtual USDT session. No MEXC link or deposit is required; public market data and a working AI key are still required for AI-selected trades.
- `/capital 1000`: after MEXC link, seed an independent 1,000 USDT paper session from the account reference; reject if this exceeds observed available Futures USDT.
- `/capital full`: snapshot the currently observed available Futures USDT as the paper seed. This does not transfer funds or continuously copy wallet changes.

All amounts are **USDT**, not TRY. Both text/VLM variants receive the same chosen starting seed for a session. The choice is a simulation budget, not per-order notional or exchange margin. Existing paper PnL evolves independently. Capital changes pause entries, require no open positions and wait for in-flight jobs; each confirmed change creates a new session rather than rewriting old outcomes. Old sessions remain on disk for audit. Legacy accounts must choose capital explicitly before restarting entries. There is no live toggle or live order broker in this release.

`/report` shows today's closed-trade results for both variants, trade direction/strategy/leverage, costs, reasons and observed R excursions. `/history` summarizes the latest seven days; `/report YYYY-MM-DD` selects a cached day within that window. Current equity and open PnL are separate from realized daily net PnL. Reports use Europe/Istanbul closure dates, include delayed funding corrections and label incomplete legacy records. Previous capital sessions are retained but not combined into current-session reports.

New positions store immutable initial stop/risk and entry timeframe (currently 15m). Every supervised fresh quote updates observed gross-price R, MFE and MAE; these are samples, not reconstructed intracycle extrema. Leverage ROI is not R. Protection still checks current SL/TP independently of AI.

A separate management job checks completed 15m candles. Experimental `paper-structure-trail-v1` requires at least +1 initial R at a closed candle and two confirmed post-entry higher lows (long) or lower highs (short); the latest pivot plus/minus 0.25 ATR14 buffer proposes a tighter stop. Confirmation radius is two candles. The ledger checks position identity, newer evidence, exchange price step, monotonic tightening and a fresh executable quote. It never widens stops or moves them every tick. Every accepted change queues a Telegram `STOP_UPDATED` notification containing old/new stop, reason, entry, TP and balance.

This management is **deterministic research policy, not a Claude-selected stop move** and not a validated ICT system. The +1R, two-pivot, ATR14/0.25 parameters are experimental constants. No partial TP, automatic breakeven solely on a percent move, fixed one-hour exit or runtime strategy learning is implemented. Original stop remains active while waiting for structural evidence. Users should compare management-enabled/disabled results in replay before concluding it improves expectancy.

Price supervision is not delayed by candle/model/Telegram calls. Management errors leave the existing stop unchanged; quote outages block new risk. The system does not place exchange stops, so no execution guarantee is implied. Screenshot interpretation alone cannot establish historical fills or confirm ICT causality; exchange-specific OHLCV and timestamped decisions are needed.

## Persistent Ubuntu service

A service template is included at `infrastructure/mexc-paper-worker.service` for the existing `/opt/mexc-ai-trading` installation. It reads `.env` through the application's settings; do not put secrets in the service unit. The template runs as the service manager's default system user (root); it matches the current root-owned installation. A dedicated service user can be configured separately after assigning directory/secret permissions.

Stop the foreground worker with Ctrl+C first. Then, on the server:

```sh
cp infrastructure/mexc-paper-worker.service /etc/systemd/system/mexc-paper-worker.service
systemctl daemon-reload
systemctl enable --now mexc-paper-worker
systemctl status mexc-paper-worker --no-pager
journalctl -u mexc-paper-worker -n 30 --no-pager
```

Use `systemctl restart mexc-paper-worker` after a successful code/config update. Do not launch another foreground worker while this service runs. Telegram `/stop` pauses new simulated entries while supervision remains active; `systemctl stop mexc-paper-worker` stops the whole application and all simulated position supervision. This release has no live exchange orders. The service unit has not been executed on Hetzner in this session.

The Fleet worker now logs successful startup and one per-account status heartbeat per minute (account identifier, version and reason; no balances or secrets). An integration test covers Telegram demo selection → plan acceptance → paper entry → tighter stop event → SL close → notification queue/report → restart with preserved balance and paused control, without calling an exchange or AI provider.

## Groq-first evaluation and AI stop

Fleet routes both text and VLM evaluations through Groq first when `GROQ_API_KEY` is configured. Text defaults to `openai/gpt-oss-20b`; vision defaults to `qwen/qwen3.8-27b` (three images). Models and access can change: check [Groq vision](https://console.groq.com/docs/vision), [models](https://console.groq.com/docs/models) and your account's [limits](https://console.groq.com/docs/rate-limits). A free key does not guarantee enough quota for a three-chart prompt. No prompt, memory or image is silently removed to fit quota/context. An over-limit request waits or falls back.

Server `.env` settings:

```dotenv
GROQ_API_KEY=your_server_only_key
AI_PRIMARY_PROVIDER=groq
GROQ_TEXT_MODEL=openai/gpt-oss-20b
GROQ_VISION_MODEL=qwen/qwen3.8-27b
AI_CLAUDE_FALLBACK_ENABLED=true
```

Fallback `true` permits potentially paid Claude calls for Groq rate limits, unavailable models, transport errors or invalid JSON/schema. Valid HOLD/WATCH decisions do not trigger fallback. `false` waits instead. Rate limits create a cooldown shared by the fleet accounts; there is no rapid retry loop. Explicit JSON object output is locally validated for both providers. Context is application-owned: the identical system prompt, market/account snapshot, bounded journal and chart paths are passed to the fallback. This preserves inputs, not equivalence of model judgment. No provider request or rate-limit behavior has been tested against a real key in this session.

Telegram has separate **İşlem arayışını duraklat** and **Bot / AI durdur** controls. Both block new entry-analysis model calls; full AI stop is recorded explicitly and also guards fresh fallback calls. Neither kills the server process or disables deterministic SL/TP price protection. Current stop management uses code, not a model. Already submitted requests can complete and incur usage; their returned proposal still must pass the current control gate. To terminate the application itself, use `systemctl stop mexc-paper-worker`; Telegram control is then unavailable until it is started again.

The cost display separates token-based **Claude estimated settled cost** from **pending/uncertain reservations**; neither is the provider's invoice or remaining credit. Definitively rejected requests release reservations; timeouts/ambiguous failures keep them. Historical uncertain reservations are not automatically erased, and a new demo session does not reset the shared usage record. Groq usage is displayed as token counts/valid response counts, without assuming your account is free or charging Claude rates. Other applications' key usage cannot be observed here.

## Candidate diagnostics and dynamic risk display

`/diagnostics` (also `/events` or **Son analiz / AI istekleri**) shows recent account/version analyses, scan counts, candidate symbol/strategy/heuristic score, provider attempts, HTTP/error categories, latency and known token usage. A single candidate-scoped completion notification replaces generic repeated AI/fallback error alerts. History retains the last 30 evaluations per account/version; the panel shows the latest three. Shared provider request counters start with this release; they do not reconstruct historical calls or count other applications. Groq cooldown skips are not counted as HTTP requests. Claude token-count requests and generation requests are distinct.

Only allowlisted provider error codes/parameter names and fixed error categories are retained. Raw provider error messages, `failed_generation`, prompts, authorization headers and URLs are never persisted or sent to Telegram. A bare HTTP400 from an older version has no recoverable cause here: inspect the next candidate's safe diagnostic category instead. GPT-OSS requests explicitly use `include_reasoning=false` and low reasoning effort with JSON output per [Groq reasoning documentation](https://console.groq.com/docs/reasoning); no claim is made that this resolves an unobserved historical400.

Settings now show the profile's maximum risk percentage and a USDT amount computed from the selected variant's last observed paper equity, falling back to an explicitly labeled initial seed before the first observation. Execution always recomputes against current ledger equity. OPEN notifications include the algorithm, heuristic score and planned stop-loss percentage/USDT estimate after contract rounding and paper cost assumptions. Aggressive mode changes risk limits, not the candidate-generation algorithm or proven expectancy. No strategy percentage allocation or calibrated success probability exists. The current generators remain simple SMA8/21 trend, ROC12 momentum and SMA20 mean-reversion rules; this is not a completed discretionary ICT strategy engine.

## Export observed provider usage (no AI calls)

```sh
.venv/bin/python -m trading.control.usage_report --output-dir data/reports
# Optional start date/time, e.g. --since 2026-11-07T00:00:00+03:00
```

This reads account/session/text-or-VLM SQLite journals and writes private `ai-usage-summary.txt`, `ai-requests.csv`, and `ai-usage.json` files. Summary groups include provider/model, actual generation and token-count request counters, failures, cooldown skips, known/unknown token usage, mean tokens for known-usage requests, known Claude cost estimates and uncertain reservations. Export performs no provider or exchange call, can run alongside the service and does not change account control. Output includes private account labels/symbols but excludes prompts, raw errors, images and credentials. Files are regenerated with mode0600.

New completed evaluations are archived separately from the rolling last30 Telegram history. Older retained history is included and deduplicated, but previously discarded history cannot be reconstructed. Interrupted/in-flight requests may be missing; token usage missing from failed requests is not assumed zero. Means apply only to the recorded known-usage samples, not the whole lifetime or a forecast of future daily spend. Groq billed cost and other applications' usage remain unknown. A service restart preserves account controls, balances and journals; client `not_called` resets for the new process and does not mean historical provider usage was zero.

## Plain Telegram outcomes and Groq quota scheduling

An AI `trade` response is a proposal, not a fill. The analysis journal now separately records final `execution_status` and `execution_reason`: `opened` only after paper entry succeeds, `rejected` if a later control/price/risk check blocks it, and `not_opened` for HOLD/WATCH or unavailable analysis. Automatic candidate notifications wait for this final outcome; technical request details remain in `/diagnostics`. Telegram status is a readout, not an extra provider request. **Açık işlemler** (`/positions`) displays positions across the account's text and VLM ledgers, with entry, SL, TP, unrealized PnL, leverage and original decision model, plus manual close controls. Rendering charts for a Telegram trade notification is independent from sending images to a model: the text variant does not send charts to the LLM, but both variants may attach locally generated charts to an OPEN notification. These are rendered MEXC charts, not browser screenshots.

`GROQ_REQUEST_TOKEN_BUDGET=8000` is a configurable conservative local token scheduling allowance, not a claim about your actual Groq tier. Estimated text tokens are UTF-8 bytes/3 plus512 overhead; vision adds2048 per chart and the configured output allowance. `GROQ_MAX_COMPLETION_TOKENS=2048` replaces the previous4096 minimum. The prompt uses compact JSON without removing fields. Requests larger than the local allowance are skipped before HTTP rather than silently truncated. A shared rolling60s model window also bounds estimated token reservations and30 requests across this fleet's accounts. `0` disables the local estimate/gate. Other programs sharing a Groq account are not observed. Set allowance to your actual model/account quota; the estimate may conservatively skip requests the provider would have accepted. Full three-chart VLM contexts can exceed a free allowance and route to Claude often.

The original complete text/memory/images are forwarded once to Claude when primary output is unavailable and `AI_CLAUDE_FALLBACK_ENABLED=true`. Valid HOLD/WATCH never falls back. `false` waits instead; skipped or failed requests do not approve trades. Actual provider413 token-budget rejection and429 rate limits remain diagnosed even with local estimates. No tight retry or sleep blocks position protection. `AI_PROVIDER_TIMEOUT_SECONDS=25` applies per provider operation, with no automatic SDK retries; final decision TTL and current-price checks still reject stale outcomes. This can reduce failures caused by a15s client timeout but does not guarantee a provider response or erase uncertain past usage. Account quota handling has mock tests; deployment behavior still needs paper observation with real keys.

## Gemini setup (text and vision)

Set these in the server `.env`; keep the key out of chat, shell arguments and git:

```dotenv
GEMINI_API_KEY=your_server_only_key
GEMINI_TEXT_MODEL=gemini-3.5-flash-lite
GEMINI_VISION_MODEL=gemini-3.5-flash-lite
AI_PRIMARY_PROVIDER=groq
AI_VISION_PRIMARY_PROVIDER=gemini
AI_CLAUDE_FALLBACK_ENABLED=true
```

With both keys, this chooses Groq → Gemini → Claude for text and Gemini → Groq → Claude for charts. Set `AI_PRIMARY_PROVIDER=gemini` to put Gemini first for text too. A missing free-provider key is skipped. Valid trade/watch/hold JSON ends the chain; local schema validation, executable-price/risk/control gates still apply. `AI_CLAUDE_FALLBACK_ENABLED=false` disables the final potentially paid Claude attempt while retaining the two free-provider attempts. `AI_PRIMARY_PROVIDER=anthropic` explicitly uses Claude directly unless a vision primary override is set. Accounts keep isolated state and identical complete market/memory/chart inputs on fallback; no promise of identical judgments between models. The application deadline blocks fresh fallback calls after a decision expires.

Gemini uses Google's [OpenAI-compatible endpoint](https://ai.google.dev/gemini-api/docs/openai), explicit JSON object output and base64 inline charts. The [model code](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite) is configurable. Access/free quotas depend on your Google project; this code neither enables Cloud Billing nor verifies that your project's usage is free. HTTP429 creates a shared per-model cooldown; transient/invalid responses have a60s cooldown and no immediate retry. Telegram diagnostics/usage exports identify Gemini and its token usage without assigning Claude pricing to it. No real Gemini request has been executed in this development session; mock tests verify unchanged text/images, correct failover, stop/deadline and no paid fallback after a valid free response.
