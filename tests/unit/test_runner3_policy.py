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
