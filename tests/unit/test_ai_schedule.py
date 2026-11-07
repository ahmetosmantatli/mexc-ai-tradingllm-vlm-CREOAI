from types import SimpleNamespace
from time import time

import pytest
from pydantic import ValidationError

from trading.config import Settings
from trading.agents.schemas import EvaluationResult,GraphAction
from trading.evaluation.service import ContextEvaluator
from trading.runtime.runner import PaperRunner
from trading.runtime.paper import PaperLedger
from trading.exchange.mexc_public import TickerSnapshot
from trading.quant.signals import CandidateSignal,StrategyName
from trading.models.domain import Side


def signal(symbol,confidence=.8,closed_at=None):
    return CandidateSignal(symbol=symbol,side=Side.LONG,strategy=StrategyName.TREND,entry_price=100,
        stop_loss_price=98,take_profit_price=104,confidence=confidence,
        features={'dedup_key':symbol,'source_closed_at':closed_at or time()-100})


def runner_fixture(tmp_path):
    cfg=Settings(_env_file=None,runtime_db_path=str(tmp_path/'runner'),require_mexc_balance=False,
        paper_seed_usdt=500,anthropic_api_key='fake')
    class Public:
        use_fixtures=True
        def get_ticker(self,symbol):
            return TickerSnapshot(symbol,100,99.99,100.01,1e6,raw={'timestamp':time()*1000,'fairPrice':100})
    runner=PaperRunner(cfg,public=Public())
    runner.ledger=PaperLedger(runner.store,500)
    runner.protection=SimpleNamespace(assert_ready=lambda:None,snapshot=lambda:{})
    return runner


def setup_quota_runner(tmp_path):
    runner=runner_fixture(tmp_path)
    runner.cfg=runner.cfg.model_copy(update={'ai_schedule_mode':'quota'})
    runner.external_scan=True
    runner.candidate_queue().refresh([signal('AAA_USDT',.95,closed_at=time()-100),signal('BBB_USDT',.8,closed_at=time()-100)])
    return runner


def test_interval_is_compatible_default_and_invalid_modes_rejected():
    assert Settings(_env_file=None).ai_schedule_mode=='interval'
    with pytest.raises(ValidationError):Settings(_env_file=None,ai_schedule_mode='unlimited')


def test_quota_mode_visits_next_candidate_without_fixed_ai_wait(tmp_path):
    runner=setup_quota_runner(tmp_path)
    calls=[]
    def evaluate(candidates,**kw):
        calls.append(candidates[0].symbol)
        return EvaluationResult(action=GraphAction.HOLD,hold_reason='hold',metadata={'provider_called':True})
    runner.graph.evaluate_candidates=evaluate
    runner.store.put('last_evaluation_at',time())
    assert runner.cycle()['reason']!='HOLD_EVALUATION_INTERVAL'
    assert runner.cycle()['reason']!='HOLD_EVALUATION_INTERVAL'
    assert calls==['AAA_USDT','BBB_USDT']


def test_quota_mode_does_not_repeat_provider_for_unchanged_context(tmp_path,monkeypatch):
    runner=setup_quota_runner(tmp_path)
    runner.candidate_queue().refresh([signal('AAA_USDT',.95,closed_at=time()-100)])
    monkeypatch.setattr('trading.analysis.build_bundle',lambda *a,**kw:SimpleNamespace(fingerprint='closed-context',to_context=lambda:{'timeframes':[],'observed_at':123}))
    monkeypatch.setattr('trading.analysis.render_bundle',lambda *a,**kw:[])
    calls=[]
    def complete(**kw):
        calls.append(kw)
        return dict(action='hold',signal_id=None,rationale='Kapanış teyidi bekleniyor.',invalidation='Alt seviye kırılırsa iptal.',watch_note='Yeni kapanış bekle.')
    llm=SimpleNamespace(available=True,budget=None,last_status='success',complete_json=complete)
    runner.llm=llm
    runner.graph=ContextEvaluator(llm,runner.store,runner.public,runner.cfg)
    for _ in range(10):
        runner.cycle()
    assert len(calls)==1
    assert runner.store.get('stage')=='unchanged_closed_candles'


def test_quota_mode_respects_provider_backoff_without_hot_retry(tmp_path):
    runner=setup_quota_runner(tmp_path)
    runner.cfg=runner.cfg.model_copy(update={'ai_provider_pool_enabled':True})
    calls=[]
    def evaluate(*args,**kw):
        calls.append(1)
        return EvaluationResult(action=GraphAction.HOLD,hold_reason='invalid_or_unavailable_model',metadata={'provider_called':False})
    runner.graph.evaluate_candidates=evaluate
    runner.cycle()
    assert runner.store.get('provider_defer_until')>time()
    assert runner.cycle()['reason']=='HOLD_PROVIDER_POOL_WAIT'
    assert calls==[1]


def test_quota_mode_still_obeys_pause_and_no_open_position_duplicates(tmp_path):
    runner=setup_quota_runner(tmp_path)
    runner.external_control=lambda:{'enabled':False}
    runner.graph.evaluate_candidates=lambda *a,**kw:pytest.fail('paused quota mode must not call AI')
    assert runner.cycle()['reason']=='HOLD_TELEGRAM_PAUSED'


def test_busy_paid_slot_uses_short_capacity_wait_not_full_quota_delay(tmp_path):
    runner=setup_quota_runner(tmp_path)
    runner.cfg=runner.cfg.model_copy(update={'ai_provider_pool_enabled':True})
    runner.graph.evaluate_candidates=lambda *a,**kw:EvaluationResult(action=GraphAction.HOLD,
        hold_reason='invalid_or_unavailable_model',metadata={'provider_called':False,'provider_busy':True})
    before=time()
    runner.cycle()
    assert before+4 <= runner.store.get('provider_defer_until') <= time()+6
