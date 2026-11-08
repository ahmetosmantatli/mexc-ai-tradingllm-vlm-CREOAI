"""Runner 2 exit policy replay using recorded quotes and simulated fills."""

from decimal import Decimal, ROUND_DOWN
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from trading.evaluation.price_plan import CandleReference, _reference
from trading.runtime.runner2_entry import ioc_price_limit

RUNNER_ID = 'runner2'
RUNNER_NAME = 'Runner 2'
Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
HORIZON_RANK = {'scalp': 0, 'intraday': 1, 'swing': 2}
REVIEW_TIMEFRAME_RANK = {'5m': 0, '15m': 1, '4h': 2}


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Target(Record):
    price: Positive
    reference: CandleReference


class ExitPlan(Record):
    targets: list[Target] = Field(min_length=1, max_length=2)
    first_fraction: Decimal | None = Field(default=None, gt=0, lt=1)
    reason: str = Field(min_length=12, max_length=600)


class Quote(Record):
    at: int = Field(ge=0, strict=True)
    bid: Positive
    ask: Positive
    fair: Positive


class EntryRisk(Record):
    contract_size: float = Field(gt=0, strict=True)
    loss_budget: float = Field(gt=0, strict=True)
    fee_rate: float = Field(ge=0, lt=1, strict=True)
    exit_slippage_bps: float = Field(ge=0, lt=10000, strict=True)


class Horizon(Record):
    style: Literal['scalp', 'intraday', 'swing']
    review_timeframe: Literal['5m', '15m', '4h']
    expires_at: int = Field(gt=0, strict=True)


class Opening(Record):
    symbol: str = Field(pattern=r'^[A-Z0-9]+_USDT$')
    side: Literal['long', 'short']
    entry: Positive
    contracts: Positive
    quantity_step: Positive
    minimum_contracts: Positive
    price_step: Positive
    stop: Positive
    quote: Quote
    evidence: dict
    plan: ExitPlan
    entry_risk: EntryRisk | None = None
    planned_entry: Positive | None = None
    horizon: Horizon | None = None


class Revision(Record):
    kind: Literal['revise']
    expected_revision: int = Field(ge=0, strict=True)
    quote: Quote
    evidence: dict
    plan: ExitPlan | None = None
    stop: Positive | None = None
    stop_reference: CandleReference | None = None
    horizon: Horizon | None = None


class Fill(Record):
    kind: Literal['fill']
    expected_revision: int = Field(ge=0, strict=True)
    at: int = Field(ge=0, strict=True)
    fill_id: str = Field(min_length=1, max_length=100)
    exit: Literal['tp1', 'tp2', 'stop', 'reduce']
    contracts: Positive
    price: Positive


Event = Annotated[Revision | Fill, Field(discriminator='kind')]
events_adapter = TypeAdapter(list[Event])


def _aligned(value, step):
    return value % step == 0


def _check_quote(quote):
    if quote.bid > quote.ask:
        raise ValueError('crossed_quote')


def _check_level(price, reference, evidence, at, field, step):
    if reference.field != field or not _aligned(price, step):
        raise ValueError('invalid_price_tick_or_reference_direction')
    interval = {'5m': 300, '15m': 900, '4h': 14400}[reference.timeframe]
    rows = evidence.get(reference.timeframe, [])
    if not isinstance(rows, list) or len(rows) > 12 or any(
        not isinstance(row, list) or len(row) < 6 or type(row[0]) is not int
        or row[0] < 0 or row[0] + interval > at for row in rows
    ):
        raise ValueError('invalid_or_future_candle_evidence')
    anchor, atr = _reference(reference, evidence, at)
    if abs(price - Decimal(str(anchor))) > max(Decimal(str(atr)), step):
        raise ValueError('price_too_far_from_candle_reference')
    return Decimal(str(anchor))


