"""Model -> validated plan -> final fresh paper sizing integration."""
from time import time
from types import SimpleNamespace

import pytest

from trading.agents.schemas import GraphAction
from trading.config import Settings, RiskProfile
from trading.evaluation.service import ContextEvaluator
from trading.exchange.contracts import ContractRules
from trading.exchange.mexc_public import TickerSnapshot
from trading.quant.signals import CandidateSignal
from trading.runtime.paper import PaperLedger
from trading.runtime.runner import PaperRunner
from trading.runtime.store import RuntimeStore


def signal():
    return CandidateSignal(symbol='BTC_USDT', side='long', strategy='trend', entry_price=100.,
        stop_loss_price=98., take_profit_price=106., confidence=.85,
        features={'dedup_key':'btc-closed-bar','source_closed_at':time()-100})


class Public:
    def get_contract_rules(self, symbol):
        return ContractRules(.001,1,1,1e8,20,.005,.01)

    def get_ticker(self, symbol):
        return TickerSnapshot(symbol,100,99.99,100.,1e6,
            raw={'timestamp':time()*1000,'fairPrice':100})


def evaluate(tmp_path, monkeypatch, candidate, plan='omitted'):
    monkeypatch.setattr('trading.analysis.build_bundle',lambda *a,**kw:
        SimpleNamespace(fingerprint='current-closed-bars',to_context=lambda:{}))
    monkeypatch.setattr('trading.analysis.render_bundle',lambda *a,**kw:[])
    cfg=Settings(_env_file=None,runtime_db_path=str(tmp_path/'state'), require_mexc_balance=False,
        analysis_chart_dir=str(tmp_path/'charts'),risk_profile=RiskProfile.BALANCED,
        risk_per_trade_pct=.01,paper_max_leverage=5,default_target_leverage=5,
        kill_switch_path=str(tmp_path/'kill'))
    payload=dict(action='trade',signal_id=candidate.signal_id,rationale='Structure confirmed',
        invalidation='Stop invalidates setup',watch_note='Track structure')
    if plan != 'omitted':
        payload['sizing_plan']=plan
    llm=SimpleNamespace(budget=None,complete_json=lambda **kw:payload,
        response_validator=None,decision_schema=None,last_status='success')
    store=RuntimeStore(cfg.runtime_db_path)
    result=ContextEvaluator(llm,store,Public(),cfg).evaluate_candidates([candidate],
        equity=500.,risk_per_trade_pct=.01,target_leverage=5.)
    return cfg,llm,store,result


@pytest.mark.parametrize('plan', ['omitted',None,dict(leverage=2,risk_per_trade_pct=.005)])
def test_evaluator_preserves_optional_model_plan_and_fixed_candidate_levels(tmp_path,monkeypatch,plan):
    candidate=signal()
    _,llm,_,result=evaluate(tmp_path,monkeypatch,candidate,plan)
    assert result.action==GraphAction.TRADE
    assert 'sizing_plan' in llm.decision_schema['properties']
    assert result.metadata['sizing_plan']==(plan if isinstance(plan,dict) else None)
    assert result.proposal.leverage==(2 if isinstance(plan,dict) else 5)
    assert result.proposal.max_loss_if_stopped==(2.5 if isinstance(plan,dict) else 5)
    assert (result.proposal.entry_price,result.proposal.stop_loss.price,result.proposal.take_profit_price)==(100,98,106)


@pytest.mark.parametrize('plan', [dict(leverage=6,risk_per_trade_pct=.005),
    dict(leverage=2,risk_per_trade_pct=.02),dict(leverage=2,risk_per_trade_pct=.005,stop_loss=95)])
def test_evaluator_rejects_over_policy_and_invented_setup(tmp_path,monkeypatch,plan):
    _,_,_,result=evaluate(tmp_path,monkeypatch,signal(),plan)
    assert result.action==GraphAction.HOLD
    assert result.proposal is None
    assert result.hold_reason in ('invalid_model_sizing','invalid_or_unavailable_model')


def runner_for(cfg,llm,store,candidate):
    runner=PaperRunner(cfg,public=Public(),llm=llm,store=store)
    runner.ledger=PaperLedger(store,500.)
    runner.protection=SimpleNamespace(assert_ready=lambda:None,snapshot=lambda:{})
    runner.candidate_queue().refresh([candidate])
    return runner


def test_runner_rebuilds_model_sizing_under_final_lock_from_fresh_equity(tmp_path,monkeypatch):
    candidate=signal()
    cfg,llm,store,result=evaluate(tmp_path,monkeypatch,candidate,dict(leverage=2,risk_per_trade_pct=.005))
    runner=runner_for(cfg,llm,store,candidate)
    # A caller cannot smuggle stale/higher sizing through the proposal object.
    result.proposal.notional=100000
    result.proposal.leverage=99
    original_quote=runner.quote
    calls=[]
    def quote(symbol):
        calls.append(symbol)
        if len(calls)==2:
            # Protection updates equity after initial check, before execution lock.
            runner.ledger.state['cash']=495.
            runner.ledger.save()
        return original_quote(symbol)
    monkeypatch.setattr(runner,'quote',quote)
    received=[]
    original_open=runner.ledger.open
    def open_position(proposal,*args,**kwargs):
        received.append(proposal)
        return original_open(proposal,*args,**kwargs)
    monkeypatch.setattr(runner.ledger,'open',open_position)
    outcome=runner._apply_evaluation(candidate,result,None,None,.01,time())
    assert outcome['reason']=='PAPER_OPENED'
    final=received[0]
    assert final.leverage==2
    assert final.max_loss_if_stopped==pytest.approx(495*.005)
    assert final.notional==pytest.approx(495*.005/.02)
    assert final.metadata['sized_from_equity']==495.
    position=runner.ledger.positions[candidate.symbol]
    assert position['stop_loss_estimate']<=495*.005+1e-8
    assert position['sizing_source']=='model'
    assert position['sizing_risk_fraction']==.005
    assert position['sizing_plan']==dict(leverage=2,risk_per_trade_pct=.005)
    event=runner.ledger.state['events'][-1]
    assert event['sizing_plan']==position['sizing_plan']
    assert event['leverage']==2
    reloaded=PaperLedger(RuntimeStore(cfg.runtime_db_path),0)
    assert reloaded.positions[candidate.symbol]['sizing_risk_fraction']==.005


def test_runner_rejects_plan_when_exchange_cap_changes_after_analysis(tmp_path,monkeypatch):
    candidate=signal()
    cfg,llm,store,result=evaluate(tmp_path,monkeypatch,candidate,dict(leverage=4,risk_per_trade_pct=.005))
    runner=runner_for(cfg,llm,store,candidate)
    monkeypatch.setattr(runner.public,'get_contract_rules',lambda symbol:ContractRules(.001,1,1,1e8,3,.005,.01))
    outcome=runner._apply_evaluation(candidate,result,None,None,.01,time())
    assert outcome['reason']=='HOLD_INVALID_MODEL_SIZING'
    assert not runner.ledger.positions
