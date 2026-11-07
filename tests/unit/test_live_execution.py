from copy import deepcopy
from types import SimpleNamespace
from time import time

import pytest

from trading.brokers.live.execution import ApprovedLiveExecutor,LiveExecutionGates,LiveExecutionError
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
        self.submits=[];self.closes=[];self.protection_writes=[]
        self.order_row=None;self.protections=[];self.timeout=False
        self.native=True;self.protection_fail=False
    def open_positions(self,symbol=None):
        return deepcopy(self.positions)
    def submit_market(self,**kw):
        self.submits.append(kw)
        self.order_row=dict(externalOid=kw['external_id'],symbol=kw['symbol'],side=1,openType=1,
            positionMode=1,vol=kw['contracts'],dealVol=kw['contracts'],state=3,positionId=42)
        self.positions=[dict(positionId=42,symbol=kw['symbol'],positionType=1,holdVol=kw['contracts'],
            state=1,openType=1,autoAddIm=0)]
        if self.native:self.protections=[self.pair(kw)]
        if self.timeout:raise MexcPrivateError('submit',ambiguous=True)
        return {'orderId':'o1'}
    @staticmethod
    def pair(kw):
        return dict(positionId=42,state=1,isFinished=0,lossTrend=2,profitTrend=2,
            stopLossType=0,takeProfitType=0,stopLossReverse=2,takeProfitReverse=2,
            profitLossVolType='SAME',vol=kw['contracts'],stopLossPrice=kw['stop_loss'],takeProfitPrice=kw['take_profit'])
    def order_by_external_id(self,symbol,external_id):
        if external_id.startswith('close-'):
            return dict(externalOid=external_id,symbol=symbol,side=4,state=3,dealVol=1)
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
    row.update(contracts=1,position_mode=1,entry_write_attempted=True)
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
    row.update(contracts=1,position_mode=1,entry_write_attempted=True)
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
