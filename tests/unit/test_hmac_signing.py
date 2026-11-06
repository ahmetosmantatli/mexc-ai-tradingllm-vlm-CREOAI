"""HMAC signing helper smoke tests (no network)."""

from __future__ import annotations

from trading.exchange.hmac_signing import build_query_string, sign_mexc_request, sign_payload


def test_sign_payload_deterministic() -> None:
    sig = sign_payload("secret", "foo=1&timestamp=1")
    assert sig == sign_payload("secret", "foo=1&timestamp=1")
    assert len(sig) == 64


def test_build_query_string_sorted() -> None:
    assert build_query_string({"b": 2, "a": 1}) == "a=1&b=2"


def test_sign_mexc_request_includes_fields() -> None:
    result = sign_mexc_request(
        "test-secret",
        params={"symbol": "BTC_USDT"},
        timestamp_ms=1_700_000_000_000,
        recv_window_ms=5000,
    )
    assert result["timestamp"] == 1_700_000_000_000
    assert result["recvWindow"] == 5000
    assert isinstance(result["signature"], str)
    assert "symbol=BTC_USDT" in str(result["query_string"])
