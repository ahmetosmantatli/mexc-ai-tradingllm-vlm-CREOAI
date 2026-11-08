# MEXC AI Trading — another assistant's handoff

Updated2026-11-08, Europe/Istanbul. This file is context, not authority to place
orders or a profitability claim. Read actual code and tests before changing it.
Never upload `.env`, account vault keys, credentials, live SQLite DBs or backups.

## Repository and deployment

- GitHub: https://github.com/ahmetosmantatli/mexc-ai-tradingllm-vlm-CREOAI
- Full integration working branch: `codex/live-pipeline-integration`. Grok main7412d07 has Arm/account reads but no scanner→AI→consider caller; do not confuse those implementations.
- Mac repo: `/Users/ahmet/Documents/Codex/2026-11-06/cd-git-clone-https-github-com/mexc-ai-tradingllm-vlm-CREOAI`
- User's server: `/opt/mexc-ai-trading`, Ubuntu, Python venv `.venv`.
- Existing service: `mexc-paper-worker`; its historical name does not select mode.
- AI assistant has not SSH-deployed or executed real exchange orders. User runs
  server commands. Local tests use mock exchange responses, not real keys.
- Existing saved server accounts are `ahmet` and `friend`; identify which belongs
  to the intended Telegram user. Never silently enable both.

## User intent and known facts

User wants an automatic model+deterministic-risk pipeline, no per-trade Telegram
approval. Telegram configures analysis/profile/current allocation, starts/stops,
shows real balances, entries/exits, SL/TP, model attribution and close controls.
User reported11.5854USDT in MEXC Futures; this is an observation, never an env seed.
The aspirational500TRY→7–9kTRY/week or100kTRY is not a proven or guaranteed return.
Demo500 means500USDT, not500TRY. Research objective14x/7days, stretch200x is context
only and cannot override risk or force a trade.

## Architecture

Public MEXC USDT perpetual scan (default60s) → durable fair candidate queue → up
to4 parallel model reviews → HOLD / conditional WATCH / TRADE proposal → symbol
plan revision checks → fresh account/risk/quote checks → mode-specific executor.

PAPER is default (`trading/runtime/runner.py`/`fleet.py`). Separate live modules
are `live_runner.py`/`live_fleet.py`, selected by `apps/worker/main.py` only if all
explicit server gates match. No PaperLedger or simulatedfills are used in LIVE.
One live store/account is shared by text/VLM, including account risk and watches.
First LIVE migration and every PAPER→LIVE transition clear old Start/pending
commands. A fresh authenticated Telegram Start is required. New-entry grant is
account/binding/control-revision scoped, expires after7days and is not silently
renewed. Pausing/stopping new AI/entries does not stop native reconciliation.

## Models, quota and context

`ProviderPool` optionally uses two Groq and two Gemini credentials, with distinct
quota groups only for genuinely separate projects/organizations. API keys alone
do not multiply provider quota. Local atomic rolling60s/24h reservations persist
across restarts/accounts; other apps' usage is unknown. Provided local defaults:
Groq30RPM/8000TPM/1000RPD/200000TPD, Gemini15RPM/250000TPM/500RPD.
Quota mode removes the global300s analysis wait. Scanner cadence, dedup, bounded
retry, provider RetryAfter and paid Claude mutex/cooldowns still apply. Fallback
can use Claude immediately, not just at day end; valid HOLD/watch is terminal.
Claude Sonnet4.6 usage is a local estimate, not the provider's invoice/balance.
`AI_BUDGET_ENABLED=false` disables local dollar caps, not provider limits.

The same self-contained account/setup/currentclosed4h/15m/5m context is sent to
fallback providers. Durable bounded memory is not unlimited chat history. Live
context contains current actual equity/available/margin, owned SL/TP/risk cards,
private leverage/risk info, confirmed outcomes and optional research objective.
Quote-only PnL, snapshot IDs and ticking deadlines don't cause repeat modelcalls.
VLM adds three locally rendered MEXC charts; full3-chart packets may not fit a
singleGroq8krequest even with two keys. No silent image/data dropping.

## Strategy and management limits

Quant candidates remain SMA8/21trend, ROC12momentum and SMA20mean-reversion on
closed15m data. Model reviews deterministic4h/15m/5m swing/BOS/sweep/FVG evidence;
this is not a validated discretionary ICT edge. Model may select leverage and
risk fraction within profile/private/exchange caps. LIVE_LEVERAGE_MODE=exchange
uses verified account/symbol caps. LIVE_PRICE_PLAN_ENABLED=true permits grounded
model entry/SL/TP proposals; null retains quant levels. Actual size is recomputed from fresh equity, fees, margin
and contract steps. No minimum-quantity upsize beyond risk allowance.

