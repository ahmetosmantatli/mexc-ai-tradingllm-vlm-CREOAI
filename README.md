# MEXC Hybrid AI + Quant Trading

Paper-first futures automation: **Quant → AI (TradeProposal only) → Risk Engine → Paper Broker**.

Live trading is **gated off** (`LIVE_TRADING_ENABLED=false`). Product default risk profile is **Aggressive Growth** — that is a risk posture, not a profit promise.

> **Honesty:** Attempting ~700 → 7–9k TRY in ~1 week (or stretch toward ~100k) is extreme and has a **high probability of ruin**. Fee-free pairs reduce cost; they do not create edge. Always-on scanning increases fee drag. Full campaign wipe is an accepted operator risk; **single-trade full liquidation is forbidden**.

This repository currently delivers **Phase 1** (skeleton, config, public/read-only stubs, paper isolation, risk stub, tests). Phases 2+ (Quant, LangGraph, full risk loop, dashboard, live) are deferred.

---

## Safety rules (non-negotiable)

1. AI never places orders — only structured `TradeProposal` JSON.
2. Risk Engine is deterministic and non-bypassable; every accept/reject is audited.
3. **Paper Broker never calls MEXC private order / cancel / modify endpoints** (enforced by deny-list tests).
4. `LIVE_TRADING_ENABLED` defaults to `false`. No silent paper→live promotion.
5. Every accepted trade requires stop-loss logic; proposals without SL are rejected.
6. One stop-out must leave residual capital (single-trade wipe ban).
7. Secrets only via environment / Docker secrets — never commit `.env` or API keys.
8. Prefer a **new read-only** MEXC API key for Phase 1. No trade/withdraw permissions until a later live gate.

---

## Repository layout

```
apps/api/                 # FastAPI: /health, /settings
apps/worker/              # Phase 1 boot stub (loops arrive in Phase 2)
trading/
  config.py               # Pydantic settings
  quant/                  # Phase 2
  agents/                 # LangGraph — Phase 2
  risk/                   # Presets + Risk Engine stub
  brokers/paper/          # Simulated fills only
  brokers/live/           # Gated OFF stub
  exchange/               # Public stubs, read-only stub, HMAC helpers
  models/                 # TradeProposal + SQLAlchemy records
infrastructure/           # docker-compose, postgres, redis
tests/unit|integration|replay/
.env.example
Dockerfile
```

Compose project name: **`mexc-ai`** (kept separate from Creotora on a shared Hetzner VPS).

---

## Local run

### Prerequisites

- Python 3.11+
- Docker + Docker Compose (for full stack)
- Copy env template (no secrets in git):

```bash
cp .env.example .env
# Edit .env locally. Do not commit it.
```

### Install + tests (host)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
pytest -q
```

### API only (host)

```bash
uvicorn apps.api.main:app --host 127.0.0.1 --port 8080
curl -s http://127.0.0.1:8080/health
curl -s http://127.0.0.1:8080/settings
```

### Full stack (Compose)

```bash
cd infrastructure
docker compose --env-file ../.env up --build -d
docker compose ps
curl -s http://127.0.0.1:8080/health
```

Services: `api`, `worker`, `postgres`, `redis`. API is bound to **127.0.0.1:8080** only.

---

## Important environment variables

| Variable | Default | Meaning |
|----------|---------|---------|
| `LIVE_TRADING_ENABLED` | `false` | Hard gate for live order path |
| `BROKER_MODE` | `paper` | Active broker (`paper` / `live`) |
| `RISK_PROFILE` | `aggressive_growth` | Default preset |
| `STARTING_CAPITAL_TRY` | `700` | Paper deposit basis `E0` |
| `CAPITAL_FLOOR_TRY` | `700` | Floor `F` (default = `E0`) |
| `ALWAYS_ON_SCAN` | `true` | Continuous scan preference |
| `FEE_FREE_PREFERENCE` | `true` | Prefer zero-commission pairs when suitable |
| `DATABASE_URL` | Compose postgres URL | SQLAlchemy DSN |
| `REDIS_URL` | Compose redis URL | Feature / cache bus |
| `MEXC_API_KEY` / `MEXC_API_SECRET` | empty | Read-only key for later private reads |
| `COMPOSE_PROJECT_NAME` | `mexc-ai` | Isolate from Creotora |

See `.env.example` for the full template.

---

## Hetzner notes (no secrets)

- Run as a **separate Compose project** (`name: mexc-ai`) beside Creotora.
- CPU/RAM **limits are placeholders** in `infrastructure/docker-compose.yml` — tune after measuring free headroom so Creotora is not starved.
- Avoid host port clashes; this stack publishes API on localhost `8080` only and does not publish Postgres/Redis by default.
- Use SSH keys / Cursor secrets for access. **Never** put Hetzner passwords, MEXC secrets, or private keys in git, docs, or chat logs.
- Dashboard (Phase 3) should stay private (VPN / auth / tunnel) — not a public bind.

---

## Phase 1 status vs later phases

**In this PR**

- Repo layout, Docker Compose, Dockerfile, `.env.example`
- Pydantic settings (live off, AG default, floor / always-on fields)
- MEXC public + read-only stubs, HMAC helpers, order-path deny-list
- PaperBroker + Broker interface (no private order HTTP)
- Risk Engine stub: mandatory SL, max-loss / residual, floor threat, always-on flag
- SQLAlchemy models: orders, fills, positions, equity, decisions
- FastAPI `/health` + `/settings`
- pytest: config defaults, paper isolation, risk reject-without-SL

**Deferred (Phase 2+)**

- Quant signals, LangGraph evaluator, full paper loop + audit persistence
- Realistic paper fill models (spread, funding, partials, LIQ proximity)
- Private dashboard, kill-switch UI, metrics
- Paper validation window / replay fixtures
- Live broker order placement (Phase 5 only, explicit gate)

---

## License / disclaimer

Software is provided for engineering and research. **No profitability is guaranteed.** Trading futures with leverage can result in total loss of capital.
