"""A structured watch is an independent review source, never an executable signal."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import time

from trading.analysis import closed_candles,structure_features
from trading.evaluation.watch import WatchCoordinator,watch_snapshot,TIMEFRAMES,watch_event_key
from trading.quant.scanner import LiquidityFilter
from trading.quant.signals import CandidateSignal,StrategyName


class LiveWatchReviewer:
    def __init__(self,runner):self.r=runner

    def bars(self,symbol,tf):
        r=self.r;seconds=TIMEFRAMES[tf];now=time.time();expected=int(now//seconds)*seconds
        key='watch_bars:'+symbol+':'+tf
        cached=r.store.get(key,{})
        if cached.get('closed_at')==expected:return cached['rows']
        interval={'5m':'Min5','15m':'Min15','4h':'Hour4'}[tf]
        payload=r.public.get_klines(symbol,interval=interval,limit=61)
        rows=[row for row in payload['candles'] if isinstance(row,(list,tuple)) and len(row)>=6 and float(row[0])+seconds<=now]
        if not rows or now-(float(rows[-1][0])+seconds)>=2*seconds:raise ValueError('stale_watch_candles')
        r.store.put(key,dict(rows=rows,closed_at=float(rows[-1][0])+seconds))
        return rows

    def one(self,symbol):
        r=self.r;coordinator=WatchCoordinator(r.store);now=time.time()
        coordinator.observe(symbol);card=coordinator.card(symbol)
        if card['status'] not in ('watching','triggered'):return None
        ticker=r.quote(symbol)
        ok,reason=LiquidityFilter(r.cfg.scanner_min_quote_volume_24h,r.cfg.scanner_max_spread_bps,require_complete=True).passes(ticker)
        if not ok:
            r.store.put('watch_wait:'+symbol,dict(reason=reason,at=now));return None
        plan=card['plan'];tf=plan.get('timeframe')
        observed=None
        if plan['trigger_condition'].startswith('closed_candle'):
            observed={tf:[dict(close=float(row[4]),closed_at=float(row[0])+TIMEFRAMES[tf],complete=True)
                for row in self.bars(symbol,tf)]}
        coordinator.observe(symbol,price=ticker.last_price,closed_candles=observed,expected_revision=card['revision'])
        card=coordinator.card(symbol)
        if card['status'] not in ('watching','triggered'):return None
        armed_single=any(row.get('symbol')==symbol and row.get('status')=='armed' and row.get('single_trigger')
            for row in r.store.get('price_action_holds', []) if isinstance(row, dict))
        # Closed opposing BOS on entry/context TFs requests review, not an automatic reversal.
        # One stored numeric trigger is not sent back on every structure twitch.
        if not armed_single:
            for structural_tf in ('15m','4h'):
                rows=self.bars(symbol,structural_tf)
                if len(rows)<30:continue
                candles=closed_candles(rows,TIMEFRAMES[structural_tf],now)
                features=structure_features(candles)
                adverse='bearish' if card['side']=='long' else 'bullish'
                breaks=[b for b in features['breaks'] if b['direction']==adverse and b['at']+TIMEFRAMES[structural_tf]>card['created_at']]
                if breaks:
                    latest=breaks[-1]
                    event=dict(timeframe=structural_tf,available_at=latest['at']+TIMEFRAMES[structural_tf],
                        direction=adverse,level=latest['level'],kind='confirmed_opposing_BOS')
                    coordinator.request_structure_review(symbol,event,expected_revision=card['revision'],now=now)
                    card=coordinator.card(symbol)
        if card['status']!='triggered':return None
        if armed_single and card.get('reason')=='structure_changed':return None
        rows=self.bars(symbol,'15m');latest=rows[-1]
        reference=card['candidate']
        event_key=watch_event_key(card)
        r.store.put('watch_wait:'+symbol,dict(reason='review_queued',at=now,event_key=event_key))
        return CandidateSignal(symbol=symbol,side=card['side'],strategy=StrategyName.WATCH_REVIEW,
            entry_price=ticker.last_price,stop_loss_price=reference['stop_loss_price'],take_profit_price=reference['take_profit_price'],
            confidence=1.,rationale='Independent watch review; old levels are context only. '+card.get('last_rationale',''),
            created_at=datetime.now(timezone.utc),features=dict(watch_review=True,review_event_key=event_key,
                source_open_at=float(latest[0]),source_closed_at=float(latest[0])+900,
                observed_spread_bps=ticker.spread_bps,original_strategy=reference['strategy'],review_reason=card.get('reason')))

    def collect(self):
        symbols=[w['symbol'] for w in watch_snapshot(self.r.store) if w['status'] in ('watching','triggered')][:40]
        def inspect(symbol):
            try:return self.one(symbol)
            except Exception as exc:
                self.r.store.put('watch_wait:'+symbol,dict(reason='data_unavailable',error_type=type(exc).__name__,at=time.time()))
                return None
        with ThreadPoolExecutor(max_workers=4,thread_name_prefix='live-watch') as pool:
            return [c for c in pool.map(inspect,symbols) if c is not None]
