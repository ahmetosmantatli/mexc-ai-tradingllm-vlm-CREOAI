"""Coverage, independent watch reviews and event dedup; no network/exchange writes."""
from time import time
from types import SimpleNamespace
from copy import deepcopy
import pytest

from trading.runtime.universe import select_universe
from trading.runtime.store import RuntimeStore
from trading.runtime.candidates import CandidateQueue
from trading.runtime.live_watch import LiveWatchReviewer
from trading.evaluation.watch import WatchCoordinator,watch_event_key
from trading.config import Settings
from trading.exchange.mexc_public import TickerSnapshot
from .test_watch_service import signal,plan


def test_rotation_covers_every_symbol_persists_and_ignores_exchange_order(tmp_path):
    path=str(tmp_path/'runtime.sqlite3');store=RuntimeStore(path)
    symbols=[f'C{i:03d}_USDT' for i in range(101)]
    first=select_universe(store,symbols,40)
    second=select_universe(RuntimeStore(path),list(reversed(symbols)),40)
    third=select_universe(store,symbols,40)
    assert not set(first)&set(second)
    assert set(first+second+third)==set(symbols)
    assert store.get('universe_rotation')['total_symbols']==101
    assert len(select_universe(store,symbols[:2],40))==2


def test_rotating_refresh_retains_unscanned_candidates_but_drops_scanned_missing_signal(tmp_path):
    store=RuntimeStore(str(tmp_path/'state'));queue=CandidateQueue(store)
    a,b=signal(),signal();b.symbol='ETH_USDT'
    queue.refresh([a],now=100)
    queue.refresh([b],now=110,refreshed_symbols=['ETH_USDT'])
    assert {r['signal']['symbol'] for r in store.get('candidate_queue')}=={'BTC_USDT','ETH_USDT'}
    queue.refresh([],now=120,refreshed_symbols=['BTC_USDT'])
    assert [r['signal']['symbol'] for r in store.get('candidate_queue')]==['ETH_USDT']
    queue.refresh([],now=2000,refreshed_symbols=[])
    assert not store.get('candidate_queue')


def reviewer(tmp_path,*,triggered=False):
    store=RuntimeStore(str(tmp_path/'state'));coordinator=WatchCoordinator(store)
    candidate=signal();p=plan();p['trigger_price']=101.
    coordinator.commit(candidate,'watch',0,watch_plan=p,rationale='Eski trend sinyali artık olmasa da planı takip et.')
    if triggered:coordinator.observe(candidate.symbol,price=102.)
    now=int(time());stamp=int(now//900)*900-900
    rows=[[stamp-900,100,103,98,100,10],[stamp,100,103,98,101,10]]
    class Public:
        def get_klines(self,symbol,interval,limit):
            if interval=='Hour4':
                ts=int(now//14400)*14400-14400
                return {'candles':[[ts,100,103,98,101,10]]}
            return {'candles':deepcopy(rows)}
    runner=SimpleNamespace(store=store,cfg=Settings(_env_file=None),public=Public(),
        quote=lambda symbol:TickerSnapshot(symbol,102.,101.99,102.,1e6,raw={'timestamp':time()*1000,'fairPrice':102.}))
    return LiveWatchReviewer(runner),coordinator,runner


def test_triggered_watch_creates_independent_candidate_without_any_strategy_call(tmp_path):
    watcher,coordinator,runner=reviewer(tmp_path,triggered=True)
    candidate=watcher.one('BTC_USDT')
    assert candidate.strategy.value=='watch_review' and candidate.features['watch_review']
    assert candidate.features['review_event_key']==watch_event_key(coordinator.card('BTC_USDT'))
    assert candidate.features['original_strategy']=='trend'
    assert candidate.entry_price==102


def test_invalidated_watch_produces_no_candidate(tmp_path):
    watcher,coordinator,runner=reviewer(tmp_path,triggered=True)
    coordinator.observe('BTC_USDT',price=96.)
    assert watcher.one('BTC_USDT') is None


def test_structure_event_is_recorded_once_per_timeframe_not_alternated_forever(tmp_path):
    watcher,c,r=reviewer(tmp_path)
    old=c.card('BTC_USDT');old['created_at']=time()-10;r.store.put('watch:BTC_USDT',old);when=time()-1
    a=dict(timeframe='15m',available_at=when,kind='confirmed_opposing_BOS',direction='bearish',level=99.)
    b=dict(a,timeframe='4h',level=98.)
    assert c.request_structure_review('BTC_USDT',a,expected_revision=old['revision'])
    assert c.request_structure_review('BTC_USDT',b,expected_revision=c.card('BTC_USDT')['revision'])
    revision=c.card('BTC_USDT')['revision'];event_key=watch_event_key(c.card('BTC_USDT'))
    for _ in range(3):
        assert not c.request_structure_review('BTC_USDT',a,expected_revision=revision)
        assert not c.request_structure_review('BTC_USDT',b,expected_revision=revision)
    assert c.card('BTC_USDT')['revision']==revision
    assert watch_event_key(c.card('BTC_USDT'))==event_key
    assert c.card('BTC_USDT')['reason']=='structure_changed'


def test_due_watch_is_considered_before_ordinary_queue_candidate(tmp_path):
    watcher,c,r=reviewer(tmp_path,triggered=True)
    watch=watcher.one('BTC_USDT');normal=signal();normal.symbol='ETH_USDT'
    queue=CandidateQueue(r.store);queue.refresh([normal,watch])
    assert queue.next().features['watch_review']


def test_failed_watch_does_not_hide_other_watch_symbols(tmp_path,monkeypatch):
    watcher,c,r=reviewer(tmp_path,triggered=True)
    other=signal();other.symbol='ETH_USDT';c.commit(other,'watch',0,watch_plan=plan())
    c.observe('ETH_USDT',price=102.)
    original=watcher.one
    def one(symbol):
        if symbol=='BTC_USDT':raise ValueError('one public market failed')
        return original(symbol)
    monkeypatch.setattr(watcher,'one',one)
    assert [row.symbol for row in watcher.collect()]==['ETH_USDT']
    assert r.store.get('watch_wait:BTC_USDT')['reason']=='data_unavailable'


def test_opposing_closed_structure_requests_review_before_price_trigger(tmp_path,monkeypatch):
    watcher,c,r=reviewer(tmp_path)
    card=c.card('BTC_USDT');card['created_at']=time()-7200;r.store.put('watch:BTC_USDT',card)
    r.quote=lambda symbol:TickerSnapshot(symbol,100.,99.99,100.,1e6,raw={'timestamp':time()*1000,'fairPrice':100.})
    def klines(symbol,interval,limit):
        seconds=900 if interval=='Min15' else 14400
        end=int(time()//seconds)*seconds
        return {'candles':[[end-(31-i)*seconds,100,102,98,100,10] for i in range(31)]}
    r.public.get_klines=klines
    monkeypatch.setattr('trading.runtime.live_watch.structure_features',lambda candles:{'breaks':[
        dict(direction='bearish',at=candles[-1].timestamp,level=99.)]})
    candidate=watcher.one('BTC_USDT')
    assert candidate is not None and c.card('BTC_USDT')['reason']=='structure_changed'
    assert candidate.features['review_reason']=='structure_changed'
    revision=c.card('BTC_USDT')['revision']
    watcher.one('BTC_USDT')
    assert c.card('BTC_USDT')['revision']==revision
