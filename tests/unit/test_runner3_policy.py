import pytest

from trading.runtime.runner3_policy import margin_policy,validate_economics
from trading.quant.structure_setups import build_structure_setups
from .test_structure_setups import context,view


def test_small_equity_margin_target_is_35_percent_strong_and_20_percent_standard():
    strong=margin_policy(11,11,11,'strong','intraday')
    standard=margin_policy(11,11,11,'standard','intraday')
    assert strong['margin_fraction']==.35
    assert standard['margin_fraction']==.20
    assert strong['target_margin_usdt']==pytest.approx(11*.35)
    assert standard['target_margin_usdt']==pytest.approx(11*.20)
    # The share is equity, not a smaller allocated capital figure.
    assert margin_policy(50,40,50,'strong','swing')['target_margin_usdt']==pytest.approx(17.5)
    assert margin_policy(50,40,50,'standard','scalp')['target_margin_usdt']==pytest.approx(10)
    assert margin_policy(100,100,100,'strong','intraday')['margin_fraction']==.2
    assert margin_policy(100,100,100,'standard','intraday')['margin_fraction']==.1
    assert margin_policy(99.99,40,99.99,'strong','intraday')['margin_fraction']==.35
    assert margin_policy(99.99,40,99.99,'standard','intraday')['target_margin_usdt']==pytest.approx(99.99*.20)
    assert margin_policy(11,11,1,'strong','intraday')['max_entry_cost_usdt']==0
    assert margin_policy(11,11,11,'strong','swing')['margin_fraction']==.35


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


def test_margin_under_one_usdt_is_rejected_while_equity_is_under_100():
    from trading.runtime.runner3_policy import leverage_for_choch_loss

    common = dict(notional=.8, entry=100., stop=98., free_margin=50., max_leverage=20,
                  maintenance=.005, initial_rate=.01, fee=.0006, liquidation_buffer=.02)
    with pytest.raises(ValueError, match='runner3_margin_below_minimum'):
        leverage_for_choch_loss(equity=40., **common)
    leverage, margin, size = leverage_for_choch_loss(equity=100., **common)
    assert size == pytest.approx(.8)
    assert margin < 1
    assert leverage == 1


def test_evidence_grade_is_strong_only_for_the_two_confirmed_patterns():
    from trading.runtime.runner3_policy import evidence_grade

    sweep=dict(evidence=dict(kind='sweep_mss_fvg_retest'), reward_risk=1.1, macro_trend='bearish', side='long')
    closed_break=dict(evidence=dict(kind='closed_bos'), reward_risk=2, macro_trend='bullish', side='long')
    short_break=dict(evidence=dict(kind='closed_bos'), reward_risk=2.4, macro_trend='bearish', side='short')
    zone_break=dict(evidence=dict(kind='htf_zone_choch', context_last_break=dict(direction='bullish')),
                    reward_risk=2, macro_trend='bullish', side='long')
    zone_mss=dict(evidence=dict(kind='htf_zone_mss'), reward_risk=3, macro_trend='bullish', side='long')
    zone_choch=dict(evidence=dict(kind='htf_zone_choch'), reward_risk=3, macro_trend='bullish', side='long')
    low_payoff=dict(evidence=dict(kind='closed_bos'), reward_risk=1.99, macro_trend='bullish', side='long')
    opposing=dict(evidence=dict(kind='htf_zone_mss', context_last_break=dict(direction='bearish')),
                  reward_risk=3, macro_trend='bullish', side='long')
    counter_break=dict(evidence=dict(kind='closed_bos'), reward_risk=3, macro_trend='bearish', side='long')
    assert evidence_grade(sweep)=='strong'
    assert evidence_grade(closed_break)=='strong'
    assert evidence_grade(short_break)=='strong'
    assert evidence_grade(zone_break)=='strong'
    assert evidence_grade(zone_mss)=='standard'
    assert evidence_grade(zone_choch)=='standard'
    assert evidence_grade(low_payoff)=='standard'
    assert evidence_grade(opposing)=='standard'
    assert evidence_grade(counter_break)=='standard'


def test_both_shares_stay_inside_the_margin_target_and_the_choch_loss_ceiling():
    from trading.runtime.runner3_policy import leverage_for_choch_loss

    common=dict(entry=100., stop=98., free_margin=40., equity=50., max_leverage=20,
                maintenance=.005, initial_rate=.01, fee=.0006, liquidation_buffer=.02)
    standard_leverage, standard_margin, standard_size=leverage_for_choch_loss(
        notional=20., margin_target=10., **common)
    strong_leverage, strong_margin, strong_size=leverage_for_choch_loss(
        notional=30., margin_target=17.5, **common)
    assert standard_size==pytest.approx(20.)
    assert standard_leverage==3
    assert standard_margin==pytest.approx(20./3)
    assert standard_margin<=10
    assert strong_size==pytest.approx(30.)
    assert strong_leverage==2
    assert strong_margin==pytest.approx(15.)
    assert strong_margin<=17.5
    assert strong_size>standard_size
    assert standard_leverage not in (10, 20)
    assert strong_leverage not in (10, 20)


