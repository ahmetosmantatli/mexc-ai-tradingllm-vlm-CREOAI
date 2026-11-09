from copy import deepcopy
from types import SimpleNamespace
from time import time
from threading import Barrier

import pytest

from trading.config import Settings,RiskProfile
from trading.exchange.contracts import ContractRules
from trading.exchange.mexc_public import TickerSnapshot
from trading.quant.signals import CandidateSignal
from trading.agents.schemas import EvaluationResult,GraphAction,MarketAnalystNote,StrategyReviewNote,PortfolioAnalystNote
from trading.evaluation.planning import apply_decision_sizing
from trading.evaluation.watch import WatchCoordinator
from trading.runtime.store import RuntimeStore
from trading.runtime.live_runner import LiveRunner


class Private:
    def __init__(self):
        self.equity=500.;self.available=400.;self.positions=[];self.orders=[];self.fail=False;self.changed=[];self.multiplier=3
    def account_assets(self):
        if self.fail:raise ValueError('private response never persisted')
        return [dict(currency='USDT',equity=self.equity,availableBalance=self.available,positionMargin=0,unrealized=0)]
    def open_positions(self,*args):return deepcopy(self.positions)
    def current_orders(self,**kw):return deepcopy(self.orders)
    def position_mode(self):return 1
    def risk_limits(self,symbol):
        return {symbol:[dict(symbol=symbol,positionType=s,level=1,maxVol=1e8,maxLeverage=20,mmr=.005,imr=.05) for s in (1,2)]}
    def leverage(self,symbol):
        return [dict(positionType=s,openType=1,leverage=self.multiplier,maxVol=1e8,currentMmr=.005,maxLeverageView=20) for s in (1,2)]
    def change_leverage(self,**kw):self.changed.append(kw);self.multiplier=kw['leverage']


class Public:
    use_fixtures=True
    def get_ticker(self,symbol):return TickerSnapshot(symbol,100,99.99,100.,1e6,raw={'timestamp':time()*1000,'fairPrice':100})
    def get_contract_rules(self,symbol):return ContractRules(.001,1,1,1e8,20,.005,.01,1e8)


class Executor:
    def __init__(self):self.calls=[];self.can_execute=lambda:True;self.rows=[];self.monitor_calls=0
    def authorize(self,proposal,control_revision):
        self.calls.append(('authorize',proposal,control_revision));self.proposal=proposal
        return {'intent_id':'intent1'}
    def execute(self,intent_id):
        assert self.can_execute()
        self.calls.append(('execute',intent_id))
        return {'intent_id':intent_id,'status':'protected'}
    def reconcile_all(self):self.monitor_calls+=1
    def intents(self):return deepcopy(self.rows)
    def request_close(self,intent_id):self.calls.append(('close',intent_id));return {'status':'close_requested'}


def candidate(symbol='BTC_USDT'):
    return CandidateSignal(symbol=symbol,side='long',strategy='trend',entry_price=100,stop_loss_price=98,
        take_profit_price=106,confidence=.9,features={'source_open_at':time()-1000,'source_closed_at':time()-100,'dedup_key':symbol+'-bar'})


def prepare(tmp_path):
    cfg=Settings(_env_file=None,risk_profile=RiskProfile.BALANCED,risk_per_trade_pct=.01,
        default_target_leverage=5,paper_max_leverage=5,kill_switch_path=str(tmp_path/'kill'),
        runtime_db_path=str(tmp_path/'runtime'))
    cfg=cfg.model_copy(update={'ai_parallel_candidates':2})
    store=RuntimeStore(cfg.runtime_db_path)
    control=dict(enabled=True,version='text',risk_profile='balanced',risk_per_trade_pct=.01,
        capital_amount=200.,allocation_mode='fixed',updated_at=1,max_positions=2)
    private,executor=Private(),Executor()
    llm=SimpleNamespace(last_status='success',can_call=lambda:True)
    runner=LiveRunner(cfg,store=store,account_id='ahmet',binding_id='binding1',private=private,public=Public(),llm=llm,
        external_control=lambda:dict(control),executor=executor)
    return runner,private,executor,control


