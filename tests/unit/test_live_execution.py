from copy import deepcopy
from types import SimpleNamespace
from time import time

import pytest

from trading.brokers.live.execution import (
    ApprovedLiveExecutor, LiveExecutionError, LiveExecutionGates, entry_stop_loss, worst_entry_price)
from trading.config import Settings
from trading.exchange.contracts import ContractRules
from trading.exchange.mexc_private import MexcPrivateError
from trading.models.domain import TradeProposal,Side,StopLossSpec
from trading.risk.engine import RiskEngine
from trading.runtime.store import RuntimeStore
from .test_live_account import Client


class Exchange(Client):
    writes_enabled=True
    def __init__(self):
        super().__init__()
        self.submits=[];self.closes=[];self.protection_writes=[];self.market_orders=[];self.cancels=[]
        self.order_row=None;self.protections=[];self.timeout=False
        self.native=True;self.protection_fail=False
        self.fill_price=None;self.unfilled=False;self.partial_fill=False;self.resting=False
        self.cancel_leaves_open=False
    def open_positions(self,symbol=None):
        return deepcopy(self.positions)
    def submit_market(self,**kw):
        self.market_orders.append(kw)
        self.submits.append(kw)
        self.order_row=dict(orderId=9001,externalOid=kw['external_id'],symbol=kw['symbol'],side=1,openType=1,
            positionMode=1,vol=kw['contracts'],dealVol=kw['contracts'],dealAvgPrice=100,state=3,positionId=42)
        self.positions=[dict(positionId=42,symbol=kw['symbol'],positionType=1,holdVol=kw['contracts'],
            state=1,openType=1,autoAddIm=0)]
        if self.native:self.protections=[self.pair(kw)]
        if self.timeout:raise MexcPrivateError('submit',ambiguous=True)
        return {'orderId':'9001'}
    def submit_ioc(self,**kw):
        self.submits.append(kw)
        filled=0 if self.unfilled else (kw['contracts']/2 if self.partial_fill else kw['contracts'])
        state=2 if self.resting else (4 if self.unfilled or self.partial_fill else 3)
        price=kw['price'] if self.fill_price is None else self.fill_price
        side=1 if kw['side']=='long' else 3
        self.order_row=dict(orderId=9001,externalOid=kw['external_id'],symbol=kw['symbol'],side=side,openType=1,
            positionMode=kw.get('position_mode',1),vol=kw['contracts'],dealVol=filled,dealAvgPrice=price,
            price=kw['price'],state=state,positionId=42)
        if filled>0:
            self.positions=[dict(positionId=42,symbol=kw['symbol'],positionType=1 if kw['side']=='long' else 2,
                holdVol=filled,state=1,openType=1,autoAddIm=0)]
            self.protections=[self.pair(kw)] if self.native and not self.partial_fill else []
        else:
            self.positions=[];self.protections=[]
        if self.timeout:raise MexcPrivateError('submit',ambiguous=True)
        return {'orderId':'9001'}
    def cancel_order(self,order_id):
        self.cancels.append(order_id)
        if self.order_row and self.order_row.get('state') in (1,2) and not self.cancel_leaves_open:
            self.order_row['state']=4
        return []
    @staticmethod
    def pair(kw):
        return dict(positionId=42,state=1,isFinished=0,lossTrend=2,profitTrend=2,
            stopLossType=0,takeProfitType=0,stopLossReverse=2,takeProfitReverse=2,
            profitLossVolType='SAME',vol=kw['contracts'],stopLossPrice=kw['stop_loss'],takeProfitPrice=kw['take_profit'])
    def order_by_external_id(self,symbol,external_id):
        if external_id.startswith('close-'):
            vol=self.closes[-1]['contracts'] if self.closes else 1
            return dict(externalOid=external_id,symbol=symbol,side=4,state=3,dealVol=vol)
        return deepcopy(self.order_row)
    def protection_orders(self,symbol=None,page_num=1,page_size=100):return deepcopy(self.protections)
    def place_protection(self,**kw):
        self.protection_writes.append(kw)
        if self.protection_fail:raise MexcPrivateError('protection',ambiguous=True)
        self.protections=[self.pair(kw)]
    def close_position(self,**kw):
        self.closes.append(kw);self.positions=[]
        return {'orderId':'c1'}


