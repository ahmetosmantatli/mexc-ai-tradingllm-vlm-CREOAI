from types import SimpleNamespace
import json
import time

from trading.agents.schemas import GraphAction
from trading.config import Settings
from trading.evaluation.service import ContextEvaluator
from trading.evaluation.watch import WatchCoordinator
from trading.models.domain import Side
from trading.quant.signals import CandidateSignal,StrategyName
from trading.runtime.store import RuntimeStore


def signal(side=Side.LONG):
    return CandidateSignal(symbol='BTC_USDT',side=side,strategy=StrategyName.TREND,entry_price=100,
        stop_loss_price=98 if side==Side.LONG else 102,take_profit_price=104 if side==Side.LONG else 96,confidence=.8)


def plan():
    return dict(trigger_condition='price_above',trigger_price=101.,timeframe=None,
        invalidation_condition='below',invalidation_price=97.,expires_at=time.time()+3600)


def evaluate_setup(tmp_path,monkeypatch):
    store=RuntimeStore(str(tmp_path/'state'))
    monkeypatch.setattr('trading.analysis.build_bundle',lambda *a,**kw:SimpleNamespace(fingerprint='same',to_context=lambda:{}))
    monkeypatch.setattr('trading.analysis.render_bundle',lambda *a,**kw:[])
    response=[dict(action='watch',signal_id=None,rationale='Direnç üstünde teyit bekliyorum.',invalidation='97 altı iptal.',watch_note='101 üstü izle.',watch_plan=plan())]
    calls=[]
    def complete(**kwargs):
        calls.append(json.loads(kwargs['user']))
        return response[0]
    llm=SimpleNamespace(budget=None,complete_json=complete)
    evaluator=ContextEvaluator(llm,store,None,Settings(_env_file=None))
    def run(candidate=None):
        return evaluator.evaluate_candidates([candidate or signal()],equity=500,risk_per_trade_pct=.03,target_leverage=10)
    return store,response,calls,run


def test_structured_watch_persists_and_hold_revision_does_not_create_call_loop(tmp_path,monkeypatch):
    store,response,calls,run=evaluate_setup(tmp_path,monkeypatch)
    first=run()
    card=WatchCoordinator(store).card('BTC_USDT')
    assert first.hold_reason=='watch' and card['status']=='watching'
    assert first.metadata['watch_revision']==card['revision']==1
    assert calls[0]['account']['canonical_watch']['revision']==0
    assert run().metadata['provider_called'] is False
    assert len(calls)==1
    WatchCoordinator(store).observe('BTC_USDT',price=102.)
    assert run().metadata['provider_called'] is True # explicit trigger lifecycle is new evidence
    assert len(calls)==2


def test_opposite_trade_requires_explicit_current_revision_and_reason(tmp_path,monkeypatch):
    store,response,calls,run=evaluate_setup(tmp_path,monkeypatch)
    run()
    short=signal(Side.SHORT)
    response[0]=dict(action='trade',signal_id=short.signal_id,rationale='Eski long planı düşüşte yapı kırıldığı için geçersiz.',invalidation='102 üstü iptal.',watch_note='')
    missing=run(short)
    assert missing.hold_reason=='watch_conflict'
    assert WatchCoordinator(store).card('BTC_USDT')['status']=='watching'
    assert run(short).hold_reason=='analysis_error_backoff'
    assert len(calls)==2
    # Error delay elapses; an explicit acknowledgement can then be reviewed.
    memory=store.get('memory:BTC_USDT:trend:short')
    memory['last_failure']['retry_at']=0
    store.put('memory:BTC_USDT:trend:short',memory)
    response[0]['reviewed_watch_revision']=1
    allowed=run(short)
    assert allowed.action==GraphAction.TRADE
    assert allowed.metadata['watch_revision']==2
    card=WatchCoordinator(store).card('BTC_USDT')
    assert card['status']=='superseded' and card['side']=='short'
    assert calls[-1]['account']['canonical_watch']['side']=='long'


def test_late_provider_result_cannot_overwrite_cancelled_watch(tmp_path,monkeypatch):
    store,response,calls,run=evaluate_setup(tmp_path,monkeypatch)
    run()
    candidate=signal(Side.SHORT)
    response[0]=dict(action='trade',signal_id=candidate.signal_id,rationale='Önceki long planı değişen yapı nedeniyle iptal.',invalidation='102 üstü',watch_note='',reviewed_watch_revision=1)
    # Provider callback cancels the plan after the evaluator captured revision1.
    evaluator=run.__closure__
    # Locate the local evaluator without coupling to closure ordering.
    local=next(cell.cell_contents for cell in evaluator if isinstance(cell.cell_contents,ContextEvaluator))
    def complete(**kwargs):
        WatchCoordinator(store).terminate('BTC_USDT','cancelled',expected_revision=1)
        return response[0]
    local.llm.complete_json=complete
    result=run(candidate)
    assert result.hold_reason=='watch_revision_changed'
    assert WatchCoordinator(store).card('BTC_USDT')['status']=='cancelled'


def test_invalid_sizing_does_not_supersede_existing_valid_watch(tmp_path,monkeypatch):
    store,response,calls,run=evaluate_setup(tmp_path,monkeypatch)
    run()
    candidate=signal(Side.SHORT)
    response[0]=dict(action='trade',signal_id=candidate.signal_id,rationale='Eski long planı yapı bozulduğu için iptal edilmeli.',invalidation='102 üstü',watch_note='',reviewed_watch_revision=1,
        sizing_plan={'leverage':100,'risk_per_trade_pct':.03})
    result=run(candidate)
    assert result.hold_reason=='invalid_model_sizing'
    assert WatchCoordinator(store).card('BTC_USDT')['status']=='watching'
    assert WatchCoordinator(store).card('BTC_USDT')['revision']==1
