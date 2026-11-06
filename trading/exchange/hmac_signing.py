"""HMAC-SHA256 helpers for future MEXC private read-only calls.

Phase 1: signing utilities only. No order-create endpoints are invoked.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Mapping
from urllib.parse import urlencode


def current_timestamp_ms() -> int:
    return int(time.time() * 1000)


def build_query_string(params: Mapping[str, object] | None) -> str:
    """Stable query string: sorted keys, skip None values."""
    if not params:
        return ""
    items = [(k, v) for k, v in params.items() if v is not None]
    items.sort(key=lambda kv: kv[0])
    return urlencode(items, doseq=True)


def sign_payload(secret: str, payload: str) -> str:
    """Return hex HMAC-SHA256 digest of payload with secret."""
    if not secret:
        raise ValueError("MEXC API secret is required for signing")
    digest = hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    )
    return digest.hexdigest()


def sign_mexc_request(
    secret: str,
    *,
    api_key: str,
    params: Mapping[str, object] | None = None,
    body: str = "",
    timestamp_ms: int | None = None,
    recv_window_ms: int = 5000,
) -> dict[str, str | int]:
    """Contract signature = HMAC(secret, api_key + request_time + sorted query/body).

    Request-Time and Recv-Window are headers, never inserted into signed query.
    """
    ts = timestamp_ms if timestamp_ms is not None else current_timestamp_ms()
    if not api_key:
        raise ValueError("MEXC API key is required")
    query = build_query_string(params)
    signature = sign_payload(secret, f"{api_key}{ts}{body or query}")
    return {"timestamp": ts, "recvWindow": recv_window_ms,
            "signature": signature, "query_string": query}
