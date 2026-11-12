# HTF OB rejection watch (parallel family)

Parallel WATCH cards (`evidence.kind=htf_ob_reject_watch`) for HTF order-block
rejection toward a still-valid weak extreme. Does **not** auto-enter and does
not change early `confirm_bar` / `ob_retest` or `ZONE_CARDS_READY`.

Stages: `ob_armed` → `prox_alert` (~1–2% of OB) → `htf_wick_ok` → `ltf_choch`.
Close through the OB distal invalidates the watch.

When an open position faces the opposing threat (`prox_alert`+), management
links defensively: tighten stop / prefer shorter TP; never flip side.

Module: `trading/quant/htf_ob_reject_watch.py`. Tests: `tests/unit/test_htf_ob_reject_watch.py`.

Worker-only deploy (no one-shot resize of existing opens):

```sh
cd /opt/mexc-ai-trading && git pull --ff-only && docker compose --env-file .env -f infrastructure/docker-compose.yml -p mexc-ai up -d --build --no-deps worker
```
