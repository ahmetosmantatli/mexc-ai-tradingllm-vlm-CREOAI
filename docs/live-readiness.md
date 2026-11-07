# Live runner: disarmed until an operator arms it

`BROKER_MODE=live` and `LIVE_TRADING_ENABLED=true` together start `LiveRunner`
instead of the paper worker. The first boot of that live generation is
disarmed. A model `TradeProposal` is handed to the live executor only when
both gates are on and an allowlisted operator has armed this generation from
`TELEGRAM_CHAT_ID`. The sender is `message.from.id` for `/arm`, and
`callback_query.from.id` for the Arm button. That id is checked against
`LIVE_OPERATOR_IDS`. An id in the message text is ignored. The live chat
shows two buttons, Arm and Durdur. Durdur and `/stop` block new entries the
same way. Replies are short Turkish and say silahlı or silahsız, and why.
`/start` only resumes entries after this generation is already armed. Switching
live → paper → live clears the arm and the running flag until a new arm.

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

`live` and `true` start the live runner, still disarmed until Arm or `/arm`. Unset stays paper.

That live process is the only `getUpdates` poller. `apps.worker.main` does not start the paper fleet in live mode, and the paper Telegram tick does not call `getUpdates` when the live gates are on. The old menu (`Ayar özeti | PAPER`, `Demo kasa`, `İşlem arayışını duraklat`, `Bot / AI durdur`) is `TelegramController` in a second `python -m apps.worker.main` that is still in paper mode. Telegram delivers each update to only one poller, so that process eats `/arm` and the live worker never sees it.

On the server the live unit is `mexc-paper-worker` (`/opt/mexc-ai-trading/.venv/bin/python -m apps.worker.main`). After this code is in that directory:

```sh
systemctl restart mexc-paper-worker
```

Do not also leave the compose worker running. If `docker ps` shows project `mexc-ai` service `worker` (container `mexc-ai-worker-1`), that is the paper poller. Stop it from `/opt/mexc-ai-trading`:

```sh
docker compose --env-file .env -f infrastructure/docker-compose.yml -p mexc-ai stop worker
```

`python -m trading.control.live_preflight` is still read-only. It does not arm
the runner and it does not send orders. `PaperRunner` and the Telegram paper
fleet still refuse live flags.

Arming can send real orders. If native stop readback fails, the existing
executor can flatten that position once. Do not arm a funded account until
that path has been reviewed on the account's actual position mode, permissions,
and liquidation rules. Mock tests do not prove a real account. An unknown
entry write still latches new entries until an operator intervenes; it must
not be submitted a second time.