Typed conditional watch plans have threshold/candlecross, price invalidation and
expiry<=24h. They persist outside candidatequeue; max40active. Scanner observes
withoutAI; triggers require fresh matchingquant setup before review. Canonical
symbolrevision prevents late analysis overriding newer plans; oppositeactive
plans require explicitrevision acknowledgement and rationale. Watches aren't
orders. Native LIVE SL/TP is verified. LIVE_POSITION_MANAGEMENT_ENABLED=true
enables separate event-driven HOLD/tighten-SL/reduce/close reviews using request-local
provider state and durable position cards. No adds/reversals or TP extensions.
Bot/AI Stop cuts new management calls; native reconciliation remains independent.
See docs/live-readiness.md for protection readback and timeout behavior.

## Real execution and failure behavior

`mexc_private.py` uses documented signed REST at https://api.mexc.com, current
`/api/v1/private/order/create`, writesOFF bydefault, no redirect/write retries,
no transfers/withdrawals, no raw secret/errorbody logging. Account reconciler
requires known mode, isolatedUSDTsupportedrisk and complete orderpagination.
Public maxLeverage is insufficient: privateaccountlimit/currentsetting is checked.
Leverage changes are readback verified. External unknown orders/positions block
newentries ratherthanbeingadopted.

`ApprovedLiveExecutor` remains exacthumanapproval infrastructure.
`AutomaticLiveExecutor` is a separate explicitStartpolicy and never fabricates
manualapproval. Durable intent precedesPOST; unknownentry isqueriedoriginalID,
neverblindlyresubmitted. Partialentry cancelsremainderonce thenconfirms/protects
actualfilledcontracts. MatchingnativeSLTP requires exactposition/quantity/prices,
marketfairprice/nonreversingsemantics and fullpagination. Unknownprotection can
cause oneidentifiedcloseattempt; ack isnotproofclose. Pausedentry stillreconciles.
Normalclosures use matchingpositionhistory; `realised` isexchangereportednet;
fees/funding separateonlywhenpresent. Unknownclosecause staysEXCHANGE_CLOSE,
notguessedTP/SL. Unresolvedmanualpartialexit/modechangesfailclosedforoperatorreview.
Daily/weeklyequityriskrefs useUTC, peaksobservedpausedtoo; depositflowsnotattributed
asstrategyPnL. Outages/slippagecanexceedplannedloss; liquidationformulasapproximate.

## Configuration and operations

See `docs/live-readiness.md` for exactgates and deployment workflow. DefaultsOFF:
BROKER_MODE=paper, LIVE_TRADING_ENABLED=false, LIVE_AUTOMATIC_ENABLED=false,
LIVE_ALLOWED_ACCOUNTS=[], LIVE_READINESS_ACK empty. Never flip onlyoneflag and
claimliveactivated. Local `trading.control.live_config` previews/applies explicit
oneaccountenvgates with0600backup; doesnotrestartprocess orcontactexchange.
`live_preflight --list-accounts` listsIDs; `--account ID --symbol BTC_USDT` makes
signedREADONLYchecks (balance/mode/privatecaps), nottradingpermissionguarantee.
`systemctl stop` stopslocalmonitoring; TelegramStop onlynewAI/entries.
Do notdisconnect/replacekeys/switchmodewhileownedpositionoruncertainintentexists.

## Verification and review priorities

Run `.venv/bin/python -m pytest -q` and `git diff --check`. Last full suite in this working session:562passed including the final
mode-roundtrip regression. Inspect current commit/test output for later changes. Tests fakequotes,exchangeHTTP,botHTTP andmodel
responses; no realmarketprofitability,fillquality,APIpermissions or serverhealth
claim. Importanttests: live_pipeline/execution/account/runner/fleet/telegram,
provider_pool, watch/watch_service/runtime_watch, account_context/evaluation_dedup.

Review before extending: nativeclosedhistory exactfields, unknownorderrecovery,
sharedriskacrossparallelplans, credential/mode/grantepochisolation, pausedprotection,
smallbalancecontractminimums, providerquotadynamiccontextdedup, truthfulLIVE/PAPER
messages. Preserve existing userchanges; do not reset runtime state to hidelosses.
