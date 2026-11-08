# Explicit LIVE worker integration

PAPER remains the default. This change adds a separate `LiveRunner` and
`LiveFleet`; it does not convert paper fills, cash, PnL or existing positions
into real exchange state. All tests use fake exchanges or HTTP MockTransport.
No actual exchange order or server deployment has been performed by the agent.

## Account and decision flow

One persistent live store is used per account at `data/<account>/live/`.
Text/VLM analysis shares real account risk, owned positions and canonical
symbol plans. A new credential binding cannot inherit the previous connection's
intents or automatic policy. Server registry and encrypted vault remain the
identity/credential authority; models never receive API keys.

The scanner refreshes independently, conditional watches persist, and up to4
candidate model calls can overlap. Models receive current real USDT equity,
available margin, owned entry/SL/TP/risk cards, private risk/leverage data,
confirmed close summaries and an optional research objective. Quote-only PnL,
snapshot generation and clock changes do not independently trigger repeat AI
calls on unchanged closed candles. Quota scheduling and provider fallback remain
shared across users and variants. A valid model HOLD ends a review.

Models select bounded risk and leverage. By default entry/SL/TP levels come from
quant candidates; optional grounded model price planning is described below.
Every proposal is rebuilt from fresh equity,
contract steps and fees/slippage before a persistent intent is authorized.
An account-specific leverage change is read back; no ambiguous change is assumed
successful. External or unidentified orders/positions block new entries rather
than being silently adopted. Only supported isolated USDT contracts are accepted.

## Execution and protection

Telegram Start creates an account/binding/control-revision policy grant valid
for at most7days. Automatic policy authorization is distinct from human approval
of an individual proposal; the software never invents a human approval identity.
The first LIVE boot resets old PAPER Start state to stopped. Stop, pause or
settings changes invalidate pending entries and provider retries. A new explicit
Start is required after expiry; it is not renewed in a background loop.

Submission intent is durable before the first write. Timeouts, crashes and
uncertain acknowledgements are reconciled by the original external order ID,
never blindly resubmitted. Partial entry fills cancel the remainder once,
confirm its terminal state, then protect the actual filled quantity. Pending
or contradictory states block further entry.

Native SL/TP must be read back for the exact position, quantity, prices, market
execution, fair-price reference and non-reversing direction. Ambiguous protection
is never labelled protected. A single identified reduce-close recovery may be
attempted under the explicit Start policy; its acknowledgement is not a proven
close. Normal native TP/SL/manual closures are reconciled from exchange position
history and `realised` is used as the exchange-reported net result. Fees/funding
are separately reported only if the exchange supplies them; unknown is not0.
If no verified close-cause field exists, the reason is `EXCHANGE_CLOSE`, not a
guessed TP/SL from price proximity.

Entry permission is separate from reconciliation/protection permission. Telegram
Stop prevents new AI/entry attempts but monitoring and identified closing remain
available. It cannot recall an entry write that is already in flight. Stopping
the whole systemd service stops local monitoring; already-created native orders
remain at MEXC subject to its operation. Do not disconnect, replace keys, reset
state or switch back to PAPER while owned exposure or uncertain intents remain.

Equity peaks are observed during monitoring, even while paused. Daily and weekly
risk references use UTC calendar boundaries. External deposits/withdrawals are
not inferred from trading PnL: equity-based circuit breakers and objective
progress reflect the account, not audited strategy returns.

## Server preparation — user controlled, not an automatic deployment

1. Pull `telegram-paper-control` and run `.venv/bin/python -m pytest -q`.
2. List local account IDs without keys or API requests:
   `.venv/bin/python -m trading.control.live_preflight --list-accounts`.
3. Inspect only the selected account:
   `.venv/bin/python -m trading.control.live_preflight --account ACCOUNT_ID --symbol BTC_USDT`.
   This performs signed reads, shows equity/available, current mode and per-side
   leverage limits; it does not approve or send an order. A successful read is
   not a guarantee of order permission, minimum-size feasibility or profitability.
4. Preview the explicit configuration without applying it:
   `.venv/bin/python -m trading.control.live_config --account ACCOUNT_ID`.
   The local helper can apply those settings only with both `--apply` and
   `--ack-real-orders`; it creates a private backup and never starts a process.
   Only the named registry account is allowlisted. Do not enable a friend's
   account implicitly.

Required LIVE configuration, if the operator elects to deploy it:

```dotenv
BROKER_MODE=live
LIVE_TRADING_ENABLED=true
LIVE_AUTOMATIC_ENABLED=true
LIVE_ALLOWED_ACCOUNTS=["ACCOUNT_ID"]
LIVE_READINESS_ACK=I_UNDERSTAND_REAL_ORDERS
```

