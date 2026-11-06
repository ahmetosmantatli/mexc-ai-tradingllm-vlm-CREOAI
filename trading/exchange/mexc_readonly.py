"""MEXC private READ-ONLY client stub (balance / positions).

Phase 1: signing-ready helpers only. Never places, cancels, or modifies orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from trading.exchange.hmac_signing import sign_mexc_request
from trading.exchange.order_deny import assert_not_private_order_path


# Read-only path prefixes that are allowed when live HTTP is wired later.
READONLY_PATH_ALLOWLIST: tuple[str, ...] = (
    "/api/v1/private/account/assets",
    "/api/v1/private/position/open_positions",
    "/api/v1/private/position/list/history_positions",
    "/api/v1/private/order/list/open_orders",  # read open orders — not submit
)


@dataclass
class MexcReadOnlyClient:
    api_key: str = ""
    api_secret: str = ""
    base_url: str = "https://contract.mexc.com"
    recv_window_ms: int = 5000
    timeout_seconds: float = 10.0
    _http: httpx.Client | None = field(default=None, repr=False)

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(base_url=self.base_url, timeout=self.timeout_seconds)
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def signed_headers(
        self,
        *,
        params: dict[str, object] | None = None,
        body: str = "",
    ) -> dict[str, str]:
        signed = sign_mexc_request(
            self.api_secret,
            params=params,
            body=body,
            recv_window_ms=self.recv_window_ms,
        )
        return {
            "ApiKey": self.api_key,
            "Request-Time": str(signed["timestamp"]),
            "Recv-Window": str(signed["recvWindow"]),
            "Signature": str(signed["signature"]),
        }

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """HTTP helper that refuses private order mutation paths."""
        assert_not_private_order_path(path)
        return self._client().request(method, path, **kwargs)

    def get_account_assets_stub(self) -> dict[str, Any]:
        return {
            "assets": [],
            "source": "stub",
            "note": "Read-only stub; wire real GET when read-only key is configured.",
        }

    def get_account_assets(self) -> dict[str, Any]:
        """Fetch account assets for equity view only — never places orders.

        When keys are missing, returns the empty stub. Real HTTP uses the
        read-only assets allowlist path; private order mutation paths are denied.
        """
        if not self.api_key or not self.api_secret:
            return self.get_account_assets_stub()
        path = "/api/v1/private/account/assets"
        assert_not_private_order_path(path)
        headers = self.signed_headers()
        try:
            response = self.request("GET", path, headers=headers)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                data.setdefault("source", "mexc")
                return data
            return {"assets": data, "source": "mexc"}
        except Exception as exc:  # noqa: BLE001 — equity view degrades to stub
            return {
                "assets": [],
                "source": "error",
                "note": f"read-only assets fetch failed: {exc}",
            }

    def get_open_positions_stub(self) -> dict[str, Any]:
        return {
            "positions": [],
            "source": "stub",
        }