def result(runner,signal,plan=None):
    coordinator=WatchCoordinator(runner.store)
    revision=coordinator.card(signal.symbol)['revision']
    assert coordinator.commit(signal,'trade',revision)
    proposal=apply_decision_sizing(signal,plan,200.,.01,5.,20.)
    return EvaluationResult(action=GraphAction.TRADE,proposal=proposal,selected_signal=signal,
        market=MarketAnalystNote(mode='llm'),strategy=StrategyReviewNote(mode='llm'),portfolio=PortfolioAnalystNote(mode='llm'),
        metadata=dict(sizing_plan=plan,watch_revision=revision+1,decision_started_at=time(),rationale='confirmed structure',
            decision_ai={'provider':'mock','model':'test'},charts=[]))


def test_fresh_exchange_equity_and_allocation_not_paper_or_env(tmp_path):
    runner,private,_,control=prepare(tmp_path)
    snapshot=runner._refresh()
    assert runner._capital(snapshot,control)==200.
    private.equity=150.;private.available=100.
    snapshot=runner._refresh()
    assert runner._capital(snapshot,control)==150.
    control['allocation_mode']='full'
    assert runner._capital(snapshot,control)==150.
    assert runner.store.get('paper') is None
    assert not hasattr(runner,'ledger')
    assert snapshot['balance']['available']==100.


def test_paused_monitor_reconciles_real_account_but_cycle_never_submits(tmp_path):
    runner,private,executor,control=prepare(tmp_path)
    control['enabled']=False
    assert runner.cycle()['reason']=='LIVE_PAUSED'
    assert not executor.calls
    assert runner.monitor()['reason']=='LIVE_RECONCILED'
    assert executor.monitor_calls==1
    assert runner.status('check')['live_account']['balance']['equity']==500.


def test_failed_refresh_revokes_status_and_disconnect_fails_closed(tmp_path):
    runner,private,_,_=prepare(tmp_path)
    runner.monitor()
    private.fail=True
    assert runner.monitor()['reason']=='HOLD_LIVE_RECONCILIATION'
    assert runner.status('check')['account']['ready'] is False
    assert runner.can_disconnect() is False
    assert 'private response' not in str(runner.store.get('live_monitor_error'))


def test_entry_sizing_fresh_real_allocation_fees_and_leverage_reread(tmp_path):
    runner,private,executor,control=prepare(tmp_path)
    signal=candidate();runner.candidate_queue().refresh([signal])
    decision=result(runner,signal,dict(leverage=2,risk_per_trade_pct=.005))
    private.equity=150.;private.available=140.
    assert runner._apply(signal,decision,control)['reason']=='LIVE_PROTECTED'
    proposal=executor.calls[0][1]
    assert proposal.max_loss_if_stopped==pytest.approx(.75)
    assert proposal.notional<.75/.02
    assert proposal.leverage==2
    assert proposal.metadata['sized_from_equity']==150.
    assert proposal.metadata['decision_ai']['provider']=='mock'
    assert private.changed[0]['leverage']==2
    assert [call[0] for call in executor.calls]==['authorize','execute']
    assert runner.store.get('live_seen')==[signal.features['dedup_key']]
    assert runner.store.get('paper') is None


@pytest.mark.parametrize('change',['pause','revision','stale_watch','stale_candidate','expired'])
def test_changed_entry_inputs_never_reach_executor(tmp_path,change):
    runner,_,executor,control=prepare(tmp_path)
    signal=candidate();runner.candidate_queue().refresh([signal])
    decision=result(runner,signal)
    original=dict(control)
    if change=='pause':control['enabled']=False
    if change=='revision':control['updated_at']=2
    if change=='stale_watch':WatchCoordinator(runner.store).commit(signal,'hold',1)
    if change=='stale_candidate':runner.candidate_queue().drop(signal)
    if change=='expired':decision.metadata['decision_started_at']=time()-500
    assert runner._apply(signal,decision,original)['reason'].startswith('HOLD_')
    assert not executor.calls


def test_final_gate_catches_watch_change_during_executor_preflight(tmp_path):
    runner,_,executor,control=prepare(tmp_path)
    signal=candidate();runner.candidate_queue().refresh([signal])
    decision=result(runner,signal)
    def execute(intent_id):
        WatchCoordinator(runner.store).commit(signal,'hold',1)
        assert runner.executor.can_execute() is False
        return {'status':'entry_rejected'}
    executor.execute=execute
    assert runner._apply(signal,decision,control)['reason']=='HOLD_LIVE_ENTRY_REJECTED'
    assert runner.store.get('live_seen') is None


