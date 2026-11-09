"""VLM reviews that fire only when they can change a live book.

The book itself keeps the stop, the target, and the size. VLM is asked once
when an open position is about 1% in favor, and once more around 2% if it is
still short of the target. The same packet is used for one idle look at a
triggered watch before the order and one look after it. Silence, an invalid
answer, or a wider stop leaves the existing stop and target in place.
"""
from math import isfinite

from pydantic import BaseModel, ConfigDict, Field, model_validator


FAVOR_REVIEWS = (('1pct', 0.01), ('2pct', 0.02))

VLM_BOOK_POLICY = (
    'Review the supplied MEXC USDT perpetual chart packet. It contains why we entered, '
    'the confirming CHoCH stop, the target, equity, and the position. '
    'Reply JSON with action hold, move_stop, move_target, or reduce. '
    'position_id must be the supplied id. reason is concise Turkish. '
    'Unused prices and close_fraction are null. '
    'move_stop may only tighten the current stop. It must not widen the stop past the confirming CHoCH extreme. '
    'move_target may raise or extend the target in the profitable direction. '
    'reduce scales out part of the remaining size: close_fraction is between 0 and 1 and is not a full close. '
    'Do not open, add, reverse, or change leverage. '
    'A 4h swing whose higher-timeframe structure still holds is not closed just because 15m broke. '
    'A 1h setup may protect by tightening the stop when 5m structure breaks. '
    'If you have no valid change, reply hold. The server writes only through the existing protection path '
    'and keeps the current stop and target when the answer is missing or invalid.'
)


class VlmBookDecision(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    action: str = Field(pattern='^(hold|move_stop|move_target|reduce)$')
    position_id: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=12, max_length=800)
    stop_price: float | None = Field(default=None, gt=0)
    target_price: float | None = Field(default=None, gt=0)
    close_fraction: float | None = Field(default=None, gt=0, lt=1)

    @model_validator(mode='after')
    def consistent_action(self):
        if self.action == 'move_stop':
            if self.stop_price is None or self.target_price is not None or self.close_fraction is not None:
                raise ValueError('tighten requires only a stop')
        elif self.action == 'move_target':
            if self.target_price is None or self.stop_price is not None or self.close_fraction is not None:
                raise ValueError('target change requires only a target')
        elif self.action == 'reduce':
            if self.close_fraction is None or self.stop_price is not None or self.target_price is not None:
                raise ValueError('scale-out requires only a fraction')
        elif self.stop_price is not None or self.target_price is not None or self.close_fraction is not None:
            raise ValueError('hold carries no price change')
        return self


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value > 0


def htf_structure_holds(side, higher_breaks):
    """True while the higher timeframe has not closed against the position."""
    if side not in ('long', 'short'):
        return False
    if not higher_breaks:
        return True
    last = higher_breaks[-1]
    if not isinstance(last, dict):
        return False
    direction = last.get('direction')
    if side == 'long':
        return direction != 'bearish'
    return direction != 'bullish'


def structure_break_outcome(*, context_timeframe, side, broken_timeframe, higher_breaks):
    """A lone lower-timeframe break does not close a 4h swing that still holds.

    A 1h setup may protect when 5m structure breaks. Protection is a tighter
    stop, not a close, and only when a later check accepts the price.
    """
    holds = htf_structure_holds(side, higher_breaks)
    if context_timeframe == '4h' and broken_timeframe == '15m' and holds:
        return dict(action='hold', close=False, write=False, may_tighten=False,
                    reason='4h_structure_holds')
    if context_timeframe == '1h' and broken_timeframe == '5m':
        return dict(action='protect', close=False, write=False, may_tighten=True,
                    reason='1h_may_protect_on_5m_break')
    return dict(action='hold', close=False, write=False, may_tighten=False,
                reason='no_structure_order')


def four_hour_close_blocked(*, action, context_timeframe, review_timeframe, side, higher_breaks):
    """Refuse a full close that is only a 15m break under a live 4h structure."""
    if action != 'close':
        return False
    outcome = structure_break_outcome(
        context_timeframe=context_timeframe, side=side,
        broken_timeframe=review_timeframe, higher_breaks=higher_breaks)
    return outcome['reason'] == '4h_structure_holds'


