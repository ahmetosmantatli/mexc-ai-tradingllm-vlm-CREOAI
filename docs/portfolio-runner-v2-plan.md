# Runner 2 — portfolio plan and exit replay

Recorded 2026-11-08, Europe/Istanbul. This document is a development contract,
not a claim that v2, runner switching or additional providers already work.
The current deployed baseline must continue without silent strategy changes.
Current reference code revision before this document: `7f75a5d`.

## Independent runners and analysis modes

Keep `baseline-v1` for the existing logic and `runner2` (Runner 2) for new behavior.
Runner identity is independent of text/VLM, risk profile and PAPER/LIVE mode.
Persist runner ID, immutable code/config version, account binding, analysis
mode, policy revision and model attribution on every decision and order.
Do not introduce flags that silently turn the baseline into v2. First implement
the registry and Telegram selection, with baseline as the existing-account default.
Record server-effective configuration before declaring a baseline frozen.

Execution, ownership, native protection and account risk stay centralized.
Two runners may not independently allocate the same cash or manage one position.
Switching affects new entries by default. Open positions retain their opening
runner's policy; explicit migration needs a fresh read, adoption record and plan
revision, preserving current native protection. Historical results retain their
original attribution, including technical losses; migration must not rewrite them.
PAPER/shadow comparison is separate from spending the real wallet twice.

## Shared market evidence

Fetch closed MEXC perpetual 4h/15m/5m evidence for BTC, ETH and the candidate.
Cache benchmark data once per cycle; do not create separate model calls merely
to refresh it. Respect public rate limits and preserve timestamps/freshness.
Compute trend/structure/volatility, time-aligned returns, relative performance,
rolling correlation and sample count. Unknown/short histories stay unknown.
No automatic rule banning altcoin longs simply because BTC declines: allow an
explicit measured independence/relative-strength thesis with its own trigger.
Breadth must name the sampled universe, coverage, missing symbols and timestamp;
the current rotating 40-symbol batch is not a complete 1090-contract market view.

Retain swing/BOS/sweep/reclaim/FVG and continuation/pullback/countertrend families.
Do not claim these implement all discretionary ICT. Add 1m/burst detection only
as a separately validated feature with closed bars, volume/volatility expansion,
spread and liquidity checks, expiry and a rule against chasing late entries.

## Instrument quality and leverage explanation

Separate trade-thesis health from instrument/execution quality and opportunity
quality. Supply observed volume, spread, depth if actually measured, data age,
contract quantity/tick rules, account leverage bounds, funding and costs when
known. Never invent a universal trust score or calibrated win probability.
Use data-missing flags rather than filling unknown metrics with zero.

Persist chosen leverage/risk, model versus policy sizing source, allowed range,
stop distance, maintenance margin, estimated liquidation buffer, available
margin and fees/slippage, plus a concise sizing reason. Exchange maximum is not
a target; leverage is not a profit-probability score. Revalidate after actual fill.
Avoid sending an ambiguous `leverage=cap` as though it were a recommendation in
v2 prompts; label limits explicitly. Leave baseline prompts unchanged.

## Existing-position and portfolio decisions

Supply all current position cards, account balances, pending writes, open risk,
current quote/native protection, and the best *current* alternative candidates.
Entry scanning and protection reconciliation remain independent activities.
Use bounded cards and meaningful change triggers, not full repeated journals.

Separate entry timeframe, review timeframe, intended horizon, maximum holding
bars and invalidation. Changing review frequency does not rewrite time held.
Apply decisions symmetrically to long and short:

- hold or watch continuation; trail a stop only toward lower risk;
- reduce or close based on a revised thesis and measured costs;
- propose a new grounded target before the original target triggers;
- explicitly revise a scalp into an intraday/swing plan, with reason, expiry,
  horizon and invalidation, without widening stop or increasing initial loss risk.

Target extensions need a candle reference, expected plan revision and native
amend/read-back. The opt-in LIVE pilot allows one outward target extension on
a near-target event and leaves the native stop unchanged. Keep prior protection during uncertain updates;
never remove a valid stop merely to extend a target. A target already triggered
or closed position must reconcile, not be revived. Bound repeated extensions.

Runner 2 can choose a single target or TP1 plus TP2 at entry. The same exit
plan can be revised for an open position, including switching back to one
target for the remaining contracts. Round TP1 down to the contract step and
assign the remainder to TP2. Both planned legs must meet the exchange minimum.
Record each fill once by ID. A TP1 fill reduces the size under the common stop
and cannot create a second TP1 allocation. A stop may only tighten, and a
crossed old target must be reconciled before editing the plan.

