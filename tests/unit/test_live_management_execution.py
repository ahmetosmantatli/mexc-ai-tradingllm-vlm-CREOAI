"""Fault-injected position management; every exchange is an in-memory fake."""
from copy import deepcopy
from time import time
from types import SimpleNamespace
import pytest

from trading.brokers.live.execution import LiveExecutionError
from .test_live_execution import setup,approve,Client,Public


class ManagementClient(Client):
    def __init__(self):
        super().__init__();self.management_orders={};self.modifies=[]
        self.modify_unknown=False;self.modify_apply=True;self.reduce_unknown=False
        self.resize_unknown=False;self.resize_apply=True;self.reduce_fraction=1.
    def place_protection(self,**kw):
        if self.protects and not self.resize_apply:
            self.protects.append(kw['request_id']);raise TimeoutError()
        result=super().place_protection(**kw)
        self.stops[0]['id']=33
        if self.resize_unknown:raise TimeoutError()
        return result
    def modify_protection(self,**kw):
        self.modifies.append(kw)
        if self.modify_apply:
            assert kw['protection_id']==33
            self.stops[0].update(stopLossPrice=kw['stop_loss'],takeProfitPrice=kw['take_profit'])
        if self.modify_unknown:raise TimeoutError()
        return {}
    def order_by_external_id(self,symbol,external_id):
        if external_id.startswith('mg-'):return deepcopy(self.management_orders.get(external_id))
        return super().order_by_external_id(symbol,external_id)
    def close_position(self,**kw):
        if not kw['external_id'].startswith('mg-'):
            result=super().close_position(**kw)
            self.make_history(self.order_row['dealVol'])
            return result
        self.closes.append(kw['external_id'])
        filled=kw['contracts']*self.reduce_fraction
        self.management_orders[kw['external_id']]=dict(symbol=kw['symbol'],externalOid=kw['external_id'],
            side=4 if kw['side']=='long' else 2,state=3 if self.reduce_fraction==1 else 4,dealVol=filled)
        self.positions[0]['holdVol']-=filled
        if self.positions[0]['holdVol']==0:
            self.positions=[];self.stops=[];self.make_history(self.order_row['dealVol'])
        if self.reduce_unknown:raise TimeoutError()
        return {}


class ManagementPublic(Public):
    def get_ticker(self,symbol):return SimpleNamespace(bid1=99.99,ask1=100.,last_price=100.,quote_volume_24h=1e6,raw={'timestamp':time()*1000,'fairPrice':100.})


def prepared(tmp_path):
    executor,_,store=setup(tmp_path)
    private=ManagementClient();executor.private=private;executor.public=ManagementPublic()
    row=approve(executor);row=executor.execute(row['intent_id'])
    assert row['status']=='protected'
    return executor,private,store,row


def decision(action='move_stop',**kw):
    return {'action':action,'reason':'Yeni kapanmış mumda destek yapısı teyit edildi.',
        'ai':{'provider':'test','model':'fake'},**({'stop_price':99.} if action=='move_stop' else {'close_fraction':.5 if action=='reduce' else 1.}),**kw}


def manage(executor,row,data=None,ident='d1',guard=lambda:True):
    return executor.manage_position(row['intent_id'],data or decision(),expected_revision=row.get('management_revision',0),decision_id=ident,guard=guard)


def test_stop_modified_in_place_and_reconciles_after_restart_without_changing_original_plan(tmp_path):
    executor,private,store,row=prepared(tmp_path)
    changed=manage(executor,row)
    assert changed['status']=='protected' and changed['current_protection']['stop']==99
    assert changed['proposal']['stop_loss']['price']==98 # Immutable initial risk/thesis.
    assert len(private.modifies)==1 and len(private.protects)==1 and not private.cancels
    executor.can_execute=lambda:False # Monitoring still reconciles while stopped.
    assert executor.reconcile(row['intent_id'])['status']=='protected'
    assert not store.get('live:halt')


@pytest.mark.parametrize('new_stop',[97.,98.,100.,101.,99.005])
def test_invalid_stop_cannot_widen_trigger_immediately_or_violate_tick(tmp_path,new_stop):
    executor,private,store,row=prepared(tmp_path)
    with pytest.raises(LiveExecutionError):manage(executor,row,decision(stop_price=new_stop))
    assert not private.modifies


def test_timeout_after_applied_stop_is_confirmed_not_sent_twice(tmp_path):
    executor,private,_,row=prepared(tmp_path);private.modify_unknown=True
    changed=manage(executor,row)
    assert changed['management_events'][0]['status']=='confirmed'
    assert executor.reconcile(row['intent_id'])['status']=='protected'
    assert len(private.modifies)==1


def test_unknown_stop_is_persisted_and_reconciled_without_duplicate_post(tmp_path):
    executor,private,store,row=prepared(tmp_path)
    private.modify_unknown=True;private.modify_apply=False
    changed=manage(executor,row)
    assert changed['status']=='management_pending' and store.get('live:halt')
    for _ in range(3):executor.reconcile(row['intent_id'])
    assert len(private.modifies)==1
    with pytest.raises(LiveExecutionError):manage(executor,changed,ident='d2')
    private.stops[0]['stopLossPrice']=99.
    assert executor.reconcile(row['intent_id'])['status']=='protected'
    assert not store.get('live:halt')


def test_partial_fill_resizes_native_pair_then_native_exit_has_cumulative_history(tmp_path):
    executor,private,store,row=prepared(tmp_path)
    changed=manage(executor,row,decision('reduce'))
    assert changed['managed_contracts']==500
    assert changed['effective_contracts']==1000 and private.stops[0]['vol']==500
    assert changed['management_events'][0]['remaining_contracts']==500
    assert executor.reconcile(row['intent_id'])['status']=='protected'
    private.positions=[];private.stops=[];private.make_history(1000)
    closed=executor.reconcile(row['intent_id'])
    assert closed['status']=='closed' and closed['net_pnl']==4.3 and closed['realized_complete']
    assert not private.cancels


