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
        'Price-action playbook, before you answer:',
        'High structure is 4h with a 15m trigger. Low structure is 1h with a 5m trigger. Both directions use the same rule.',
        'The stop is the CHoCH swing. A short uses the CHoCH high. A long uses the CHoCH low. Do not move it.',
        'When the trade is against the higher timeframe, target only the next opposing block or flow on the trigger timeframe. Do not hold that trade toward a far higher-timeframe target.',
        'When the trade is with the higher timeframe, a farther target is allowed. With the higher timeframe means the higher timeframe is long, price reached a block or flow, and the trigger CHoCH turns up, or the short mirror.',
        'A hold is not the default when the CHoCH, the block or flow, and a target exist.',
        'Free margin must cover the order. You choose the balance share.',
        'sizing_plan.risk_per_trade_pct is required. Set it from the account equity and the account target, inside the 15% cap.',
        'Runner 3 minimum cost-adjusted R is 1.25.',
        'Fee assumption is 0.0006 per side. Exit-slippage assumption is 0.0005 (5 bps), applied to the stop and the target.',
        'direction is 1 for long and -1 for short.',
        'stop_fill = stop * (1 - direction * slippage).',
        'target_fill = target * (1 - direction * slippage).',
        'loss = abs(entry - stop_fill) + fee * (entry + stop_fill).',
        'net = direction * (target_fill - entry) - fee * (entry + target_fill).',
        'cost_adjusted_R = net / loss.',
        'gross = abs(target - entry).',
        'The engine rejects the proposal when loss <= 0, and when gross <= 0 or (gross - net) / gross > 0.30.',
        'The continuous equity stop cap is 15% of equity. One stop cannot lose more than that fraction of equity.',
        'When continuous aggressive is off, aggressive model sizing uses the 10% equity stop cap.',
        'When the setup is real (zone, touch, and a closed CHoCH/MSS) and at least one supplied opposing target has cost-adjusted R that clears 1.25, you must ENTER (action trade) at that supplied target.',
        'When 1.25 would otherwise block, you may choose a farther supplied opposing target you judge has more potential. That choice stands.',
        'If your action is trade and the target you name is one of the supplied opposing levels, the engine does not reject that choice for cost-adjusted R.',
        'Balance, free margin, and leverage (the multiplier) stay in the packet so you can size. They are not BEKLE reasons.',
        'These are not BEKLE reasons: account balance, free margin, leverage, open positions, a higher-timeframe trend that disagrees, consolidation or a correction, or "watch already exists".',
        'When the supplied setup already has a closed CHoCH/MSS, an unbroken higher timeframe is not missing confirmation.',
        'The 15% equity stop cap still applies. Do not move the stop.',
        'A silent or invalid answer opens nothing. When the selected setup is real and you hold, the engine enters the farthest supplied opposing target that clears 1.25 and keeps the stop. A hold with no such level stays a hold.',
        'Do not move the stop to manufacture R. Do not invent a level that was not supplied.',
        'Do not invent a trade when you are silent.',
    ):
        assert line in text
    assert 'must be BEKLE (action hold) or a farther target' not in text
    assert 'first choose a farther target' not in text
    assert 'BEKLE (action hold) only when none of the supplied levels clears 1.25.' not in text
    assert 'BEKLE (action hold) only when confirmation is missing' not in text
    assert 'cost_adjusted_R < 1.25' not in text
    assert 'do not replace the cost-adjusted R check' not in text
    assert 'If you BEKLE because no supplied level clears 1.25' not in text
    assert text.index('Price-action playbook, before you answer:') < text.index('Runner 3 minimum cost-adjusted R is 1.25.')
    assert text.index('you must ENTER (action trade)') < text.index('would otherwise block')
    assert text.index('would otherwise block') < text.index('That choice stands')
    assert text.index('That choice stands') < text.index('does not reject that choice for cost-adjusted R')
    assert text.index('Balance, free margin, and leverage (the multiplier) stay in the packet') < text.index('Do not move the stop to manufacture R')
    assert text.index('These are not BEKLE reasons') < text.index('Do not move the stop to manufacture R')
    compact_system = system_with_entry_gate(COMPACT_SYSTEM, 'full policy\n' + text + '\nmore')
    assert 'cost_adjusted_R = net / loss.' in compact_system
    assert 'The continuous equity stop cap is 15% of equity.' in compact_system
    assert system_with_entry_gate(COMPACT_SYSTEM, 'no gate here') == COMPACT_SYSTEM
    raw = json.dumps(dict(context_policy='pa-context-v4', account=dict(runner3_entry_gate=text, equity=10),
                          market=dict(symbol='LAB_USDT')), separators=(',', ':'))
    packet = json.loads(compact_decision_packet(raw))
    assert packet['gates']['cost_adjusted_r'] == 1.25
    assert packet['gates']['equity_stop'] == '15%'
    assert 'free_margin' in packet['gates']
    assert packet['symbol'] == 'LAB_USDT'
    assert 'stop_fill' not in compact_decision_packet(raw)


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


