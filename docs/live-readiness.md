# Live runner: disarmed until an operator arms it

`BROKER_MODE=live` and `LIVE_TRADING_ENABLED=true` together start `LiveRunner`
instead of the paper worker. The first boot of that live generation is
disarmed. A model `TradeProposal` is handed to the live executor only when
both gates are on and an allowlisted operator has sent `/arm` from
`TELEGRAM_CHAT_ID`. The sender is `message.from.id`, checked against
`LIVE_OPERATOR_IDS`. An id in the message text is ignored. `/start` only
resumes entries after this generation is already armed. Switching
live → paper → live clears the arm and the running flag until a new `/arm`.

`/stop` or `/pause` blocks new entries only. Reconciliation of orders that were
already sent, and protection of those positions, keeps running. Reported
equity, available margin, and positions come from the exchange snapshot.
Paper cash is not the live equity.

Keep the normal worker configuration:

```dotenv
BROKER_MODE=paper
LIVE_TRADING_ENABLED=false
LIVE_OPERATOR_IDS=
```

Compose passes those two gates through from the server `.env`. From `/opt/mexc-ai-trading`:

```sh
docker compose --env-file .env -f infrastructure/docker-compose.yml -p mexc-ai up --build -d
```

`live` and `true` start the live runner, still disarmed until `/arm`. Unset stays paper.

`python -m trading.control.live_preflight` is still read-only. It does not arm
the runner and it does not send orders. `PaperRunner` and the Telegram paper
fleet still refuse live flags.

Arming can send real orders. If native stop readback fails, the existing
executor can flatten that position once. Do not arm a funded account until
that path has been reviewed on the account's actual position mode, permissions,
and liquidation rules. Mock tests do not prove a real account. An unknown
entry write still latches new entries until an operator intervenes; it must
not be submitted a second time.