def test_partial_timeout_after_fill_does_not_duplicate_exit(tmp_path):
    executor,private,_,row=prepared(tmp_path);private.reduce_unknown=True
    changed=manage(executor,row,decision('reduce'))
    assert changed['status']=='protected' and changed['managed_contracts']==500
    executor.reconcile(row['intent_id'])
    assert len(private.closes)==1


def test_actual_partial_exit_fill_not_requested_amount_is_used(tmp_path):
    executor,private,_,row=prepared(tmp_path);private.reduce_fraction=.5
    changed=manage(executor,row,decision('reduce'))
    assert changed['managed_contracts']==750 and private.stops[0]['vol']==750
    assert changed['management_events'][0]['filled_contracts']==250


def test_resize_ambiguity_keeps_journal_and_does_not_cancel_or_repeat(tmp_path):
    executor,private,store,row=prepared(tmp_path);private.resize_apply=False
    changed=manage(executor,row,decision('reduce'))
    assert changed['status']=='management_pending' and changed['managed_contracts']==500
    assert store.get('live:halt')
    for _ in range(3):executor.reconcile(row['intent_id'])
    assert len(private.closes)==1 and len(private.protects)==2 and not private.cancels
    private.stops[0]['vol']=500
    assert executor.reconcile(row['intent_id'])['status']=='protected'


def test_full_ai_exit_and_manual_close_after_partial_both_reconcile(tmp_path):
    executor,private,_,row=prepared(tmp_path)
    changed=manage(executor,row,decision('reduce'))
    changed=executor.request_close(row['intent_id'])
    assert changed['close_contracts']==500
    assert executor.reconcile(row['intent_id'])['status']=='closed'


def test_full_ai_exit_is_not_an_acknowledgement_only(tmp_path):
    executor,private,_,row=prepared(tmp_path)
    changed=manage(executor,row,decision('close'))
    assert changed['status']=='closed' and changed['realized_complete']
    assert changed['management_events'][0]['remaining_contracts']==0


def test_revision_permission_and_lease_races_cannot_write(tmp_path):
    executor,private,_,row=prepared(tmp_path)
    with pytest.raises(LiveExecutionError):manage(executor,row,guard=lambda:False)
    changed=manage(executor,row)
    with pytest.raises(LiveExecutionError):manage(executor,row,ident='old')
    token=executor._acquire_lease(row['intent_id'])
    with pytest.raises(LiveExecutionError):manage(executor,changed,decision('reduce'),ident='busy')
    executor._release_lease(token)
    assert len(private.modifies)==1 and not private.closes


def test_too_small_partial_exit_and_missing_protection_id_do_not_write(tmp_path):
    executor,private,_,row=prepared(tmp_path)
    with pytest.raises(LiveExecutionError):manage(executor,row,decision('reduce',close_fraction=.0001))
    private.stops[0].pop('id')
    with pytest.raises(LiveExecutionError):manage(executor,row)
    assert not private.closes and not private.modifies


def test_unprotected_confirmed_remainder_emergency_closes_once_after_readback_window(tmp_path):
    executor,private,store,row=prepared(tmp_path);private.resize_apply=False
    changed=manage(executor,row,decision('reduce'))
    changed['management_pending']['at']=time()-31;executor._save(changed)
    changed=executor.reconcile(row['intent_id'])
    assert changed['close_reason']=='EMERGENCY' and changed['close_contracts']==500
    assert changed['management_events'][0]['status']=='protection_failed'
    assert executor.reconcile(row['intent_id'])['status']=='closed'
    assert len(private.closes)==2 # One partial, one separate remainder close, neither retried.


def test_native_exit_racing_unknown_stop_is_historical_close_not_repeat_write(tmp_path):
    executor,private,_,row=prepared(tmp_path)
    private.modify_apply=False;private.modify_unknown=True
    manage(executor,row)
    private.positions=[];private.stops=[];private.make_history(1000)
    changed=executor.reconcile(row['intent_id'])
    assert changed['status']=='closed' and changed['management_events'][0]['status']=='position_closed'
    assert not changed.get('management_pending') and len(private.modifies)==1


def test_short_stop_tightens_downward_and_remains_protected(tmp_path):
    from trading.models.domain import Side
    from .test_live_execution import proposal
    executor,_,_=setup(tmp_path)
    private=ManagementClient();executor.private=private;executor.public=ManagementPublic()
    submit=private.submit_market
    def short_submit(**kw):
        result=submit(**kw);private.positions[0]['liquidatePrice']=120.;return result
    private.submit_market=short_submit
    row=approve(executor,proposal(side=Side.SHORT,stop_loss={'price':102.},take_profit_price=96.))
    row=executor.execute(row['intent_id']);assert row['status']=='protected'
    changed=manage(executor,row,decision(stop_price=101.))
    assert changed['current_protection']['stop']==101.
    assert executor.reconcile(row['intent_id'])['status']=='protected'


def test_profit_side_stop_reduces_open_risk_and_does_not_trigger_false_emergency(tmp_path):
    executor,private,_,row=prepared(tmp_path)
    original=executor.public.get_ticker
    def quote(symbol):
        q=original(symbol);q.bid1=102.;q.ask1=102.01;q.raw['fairPrice']=102.;return q
    executor.public.get_ticker=quote
    changed=manage(executor,row,decision(stop_price=101.))
    assert changed['planned_loss']<row['planned_loss']
    assert executor.reconcile(row['intent_id'])['status']=='protected'
    assert not private.closes
