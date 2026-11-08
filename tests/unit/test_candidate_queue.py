from types import SimpleNamespace
from time import time
from concurrent.futures import ThreadPoolExecutor
import threading
import hashlib

import pytest

from trading.runtime.candidates import CandidateQueue
from trading.runtime.store import RuntimeStore
from trading.runtime.runner import PaperRunner
from trading.runtime.paper import PaperLedger
from trading.quant.signals import CandidateSignal,StrategyName
from trading.quant.scanner import ScanResult,MarketScanner
from trading.models.domain import Side
from trading.config import Settings
from trading.exchange.mexc_public import TickerSnapshot
from trading.agents.schemas import EvaluationResult,GraphAction


def signal(symbol='AAA_USDT',confidence=.8,closed_at=100):
    return CandidateSignal(symbol=symbol,side=Side.LONG,strategy=StrategyName.TREND,
        entry_price=100,stop_loss_price=98,take_profit_price=104,confidence=confidence,
        features={'source_closed_at':closed_at,'dedup_key':symbol+':candle'})


def test_queue_rotates_across_refresh_and_restart(tmp_path):
    path=str(tmp_path/'queue')
    queue=CandidateQueue(RuntimeStore(path))
    queue.refresh([signal('AAA_USDT',.95),signal('BBB_USDT',.7)],now=100)
    first=queue.next(now=101)
    assert first.symbol=='AAA_USDT'
    queue.evaluated(first,now=102)
    # Random signal IDs and repeated best-score refresh cannot reset its place.
    queue.refresh([signal('AAA_USDT',.95),signal('BBB_USDT',.7)],now=103)
    restored=CandidateQueue(RuntimeStore(path))
    assert restored.next(now=104).symbol=='BBB_USDT'
    assert restored.next(now=105).symbol=='AAA_USDT'


def test_queue_drops_missing_and_expired_signals_filters_open_seen(tmp_path):
    queue=CandidateQueue(RuntimeStore(str(tmp_path/'q')),ttl_seconds=10)
    queue.refresh([signal('AAA_USDT'),signal('BBB_USDT')],now=100)
    assert queue.next(now=101,open_symbols={'AAA_USDT'},seen={'BBB_USDT:candle'}) is None
    queue.refresh([signal('BBB_USDT')],now=102)
    assert queue.next(now=103).symbol=='BBB_USDT'
    assert queue.next(now=113) is None


def runner_fixture(tmp_path):
    cfg=Settings(_env_file=None,runtime_db_path=str(tmp_path/'runner'),require_mexc_balance=False,paper_seed_usdt=500,anthropic_api_key='fake')
    class Public:
        use_fixtures=True
        def get_ticker(self,symbol):
            return TickerSnapshot(symbol,100,99.99,100.01,1e6,raw={'timestamp':time()*1000,'fairPrice':100})
        def get_klines(self,*args,**kwargs):
            return {'candles':[[int(time())-1000,100,101,99,100,1]]}
    runner=PaperRunner(cfg,public=Public())
    runner.ledger=PaperLedger(runner.store,500)
    # Simulate independent quote supervision; this test is about scans/AI cadence.
    runner.protection=SimpleNamespace(assert_ready=lambda:None,snapshot=lambda:{})
    return runner


def test_inline_scan_refreshes_while_ai_interval_waits(tmp_path,monkeypatch):
    runner=runner_fixture(tmp_path)
    scanned=[]
    monkeypatch.setattr('trading.runtime.runner.MarketScanner.scan',lambda *a,**kw:scanned.append(1) or ScanResult(1,1,candidates=[signal(closed_at=time()-100)]))
    runner.store.put('last_evaluation_at',time())
    runner.graph.evaluate_candidates=lambda *a,**kw:pytest.fail('must not call AI during interval')
    assert runner.cycle()['reason']=='HOLD_EVALUATION_INTERVAL'
    assert scanned==[1]
    assert runner.store.get('scan_summary')['queue_size']==1


def test_unchanged_context_rotates_without_spending_ai_interval(tmp_path,monkeypatch):
    runner=runner_fixture(tmp_path)
    runner.external_scan=True
    runner.candidate_queue().refresh([signal('AAA_USDT',.95,closed_at=time()-100),signal('BBB_USDT',.8,closed_at=time()-100)])
    called=[]
    def evaluate(candidates,**kw):
        called.append(candidates[0].symbol)
        return EvaluationResult(action=GraphAction.HOLD,hold_reason='unchanged_closed_candles',metadata={'provider_called':False})
    runner.graph.evaluate_candidates=evaluate
    runner.cycle();runner.cycle()
    assert called==['AAA_USDT','BBB_USDT']
    assert runner.store.get('last_evaluation_at',0)==0


def test_separate_scan_finishes_while_provider_evaluation_is_blocked(tmp_path,monkeypatch):
    runner=runner_fixture(tmp_path)
    runner.external_scan=True
    runner.candidate_queue().refresh([signal(closed_at=time()-100)])
    evaluating=threading.Event();release=threading.Event()
    def evaluate(*args,**kw):
        evaluating.set()
        assert release.wait(2)
        return EvaluationResult(action=GraphAction.HOLD,hold_reason='hold')
    runner.graph.evaluate_candidates=evaluate
    monkeypatch.setattr('trading.runtime.runner.MarketScanner.scan',lambda *a,**kw:ScanResult(2,2,candidates=[signal('BBB_USDT',closed_at=time()-100)]))
    with ThreadPoolExecutor(2) as pool:
        analysis=pool.submit(runner.cycle)
        assert evaluating.wait(1)
        summary=pool.submit(runner.scan_market).result(timeout=1)
        assert summary['queue_size']==1 and not analysis.done()
        release.set()
        analysis.result(timeout=1)


