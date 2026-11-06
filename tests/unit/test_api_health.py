"""FastAPI health + settings + equity skeleton."""

from __future__ import annotations

from fastapi.testclient import TestClient

import apps.api.main as api_main
from apps.api.main import app
from trading.config import Settings, get_settings
from trading.equity.service import EquityService
from trading.equity.store import InMemoryEquityStore


def _client_with_memory_equity() -> TestClient:
    get_settings.cache_clear()
    settings = Settings(_env_file=None)
    api_main._equity_service = EquityService(  # noqa: SLF001
        settings=settings,
        store=InMemoryEquityStore(),
    )
    return TestClient(app)


def test_health_ok() -> None:
    client = _client_with_memory_equity()
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["live_trading_enabled"] is False
    assert body["broker_mode"] == "paper"
    assert body["risk_profile"] == "aggressive_growth"
    assert body["always_on_scan"] is True


def test_settings_route_no_secrets() -> None:
    client = _client_with_memory_equity()
    resp = client.get("/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert "mexc_api_secret" not in body
    assert body["live_trading_enabled"] is False
    assert body["capital_floor_try"] == 700.0
    assert body["starting_capital_is_seed_only"] is True


def test_equity_route_seed_then_deposit() -> None:
    client = _client_with_memory_equity()
    first = client.get("/equity")
    assert first.status_code == 200
    body = first.json()
    assert body["equity"] == 700.0
    assert body["source"] == "seed"
    assert body["orders_are_paper"] is True

    deposited = client.post("/equity/deposit", json={"amount_try": 300.0})
    assert deposited.status_code == 200
    dbody = deposited.json()
    assert dbody["equity"] == 1000.0
    assert dbody["starting_capital"] == 1000.0
    assert dbody["capital_floor"] == 1000.0
    assert dbody["source"] == "deposit_api"
