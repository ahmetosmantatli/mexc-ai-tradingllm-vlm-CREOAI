from copy import deepcopy

import pytest

from trading.runtime.live_account import LiveAccountReconciler,LiveAccountError
from trading.runtime.store import RuntimeStore


class Client:
    def __init__(self):
        self.assets=[dict(currency='USDT',equity='500',availableBalance='300',positionMargin='190',unrealized='10',cashBalance='490',frozenBalance='0')]
        self.positions=[]
        self.orders=[]
        self.mode=1
        self.fail=False
        self.calls=[]
    def account_assets(self):
        self.calls.append('assets')
        if self.fail:raise RuntimeError('NEVER_PERSIST_KEY_OR_RAW_RESPONSE')
        return deepcopy(self.assets)
    def open_positions(self):self.calls.append('positions');return deepcopy(self.positions)
    def current_orders(self,**kw):self.calls.append(('orders',kw));return deepcopy(self.orders)
    def position_mode(self):self.calls.append('mode');return self.mode
    def risk_limits(self,symbol=None):
        self.calls.append(('risk',symbol))
        return {symbol:[dict(symbol=symbol,positionType=side,level=1,maxVol=1000,maxLeverage=10,mmr=.005,imr=.1) for side in (1,2)]}
    def leverage(self,symbol):
        self.calls.append(('leverage',symbol))
        return [dict(positionType=side,openType=1,leverage=5,maxVol=1000,currentMmr=.005,maxLeverageView=10) for side in (1,2)]


def setup(tmp_path,account='ahmet',binding='connection-1'):
    clock=[1000.]
    state=RuntimeStore(str(tmp_path/'account'))
    reconciler=LiveAccountReconciler(account,state,binding_id=binding,clock=lambda:clock[0])
    return reconciler,Client(),clock


def preflight(reconciler,**overrides):
    values=dict(symbol='BTC_USDT',side=1,contracts=10,leverage=5,notional=100,position_mode=1)
    values.update(overrides)
    return reconciler.preflight(**values)


def test_account_equity_available_and_pnl_are_distinct_dynamic_exchange_values(tmp_path):
    reconciler,client,clock=setup(tmp_path)
    first=reconciler.refresh(client,symbols=['BTC_USDT'])
    assert first['balance']['equity']==500
    assert first['balance']['available']==300
    assert first['balance']['unrealized']==10
    assert first['balance']['position_margin']==190
    assert preflight(reconciler).ready
    client.assets[0].update(equity='550',availableBalance='350',unrealized='60')
    clock[0]+=1
    second=reconciler.refresh(client,symbols=['BTC_USDT'])
    assert second['balance']['equity']==550
    assert first['generation']!=second['generation']
    assert preflight(reconciler,expected_generation=first['generation']).reasons==('ACCOUNT_GENERATION_CHANGED',)
    assert preflight(reconciler).equity==550


def test_read_failure_revokes_previous_ready_state_without_env_seed(tmp_path):
    reconciler,client,clock=setup(tmp_path)
    reconciler.refresh(client,symbols=['BTC_USDT'])
    client.fail=True
    with pytest.raises(LiveAccountError,match='ACCOUNT_READ_FAILED'):
        reconciler.refresh(client,symbols=['BTC_USDT'])
    assert not preflight(reconciler).ready
    assert 'NEVER_PERSIST' not in str(reconciler.store.get(reconciler.key))


def test_account_credential_binding_and_age_isolation(tmp_path):
    reconciler,client,clock=setup(tmp_path)
    reconciler.refresh(client,symbols=['BTC_USDT'])
    new_key=LiveAccountReconciler('ahmet',reconciler.store,binding_id='connection-2',clock=lambda:clock[0])
    assert preflight(new_key).reasons==('ACCOUNT_BINDING_MISMATCH',)
    other=LiveAccountReconciler('friend',reconciler.store,binding_id='connection-1',clock=lambda:clock[0])
    assert preflight(other).reasons==('ACCOUNT_BINDING_MISMATCH',)
    clock[0]+=31
    assert preflight(reconciler).reasons==('ACCOUNT_SNAPSHOT_STALE',)


@pytest.mark.parametrize('mode',[None,True,1.0,2.0,'1',0,3,{'positionMode':1},[{'positionType':1}]])
def test_unknown_or_ambiguous_position_mode_never_guessed(tmp_path,mode):
    reconciler,client,clock=setup(tmp_path)
    client.mode=mode
    with pytest.raises(LiveAccountError,match='UNKNOWN_POSITION_MODE'):
        reconciler.refresh(client,symbols=['BTC_USDT'])
    assert not reconciler.snapshot()['ready']


@pytest.mark.parametrize('field,value',[('equity','NaN'),('availableBalance',True),('positionMargin','-1'),('unrealized','bad')])
def test_malformed_assets_rejected_without_static_fallback(tmp_path,field,value):
    reconciler,client,clock=setup(tmp_path)
    client.assets[0][field]=value
    with pytest.raises(LiveAccountError):reconciler.refresh(client,symbols=['BTC_USDT'])


def position(symbol='ETH_USDT',identifier=1,**overrides):
    row=dict(positionId=identifier,symbol=symbol,positionType=1,openType=1,state=1,holdVol=1,leverage=5,holdAvgPrice=100,im=20,unRealizedPnl=0,liquidatePrice=80)
    return {**row,**overrides}