Existing AI/vault/Telegram keys remain in their current server settings. Do not
put an observed balance such as11.5854 into `.env`. Optional global LIVE limits
are `LIVE_MAX_LEVERAGE=10` and `LIVE_MAX_POSITIONS=4`; chosen profile/private
exchange/risk limits may be lower. A maximum is not a target or a minimum.
`STARTING_CAPITAL_TRY` and PAPER seeds do not size this worker.

The operator restarts the existing service after their configuration decision.
Do not run a second worker for the same bot/account. The first LIVE Telegram
panel is stopped: connect/select the current MEXC full-account allocation,
choose text/VLM and a trader profile, then explicitly Start. Model-authorized
trades require no subsequent Telegram approval. Manual closing requested by a
person has its own position-bound confirmation.

The default safe configuration is still:

```dotenv
BROKER_MODE=paper
LIVE_TRADING_ENABLED=false
LIVE_AUTOMATIC_ENABLED=false
LIVE_ALLOWED_ACCOUNTS=[]
LIVE_READINESS_ACK=
```

## Limits and validation

Tests cover default-off startup, one-account isolation, mode migration, actual
balance changes, current grant/credential changes, concurrent planning and
serialized entry risk, no duplicate entry after timeout/restart, partial entry,
normal close followed by another entry, independent paused reconciliation,
manual native closing, loss circuit breakers and exact SL/TP readback.
They do not demonstrate profitability or exact live exchange behavior.

This is not a complete discretionary ICT strategy. Existing quant algorithms
propose setups; optional model price planning and event-driven position
management are described below. Native SL/TP protects owned exposure. PAPER
structural stop-tightening is not silently reused as a LIVE cash/fill simulator. Unsupported manual partial exits, exchange
mode changes and unidentified exposure may require operator reconciliation;
new entries fail closed. API outages, order limits, funding and slippage can
produce losses beyond a plan. Small balances may fail contract minimums.

Official references checked2026-11-08:

- https://www.mexc.com/api-docs/futures/integration-guide
- https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/place-order
- https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/get-risk-limits
- https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/get-historical-positions
- https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/place-tpsl-order-by-position

## Optional exchange-based leverage selection

`LIVE_LEVERAGE_MODE=profile` remains the default. Set `exchange` only if the
operator chooses to replace the local/profile leverage ceilings with the fresh
minimum of public contract maxLeverage, private account risk maxLeverage and
side-specific private leverage maxLeverageView. In this mode `LIVE_MAX_LEVERAGE`,
the profile leverage cap and Telegram's saved max_leverage do not set the ceiling;
the model must explicitly return integer leverage and a risk fraction. A missing
plan is HOLD, never automatic use of the maximum. PAPER behavior is unchanged.

Risk fraction, total open risk, available margin, initial margin rate, contract
minimum/step and stop/estimated liquidation buffers still apply, before the
entry and again on confirmed filled exposure. A500x exchange upper bound is
not a feasible500x stop plan. The existing default minimum stop-to-liquidation
buffer is2% of price, and current quant stops are typically1.5–2% of price; many
high leverage proposals will correctly be rejected. This setting does not add
model-selected arbitrary entry/SL/TP levels or alter existing positions.

## Optional model entry / SL / TP planning

`LIVE_PRICE_PLAN_ENABLED=true` lets the same model review return a strict
`price_plan` for the selected candidate's symbol and side. Default false retains
quant levels. A null plan also deliberately retains quant levels; a malformed
plan is rejected, never silently substituted. Text and VLM use the same schema.
No extra provider call or browser tool is introduced; the typed response budget
increases from700 to1100 output tokens when planning is enabled.

Only a market entry reference near the current quote is accepted. A distant
entry must use a conditional watch, not a disguised market/limit order. The plan
contains entry/SL/TP prices, entry timeframe, stop/target candle references and a
reason. References use exact candle-open UTC seconds, timeframe and high/low from
the recent closed OHLCV actually supplied to the model (latest12 per timeframe).
LONG stops must be at/below a referenced low; SHORT stops at/above a high. Stop
buffer is at most one recent average true range. Targets reference an opposite
observed high/low on the profit side, within one recent ATR. These are explicit
validation definitions, not a calibrated ICT edge or evidence of profitability.

Prices must respect the MEXC tick step, direction and current executable price.
The server preserves original signal/queue identity and immutable evidence; the
runtime validates the plan again with fresh price/tick/equity before sizing.
Fees/slippage, minimum contracts, margin, shared risk, expiry, current plan
revision and liquidation gates remain mandatory. A target with nonpositive
estimated net reward after configured costs is rejected. Native stop/target
verification uses the final model prices, not the original quant levels.

