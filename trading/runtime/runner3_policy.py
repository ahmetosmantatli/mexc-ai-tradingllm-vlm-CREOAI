"""Deterministic Runner 3 allocation and evidence rules."""
from datetime import datetime, timedelta
from math import floor, isfinite
from time import time
from zoneinfo import ZoneInfo

COHORT = 'runner3-htf-zone-v3'
MODEL_SIZING_COHORT = 'runner3-htf-zone-v3-aggressive-market-margin'
CONTINUOUS_COHORT = 'runner3-htf-zone-v3-aggressive-continuous'
AGGRESSIVE_STOP_RISK_CAP = .10
CONTINUOUS_STOP_RISK_CAP = .15
MARGIN_RESERVE_FRACTION = .15
MIN_NET_TARGET_R = 1.25
MAX_COST_SHARE = .30
SMALL_ACCOUNT_EQUITY_USDT = 100.
MIN_OPEN_MARGIN_USDT = 1.
GUCLU_MARGIN_FALLBACK = .35
GUCLU_MARGIN_INSTRUCTION = (
    'Under 100 USDT Standart margin stays 20% of equity. Güçlü is not a fixed percent. '
    'When a setup carries guclu_margin_choice, that label, its evidence, free margin, equity, open positions, the CHoCH loss ceiling, and the account goal are for you, the text model. '
    'The goal is to grow the cash over that setup horizon: 4h swing or 1h intraday. '
    'Choose sizing_plan.margin_share as the fraction of equity for that Güçlü plan while equity is under 100 USDT. It must sit inside the CHoCH loss ceiling. '
    'You cannot widen or tighten the CHoCH stop to make the position bigger. '
    'Final size is the tightest of your share, the CHoCH loss ceiling, and free margin. '
    'If margin_share is missing or invalid the engine uses 35% of equity and does not invent a larger share. '
    'At or above 100 USDT the engine keeps its profile margin target. '
    'You do not place the order. One writer does. '
)


def continuous_aggressive(settings, profile, runner_id):
    return (getattr(settings, 'live_runner3_continuous_aggressive_enabled', False)
            and profile == 'aggressive_growth' and runner_id == 'runner3')


def aggressive_model_sizing(settings, profile, runner_id, equity):
    if continuous_aggressive(settings, profile, runner_id):
        return type(equity) in (int, float) and isfinite(equity) and equity > 0
    return (getattr(settings, 'live_runner3_aggressive_model_sizing_enabled', False)
            and profile == 'aggressive_growth' and runner_id == 'runner3'
            and type(equity) in (int, float) and 0 < equity < SMALL_ACCOUNT_EQUITY_USDT)


def uses_fixed_position_limit(runner_id, equity, *, continuous=False):
    return not continuous and not (runner_id == 'runner3' and 0 < equity < SMALL_ACCOUNT_EQUITY_USDT)


def evidence_grade(setup):
    """Güçlü only when the evidence itself says so. There is no confidence score.

    That is a liquidity sweep plus MSS plus an FVG retest, or a closed break
    in the higher-timeframe direction with at least 2R to the opposing level.
    Every other valid setup stays Standart.
    """
    evidence = setup['evidence']
    kind = evidence['kind']
    with_higher_timeframe = setup['macro_trend'] == ('bullish' if setup['side']=='long' else 'bearish')
    try:
        payoff = float(setup.get('reward_risk'))
    except (TypeError, ValueError):
        payoff = 0.
    closed_break = kind == 'closed_bos'
    higher_break = evidence.get('context_last_break')
    if not closed_break and isinstance(higher_break, dict):
        closed_break = higher_break.get('direction') == setup.get('macro_trend')
    strong = kind == 'sweep_mss_fvg_retest' or (
        closed_break and payoff >= 2 and with_higher_timeframe)
    return 'strong' if strong else 'standard'


