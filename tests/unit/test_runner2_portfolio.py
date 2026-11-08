import json
import subprocess
import sys
from decimal import Decimal

import pytest

from trading.runtime.runner2_portfolio import Runner2Portfolio, commands_adapter
from trading.runtime.store import RuntimeStore


def evidence(at, symbol='HOT_USDT'):
    return dict(symbol=symbol, family='sweep', timeframe='5m', regime='range', observed_at=at,
                features=dict(spread_bps=12, btc_return=.001))


def command(kind, cid, at, **kw):
    return dict(kind=kind, command_id=cid, at=at, **kw)


def queue(cid, at, subject='candidate', trigger='entry'):
    return command('queue', cid, at, subject=subject, revision=0, trigger=trigger,
                   expires_at=at+300, evidence=evidence(at))


def apply(p, row):
    return p.apply(commands_adapter.validate_python([row])[0])


def make_trade(p, cid, at):
    apply(p, queue(cid+'-q', at, subject=cid))
    leases = apply(p, command('dispatch', cid+'-dispatch', at+1, available_slots=1))['leases']
    apply(p, command('result', cid, at+2, lease=leases[0]['lease'], action='trade', reason='Setup valid'))


def test_management_priority_retry_and_restart_without_duplicate_dispatch(tmp_path):
    path = str(tmp_path/'runner2-shadow-test.sqlite3')
    p = Runner2Portfolio(RuntimeStore(path), 'demo')
    apply(p, queue('entry', 900))
    apply(p, queue('urgent', 901, subject='position', trigger='structure_break'))
    assert apply(p, command('dispatch', 'zero', 902, available_slots=0))['status'] == 'waiting'
    dispatch = command('dispatch', 'first', 903, available_slots=1)
    lease = apply(p, dispatch)['leases'][0]
    assert lease['subject'] == 'position'
    waiting = command('result', 'err', 904, lease=lease['lease'], action='unavailable',
                      reason='All routes unavailable', retry_after_seconds=10)
    assert apply(p, waiting)['retry_at'] == 914
    p.store.db.close()
    p = Runner2Portfolio(RuntimeStore(path), 'demo')
    assert apply(p, dispatch)['leases'][0] == lease
    assert p.snapshot()['active_reviews'] == 0
    second = apply(p, command('dispatch', 'second', 914, available_slots=1))['leases'][0]
    assert second['subject'] == 'position' and second['attempts'] == 2
    apply(p, command('result', 'hold', 915, lease=second['lease'], action='hold', reason='Keep protection'))
    assert p.snapshot()['pending_reviews'] == 1 and p.snapshot()['decisions'] == 1
    p.store.db.close()


def test_rotation_requires_confirmed_reduce_and_fresh_evidence_and_reserves_once():
    p = Runner2Portfolio(RuntimeStore(':memory:'), 'demo')
    make_trade(p, 'old', 900)
    apply(p, command('balance', 'b1', 903, observed_at=903, available_margin=10, available_risk=1))
    apply(p, command('reduction_confirmed', 'r1', 904, reduction_id='reduce-1', remaining_protection_verified=True))
    reservation = dict(decision_id='old', balance_version=1, margin=7, risk=.7, quote_at=905, reduction_id='reduce-1')
    with pytest.raises(ValueError, match='reduction_and_fresh_balance_and_decision_required'):
        apply(p, command('reserve', 'reject', 905, **reservation))
    apply(p, command('balance', 'b2', 906, observed_at=906, available_margin=10, available_risk=1))
    make_trade(p, 'new', 907)
    apply(p, command('reserve', 'reserve', 910, **(reservation | dict(decision_id='new', balance_version=2, quote_at=910))))
    apply(p, command('entry_result', 'unknown', 911, decision_id='new', outcome='unknown'))
    make_trade(p, 'parallel', 912)
    with pytest.raises(ValueError, match='insufficient_unreserved_margin'):
        apply(p, command('reserve', 'overbook', 915, decision_id='parallel', balance_version=2,
                         margin=4, risk=.4, quote_at=915))
    apply(p, command('entry_result', 'no-fill', 916, decision_id='new', outcome='no_fill'))
    apply(p, command('reserve', 'second', 917, decision_id='parallel', balance_version=2,
                     margin=4, risk=.4, quote_at=917))


def test_retrieval_only_includes_results_known_at_decision_time():
    p = Runner2Portfolio(RuntimeStore(':memory:'), 'demo')
    make_trade(p, 'trade', 900)
    apply(p, command('balance', 'balance', 903, observed_at=903, available_margin=10, available_risk=1))
    apply(p, command('reserve', 'reserve', 904, decision_id='trade', balance_version=1, margin=4, risk=.4, quote_at=904))
    apply(p, command('entry_result', 'filled', 905, decision_id='trade', outcome='filled', initial_risk_usdt=.3))
    apply(p, command('close_result', 'closed', 910, decision_id='trade', net_usdt=.6))
    past = apply(p, command('retrieve', 'past', 911, evidence=evidence(908)))['examples'][0]
    assert past['outcome'] == dict(outcome='filled', at=905)
    later = apply(p, command('retrieve', 'later', 912, evidence=evidence(912)))['examples'][0]
    assert later['outcome']['realized_r'] == '2'