def test_a_margin_share_that_cannot_hold_the_loss_notional_does_not_use_max_leverage():
    from trading.runtime.runner3_policy import leverage_for_choch_loss

    leverage, margin, size=leverage_for_choch_loss(
        notional=100., entry=100., stop=98., free_margin=40., margin_target=2., equity=50.,
        max_leverage=20, maintenance=.005, initial_rate=.01, fee=.0006, liquidation_buffer=.02)
    assert leverage==1
    assert size==pytest.approx(2/1.0006)
    assert margin+size*.0006<=2+1e-8
    assert leverage!=20


def test_both_shares_reject_margin_under_one_usdt():
    from trading.runtime.runner3_policy import leverage_for_choch_loss

    common=dict(notional=8., entry=100., stop=98., free_margin=30., equity=40., max_leverage=20,
                maintenance=.005, initial_rate=.01, fee=.0006, liquidation_buffer=.02)
    for target in (.20*40*.05, .35*40*.05):
        assert target<1
        with pytest.raises(ValueError, match='runner3_margin_below_minimum'):
            leverage_for_choch_loss(margin_target=target, **common)


def test_choch_loss_uses_free_margin_instead_of_a_default_or_maximum_leverage():
    from trading.runtime.runner3_policy import leverage_for_choch_loss

    leverage, margin, size = leverage_for_choch_loss(
        notional=20., entry=100., stop=97., free_margin=12., equity=40., max_leverage=20,
        maintenance=.005, initial_rate=.01, fee=.0006, liquidation_buffer=.02)
    assert size == pytest.approx(20.)
    assert leverage == 2
    assert margin == pytest.approx(10.)
    assert leverage not in (10, 20)


def test_small_price_drift_inside_the_choch_risk_still_enters():
    from trading.runtime.runner3_policy import drift_respects_choch_risk, ioc_target_economics

    fee, slip = .0006, .0005
    quantity = 2.
    budget = ioc_target_economics(side='long', entry=100.25, stop=98., target=110., fee=fee, slippage=slip)[2] * quantity
    assert drift_respects_choch_risk(
        side='long', fresh_entry=100.25, stop=98., target=110., choch_extreme=98.1,
        quantity=quantity, risk_budget=budget, fee=fee, slippage=slip)
    short_budget = ioc_target_economics(side='short', entry=99.75, stop=102., target=90., fee=fee, slippage=slip)[2] * quantity
    assert drift_respects_choch_risk(
        side='short', fresh_entry=99.75, stop=102., target=90., choch_extreme=101.9,
        quantity=quantity, risk_budget=short_budget, fee=fee, slippage=slip)


def test_drifted_price_that_blows_the_loss_budget_does_not_enter():
    from trading.runtime.runner3_policy import drift_respects_choch_risk, ioc_target_economics

    fee, slip = .0006, .0005
    quantity = 2.
    budget = ioc_target_economics(side='long', entry=100.25, stop=98., target=110., fee=fee, slippage=slip)[2] * quantity
    assert not drift_respects_choch_risk(
        side='long', fresh_entry=102.5, stop=98., target=110., choch_extreme=98.1,
        quantity=quantity, risk_budget=budget, fee=fee, slippage=slip)
    assert not drift_respects_choch_risk(
        side='long', fresh_entry=100.1, stop=98., target=110., choch_extreme=97.9,
        quantity=quantity, risk_budget=budget * 10, fee=fee, slippage=slip)


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


def test_margin_under_one_usdt_is_rejected_before_the_order_is_sent(tmp_path):
    from .test_live_execution import approve, proposal, setup
    from .test_live_management_execution import ManagementPublic

    executor, private, _ = setup(tmp_path)
    executor.public = ManagementPublic()
    private.leverage_value = 10
    private.submit_ioc = lambda **kw: pytest.fail('order must remain unsent')
    private.assets = [dict(currency='USDT', equity=11., availableBalance=11., positionMargin=0., unrealized=0.)]
    row = approve(executor, proposal(notional=8., leverage=10, max_loss_if_stopped=3.,
        metadata={'runner_id': 'runner3', 'runner3_allocation': {'grade': 'strong', 'horizon': 'swing'}}))
    from trading.brokers.live.execution import LiveExecutionError
    with pytest.raises(LiveExecutionError, match='runner3_margin_below_minimum'):
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


def test_small_account_stop_budget_applies_immediately_below_threshold():
    from trading.runtime.runner3_policy import small_account_risk_target

    assert small_account_risk_target(9.45, .03, 'strong') == .03
    assert small_account_risk_target(9.45, .03, 'standard') == pytest.approx(.024)
    assert small_account_risk_target(99.99, .015, 'strong') == .015
    assert small_account_risk_target(100., .03, 'strong') is None


def test_price_drift_diagnostics_capture_stage_and_signed_move():
    from trading.runtime.execution_diagnostics import EntryPriceDrift, safe_execution_error

    error = EntryPriceDrift('live entry price drift', stage='planning',
                            planned=100., observed=99.6, tolerance=.003)
    detail = safe_execution_error(error)
    assert detail['code'] == 'ENTRY_PRICE_DRIFT'
    assert detail['price_drift'] == pytest.approx(dict(
        stage='planning', planned=100., observed=99.6, deviation=-.004, tolerance=.003))