def test_disconnect_requires_no_real_exposure_orders_or_uncertain_intents(tmp_path):
    runner,_,executor,_=prepare(tmp_path)
    assert runner.can_disconnect()
    executor.rows=[dict(intent_id='i',entry_write_attempted=True,status='entry_write_unknown')]
    assert not runner.can_disconnect()
    executor.rows[0]['status']='closed'
    assert runner.can_disconnect()


def test_manual_close_maps_symbol_to_one_managed_intent_while_paused(tmp_path):
    runner,_,executor,control=prepare(tmp_path)
    control['enabled']=False
    executor.rows=[dict(intent_id='i',entry_write_attempted=True,status='protected',proposal={'symbol':'BTC_USDT'})]
    assert runner.request_close({'symbol':'BTC_USDT'})['status']=='close_requested'
    assert executor.calls==[('close','i')]
    with pytest.raises(ValueError):runner.request_close({'symbol':'ETH_USDT'})


def test_status_contains_authoritative_account_unknown_pnl_never_fabricated(tmp_path):
    runner,private,executor,_=prepare(tmp_path)
    private.positions=[dict(positionId=45,symbol='BTC_USDT',positionType=1,openType=1,state=1,
        holdVol=2,leverage=3,holdAvgPrice=100,im=10,unRealizedPnl=3,liquidatePrice=70)]
    executor.rows=[dict(intent_id='i',position_id='45',entry_write_attempted=True,status='protected',
        proposal={'symbol':'BTC_USDT','stop_loss':{'price':98},'take_profit_price':106,
            'metadata':{'analysis_version':'vlm','decision_ai':{'provider':'mock'}}})]
    runner._refresh()
    status=runner.status('test')
    assert status['mode']=='LIVE' and status['is_simulated'] is False
    assert status['positions']['BTC_USDT']['stop']==98
    assert status['positions']['BTC_USDT']['unrealized']==3
    assert status['positions']['BTC_USDT']['analysis_version']=='vlm'
    assert status['closed']==[]
    assert runner.store.get('live_events')[0]['net_pnl'] is None


def test_parallel_analysis_different_symbols_canonical_same_real_account(tmp_path,monkeypatch):
    runner,_,_,control=prepare(tmp_path)
    barrier=Barrier(2)
    children=[]
    class Child:
        can_call=None
        def close(self):self.closed=True
    def fork():
        child=Child();children.append(child);return child
    runner.llm.fork=fork
    class Evaluator:
        def __init__(self,llm,store,public,cfg):pass
        def evaluate_candidates(self,candidates,**kwargs):
            barrier.wait(3)
            assert kwargs['live_account_context']['mode']=='LIVE'
            assert kwargs['live_account_context']['equity']==500.
            assert kwargs['equity']==200.
            assert kwargs['live_positions']=={}
            return EvaluationResult(action=GraphAction.HOLD,hold_reason='hold',metadata={'provider_called':True})
    monkeypatch.setattr('trading.runtime.live_runner.ContextEvaluator',Evaluator)
    runner.candidate_queue().refresh([candidate('BTC_USDT'),candidate('ETH_USDT')])
    assert runner.cycle()['reason']=='HOLD_LIVE_MODEL'
    assert len(children)==2 and all(child.closed for child in children)
    assert runner.store.get('paper') is None


def test_context_reports_actual_closed_outcomes_and_persisted_live_objective(tmp_path):
    runner,private,executor,control=prepare(tmp_path)
    runner.cfg.research_goal_enabled=True
    snapshot=runner._refresh()
    first=runner._live_context(snapshot,control)
    assert first['initial_usdt']==500.
    assert first['objective']['target_usdt']==7000.
    executor.rows=[dict(intent_id='closed',status='closed',closed_at=123000,net_pnl=7.,realized_complete=True,
        total_fee=.2,funding_signed=-.1,close_reason='TP',proposal={'symbol':'BTC_USDT','side':'long'})]
    private.equity=507.
    after=runner._live_context(runner._refresh(),control)
    assert after['initial_usdt']==500.
    assert after['equity_snapshot_usdt']==507.
    assert after['closed_trade_count']==1
    assert after['close_reason_counts']['TP']==1
    assert after['realized_net_total_usdt']==7.
    assert after['fees_total_usdt']==.2
    assert after['funding_total_usdt']==-.1
    assert after['objective']['deadline']==first['objective']['deadline']
    assert after['objective']['must_not_increase_risk'] is True