def test_a_model_chosen_supplied_target_is_not_rejected_for_r():
    from time import time

    from trading.evaluation.price_plan import CandleReference, MarketPricePlan
    from trading.runtime.runner3_policy import (
        CONTINUOUS_STOP_RISK_CAP, MIN_NET_TARGET_R, ioc_target_economics,
        order_names_supplied_target, validate_economics)

    assert MIN_NET_TARGET_R == 1.25
    assert CONTINUOUS_STOP_RISK_CAP == .15
    near = dict(timeframe='4h', open_time=2, field='high', price=110.)
    far = dict(timeframe='4h', open_time=3, field='high', price=130.)
    stop = CandleReference(timeframe='15m', open_time=1, field='low')
    setup = dict(
        side='long', state='ready', context_timeframe='4h', trigger_timeframe='15m',
        evidence=dict(
            kind='htf_zone_mss', stage='confirmed', expires_at=time() + 60,
            zone=dict(timeframe='4h', direction='bullish', low=95., high=99., open_time=1, confirmed_at=2),
            touch_at=10., shift=dict(kind='mss', at=11, direction='bullish', level=100.),
            confirmation_at=12.),
        stop_loss_price=90., stop_reference=stop.model_dump(),
        target_reference={key: near[key] for key in ('timeframe', 'open_time', 'field')},
        opposing_target_levels=[near, far])
    far_plan = MarketPricePlan(
        entry_type='market', entry_price=100, stop_loss_price=90, take_profit_price=105,
        entry_timeframe='15m', stop_reference=stop,
        target_reference=CandleReference(timeframe='4h', open_time=3, field='high'),
        reason='Daha uzak verilen seviyede potansiyel var.')
    gross, net, loss = ioc_target_economics(
        side='long', entry=100, stop=90, target=105, fee=0.0006, slippage=0.0005)
    assert loss > 0 and net / loss < MIN_NET_TARGET_R
    assert (gross - net) / gross <= 0.30
    assert order_names_supplied_target(setup, far_plan, stop=90, target=105) is True
    validate_economics(gross, net, loss, supplied_target=True)
    with pytest.raises(ValueError, match='runner3_net_target_r_too_low'):
        validate_economics(gross, net, loss)
    with pytest.raises(ValueError, match='runner3_net_target_r_too_low'):
        validate_economics(1.0, 0.9, 0, supplied_target=True)
    with pytest.raises(ValueError, match='runner3_cost_share_too_high'):
        validate_economics(1.0, 0.5, 1.0, supplied_target=True)

    near_plan = far_plan.model_copy(update={
        'take_profit_price': 110,
        'target_reference': CandleReference(timeframe='4h', open_time=2, field='high')})
    assert order_names_supplied_target(setup, near_plan, stop=90, target=110) is True
    invented = far_plan.model_copy(update={
        'target_reference': CandleReference(timeframe='4h', open_time=9, field='high')})
    assert order_names_supplied_target(setup, invented, stop=90, target=105) is False
    moved_stop = far_plan.model_copy(update={'stop_loss_price': 95})
    assert order_names_supplied_target(setup, moved_stop, stop=95, target=105) is False
    wider_stop = far_plan.model_copy(update={'stop_loss_price': 85})
    assert order_names_supplied_target(setup, wider_stop, stop=85, target=105) is False
    assert order_names_supplied_target(setup, far_plan, stop=95, target=105) is False
    assert order_names_supplied_target(setup, far_plan, stop=90, target=130) is False
    assert order_names_supplied_target(setup, far_plan, stop=90, target=105, action='hold') is False
    assert order_names_supplied_target(setup, None, stop=90, target=105) is False
    for broken in (
            dict(setup, evidence=dict(setup['evidence'], zone=None)),
            dict(setup, evidence=dict(setup['evidence'], shift=None)),
            dict(setup, evidence=dict(setup['evidence'], shift=dict(kind='bos', at=11))),
            dict(setup, evidence=dict(setup['evidence'], stage='awaiting_touch')),
            dict(setup, state='watch')):
        assert order_names_supplied_target(broken, far_plan, stop=90, target=105) is False
    missing_touch = dict(setup, evidence=dict(setup['evidence']))
    missing_touch['evidence'].pop('touch_at')
    assert order_names_supplied_target(missing_touch, far_plan, stop=90, target=105) is False


