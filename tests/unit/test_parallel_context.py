from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading
import time

import pytest

from trading.agents.schemas import GraphAction
from trading.config import Settings
from trading.evaluation.service import ContextEvaluator,finalize_trace,publish_progress
from trading.quant.signals import CandidateSignal, StrategyName
from trading.models.domain import Side
from trading.runtime.store import RuntimeStore


def signal(symbol):
    return CandidateSignal(symbol=symbol,side=Side.LONG,strategy=StrategyName.TREND,
        entry_price=100,stop_loss_price=98,take_profit_price=104,confidence=.8)


@pytest.mark.parametrize('actions',[('trade','hold'),('trade','trade')])
def test_parallel_reviews_keep_both_results_and_finalize_exact_trade(tmp_path,monkeypatch,actions):
    store=RuntimeStore(str(tmp_path/'state'))
    barrier=threading.Barrier(2)
    candidates=[signal('BTC_USDT'),signal('ETH_USDT')]
    def bundle(public,symbol):
        return SimpleNamespace(fingerprint=symbol+'-closed',to_context=lambda:{'symbol':symbol,'timeframes':[]})
    monkeypatch.setattr('trading.analysis.build_bundle',bundle)
    monkeypatch.setattr('trading.analysis.render_bundle',lambda *a,**kw:[])
    def evaluate(index):
        candidate=candidates[index]
        action=actions[index]
        llm=SimpleNamespace(budget=None,on_trace=None,response_validator=None,decision_schema=None,
            last_credential_id='safe-label',last_quota_group='quota-label')
        def complete(**kwargs):
            barrier.wait(timeout=2)
            llm.on_trace({'attempts':[dict(provider='gemini',model='test',status='success',generation_requests=1)],'status':'success'})
            return dict(action=action,signal_id=candidate.signal_id if action=='trade' else None,
                rationale='Teyit var.' if action=='trade' else 'Teyit bekleniyor.',invalidation='Dip kırılırsa iptal.',watch_note='Yeni kapanışı izle.')
        llm.complete_json=complete
        evaluator=ContextEvaluator(llm,store,None,Settings(_env_file=None))
        return evaluator.evaluate_candidates([candidate],equity=500,risk_per_trade_pct=.01,target_leverage=3)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(evaluate,range(2)))
    history=store.get('analysis_history')
    assert len(history)==2 and {r['symbol'] for r in history}=={'BTC_USDT','ETH_USDT'}
    ids=[result.metadata['analysis_trace_id'] for result in results]
    assert len(set(ids))==2
    assert finalize_trace(store,ids[0],'opened','PAPER_OPENED')
    if actions[1]=='trade':
        assert finalize_trace(store,ids[1],'rejected','HOLD_PRICE_DRIFT')
    else:
        assert not finalize_trace(store,ids[1],'opened','PAPER_OPENED')
    finalized={row['id']:row for row in store.get('analysis_history')}
    assert finalized[ids[0]]['execution_status']=='opened'
    assert finalized[ids[1]]['execution_status']==('rejected' if actions[1]=='trade' else 'not_opened')
    assert not finalize_trace(store,ids[0],'rejected','HOLD_SETTINGS_CHANGED')
    assert store.db.execute('SELECT COUNT(*) FROM ai_usage_events').fetchone()[0]==2
    if actions[1]=='trade':
        assert results[1].metadata['decision_ai']['credential_id']=='safe-label'


def test_latest_review_is_not_replaced_by_older_completion_or_late_callback(tmp_path):
    store=RuntimeStore(str(tmp_path/'state'))
    evaluator=ContextEvaluator.__new__(ContextEvaluator);evaluator.store=store
    old=dict(id='older',started_at=1,status='completed',completed_at=3,action='trade',execution_status='awaiting_risk',attempts=[])
    newer=dict(id='newer',started_at=2,status='completed',completed_at=4,action='hold',execution_status='not_opened',attempts=[])
    evaluator._save_trace(newer)
    evaluator._save_trace(old)
    assert store.get('analysis_trace')['id']=='newer'
    assert finalize_trace(store,'older','opened','PAPER_OPENED')
    assert store.get('analysis_trace')['id']=='newer'
    publish_progress(store,dict(id='newer',started_at=2,status='pending',attempts=[]))
    assert store.get('analysis_trace')['status']=='completed'
    assert store.get('analysis_trace')['execution_status']=='not_opened'


def test_parallel_archive_keeps_all_records_and_bounded_recent_history(tmp_path):
    store=RuntimeStore(str(tmp_path/'state'))
    evaluator=ContextEvaluator.__new__(ContextEvaluator);evaluator.store=store
    def save(index):
        evaluator._save_trace(dict(id=str(index),started_at=index,status='completed',completed_at=index+1,
            action='hold',execution_status='not_opened',attempts=[]))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(save,range(50)))
    history=store.get('analysis_history')
    assert len(history)==30
    assert {row['id'] for row in history}=={str(i) for i in range(20,50)}
    assert store.db.execute('SELECT COUNT(*) FROM ai_usage_events').fetchone()[0]==50


def test_chart_render_is_serialized_without_holding_model_network_lock(monkeypatch):
    import trading.analysis.charts as charts
    active=[0]
    peak=[0]
    guard=threading.Lock()
    def render(*args):
        with guard:
            active[0]+=1;peak[0]=max(peak[0],active[0])
        time.sleep(.01)
        with guard:active[0]-=1
        return []
    monkeypatch.setattr(charts,'_render',render)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda symbol:charts.render_bundle(SimpleNamespace(symbol=symbol),'unused'),['BTC_USDT','ETH_USDT']))
    assert peak[0]==1