def test_live_leverage_cap_uses_live_policy_not_paper_three_x_default(tmp_path):
    runner,_,_,control=prepare(tmp_path)
    runner.cfg.paper_max_leverage=3
    runner.cfg.risk_profile=RiskProfile.AGGRESSIVE_GROWTH
    control.update(risk_profile='aggressive_growth',max_leverage=10)
    runner.configure(control)
    assert runner._leverage_cap(control)==10


def test_managed_actual_position_card_contains_stop_target_risk_and_decision(tmp_path):
    runner,private,executor,_=prepare(tmp_path)
    private.positions=[dict(positionId=45,symbol='BTC_USDT',positionType=1,openType=1,state=1,
        holdVol=2,leverage=3,holdAvgPrice=100,im=10,unRealizedPnl=3,liquidatePrice=70)]
    executor.rows=[dict(position_id='45',status='protected',proposal=dict(notional=200,max_loss_if_stopped=2,
        stop_loss={'price':98},take_profit_price=106,metadata={'strategy':'trend','decision_ai':{'provider':'mock'}}))]
    card=runner._positions(runner._refresh())['BTC_USDT']
    assert (card['stop'],card['target'],card['risk'],card['notional'],card['margin'])==(98,106,2,200,10)
    assert card['decision_ai']=={'provider':'mock'}


def test_actual_automatic_executor_chain_with_mock_exchange_no_paper(tmp_path):
    from trading.brokers.live.automatic import AutomaticLiveExecutor,AutomaticLivePolicy
    from trading.brokers.live.execution import LiveExecutionGates
    runner,private,_,control=prepare(tmp_path)
    private.writes_enabled=True
    private.open_positions=lambda symbol=None:deepcopy(private.positions)
    private.stops=[];private.submitted=[]
    def submit(**kwargs):
        private.submitted.append(kwargs)
        private.order_row=dict(symbol=kwargs['symbol'],externalOid=kwargs['external_id'],positionId=45,orderId=123,
            side=1,openType=1,positionMode=1,vol=kwargs['contracts'],dealVol=kwargs['contracts'],dealAvgPrice=100.,state=3)
        private.positions=[dict(positionId=45,symbol='BTC_USDT',positionType=1,openType=1,state=1,autoAddIm=False,
            holdVol=kwargs['contracts'],leverage=kwargs['leverage'],holdAvgPrice=100,im=10,unRealizedPnl=0,liquidatePrice=70)]
        return {'orderId':123}
    def protect(**kwargs):
        private.stops=[dict(positionId=45,state=1,isFinished=0,lossTrend=2,profitTrend=2,stopLossType=0,takeProfitType=0,
            stopLossReverse=2,takeProfitReverse=2,profitLossVolType='SAME',vol=kwargs['contracts'],
            stopLossPrice=kwargs['stop_loss'],takeProfitPrice=kwargs['take_profit'])]
        return {'id':456}
    private.submit_market=submit
    private.order_by_external_id=lambda symbol,external_id:deepcopy(private.order_row)
    private.place_protection=protect
    private.protection_orders=lambda *a,**kw:deepcopy(private.stops)
    executor=AutomaticLiveExecutor(AutomaticLivePolicy(enabled=True,allowed_accounts=('ahmet',),operator_ids=('7',)),
        LiveExecutionGates(live_trading_enabled=True,broker_mode='live',reviewed=True,operator_ids=('7',)),
        runner.store,private,runner.public,runner.risk,account_id='ahmet',binding_id='binding1',control_provider=lambda:dict(control))
    runner.executor=executor
    executor.grant_start('7',1)
    signal=candidate();runner.candidate_queue().refresh([signal])
    decision=result(runner,signal,dict(leverage=2,risk_per_trade_pct=.005))
    assert runner._apply(signal,decision,control)['reason']=='LIVE_PROTECTED'
    assert len(private.submitted)==1
    assert executor.intents()[0]['status']=='protected'
    assert executor.intents()[0]['authorization_type']=='automatic_start_grant'
    assert runner.store.get('paper') is None
    assert private.changed[0]['leverage']==2


