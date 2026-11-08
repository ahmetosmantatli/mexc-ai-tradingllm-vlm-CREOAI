import json
import subprocess
import sys
from decimal import Decimal

import pytest

from trading.runtime.runner2 import Fill, Opening, Revision, Runner2ExitReplay


def target(price, timestamp, field):
    return dict(price=price, reference=dict(timeframe='5m', open_time=timestamp, field=field))


def scenario(side='long', *, split=True, contracts=10):
    long = side == 'long'
    field = 'high' if long else 'low'
    targets = [target(110 if long else 90, 0, field), target(120 if long else 80, 300, field)]
    return dict(
        symbol='HOT_USDT', side=side, entry=100, contracts=contracts, quantity_step=1,
        minimum_contracts=2, price_step=1, stop=95 if long else 105,
        quote=dict(at=900, bid=99, ask=101, fair=100),
        evidence={'5m': [[0, 100, 110, 90, 100, 10], [300, 100, 120, 80, 100, 10]]},
        plan=dict(targets=targets if split else targets[-1:], first_fraction='0.45' if split else None,
                  reason='Teyitli yapı seviyeleri ve iki hedef'),
    )


def revision(opening, *, split=False, expected=0, **overrides):
    plan = dict(opening['plan'])
    if not split:
        plan.update(targets=plan['targets'][-1:], first_fraction=None)
    return Revision.model_validate(dict(kind='revise', expected_revision=expected, quote=opening['quote'],
                                        evidence=opening['evidence'], plan=plan) | overrides)


@pytest.mark.parametrize('side', ['long', 'short'])
def test_entry_split_and_confirmed_partial_fill_resize_stop(side):
    opening = scenario(side)
    replay = Runner2ExitReplay(Opening.model_validate(opening))
    assert replay.snapshot()['targets']['tp1']['contracts'] == '4'
    fill = Fill(kind='fill', expected_revision=0, at=901, fill_id='tp1-fill', exit='tp1',
                contracts=2, price=110 if side == 'long' else 90)
    replay.apply(fill)
    replay.apply(fill)
    assert replay.snapshot()['stop_contracts'] == '8'
    assert replay.snapshot()['targets']['tp1']['contracts'] == '2'
    assert replay.revision == 1
    with pytest.raises(ValueError, match='conflicting_fill_id'):
        replay.apply(fill.model_copy(update={'contracts': Decimal(1)}))


def test_replay_enforces_optional_entry_risk_budget():
    opening = scenario()
    opening['entry_risk'] = dict(contract_size=1, loss_budget=60, fee_rate=0, exit_slippage_bps=0)
    replay = Runner2ExitReplay(Opening.model_validate(opening))
    assert replay.snapshot()['entry_ioc_limit'] == '101'
    opening['entry'] = 102
    with pytest.raises(ValueError, match='simulated_entry_outside_risk_limit'):
        Runner2ExitReplay(Opening.model_validate(opening))


def test_partial_stop_keeps_remainder_pending_and_records_net_risk():
    opening = scenario()
    opening['entry_risk'] = dict(contract_size=1, loss_budget=60, fee_rate=0, exit_slippage_bps=0)
    opening['planned_entry'] = 100
    replay = Runner2ExitReplay(Opening.model_validate(opening))
    replay.apply(Fill(kind='fill', expected_revision=0, at=901, fill_id='s1', exit='stop',
                      contracts=4, price=95))
    assert replay.snapshot()['stop_contracts'] is None
    with pytest.raises(ValueError, match='stop_execution_needs_reconciliation'):
        replay.apply(revision(opening, expected=1, quote=dict(at=902, bid=99, ask=101, fair=100)))
    replay.apply(Fill(kind='fill', expected_revision=1, at=903, fill_id='s2', exit='stop',
                      contracts=6, price=94))
    snapshot = replay.snapshot()
    assert snapshot['remaining_contracts'] == '0'
    assert Decimal(snapshot['performance']['realized_net_before_funding_usdt']) == -56
    assert Decimal(snapshot['performance']['realized_r_before_funding']) == Decimal('-1.12')


def test_open_position_can_split_then_return_to_single_target():
    opening = scenario(split=False)
    replay = Runner2ExitReplay(Opening.model_validate(opening))
    split = scenario()
    replay.apply(revision(split, split=True))
    replay.apply(revision(split, expected=1))
    assert replay.snapshot()['targets'] == {'tp2': {'price': '120', 'contracts': '10'}}
    with pytest.raises(ValueError, match='stale_exit_event'):
        replay.apply(revision(split, expected=1))


