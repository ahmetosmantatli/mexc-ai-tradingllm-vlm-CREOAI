"""MEXC public market-data client (real HTTP; injectable transport for tests)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from trading.exchange.order_deny import assert_not_private_order_path


class PublicMarketError(ValueError):
    def __init__(self,code):
        super().__init__('MEXC public request rejected')
        self.public_error_code=code if type(code) is int and 0<=code<=1000000000 else None


class HttpTransport(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response: ...


@dataclass
class TickerSnapshot:
    symbol: str
    last_price: float | None = None
    bid1: float | None = None
    ask1: float | None = None
    quote_volume_24h: float | None = None
    fee_free: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def spread_bps(self) -> float | None:
        if self.bid1 is None or self.ask1 is None or self.bid1 <= 0:
            return None
        mid = (self.bid1 + self.ask1) / 2.0
        if mid <= 0:
            return None
        return ((self.ask1 - self.bid1) / mid) * 10_000.0


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_ticker_row(row: dict[str, Any], symbol_hint: str | None = None) -> TickerSnapshot:
    symbol = str(row.get("symbol") or row.get("symbolName") or symbol_hint or "").upper()
    last = _as_float(
        row.get("lastPrice")
        or row.get("lastPrice")
        or row.get("fairPrice")
        or row.get("price")
    )
    bid = _as_float(row.get("bid1") or row.get("bidPrice") or row.get("bestBidPrice"))
    ask = _as_float(row.get("ask1") or row.get("askPrice") or row.get("bestAskPrice"))
    vol = _as_float(
        row.get("amount24")
        or row.get("volume24")
        or row.get("quoteVolume")
        or row.get("turnover24")
    )
    fee_free = bool(row.get("feeFree") or row.get("isZeroFee") or row.get("zeroFee"))
    return TickerSnapshot(
        symbol=symbol,
        last_price=last,
        bid1=bid,
        ask1=ask,
        quote_volume_24h=vol,
        fee_free=fee_free,
        raw=row,
    )


@dataclass
class MexcPublicClient:
    """Public REST client for ticker / kline / depth / funding / contract list.

    Inject ``transport`` (httpx MockTransport or custom) to avoid network in tests.
    Order / private mutation paths are denied.
    """

    base_url: str = "https://api.mexc.com"
    timeout_seconds: float = 10.0
    transport: httpx.BaseTransport | None = field(default=None, repr=False)
    _http: httpx.Client | None = field(default=None, repr=False)
    # Optional in-memory fixtures used when ``use_fixtures`` is True (deterministic tests).
    use_fixtures: bool = False
    fixture_tickers: dict[str, TickerSnapshot] = field(default_factory=dict)
    fixture_klines: dict[str, list[Any]] = field(default_factory=dict)
    fixture_symbols: list[str] = field(default_factory=list)
    fixture_fee_free: list[str] = field(default_factory=list)

    def _client(self) -> httpx.Client:
        if self._http is None:
            kwargs: dict[str, Any] = {
                "base_url": self.base_url.rstrip("/"),
                "timeout": self.timeout_seconds,
            }
            if self.transport is not None:
                kwargs["transport"] = self.transport
            self._http = httpx.Client(**kwargs)
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        assert_not_private_order_path(path)
        return self._client().request(method, path, **kwargs)

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.request(method, path, **kwargs)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and (payload.get("success") is False or payload.get("code", 0) != 0):
            raise PublicMarketError(payload.get("code"))
        return payload

    def list_contract_symbols(self) -> list[str]:
        if self.use_fixtures:
            return list(self.fixture_symbols)
        payload = self._json("GET", "/api/v1/contract/detail")
        data = payload.get("data") if isinstance(payload, dict) else payload
        symbols: list[str] = []
        if isinstance(data, list):
            for row in data:
                if isinstance(row, dict) and row.get("symbol"):
                    symbols.append(str(row["symbol"]).upper())
        return symbols

    def get_ticker(self, symbol: str) -> TickerSnapshot:
        sym = symbol.upper()
        if self.use_fixtures:
            if sym in self.fixture_tickers:
                return self.fixture_tickers[sym]
            return TickerSnapshot(symbol=sym, last_price=None)

        payload = self._json("GET", "/api/v1/contract/ticker", params={"symbol": sym})
        data = payload.get("data") if isinstance(payload, dict) else payload
        if isinstance(data, list) and data:
            row = next((r for r in data if str(r.get("symbol", "")).upper() == sym), data[0])
            return _parse_ticker_row(row, sym)
        if isinstance(data, dict):
            return _parse_ticker_row(data, sym)
        return TickerSnapshot(symbol=sym)

    def get_all_tickers(self) -> list[TickerSnapshot]:
        if self.use_fixtures:
            return list(self.fixture_tickers.values())
        payload = self._json("GET", "/api/v1/contract/ticker")
        data = payload.get("data") if isinstance(payload, dict) else payload
        out: list[TickerSnapshot] = []
        if isinstance(data, list):
            for row in data:
                if isinstance(row, dict):
                    out.append(_parse_ticker_row(row))
        return out

    def get_klines(
        self,
        symbol: str,
        interval: str = "Min15",
        limit: int = 100,
    ) -> dict[str, Any]:
        sym = symbol.upper()
        if self.use_fixtures:
            candles = list(self.fixture_klines.get(sym, []))
            return {
                "symbol": sym,
                "interval": interval,
                "limit": limit,
                "candles": candles[:limit],
                "source": "fixture",
            }

        path = f"/api/v1/contract/kline/{sym}"
        payload = self._json(
            "GET",
            path,
            params={"interval": interval},
        )
        data = payload.get("data") if isinstance(payload, dict) else payload
        candles: list[Any] = []
        if isinstance(data, dict):
            # Some responses nest time/open/high/low/close arrays.
            if "time" in data and "close" in data:
                times = data.get("time") or []
                opens = data.get("open") or []
                highs = data.get("high") or []
                lows = data.get("low") or []
                closes = data.get("close") or []
                vols = data.get("vol") or data.get("volume") or []
                for i in range(min(len(times), len(closes))):
                    candles.append(
                        [
                            times[i],
                            opens[i] if i < len(opens) else None,
                            highs[i] if i < len(highs) else None,
                            lows[i] if i < len(lows) else None,
                            closes[i],
                            vols[i] if i < len(vols) else None,
                        ]
                    )
            elif isinstance(data.get("kline"), list):
                candles = list(data["kline"])
        elif isinstance(data, list):
            candles = list(data)
        return {
            "symbol": sym,
            "interval": interval,
            "limit": limit,
            "candles": candles[-limit:],
            "source": "http",
        }

    def get_depth(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        sym = symbol.upper()
        if self.use_fixtures:
            return {"symbol": sym, "limit": limit, "bids": [], "asks": [], "source": "fixture"}
        payload = self._json("GET", f"/api/v1/contract/depth/{sym}", params={"limit": limit})
        data = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(data, dict):
            data = {}
        return {
            "symbol": sym,
            "limit": limit,
            "bids": data.get("bids") or [],
            "asks": data.get("asks") or [],
            "source": "http",
        }

    def get_funding_rate(self, symbol: str) -> dict[str, Any]:
        sym = symbol.upper()
        if self.use_fixtures:
            return {"symbol": sym, "fundingRate": None, "source": "fixture"}
        payload = self._json("GET", f"/api/v1/contract/funding_rate/{sym}")
        data = payload.get("data") if isinstance(payload, dict) else payload
        rate = None
        if isinstance(data, dict):
            rate = _as_float(data.get("fundingRate") or data.get("rate"))
        elif isinstance(data, list) and data and isinstance(data[0], dict):
            rate = _as_float(data[0].get("fundingRate") or data[0].get("rate"))
        return {"symbol": sym, "fundingRate": rate, "source": "http", "raw": data}

    def list_fee_free_candidates(self) -> list[str]:
        if self.use_fixtures:
            return list(self.fixture_fee_free)
        # Prefer explicit fee-free flag on contract detail when present.
        payload = self._json("GET", "/api/v1/contract/detail")
        data = payload.get("data") if isinstance(payload, dict) else payload
        out: list[str] = []
        if isinstance(data, list):
            for row in data:
                if not isinstance(row, dict) or not row.get("symbol"):
                    continue
                if row.get("feeFree") or row.get("isZeroFee") or row.get("zeroFee"):
                    out.append(str(row["symbol"]).upper())
        return out

    def get_contract_rules(self, symbol):
        from trading.exchange.contracts import ContractRules
        payload = self._json("GET", "/api/v1/contract/detail", params={"symbol": symbol})
        rows = payload.get("data")
        if isinstance(rows, dict):
            rows = [rows]
        row = next(r for r in rows if r.get("symbol") == symbol)
        return ContractRules.parse(row)

    def get_funding_history(self, symbol):
        payload = self._json("GET", "/api/v1/contract/funding_rate/history", params={"symbol": symbol, "page_num": 1, "page_size": 1000})
        data = payload["data"]
        rows = data["resultList"]
        if not isinstance(rows, list):
            raise ValueError("funding history incomplete; manual reconciliation required")
        return rows