def test_market_listing_error_retains_unexpired_queue(tmp_path,monkeypatch):
    runner=runner_fixture(tmp_path)
    runner.candidate_queue().refresh([signal(closed_at=time()-100)])
    monkeypatch.setattr('trading.runtime.runner.MarketScanner.scan',lambda *a,**kw:(_ for _ in ()).throw(TimeoutError('private error not retained')))
    assert runner.scan_market() is None
    assert runner.store.get('scan_error')['type']=='TimeoutError'
    assert len(runner.store.get('candidate_queue'))==1


def test_existing_seen_float_timestamp_does_not_reopen_after_upgrade(tmp_path,monkeypatch):
    runner=runner_fixture(tmp_path)
    candidate=signal(closed_at=time()-100)
    opened=int(time())-1000
    candidate.features.update(source_open_at=opened,source_closed_at=opened+900)
    legacy=hashlib.sha256(f'{candidate.symbol}:trend:long:{float(opened)}'.encode()).hexdigest()
    runner.ledger.state['seen'].append(legacy)
    monkeypatch.setattr('trading.runtime.runner.MarketScanner.scan',lambda *a,**kw:ScanResult(1,1,candidates=[candidate]))
    assert runner.scan_market()['eligible']==0


def test_scanner_one_symbol_failure_does_not_discard_other_results():
    class Public:
        use_fixtures=True
        def list_fee_free_candidates(self):return []
        def list_contract_symbols(self):return ['BAD_USDT','GOOD_USDT']
        def get_ticker(self,symbol):
            if symbol=='BAD_USDT':raise ValueError('not safe to log raw')
            return TickerSnapshot(symbol,120,119.99,120.01,1e6)
        def get_klines(self,*a,**kw):
            return {'candles':[[i,100+i*.5,101+i*.5,99+i*.5,100+i*.5,1] for i in range(40)]}
    result=MarketScanner(Public()).scan()
    assert result.scanned==2 and result.errors=={'BAD_USDT':'DATA_FORMAT'}
    assert any(c.symbol=='GOOD_USDT' for c in result.candidates)
    assert result.duration_seconds>=0


def test_scanner_permission_is_independent_of_entry_funding_gate(tmp_path,monkeypatch):
    runner=runner_fixture(tmp_path)
    runner.external_control=lambda:{'enabled':False,'reason':'funding'}
    runner.external_scan_control=lambda:{'enabled':True}
    monkeypatch.setattr('trading.runtime.runner.MarketScanner.scan',lambda *a,**kw:ScanResult(1,1,candidates=[signal(closed_at=time()-100)]))
    assert runner.scan_market()['queue_size']==1


@pytest.mark.parametrize('change,expected',[
    ('removed','HOLD_CANDIDATE_STALE'),('replaced','HOLD_CANDIDATE_STALE'),('expired','HOLD_CANDIDATE_STALE'),
    ('kill','HOLD_KILL_SWITCH'),('duplicate','HOLD_DUPLICATE_OR_ALREADY_OPEN')])
def test_final_entry_rechecks_queue_and_stop_after_slow_model(tmp_path,monkeypatch,change,expected):
    from pathlib import Path
    from trading.agents.graph import size_proposal_from_signal
    from trading.agents.schemas import MarketAnalystNote,StrategyReviewNote,PortfolioAnalystNote
    from trading.exchange.contracts import ContractRules
    runner=runner_fixture(tmp_path)
    runner.cfg=runner.cfg.model_copy(update={'kill_switch_path':str(tmp_path/'STOP')})
    runner.external_scan=True
    runner.candidate_queue().refresh([signal(closed_at=time()-100)])
    runner.public.get_contract_rules=lambda symbol:ContractRules(.001,1,1,1e8,10,.005,.01)
    def evaluate(candidates,**kwargs):
        selected=candidates[0]
        if change=='removed':
            runner.candidate_queue().refresh([])
        elif change=='replaced':
            updated=signal(closed_at=time()-1)
            updated.features['dedup_key']='new-candle'
            runner.candidate_queue().refresh([updated])
        elif change=='expired':
            rows=runner.store.get('candidate_queue')
            rows[0]['expires_at']=time()-1
            runner.store.put('candidate_queue',rows)
        elif change=='kill':
            Path(runner.cfg.kill_switch_path).touch()
        elif change=='duplicate':
            monkeypatch.setattr(runner.ledger,'open',lambda *a,**kw:None)
        return EvaluationResult(action=GraphAction.TRADE,
            proposal=size_proposal_from_signal(selected,equity=500,risk_per_trade_pct=.03,target_leverage=3),
            selected_signal=selected,market=MarketAnalystNote(mode='llm'),strategy=StrategyReviewNote(mode='llm'),
            portfolio=PortfolioAnalystNote(mode='llm'))
    runner.graph.evaluate_candidates=evaluate
    assert runner.cycle()['reason']==expected
    assert not runner.ledger.positions
    assert not any(e['kind']=='OPEN' for e in runner.ledger.state['events'])
