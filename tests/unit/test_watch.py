from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from trading.evaluation.watch import WatchPlan,WatchCoordinator,watch_triggered,watch_invalidated,watch_snapshot
from trading.runtime.store import RuntimeStore
from trading.quant.signals import CandidateSignal


def candidate(side='long'):
    return CandidateSignal(symbol='BTC_USDT',side=side,strategy='trend',entry_price=100,
        stop_loss_price=98 if side=='long' else 102,take_profit_price=106 if side=='long' else 94,
        confidence=.8,rationale='setup',features={'dedup_key':'bar','secret':'excluded'})


def plan(**updates):
    return WatchPlan(**dict(dict(trigger_condition='price_above',trigger_price=101,
        invalidation_condition='below',invalidation_price=98,expires_at=2000),**updates))


def coord(tmp_path):
    return WatchCoordinator(RuntimeStore(str(tmp_path/'watch')))


def test_watch_independent_queue_persistent_safe_snapshot(tmp_path):
    c=coord(tmp_path)
    assert c.commit(candidate(),'watch',0,plan(),now=1000)
    c.store.put('candidate_queue',[])
    restarted=coord(tmp_path)
    card=restarted.card('BTC_USDT')
    assert card['status']=='watching' and card['revision']==1
    assert card['candidate']['entry_price']==100
    assert 'secret' not in card['candidate']['features']
    row=watch_snapshot(c.store)[0]
    assert row['trigger_type']=='price_above' and row['trigger_price']==101
    assert row['invalidation']==dict(condition='below',price=98)
    assert set(c.store.get('watch_events')[0])=={'id','symbol','status','reason','at','revision'}


@pytest.mark.parametrize('condition,price,expected', [('price_above',101,False),('price_above',102,True),
    ('price_below',100,True),('price_below',101,False)])
def test_pure_price_thresholds(condition,price,expected):
    p=plan(trigger_condition=condition,invalidation_price=90)
    assert watch_triggered(p,price=price,now=1000)==expected
    assert not watch_triggered(p,price=price,now=2000)


@pytest.mark.parametrize('timeframe',['5m','15m','4h'])
@pytest.mark.parametrize('above',[True,False])
def test_closed_cross_requires_two_complete_ordered_candles(timeframe,above):
    p=plan(trigger_condition='closed_candle_cross_'+('above' if above else 'below'),timeframe=timeframe,invalidation_price=90,expires_at=50000)
    gap={'5m':300,'15m':900,'4h':14400}[timeframe]
    candles=[{'close':100 if above else 102,'closed_at':900},{'close':102 if above else 100,'closed_at':900+gap}]
    assert watch_triggered(p,closed_candles={timeframe:candles},now=901+gap)
    assert not watch_triggered(p,closed_candles=candles,now=999)
    assert not watch_triggered(p,closed_candles=list(reversed(candles)),now=901+gap)
    assert not watch_triggered(p,closed_candles=[candles[1]],now=901+gap)
    candles[-1]['complete']=False
    assert not watch_triggered(p,closed_candles=candles,now=901+gap)


def test_trigger_once_invalidation_priority_and_expiry(tmp_path):
    c=coord(tmp_path)
    c.commit(candidate(),'watch',0,plan(),now=1000)
    assert c.observe('BTC_USDT',price=102,now=1001)['status']=='triggered'
    assert c.observe('BTC_USDT',price=103,now=1002) is None
    assert c.observe('BTC_USDT',price=98,now=1003)['status']=='invalidated'
    assert c.observe('BTC_USDT',price=105,now=1004) is None
    rev=c.card('BTC_USDT')['revision']
    c.commit(candidate(),'watch',rev,plan(),now=1005)
    assert c.observe('BTC_USDT',price=105,now=2000)['status']=='expired'


def test_parallel_late_decision_cannot_replace_new_symbol_revision(tmp_path):
    c=coord(tmp_path)
    # Separate stores share SQLite atomic CAS, same observation only one wins.
    other=coord(tmp_path)
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda coordinator:coordinator.commit(candidate(),'watch',0,plan(),now=1000),[c,other]))
    assert sorted(results)==[False,True]
    assert c.card('BTC_USDT')['revision']==1
    assert len(c.store.get('watch_events'))==1
    assert not c.commit(candidate(),'trade',0,now=1001)


def test_opposite_trade_requires_explicit_prior_revision_acknowledgement(tmp_path):
    c=coord(tmp_path)
    c.commit(candidate(),'watch',0,plan(),now=1000)
    assert not c.commit(candidate('short'),'trade',1,now=1001)
    assert not c.commit(candidate('short'),'trade',1,reviewed_revision=0,now=1001)
    assert c.commit(candidate('short'),'trade',1,reviewed_revision=1,now=1001)
    assert c.card('BTC_USDT')['status']=='superseded'


def test_hold_preserves_watch_but_invalidates_inflight_old_decision(tmp_path):
    c=coord(tmp_path)
    c.commit(candidate(),'watch',0,plan(),now=1000)
    assert c.commit(candidate(),'hold',1,now=1001)
    assert c.card('BTC_USDT')['status']=='watching'
    assert not c.commit(candidate(),'trade',1,now=1002)
    assert c.terminate('BTC_USDT','position_opened',now=1003)['status']=='position_opened'
    assert c.terminate('BTC_USDT','cancelled',now=1004) is None