def setup(tmp_path,**overrides):
    private=Exchange()
    public=SimpleNamespace(get_contract_rules=lambda symbol:ContractRules(1,1,1,1000,10,.005,.01,1000),
        get_ticker=lambda symbol:SimpleNamespace(bid1=100.,ask1=100.,raw={'timestamp':time()*1000}))
    cfg=Settings(_env_file=None,risk_per_trade_pct=.03)
    gates=LiveExecutionGates(True,'live',('11',),True)
    executor=ApprovedLiveExecutor(gates,RuntimeStore(str(tmp_path/'live')),private,public,RiskEngine(settings=cfg),
        account_id='ahmet',binding_id='connection1',**overrides)
    proposal=TradeProposal(symbol='BTC_USDT',side=Side.LONG,leverage=5,notional=100,entry_price=100,
        stop_loss=StopLossSpec(price=98),take_profit_price=104,max_loss_if_stopped=3,
        expected_edge=.8,fee_free_pair=False,rationale='Test exact operator proposal')
    return executor,private,proposal


def approved(executor,proposal):
    row=executor.stage(proposal)
    return executor.approve(row['intent_id'],row['proposal_fingerprint'],'11',allow_emergency_close=True)


def test_stage_never_calls_exchange_and_exact_human_approval_is_required(tmp_path):
    executor,private,proposal=setup(tmp_path)
    row=executor.stage(proposal)
    assert private.calls==[] and private.submits==[]
    with pytest.raises(LiveExecutionError,match='manual_approval'):executor.execute(row['intent_id'])
    with pytest.raises(LiveExecutionError,match='operator_not_allowed'):
        executor.approve(row['intent_id'],row['proposal_fingerprint'],'22',allow_emergency_close=True)
    with pytest.raises(LiveExecutionError,match='mismatch'):
        executor.approve(row['intent_id'],'changed','11',allow_emergency_close=True)


def test_confirmed_native_protection_and_repeat_execute_never_resubmits(tmp_path):
    executor,private,proposal=setup(tmp_path)
    row=approved(executor,proposal)
    result=executor.execute(row['intent_id'])
    assert result['status']=='protected'
    assert len(private.submits)==1 and not private.protection_writes and not private.closes
    assert executor.execute(row['intent_id'])['status']=='protected'
    assert len(private.submits)==1


def test_ambiguous_entry_is_reconciled_by_original_id_without_second_write(tmp_path):
    executor,private,proposal=setup(tmp_path)
    row=approved(executor,proposal);private.timeout=True
    result=executor.execute(row['intent_id'])
    assert result['entry_write_attempted'] and result['halt_reason']=='entry_write_outcome_unknown'
    assert executor.reconcile(row['intent_id'])['status']=='protected'
    assert len(private.submits)==1
    assert executor.store.get('live:halt')


def test_failed_native_protection_closes_once_only_with_prior_operator_permission(tmp_path):
    executor,private,proposal=setup(tmp_path)
    row=approved(executor,proposal);private.native=False;private.protection_fail=True
    assert executor.execute(row['intent_id'])['status']=='emergency_close_unknown'
    assert len(private.protection_writes)==1 and len(private.closes)==1
    assert executor.reconcile(row['intent_id'])['status']=='closed'
    assert len(private.protection_writes)==1 and len(private.closes)==1


def test_live_account_leverage_mismatch_rejects_before_submit(tmp_path):
    executor,private,proposal=setup(tmp_path)
    proposal.leverage=3
    row=approved(executor,proposal)
    with pytest.raises(LiveExecutionError,match='private_preflight'):executor.execute(row['intent_id'])
    assert not private.submits


def test_binding_change_and_pause_block_writes(tmp_path):
    executor,private,proposal=setup(tmp_path)
    row=approved(executor,proposal)
    executor.binding_id='new-key'
    with pytest.raises(LiveExecutionError,match='binding_changed'):executor.execute(row['intent_id'])
    executor.binding_id='connection1';executor.can_execute=lambda:False
    with pytest.raises(LiveExecutionError,match='paused'):executor.execute(row['intent_id'])
    assert not private.submits


