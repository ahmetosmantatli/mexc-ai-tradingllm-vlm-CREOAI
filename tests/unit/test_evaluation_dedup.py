import copy
import json
from types import SimpleNamespace

from trading.config import Settings
from trading.evaluation.service import ContextEvaluator
from trading.models.domain import Side
from trading.quant.signals import CandidateSignal,StrategyName
from trading.runtime.store import RuntimeStore


def setup(tmp_path,monkeypatch,response):
    store=RuntimeStore(str(tmp_path/'state'))
    store.put('paper',dict(currency='USDT',initial=500.,cash=500.,realized=0.,fees=0.,funding=0.,peak=501.,
        day='2026-11-07',day_start=500.,positions={'BTC_USDT':dict(symbol='BTC_USDT',side='long',entry=100.,stop=98.,target=104.,
            quantity=1.,margin=10.,notional=100.,risk=2.,leverage=10.,opened_at=1794052000000,unrealized=0.,current_r=0,mfe_r=0,mae_r=0)},closed_positions=[]))
    current=['same-closed-bars']
    monkeypatch.setattr('trading.analysis.build_bundle',lambda *a,**kw:SimpleNamespace(fingerprint=current[0],to_context=lambda:{}))
    renders=[]
    monkeypatch.setattr('trading.analysis.render_bundle',lambda *a,**kw:renders.append(True) or [])
    calls=[]
    def complete(**kwargs):
        calls.append(json.loads(kwargs['user']))
        return copy.deepcopy(response)
    llm=SimpleNamespace(budget=None,complete_json=complete,
        last_trace={'attempts':[dict(provider='anthropic',status='error_or_invalid_output' if response is None else 'success',generation_requests=1)]})
    evaluator=ContextEvaluator(llm,store,None,Settings(_env_file=None))
    candidate=CandidateSignal(symbol='ETH_USDT',side=Side.LONG,strategy=StrategyName.TREND,
        entry_price=100.,stop_loss_price=98.,take_profit_price=104.,confidence=.8)
    return store,current,renders,calls,evaluator,candidate


def review(evaluator,candidate,equity=500):
    return evaluator.evaluate_candidates([candidate],equity=equity,risk_per_trade_pct=.03,target_leverage=10)


def test_valid_hold_ignores_quote_pnl_noise_but_sends_fresh_context_on_real_events(tmp_path,monkeypatch):
    response=dict(action='hold',signal_id=None,rationale='15dk teyidi bekle.',invalidation='Dip kırılırsa iptal.',watch_note='Yeni yapı izle.')
    store,current,renders,calls,evaluator,candidate=setup(tmp_path,monkeypatch,response)
    review(evaluator,candidate)
    for mark in (1.25,2.5,-.5):
        state=store.get('paper')
        position=state['positions']['BTC_USDT']
        position.update(unrealized=mark,current_r=mark/2,mfe_r=max(mark,0),mae_r=abs(min(mark,0)))
        state['peak']=max(state['peak'],500+mark)
        store.put('paper',state)
        result=review(evaluator,candidate,500+mark)
        assert result.metadata['provider_called'] is False
    assert len(calls)==1 and len(renders)==1
    # A moved stop is a real management event and must not be hidden by dedup.
    state=store.get('paper');state['positions']['BTC_USDT']['stop']=99.;store.put('paper',state)
    assert review(evaluator,candidate,499.5).metadata['provider_called'] is True
    assert calls[-1]['account']['equity']==499.5
    assert calls[-1]['open_positions'][0]['unrealized']==-.5 # full current snapshot, not the dedup projection
    assert calls[-1]['open_positions'][0]['stop']==99.
    # Realized cash changes are also events even if closed market bars are equal.
    state=store.get('paper');state['cash']=510.;state['realized']=10.;store.put('paper',state)
    assert review(evaluator,candidate,509.5).metadata['provider_called'] is True
    assert len(calls)==3


def test_failed_identical_input_has_bounded_backoff_not_a_paid_hotloop(tmp_path,monkeypatch):
    store,current,renders,calls,evaluator,candidate=setup(tmp_path,monkeypatch,None)
    first=review(evaluator,candidate)
    assert first.hold_reason=='invalid_or_unavailable_model' and first.metadata['provider_called'] is True
    for _ in range(5):
        second=review(evaluator,candidate)
        assert second.hold_reason=='analysis_error_backoff' and second.metadata['provider_called'] is False
    assert len(calls)==1 and len(renders)==1
    # Changed closed evidence is not blocked by a failure for old input.
    current[0]='new-closed-bars'
    assert review(evaluator,candidate).metadata['provider_called'] is True
    assert len(calls)==2
    # After the error delay, the same input can be attempted again.
    key='memory:ETH_USDT:trend:long'
    memory=store.get(key);memory['last_failure']['retry_at']=0;store.put(key,memory)
    assert review(evaluator,candidate).metadata['provider_called'] is True
    assert len(calls)==3


def test_no_http_quota_skip_does_not_create_a_model_failure_backoff(tmp_path,monkeypatch):
    store,current,renders,calls,evaluator,candidate=setup(tmp_path,monkeypatch,None)
    evaluator.llm.last_trace={'attempts':[dict(provider='groq',status='quota_skipped',generation_requests=0,count_requests=0)]}
    result=review(evaluator,candidate)
    assert result.metadata['provider_called'] is False
    assert not (store.get('memory:ETH_USDT:trend:long',{}) or {}).get('last_failure')


def test_paid_inflight_is_capacity_wait_not_error_backoff(tmp_path,monkeypatch):
    store,current,renders,calls,evaluator,candidate=setup(tmp_path,monkeypatch,None)
    evaluator.llm.last_status='paid_in_flight'
    evaluator.llm.last_trace={'attempts':[dict(provider='anthropic',status='paid_in_flight',generation_requests=0,count_requests=0)]}
    result=review(evaluator,candidate)
    assert result.metadata['provider_called'] is False
    assert result.metadata['provider_busy'] is True
    assert not (store.get('memory:ETH_USDT:trend:long',{}) or {}).get('last_failure')
