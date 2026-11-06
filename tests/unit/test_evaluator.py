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
    llm=SimpleNamespace(budget=None,complete_json=complete)
    settings=Settings(_env_file=None,analysis_version=version,analysis_chart_dir=str(tmp_path))
    store=RuntimeStore(str(tmp_path/'memory'))
    service=ContextEvaluator(llm,store,None,settings)
    kwargs=dict(equity=500,risk_per_trade_pct=.01,target_leverage=3)
    first=service.evaluate_candidates([candidate],**kwargs)
    assert first.action==GraphAction.TRADE
    assert first.metadata['charts']==paths
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