@pytest.mark.parametrize('patch',[
    {'stopLossReverse':1},{'profitLossVolType':'SEPARATE','stopLossVol':.5,'takeProfitVol':1},
    {'profitLossVolType':None},{'lossTrend':1},{'takeProfitPrice':105},
])
def test_unsafe_or_wrong_native_protection_is_not_reported_protected(tmp_path,patch):
    executor,private,proposal=setup(tmp_path)
    row=approved(executor,proposal)
    private.submit_market(symbol='BTC_USDT',side='long',contracts=1,leverage=5,external_id=row['external_id'],stop_loss=98,take_profit=104,position_mode=1)
    row.update(contracts=1,position_mode=1,entry_write_attempted=True,quantity=1,
        worst_allowed_price=101,planned_loss=3,equity=500)
    executor._save(row)
    private.protections[0].update(patch)
    private.protection_fail=True
    result=executor.reconcile(row['intent_id'])
    assert result['status']!='protected' and len(private.closes)==1


def test_partial_fill_is_never_reported_complete_or_retried(tmp_path):
    executor,private,proposal=setup(tmp_path)
    row=approved(executor,proposal)
    private.submit_market(symbol='BTC_USDT',side='long',contracts=1,leverage=5,external_id=row['external_id'],stop_loss=98,take_profit=104,position_mode=1)
    private.order_row['dealVol']=.5
    row.update(contracts=1,position_mode=1,entry_write_attempted=True,quantity=1,
        worst_allowed_price=101,planned_loss=3,equity=500)
    executor._save(row)
    result=executor.reconcile(row['intent_id'])
    assert result['halt_reason']=='entry_fill_not_fully_confirmed'
    assert len(private.submits)==1


def test_changed_balance_before_entry_is_used_instead_of_old_seed(tmp_path):
    executor,private,proposal=setup(tmp_path)
    row=approved(executor,proposal)
    private.assets[0].update(equity='11.5854',availableBalance='11.5854',positionMargin='0',unrealized='0',cashBalance='11.5854')
    with pytest.raises(LiveExecutionError):executor.execute(row['intent_id'])
    assert not private.submits
    assert executor.reconciler.snapshot()['balance']['equity']==11.5854


def test_expired_exact_approval_never_sends_entry(tmp_path):
    executor,private,proposal=setup(tmp_path)
    row=approved(executor,proposal);row['expires_at']=time()-1;executor._save(row)
    with pytest.raises(LiveExecutionError,match='manual_approval'):executor.execute(row['intent_id'])
    assert not private.submits


def test_shared_execution_lease_rejects_second_executor_without_http(tmp_path):
    executor,private,proposal=setup(tmp_path)
    row=approved(executor,proposal)
    token=executor._acquire_lease(row['intent_id'])
    try:
        with pytest.raises(LiveExecutionError,match='busy'):executor.execute(row['intent_id'])
        assert not private.calls and not private.submits
    finally:
        executor._release_lease(token)


def test_network_operations_do_not_hold_store_lock(tmp_path):
    import threading
    executor,private,proposal=setup(tmp_path)
    row=approved(executor,proposal)
    original=private.account_assets
    def checked():
        acquired=[]
        def check():
            with executor.store.lock:acquired.append(True)
        worker=threading.Thread(target=check);worker.start();worker.join(timeout=1)
        assert acquired, 'HTTP request holds storage lock'
        return original()
    private.account_assets=checked
    assert executor.execute(row['intent_id'])['status']=='protected'