def order(identifier='one',**overrides):
    row=dict(orderId=identifier,symbol='ETH_USDT',state=2,side=1,vol=5,dealVol=0,leverage=5,openType=1,positionMode=1,orderMargin=10)
    return {**row,**overrides}


def test_external_positions_and_orders_block_entry_until_reconciled(tmp_path):
    reconciler,client,clock=setup(tmp_path)
    client.positions=[position()]
    client.orders=[order()]
    reconciler.refresh(client,symbols=['BTC_USDT'])
    result=preflight(reconciler)
    assert 'UNRECONCILED_EXTERNAL_POSITIONS' in result.reasons
    assert 'UNRECONCILED_EXTERNAL_ORDERS' in result.reasons
    assert preflight(reconciler,managed_position_ids=['1'],managed_order_ids=['one']).ready
    assert not preflight(reconciler,managed_position_ids=['1'],managed_order_ids=['one'],allowed_capital=40).ready


def test_symbol_already_open_and_unsupported_cross_margin_refuse_entry(tmp_path):
    reconciler,client,clock=setup(tmp_path)
    client.positions=[position(symbol='BTC_USDT',openType=2)]
    reconciler.refresh(client,symbols=['BTC_USDT'])
    result=preflight(reconciler,managed_position_ids=['1'])
    assert 'SYMBOL_ALREADY_OPEN' in result.reasons
    assert 'UNSUPPORTED_POSITION_STATE_OR_MARGIN_MODE' in result.reasons


def test_leverage_risk_volume_and_margin_limits_use_fresh_exchange_values(tmp_path):
    reconciler,client,clock=setup(tmp_path)
    reconciler.refresh(client,symbols=['BTC_USDT'])
    assert 'LEVERAGE_SETTING_MISMATCH' in preflight(reconciler,leverage=3).reasons
    assert 'EXCHANGE_LEVERAGE_CAP' in preflight(reconciler,leverage=20).reasons
    assert 'EXCHANGE_RISK_VOLUME_CAP' in preflight(reconciler,contracts=1001).reasons
    assert 'INSUFFICIENT_FREE_MARGIN' in preflight(reconciler,notional=2000).reasons
    assert 'ALLOCATION_EXCEEDS_CURRENT_EQUITY' in preflight(reconciler,allowed_capital=600).reasons


def test_zero_balance_is_valid_snapshot_but_cannot_open(tmp_path):
    reconciler,client,clock=setup(tmp_path)
    client.assets[0].update(equity=0,availableBalance=0,positionMargin=0,unrealized=0,cashBalance=0)
    assert reconciler.refresh(client,symbols=['BTC_USDT'])['ready']
    result=preflight(reconciler)
    assert not result.ready and 'NO_CURRENT_AVAILABLE_FUNDS' in result.reasons


def test_orders_are_paginated_and_incomplete_collection_blocks_readiness(tmp_path):
    reconciler,client,clock=setup(tmp_path)
    calls=[]
    def pages(page_num,page_size):
        calls.append(page_num)
        return [order(str(i+page_num*100)) for i in range(100)] if page_num==1 else []
    client.current_orders=pages
    reconciler.refresh(client,symbols=['BTC_USDT'])
    assert calls==[1,2]
    client.current_orders=lambda **kw:[order(str(i)) for i in range(100)]
    with pytest.raises(LiveAccountError,match='ORDER_PAGINATION_INCOMPLETE'):
        reconciler.refresh(client,symbols=['BTC_USDT'])


def test_snapshot_missing_symbol_configuration_fails_closed(tmp_path):
    reconciler,client,clock=setup(tmp_path)
    reconciler.refresh(client,symbols=[])
    assert preflight(reconciler).reasons==('SYMBOL_CONFIGURATION_UNAVAILABLE',)


@pytest.mark.parametrize('mode',[True,1.0,2.0,'1',None])
def test_requested_position_mode_requires_exact_integer(tmp_path,mode):
    reconciler,client,clock=setup(tmp_path)
    reconciler.refresh(client,symbols=['BTC_USDT'])
    assert preflight(reconciler,position_mode=mode).reasons==('UNKNOWN_REQUESTED_POSITION_MODE',)


def test_equity_high_water_is_not_kept_as_an_entry_latch(tmp_path):
    from trading.runtime.live_account import observe_risk_reference
    store=RuntimeStore(str(tmp_path/'risk'))
    start=1794080000
    observe_risk_reference(store,'binding',500,now=start)
    observe_risk_reference(store,'binding',600,now=start+1)
    key='live:risk_reference:binding'
    planted=store.get(key)
    planted['peak']=600
    store.put(key,planted)
    result=observe_risk_reference(store,'binding',510,now=start+2)
    assert result['initial']==500 and result['drawdown_pct']==0
    assert result['daily_loss_pct']==0
    assert 'peak' not in store.get(key)
    again=observe_risk_reference(RuntimeStore(str(tmp_path/'risk')),'binding',510,now=start+3)
    assert 'peak' not in store.get(key) and again['drawdown_pct']==0
    lost=observe_risk_reference(store,'binding',400,now=start+4)
    assert lost['daily_loss_pct']==pytest.approx(.2)
    assert lost['drawdown_pct']==0 and 'peak' not in store.get(key)
