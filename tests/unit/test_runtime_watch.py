from types import SimpleNamespace
from time import time
from unittest.mock import patch
import threading

from trading.config import Settings
from trading.runtime.runner import PaperRunner
from trading.runtime.paper import PaperLedger
from trading.quant.signals import CandidateSignal,StrategyName
from trading.quant.scanner import ScanResult
from trading.models.domain import Side
from trading.exchange.mexc_public import TickerSnapshot
from trading.evaluation.watch import WatchCoordinator
from trading.agents.schemas import EvaluationResult,GraphAction,MarketAnalystNote,StrategyReviewNote,PortfolioAnalystNote
from trading.agents.graph import size_proposal_from_signal
from trading.exchange.contracts import ContractRules


def setup(tmp_path):
    cfg=Settings(_env_file=None,runtime_db_path=str(tmp_path/'runtime'),require_mexc_balance=False,paper_seed_usdt=500,anthropic_api_key='fake')
    class Public:
        use_fixtures=True
        price=100.
        bars=[]
        def get_ticker(self,symbol):return TickerSnapshot(symbol,self.price,self.price-.01,self.price+.01,1e6,raw={'timestamp':time()*1000,'fairPrice':self.price})
        def get_klines(self,*a,**kw):return {'candles':self.bars}
        def get_contract_rules(self,symbol):return ContractRules(.001,1,1,1e8,5,.005,.01)
    runner=PaperRunner(cfg,public=Public())
    runner.ledger=PaperLedger(runner.store,500)
    runner.protection=SimpleNamespace(assert_ready=lambda:None,snapshot=lambda:{})
    runner.external_scan=True
    runner.external_control=lambda:{'enabled':True,'updated_at':1}
    candidate=CandidateSignal(symbol='AAA_USDT',side=Side.LONG,strategy=StrategyName.TREND,entry_price=100,stop_loss_price=98,take_profit_price=104,confidence=.8,features={'dedup_key':'fresh','source_closed_at':time()-100})
    return runner,candidate


def watch(runner,candidate,condition='price_above',created=None):
    now=time()
    plan=dict(trigger_condition=condition,trigger_price=100.,timeframe='15m' if condition.startswith('closed') else None,
        invalidation_condition='below',invalidation_price=95.,expires_at=now+3600)
    coordinator=WatchCoordinator(runner.store)
    assert coordinator.commit(candidate,'watch',0,watch_plan=plan,rationale='wait',now=created or now-10)
    return coordinator


def test_price_watch_survives_queue_absence_and_triggers_once_with_fresh_quant(tmp_path,monkeypatch):
    runner,candidate=setup(tmp_path)
    coordinator=watch(runner,candidate)
    runner.public.price=100.1
    monkeypatch.setattr('trading.runtime.runner.MarketScanner.scan',lambda *a,**kw:ScanResult(1,1,candidates=[candidate.model_copy(deep=True)]))
    assert not runner.store.get('candidate_queue')
    summary=runner.scan_market()
    assert summary['queue_size']==1
    current=coordinator.card('AAA_USDT')
    assert current['status']=='triggered'
    events=runner.store.get('watch_events')
    assert sum(e['status']=='triggered' for e in events)==1
    queued=runner.store.get('candidate_queue')[0]['signal']
    assert queued['features']['watch_trigger_revision']==current['revision']
    event_id=queued['features']['watch_trigger_event_id']
    runner.scan_market()
    assert sum(e['status']=='triggered' for e in runner.store.get('watch_events'))==1
    assert runner.store.get('candidate_queue')[0]['signal']['features']['watch_trigger_event_id']==event_id
    assert not runner.ledger.positions


def test_trigger_without_fresh_quant_setup_waits_without_rebasing(tmp_path,monkeypatch):
    runner,candidate=setup(tmp_path)
    coordinator=watch(runner,candidate)
    runner.public.price=110.
    monkeypatch.setattr('trading.runtime.runner.MarketScanner.scan',lambda *a,**kw:ScanResult(1,1,candidates=[]))
    runner.scan_market()
    assert coordinator.card('AAA_USDT')['status']=='triggered'
    assert runner.store.get('candidate_queue')==[]
    assert runner.store.get('watch_waiting_quant:AAA_USDT')['reason']=='triggered_waiting_fresh_quant_setup'
    assert not runner.ledger.positions


def test_watching_symbol_not_reevaluated_before_price_condition(tmp_path,monkeypatch):
    runner,candidate=setup(tmp_path)
    watch(runner,candidate)
    runner.public.price=99.
    runner.candidate_queue().refresh([candidate])
    runner.graph.evaluate_candidates=lambda *a,**kw:(_ for _ in ()).throw(AssertionError('AI beforewatchtrigger'))
    assert runner.cycle()['reason']=='HOLD_NO_QUALIFIED_SIGNAL'


def test_closed_watch_ignores_open_candle_and_observes_completed_cross(tmp_path,monkeypatch):
    runner,candidate=setup(tmp_path)
    now=time()
    coordinator=watch(runner,candidate,'closed_candle_cross_above',created=now-1000)
    monkeypatch.setattr('trading.runtime.runner.MarketScanner.scan',lambda *a,**kw:ScanResult(1,1,candidates=[candidate.model_copy(deep=True)]))
    runner.public.price=100.
    runner.public.bars=[[now-1800,99,100,98,99,1],[now-100,101,102,100,101,1]]
    assert runner.monitor_watch()==[]
    assert coordinator.card('AAA_USDT')['status']=='watching'
    runner.public.bars=[[now-1900,99,100,98,99,1],[now-1000,101,102,100,101,1]]
    fresh=runner.monitor_watch()
    assert len(fresh)==1 and coordinator.card('AAA_USDT')['status']=='triggered'


def test_stale_watch_revision_blocks_execution_after_model_trade(tmp_path):
    runner,candidate=setup(tmp_path)
    coordinator=watch(runner,candidate)
    runner.candidate_queue().refresh([candidate])
    observed=coordinator.card(candidate.symbol)['revision']
    assert coordinator.commit(candidate,'trade',observed)
    committed=coordinator.card(candidate.symbol)['revision']
    proposal=size_proposal_from_signal(candidate,equity=500,risk_per_trade_pct=.01,target_leverage=3)
    result=EvaluationResult(action=GraphAction.TRADE,proposal=proposal,selected_signal=candidate,
        market=MarketAnalystNote(mode='llm'),strategy=StrategyReviewNote(mode='llm'),portfolio=PortfolioAnalystNote(mode='llm'),
        metadata={'watch_revision':committed,'provider_called':True,'decision_started_at':time()})
    assert coordinator.commit(candidate,'hold',committed)
    outcome=runner._apply_evaluation(candidate,result,None,{'enabled':True,'updated_at':1},.01,time())
    assert outcome['reason']=='HOLD_WATCH_CHANGED'
    assert not runner.ledger.positions