def test_a_hold_keeps_only_the_farthest_supplied_level_that_clears_the_floor():
    from types import SimpleNamespace

    from trading.runtime.runner3_policy import (
        MIN_NET_TARGET_R, clearing_supplied_targets, cost_adjusted_r, selected_closed_setup)

    near = dict(timeframe='4h', open_time=2, field='high', price=110.)
    mid = dict(timeframe='4h', open_time=4, field='high', price=140.)
    far = dict(timeframe='4h', open_time=3, field='high', price=160.)
    setup = dict(
        setup_id='ready-long', side='long', state='ready',
        evidence=dict(
            kind='htf_zone_mss', stage='confirmed',
            zone=dict(low=95., high=99.), touch_at=10.,
            shift=dict(kind='mss', at=11)),
        stop_loss_price=90.,
        opposing_target_levels=[near, mid, far])
    fee, slip = 0.0006, 0.0005
    assert cost_adjusted_r(side='long', entry=100, stop=90, target=110, fee=fee, slippage=slip) < MIN_NET_TARGET_R
    assert cost_adjusted_r(side='long', entry=100, stop=90, target=160, fee=fee, slippage=slip) >= MIN_NET_TARGET_R
    found = clearing_supplied_targets(setup, entry=100, fee=fee, slippage=slip, price_step=0)
    assert [row['take_profit_price'] for row in found] == [160., 140.]
    assert all(row['price'] in (140., 160.) for row in found)
    assert clearing_supplied_targets(
        dict(setup, opposing_target_levels=[near]), entry=100, fee=fee, slippage=slip) == []
    invented = dict(timeframe='4h', open_time=9, field='high', price=200.)
    assert invented['price'] not in {
        row['take_profit_price'] for row in clearing_supplied_targets(
            setup, entry=100, fee=fee, slippage=slip)}
    bare = dict(setup, evidence=dict(setup['evidence'], zone=None))
    assert clearing_supplied_targets(bare, entry=100, fee=fee, slippage=slip) == []

    short_near = dict(timeframe='4h', open_time=2, field='low', price=90.)
    short_far = dict(timeframe='4h', open_time=3, field='low', price=70.)
    short = dict(setup, side='short', stop_loss_price=110.,
                 opposing_target_levels=[short_near, short_far])
    short_found = clearing_supplied_targets(short, entry=100, fee=fee, slippage=slip)
    assert short_found[0]['take_profit_price'] == 70.
    assert cost_adjusted_r(side='short', entry=100, stop=110, target=90, fee=fee, slippage=slip) < MIN_NET_TARGET_R

    chosen = SimpleNamespace(features={'setup_id': 'ready-long'})
    assert selected_closed_setup(chosen, [setup])['setup_id'] == 'ready-long'
    assert selected_closed_setup(SimpleNamespace(features={}), [setup]) is None
    assert selected_closed_setup(SimpleNamespace(features={'setup_id': 'other'}), [setup]) is None
    assert selected_closed_setup(chosen, [dict(setup, state='watch')]) is None