def strong_evidence_label(setup):
    """The evidence phrase sent with a Güçlü label. Standart has no phrase."""
    try:
        if evidence_grade(setup) != 'strong':
            return None
        kind = setup['evidence']['kind']
    except (KeyError, TypeError):
        return None
    if kind == 'sweep_mss_fvg_retest':
        return 'sweep+MSS+FVG retest'
    return 'with-trend closed break and at least 2R'


def setup_time_horizon(setup):
    """4h swing or 1h intraday, from the lane the setup already belongs to."""
    context = setup.get('context_timeframe')
    horizon = setup.get('horizon')
    if context == '4h' or (context is None and horizon == 'swing'):
        return '4h swing'
    if context == '1h' or (context is None and horizon == 'intraday'):
        return '1h intraday'
    if horizon == 'swing':
        return '4h swing'
    if horizon == 'intraday':
        return '1h intraday'
    return None


def guclu_margin_request(setup, *, equity, free_margin, open_positions, loss_ceiling_usdt):
    """Facts the text model needs before it chooses a Güçlü margin share."""
    evidence = strong_evidence_label(setup)
    if evidence is None:
        return None
    return dict(
        label='Güçlü', evidence=evidence, free_margin_usdt=free_margin, equity_usdt=equity,
        open_positions=list(open_positions or []), choch_loss_ceiling_usdt=loss_ceiling_usdt,
        account_goal='grow the cash', time_horizon=setup_time_horizon(setup))


