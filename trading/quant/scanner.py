"""Market scanner + liquidity filter over public MEXC data."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from trading.exchange.mexc_public import MexcPublicClient, TickerSnapshot
from trading.quant.signals import CandidateSignal
from trading.quant.strategies import (
    DEFAULT_STRATEGIES,
    Strategy,
    StrategyContext,
    run_strategies,
)


@dataclass
class LiquidityFilter:
    """Drop illiquid / junk listings before strategy evaluation."""

    min_quote_volume_24h: float = 500_000.0
    max_spread_bps: float = 25.0
    require_last_price: bool = True

    def passes(self, ticker: TickerSnapshot) -> tuple[bool, str]:
        if self.require_last_price and (ticker.last_price is None or ticker.last_price <= 0):
            return False, "NO_LAST_PRICE"
        if ticker.quote_volume_24h is not None and ticker.quote_volume_24h < self.min_quote_volume_24h:
            return False, "LOW_QUOTE_VOLUME"
        if ticker.spread_bps is not None and ticker.spread_bps > self.max_spread_bps:
            return False, "WIDE_SPREAD"
        return True, "OK"


@dataclass
class ScanResult:
    scanned: int
    kept: int
    filtered_out: dict[str, str] = field(default_factory=dict)
    candidates: list[CandidateSignal] = field(default_factory=list)


@dataclass
class MarketScanner:
    """Scan symbols via public client; apply liquidity filter; emit candidates."""

    client: MexcPublicClient
    liquidity: LiquidityFilter = field(default_factory=LiquidityFilter)
    strategies: Sequence[Strategy] = field(default_factory=lambda: DEFAULT_STRATEGIES)
    kline_interval: str = "Min15"
    kline_limit: int = 60
    prefer_fee_free: bool = True
    max_symbols: int = 40

    def scan(self, symbols: Sequence[str] | None = None) -> ScanResult:
        fee_free = set(s.upper() for s in self.client.list_fee_free_candidates())
        universe = list(symbols) if symbols is not None else self.client.list_contract_symbols()
        if self.prefer_fee_free and fee_free and symbols is None:
            # Prefer fee-free first, then fill remaining slots.
            preferred = [s for s in universe if s.upper() in fee_free]
            others = [s for s in universe if s.upper() not in fee_free]
            universe = preferred + others
        universe = universe[: self.max_symbols]

        filtered_out: dict[str, str] = {}
        candidates: list[CandidateSignal] = []
        kept = 0

        for symbol in universe:
            sym = symbol.upper()
            ticker = self.client.get_ticker(sym)
            ok, reason = self.liquidity.passes(ticker)
            if not ok:
                filtered_out[sym] = reason
                continue
            kept += 1
            klines = self.client.get_klines(sym, interval=self.kline_interval, limit=self.kline_limit)
            ctx = StrategyContext(
                symbol=sym,
                last_price=float(ticker.last_price or 0.0),
                klines=klines.get("candles") or [],
                fee_free_pair=sym in fee_free or bool(ticker.fee_free),
                quote_volume_24h=ticker.quote_volume_24h,
            )
            candidates.extend(run_strategies(ctx, self.strategies))

        candidates.sort(key=lambda c: c.confidence, reverse=True)
        return ScanResult(
            scanned=len(universe),
            kept=kept,
            filtered_out=filtered_out,
            candidates=candidates,
        )

    def scan_to_dicts(self, symbols: Sequence[str] | None = None) -> dict[str, Any]:
        result = self.scan(symbols)
        return {
            "scanned": result.scanned,
            "kept": result.kept,
            "filtered_out": result.filtered_out,
            "candidates": [c.model_dump(mode="json") for c in result.candidates],
        }
