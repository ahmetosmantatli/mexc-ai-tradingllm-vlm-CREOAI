import pytest

from trading.runtime.runner3_policy import margin_policy,validate_economics
from trading.quant.structure_setups import build_structure_setups
from .test_structure_setups import context,view


def test_small_equity_remains_eligible_and_grade_expands_only_margin_ceiling():
    strong=margin_policy(11,11,11,'strong','intraday')
    standard=margin_policy(11,11,11,'standard','intraday')
    assert strong['target_margin_usdt']==pytest.approx(3.85)
    assert standard['target_margin_usdt']==pytest.approx(2.2)
    assert margin_policy(100,100,100,'strong','intraday')['margin_fraction']==.2
    assert margin_policy(99.99,99.99,99.99,'strong','intraday')['margin_fraction']==pytest.approx(.2,abs=.0001)
    assert margin_policy(11,11,1,'strong','intraday')['max_entry_cost_usdt']==0
    assert margin_policy(11,11,11,'strong','swing')['margin_fraction']==.25


def test_runner3_economics_rejects_small_payoff_and_expensive_entry():
    validate_economics(2.,1.8,1.)
    with pytest.raises(ValueError,match='runner3_net_target_r_too_low'):
        validate_economics(2.,1.1,1.)
    with pytest.raises(ValueError,match='runner3_cost_share_too_high'):
        validate_economics(2.,1.3,1.)


def test_runner3_countertrend_requires_valid_causal_chain():
    market=context()
    higher=view(market,'4h')
    higher['features']['trend']='bearish'
    micro=view(market,'5m')
    rows=micro['recent_ohlcv']
    rows[2][2]=115.
    micro['features']['swings'][0]['price']=115.
    rows[-3][3]=94.
    rows[-1][1:5]=[101.,104.,100.,103.]
    micro['features'].update(sweeps=[dict(direction='low_sweep',at=rows[-3][0],level=96.)],
        structure_shifts=[dict(direction='bullish',kind='mss',at=rows[-2][0])],
        fvgs=[dict(direction='bullish',at=rows[-2][0],lower=99.,upper=101.)])
    def reversal():
        return [p for p in build_structure_setups(market,current_price=103.,trigger_timeframe='5m',strict_ict=True)
                if p['family']=='countertrend_sweep_reclaim']
    assert reversal()[0]['evidence']['kind']=='sweep_mss_fvg_retest'
    rows[-1][3]=98.
    assert not reversal()


def test_runner3_reserve_rechecks_account_before_sending_ioc(tmp_path):
    from .test_live_execution import approve, proposal, setup

    from .test_live_management_execution import ManagementPublic

    executor,private,_=setup(tmp_path)
    executor.public=ManagementPublic()
    private.submit_ioc=lambda **kw:pytest.fail('order must remain unsent')
    private.assets=[dict(currency='USDT',equity=11.,availableBalance=3.,positionMargin=0.,unrealized=0.)]
    row=approve(executor,proposal(notional=10.,metadata={'runner_id':'runner3',
        'runner3_allocation':{'grade':'strong','horizon':'intraday'}}))
    from trading.brokers.live.execution import LiveExecutionError
    with pytest.raises(LiveExecutionError,match='runner3_margin_reserve'):
        executor.execute(row['intent_id'])
    assert not executor.state(row['intent_id'])['entry_write_attempted']


@pytest.mark.parametrize(('side', 'stop', 'target'), [
    ('long', 99., 101.65),
    ('short', 101., 98.35),
])
def test_ioc_economics_at_executable_limit(side, stop, target):
    from trading.runtime.runner3_policy import ioc_target_economics

    gross, net, loss = ioc_target_economics(
        side=side, entry=100., stop=stop, target=target, fee=.0006, slippage=.0005)
    assert gross == pytest.approx(1.65)
    assert 1.25 < net / loss < 1.3
    validate_economics(gross, net, loss)


def test_quantity_rejection_preserves_exchange_constraints_for_audit():
    from trading.exchange.contracts import ContractRules
    from trading.runtime.execution_diagnostics import safe_execution_error

    rules = ContractRules(1., 1., 2., 100., 10., .005, .01, 50.)
    with pytest.raises(ValueError) as error:
        rules.size_order(19., 10., 5.)
    detail = safe_execution_error(error.value)
    assert detail['code'] == 'CONTRACT_QUANTITY_LIMIT'
    assert detail['contracts'] == {'quantity': 1., 'minimum': 2., 'maximum': 100., 'step': 1.}


def test_evidence_risk_requires_repeatable_net_results_and_limits_the_bonus():
    from trading.runtime.runner3_policy import COHORT, evidence_risk_budget, evidence_risk_groups

    now = 1800000000.
    rows = []
    segment = ('trend_continuation', 'intraday', 'bearish', 'text')
    for age in (10, 40):
        for grade, pnl in [('strong', .2), ('standard', .1)]:
            for i in range(10):
                rows.append(dict(status='closed', realized_complete=True, entry_fill_price=100.,
                    closed_at=(now-age*86400-i)*1000, net_pnl=pnl,
                    entry_metrics={'initial_stop_risk_usdt': .2},
                    proposal={'metadata': dict(runner_id='runner3', strategy_cohort=COHORT,
                        setup_family=segment[0], analysis_version='text',
                        runner3_allocation=dict(grade=grade, horizon='intraday', macro_trend='bearish'))}))
    comparison = evidence_risk_groups(rows, now)[segment]
    assert comparison['eligible']
    assert comparison['prior_30_days']['strong_mean_net_r'] == pytest.approx(1.)
    assert comparison['recent_30_days']['standard_mean_net_r'] == pytest.approx(.5)
    assert not evidence_risk_groups(rows[:-1], now)[segment]['eligible']
    rows[-1]['proposal']['metadata']['runner3_allocation']['risk_boost_applied'] = True
    assert not evidence_risk_groups(rows, now)[segment]['eligible']

    def budget(model=.015, ceiling=.03, grade='strong', same_side=False):
        return evidence_risk_budget(model, ceiling, grade=grade, comparison=comparison, same_side_open=same_side)

    assert budget() == pytest.approx(.0175)
    assert budget(model=.004) == pytest.approx(.005)
    assert budget(ceiling=.016) == pytest.approx(.016)
    assert budget(grade='standard') == .015
    assert budget(same_side=True) == .015
    assert evidence_risk_budget(.015, .03, grade='strong', comparison=None, same_side_open=False) == .015


def test_price_drift_diagnostics_capture_stage_and_signed_move():
    from trading.runtime.execution_diagnostics import EntryPriceDrift, safe_execution_error

    error = EntryPriceDrift('live entry price drift', stage='planning',
                            planned=100., observed=99.6, tolerance=.003)
    detail = safe_execution_error(error)
    assert detail['code'] == 'ENTRY_PRICE_DRIFT'
    assert detail['price_drift'] == pytest.approx(dict(
        stage='planning', planned=100., observed=99.6, deviation=-.004, tolerance=.003))