def test_entry_veto_names_the_failed_check_and_reentry_uses_the_current_price():
    from trading.runtime.runner3_policy import entry_veto, priced_reentry

    fee, slip = 0.0006, 0.0005
    passed = dict(side='long', entry=100, stop=98, target=105, equity=100, free_margin=50,
                  fee=fee, slippage=slip, notional=10, leverage=5)
    assert entry_veto(**passed) is None
    assert entry_veto(**dict(passed, target=100.4)) == 'cost_adjusted_r'
    assert entry_veto(**dict(passed, target=100.2, free_margin=1, notional=100)) == 'cost_adjusted_r'
    assert entry_veto(side='long', entry=100, stop=80, target=150, equity=100, free_margin=100,
                      fee=fee, slippage=slip, notional=100, leverage=1) == 'equity_stop'
    assert entry_veto(side='long', entry=100, stop=99, target=110, equity=1000, free_margin=10,
                      fee=fee, slippage=slip, notional=40, leverage=1) == 'free_margin'

    near = dict(timeframe='15m', open_time=2, field='high', price=105., kind='block')
    far = dict(timeframe='4h', open_time=3, field='high', price=130., kind='block')
    setup = dict(side='long', stop_loss_price=90, take_profit_price=120,
                 entry_reference=100, opposing_target_levels=[dict(price=120, kind='block')],
                 evidence=dict(alignment='against_higher_timeframe'))
    prices, failed = priced_reentry(setup, price=96, equity=100, free_margin=50, fee=fee, slippage=slip)
    assert failed is None
    assert prices['entry'] == 96 and prices['stop'] == 90 and prices['target'] == 120
    _prices, failed = priced_reentry(
        dict(setup, take_profit_price=101, opposing_target_levels=[dict(price=101, kind='block')]),
        price=100, equity=100, free_margin=50, fee=fee, slippage=slip)
    assert failed == 'cost_adjusted_r'
    _prices, failed = priced_reentry(
        dict(setup, stop_loss_price=80, take_profit_price=150,
             opposing_target_levels=[dict(price=150, kind='block')]),
        price=100, equity=100, free_margin=100, fee=fee, slippage=slip, notional=100, leverage=1)
    assert failed == 'equity_stop'
    # Against the higher timeframe the next block wins. With it, the farther supplied target wins.
    against = dict(setup, stop_loss_price=98, take_profit_price=105, opposing_target_levels=[near, far])
    against_prices, against_failed = priced_reentry(
        against, price=100, equity=1000, free_margin=1000, fee=fee, slippage=slip, notional=10, leverage=5)
    assert against_failed is None and against_prices['target'] == 105
    assert against_prices['target_reference']['open_time'] == 2
    with_prices, with_failed = priced_reentry(
        dict(against, evidence=dict(alignment='with_higher_timeframe')),
        price=100, equity=1000, free_margin=1000, fee=fee, slippage=slip, notional=10, leverage=5)
    assert with_failed is None and with_prices['target'] == 130
    assert with_prices['target_reference']['open_time'] == 3


def test_score_order_is_expected_r_then_with_the_higher_timeframe(tmp_path):
    from time import time

    from trading.models.domain import Side
    from trading.quant.signals import CandidateSignal, StrategyName
    from trading.runtime.candidates import CandidateQueue
    from trading.runtime.runner3_policy import rank_setups, swap_candidate
    from trading.runtime.store import RuntimeStore

    fee, slip = 0.0006, 0.0005
    against = dict(symbol='AAA', setup_id='a', side='long', entry_reference=100,
                   stop_loss_price=90, take_profit_price=120,
                   evidence=dict(alignment='against_higher_timeframe'))
    with_htf = dict(symbol='BBB', setup_id='b', side='long', entry_reference=100,
                    stop_loss_price=90, take_profit_price=120,
                    evidence=dict(alignment='with_higher_timeframe'))
    richer = dict(symbol='CCC', setup_id='c', side='long', entry_reference=100,
                  stop_loss_price=90, take_profit_price=150,
                  evidence=dict(alignment='against_higher_timeframe'))
    assert [row['symbol'] for row in rank_setups([against, with_htf, richer], fee=fee, slippage=slip)] == [
        'CCC', 'BBB', 'AAA']

    report = swap_candidate(
        [dict(richer, allocation=dict(target_margin_usdt=10))],
        [dict(symbol='ETH_USDT', side='long', entry=100, stop=90, target=101)],
        free_margin=1, required_margin=10, fee=fee, slippage=slip)
    assert report['close'] is False
    assert report['waiting']['symbol'] == 'CCC'
    assert report['waiting']['expected_r'] > report['open_positions'][0]['expected_r']
    assert swap_candidate([richer], [dict(symbol='ETH_USDT', side='long', entry=100, stop=90, target=101)],
                          free_margin=10, required_margin=10, fee=fee, slippage=slip) is None

    now = time()

    def candidate(symbol, rank, aligned):
        return CandidateSignal(
            symbol=symbol, side=Side.LONG, strategy=StrategyName.STRUCTURE_SETUP,
            entry_price=100, stop_loss_price=90, take_profit_price=120, confidence=.5,
            features=dict(htf_zone_seed=True, seed_timeframe='15m', source_closed_at=now,
                          capital_rank=rank, with_higher_timeframe=aligned, dedup_key=symbol))

    queue = CandidateQueue(RuntimeStore(str(tmp_path / 'state')))
    queue.refresh([
        candidate('AGAINST', 2, False),
        candidate('WITH', 2, True),
        candidate('RICHER', 3, False),
    ], now=now)
    order = [queue.next(htf_zone_entries=True, now=now, lease_seconds=30).symbol for _ in range(3)]
    assert order == ['RICHER', 'WITH', 'AGAINST']