The setup card and canonical symbol plan record the model's actual proposed
levels. Telegram identifies **AI plan** versus **algorithm levels**, and reports
the model's plan reason. Existing open positions are not rewritten by enabling
this option. Event-driven native stop/exit management requires the separate
position-management flag below; arbitrary pending/limit-entry plans remain unsupported.

## Optional AI position management

`LIVE_POSITION_MANAGEMENT_ENABLED=true` enables a separate account management
job while the same operator Start grant is active. `LIVE_MANAGEMENT_MIN_INTERVAL_SECONDS`
(default 60, range 15..3600) is a per-position call floor, not periodic permission
to call on unchanged evidence. First review, a newly closed review-timeframe bar,
or a new sampled favorable integer-R milestone may trigger a call. Entry plan
TF defaults to 15m if absent; the model may select 5m/15m/4h for subsequent reviews.
The monitor samples executable quotes for excursions; sampled MAE/MFE is explicitly
not tick-exact. No automatic fixed-R stop move or mandatory profit taking exists.

A request-local provider client uses the same quota groups, shared paid budget,
unchanged-context fallback, VLM/text evidence and deadline as entry reviews. A
self-contained card includes initial thesis/entry/stop/risk, current native
levels and remaining contracts, actual account snapshot, elapsed bars, bounded
previous decisions and confirmed actions, plus current compact closed candles.
Management usage is archived under strategy `position_management` in the same
usage database. HOLD/failure on the same event is not repeatedly submitted;
there is no assumption of provider-side conversational memory.

Supported typed actions are HOLD, tighten SL, reduce a fraction of CURRENT
remaining contracts, and close. Tightened stops reference a supplied closed
candle wick, obey tick/fair/executable-price/liquidation checks, and never widen
initial risk. Existing targets remain unchanged; no adds, reversals, leverage
changes, or arbitrary TP extensions are supported. Initial proposal/fingerprint
is immutable; current protection, remaining size and management revision are
separate state. PnL reconciliation uses cumulative original filled exposure,
including partial exits, and never fabricates separate partial-exit net profit.

The account executor lease serializes writes with entries/manual closes/monitor.
Ownership, native protection, revision, permission and deadline are checked
again after inference and before a write. Durable records precede POSTs. Timeout,
crash or missing read-back never causes blind resubmission. Stop modifications
use the identified native pair, without cancelling protection. Partial closes
are confirmed by original external ID and actual remaining position, including
terminal partial fills; the remaining pair is read back and, if necessary,
resized with one same-price placement. No additional AI write is allowed during
an unresolved action; new entries halt while read-only reconciliation continues.

MEXC documents in-place stop price modification and asynchronous same-price
quantity updates: [modify native TP/SL](https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/modify-tpsl-prices-on-a-tpsl-planned-order),
[place TP/SL by position](https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/place-tpsl-order-by-position).
These are exchange capabilities, not atomic execution guarantees. During delayed
resize confirmation the previous pair is retained and protection is explicitly
unverified for the remaining size. After the executor read-back window (default
30s), a confirmed remainder still lacking verified protection is emergency-closed
once under the existing emergency permission. That close is separately read back;
an exchange outage may still require a manual exchange check.

Telegram shows current levels/remaining size, last management rationale, provider
and model, confirmed stop/partial/full-exit events, or explicit uncertain-action
notifications. Bot/AI Stop (and Pause in the current LIVE panel) cuts new management
calls/actions; already-sent writes still reconcile, including protective resize.
Native SL/TP monitoring is independent of model calls and continues while stopped.
This release is verified with fake exchanges only, not a live-account execution test.

## Coverage, independent watches and adaptive analysis admission

LIVE scanning persists an alphabetical round-robin cursor over the discovered
symbol universe. `SCANNER_MAX_SYMBOLS` (default 40) is work per scan, not a fixed
first-40 universe. Each covered symbol still passes liquidity and fresh-candle
checks. Explicit watch symbols are checked separately. Rotation ignores API list
order and fee-free preference for coverage; it does not rank expected returns.
The bounded candidate queue retains unexpired candidates from unscanned symbols,
replacing/removing candidates for actually refreshed symbols. Full-queue limits
and stale-data limits still apply; this does not promise review of every listing.

