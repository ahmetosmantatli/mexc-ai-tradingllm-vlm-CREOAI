"""Quant strategy stubs: trend, momentum, mean-reversion → CandidateSignal."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Sequence

from trading.models.domain import Side
from trading.quant.signals import CandidateSignal, StrategyName


def _closes_from_klines(klines: Sequence[dict[str, Any]] | Sequence[Sequence[Any]]) -> list[float]:
    closes: list[float] = []
    for row in klines:
        if isinstance(row, dict):
            for key in ("close", "c", "Close"):
                if key in row and row[key] is not None:
                    closes.append(float(row[key]))
                    break
        elif isinstance(row, (list, tuple)) and len(row) >= 5:
            # MEXC kline often: [time, open, high, low, close, volume, ...]
            closes.append(float(row[4]))
    return closes


def _sma(values: Sequence[float], window: int) -> float | None:
    if len(values) < window or window <= 0:
        return None
    chunk = values[-window:]
    return sum(chunk) / float(window)


def _roc(values: Sequence[float], lookback: int) -> float | None:
    if len(values) <= lookback or lookback <= 0:
        return None
    prev = values[-(lookback + 1)]
    if prev == 0:
        return None
    return (values[-1] - prev) / prev


@dataclass
class StrategyContext:
    symbol: str
    last_price: float
    klines: Sequence[dict[str, Any]] | Sequence[Sequence[Any]]
    fee_free_pair: bool = False
    quote_volume_24h: float | None = None


class Strategy(ABC):
    name: StrategyName

    @abstractmethod
    def evaluate(self, ctx: StrategyContext) -> CandidateSignal | None:
        raise NotImplementedError


@dataclass
class TrendStrategy(Strategy):
    """Simple SMA crossover stub (fast vs slow)."""

    name: StrategyName = StrategyName.TREND
    fast_window: int = 8
    slow_window: int = 21
    stop_pct: float = 0.02
    target_rr: float = 2.0
    min_confidence: float = 0.45

    def evaluate(self, ctx: StrategyContext) -> CandidateSignal | None:
        closes = _closes_from_klines(ctx.klines)
        if not closes:
            closes = [ctx.last_price]
        fast = _sma(closes, self.fast_window)
        slow = _sma(closes, self.slow_window)
        if fast is None or slow is None or ctx.last_price <= 0:
            return None
        spread = (fast - slow) / ctx.last_price
        if abs(spread) < 0.001:
            return None
        side = Side.LONG if spread > 0 else Side.SHORT
        entry = ctx.last_price
        if side == Side.LONG:
            stop = entry * (1 - self.stop_pct)
            target = entry * (1 + self.stop_pct * self.target_rr)
        else:
            stop = entry * (1 + self.stop_pct)
            target = entry * (1 - self.stop_pct * self.target_rr)
        confidence = min(0.95, self.min_confidence + abs(spread) * 20)
        return CandidateSignal(
            symbol=ctx.symbol,
            side=side,
            strategy=self.name,
            entry_price=entry,
            stop_loss_price=stop,
            take_profit_price=target,
            confidence=confidence,
            rationale=f"trend SMA{self.fast_window}/{self.slow_window} spread={spread:.4f}",
            fee_free_pair=ctx.fee_free_pair,
            features={"fast_sma": fast, "slow_sma": slow, "spread": spread},
        )


@dataclass
class MomentumStrategy(Strategy):
    """Rate-of-change momentum stub."""

    name: StrategyName = StrategyName.MOMENTUM
    lookback: int = 12
    threshold: float = 0.015
    stop_pct: float = 0.02
    target_rr: float = 1.8
    min_confidence: float = 0.4

    def evaluate(self, ctx: StrategyContext) -> CandidateSignal | None:
        closes = _closes_from_klines(ctx.klines)
        if not closes:
            closes = [ctx.last_price]
        roc = _roc(closes, self.lookback)
        if roc is None or abs(roc) < self.threshold or ctx.last_price <= 0:
            return None
        side = Side.LONG if roc > 0 else Side.SHORT
        entry = ctx.last_price
        if side == Side.LONG:
            stop = entry * (1 - self.stop_pct)
            target = entry * (1 + self.stop_pct * self.target_rr)
        else:
            stop = entry * (1 + self.stop_pct)
            target = entry * (1 - self.stop_pct * self.target_rr)
        confidence = min(0.95, self.min_confidence + abs(roc) * 8)
        return CandidateSignal(
            symbol=ctx.symbol,
            side=side,
            strategy=self.name,
            entry_price=entry,
            stop_loss_price=stop,
            take_profit_price=target,
            confidence=confidence,
            rationale=f"momentum ROC({self.lookback})={roc:.4f}",
            fee_free_pair=ctx.fee_free_pair,
            features={"roc": roc, "lookback": self.lookback},
        )


@dataclass
class MeanReversionStrategy(Strategy):
    """Fade stretched moves vs SMA mean."""

    name: StrategyName = StrategyName.MEAN_REVERSION
    window: int = 20
    z_entry: float = 0.02
    stop_pct: float = 0.015
    target_rr: float = 1.5
    min_confidence: float = 0.4

    def evaluate(self, ctx: StrategyContext) -> CandidateSignal | None:
        closes = _closes_from_klines(ctx.klines)
        if not closes:
            closes = [ctx.last_price]
        mean = _sma(closes, self.window)
        if mean is None or mean <= 0 or ctx.last_price <= 0:
            return None
        deviation = (ctx.last_price - mean) / mean
        if abs(deviation) < self.z_entry:
            return None
        # Fade: price above mean → short; below → long
        side = Side.SHORT if deviation > 0 else Side.LONG
        entry = ctx.last_price
        if side == Side.LONG:
            stop = entry * (1 - self.stop_pct)
            target = min(mean, entry * (1 + self.stop_pct * self.target_rr))
        else:
            stop = entry * (1 + self.stop_pct)
            target = max(mean, entry * (1 - self.stop_pct * self.target_rr))
        confidence = min(0.9, self.min_confidence + abs(deviation) * 10)
        return CandidateSignal(
            symbol=ctx.symbol,
            side=side,
            strategy=self.name,
            entry_price=entry,
            stop_loss_price=stop,
            take_profit_price=target,
            confidence=confidence,
            rationale=f"mean-reversion deviation={deviation:.4f} vs SMA{self.window}",
            fee_free_pair=ctx.fee_free_pair,
            features={"mean": mean, "deviation": deviation},
        )


DEFAULT_STRATEGIES: tuple[Strategy, ...] = (
    TrendStrategy(),
    MomentumStrategy(),
    MeanReversionStrategy(),
)


def run_strategies(
    ctx: StrategyContext,
    strategies: Sequence[Strategy] | None = None,
) -> list[CandidateSignal]:
    active = strategies or DEFAULT_STRATEGIES
    out: list[CandidateSignal] = []
    for strategy in active:
        signal = strategy.evaluate(ctx)
        if signal is not None:
            out.append(signal)
    return out