def test_no_progress_trigger_and_shadow_cli(tmp_path):
    source = tmp_path/'scenario.json'
    source.write_text(json.dumps(dict(commands=[
        command('observe', 'obs', 1000, subject='position', revision=0, opened_at=0,
                max_holding_bars=10, no_progress_bars=3, minimum_progress_r=.5, mfe_r=.1,
                evidence=evidence(1000)),
        command('dispatch', 'dispatch', 1001, available_slots=1),
    ])))
    output = subprocess.run([sys.executable, '-m', 'trading.control.runner2', str(source),
                            '--portfolio-db', str(tmp_path/'runner2-shadow-cli.sqlite3')],
                            check=True, capture_output=True, text=True)
    state = json.loads(output.stdout)
    assert state['mode'] == 'SHADOW' and state['active_reviews'] == 1
    assert state['results'][1]['leases'][0]['trigger'] == 'no_progress'


def target_observation(cid, at, *, side='long', price='110', bid='109.7', ask='109.8',
                       revision=0, quote_at=None, atr='2'):
    return command(
        'observe', cid, at, subject='position', revision=revision, opened_at=900,
        max_holding_bars=10, no_progress_bars=3, minimum_progress_r=.5, mfe_r=2,
        evidence=evidence(at),
        target=dict(side=side, price=price, initial_risk_distance='2', atr=atr,
                    quote=dict(at=at if quote_at is None else quote_at, bid=bid, ask=ask, fair=bid)),
    )


@pytest.mark.parametrize(('side', 'bid', 'ask'), [('long', '109.7', '109.8'), ('short', '110.2', '110.3')])
def test_target_observation_queues_once_per_revision_and_expires(side, bid, ask):
    p = Runner2Portfolio(RuntimeStore(':memory:'), 'demo')
    obs = dict(side=side, bid=bid, ask=ask)
    assert apply(p, target_observation('wide', 910, atr='.2', **obs))['status'] == 'no_trigger'
    assert apply(p, target_observation('near', 911, **obs))['status'] == 'queued'
    assert apply(p, target_observation('duplicate', 912, **obs))['status'] == 'coalesced'
    assert apply(p, command('dispatch', 'expired', 941, available_slots=1))['leases'] == []
    # A new quote is enough to reconsider an undispatched event even when
    # the candle used for structure has not changed.
    fresh = target_observation('near-fresh', 942, **obs) | dict(evidence=evidence(911))
    assert apply(p, fresh)['status'] == 'queued'
    lease = apply(p, command('dispatch', 'process', 943, available_slots=1))['leases'][0]
    assert lease['trigger'] == 'near_target'
    assert Decimal(lease['target_proximity']['distance']) == Decimal('.2')
    assert Decimal(lease['target_proximity']['threshold']) == Decimal('.5')
    apply(p, command('result', 'hold', 944, lease=lease['lease'], action='hold', reason='Keep protection'))
    assert apply(p, target_observation('same-revision', 945, **obs))['status'] == 'coalesced'
    assert apply(p, target_observation('new-revision', 946, revision=1, **obs))['status'] == 'queued'


def test_target_cross_and_new_plan_supersede_position_reviews():
    p = Runner2Portfolio(RuntimeStore(':memory:'), 'demo')
    with pytest.raises(ValueError, match='invalid_or_stale_target_quote'):
        apply(p, target_observation('stale', 930, quote_at=900))
    apply(p, target_observation('near', 931))
    lease = apply(p, command('dispatch', 'process', 932, available_slots=1))['leases'][0]
    with pytest.raises(ValueError, match='target_changed_without_new_plan_revision'):
        apply(p, target_observation('mismatch', 933, price='112'))
    assert apply(p, target_observation('touch', 933, bid='109.9', ask='110')) == {
        'status': 'target_reconciliation_required'
    }
    assert apply(p, command('result', 'stale-hold', 934, lease=lease['lease'], action='hold',
                            reason='Keep protection')) == {'status': 'superseded'}
    assert apply(p, target_observation('retrace', 935))['status'] == 'target_reconciliation_required'
    assert apply(p, target_observation('updated-far', 936, price='112', revision=1))['status'] == 'no_trigger'
    assert apply(p, target_observation('updated-near', 937, price='112', bid='111.7', ask='111.8',
                                       revision=1))['status'] == 'queued'
    assert apply(p, target_observation('later-revision-far', 938, price='113', revision=2))['status'] == 'no_trigger'
    assert apply(p, command('dispatch', 'superseded-queue', 939, available_slots=1))['leases'] == []


def test_shadow_ai_batch_honors_capacity_and_passes_only_priority_job():
    from types import SimpleNamespace
    from trading.runtime.runner2_reviews import run_review_batch

    p = Runner2Portfolio(RuntimeStore(':memory:'), 'demo')
    apply(p, queue('entry', 900))
    apply(p, queue('urgent', 901, subject='position', trigger='structure_break'))
    requests = []

    def complete(**kwargs):
        requests.append(json.loads(kwargs['user']))
        result = dict(action='reduce', reason='Yapı bozuldu, riski düşür.', close_fraction=.5)
        child.response_validator(result)
        return result

    child = SimpleNamespace(complete_json=complete, close=lambda: None,
                            last_provider='gemini', last_model='fake')
    pool = SimpleNamespace(analysis_capacity=lambda **kwargs: {'parallel_candidates': 0},
                           fork=lambda: child)
    assert run_review_batch(p, pool, clock=lambda: 902)['reviews'] == []
    assert requests == []
    pool.analysis_capacity = lambda **kwargs: {'parallel_candidates': 1}
    reviews = run_review_batch(p, pool, clock=lambda: 903)['reviews']
    assert [r['subject'] for r in reviews] == ['position']
    assert requests[0]['trigger'] == 'structure_break'
    assert p.snapshot()['pending_reviews'] == 1
    decision = p.store.get(p.key)['decisions'][reviews[0]['decision_id']]
    assert decision['close_fraction'] == .5