@pytest.mark.parametrize('expiry',[999,1000,87401])
def test_expiry_validation_does_not_mutate_store(tmp_path,expiry):
    c=coord(tmp_path)
    with pytest.raises(ValueError):
        c.commit(candidate(),'watch',0,plan(expires_at=expiry),now=1000)
    assert c.card('BTC_USDT')['revision']==0
    assert c.store.get('watch_events') is None


def test_schema_requires_candle_timeframe_and_finite_prices():
    with pytest.raises(ValidationError):
        plan(trigger_condition='closed_candle_cross_above')
    with pytest.raises(ValidationError):
        plan(trigger_price=float('nan'))
    assert not watch_invalidated(plan(),price=float('nan'))


def test_events_bounded_and_terminal_cancel(tmp_path):
    c=coord(tmp_path)
    for _ in range(55):
        rev=c.card('BTC_USDT')['revision']
        c.commit(candidate(),'watch',rev,plan(),now=1000)
        c.terminate('BTC_USDT',now=1001)
    assert len(c.store.get('watch_events'))==100
    assert c.card('BTC_USDT')['status']=='cancelled'


def test_active_capacity_bound_preserves_existing_watch_updates(tmp_path):
    c=WatchCoordinator(RuntimeStore(str(tmp_path/'watch')),max_active=1)
    first=candidate()
    c.commit(first,'watch',0,plan(),now=1000)
    second=first.model_copy(update={'symbol':'ETH_USDT'})
    assert not c.commit(second,'watch',0,plan(),now=1001)
    assert c.commit(first,'watch',1,plan(),now=1001)
    c.terminate(first.symbol,now=1002)
    assert c.commit(second,'watch',0,plan(),now=1003)


def test_trade_record_uses_new_side_and_open_revision_cannot_override_later_watch(tmp_path):
    c=coord(tmp_path)
    c.commit(candidate(),'watch',0,plan(),rationale='Wait for reclaim',now=1000)
    assert watch_snapshot(c.store)[0]['reason']=='Wait for reclaim'
    c.commit(candidate('short'),'trade',1,reviewed_revision=1,now=1001)
    assert c.card('BTC_USDT')['side']=='short'
    assert c.terminate('BTC_USDT','position_opened',expected_revision=2,now=1002)['status']=='position_opened'
    c.commit(candidate(),'watch',3,plan(),now=1003)
    assert c.terminate('BTC_USDT','position_opened',expected_revision=2,now=1004) is None
    assert c.card('BTC_USDT')['status']=='watching'


def test_old_cross_and_candle_gaps_cannot_trigger_new_watch(tmp_path):
    c=coord(tmp_path)
    p=plan(trigger_condition='closed_candle_cross_above',timeframe='5m')
    c.commit(candidate(),'watch',0,p,now=1000)
    old=[{'close':100,'closed_at':600},{'close':102,'closed_at':900}]
    assert c.observe('BTC_USDT',closed_candles=old,now=1001) is None
    gap=[{'close':100,'closed_at':300},{'close':102,'closed_at':1200}]
    assert c.observe('BTC_USDT',closed_candles=gap,now=1201) is None
    fresh=[{'close':100,'closed_at':900},{'close':102,'closed_at':1200}]
    assert c.observe('BTC_USDT',closed_candles=fresh,now=1201)['status']=='triggered'


@pytest.mark.parametrize('updates', [dict(trigger_condition='price_above',invalidation_condition='below',invalidation_price=102),
    dict(trigger_condition='price_below',invalidation_condition='above',invalidation_price=100),
    dict(invalidation_condition='below',invalidation_price=101),dict(trigger_price=True),dict(expires_at='2000')])
def test_trigger_threshold_outside_invalidation_and_strict_numeric_input(updates):
    with pytest.raises(ValidationError):
        plan(**updates)


def test_trigger_timestamp_immutable_across_hold_and_reset_for_new_plan(tmp_path):
    c=coord(tmp_path)
    c.commit(candidate(),'watch',0,plan(),now=1000)
    c.observe('BTC_USDT',price=102,now=1001)
    assert c.card('BTC_USDT')['triggered_at']==1001
    c.commit(candidate(),'hold',2,now=1002)
    assert c.card('BTC_USDT')['triggered_at']==1001
    c.commit(candidate(),'watch',3,plan(),now=1003)
    assert c.card('BTC_USDT')['triggered_at'] is None


def test_observation_of_old_plan_cannot_trigger_replacement_plan(tmp_path):
    c=coord(tmp_path)
    c.commit(candidate(),'watch',0,plan(),now=1000)
    c.commit(candidate(),'watch',1,plan(trigger_price=100.5),now=1001)
    assert c.observe('BTC_USDT',price=102,expected_revision=1,now=1002) is None
    assert c.card('BTC_USDT')['status']=='watching'
    assert c.observe('BTC_USDT',price=102,expected_revision=2,now=1003)['status']=='triggered'
