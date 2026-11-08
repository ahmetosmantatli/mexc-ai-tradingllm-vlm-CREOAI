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
