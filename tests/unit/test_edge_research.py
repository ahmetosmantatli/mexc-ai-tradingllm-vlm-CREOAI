from copy import deepcopy
import pytest
from trading.runtime.edge_research import replay,compare,record_review
from trading.control.edge_report import export
from trading.config import Settings
from trading.runtime.store import RuntimeStore
from .test_watch_service import signal


PLAN=dict(side='long',entry_price=100.,stop_loss_price=98.,take_profit_price=104.)
COSTS=dict(decision_at=100,horizon_bars=2,fee_rate=.001,slippage_bps=5,spread_bps=10,now=2000)


def test_replay_costs_and_stop_first_ambiguity_reduce_return():
    rows=[[300,100,105,97,101,10],[600,101,102,100,101,10]]
    result=replay(PLAN,rows,**COSTS)
    assert result['status']=='completed' and result['exit_reason']=='SL' and result['ambiguous_intrabar']
    assert result['r_after_estimated_costs_ex_funding'] < -1
    assert result['funding_included'] is False


def test_horizon_pending_missing_history_and_missing_spread_not_zero():
    rows=[[300,100,101,99,100,10]]
    assert replay(PLAN,rows,**COSTS)['status']=='pending'
    assert replay(PLAN,[],**COSTS)['status']=='missing_entry_bar'
    assert replay(PLAN,rows,**dict(COSTS,spread_bps=None))['status']=='missing_spread'
    assert replay(PLAN,[[]],**COSTS)['status']=='invalid_or_missing_bars'
    assert replay(PLAN,[rows[0],[900,100,101,99,100,10]],**COSTS)['status']=='invalid_or_missing_bars'


def test_compare_keeps_first_opportunity_and_does_not_count_repeated_reviews_as_independent():
    base=dict(id='a',at=100,opportunity='same',symbol='BTC_USDT',action='hold',strategy='trend',baseline=PLAN,
        model_plan=None,fee_rate=.001,slippage_bps=5,observed_spread_bps=10)
    duplicate=dict(base,id='b',at=120)
    report=compare([base,duplicate],{'BTC_USDT':[[300,100,105,99,104,10]]},now=2000)
    assert report['unique_opportunities']==1 and report['matched']==1
    assert report['mean_delta_r']<0 # Filtering a winner hurt this hypothetical sample.
    assert 'not actual fills' in report['limitations']


def test_record_is_immutable_and_export_is_offline(tmp_path):
    store=RuntimeStore(str(tmp_path/'data'/'friend'/'live'/'runtime.sqlite3'))
    candidate=signal();candidate.features.update(source_open_at=0,observed_spread_bps=10)
    trace=dict(id='a',completed_at=100,action='hold',market_fingerprint='m',execution_status='not_opened')
    cfg=Settings(_env_file=None)
    record_review(store,trace,candidate,candidate,cfg)
    trace['action']='trade';record_review(store,trace,candidate,candidate,cfg)
    report=export(tmp_path/'data',tmp_path/'out',bars={'BTC_USDT':[[300,100,105,99,104,10]]})
    assert report['recorded_reviews']==1 and report['results'][0]['action']=='hold'
    assert report['matched']==1
    assert (tmp_path/'out'/'edge-comparison.json').exists()
