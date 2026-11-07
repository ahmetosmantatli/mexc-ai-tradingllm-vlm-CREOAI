from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from threading import Barrier,Event
from time import time

import pytest

from trading.config import Settings,RiskProfile
from trading.runtime.runner import PaperRunner
from trading.runtime.paper import PaperLedger
from trading.runtime.usage import archive_trace
from trading.models.domain import Side
from trading.quant.signals import CandidateSignal,StrategyName
from trading.exchange.mexc_public import TickerSnapshot
from trading.exchange.contracts import ContractRules
from trading.agents.schemas import EvaluationResult,GraphAction,MarketAnalystNote,StrategyReviewNote,PortfolioAnalystNote
from trading.agents.graph import size_proposal_from_signal


class Pool:
    available=True
    last_status='not_called'
    def __init__(self):self.children=[]
    def fork(self):
        child=SimpleNamespace(last_status='success',last_provider='gemini',last_model='child-model',last_fallback_reason='',closed=False)
        child.close=lambda:setattr(child,'closed',True)
        self.children.append(child)
        return child


def prepare(tmp_path,max_positions=2):
    cfg=Settings(_env_file=None,runtime_db_path=str(tmp_path/'runtime'),require_mexc_balance=False,
        risk_profile=RiskProfile.BALANCED,risk_per_trade_pct=.01,paper_max_positions=max_positions,paper_max_leverage=3)
    cfg=cfg.model_copy(update={'ai_provider_pool_enabled':True,'ai_parallel_candidates':2})
    class Public:
        use_fixtures=True
        def get_ticker(self,symbol):return TickerSnapshot(symbol,100,99.99,100.01,1e6,raw={'timestamp':time()*1000,'fairPrice':100})
        def get_contract_rules(self,symbol):return ContractRules(.001,1,1,1e8,5,.005,.01)
    pool=Pool()
    runner=PaperRunner(cfg,public=Public(),llm=pool)
    runner.ledger=PaperLedger(runner.store,500)
    runner.protection=SimpleNamespace(assert_ready=lambda:None,snapshot=lambda:{})
    runner.external_scan=True
    runner.external_control=lambda:{'enabled':True,'updated_at':1}
    candidates=[CandidateSignal(symbol=symbol,side=Side.LONG,strategy=StrategyName.TREND,entry_price=100,
        stop_loss_price=98,take_profit_price=104,confidence=.9,
        features={'dedup_key':symbol,'source_closed_at':time()-100}) for symbol in ('AAA_USDT','BBB_USDT')]
    runner.candidate_queue().refresh(candidates)
    return runner,pool


def install_evaluator(monkeypatch,barrier,control=None):
    class Evaluator:
        def __init__(self,llm,store,public,cfg):self.llm,self.store=llm,store
        def evaluate_candidates(self,candidates,**kw):
            candidate=candidates[0]
            assert barrier.wait(timeout=2) in (0,1)
            if control:
                control(candidate)
            trace=dict(id=candidate.symbol,symbol=candidate.symbol,action='trade',status='completed',execution_status='awaiting_risk',attempts=[])
            with self.store.lock:
                archive_trace(self.store,trace)
                self.store.put('analysis_trace',trace)
                self.store.put('analysis_history',self.store.get('analysis_history',[])+[trace])
            proposal=size_proposal_from_signal(candidate,equity=kw['equity'],risk_per_trade_pct=kw['risk_per_trade_pct'],target_leverage=3)
            return EvaluationResult(action=GraphAction.TRADE,proposal=proposal,selected_signal=candidate,
                market=MarketAnalystNote(mode='llm'),strategy=StrategyReviewNote(mode='llm'),portfolio=PortfolioAnalystNote(mode='llm'),
                metadata={'provider_called':True,'analysis_trace_id':candidate.symbol,'decision_started_at':time(),
                          'decision_ai':{'provider':'gemini','model':'child-model','fallback_reason':''}})
    monkeypatch.setattr('trading.evaluation.service.ContextEvaluator',Evaluator)


def test_two_candidate_models_overlap_but_commit_with_current_risk(tmp_path,monkeypatch):
    runner,pool=prepare(tmp_path)
    install_evaluator(monkeypatch,Barrier(2))
    result=runner.cycle()
    assert result['reason']=='PAPER_OPENED'
    assert set(runner.ledger.positions)=={'AAA_USDT','BBB_USDT'}
    assert len(pool.children)==2 and all(child.closed for child in pool.children)
    history=runner.store.get('analysis_history')
    assert len(history)==2 and all(row['execution_status']=='opened' for row in history)
    assert all(p['decision_ai']['model']=='child-model' for p in runner.ledger.positions.values())
    assert runner.store.get('status')['paper']['open_positions']==2
    assert runner.store.get('last_evaluation_at',0)>0


def test_second_candidate_cannot_bypass_updated_position_limit(tmp_path,monkeypatch):
    runner,pool=prepare(tmp_path,max_positions=1)
    install_evaluator(monkeypatch,Barrier(2))
    runner.cycle()
    assert len(runner.ledger.positions)==1
    history=runner.store.get('analysis_history')
    assert sorted(row['execution_status'] for row in history)==['opened','rejected']
    rejected=next(row for row in history if row['execution_status']=='rejected')
    assert rejected['execution_reason']=='HOLD_RISK_CHANGED'


def test_stop_during_parallel_evaluations_rejects_both_proposals(tmp_path,monkeypatch):
    runner,pool=prepare(tmp_path)
    state={'enabled':True,'updated_at':1}
    runner.external_control=lambda:dict(state)
    def stop(candidate):state.update(enabled=False,updated_at=2)
    install_evaluator(monkeypatch,Barrier(2),control=stop)
    runner.cycle()
    assert not runner.ledger.positions
    history=runner.store.get('analysis_history')
    assert len(history)==2 and all(row['execution_reason']=='HOLD_SETTINGS_CHANGED' for row in history)


def test_parallel_queue_picks_distinct_symbols(tmp_path,monkeypatch):
    runner,pool=prepare(tmp_path)
    first=runner.candidate_queue().next()
    duplicate=first.model_copy(update={'strategy':StrategyName.MOMENTUM,'confidence':.95})
    current=[CandidateSignal.model_validate(row['signal']) for row in runner.store.get('candidate_queue')]
    runner.candidate_queue().refresh([*current,duplicate])
    install_evaluator(monkeypatch,Barrier(2))
    runner.cycle()
    assert len(runner.ledger.positions)==2


def test_inflight_candidate_lease_survives_scan_and_expires_after_restart(tmp_path):
    from trading.runtime.candidates import CandidateQueue
    from trading.runtime.store import RuntimeStore
    runner,_=prepare(tmp_path)
    queue=runner.candidate_queue()
    current=[CandidateSignal.model_validate(row['signal']) for row in runner.store.get('candidate_queue')]
    now=time()
    first=queue.next(now=now,lease_seconds=120)
    queue.refresh(current,now=now+1)
    restarted=CandidateQueue(RuntimeStore(runner.cfg.runtime_db_path))
    second=restarted.next(now=now+2,lease_seconds=120)
    assert second.symbol!=first.symbol
    assert restarted.next(now=now+3) is None
    restarted.release(first)
    assert restarted.next(now=now+4).symbol==first.symbol
