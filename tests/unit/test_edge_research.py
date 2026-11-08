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



def test_open_gap_resolves_barrier_order_before_intrabar_extremes():
    before=[300,100,101,99,100,10]
    gap_profit=[600,105,106,97,100,10]
    result=replay(PLAN,[before,gap_profit],**COSTS)
    assert result['exit_reason']=='TP' and result['ambiguous_intrabar'] is False
    gap_loss=[600,97,105,96,100,10]
    result=replay(PLAN,[before,gap_loss],**COSTS)
    assert result['exit_reason']=='SL' and result['ambiguous_intrabar'] is False
    assert result['conservative_r']==result['optimistic_r']


def test_replay_never_uses_decision_bar_or_unclosed_future_bar():
    bars=[[0,100,110,90,100,10],[300,100,101,99,100,10],[600,100,105,99,104,10]]
    result=replay(PLAN,bars,**dict(COSTS,now=899))
    assert result['status']=='pending' and result['observed_bars']==1
    assert replay(PLAN,bars,**dict(COSTS,now=900))['exit_reason']=='TP'


def test_ambiguous_reference_is_separate_from_decisive_hold_outcomes():
    base=dict(id='a',at=100,opportunity='one',symbol='BTC_USDT',action='hold',strategy='trend',baseline=PLAN,
        model_plan=None,fee_rate=.001,slippage_bps=5,observed_spread_bps=10)
    report=compare([base],{'BTC_USDT':[[300,100,105,97,104,10]]},now=2000)
    assert report['matched']==1 and report['decisive_matched']==0 and report['ambiguous_matched']==1
    assert report['mean_decisive_delta_r'] is None
    assert report['withheld_reference_outcomes']=={'ambiguous_intrabar':1}
    result=report['results'][0]['quant']
    assert result['conservative_r']<0<result['optimistic_r']
    assert 'not proof' in report['limitations']


def test_rejected_grounded_trade_retains_model_decision_and_plan(tmp_path):
    store=RuntimeStore(str(tmp_path/'data'/'friend'/'live'/'runtime.sqlite3'))
    candidate=signal();candidate.features.update(source_open_at=0,observed_spread_bps=10)
    trace=dict(id='rejected',completed_at=100,action='hold',model_action='trade',status='invalid_model_sizing',
        market_fingerprint='m',execution_status='not_opened')
    record_review(store,trace,candidate,candidate,Settings(_env_file=None))
    report=export(tmp_path/'data',tmp_path/'out',bars={'BTC_USDT':[[300,100,105,99,104,10]]})
    row=report['results'][0]
    assert row['action']=='hold' and row['model_action']=='trade'
    assert row['decision_status']=='invalid_model_sizing'
    assert row['model']['exit_reason']=='TP'
    assert report['withheld_reference_outcomes']=={}


def test_independent_watch_reference_is_not_replayed_as_real_quant_setup():
    base=dict(id='a',at=100,opportunity='one',symbol='BTC_USDT',action='hold',strategy='watch_review',baseline=PLAN,
        model_plan=None,fee_rate=.001,slippage_bps=5,observed_spread_bps=10)
    report=compare([base],{'BTC_USDT':[[300,100,105,97,104,10]]},now=2000)
    assert report['matched']==0
    assert report['results'][0]['quant']['status']=='no_quant_baseline'
    assert report['withheld_reference_outcomes']=={'no_quant_baseline':1}


@pytest.mark.parametrize('plan',[None,dict(PLAN,side='unknown'),dict(PLAN,entry_price=True),dict(PLAN,stop_loss_price=float('nan'))])
def test_invalid_plans_cannot_become_synthetic_profitable_short(plan):
    assert replay(plan,[[300,100,105,99,104,10]],**COSTS)['status']=='invalid_plan'



def test_failed_generation_fallback_hold_is_not_a_valid_model_hold(tmp_path):
    import json
    store=RuntimeStore(str(tmp_path/'runtime.sqlite3'));candidate=signal()
    trace=dict(id='failed',completed_at=100,action='hold',status='invalid_or_unavailable_model',market_fingerprint='m')
    record_review(store,trace,candidate,None,Settings(_env_file=None))
    recorded=json.loads(store.db.execute('SELECT payload FROM edge_reviews').fetchone()[0])
    assert recorded['model_action']=='unknown' and recorded['decision_status']=='invalid_or_unavailable_model'


def test_structural_screen_is_not_a_confirmed_quant_baseline(tmp_path):
    store=RuntimeStore(str(tmp_path/'state'));candidate=signal();candidate.features.update(structure_seed=True,observed_spread_bps=10)
    trace=dict(id='seed',action='watch',model_action='watch',completed_at=100,market_fingerprint='a',setup_family='pullback')
    record_review(store,trace,candidate,None,Settings(_env_file=None))
    import json
    row=json.loads(store.db.execute('SELECT payload FROM edge_reviews').fetchone()[0])
    result=compare([row],{'BTC_USDT':[[300,100,105,99,104,10]]},now=2000)
    assert result['results'][0]['quant']['status']=='no_quant_baseline'
    assert result['groups'][0]['setup_kind']=='pullback'


def test_unknown_generation_is_never_matched_to_a_fabricated_zero_return():
    row=dict(id='unknown',at=100,opportunity='x',symbol='BTC_USDT',action='hold',model_action='unknown',strategy='trend',baseline=PLAN,
        model_plan=None,fee_rate=.001,slippage_bps=5,observed_spread_bps=10)
    result=compare([row],{'BTC_USDT':[[300,100,105,99,104,10]]},now=2000)
    assert result['matched']==0 and result['results'][0]['model']['status']=='unknown_decision'