def test_latched_live_halt_blocks_model_and_all_entry_related_writes(tmp_path):
    runner,private,executor,control=prepare(tmp_path)
    signal=candidate();runner.candidate_queue().refresh([signal])
    decision=result(runner,signal,dict(leverage=2,risk_per_trade_pct=.005))
    runner.store.put('live:halt',{'reason':'unknown write'})
    assert runner.cycle()['reason']=='HOLD_LIVE_HALT'
    assert runner._apply(signal,decision,control)['reason']=='HOLD_LIVE_HALT'
    assert not executor.calls and not private.changed
    assert runner.monitor()['reason']=='LIVE_RECONCILED'


def test_stub_trade_cannot_reach_live_executor(tmp_path):
    runner,_,executor,control=prepare(tmp_path)
    signal=candidate();runner.candidate_queue().refresh([signal])
    decision=result(runner,signal)
    decision.market.mode='stub'
    assert runner._apply(signal,decision,control)['reason']=='HOLD_LIVE_UNVALIDATED_MODEL'
    assert not executor.calls


def test_dynamic_zero_capacity_blocks_provider_work_without_consuming_candidate(tmp_path):
    runner,private,executor,control=prepare(tmp_path)
    calls=[]
    runner.llm.analysis_capacity=lambda **kw:dict(parallel_candidates=0,reason='quota_cooldown_or_in_flight')
    runner.candidate_queue().refresh([candidate()])
    runner._evaluate=lambda *a,**kw:calls.append('provider')
    result=runner.cycle()
    assert result['reason']=='HOLD_LIVE_AI_CAPACITY' and not calls
    assert runner.store.get('candidate_queue')[0]['last_considered_at']==0
    assert runner.monitor()['entry_status']['reason']=='HOLD_LIVE_AI_CAPACITY'


def test_runner3_small_account_sizes_immediately_without_same_side_exposure(tmp_path):
    runner, private, _, control = prepare(tmp_path)
    runner.cfg.live_price_plan_enabled = True
    control['runner_id'] = 'runner3'
    signal = candidate()
    decision = result(runner, signal, dict(leverage=5, risk_per_trade_pct=.006))
    stamp = int(time()) - 1800
    stop_reference = dict(timeframe='15m', open_time=stamp, field='low')
    decision.metadata.update(
        setup_selection=dict(setup_id='setup1', family='trend_continuation', horizon='intraday',
                             macro_trend='bullish', side='long', reward_risk=2.,
                             state='ready', context_timeframe='4h', trigger_timeframe='15m',
                             evidence={'kind': 'htf_zone_mss'}, stop_reference=stop_reference, stop_loss_price=98.,
                             target_reference=dict(timeframe='4h', open_time=stamp-14400, field='high')),
        price_plan=dict(entry_type='market', entry_price=100., stop_loss_price=98., take_profit_price=106.,
                        entry_timeframe='15m', stop_reference=stop_reference,
                        target_reference=dict(timeframe='4h', open_time=stamp-14400, field='high'),
                        reason='Closed wick and structural target.'),
        price_evidence={'15m': [[stamp, 100, 106, 98, 101, 1]],
                        '4h': [[stamp-14400, 100, 106, 98, 101, 1]]},
    )
    private.equity, private.available = 50., 50.
    snapshot = runner._refresh([signal.symbol])
    boosted = runner._final_proposal(signal, decision, snapshot, control)
    assert boosted.metadata['runner3_allocation']['risk_boost_applied']
    assert boosted.metadata['sizing_plan']['risk_per_trade_pct'] == .006
    assert boosted.max_loss_if_stopped == pytest.approx(.5)
    snapshot['positions'] = [{'side': 1}]
    baseline = runner._final_proposal(signal, decision, snapshot, control)
    assert not baseline.metadata['runner3_allocation']['risk_boost_applied']
    assert baseline.max_loss_if_stopped == pytest.approx(.3)
    assert baseline.notional < boosted.notional
    assert baseline.stop_loss == boosted.stop_loss