def book_vlm_enabled(metadata):
    """Only a CHoCH book asks VLM. Ordinary management stays on its own loop."""
    if not isinstance(metadata, dict):
        return False
    setup = metadata.get('htf_setup') or {}
    if setup.get('context_timeframe') in ('4h', '1h'):
        return True
    return _positive(metadata.get('choch_extreme'))


def choch_extreme(metadata):
    """The confirming candle wick. Missing evidence does not invent one."""
    if not isinstance(metadata, dict):
        return None
    explicit = metadata.get('choch_extreme')
    if _positive(explicit):
        return float(explicit)
    plan = metadata.get('price_plan') or {}
    reference = plan.get('stop_reference') if isinstance(plan, dict) else None
    evidence = metadata.get('price_evidence')
    if not isinstance(evidence, dict):
        return None
    from trading.runtime.runner3_policy import confirming_extreme
    extreme = confirming_extreme(reference, evidence)
    return float(extreme) if _positive(extreme) else None


def target_still_open(side, target, bid, ask, fair):
    if not all(_positive(value) for value in (target, bid, ask, fair)) or ask < bid:
        return False
    if side == 'long':
        return target > max(ask, fair)
    if side == 'short':
        return target < min(bid, fair)
    return False


def favorable_fraction(side, entry, bid, ask):
    if not _positive(entry) or not _positive(bid) or not _positive(ask):
        return None
    if side == 'long':
        return (bid - entry) / entry
    if side == 'short':
        return (entry - ask) / entry
    return None


def due_favor_review(side, entry, bid, ask, fair, target, fired):
    """The lowest 1% or 2% mark that has not been asked yet, while TP is intact."""
    if not target_still_open(side, target, bid, ask, fair):
        return None
    move = favorable_fraction(side, entry, bid, ask)
    if move is None:
        return None
    done = set(fired or [])
    for name, level in FAVOR_REVIEWS:
        if name not in done and move + 1e-12 >= level:
            return name
    return None


def past_choch_extreme(side, proposed, extreme):
    """True when a long stop is below the confirming wick, or a short stop is above it."""
    if side not in ('long', 'short') or not _positive(proposed) or not _positive(extreme):
        return False
    return proposed < extreme if side == 'long' else proposed > extreme


def stop_verdict(side, current, proposed, extreme):
    """A stop may tighten. Any loosen, including one past the CHoCH wick, is a widen."""
    if side not in ('long', 'short') or not _positive(current) or not _positive(proposed):
        return 'invalid'
    tighter = proposed > current if side == 'long' else proposed < current
    if not tighter:
        return 'widen'
    return 'tighten'


def target_extends(side, current, proposed):
    if side not in ('long', 'short') or not _positive(current) or not _positive(proposed):
        return False
    return proposed > current if side == 'long' else proposed < current


def judge_book_answer(raw, *, position_id, side, current_stop, current_target, extreme):
    """Silence and invalid answers change nothing. A widen is rejected before any write."""
    if raw is None:
        return dict(kind='silent', decision=None)
    try:
        decision = VlmBookDecision.model_validate(raw)
    except (ValueError, TypeError):
        return dict(kind='invalid', decision=None)
    if decision.position_id != position_id:
        return dict(kind='invalid', decision=None)
    if decision.action == 'hold':
        return dict(kind='hold', decision=decision)
    if decision.action == 'move_stop':
        verdict = stop_verdict(side, current_stop, decision.stop_price, extreme)
        if verdict != 'tighten':
            return dict(kind='rejected', decision=decision, verdict=verdict,
                        past_extreme=past_choch_extreme(side, decision.stop_price, extreme))
        return dict(kind='apply', decision=decision)
    if decision.action == 'move_target':
        if not target_extends(side, current_target, decision.target_price):
            return dict(kind='rejected', decision=decision, verdict='target')
        return dict(kind='apply', decision=decision)
    return dict(kind='apply', decision=decision)


def build_packet(*, phase, why, choch_stop, target, equity, position, structure):
    """One packet for a profit review and for the idle watch before and after the order."""
    return dict(task='vlm_book_review', phase=phase, why_we_entered=why or '',
                choch_stop=choch_stop, target=target, equity=equity,
                position=position, structure=structure or {})
