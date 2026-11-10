import pytest

from trading.runtime.runner3_policy import margin_policy,validate_economics
from trading.quant.structure_setups import build_structure_setups
from .test_structure_setups import context,view


def test_small_equity_silent_fallback_is_35_percent_and_standard_stays_20_percent():
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


def test_text_model_share_replaces_the_guclu_fallback_and_invalid_does_not_grow():
    from trading.runtime.runner3_policy import apply_guclu_margin_share, margin_policy

    strong = margin_policy(50, 40, 50, 'strong', 'swing')
    chosen = apply_guclu_margin_share(strong, equity=50, available=50, raw_share=.22)
    assert chosen['margin_fraction'] == pytest.approx(.22)
    assert chosen['target_margin_usdt'] == pytest.approx(11)
    assert chosen['margin_share_source'] == 'model'
    assert chosen['max_entry_cost_usdt'] == pytest.approx(11)
    for raw in (None, 'high', True, 0, 1.2, float('nan'), float('inf')):
        fallback = apply_guclu_margin_share(strong, equity=50, available=50, raw_share=raw)
        assert fallback['margin_fraction'] == .35
        assert fallback['target_margin_usdt'] == pytest.approx(17.5)
        assert fallback['margin_share_source'] == 'fallback_35'
    standard = apply_guclu_margin_share(
        margin_policy(50, 50, 50, 'standard', 'intraday'), equity=50, available=50, raw_share=.5)
    assert standard['margin_fraction'] == .20
    assert standard['target_margin_usdt'] == pytest.approx(10)
    assert 'margin_share_source' not in standard
    large = apply_guclu_margin_share(
        margin_policy(150, 150, 150, 'strong', 'intraday'), equity=150, available=150, raw_share=.5)
    assert large['margin_fraction'] == .20
    assert large['target_margin_usdt'] == pytest.approx(30)


def test_guclu_packet_carries_the_label_evidence_and_account_goal():
    from trading.runtime.runner3_policy import GUCLU_MARGIN_INSTRUCTION, guclu_margin_request

    sweep = dict(evidence=dict(kind='sweep_mss_fvg_retest'), reward_risk=1.1,
                 macro_trend='bearish', side='long', context_timeframe='4h', horizon='swing')
    closed = dict(evidence=dict(kind='htf_zone_choch', context_last_break=dict(direction='bearish')),
                  reward_risk=2.4, macro_trend='bearish', side='short', context_timeframe='1h', horizon='intraday')
    standard = dict(evidence=dict(kind='htf_zone_mss'), reward_risk=3,
                    macro_trend='bullish', side='long', context_timeframe='4h', horizon='swing')
    positions = [dict(symbol='ETH_USDT', side='short')]
    packet = guclu_margin_request(sweep, equity=11.5, free_margin=8.2, open_positions=positions, loss_ceiling_usdt=0.34)
    assert packet['label'] == 'Güçlü'
    assert packet['evidence'] == 'sweep+MSS+FVG retest'
    assert packet['free_margin_usdt'] == 8.2
    assert packet['equity_usdt'] == 11.5
    assert packet['open_positions'] == positions
    assert packet['open_positions'] is not positions
    assert packet['choch_loss_ceiling_usdt'] == 0.34
    assert packet['account_goal'] == 'grow the cash'
    assert packet['time_horizon'] == '4h swing'
    intraday = guclu_margin_request(closed, equity=40, free_margin=20, open_positions=[], loss_ceiling_usdt=0.4)
    assert intraday['evidence'] == 'with-trend closed break and at least 2R'
    assert intraday['time_horizon'] == '1h intraday'
    assert guclu_margin_request(standard, equity=40, free_margin=20, open_positions=[], loss_ceiling_usdt=0.4) is None
    assert '20%' in GUCLU_MARGIN_INSTRUCTION
    assert '35%' in GUCLU_MARGIN_INSTRUCTION
    assert 'Güçlü is not a fixed percent' in GUCLU_MARGIN_INSTRUCTION
    assert '35% for Güçlü' not in GUCLU_MARGIN_INSTRUCTION


def test_text_share_wins_over_a_later_sizing_plan():
    from trading.runtime.runner3_policy import chosen_margin_share

    assert chosen_margin_share({'guclu_margin_share': None, 'sizing_plan': {'margin_share': .9}}) is None
    assert chosen_margin_share({'guclu_margin_share': .22, 'sizing_plan': {'margin_share': .9}}) == .22
    assert chosen_margin_share({'sizing_plan': {'margin_share': .22}}) == .22
    assert chosen_margin_share({'sizing_plan': {}}) is None


