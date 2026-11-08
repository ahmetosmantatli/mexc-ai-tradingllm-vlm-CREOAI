from copy import deepcopy
from types import SimpleNamespace
import json
from time import time
import pytest
from pydantic import ValidationError
from trading.evaluation.price_plan import MarketPricePlan,PricePlanError,apply_market_price_plan,evidence_from_market
from trading.evaluation.service import ContextEvaluator
from trading.runtime.store import RuntimeStore
from trading.config import Settings
from trading.exchange.contracts import ContractRules
from trading.agents.schemas import GraphAction
from trading.models.domain import Side
from .test_live_runner import candidate,prepare,result


NOW=1800000000
STAMP=NOW-900


def evidence():
    return {'15m':[[STAMP-900,100,102,99,100,10],[STAMP,100,104,99.5,101,12]]}


def plan():
    return dict(entry_type='market',entry_price=100.,stop_loss_price=99.4,take_profit_price=103.9,
        entry_timeframe='15m',stop_reference=dict(timeframe='15m',open_time=STAMP,field='low'),
        target_reference=dict(timeframe='15m',open_time=STAMP,field='high'),reason='Son kapanmış mumun fitil altı ve üst likidite hedefi.')


def apply(raw=None,**kwargs):
    return apply_market_price_plan(candidate(),raw or plan(),evidence(),price_step=.1,current_price=100,now=NOW,**kwargs)


def test_wick_plan_changes_levels_without_changing_symbol_side_identity_or_quant_candidate():
    original=candidate();before=original.model_dump()
    planned=apply_market_price_plan(original,plan(),evidence(),price_step=.1,current_price=100,now=NOW)
    assert planned.stop_loss_price==99.4 and planned.take_profit_price==103.9
    assert planned.signal_id==original.signal_id and planned.features==original.features
    assert original.model_dump()==before


@pytest.mark.parametrize('patch',[
    {'entry_type':'limit'},{'entry_price':True},{'symbol':'ETH_USDT'},
    {'stop_loss_price':float('nan')},{'take_profit_price':float('inf')},
])
def test_strict_price_schema_rejects_nonmarket_extra_and_nonfinite(patch):
    with pytest.raises(ValidationError):MarketPricePlan.model_validate({**plan(),**patch})


@pytest.mark.parametrize('patch,reason',[
    ({'entry_price':101.},'too_far'),({'stop_loss_price':100.1},'direction'),
    ({'take_profit_price':99.8},'direction'),({'stop_loss_price':99.41},'tick'),
    ({'stop_loss_price':99.6},'beyond'),({'stop_loss_price':90.},'too_far'),
    ({'take_profit_price':120.},'too_far'),
    ({'stop_reference':{'timeframe':'15m','open_time':STAMP+1,'field':'low'}},'missing'),
    ({'stop_reference':{'timeframe':'15m','open_time':STAMP,'field':'high'}},'direction'),
])
def test_invalid_plan_is_rejected_not_silently_repaired(patch,reason):
    with pytest.raises(PricePlanError,match=reason):apply({**plan(),**patch})


def test_future_unclosed_reference_rejected():
    with pytest.raises(PricePlanError,match='open_candle'):
        apply_market_price_plan(candidate(),plan(),evidence(),price_step=.1,current_price=100,now=STAMP+899)


def setup_service(tmp_path,monkeypatch,enabled=True):
    cfg=Settings(_env_file=None,live_price_plan_enabled=enabled)
    market={'timeframes':[{'timeframe':'15m','recent_ohlcv':evidence()['15m']}]}
    monkeypatch.setattr('trading.analysis.build_bundle',lambda *a,**kw:SimpleNamespace(fingerprint='bars',to_context=lambda:market))
    monkeypatch.setattr('trading.analysis.render_bundle',lambda *a,**kw:[])
    # Model and validator run at a fixed time consistent with the supplied evidence.
    monkeypatch.setattr('trading.evaluation.service.time.time',lambda:NOW)
    llm=SimpleNamespace(budget=None,last_provider='gemini',last_model='mock',last_status='success',
        last_trace={'attempts':[{'generation_requests':1}]})
    signal=candidate()
    payload=dict(action='trade',signal_id=signal.signal_id,rationale='Fitil bazlı teyitli plan.',invalidation='Yapı bozulması.',watch_note='',
        sizing_plan={'leverage':3,'risk_per_trade_pct':.001},price_plan=plan())
    captures=[]
    def complete(**kw):captures.append(kw);return deepcopy(payload)
    llm.complete_json=complete
    public=SimpleNamespace(get_contract_rules=lambda symbol:ContractRules(.001,1,1,1e8,20,.005,.1,1e8))
    store=RuntimeStore(str(tmp_path/'state'))
    evaluator=ContextEvaluator(llm,store,public,cfg)
    def run():return evaluator.evaluate_candidates([signal],equity=500,risk_per_trade_pct=.01,target_leverage=5,
        live_account_context=dict(mode='LIVE',currency='USDT',cash_usdt=500),live_positions={})
    return run,payload,captures,store,signal