def hot_setup(tmp_path,**flags):
    executor,private,_btc=setup(tmp_path)
    for key,value in flags.items():
        setattr(private,key,value)
    private.assets[0].update(equity='11.1378304',availableBalance='11.1378304',positionMargin='0',
        unrealized='0',cashBalance='11.1378304',frozenBalance='0')
    def leverage(symbol):
        private.calls.append(('leverage',symbol))
        return [dict(positionType=side,openType=1,leverage=10,maxVol=100000,currentMmr=.005,maxLeverageView=20) for side in (1,2)]
    def risk_limits(symbol=None):
        private.calls.append(('risk',symbol))
        return {symbol:[dict(symbol=symbol,positionType=side,level=1,maxVol=100000,maxLeverage=20,mmr=.005,imr=.1) for side in (1,2)]}
    private.leverage=leverage
    private.risk_limits=risk_limits
    executor.public=SimpleNamespace(
        get_contract_rules=lambda symbol:ContractRules(1,1,1,100000,20,.005,.0000001,100000),
        get_ticker=lambda symbol:SimpleNamespace(bid1=0.0004435,ask1=0.0004436,raw={'timestamp':time()*1000}))
    proposal=TradeProposal(symbol='HOT_USDT',side=Side.LONG,leverage=10,notional=8.2540652,entry_price=0.0004417,
        stop_loss=StopLossSpec(price=0.0004266),take_profit_price=0.00048,max_loss_if_stopped=0.334134912,
        expected_edge=.8,fee_free_pair=False,rationale='HOT entry must stay inside the approved stop')
    return executor,private,proposal


def test_hot_stop_budget_rejects_the_unbounded_fill_price():
    worst=worst_entry_price('long',0.0004266,18607,0.334134912,0.0006,0.0005,0.0000001)
    bad=entry_stop_loss('long',0.0004494,0.0004266,18607,0.0006,0.0005)
    capped=entry_stop_loss('long',worst,0.0004266,18607,0.0006,0.0005)
    planned=entry_stop_loss('long',0.0004436*(1+0.0005),0.0004266,18607,0.0006,0.0005)
    better=entry_stop_loss('long',0.0004436,0.0004266,18607,0.0006,0.0005)
    assert worst==0.0004438
    assert worst<0.0004494
    assert planned==0.3341300874356986
    assert bad==pytest.approx(0.437985930976139)
    assert bad>0.334134912
    assert bad/11.1378304==pytest.approx(0.03932416954)
    assert capped<=0.334134912+1e-8
    assert better<=capped


@pytest.mark.parametrize('fill', [None, 0.0004436])
def test_hot_fill_at_or_inside_the_worst_price_is_protected(tmp_path,fill):
    worst=worst_entry_price('long',0.0004266,18607,0.334134912,0.0006,0.0005,0.0000001)
    executor,private,proposal=hot_setup(tmp_path,fill_price=fill)
    row=approved(executor,proposal)
    opened=executor.execute(row['intent_id'])
    assert private.market_orders==[]
    assert private.submits[0]['price']==worst
    assert private.submits[0]['price']<0.0004494
    assert opened['status']=='protected'
    assert opened['fill_price']==(worst if fill is None else fill)
    assert opened['fill_price']<=worst
    assert opened['computed_loss']<=proposal.max_loss_if_stopped+1e-8
    assert opened['planned_loss']==0.3341300874356986
    assert opened['equity']==11.1378304
    assert not executor.store.get('live:halt')
    assert not private.closes