def resolve_guclu_margin_share(value):
    """Usable fraction of equity, or exactly 35%. Never a larger invented share."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return GUCLU_MARGIN_FALLBACK, 'fallback_35'
    value = float(value)
    if not isfinite(value) or not 0 < value <= 1:
        return GUCLU_MARGIN_FALLBACK, 'fallback_35'
    return value, 'model'


def chosen_margin_share(metadata):
    """Text-model share when the key is present, including an explicit silence.

    A later chart decision can carry its own sizing plan. That plan does not
    replace a share the text model already gave or withheld.
    """
    if not isinstance(metadata, dict):
        return None
    if 'guclu_margin_share' in metadata:
        return metadata.get('guclu_margin_share')
    plan = metadata.get('sizing_plan')
    if isinstance(plan, dict):
        return plan.get('margin_share')
    return getattr(plan, 'margin_share', None)


def apply_guclu_margin_share(allocation, *, equity, available, raw_share, allow_standard=False):
    """Use the text model's Güçlü share under 100 USDT.

    Standart defaults to 20%; the opt-in model policy can honor its explicit share.
    Silence or an unusable share falls back to 35% of equity.
    """
    strong = allocation.get('grade') == 'strong'
    if not (strong or allow_standard):
        return allocation
    if type(equity) not in (int, float) or not isfinite(equity) or equity <= 0:
        return allocation
    if not allow_standard and equity >= SMALL_ACCOUNT_EQUITY_USDT:
        return allocation
    fraction, source = resolve_guclu_margin_share(raw_share)
    if not strong and source != 'model':
        return allocation
    target = equity * fraction
    updated = dict(allocation)
    updated.update(
        margin_fraction=fraction, target_margin_usdt=target, margin_share_source=source,
        max_entry_cost_usdt=max(0., min(target, available - allocation['reserve_usdt'])))
    return updated


def validate_evidence(setup, plan, now=None):
    now = time() if now is None else now
    if not setup or plan is None:
        raise ValueError('runner3_requires_verified_setup')
    evidence = setup['evidence']
    if evidence.get('stage') in ('awaiting_touch', 'awaiting_confirmation'):
        raise ValueError('touch_is_not_an_entry')
    if evidence.get('expires_at', float('inf')) <= now:
        raise ValueError('runner3_evidence_expired')
    if setup.get('context_timeframe'):
        if setup.get('state') != 'ready' or evidence.get('kind') not in ('htf_zone_choch', 'htf_zone_mss'):
            raise ValueError('runner3_requires_verified_setup')
        if plan.entry_timeframe != setup['trigger_timeframe']:
            raise ValueError('runner3_trigger_timeframe_mismatch')
        if _target_identity(plan.target_reference) not in supplied_target_identities(setup):
            raise ValueError('runner3_target_reference_mismatch')
    if plan.stop_reference.model_dump() != setup['stop_reference']:
        raise ValueError('runner3_stop_reference_mismatch')
    if (plan.stop_loss_price > setup['stop_loss_price'] if setup['side']=='long'
            else plan.stop_loss_price < setup['stop_loss_price']):
        raise ValueError('runner3_structural_stop_too_tight')


def margin_policy(equity, capital, available, grade, horizon, *, model_sizing=False):
    if not all(type(v) in (int, float) and isfinite(v) and v >= 0 for v in (equity, capital, available)):
        raise ValueError('runner3_invalid_margin_context')
    if grade not in ('standard','strong') or horizon not in ('scalp','intraday','swing'):
        raise ValueError('runner3_invalid_margin_context')
    small_account = 0 < equity < SMALL_ACCOUNT_EQUITY_USDT
    if small_account:
        # Standart is 20% of equity. 35% is only the silent Güçlü fallback.
        # The text model's share replaces it before an order is sized.
        fraction = GUCLU_MARGIN_FALLBACK if grade == 'strong' else .20
        target = equity * fraction
    else:
        fraction = .20 if grade == 'strong' else .10
        target = capital * fraction
    reserve = 0. if model_sizing else capital * MARGIN_RESERVE_FRACTION
    return dict(grade=grade, horizon=horizon, equity_usdt=equity, margin_fraction=fraction,
                target_margin_usdt=target, reserve_usdt=reserve,
                max_entry_cost_usdt=max(0., min(target, available - reserve)))


def confirming_extreme(reference, evidence):
    """Wick of the confirming CHoCH/MSS candle. A missing candle fails closed."""
    if reference is None or not isinstance(evidence, dict):
        return None
    timeframe = getattr(reference, 'timeframe', None) or (reference.get('timeframe') if isinstance(reference, dict) else None)
    open_time = getattr(reference, 'open_time', None)
    if open_time is None and isinstance(reference, dict):
        open_time = reference.get('open_time')
    field = getattr(reference, 'field', None) or (reference.get('field') if isinstance(reference, dict) else None)
    rows = evidence.get(timeframe) or []
    matches = [row for row in rows if isinstance(row, (list, tuple)) and len(row) >= 4
               and type(row[0]) in (int, float) and row[0] == open_time]
    if len(matches) != 1 or field not in ('low', 'high'):
        return None
    wick = matches[0][3] if field == 'low' else matches[0][2]
    if isinstance(wick, bool) or not isinstance(wick, (int, float)) or not isfinite(wick) or wick <= 0:
        return None
    return float(wick)


def drift_respects_choch_risk(*, side, fresh_entry, stop, target, choch_extreme, quantity, risk_budget, fee, slippage):
    """A moved quote still enters only while the CHoCH stop and the planned loss both hold.

    The quantity is the one already sized at the planned entry. A price that has run
    far enough for that loss to exceed the risk budget is not chased by shrinking it.
    """
    values = (fresh_entry, stop, target, choch_extreme, quantity, risk_budget)
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value > 0 for value in values):
        return False
    if side == 'long':
        intact = stop <= choch_extreme < fresh_entry < target
    elif side == 'short':
        intact = target < fresh_entry < choch_extreme <= stop
    else:
        return False
    if not intact:
        return False
    _, _, unit = ioc_target_economics(
        side=side, entry=fresh_entry, stop=stop, target=target, fee=fee, slippage=slippage)
    return unit * quantity <= risk_budget + 1e-8


def leverage_for_choch_loss(*, notional, entry, stop, free_margin, equity, max_leverage,
                            maintenance, initial_rate, fee, liquidation_buffer, margin_target=None,
                            preferred_leverage=None, model_sizing=False):
    """Lowest leverage that posts the tightest of the loss budget, margin target, and free margin.

    ``notional`` is the CHoCH stop-loss budget. It is a ceiling, not a reason to
    move the stop. ``margin_target`` is the margin cap: Standart's 20% of equity,
    the text model's Güçlü share, or the 35% fallback. The size cannot exceed
    that cap, the loss budget, or free margin. Leverage is only raised far
    enough to post the loss-sized notional inside that margin cap unless the
    model's preferred leverage fits. The legacy small-account policy also
    enforces a 1 USDT margin minimum.
    """
    values = (notional, entry, stop, free_margin, equity, max_leverage, maintenance, initial_rate, fee, liquidation_buffer)
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) for value in values):
        raise ValueError('runner3_margin_reserve')
    if min(notional, entry, free_margin, max_leverage) <= 0 or stop <= 0 or stop == entry:
        raise ValueError('runner3_margin_reserve')
    margin_cap = free_margin
    if margin_target is not None:
        if isinstance(margin_target, bool) or not isinstance(margin_target, (int, float)) or not isfinite(margin_target):
            raise ValueError('runner3_margin_reserve')
        margin_cap = min(free_margin, margin_target)
    if margin_cap <= 0:
        raise ValueError('runner3_margin_reserve')
    minimum_margin = MIN_OPEN_MARGIN_USDT if not model_sizing and 0 < equity < SMALL_ACCOUNT_EQUITY_USDT else 0.
    if margin_cap < minimum_margin:
        raise ValueError('runner3_margin_below_minimum')
    cap = floor(max_leverage)
    if cap < 1 or cap > 100000:
        raise ValueError('runner3_margin_reserve')
    long = stop < entry

    def fit(leverage, size):
        margin_rate = max(1 / leverage, initial_rate)
        margin = size * margin_rate
        if margin + size * fee > margin_cap + 1e-9:
            return None
        distance = 1 / leverage - maintenance - fee
        if distance <= 0:
            return None
        liquidation = entry * (1 - distance if long else 1 + distance)
        buffer = (stop - liquidation) / entry if long else (liquidation - stop) / entry
        if buffer + 1e-12 < liquidation_buffer:
            return None
        if margin < minimum_margin:
            return None
        return leverage, margin, size

    if isinstance(preferred_leverage, (int, float)) and 1 <= preferred_leverage <= cap and preferred_leverage == int(preferred_leverage):
        leverage = int(preferred_leverage)
        margin_rate = max(1 / leverage, initial_rate)
        fitted = min(notional, margin_cap / (margin_rate + fee))
        selected = fit(leverage, fitted)
        if selected is not None:
            return selected

    for leverage in range(1, cap + 1):
        selected = fit(leverage, notional)
        if selected is not None:
            return selected
    # The loss-sized notional does not fit. Keep the stop and take the margin
    # cap at the lowest safe leverage instead of the exchange maximum.
    for leverage in range(1, cap + 1):
        margin_rate = max(1 / leverage, initial_rate)
        fitted = min(notional, margin_cap / (margin_rate + fee))
        selected = fit(leverage, fitted)
        if selected is not None:
            return selected
    if min(notional, margin_cap) <= minimum_margin:
        raise ValueError('runner3_margin_below_minimum')
    raise ValueError('runner3_margin_reserve')


def ioc_target_economics(*, side, entry, stop, target, fee, slippage):
    """Per-unit gross reward, net reward and modeled stop loss at the IOC limit."""
    direction = 1 if side == 'long' else -1
    stop_fill = stop * (1 - direction * slippage)
    target_fill = target * (1 - direction * slippage)
    loss = abs(entry - stop_fill) + fee * (entry + stop_fill)
    net = direction * (target_fill - entry) - fee * (entry + target_fill)
    return abs(target - entry), net, loss


def validate_economics(gross, net, loss):
    if loss <= 0 or net / loss < MIN_NET_TARGET_R:
        raise ValueError('runner3_net_target_r_too_low')
    if gross <= 0 or (gross - net) / gross > MAX_COST_SHARE:
        raise ValueError('runner3_cost_share_too_high')

def small_account_risk_target(equity, ceiling, grade):
    """Güçlü may use the whole per-trade loss ceiling. Standart stays at 80% of it."""
    if 0 < equity < SMALL_ACCOUNT_EQUITY_USDT:
        return ceiling if grade == 'strong' else ceiling * .80
    return None


def cost_adjusted_r(*, side, entry, stop, target, fee, slippage):
    """Net reward divided by modeled stop loss, after fees and slippage."""
    _gross, net, loss = ioc_target_economics(
        side=side, entry=entry, stop=stop, target=target, fee=fee, slippage=slippage)
    if loss <= 0 or not isfinite(net) or not isfinite(loss):
        return None
    return net / loss


ENTRY_GATE_BEGIN = 'Runner 3 entry gate, checked before you propose and again by the engine:'
ENTRY_GATE_END = 'End Runner 3 entry gate.'


def _cost_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
        return None
    return float(value)


def prompt_entry_costs(settings, costs=None):
    """Fee and exit slippage the live entry prompt must state.

    A supplied executor pair wins. Otherwise the settings pair is the same
    default the executor uses when it is constructed without an override.
    """
    fee = _cost_number(getattr(settings, 'paper_fee_rate', None))
    bps = _cost_number(getattr(settings, 'paper_slippage_bps', None))
    slippage = None if bps is None else bps / 10000
    if isinstance(costs, dict):
        supplied_fee = _cost_number(costs.get('fee'))
        supplied_slip = _cost_number(costs.get('slippage'))
        if supplied_fee is not None and supplied_slip is not None:
            return supplied_fee, supplied_slip
    if fee is None or slippage is None:
        raise ValueError('runner3_entry_cost_assumptions')
    return fee, slippage


def _target_identity(reference):
    if hasattr(reference, 'model_dump'):
        reference = reference.model_dump()
    if not isinstance(reference, dict):
        return None
    identity = tuple(reference.get(key) for key in ('timeframe', 'open_time', 'field'))
    if any(part is None for part in identity):
        return None
    return identity


def supplied_target_identities(setup):
    """Supplied opposing targets the model may name. The nearest stays first."""
    found = []
    if not isinstance(setup, dict):
        return found
    primary = _target_identity(setup.get('target_reference'))
    if primary is not None:
        found.append(primary)
    levels = setup.get('opposing_target_levels')
    for level in levels if isinstance(levels, list) else []:
        identity = _target_identity(level)
        if identity is not None and identity not in found:
            found.append(identity)
    return found


def entry_gate_instruction(*, fee, slippage):
    """Prompt text for the cost gate validate_economics still enforces.

    A real setup tries a farther supplied level before BEKLE. The 1.25 floor
    is unchanged: the engine still rejects cost-adjusted R under that floor.
    """
    fee = _cost_number(fee)
    slippage = _cost_number(slippage)
    if fee is None or slippage is None:
        raise ValueError('runner3_entry_cost_assumptions')
    lines = (
        ENTRY_GATE_BEGIN,
        'Before you propose an entry, apply this deterministic gate. The engine still runs it after you answer and still rejects a failure.',
        f'Runner 3 minimum cost-adjusted R is {MIN_NET_TARGET_R:.2f}.',
        f'Fee assumption is {fee:.4f} per side. Exit-slippage assumption is {slippage:.4f} ({slippage * 10000:.0f} bps), applied to the stop and the target.',
        'direction is 1 for long and -1 for short.',
        'stop_fill = stop * (1 - direction * slippage).',
        'target_fill = target * (1 - direction * slippage).',
        'loss = abs(entry - stop_fill) + fee * (entry + stop_fill).',
        'net = direction * (target_fill - entry) - fee * (entry + target_fill).',
        'cost_adjusted_R = net / loss.',
        'gross = abs(target - entry).',
        f'The engine rejects the proposal when loss <= 0 or cost_adjusted_R < {MIN_NET_TARGET_R:.2f}, and when gross <= 0 or (gross - net) / gross > {MAX_COST_SHARE:.2f}.',
        f'The continuous equity stop cap is {CONTINUOUS_STOP_RISK_CAP:.0%} of equity. One stop cannot lose more than that fraction of equity.',
        f'When continuous aggressive is off, aggressive model sizing uses the {AGGRESSIVE_STOP_RISK_CAP:.0%} equity stop cap.',
        f'When the setup is real (zone, touch, and a later closed CHoCH/MSS), first choose a farther target that is one of the supplied opposing target levels and whose cost-adjusted R clears {MIN_NET_TARGET_R:.2f}.',
        f'BEKLE (action hold) only when none of the supplied levels clears {MIN_NET_TARGET_R:.2f}.',
        'Do not move the stop to manufacture R. Do not invent a level that was not supplied.',
        'Do not invent a trade when you are silent.',
        ENTRY_GATE_END,
    )
    return '\n'.join(lines)


def thesis_invalidated(position, price):
    """The open thesis is invalidated only when price has crossed its structural level."""
    if not isinstance(position, dict):
        return False
    level = position.get('invalidation_price')
    if not isinstance(level, (int, float)) or isinstance(level, bool):
        level = position.get('stop')
    if not isinstance(level, (int, float)) or isinstance(level, bool) or level <= 0:
        return False
    if not isinstance(price, (int, float)) or isinstance(price, bool) or price <= 0:
        return False
    if position.get('side') == 'long':
        return price <= level
    if position.get('side') == 'short':
        return price >= level
    return False


def rank_confirmed_plans(plans):
    """Stronger evidence and payoff first. Ties stay on symbol and setup id."""
    def key(plan):
        grade = 1 if plan.get('grade') == 'strong' else 0
        try:
            reward = float(plan.get('capital_rank') if plan.get('capital_rank') is not None else plan.get('reward_risk') or 0)
        except (TypeError, ValueError):
            reward = 0.
        return (-grade, -reward, str(plan.get('symbol') or ''), str(plan.get('setup_id') or ''))
    return sorted(list(plans or []), key=key)


def position_brief(position):
    """PnL, thesis, and room to the planned target for a capital comparison."""
    return dict(symbol=position.get('symbol'), side=position.get('side'),
                unrealized_pnl=position.get('unrealized'),
                thesis=str(position.get('thesis') or '')[:500],
                remaining_room=copy_room(position.get('remaining_room')),
                leverage=position.get('leverage'), entry=position.get('entry'),
                stop=position.get('stop'), target=position.get('target'),
                mark=position.get('mark'),
                invalidation_price=position.get('invalidation_price'))


def copy_room(room):
    if not isinstance(room, dict):
        return None
    return {key: room.get(key) for key in ('to_target_price', 'to_target_r')}


def daily_objective(closed, target, now):
    local = datetime.fromtimestamp(now, ZoneInfo('Europe/Istanbul'))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = [row for row in closed if (row.get('closed_at') or 0)/1000 >= start.timestamp()]
    complete = all(row.get('realized_complete') is True and type(row.get('net_pnl')) in (float,int)
                   for row in rows)
    # Missing close timestamps cannot be assigned to a day.
    complete = complete and all(row.get('closed_at') for row in closed)
    net = sum(row['net_pnl'] for row in rows) if complete else None
    return dict(timezone='Europe/Istanbul',scope='managed_closed_trade_net_only',
                target_usdt=target,realized_net_usdt=net,
                remaining_usdt=max(0.,target-net) if target is not None and net is not None else None,
                remaining_seconds=max(0.,(start+timedelta(days=1)).timestamp()-now),
                affects_risk=False)