def test_real_evaluator_passes_grounded_model_levels_and_evidence_to_execution(tmp_path,monkeypatch):
    run,payload,captures,store,signal=setup_service(tmp_path,monkeypatch)
    value=run()
    assert value.action==GraphAction.TRADE and value.proposal.stop_loss.price==99.4
    assert value.proposal.take_profit_price==103.9
    assert value.selected_signal.signal_id==signal.signal_id
    assert value.metadata['price_plan']==plan() and value.metadata['price_evidence']==evidence()
    assert json.loads(captures[0]['user'])['account']['price_planning']['enabled'] is True
    assert captures[0]['max_tokens']==1100
    assert store.get('analysis_trace')['price_plan']==plan()
    canonical=store.get('watch:'+signal.symbol)
    assert canonical['candidate']['stop_loss_price']==99.4
    assert canonical['candidate']['signal_id']==signal.signal_id


def test_disabled_price_planning_rejects_model_override_and_keeps_no_canonical_trade(tmp_path,monkeypatch):
    run,payload,captures,store,signal=setup_service(tmp_path,monkeypatch,False)
    assert run().action==GraphAction.HOLD
    assert store.get('watch:'+signal.symbol) is None


def test_null_price_plan_keeps_quant_levels_without_changing_identity(tmp_path,monkeypatch):
    run,payload,captures,store,signal=setup_service(tmp_path,monkeypatch)
    payload['price_plan']=None
    value=run()
    assert value.action==GraphAction.TRADE and value.proposal.stop_loss.price==signal.stop_loss_price


def test_final_runtime_revalidates_fresh_tick_price_and_recalculates_risk(tmp_path):
    runner,private,executor,control=prepare(tmp_path)
    runner.cfg.live_price_plan_enabled=True
    signal=candidate()
    response=result(runner,signal,{'leverage':3,'risk_per_trade_pct':.001})
    now=int(time());stamp=now-1000
    raw=plan();raw['stop_reference']['open_time']=stamp;raw['target_reference']['open_time']=stamp
    rows=evidence()['15m'];rows[0][0]=stamp-900;rows[1][0]=stamp
    response.metadata.update(price_plan=raw,price_evidence={'15m':rows})
    snapshot=runner._refresh(['BTC_USDT'])
    value=runner._final_proposal(signal,response,snapshot,control)
    assert value.stop_loss.price==99.4 and value.take_profit_price==103.9
    assert value.metadata['price_source']=='model'
    assert value.notional*(100.-99.4)/100. < value.max_loss_if_stopped
    runner.cfg.live_price_plan_enabled=False
    with pytest.raises(ValueError,match='disabled'):runner._final_proposal(signal,response,snapshot,control)


def test_short_plan_uses_high_wick_stop_and_low_target():
    signal=candidate().model_copy(update={'side':Side.SHORT})
    rows={'15m':[[STAMP-900,100,101,97,99,10],[STAMP,99,100.5,96,98,10]]}
    raw=plan();raw.update(stop_loss_price=100.6,take_profit_price=96.1)
    raw['stop_reference']['field']='high';raw['target_reference']['field']='low'
    cloned=apply_market_price_plan(signal,raw,rows,price_step=.1,current_price=100,now=NOW)
    assert cloned.stop_loss_price==100.6 and cloned.take_profit_price==96.1


def test_market_moves_after_analysis_rejects_old_entry_plan():
    with pytest.raises(PricePlanError,match='too_far'):
        apply_market_price_plan(candidate(),plan(),evidence(),price_step=.1,current_price=101,now=NOW)


def test_model_target_must_cover_estimated_fees_and_slippage(tmp_path):
    runner,private,executor,control=prepare(tmp_path)
    runner.cfg.live_price_plan_enabled=True
    signal=candidate();response=result(runner,signal)
    stamp=int(time())-1000
    raw=plan();raw['stop_reference']['open_time']=stamp;raw['target_reference']['open_time']=stamp
    raw['take_profit_price']=100.1
    response.metadata.update(price_plan=raw,price_evidence={'15m':[[stamp-900,100,100.2,99,100,10],[stamp,100,100.1,99.5,100,10]]})
    with pytest.raises(ValueError,match='trading costs'):
        runner._final_proposal(signal,response,runner._refresh(['BTC_USDT']),control)
