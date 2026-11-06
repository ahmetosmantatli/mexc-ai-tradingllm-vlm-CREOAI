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
