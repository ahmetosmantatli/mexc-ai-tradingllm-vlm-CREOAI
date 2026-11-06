"""MEXC public market-data client stubs (Phase 1 — no live HTTP required)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from trading.exchange.order_deny import assert_not_private_order_path


@dataclass
class MexcPublicClient:
    """Public REST stubs for kline / ticker / depth / funding.

    Phase 1 returns structured placeholders so Quant/Risk wiring can proceed.
    Optional httpx client is injected for future real calls; order paths denied.
    """

    base_url: str = "https://contract.mexc.com"
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

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        assert_not_private_order_path(path)
        return self._client().request(method, path, **kwargs)

    # --- Stubs (deterministic placeholders; no network in unit tests) ---

    def get_ticker(self, symbol: str) -> dict[str, Any]:
        return {
            "symbol": symbol.upper(),
            "lastPrice": None,
            "bid1": None,
            "ask1": None,
            "source": "stub",
        }

    def get_klines(
        self,
        symbol: str,
        interval: str = "Min15",
        limit: int = 100,
    ) -> dict[str, Any]:
        return {
            "symbol": symbol.upper(),
            "interval": interval,
            "limit": limit,
            "candles": [],
            "source": "stub",
        }

    def get_depth(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        return {
            "symbol": symbol.upper(),
            "limit": limit,
            "bids": [],
            "asks": [],
            "source": "stub",
        }

    def get_funding_rate(self, symbol: str) -> dict[str, Any]:
        return {
            "symbol": symbol.upper(),
            "fundingRate": None,
            "source": "stub",
        }

    def list_fee_free_candidates(self) -> list[str]:
        """Placeholder metadata for fee-free preference filtering."""
        return []
