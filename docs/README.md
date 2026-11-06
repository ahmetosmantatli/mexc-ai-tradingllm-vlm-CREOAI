# Docs

Project planning documents live in the Cursor Project store:

- `mexc-hybrid-trading-plan.md`
- `cursor-master-prompt.md`

## Equity / capital (runtime)

- `STARTING_CAPITAL_TRY` / `CAPITAL_FLOOR_TRY` in `.env` are **seed defaults only** (`örnek / seed, gerçek kasa değil`).
- First boot or empty DB: seed E0/F from those values (operator may use 500, 700, or any deposit).
- Paper mode: after first run, equity comes from persisted `equity_snapshots` (Postgres; optional Redis cache). Restart restores last equity — does **not** reset to 700 unless DB empty or `EQUITY_RESET=true`.
- MEXC read-only keys: prefer syncing available balance into `/equity` (TRY vs USDT display). Orders stay paper while `LIVE_TRADING_ENABLED=false`.
- Risk / sizing use **current equity E**, never the static env constant alone.
- Deposit / floor: `POST /equity/deposit`, `POST /equity/floor`, `POST /equity/reset`.