A price/candle watch trigger now creates a separate `watch_review` opportunity.
It no longer requires the original SMA/ROC/mean-reversion strategy to emit again.
Closed opposing BOS observations on 15m/4h after watch creation also request a
review, without implying an entry trigger or automatically reversing a position.
Per-timeframe event records prevent the same 15m/4h observations alternating into
repeat calls. These are explicit structure definitions, not all possible thesis
changes or a validated ICT strategy. Watch expiration and price invalidation
remain deterministic; queued expired/superseded events cannot authorize a trade.

Independent reviews retain the canonical prior plan, its rationale and revision
in self-contained context. Existing entry/SL/TP values are reference-only: trading
an independent watch requires `LIVE_PRICE_PLAN_ENABLED=true` and a fresh valid
model price plan. HOLD/watch remains possible without it. Current liquidity,
closed context, quota, execution costs, margin, risk, native protection and
credential/start-grant gates are unchanged. Different watch data failures are
isolated per symbol. Watch candle data is cached through its closed-bar boundary;
watch monitoring does not itself call a model.

`AI_DYNAMIC_PARALLEL_ENABLED=true` uses ProviderPool local quota state to shrink
or expand each LIVE entry batch up to `AI_PARALLEL_CANDIDATES` (now 1..8). Capacity
counts distinct available quota-group/model slots, in-flight leases, cooldowns,
rolling minute/day requests/tokens and conservative request-size estimates
including observed token counts. Each existing group mutex permits one operation
at a time; Claude contributes at most one available paid fallback slot, subject
to its guard and configured budget. This is advisory scheduling, not real provider
quota/credit telemetry. Atomic request reservations remain final authority when
accounts race or actual input sizes differ. An unavailable pool does not consume
candidate consideration or stop scanning/position reconciliation. The fleet reserves
four job slots per account (monitor/scan/entry/management), so three slow analysis
jobs cannot occupy every worker needed by the next monitor cycle. No fallback
input is truncated. Telegram shows coverage and admission estimates separately
from last entry status so monitor updates do not hide a waiting entry reason.

## Measuring model contribution without inventing profits

Valid LIVE reviews now append immutable `edge_reviews` records: raw quant plan,
model action/initial price plan, variant, observed spread and configured estimated
fee/slippage. Each opportunity is counted once in a matched replay, even if it was
reviewed repeatedly. Independent watch reviews have no equivalent fresh quant
baseline and are explicitly excluded from that comparison.

Run the research export in the worker with persisted data available:

```bash
python -m trading.control.edge_report --root /app/data --fetch-public --output-dir /app/data/reports
python -m trading.control.usage_report --output-dir /app/data/reports
```

The first command makes public 5m candle calls only, never model/private order
calls. Alternatively supply `--candles-json PATH` for fully offline replay.
Default horizon is 48 closed 5m bars (4h). Both baseline/model use the next-full-bar
open convention, entry drift validation, adverse gap exits, stop-first treatment
when a candle hits both levels, configured fees/slippage and a constant observed
spread assumption. Missing/expired candle history, missing spread or incomplete
horizons are explicitly incomplete, not zero. Only available recent public history
is fetched; missing older data is not fabricated or silently interpolated.

This isolates selection and INITIAL price planning on overlapping opportunities;
it is not a portfolio backtest, exact live fills, dynamic management replay or
proof of edge. Funding/liquidation/AI API dollars are not combined into a false net
return. AI usage/cost is exported separately; actual LIVE realized outcomes remain
exchange-reconciled records. LIVE usage exports now include account/live databases
and separate known text/VLM variants; legacy untagged LIVE usage is unknown.
No historical review records are invented for releases before this journal existed.

## Provider schema compatibility and actionable diagnostics

Anthropic strict tool schemas are passed through the official SDK
`transform_schema` before both count_tokens and generation. Unsupported numeric
and length constraints move into descriptions; original Pydantic and deterministic
execution checks still enforce them locally. Nullable type arrays are normalized
to anyOf for SDK compatibility. The Anthropic SDK floor is 2.6.0.
Reference: [schema transformation and limitations](https://platform.claude.com/docs/en/build-with-claude/structured-outputs).
This fixes a concrete incompatible raw-schema path; an observed HTTP 400 may also
have other causes, so safe phase/category diagnostics remain necessary.

Gemini/Router validation telemetry now preserves allowlisted schema fields and
fixed semantic categories (for example watch_requires_fresh_price_plan or
watch_condition_already_met), never raw model output or rejected field values.
Market scans report safe HTTP status, timeout/network or format categories and
numeric exchange rejection codes. Diagnostics distinguish generated candidates,
eligible candidates and retained queue size, and do not count metadata failures
as skipped individual symbols. Shared request statistics and local quota-pool
snapshots are included in LIVE diagnostics as well as PAPER diagnostics.