def test_entry_gate_prompt_states_the_engine_formula_and_does_not_lower_it():
    from inspect import signature
    import json

    from trading.brokers.live.execution import ApprovedLiveExecutor
    from trading.config import Settings
    from trading.evaluation.context import COMPACT_SYSTEM, compact_decision_packet, system_with_entry_gate
    from trading.runtime.runner3_policy import (
        AGGRESSIVE_STOP_RISK_CAP, CONTINUOUS_STOP_RISK_CAP, MAX_COST_SHARE, MIN_NET_TARGET_R,
        entry_gate_instruction, prompt_entry_costs, validate_economics)

    defaults = signature(ApprovedLiveExecutor.__init__)
    fee = defaults.parameters['fee_rate'].default
    slip_bps = defaults.parameters['slippage_bps'].default
    slippage = slip_bps / 10000
    assert fee == Settings.model_fields['paper_fee_rate'].default == 0.0006
    assert slip_bps == Settings.model_fields['paper_slippage_bps'].default == 5
    assert MIN_NET_TARGET_R == 1.25
    assert CONTINUOUS_STOP_RISK_CAP == .15
    assert AGGRESSIVE_STOP_RISK_CAP == .10
    assert MAX_COST_SHARE == .30
    validate_economics(1.5, 1.25, 1.)
    with pytest.raises(ValueError, match='runner3_net_target_r_too_low'):
        validate_economics(1.5, 1.249, 1.)

    class Costs:
        paper_fee_rate = 0.0006
        paper_slippage_bps = 5

    assert prompt_entry_costs(Costs()) == (0.0006, 0.0005)
    assert prompt_entry_costs(Costs(), {'fee': 0.001, 'slippage': 0.001}) == (0.001, 0.001)
    assert prompt_entry_costs(Costs(), {'fee': True, 'slippage': 0.001}) == (0.0006, 0.0005)

    text = entry_gate_instruction(fee=fee, slippage=slippage)
    for line in (
        'Runner 3 minimum cost-adjusted R is 1.25.',
        'Fee assumption is 0.0006 per side. Exit-slippage assumption is 0.0005 (5 bps), applied to the stop and the target.',
        'direction is 1 for long and -1 for short.',
        'stop_fill = stop * (1 - direction * slippage).',
        'target_fill = target * (1 - direction * slippage).',
        'loss = abs(entry - stop_fill) + fee * (entry + stop_fill).',
        'net = direction * (target_fill - entry) - fee * (entry + target_fill).',
        'cost_adjusted_R = net / loss.',
        'gross = abs(target - entry).',
        'The engine rejects the proposal when loss <= 0 or cost_adjusted_R < 1.25, and when gross <= 0 or (gross - net) / gross > 0.30.',
        'The continuous equity stop cap is 15% of equity. One stop cannot lose more than that fraction of equity.',
        'When continuous aggressive is off, aggressive model sizing uses the 10% equity stop cap.',
        'When the setup is real (zone, touch, and a later closed CHoCH/MSS), first choose a farther target that is one of the supplied opposing target levels and whose cost-adjusted R clears 1.25.',
        'BEKLE (action hold) only when none of the supplied levels clears 1.25.',
        'Do not move the stop to manufacture R. Do not invent a level that was not supplied.',
        'Do not invent a trade when you are silent.',
    ):
        assert line in text
    assert 'must be BEKLE (action hold) or a farther target' not in text
    assert text.index('first choose a farther target') < text.index('BEKLE (action hold) only when none')
    compact_system = system_with_entry_gate(COMPACT_SYSTEM, 'full policy\n' + text + '\nmore')
    assert 'cost_adjusted_R = net / loss.' in compact_system
    assert 'The continuous equity stop cap is 15% of equity.' in compact_system
    assert system_with_entry_gate(COMPACT_SYSTEM, 'no gate here') == COMPACT_SYSTEM
    raw = json.dumps(dict(context_policy='pa-context-v4', account=dict(runner3_entry_gate=text, equity=10),
                          market=dict(symbol='LAB_USDT')), separators=(',', ':'))
    packet = json.loads(compact_decision_packet(raw))
    assert packet['entry_gates'] == text
    assert packet['symbol'] == 'LAB_USDT'


def test_a_farther_supplied_target_is_allowed_and_the_floor_stays():
    from time import time

    from trading.evaluation.price_plan import CandleReference, MarketPricePlan
    from trading.runtime.runner3_policy import MIN_NET_TARGET_R, validate_evidence

    assert MIN_NET_TARGET_R == 1.25
    near = dict(timeframe='4h', open_time=2, field='high', price=110.)
    far = dict(timeframe='4h', open_time=3, field='high', price=130.)
    stop = CandleReference(timeframe='15m', open_time=1, field='low')
    setup = dict(
        side='long', state='ready', context_timeframe='4h', trigger_timeframe='15m',
        evidence=dict(kind='htf_zone_mss', stage='confirmed', expires_at=time() + 60),
        stop_loss_price=90., stop_reference=stop.model_dump(),
        target_reference={key: near[key] for key in ('timeframe', 'open_time', 'field')},
        opposing_target_levels=[near, far])
    plan = MarketPricePlan(
        entry_type='market', entry_price=100, stop_loss_price=90, take_profit_price=130,
        entry_timeframe='15m', stop_reference=stop,
        target_reference=CandleReference(timeframe='4h', open_time=3, field='high'),
        reason='Daha uzak verilen seviye maliyeti aşıyor.')
    validate_evidence(setup, plan)
    invented = plan.model_copy(update={
        'target_reference': CandleReference(timeframe='4h', open_time=9, field='high')})
    with pytest.raises(ValueError, match='runner3_target_reference_mismatch'):
        validate_evidence(setup, invented)
    with pytest.raises(ValueError, match='runner3_structural_stop_too_tight'):
        validate_evidence(setup, plan.model_copy(update={'stop_loss_price': 95}))
    validate_economics(1.5, 1.25, 1.)
    with pytest.raises(ValueError, match='runner3_net_target_r_too_low'):
        validate_economics(1.5, 1.249, 1.)