class Runner2ExitReplay:
    def __init__(self, opening: Opening):
        self.opening = opening
        self.direction = 1 if opening.side == 'long' else -1
        self.remaining = opening.contracts
        self.revision = 0
        self.at = opening.quote.at
        self.stop = opening.stop
        self.fills = {}
        self.tp1_taken = False
        self.stop_execution_pending = False
        self.events = []
        self.horizon = opening.horizon
        if self.horizon is not None and self.horizon.expires_at <= self.at:
            raise ValueError('opening_horizon_expired')
        if not _aligned(self.remaining, opening.quantity_step):
            raise ValueError('position_quantity_step_mismatch')
        if not _aligned(self.stop, opening.price_step):
            raise ValueError('stop_price_step_mismatch')
        if self.direction * (opening.entry - self.stop) <= 0:
            raise ValueError('initial_stop_on_wrong_side_of_entry')
        self.legs = self._validate_plan(opening.plan, opening.quote, opening.evidence, self.stop)
        self.entry_limit = None
        if opening.entry_risk is not None:
            # The nearest TP must still be beyond the worst accepted entry.
            self.entry_limit = ioc_price_limit(
                side=opening.side, stop=self.stop, target=opening.plan.targets[0].price,
                contracts=opening.contracts, price_step=opening.price_step,
                **opening.entry_risk.model_dump(),
            )
            if self.direction * (opening.entry - self.entry_limit) > 0:
                raise ValueError('simulated_entry_outside_risk_limit')
        self.events.append(dict(kind='plan', at=self.at, revision=0,
                                plan=opening.plan.model_dump(mode='json'), legs=self._legs()))

    def _validate_plan(self, plan, quote, evidence, stop):
        _check_quote(quote)
        opening = self.opening
        long = self.direction == 1
        boundary = max(quote.ask, quote.fair) if long else min(quote.bid, quote.fair)
        stop_boundary = min(quote.bid, quote.fair) if long else max(quote.ask, quote.fair)
        if self.direction * (stop_boundary - stop) <= 0:
            raise ValueError('stop_already_triggerable')
        split = len(plan.targets) == 2
        if split != (plan.first_fraction is not None):
            raise ValueError('split_requires_fraction')
        if split and self.tp1_taken:
            raise ValueError('tp1_already_filled')
        quantities = [self.remaining]
        if split:
            step = opening.quantity_step
            first = (self.remaining * plan.first_fraction / step).to_integral_value(rounding=ROUND_DOWN) * step
            quantities = [first, self.remaining - first]
        if min(quantities) < opening.minimum_contracts:
            raise ValueError('target_quantity_below_contract_minimum')
        previous = max(boundary, opening.entry) if long else min(boundary, opening.entry)
        legs = {}
        for index, (target, quantity) in enumerate(zip(plan.targets, quantities)):
            anchor = _check_level(target.price, target.reference, evidence, quote.at,
                                  'high' if long else 'low', opening.price_step)
            if self.direction * (target.price - previous) <= 0:
                raise ValueError('targets_must_be_ordered_beyond_entry_and_quote')
            if self.direction * (anchor - opening.entry) <= 0:
                raise ValueError('target_reference_on_wrong_side_of_entry')
            name = 'tp1' if split and index == 0 else 'tp2'
            legs[name] = (target.price, quantity)
            previous = target.price
        return legs

    def apply(self, event: Revision | Fill):
        if isinstance(event, Fill) and event.fill_id in self.fills:
            if self.fills[event.fill_id] != event:
                raise ValueError('conflicting_fill_id')
            return
        at = event.quote.at if isinstance(event, Revision) else event.at
        if event.expected_revision != self.revision or at < self.at:
            raise ValueError('stale_exit_event')
        if not self.remaining:
            raise ValueError('position_already_closed')
        if self.stop_execution_pending and (isinstance(event, Revision) or event.exit != 'stop'):
            raise ValueError('stop_execution_needs_reconciliation')

        if isinstance(event, Revision):
            self._revise(event)
        else:
            self._fill(event)
        self.revision += 1
        self.at = at
        self.events.append(dict(event.model_dump(mode='json'), revision=self.revision, legs=self._legs(),
                                stop=str(self.stop), remaining_contracts=str(self.remaining)))

    def _revise(self, event):
        quote = event.quote
        _check_quote(quote)
        long = self.direction == 1
        target_boundary = max(quote.ask, quote.fair) if long else min(quote.bid, quote.fair)
        stop_boundary = min(quote.bid, quote.fair) if long else max(quote.ask, quote.fair)
        if any(self.direction * (price - target_boundary) <= 0 for price, _ in self.legs.values()):
            raise ValueError('existing_target_needs_fill_reconciliation')
        if self.direction * (stop_boundary - self.stop) <= 0:
            raise ValueError('existing_stop_needs_fill_reconciliation')

        if event.horizon is not None:
            current = self.horizon
            if (current is None or quote.at >= current.expires_at
                    or HORIZON_RANK[event.horizon.style] <= HORIZON_RANK[current.style]
                    or REVIEW_TIMEFRAME_RANK[event.horizon.review_timeframe] <= REVIEW_TIMEFRAME_RANK[current.review_timeframe]
                    or event.horizon.expires_at <= current.expires_at):
                raise ValueError('invalid_horizon_promotion')
            current_target = max(self.direction * price for price, _ in self.legs.values())
            if event.plan is None or not any(
                target.reference.timeframe == event.horizon.review_timeframe
                and self.direction * target.price > current_target
                for target in event.plan.targets
            ):
                raise ValueError('horizon_promotion_requires_new_target_evidence')

        stop = self.stop if event.stop is None else event.stop
        if event.plan is None and event.stop is None:
            raise ValueError('empty_exit_revision')
        if (event.stop is None) != (event.stop_reference is None):
            raise ValueError('stop_revision_requires_candle_reference')
        if event.stop is not None:
            if self.direction * (stop - self.stop) <= 0:
                raise ValueError('stop_must_tighten')
            anchor = _check_level(stop, event.stop_reference, event.evidence, quote.at,
                                  'low' if long else 'high', self.opening.price_step)
            if self.direction * (anchor - stop) < 0:
                raise ValueError('stop_must_be_beyond_wick')
            if self.direction * (stop_boundary - stop) <= 0:
                raise ValueError('stop_already_triggerable')
        legs = self.legs if event.plan is None else self._validate_plan(event.plan, quote, event.evidence, stop)
        self.stop, self.legs = stop, legs
        if event.horizon is not None:
            self.horizon = event.horizon

    def _fill(self, event):
        if not _aligned(event.contracts, self.opening.quantity_step):
            raise ValueError('fill_quantity_step_mismatch')
        if event.exit in ('stop', 'reduce'):
            if event.contracts > self.remaining:
                raise ValueError('fill_exceeds_position_quantity')
            if event.exit == 'stop':
                self.stop_execution_pending = event.contracts < self.remaining
                self.legs.clear()
            else:
                # Simulated reduction removes the furthest target allocation first.
                reduction = event.contracts
                for name in reversed(tuple(self.legs)):
                    price, quantity = self.legs[name]
                    taken = min(reduction, quantity)
                    reduction -= taken
                    if taken == quantity:
                        del self.legs[name]
                    else:
                        self.legs[name] = price, quantity - taken
                    if not reduction:
                        break
        else:
            leg = self.legs.get(event.exit)
            if leg is None or event.contracts > leg[1]:
                raise ValueError('fill_exceeds_active_target_quantity')
            price, quantity = leg
            remainder = quantity - event.contracts
            if remainder:
                self.legs[event.exit] = price, remainder
            else:
                del self.legs[event.exit]
            if event.exit == 'tp1':
                self.tp1_taken = True
        self.remaining -= event.contracts
        self.fills[event.fill_id] = event

    def _legs(self):
        return {name: dict(price=str(price), contracts=str(contracts))
                for name, (price, contracts) in self.legs.items()}

    def _performance(self):
        opening = self.opening
        costs = opening.entry_risk
        planned = opening.planned_entry
        stats = dict(planned_entry=str(planned) if planned is not None else None,
                     filled_entry=str(opening.entry))
        if planned is not None:
            stats['adverse_entry_slippage_bps'] = str(self.direction * (opening.entry / planned - 1) * 10000)
        if costs is None:
            return stats
        contract_size = Decimal(str(costs.contract_size))
        fee = Decimal(str(costs.fee_rate))
        slip = Decimal(str(costs.exit_slippage_bps)) / 10000
        stop_exit = opening.stop * (1 - self.direction * slip)
        initial_risk = ((self.direction * (opening.entry - stop_exit)) + fee * (opening.entry + stop_exit))
        initial_risk *= opening.contracts * contract_size
        net = sum(((self.direction * (fill.price - opening.entry)) - fee * (opening.entry + fill.price))
                  * fill.contracts * contract_size for fill in self.fills.values())
        planned_net = Decimal(0)
        for leg in self.events[0]['legs'].values():
            target_exit = Decimal(leg['price']) * (1 - self.direction * slip)
            planned_net += ((self.direction * (target_exit - opening.entry))
                            - fee * (opening.entry + target_exit)) * Decimal(leg['contracts']) * contract_size
        stats.update(initial_stop_risk_usdt=str(initial_risk), realized_net_before_funding_usdt=str(net),
                     estimated_initial_target_r=str(planned_net / initial_risk) if initial_risk > 0 else None,
                     realized_r_before_funding=str(net / initial_risk) if initial_risk > 0 else None)
        return stats

    def snapshot(self):
        return dict(runner_id=RUNNER_ID, runner_name=RUNNER_NAME, mode='SHADOW',
                    opening=self.opening.model_dump(mode='json'), revision=self.revision,
                    entry_ioc_limit=str(self.entry_limit) if self.entry_limit is not None else None,
                    remaining_contracts=str(self.remaining), stop_price=str(self.stop),
                    stop_contracts=None if self.stop_execution_pending else str(self.remaining),
                    stop_execution_pending=self.stop_execution_pending,
                    horizon=self.horizon.model_dump(mode='json') if self.horizon else None,
                    targets=self._legs(), performance=self._performance(), events=self.events)
