"""FastAPI health + settings skeleton."""

from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.main import app
from trading.config import get_settings


def test_health_ok() -> None:
    get_settings.cache_clear()
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["live_trading_enabled"] is False
    assert body["broker_mode"] == "paper"
    assert body["risk_profile"] == "aggressive_growth"
    assert body["always_on_scan"] is True


def test_settings_route_no_secrets() -> None:
    get_settings.cache_clear()
    client = TestClient(app)
    resp = client.get("/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert "mexc_api_secret" not in body
    assert body["live_trading_enabled"] is False
    assert body["capital_floor_try"] == 700.0