The implemented SHADOW replay takes an `opening` object and an `events` array
with explicit simulated fills and plan revisions. Run it with
`python -m trading.control.runner2 scenario.json`. The output contains the
opening evidence, decision reasons, revisions, fills, and remaining stop
coverage. The input can be replayed from the start. See
`tests/unit/test_runner2.py` for a complete scenario.

An optional `opening.horizon` stores the initial style, review timeframe and
expiry. A SHADOW revision can promote it before expiry with a longer horizon
and a further target anchored to a closed candle on the higher review
timeframe. The original opening stays in the audit, and the current stop
remains the invalidation level unless the revision tightens it.

Optional `opening.entry_risk` takes contract size, loss budget, fee rate and
exit slippage in basis points. The replay derives a tick-rounded IOC price
limit and rejects a simulated fill outside it. This check models stop loss
and costs at the configured prices. It does not submit orders or model gaps
beyond the assumed stop slippage.

The exit replay reports planned versus filled entry, adverse entry slippage,
initial target R and realized R before funding when costs are supplied. It
accepts partial stop fills as unresolved stop execution and rejects plan
revisions while the simulated remainder is unresolved. Compare matched
opening and cost scenarios with `--compare other.json`. The comparison uses
supplied fills and reports a final net difference only when both are closed.

The portfolio SHADOW entry point uses a separate SQLite file:

```sh
python -m trading.control.runner2 scenario.json --portfolio-db data/runner2-shadow-demo.sqlite3
```

The input `commands` array accepts timestamped observations, review queue and
dispatch events, review results, balance observations, confirmed reductions,
reservations, entry/close results and historical retrieval. See
`tests/unit/test_runner2_portfolio.py` for complete inputs. Dispatch consumes
the scenario's supplied available slot count. It prioritizes position events,
leases one review per subject, discards superseded results and allows one
bounded retry when a provider result is unavailable. The journal is restart
safe by command ID. An uncertain entry retains its reservation. A confirmed
fill releases reserved headroom only when a newer balance explicitly includes
that fill. Historical retrieval uses only decisions and outcomes known at the
requested cutoff. Automatic feeding from the live scan, provider pool and
exchange adapter remains a separate integration step.

A SHADOW position observation may include the active target, side, original
price risk, ATR and a fresh quote. It queues a near-target review within the
smaller of 0.25R and 0.5 ATR. A touched target requires fill reconciliation.
A new plan revision supersedes reviews of the old target.

Pass `--ai-reviews` explicitly to process one batch of current, timestamped
SHADOW jobs with the existing free-provider pool. It reads the shared quota
ledger, dispatches eligible reviews in priority order, includes up to three
time-safe examples and uses the pool's fallback for each isolated request.
The default replay makes no network requests. The explicit batch consumes
free-provider quota, supports text evidence and records advice only. It has
no exchange writer and does not reserve capacity ahead of the existing worker.

The replay does not simulate matching or amend requests. The LIVE pilot is
opt-in with `LIVE_RUNNER2_ENABLED=true`, alongside
`LIVE_PRICE_PLAN_ENABLED=true` and `LIVE_POSITION_MANAGEMENT_ENABLED=true`.
The Telegram settings flow selects the runner for new entries, then analysis
mode, trader profile and explicit Start. Entries use price-bounded IOC with
one exchange-native SL/TP pair. If its full coverage cannot be verified, the
existing emergency close/review flow applies. Existing positions retain their
opening runner's management policy, and legacy positions are treated as Runner 1.

The pilot uses the existing managed partial close flow. Native TP1/TP2 legs,
live horizon promotion, automated portfolio reallocation and authenticated
exchange validation remain separate work. In particular verify aggregate stop
coverage, trigger/modify races and partial fills before adding two native exits.

For reallocation, one account coordinator compares keep/reduce/close/wait with
new opportunities net of switching costs, uncertainty, correlation and margin.
A losing position alone is not an exit reason, and an LLM's unsupported claim
of greater profitability is not measured expected value. Close/reduce first,
confirm actual fills and remaining protection, refresh cash, reprice/revalidate
the new setup, then authorize entry. Never spend anticipated close proceeds.
Use idempotent operations, per-account reservations and stale-plan rejection.
No leverage change/add-to-loser/reversal is introduced implicitly by migration.

## Historical retrieval and measurement