def test_crossed_target_and_looser_stop_are_rejected_without_mutation():
    opening = scenario()
    replay = Runner2ExitReplay(Opening.model_validate(opening))
    with pytest.raises(ValueError, match='existing_target_needs_fill_reconciliation'):
        replay.apply(revision(opening, quote=dict(at=901, bid=110, ask=111, fair=110)))
    with pytest.raises(ValueError, match='stop_must_tighten'):
        replay.apply(revision(opening, stop=94, stop_reference=target(94, 0, 'low')['reference']))
    assert replay.revision == 0 and replay.stop == 95


@pytest.mark.parametrize('side', ['long', 'short'])
def test_scalp_promotes_with_new_higher_timeframe_target_but_keeps_initial_risk(side):
    opening = scenario(side, split=False)
    opening['quote']['at'] = 15000
    opening['horizon'] = dict(style='scalp', review_timeframe='5m', expires_at=15600)
    replay = Runner2ExitReplay(Opening.model_validate(opening))
    new_target = target(130 if side == 'long' else 70, 0, 'high' if side == 'long' else 'low')
    new_target['reference']['timeframe'] = '4h'
    new_horizon = dict(style='swing', review_timeframe='4h', expires_at=45000)
    with pytest.raises(ValueError, match='horizon_promotion_requires_new_target_evidence'):
        replay.apply(revision(opening, horizon=new_horizon))
    plan = dict(targets=[new_target], reason='Dört saatlik kapanış daha uzak hedefi destekliyor.')
    evidence = {'4h': [[0, 100, 130, 70, 100, 10]]}
    with pytest.raises(ValueError, match='invalid_horizon_promotion'):
        replay.apply(revision(opening, horizon=new_horizon, plan=plan, evidence=evidence,
                              quote={**opening['quote'], 'at': 15600}))
    replay.apply(revision(opening, horizon=new_horizon, plan=plan, evidence=evidence))
    snapshot = replay.snapshot()
    assert snapshot['horizon'] == new_horizon
    assert snapshot['opening']['horizon'] == opening['horizon']
    assert snapshot['stop_price'] == str(opening['stop'])
    assert snapshot['targets']['tp2']['price'] == str(new_target['price'])


def test_below_minimum_and_future_evidence_are_rejected():
    with pytest.raises(ValueError, match='target_quantity_below_contract_minimum'):
        Runner2ExitReplay(Opening.model_validate(scenario(contracts=4)))
    opening = scenario()
    opening['evidence']['5m'].append([900, 100, 120, 80, 100, 10])
    with pytest.raises(ValueError, match='invalid_or_future_candle_evidence'):
        Runner2ExitReplay(Opening.model_validate(opening))


def test_stop_only_revision_after_tp1_does_not_rearm_tp1():
    opening = scenario()
    replay = Runner2ExitReplay(Opening.model_validate(opening))
    replay.apply(Fill(kind='fill', expected_revision=0, at=901, fill_id='tp1', exit='tp1',
                      contracts=4, price=110))
    replay.apply(Revision(kind='revise', expected_revision=1, stop=101,
                          quote=dict(at=1200, bid=109, ask=110, fair=109),
                          stop_reference=target(102, 900, 'low')['reference'],
                          evidence={'5m': [[900, 105, 110, 102, 109, 10]]}))
    state = replay.snapshot()
    assert state['stop_price'] == '101' and state['stop_contracts'] == '6'
    assert state['targets'] == {'tp2': {'price': '120', 'contracts': '6'}}


def test_cli_records_tp1_and_allows_final_target_for_remainder(tmp_path):
    opening = scenario()
    fill = dict(kind='fill', expected_revision=0, at=901, fill_id='tp1-fill',
                exit='tp1', contracts=4, price=110)
    single = revision(opening, expected=1, quote=dict(at=902, bid=109, ask=110, fair=109))
    path = tmp_path / 'scenario.json'
    path.write_text(json.dumps(dict(opening=opening, events=[fill, single.model_dump(mode='json')])))
    result = subprocess.run([sys.executable, '-m', 'trading.control.runner2', str(path)],
                            check=True, capture_output=True, text=True)
    state = json.loads(result.stdout)
    assert state['runner_id'] == 'runner2' and state['mode'] == 'SHADOW'
    assert state['stop_contracts'] == '6'
    assert state['targets'] == {'tp2': {'price': '120', 'contracts': '6'}}
    assert state['events'][1]['fill_id'] == 'tp1-fill'
