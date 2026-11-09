from types import SimpleNamespace
import pytest
from trading.control.live_rearm import acknowledge
from trading.runtime.store import RuntimeStore


def setup():
    store=RuntimeStore(':memory:');control=RuntimeStore(':memory:')
    control.put('account:friend:control',dict(stopped=True,enabled=False))
    store.put('live:halt',dict(intent_id='a',reason='emergency_close_requires_review',requires_review=True))
    store.put('live:intent:a',dict(status='closed',close_reason='EMERGENCY',realized_complete=True,entry_write_attempted=True))
    client=SimpleNamespace(open_positions=lambda:[],current_orders=lambda:[],protection_orders=lambda:[])
    return store,control,client


def test_reviewed_halt_acknowledgement_never_starts_trading():
    store,control,client=setup()
    result=acknowledge(store,control,client,'friend')
    assert result['trading_started'] is False
    assert not store.get('live:halt')
    assert store.get('live:reviewed_emergency:a')
    assert control.get('account:friend:control')==dict(stopped=True,enabled=False)


@pytest.mark.parametrize('method',['open_positions','current_orders','protection_orders'])
def test_reviewed_halt_cannot_clear_with_any_exchange_exposure(method):
    store,control,client=setup()
    setattr(client,method,lambda:[{}])
    with pytest.raises(ValueError):acknowledge(store,control,client,'friend')
    assert store.get('live:halt')


def test_reviewed_halt_requires_pause_and_final_close():
    store,control,client=setup()
    control.put('account:friend:control',dict(stopped=False,enabled=True))
    with pytest.raises(ValueError,match='telegram_stop_required'):acknowledge(store,control,client,'friend')
    control.put('account:friend:control',dict(stopped=True,enabled=False))
    store.put('live:intent:a',dict(status='closed',close_reason='EMERGENCY',realized_complete=False))
    with pytest.raises(ValueError,match='emergency_close_not_final'):acknowledge(store,control,client,'friend')


def test_unrelated_halt_is_never_cleared():
    store,control,client=setup()
    store.put('live:halt',dict(reason='entry_write_outcome_unknown'))
    with pytest.raises(ValueError,match='different_halt'):acknowledge(store,control,client,'friend')


def protected_setup(tmp_path):
    from .test_live_management_execution import prepared
    executor,client,store,row=prepared(tmp_path)
    row['intent_id']='protected'
    store.db.execute("DELETE FROM state WHERE key LIKE 'live:intent:%'")
    store.put('live:intent:protected',row)
    control=RuntimeStore(':memory:')
    control.put('account:friend:control',dict(stopped=True,enabled=False))
    control.put('account:friend:live_binding',row['binding_id'])
    store.put('live:halt',dict(intent_id='closed',reason='emergency_close_requires_review',requires_review=True))
    store.put('live:intent:closed',dict(status='closed',close_reason='EMERGENCY',
        realized_complete=True,entry_write_attempted=True))
    return store,control,client,row


def test_review_allows_only_verified_owned_protected_position_with_explicit_flag(tmp_path):
    store,control,client,row=protected_setup(tmp_path)
    with pytest.raises(ValueError,match='unresolved_intent'):
        acknowledge(store,control,client,'friend')
    before=store.get('live:intent:protected')
    result=acknowledge(store,control,client,'friend',allow_protected_positions=True)
    assert result['verified_protected_positions']==1
    assert not result['trading_started'] and not result['writes_enabled']
    assert store.get('live:intent:protected')==before
    assert not store.get('live:halt')
    assert not client.closes


@pytest.mark.parametrize('fault',[
    'missing_stop','wrong_quantity','different_binding','pending_management',
    'unknown_position','extra_order','missing_position','pending_close',
])
def test_protected_review_rejects_incomplete_ownership_or_protection(tmp_path,fault):
    store,control,client,row=protected_setup(tmp_path)
    if fault=='missing_stop':client.stops=[]
    if fault=='wrong_quantity':client.stops[0]['vol']=1
    if fault=='different_binding':row['binding_id']='other'
    if fault=='pending_management':row['management_pending']={'action':'reduce'}
    if fault=='unknown_position':client.positions[0]['positionId']=99
    if fault=='extra_order':client.orders=[{'orderId':99}]
    if fault=='missing_position':client.positions=[]
    if fault=='pending_close':row['close_write_attempted']=True
    store.put('live:intent:protected',row)
    with pytest.raises(ValueError):
        acknowledge(store,control,client,'friend',allow_protected_positions=True)
    assert store.get('live:halt')
    assert not client.closes


def test_review_button_cannot_clear_a_different_emergency():
    store,control,client=setup()
    with pytest.raises(ValueError,match='halt_changed'):
        acknowledge(store,control,client,'friend',expected_intent_id='stale')
    assert store.get('live:halt')['intent_id']=='a'
