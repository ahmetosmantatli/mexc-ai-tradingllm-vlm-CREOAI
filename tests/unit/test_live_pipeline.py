"""Entire LIVE software path against an in-memory fake exchange; no HTTP."""
import json
from copy import deepcopy
from time import time
from types import SimpleNamespace
from trading.config import Settings
from trading.runtime.live_runner import LiveRunner
from trading.runtime.store import RuntimeStore
from trading.brokers.live.automatic import AutomaticLiveExecutor,AutomaticLivePolicy
from trading.brokers.live.execution import LiveExecutionGates
from trading.risk.engine import RiskEngine
from trading.exchange.mexc_public import TickerSnapshot
from .test_live_execution import Client,Public
from .test_live_runner import candidate


class QuotePublic(Public):
    def get_ticker(self,symbol):
        return TickerSnapshot(symbol,100.,99.99,100.,1e6,raw={'timestamp':time()*1000,'fairPrice':100.})


class Model:
    budget=None;last_provider='groq';last_model='fake-model';last_status='success'
    last_trace={'attempts':[{'generation_requests':1,'count_requests':0,'status':'success'}]}
    def __init__(self):self.calls=0
    def complete_json(self,**kw):
        self.calls+=1
        data=json.loads(kw['user'])
        assert data['account']['operating_context']['mode']=='LIVE'
        return dict(action='trade',signal_id=data['candidate']['signal_id'],rationale='Teyitli yapıya göre deneme kararı.',
            invalidation='Yapı bozulursa iptal.',watch_note='',sizing_plan={'leverage':5,'risk_per_trade_pct':.002})


def test_real_software_pipeline_opens_reconciles_native_close_and_reenters_without_human_approval(tmp_path,monkeypatch):
    cfg=Settings(_env_file=None,broker_mode='live',live_trading_enabled=True,live_automatic_enabled=True,
        live_allowed_accounts=('ahmet',),risk_profile='balanced',risk_per_trade_pct=.01,default_target_leverage=5,
        kill_switch_path=str(tmp_path/'kill'),analysis_chart_dir=str(tmp_path/'charts'))
    store=RuntimeStore(str(tmp_path/'live'))
    private,public,llm=Client(),QuotePublic(),Model()
    control=dict(enabled=True,stopped=False,updated_at=1,version='text',risk_profile='balanced',
        risk_per_trade_pct=.01,max_leverage=5,max_positions=2,allocation_mode='full')
    executor=AutomaticLiveExecutor(AutomaticLivePolicy(True,('ahmet',),('7',),2),
        LiveExecutionGates(True,'live',('7',),True),store,private,public,RiskEngine(settings=cfg),
        control_provider=lambda:dict(control),account_id='ahmet',binding_id='binding-1',
        can_execute=lambda:control['enabled'])
    runner=LiveRunner(cfg,store=store,account_id='ahmet',binding_id='binding-1',private=private,public=public,
        llm=llm,external_control=lambda:dict(control),executor=executor)
    monkeypatch.setattr('trading.analysis.build_bundle',lambda *a,**kw:SimpleNamespace(fingerprint='closed',to_context=lambda:{'timeframes':[]}))
    monkeypatch.setattr('trading.analysis.render_bundle',lambda *a,**kw:[])
    executor.grant_start('7',1)
    first=candidate()
    runner.candidate_queue().refresh([first])
    result=runner.cycle()
    assert result['reason']=='LIVE_PROTECTED', (result,store.get('live_cycle_error'),executor.intents())
    assert len(private.submits)==1 and llm.calls==1
    intent=executor.intents()[0]
    assert intent['authorization_type']=='automatic_start_grant' and 'approved_by' not in intent
    assert store.get('paper') is None
    # Native exchange closes the position while AI/new entries are stopped.
    control.update(enabled=False,stopped=True,updated_at=2)
    executor.revoke_start()
    contracts=private.order_row['dealVol']
    private.make_history(contracts);private.positions=[];private.stops=[]
    private.assets[0]['equity']=504.3
    runner.monitor()
    closed=executor.intents()[0]
    assert closed['status']=='closed' and closed['net_pnl']==4.3
    assert runner.status('check')['live_account']['balance']['equity']==504.3
    assert runner.cycle()['reason']=='LIVE_PAUSED' and len(private.submits)==1
    # Explicit new Start grants execution; normal native closure did not wedge entry.
    control.update(enabled=True,stopped=False,updated_at=3)
    executor.grant_start('7',3)
    second=candidate();second.features['dedup_key']='next-closed-bar'
    runner.candidate_queue().refresh([second])
    result=runner.cycle()
    assert result['reason']=='LIVE_PROTECTED', (result,store.get('live_cycle_error'),executor.intents())
    assert len(private.submits)==2


