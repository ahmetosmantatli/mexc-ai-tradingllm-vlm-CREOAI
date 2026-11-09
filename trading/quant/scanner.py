"""Market scanner + liquidity filter over public MEXC data."""

from __future__ import annotations

from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Sequence
import time
import sqlite3
import httpx

from trading.exchange.mexc_public import MexcPublicClient, TickerSnapshot, PublicMarketError
from trading.exchange.public_governor import PublicDataDeferred
from trading.quant.signals import CandidateSignal, StrategyName
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
    require_complete: bool = False

    def passes(self, ticker: TickerSnapshot) -> tuple[bool, str]:
        if self.require_complete and (ticker.quote_volume_24h is None or ticker.spread_bps is None):
            return False,'MISSING_LIQUIDITY_DATA'
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
    errors: dict[str,str] = field(default_factory=dict)
    duration_seconds: float = 0.0


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
    max_workers: int = 4
    rotation_store: object | None = None
    bulk_tickers: bool = False
    structure_setups_enabled: bool = False
    multihorizon: bool = False
    strict_ict: bool = False
    htf_zone_entries: bool = False

    def scan(self, symbols: Sequence[str] | None = None) -> ScanResult:
        started=time.monotonic()
        errors={}
        try:
            fee_free = set(s.upper() for s in self.client.list_fee_free_candidates())
        except sqlite3.Error:
            raise
        except Exception as exc:
            fee_free=set()
            errors['fee_free_metadata']=type(exc).__name__
        try:
            universe = list(symbols) if symbols is not None else self.client.list_contract_symbols()
        except sqlite3.Error:raise
        except Exception as exc:
            errors['universe']=('PUBLIC_COOLDOWN' if isinstance(exc,PublicDataDeferred) else
                'MEXC_CODE_'+str(exc.public_error_code) if isinstance(exc,PublicMarketError) else type(exc).__name__)
            return ScanResult(scanned=0,kept=0,errors=errors,duration_seconds=time.monotonic()-started)
        if self.prefer_fee_free and fee_free and symbols is None:
            # Prefer fee-free first, then fill remaining slots.
            preferred = [s for s in universe if s.upper() in fee_free]
            others = [s for s in universe if s.upper() not in fee_free]
            universe = preferred + others
        if self.rotation_store is not None and symbols is None:
            from trading.runtime.universe import select_universe
            priority = []
            if self.htf_zone_entries:
                tracked = self.rotation_store.get('htf_zone_scan', {})
                priority = sorted((symbol for symbol, row in tracked.items() if row.get('priority_until', 0) > time.time()),
                                  key=lambda symbol: tracked[symbol]['last_scanned_at'])
            universe=select_universe(self.rotation_store,universe,self.max_symbols,priority=priority)
        else:
            universe = universe[: self.max_symbols]

        tickers=None
        if self.bulk_tickers:
            try:tickers={t.symbol:t for t in self.client.get_all_tickers()}
            except sqlite3.Error:raise
            except Exception as exc:
                errors['bulk_tickers']=('PUBLIC_COOLDOWN' if isinstance(exc,PublicDataDeferred) else
                    'MEXC_CODE_'+str(exc.public_error_code) if isinstance(exc,PublicMarketError) else type(exc).__name__)
                return ScanResult(scanned=len(universe),kept=0,candidates=[],errors=errors,
                    duration_seconds=time.monotonic()-started)
        filtered_out: dict[str, str] = {}
        candidates: list[CandidateSignal] = []
        kept = 0
        htf_tracking = self.rotation_store.get('htf_zone_scan', {}) if self.htf_zone_entries and self.rotation_store else {}
        htf_events = []

        def inspect(symbol):
            sym=symbol.upper()
            passed=False
            try:
                if tickers is not None and sym not in tickers:return sym,False,[],'NO_TICKER',None,None
                ticker=tickers[sym] if tickers is not None else self.client.get_ticker(sym)
                ok,reason=self.liquidity.passes(ticker)
                if not ok:
                    return sym,False,[],reason,None,None
                passed=True
                if self.htf_zone_entries:
                    signals,tracking=self._htf_candidates(sym,ticker,fee_free)
                    return sym,passed,signals,None,None,tracking
                klines=self.client.get_klines(sym,interval=self.kline_interval,limit=self.kline_limit)
                candles=klines.get('candles') or []
                if not getattr(self.client,'use_fixtures',False):
                    candles=[c for c in candles if isinstance(c,list) and len(c)>=5 and float(c[0])+900<=time.time()]
                    if not candles or time.time()-(float(candles[-1][0])+900)>1200:
                        return sym,passed,[],'STALE_OR_MISSING_CANDLES',None,None
                ctx=StrategyContext(symbol=sym,last_price=float(ticker.last_price or 0),klines=candles,
                    fee_free_pair=sym in fee_free or bool(ticker.fee_free),quote_volume_24h=ticker.quote_volume_24h)
                produced=run_strategies(ctx,self.strategies)
                if self.structure_setups_enabled:
                    produced.extend(self._structure_candidates(sym,ticker,candles,'15m',fee_free))
                for candidate in produced:
                    candidate.features['observed_spread_bps']=ticker.spread_bps
                    if candles:
                        candidate.features.update(source_open_at=candles[-1][0],source_closed_at=float(candles[-1][0])+900)
                return sym,passed,produced,None,None,None
            except sqlite3.Error:
                raise
            except Exception as exc:
                category=('PUBLIC_COOLDOWN' if isinstance(exc,PublicDataDeferred) else 'MEXC_CODE_'+str(exc.public_error_code) if isinstance(exc,PublicMarketError)
                    else 'HTTP_'+str(exc.response.status_code) if isinstance(exc,httpx.HTTPStatusError)
                    else 'TIMEOUT' if isinstance(exc,httpx.TimeoutException) else 'NETWORK_ERROR' if isinstance(exc,httpx.TransportError)
                    else 'DATA_FORMAT' if isinstance(exc,(ValueError,TypeError,KeyError,IndexError)) else type(exc).__name__)
                return sym,passed,[],'SYMBOL_DATA_ERROR',category,None

        def inspect_horizons(symbol):
            sym,passed,produced,reason,error,tracking=inspect(symbol)
            horizon_errors={}
            if passed and self.multihorizon and self.structure_setups_enabled and not self.htf_zone_entries:
                from trading.analysis.market import TIMEFRAMES
                ticker=tickers[sym] if tickers is not None else None
                for tf in ('5m','4h'):
                    interval,_=TIMEFRAMES[tf]
                    try:
                        if ticker is None:
                            ticker=self.client.get_ticker(sym)
                        payload=self.client.get_klines(sym,interval=interval,limit=self.kline_limit)
                        if payload.get('symbol')!=sym or payload.get('interval')!=interval:
                            raise ValueError('unexpected horizon candle identity')
                        produced.extend(self._structure_candidates(sym,ticker,payload['candles'],tf,fee_free))
                    except sqlite3.Error:
                        raise
                    except Exception as exc:
                        horizon_errors[f'{sym}:{tf}']=('MEXC_CODE_'+str(exc.public_error_code)
                            if isinstance(exc,PublicMarketError) else type(exc).__name__)
            return sym,passed,produced,reason,error,horizon_errors,tracking

        # httpx clients may be shared between threads. Metadata calls above have
        # initialized the dedicated scan client's connection pool before workers.
        with ThreadPoolExecutor(max_workers=max(1,min(self.max_workers,4)),thread_name_prefix='market-scan') as pool:
            for sym,passed,produced,reason,error,horizon_errors,tracking in pool.map(inspect_horizons,universe):
                if tracking is not None:
                    previous = htf_tracking.get(sym, {}).get('setups', [])
                    htf_events.extend(dict(symbol=sym, at=tracking['last_scanned_at'], **setup)
                                      for setup in tracking['setups'] if setup not in previous)
                    htf_tracking[sym] = tracking
                elif self.htf_zone_entries and not passed and not error:
                    htf_tracking.pop(sym, None)
                errors.update(horizon_errors)
                kept+=int(passed)
                candidates.extend(produced)
                if reason:
                    filtered_out[sym]=reason
                if error:
                    errors[sym]=error

        if self.htf_zone_entries and self.rotation_store:
            cutoff=time.time()-3600
            self.rotation_store.put('htf_zone_scan', {symbol: row for symbol,row in htf_tracking.items()
                if row['last_scanned_at'] >= cutoff and row['setups']})
            if htf_events:
                self.rotation_store.put('htf_zone_events', (self.rotation_store.get('htf_zone_events', []) + htf_events)[-200:])
        candidates.sort(key=lambda c: c.confidence, reverse=True)
        return ScanResult(
            scanned=len(universe),
            kept=kept,
            filtered_out=filtered_out,
            candidates=candidates, errors=errors, duration_seconds=time.monotonic()-started,
        )

    def _htf_candidates(self,symbol,ticker,fee_free):
        from trading.analysis.market import build_bundle
        from trading.quant.structure_setups import SECONDS, build_htf_zone_setups
        from trading.runtime.runner3_policy import evidence_grade

        bundle=build_bundle(self.client,symbol,include_hourly=True)
        market=bundle.to_context(ict=True,htf_history=True)
        rules=self.client.get_contract_rules(symbol)
        options=build_htf_zone_setups(market,current_price=float(ticker.last_price),price_step=rules.price_step)
        signals=[]
        for setup in options:
            if setup['state'] != 'ready':
                continue
            frame=setup['trigger_timeframe']
            closed_at=setup['source_closed_at']
            grade=evidence_grade(setup)
            signals.append(CandidateSignal(symbol=symbol,side=setup['side'],strategy=StrategyName.STRUCTURE_SETUP,
                entry_price=setup['entry_reference'],stop_loss_price=setup['stop_loss_price'],
                take_profit_price=setup['take_profit_price'],confidence=.75,
                fee_free_pair=symbol in fee_free or bool(ticker.fee_free),
                rationale=f"{setup['context_timeframe']} bölgesi ve {frame} kapanış teyidi.",
                features=dict(structure_seed=True,setup_family=setup['family'],setup_id=setup['setup_id'],
                    htf_zone_seed=True,seed_timeframe=frame,observed_spread_bps=ticker.spread_bps,
                    capital_rank=float(setup['reward_risk'])*(1.25 if grade=='strong' else 1),
                    evidence_grade=grade,evidence_kind=setup['evidence'].get('kind'),
                    reward_risk=setup['reward_risk'],
                    source_open_at=closed_at-SECONDS[frame],source_closed_at=closed_at)))
        now=market['observed_at']
        touched=any(setup['state']=='watch' and setup['evidence'].get('touch_at') for setup in options)
        tracking=dict(last_scanned_at=now,priority_until=now+1200 if touched else 0,
                      setups=[{key:setup[key] for key in ('state','side','context_timeframe','trigger_timeframe','evidence')}
                              for setup in options])
        return signals,tracking

    def _structure_candidates(self,symbol,ticker,rows,timeframe,fee_free):
        from trading.analysis.market import TIMEFRAMES, closed_candles, ict_features, structure_features
        from trading.quant.structure_setups import build_structure_setups

        _,seconds=TIMEFRAMES[timeframe]
        observed=time.time()
        closed=closed_candles(rows,seconds,observed)
        features=structure_features(closed)
        if self.strict_ict:
            features.update(ict_features(closed,features))
        market=dict(symbol=symbol,observed_at=observed,timeframes=[dict(
            timeframe=timeframe,features=features,recent_ohlcv=[c.row() for c in closed[-12:]])])
        options=build_structure_setups(market,current_price=float(ticker.last_price),
            trigger_timeframe=timeframe if self.multihorizon else None,strict_ict=self.strict_ict)
        return [CandidateSignal(symbol=symbol,side=setup['side'],strategy=StrategyName.STRUCTURE_SETUP,
            entry_price=float(ticker.last_price),stop_loss_price=setup['stop_loss_price'],
            take_profit_price=setup['take_profit_price'],confidence=.65,
            fee_free_pair=symbol in fee_free or bool(ticker.fee_free),
            rationale=f'Kapanmış {timeframe} yapısal ön eleme; tam piyasa bağlamı ayrıca değerlendirilecek.',
            features=dict(structure_seed=True,setup_family=setup['family'],setup_id=setup['setup_id'],
                requires_full_context=True,observed_spread_bps=ticker.spread_bps,
                source_open_at=closed[-1].timestamp,source_closed_at=closed[-1].timestamp+seconds,
                **({'seed_timeframe':timeframe} if self.multihorizon else {})))
            for setup in options if setup['state']=='ready']

    def scan_to_dicts(self, symbols: Sequence[str] | None = None) -> dict[str, Any]:
        result = self.scan(symbols)
        return {
            "scanned": result.scanned,
            "kept": result.kept,
            "filtered_out": result.filtered_out,
            "candidates": [c.model_dump(mode="json") for c in result.candidates],
        }
