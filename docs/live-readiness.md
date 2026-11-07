# Live execution components: operator review only

These components are not an autonomous live Telegram worker. The running
`mexc-paper-worker` still refuses live mode. Do not change its `.env` to
`BROKER_MODE=live` or `LIVE_TRADING_ENABLED=true`: it will stop rather than trade.
No bot, import, deployment or test activates the new execution adapter.

New code:

- `trading.exchange.mexc_private.MexcPrivateClient`: official signed queries and
  opt-in mutation methods; no transfers/withdrawals, redirect or write retry.
- `trading.runtime.live_account.LiveAccountReconciler`: real USDT equity versus
  available margin, paginated orders, positions, position mode and account risk
  and leverage data, isolated by account and credential binding.
- `trading.brokers.live.execution.ApprovedLiveExecutor`: exact proposal staging,
  expiring human approval, persistent write intent, external-ID reconciliation,
  native SL/TP readback and separately approved emergency closing attempt.
- `python -m trading.control.live_preflight --account ACCOUNT_ID --symbol BTC_USDT`:
  read-only connectivity inspection using the existing server vault/registry.
  It sends no POST and makes no AI requests. It is not execution authorization.

Keep the normal worker configuration:

```dotenv
BROKER_MODE=paper
LIVE_TRADING_ENABLED=false
```

Never put a current wallet balance such as11.5854 into an env capital seed for
live use. Actual equity, available balance and reserved margin must be refreshed
from authenticated exchange data. A contract's published maximum leverage is
not sufficient: the current account's private leverage/risk tier must also be
known, and the actual order must specify leverage and margin/position mode.

The executor is an engineering building block requiring an authenticated
operator caller. It is deliberately not connected to Telegram `/run`, and no
CLI in this change sends live orders. The current code does not provide the
requested unattended real-money mode. Acknowledgements are not proof of fills
or stop protection; unknown, partial or unexpected exchange states halt the
adapter for reconciliation. Mock tests do not prove behavior on a real account,
including its permissions, regional limits, position mode or liquidation rules.

Official references checked2026-11-07:

- https://www.mexc.com/api-docs/futures/integration-guide
- https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/place-order
- https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/get-risk-limits
- https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/place-tpsl-order-by-position
