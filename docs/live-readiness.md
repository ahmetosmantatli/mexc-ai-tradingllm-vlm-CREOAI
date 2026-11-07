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

Models select bounded risk and leverage; entry/SL/TP levels still come from the
existing quant candidates. Every proposal is rebuilt from fresh equity,
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

This is not a complete discretionary ICT strategy or continuous AI position
management. Existing quant algorithms propose setups; native SL/TP protects
owned exposure. PAPER structural stop-tightening is not silently reused as a
LIVE cash/fill simulator, and automatic LIVE partial-profit/trailing changes
are not introduced by this release. Unsupported manual partial exits, exchange
mode changes and unidentified exposure may require operator reconciliation;
new entries fail closed. API outages, order limits, funding and slippage can
produce losses beyond a plan. Small balances may fail contract minimums.

Official references checked2026-11-08:

- https://www.mexc.com/api-docs/futures/integration-guide
- https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/place-order
- https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/get-risk-limits
- https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/get-historical-positions
- https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/place-tpsl-order-by-position
