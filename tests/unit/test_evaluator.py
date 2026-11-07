from types import SimpleNamespace
import json
import pytest
from trading.evaluation.service import ContextEvaluator
from trading.agents.schemas import GraphAction
from trading.runtime.store import RuntimeStore
from trading.config import Settings
from trading.quant.signals import CandidateSignal, StrategyName
from trading.models.domain import Side


@pytest.mark.parametrize('version',['text','vlm'])
def test_variant_same_policy_bounded_memory_and_exact_charts(tmp_path,monkeypatch,version):
    candidate=CandidateSignal(symbol='BTC_USDT',side=Side.LONG,strategy=StrategyName.TREND,entry_price=100,stop_loss_price=98,take_profit_price=104,confidence=.8)
    bundle=SimpleNamespace(fingerprint='stable-candles',to_context=lambda:{'observed_at':123,'features':'confirmed'})
    monkeypatch.setattr('trading.analysis.build_bundle',lambda *a,**kw:bundle)
    paths=[str(tmp_path/f'{tf}.png') for tf in ('4h','15m','5m')]
    monkeypatch.setattr('trading.analysis.render_bundle',lambda *a,**kw:[SimpleNamespace(path=p) for p in paths])
    calls=[]
    def complete(**kw):
        calls.append(kw)
        return dict(action='trade',signal_id=candidate.signal_id,rationale='confirmed setup',invalidation='break level',watch_note='track structure')
    llm=SimpleNamespace(budget=None,complete_json=complete,last_provider='anthropic',last_model='claude-sonnet-4-6',last_fallback_reason='groq_http_429')
    settings=Settings(_env_file=None,analysis_version=version,analysis_chart_dir=str(tmp_path))
    store=RuntimeStore(str(tmp_path/'memory'))
    service=ContextEvaluator(llm,store,None,settings)
    kwargs=dict(equity=500,risk_per_trade_pct=.01,target_leverage=3)
    first=service.evaluate_candidates([candidate],**kwargs)
    assert first.action==GraphAction.TRADE
    assert first.metadata['charts']==paths
    assert first.metadata['decision_ai']==dict(provider='anthropic',model='claude-sonnet-4-6',fallback_reason='groq_http_429')
    assert store.get('memory:BTC_USDT')['history'][-1]['decision_ai']==first.metadata['decision_ai']
    assert calls[0]['images']==(paths if version=='vlm' else None)
    assert store.get('memory:BTC_USDT')['watch_note']=='track structure'
    assert ContextEvaluator(llm,store,None,settings).evaluate_candidates([candidate],**kwargs).hold_reason=='unchanged_closed_candles'
    assert len(calls)==1


def test_unknown_signal_and_extra_fields_fail_closed(tmp_path,monkeypatch):
    candidate=CandidateSignal(symbol='BTC_USDT',side=Side.LONG,strategy=StrategyName.TREND,entry_price=100,stop_loss_price=98,take_profit_price=104,confidence=.8)
    monkeypatch.setattr('trading.analysis.build_bundle',lambda *a,**kw:SimpleNamespace(fingerprint='x',to_context=lambda:{}))
    monkeypatch.setattr('trading.analysis.render_bundle',lambda *a,**kw:[])
    llm=SimpleNamespace(budget=None,complete_json=lambda **kw:dict(action='trade',signal_id='unknown',rationale='x',invalidation='x',watch_note='x'))
    service=ContextEvaluator(llm,RuntimeStore(str(tmp_path/'s')),None,Settings(_env_file=None))
    assert service.evaluate_candidates([candidate],equity=500,risk_per_trade_pct=.01,target_leverage=3).action==GraphAction.HOLD


def test_failed_analysis_retains_candidate_trace_without_prompt(tmp_path,monkeypatch):
    candidate=CandidateSignal(symbol='BTC_USDT',side=Side.LONG,strategy=StrategyName.TREND,entry_price=100,stop_loss_price=98,take_profit_price=104,confidence=.8)
    monkeypatch.setattr('trading.analysis.build_bundle',lambda *a,**kw:SimpleNamespace(fingerprint='closed',to_context=lambda:{}))
    monkeypatch.setattr('trading.analysis.render_bundle',lambda *a,**kw:[])
    llm=SimpleNamespace(budget=None,complete_json=lambda **kw:None,last_trace={'attempts':[{'provider':'groq','model':'test','http_status':400,'status':'rejected'}]})
    store=RuntimeStore(str(tmp_path/'trace'))
    evaluator=ContextEvaluator(llm,store,None,Settings(_env_file=None))
    evaluator.evaluate_candidates([candidate],equity=500,risk_per_trade_pct=.03,target_leverage=10)
    trace=store.get('analysis_trace')
    assert trace['symbol']=='BTC_USDT' and trace['strategy']=='trend'
    assert trace['status']=='invalid_or_unavailable_model'
    assert trace['attempts'][0]['http_status']==400
    assert store.get('analysis_history')[-1]['id']==trace['id']