def test_live_hold_is_not_recalled_for_new_snapshot_generation(tmp_path,monkeypatch):
    from .test_live_runner import prepare
    from trading.evaluation.service import ContextEvaluator
    runner,private,executor,control=prepare(tmp_path)
    calls=[]
    class Hold:
        budget=None
        last_trace={'attempts':[{'generation_requests':1}]}
        last_status='success'
        def complete_json(self,**kw):
            calls.append(json.loads(kw['user']))
            return dict(action='hold',signal_id=None,rationale='Yeni kapanış bekleniyor.',invalidation='Yapı bozulması.',watch_note='Teyit bekle.')
    runner.llm=Hold()
    runner.graph=ContextEvaluator(runner.llm,runner.store,runner.public,runner.cfg)
    monkeypatch.setattr('trading.analysis.build_bundle',lambda *a,**kw:SimpleNamespace(fingerprint='same-closed',to_context=lambda:{'timeframes':[]}))
    monkeypatch.setattr('trading.analysis.render_bundle',lambda *a,**kw:[])
    signal=candidate();runner.candidate_queue().refresh([signal])
    for _ in range(5):runner.cycle()
    assert len(calls)==1
    assert calls[0]['account']['operating_context']['equity_snapshot_usdt']==500
    assert runner.store.get('paper') is None


def test_expired_live_start_grant_blocks_ai_before_provider_call(tmp_path):
    from .test_live_execution import setup
    executor,private,store=setup(tmp_path,automatic=True)
    grant=store.get(executor._grant_key());grant['expires_at']=time()-1;store.put(executor._grant_key(),grant)
    assert executor.entry_ready() is False


def test_loss_circuit_breaker_blocks_model_but_monitor_keeps_reading(tmp_path):
    from .test_live_runner import prepare
    runner,private,executor,control=prepare(tmp_path)
    runner.monitor()
    private.equity=600.;runner.monitor()
    private.equity=510.;runner.monitor()
    runner.candidate_queue().refresh([candidate()])
    runner._evaluate=lambda *a,**kw:(_ for _ in ()).throw(AssertionError('No AI after loss breaker'))
    assert runner.cycle()['reason']=='HOLD_LIVE_LOSS_CIRCUIT_BREAKER'
    assert not executor.calls
    assert runner.monitor()['reason']=='LIVE_RECONCILED'


def test_ai_selected_wick_stop_and_target_reach_native_exchange_protection(tmp_path,monkeypatch):
    cfg=Settings(_env_file=None,broker_mode='live',live_trading_enabled=True,live_automatic_enabled=True,
        live_price_plan_enabled=True,live_allowed_accounts=('ahmet',),risk_profile='balanced',risk_per_trade_pct=.01,
        default_target_leverage=5,kill_switch_path=str(tmp_path/'kill'),analysis_chart_dir=str(tmp_path/'charts'))
    store=RuntimeStore(str(tmp_path/'live'));private,public=Client(),QuotePublic()
    control=dict(enabled=True,stopped=False,updated_at=1,version='text',risk_profile='balanced',risk_per_trade_pct=.01,
        max_leverage=5,max_positions=2,allocation_mode='full')
    stamp=int(time())-1000
    rows=[[stamp-900,100,102,99,100,10],[stamp,100,104,99.5,101,12]]
    selected=dict(entry_type='market',entry_price=100.,stop_loss_price=99.4,take_profit_price=103.9,entry_timeframe='15m',
        stop_reference=dict(timeframe='15m',open_time=stamp,field='low'),
        target_reference=dict(timeframe='15m',open_time=stamp,field='high'),reason='Görülen alt fitile dayalı stop ve üst likidite hedefi.')
    class PlanningModel(Model):
        def complete_json(self,**kw):
            response=super().complete_json(**kw)
            response['price_plan']=deepcopy(selected)
            response['sizing_plan']['risk_per_trade_pct']=.001
            return response
    llm=PlanningModel()
    executor=AutomaticLiveExecutor(AutomaticLivePolicy(True,('ahmet',),('7',),2),LiveExecutionGates(True,'live',('7',),True),
        store,private,public,RiskEngine(settings=cfg),control_provider=lambda:dict(control),account_id='ahmet',binding_id='binding-1',
        can_execute=lambda:control['enabled'])
    runner=LiveRunner(cfg,store=store,account_id='ahmet',binding_id='binding-1',private=private,public=public,llm=llm,
        external_control=lambda:dict(control),executor=executor)
    monkeypatch.setattr('trading.analysis.build_bundle',lambda *a,**kw:SimpleNamespace(fingerprint='bars',
        to_context=lambda:{'timeframes':[dict(timeframe='15m',recent_ohlcv=deepcopy(rows))]}))
    monkeypatch.setattr('trading.analysis.render_bundle',lambda *a,**kw:[])
    executor.grant_start('7',1)
    runner.candidate_queue().refresh([candidate()])
    status=runner.cycle()
    assert status['reason']=='LIVE_PROTECTED', (status,store.get('live_cycle_error'),executor.intents())
    intent=executor.intents()[0]
    assert intent['proposal']['stop_loss']['price']==99.4
    assert intent['proposal']['take_profit_price']==103.9
    assert intent['proposal']['metadata']['price_source']=='model'
    assert private.stops[0]['stopLossPrice']==99.4 and private.stops[0]['takeProfitPrice']==103.9
    assert len(private.submits)==1 and store.get('paper') is None
