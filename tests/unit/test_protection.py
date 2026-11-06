import threading
import time
from types import SimpleNamespace
import pytest
from trading.runtime.protection import PositionProtection, ProtectionUnavailable


class Ledger:
    def __init__(self):
        self.positions = {'BTC_USDT': dict(opened_at=1), 'ETH_USDT': dict(opened_at=2)}
        self.marks = []

    def mark(self, symbol, bid, ask, fair):
        self.marks.append((symbol,bid,ask,fair))


def quote(now=1000, **kw):
    values=dict(bid1=99.,ask1=101., raw=dict(timestamp=now*1000,fairPrice=100.))
    values.update(kw)
    return SimpleNamespace(**values)


def service(ledger, public=None, **kw):
    return PositionProtection(lambda:ledger,public or SimpleNamespace(get_ticker=lambda _:quote()),
                              clock=lambda:1000,monotonic=lambda:1000,**kw)


def test_initial_gate_and_fresh_supervision():
    ledger=Ledger()
    protection=service(ledger)
    with pytest.raises(ProtectionUnavailable):
        protection.assert_ready()
    assert protection.tick()['ok']
    protection.assert_ready()
    assert len(ledger.marks)==2
    # Volume is irrelevant for closing an existing position.
    assert not hasattr(quote(),'quote_volume_24h')


def test_one_bad_market_does_not_skip_other():
    ledger=Ledger()
    def get(symbol):
        if symbol=='BTC_USDT':
            raise TimeoutError()
        return quote()
    protection=service(ledger,SimpleNamespace(get_ticker=get))
    state=protection.tick()
    assert not state['ok'] and ledger.marks[0][0]=='ETH_USDT'
    with pytest.raises(ProtectionUnavailable):
        protection.assert_ready()


@pytest.mark.parametrize('ticker',[quote(now=900),quote(raw=dict(timestamp=float('nan'),fairPrice=100)),quote(bid1=102),quote(bid1=float('nan'))])
def test_invalid_market_blocks_new_risk(ticker):
    ledger=Ledger()
    protection=service(ledger,SimpleNamespace(get_ticker=lambda _:ticker))
    assert not protection.tick()['ok']
    assert ledger.marks==[]


def test_stale_health_and_new_position_gate():
    ledger=Ledger()
    now=[1000]
    protection=PositionProtection(lambda:ledger,SimpleNamespace(get_ticker=lambda _:quote()),clock=lambda:1000,monotonic=lambda:now[0])
    protection.tick()
    ledger.positions['OTHER_USDT']=dict(opened_at=3)
    with pytest.raises(ProtectionUnavailable,match='await'):
        protection.assert_ready()
    del ledger.positions['OTHER_USDT']
    now[0]=1100
    with pytest.raises(ProtectionUnavailable,match='stale'):
        protection.assert_ready()


def test_network_does_not_hold_shared_ledger_lock():
    ledger=Ledger()
    lock=threading.RLock()
    observed=[]
    def get(_):
        acquired=[]
        def check():
            ok=lock.acquire(timeout=.1)
            acquired.append(ok)
            if ok: lock.release()
        t=threading.Thread(target=check)
        t.start();t.join()
        observed.extend(acquired)
        return quote()
    service(ledger,SimpleNamespace(get_ticker=get),lock=lock).tick()
    assert observed==[True,True]


def test_replaced_position_does_not_get_previous_quote():
    ledger=Ledger()
    def get(symbol):
        ledger.positions[symbol]['opened_at']+=100
        return quote()
    state=service(ledger,SimpleNamespace(get_ticker=get)).tick()
    assert not state['ok'] and ledger.marks==[]


def test_mutation_failure_latches_fatal_even_after_transient_clears():
    ledger=Ledger()
    def fail(*_): raise OSError('disk error')
    ledger.mark=fail
    protection=service(ledger)
    assert protection.tick()['fatal']
    ledger.mark=lambda *_:None
    state=protection.tick()
    assert state['fatal'] and not state['ok']


def test_slow_analysis_does_not_stop_background_supervision():
    ledger=Ledger()
    count=threading.Event()
    def mark(*_): count.set()
    ledger.mark=mark
    protection=service(ledger,poll_seconds=.01).start()
    try:
        # Main/analysis thread is free to block independently of price thread.
        assert count.wait(.5)
        protection.assert_ready()
    finally:
        assert protection.stop()