def test_hot_market_fill_is_rejected_and_the_review_halt_stays(tmp_path):
    worst=worst_entry_price('long',0.0004266,18607,0.334134912,0.0006,0.0005,0.0000001)
    executor,private,proposal=hot_setup(tmp_path,fill_price=0.0004494)
    row=approved(executor,proposal)
    rejected=executor.execute(row['intent_id'])
    assert private.market_orders==[]
    assert len(private.submits)==1
    assert private.submits[0]['price']==worst
    assert rejected['status']=='emergency_close_unknown'
    assert rejected['halt_reason']=='filled_position_risk_exceeds_plan'
    assert rejected['fill_price']==0.0004494
    assert rejected['worst_allowed_price']==worst
    assert rejected['planned_loss']==0.3341300874356986
    assert rejected['computed_loss']==entry_stop_loss('long',0.0004494,0.0004266,18607,0.0006,0.0005)
    assert rejected['equity']==11.1378304
    assert private.closes[0]['contracts']==18607
    halt=executor.store.get('live:halt')
    assert halt['reason']=='emergency_close_requires_review' and halt['requires_review'] is True
    assert halt['fill_price']==0.0004494 and halt['worst_allowed_price']==worst
    assert halt['planned_loss']==0.3341300874356986
    assert halt['computed_loss']==rejected['computed_loss'] and halt['equity']==11.1378304
    closed=executor.reconcile(row['intent_id'])
    assert closed['status']=='closed' and closed['close_reason']=='emergency_close'
    assert closed['halt_reason']=='filled_position_risk_exceeds_plan'
    assert closed['fill_price']==0.0004494 and closed['computed_loss']==rejected['computed_loss']
    assert executor.store.get('live:halt')['requires_review'] is True
    assert len(private.closes)==1 and len(private.submits)==1
    fresh=proposal.model_copy(update={'proposal_id':'second-hot-entry'})
    staged=executor.stage(fresh)
    executor.approve(staged['intent_id'],staged['proposal_fingerprint'],'11',allow_emergency_close=True)
    with pytest.raises(LiveExecutionError,match='live_halt_latched'):
        executor.execute(staged['intent_id'])
    assert len(private.submits)==1 and len(private.closes)==1


def test_risk_engine_still_rejects_before_any_entry_order(tmp_path):
    executor,private,proposal=setup(tmp_path)
    proposal.max_loss_if_stopped=10000
    row=approved(executor,proposal)
    with pytest.raises(LiveExecutionError,match='deterministic_risk_rejected'):
        executor.execute(row['intent_id'])
    assert private.submits==[] and private.market_orders==[]


def test_unfilled_ioc_is_not_a_position(tmp_path):
    executor,private,proposal=setup(tmp_path)
    private.unfilled=True
    row=approved(executor,proposal)
    result=executor.execute(row['intent_id'])
    assert result['status']=='entry_rejected'
    assert result.get('position_id') is None and result.get('halt_reason') is None
    assert not executor.store.get('live:halt')
    assert private.positions==[] and private.closes==[] and private.market_orders==[]
    assert len(private.submits)==1


def test_resting_unfilled_entry_is_cancelled_once(tmp_path):
    executor,private,proposal=setup(tmp_path)
    private.unfilled=True
    private.resting=True
    row=approved(executor,proposal)
    result=executor.execute(row['intent_id'])
    assert private.cancels==[9001]
    assert result['status']=='entry_rejected' and result.get('position_id') is None
    assert not executor.store.get('live:halt') and private.closes==[]
    assert len(private.submits)==1


def test_open_partial_is_closed_at_the_filled_size_only(tmp_path):
    executor,private,proposal=setup(tmp_path)
    private.partial_fill=True
    private.resting=True
    private.cancel_leaves_open=True
    row=approved(executor,proposal)
    result=executor.execute(row['intent_id'])
    assert private.cancels==[9001]
    assert result['status']=='emergency_close_unknown'
    assert result['status']!='protected'
    assert private.closes[0]['contracts']==.5
    assert len(private.submits)==1
    assert result.get('position_id')
    confirmed=executor.reconcile(row['intent_id'])
    assert confirmed['status']=='closed'
    assert len(private.closes)==1 and len(private.submits)==1


def test_partial_ioc_protects_only_the_filled_size(tmp_path):
    executor,private,proposal=setup(tmp_path)
    private.partial_fill=True
    row=approved(executor,proposal)
    result=executor.execute(row['intent_id'])
    assert result['status']=='partial_protected'
    assert result['filled_contracts']==.5
    assert len(private.submits)==1 and private.closes==[]
    assert not executor.store.get('live:halt')
    assert private.protection_writes[0]['contracts']==.5


def test_short_cap_rounds_toward_the_safer_price():
    worst=worst_entry_price('short',102,1,3,0.0006,0.0005,0.01)
    assert entry_stop_loss('short',worst,102,1,0.0006,0.0005)<=3+1e-8
    assert entry_stop_loss('short',worst-0.01,102,1,0.0006,0.0005)>entry_stop_loss('short',worst,102,1,0.0006,0.0005)