Use structured retrieval first: similar family, timeframe, regime, volatility,
relative strength and execution costs, including other symbols where relevant.
An embedding/vector database is optional, not a prerequisite. Attach a small
set of comparable examples plus sample count and aggregate observed results.
Preserve entry-time facts; no future candles/outcomes may leak into old decisions.
Historical price levels are examples, not live executable levels.

Record an immutable bounded decision evidence snapshot and current market
regime, alongside thesis and subsequent confirmed actions. Store unknown close
causes as unknown. EMERGENCY remains a technical failure label, never a guessed
SL; its real PnL/fees still count in overall runner/account performance.

Daily reporting uses Europe/Istanbul boundaries and runner attribution:
start/end equity/allocation, realized net PnL, open PnL, fees/funding completeness,
AI usage cost (estimated versus actual), drawdown, number of decisions/trades,
technical losses and grouping by family/regime. Deposit/withdrawal and mixed
runner PnL cannot be silently interpreted as strategy return. If cash-flow or
valuation inputs are missing, report the percentage unavailable/partial.
Separate wallet return from allocated runner return. Never call a few observed
trades or shadow fills proof of profitability.

## Provider routing and Telegram observability

The shared pool caps actual generation requests per evaluation with
`AI_POOL_MAX_GENERATION_REQUESTS` (default 3). If paid fallback is configured,
one slot is reserved for it when the cap permits. Malformed HTTP 200 output
skips other credentials serving the same model for that evaluation.
`trading.control.usage_report` includes Runner 2 SHADOW databases under the
runtime data directory and reports generation requests per valid decision by
runner and task.

Retain application-owned position/portfolio state across provider changes.
No provider may assume persistent hidden conversation context. Match each
request to modality, context size, schema capability, quota group and measured
response reliability. Text routes never receive a silently truncated VLM job.
Honor reset/Retry-After and decision expiry; use bounded retries and jitter.
Do not call another model merely because the first made a valid HOLD decision.
Paid routes remain disabled unless explicitly configured by the operator.

Prioritize available AI capacity for active-position events over optional new
entry reviews without blocking deterministic native protection. If no suitable
provider is available, report capacity waiting and its known/estimated retry
time, keep native stops/targets, and discard expired decisions before acting.

Telegram should expose runner/mode, scanning, queue size, waiting for trigger,
unchanged setup/no new AI request, provider retry/quota waiting, analysis result,
risk rejection, order acknowledgement versus confirmed fill/protection, plan
revision and portfolio reallocation status. Notify on meaningful state changes
and bounded summaries, with deduplication; do not send a message for every tick.
Use simple Turkish and include what action was taken or what is being awaited.

## Acceptance before release

Baseline behavior/configuration must stay intact; migration and existing
SQLite records must remain readable. Test long/short symmetry, profitable and
losing positions, cross-asset divergence, stale/missing benchmarks, all-provider
exhaustion, genuine HOLD, native amend races, pending close/margin, partial fills,
restart, runner switching and no duplicate writes. Compare v2 in PAPER/shadow
before selecting it for LIVE. No deployment or live activation follows merely
from committing this plan.

## Additional provider candidates — not yet integrated

Cloudflare Workers AI: free-plan eligible `@cf/zai-org/glm-4.7-flash`,
`@cf/nvidia/nemotron-3-120b-a12b` for text and
`@cf/google/gemma-4-26b-a4b-it` for text/vision, subject to actual endpoint/schema
tests and account quota. The 10,000-neuron daily allowance is shared usage, not
10,000 requests and not a separate allowance for every model.

OpenRouter: investigate exact free IDs `nvidia/nemotron-3.5-lightning:free`,
`nvidia/nemotron-3-ultra-550b-a55b:free` (text) and
`nvidia/nemotron-3.5-super-vl:free` (vision). Validate endpoint JSON behavior and
latency using representative non-trading requests before admitting them to the
execution pipeline. More free model IDs do not multiply the account's common
free request allowance. No provider/API key has been added by this document.

OpenRouter's current pages list JSON-schema `response_format` for Super VL;
Ultra and Lightning support tool calling but not enforced `response_format`.
They therefore require a tested tool-output adapter or local JSON validation,
not blindly reusing a JSON-schema request parameter that the endpoint rejects.
Sources checked on the recording date:
https://openrouter.ai/nvidia/nemotron-3.5-super-vl:free
https://openrouter.ai/nvidia/nemotron-3-ultra-550b-a55b:free
https://openrouter.ai/nvidia/nemotron-3.5-lightning:free
https://developers.cloudflare.com/changelog/post/2026-07-28-models-require-workers-paid/
https://developers.cloudflare.com/workers-ai/platform/pricing/
https://openrouter.ai/collections/free-models
