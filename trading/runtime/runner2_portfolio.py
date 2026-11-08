"""Durable SHADOW coordinator for Runner 2. Commands never submit orders."""

import hashlib
import json
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from trading.runtime.runner2 import Positive, Quote, Record, RUNNER_ID, RUNNER_NAME

Identifier = Annotated[str, Field(pattern=r'^[A-Za-z0-9_.:-]{1,100}$')]
Timestamp = Annotated[int, Field(ge=0, strict=True)]
Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
Timeframe = Literal['5m', '15m', '4h']
SECONDS = {'5m': 300, '15m': 900, '4h': 14400}
PRIORITY = {'structure_break': 0, 'near_target': 1, 'no_progress': 1, 'horizon_due': 1,
            'closed_candle': 2, 'entry': 3}
MAX_ATTEMPTS = 2
TARGET_QUOTE_MAX_AGE_SECONDS = 30
TARGET_DISTANCE_R = Decimal('0.25')
TARGET_DISTANCE_ATR = Decimal('0.5')


class Evidence(Record):
    symbol: str = Field(pattern=r'^[A-Z0-9]+_USDT$')
    family: Identifier
    timeframe: Timeframe
    regime: Identifier
    observed_at: Timestamp
    features: dict[Identifier, float | None] = Field(default_factory=dict, max_length=32)


class Command(Record):
    command_id: Identifier
    at: Timestamp


class QueueReview(Command):
    kind: Literal['queue']
    subject: Identifier
    revision: Timestamp
    trigger: Literal['structure_break', 'near_target', 'no_progress', 'horizon_due', 'closed_candle', 'entry']
    expires_at: Timestamp
    evidence: Evidence


class PositionTarget(Record):
    """The next unfilled target from the confirmed exit plan in this revision."""

    side: Literal['long', 'short']
    price: Positive
    initial_risk_distance: Positive
    atr: Positive
    quote: Quote


class ObservePosition(Command):
    kind: Literal['observe']
    subject: Identifier
    revision: Timestamp
    opened_at: Timestamp
    max_holding_bars: int = Field(ge=1, le=1000, strict=True)
    no_progress_bars: int = Field(ge=1, le=1000, strict=True)
    minimum_progress_r: float = Field(gt=0)
    mfe_r: float = Field(ge=0)
    evidence: Evidence
    target: PositionTarget | None = None


class Dispatch(Command):
    kind: Literal['dispatch']
    available_slots: int = Field(ge=0, le=8, strict=True)


class ReviewResult(Command):
    kind: Literal['result']
    lease: Identifier
    action: Literal['hold', 'trade', 'watch', 'reduce', 'close', 'revise', 'unavailable']
    provider: Identifier | None = None
    model: str | None = Field(default=None, max_length=150)
    reason: str = Field(min_length=1, max_length=800)
    close_fraction: float | None = Field(default=None, gt=0, lt=1, strict=True)
    retry_after_seconds: int = Field(default=30, ge=1, le=3600, strict=True)


class AccountBalance(Command):
    kind: Literal['balance']
    observed_at: Timestamp
    available_margin: Money
    available_risk: Money
    reflected_fills: list[Identifier] = Field(default_factory=list, max_length=100)


class ConfirmReduction(Command):
    kind: Literal['reduction_confirmed']
    reduction_id: Identifier
    remaining_protection_verified: Literal[True]


class ReserveEntry(Command):
    kind: Literal['reserve']
    decision_id: Identifier
    balance_version: int = Field(ge=1, strict=True)
    margin: Positive
    risk: Positive
    quote_at: Timestamp
    reduction_id: Identifier | None = None


class EntryResult(Command):
    kind: Literal['entry_result']
    decision_id: Identifier
    outcome: Literal['filled', 'no_fill', 'rejected', 'unknown']
    initial_risk_usdt: Positive | None = None


class CloseResult(Command):
    kind: Literal['close_result']
    decision_id: Identifier
    net_usdt: Decimal


class Retrieve(Command):
    kind: Literal['retrieve']
    evidence: Evidence
    limit: int = Field(default=5, ge=1, le=20, strict=True)


PortfolioCommand = Annotated[
    QueueReview | ObservePosition | Dispatch | ReviewResult | AccountBalance |
    ConfirmReduction | ReserveEntry | EntryResult | CloseResult | Retrieve,
    Field(discriminator='kind'),
]
commands_adapter = TypeAdapter(list[PortfolioCommand])


class Runner2Portfolio:
    def __init__(self, store, account):
        self.store = store
        self.account = account
        self.key = 'runner2:shadow:' + account
        with store.lock, store.db:
            store.db.execute('''CREATE TABLE IF NOT EXISTS runner2_commands (
                account TEXT NOT NULL, id TEXT NOT NULL, command TEXT NOT NULL, result TEXT NOT NULL,
                PRIMARY KEY (account, id))''')

    def apply(self, command):
        raw = command.model_dump(mode='json')
        if isinstance(command, ObservePosition) and command.target is None:
            raw.pop('target')  # Preserve existing journal command IDs on replay.
        encoded = json.dumps(raw, sort_keys=True, allow_nan=False)
        with self.store.lock, self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            previous = self.store.db.execute(
                'SELECT command,result FROM runner2_commands WHERE account=? AND id=?',
                (self.key, command.command_id),
            ).fetchone()
            if previous:
                if previous[0] != encoded:
                    raise ValueError('conflicting_command_id')
                return json.loads(previous[1])
            state = self.store.get(self.key, dict(at=0, jobs={}, active={}, decisions={}, outcomes={},
                                                 balance=None, reservations={}, reductions={}, triggers={},
                                                 latest_review={}))
            if command.at < state['at']:
                raise ValueError('portfolio_event_out_of_order')
            self._expire_jobs(state, command.at)
            result = self._apply(state, command, raw)
            state['at'] = command.at
            self.store.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',
                                  (self.key, json.dumps(state, allow_nan=False)))
            self.store.db.execute('INSERT INTO runner2_commands VALUES (?,?,?,?)',
                                  (self.key, command.command_id, encoded, json.dumps(result, allow_nan=False)))
            return result

    def _expire_jobs(self, state, now):
        for subject, job in list(state['jobs'].items()):
            if job['expires_at'] <= now:
                del state['jobs'][subject]
        for lease, job in list(state['active'].items()):
            if job['lease_until'] > now:
                continue
            del state['active'][lease]
            current = state['latest_review'][job['subject']]['id'] == job['command_id']
            if (current and self._position_plan_current(state, job)
                    and job['expires_at'] > now and job['attempts'] < MAX_ATTEMPTS):
                job['ready_at'] = now
                state['jobs'][job['subject']] = job

    def _apply(self, state, command, raw):
        if isinstance(command, QueueReview):
            return self._queue(state, raw)
        if isinstance(command, ObservePosition):
            return self._observe(state, command)
        if isinstance(command, Dispatch):
            return self._dispatch(state, command)
        if isinstance(command, ReviewResult):
            return self._review_result(state, command, raw)
        if isinstance(command, AccountBalance):
            previous = state['balance']
            if command.observed_at > command.at or previous and command.observed_at <= previous['observed_at']:
                raise ValueError('stale_account_balance')
            version = previous['version'] + 1 if previous else 1
            state['balance'] = dict(raw, version=version)
            for key in command.reflected_fills:
                row = state['reservations'].get(key)
                if row is None or row.get('filled_at', command.observed_at) >= command.observed_at:
                    raise ValueError('balance_does_not_confirm_reserved_fill')
                del state['reservations'][key]
            return dict(status='balance_updated', version=version)
        if isinstance(command, ConfirmReduction):
            state['reductions'].setdefault(command.reduction_id, command.at)
            return dict(status='awaiting_fresh_balance')
        if isinstance(command, ReserveEntry):
            return self._reserve(state, command, raw)
        if isinstance(command, EntryResult):
            return self._entry_result(state, command, raw)
        if isinstance(command, CloseResult):
            opened = state['outcomes'].get(command.decision_id)
            if not opened or opened['outcome'] != 'filled' or 'closed_at' in opened:
                raise ValueError('filled_entry_required')
            opened.update(closed_at=command.at, net_usdt=raw['net_usdt'],
                          realized_r=str(command.net_usdt / Decimal(opened['initial_risk_usdt'])))
            return dict(status='closed', realized_r=opened['realized_r'])
        return self._retrieve(state, command)

    def _queue(self, state, job):
        if not job['evidence']['observed_at'] <= job['at'] < job['expires_at']:
            raise ValueError('invalid_review_window')
        subject = job['subject']
        if not self._position_plan_current(state, job):
            return dict(status='position_plan_unavailable')
        latest = state['latest_review'].get(subject)
        if latest and (job['revision'] < latest['revision']
                       or job['evidence']['observed_at'] < latest['observed_at']):
            raise ValueError('stale_review_evidence')
        if (latest and all(job[field] == latest[field] for field in ('revision', 'trigger', 'evidence'))
                and job.get('target_proximity') == latest.get('target_proximity')):
            return dict(status='coalesced')
        pending = state['jobs'].get(subject)
        active = next((row for row in state['active'].values() if row['subject'] == subject), None)
        for previous in (pending, active):
            if previous is None:
                continue
            if job['revision'] < previous['revision'] or job['evidence']['observed_at'] < previous['evidence']['observed_at']:
                raise ValueError('stale_review_evidence')
            same_revision = job['revision'] == previous['revision']
            same_event = same_revision and job['trigger'] == previous['trigger']
            if same_event and job['evidence'] == previous['evidence']:
                return dict(status='coalesced')
            if same_revision and PRIORITY[job['trigger']] > PRIORITY[previous['trigger']]:
                return dict(status='higher_priority_already_queued')
        state['jobs'][subject] = dict(job, attempts=0, ready_at=job['at'])
        state['latest_review'][subject] = dict(id=job['command_id'], revision=job['revision'],
                                               observed_at=job['evidence']['observed_at'],
                                               evidence=job['evidence'], trigger=job['trigger'])
        if 'target_proximity' in job:
            state['latest_review'][subject]['target_proximity'] = job['target_proximity']
        return dict(status='queued')

    def _observe(self, state, observation):
        evidence = observation.evidence
        if not observation.opened_at <= evidence.observed_at <= observation.at:
            raise ValueError('invalid_position_observation')
        target_proximity = None
        if observation.target is not None:
            target_proximity = self._observe_target(state, observation)
            if target_proximity['reconciliation_required']:
                return dict(status='target_reconciliation_required')
        elif not self._position_plan_current(state, observation.model_dump()):
            return dict(status='position_plan_unavailable')
        bars = (evidence.observed_at - observation.opened_at) // SECONDS[evidence.timeframe]
        if bars >= observation.max_holding_bars:
            trigger = 'horizon_due'
        elif target_proximity and target_proximity['within_review_band']:
            trigger = 'near_target'
        elif bars >= observation.no_progress_bars and observation.mfe_r < observation.minimum_progress_r:
            trigger = 'no_progress'
        else:
            return dict(status='no_trigger', elapsed_bars=bars)
        if self._already_observed(state, observation, trigger):
            return dict(status='coalesced', elapsed_bars=bars)
        job = observation.model_dump(mode='json', exclude={'target'})
        job.update(trigger=trigger, expires_at=observation.at + min(SECONDS[evidence.timeframe], 300))
        if trigger == 'near_target':
            job.update(target_proximity=target_proximity,
                       expires_at=observation.target.quote.at + TARGET_QUOTE_MAX_AGE_SECONDS)
        result = self._queue(state, job)
        if result['status'] == 'queued' and trigger != 'near_target':
            state['triggers'][observation.subject] = [observation.revision, trigger]
        return dict(result, elapsed_bars=bars)

    @staticmethod
    def _already_observed(state, observation, trigger):
        if trigger == 'near_target':
            target = state['position_targets'][observation.subject]
            pending = state['jobs'].get(observation.subject)
            return (target['reviewed'] or pending is not None
                    and pending['revision'] == observation.revision and pending['trigger'] == trigger)
        return state['triggers'].get(observation.subject) == [observation.revision, trigger]

    @staticmethod
    def _position_plan_current(state, job):
        target = state.get('position_targets', {}).get(job['subject'])
        return target is None or (
            job['revision'] == target['revision'] and not target['reconciliation_required']
        )

    def _observe_target(self, state, observation):
        target = observation.target
        quote = target.quote
        if (not observation.opened_at <= quote.at <= observation.at
                or observation.at - quote.at >= TARGET_QUOTE_MAX_AGE_SECONDS or quote.bid > quote.ask):
            raise ValueError('invalid_or_stale_target_quote')
        targets = state.setdefault('position_targets', {})
        prior = targets.get(observation.subject)
        revision = observation.revision
        plan = dict(side=target.side, price=str(target.price.normalize()),
                    initial_risk_distance=str(target.initial_risk_distance.normalize()))
        if prior:
            if revision < prior['revision'] or quote.at < prior['quote_at']:
                raise ValueError('stale_position_target_observation')
            if any(plan[field] != prior['plan'][field] for field in ('side', 'initial_risk_distance')):
                raise ValueError('target_observation_changed_position_identity')
            if revision == prior['revision'] and prior['plan'] != plan:
                raise ValueError('target_changed_without_new_plan_revision')
        if prior is None or revision > prior['revision']:
            prior = dict(revision=revision, plan=plan, reconciliation_required=False, reviewed=False)
            targets[observation.subject] = prior
        prior['quote_at'] = quote.at

        # Match the exit replay's conservative touch boundary. A touch requires
        # reconciliation before another AI decision can change this exit plan.
        if target.side == 'long':
            distance = target.price - max(quote.ask, quote.fair)
        else:
            distance = min(quote.bid, quote.fair) - target.price
        prior['reconciliation_required'] |= distance <= 0
        pending = state['jobs'].get(observation.subject)
        if pending and not self._position_plan_current(state, pending):
            del state['jobs'][observation.subject]
        threshold = min(target.initial_risk_distance * TARGET_DISTANCE_R, target.atr * TARGET_DISTANCE_ATR)
        return dict(**plan, quote=quote.model_dump(mode='json'), atr=str(target.atr),
                    distance=str(distance), threshold=str(threshold), within_review_band=0 < distance <= threshold,
                    reconciliation_required=prior['reconciliation_required'])

    def _dispatch(self, state, command):
        active_subjects = {job['subject'] for job in state['active'].values()}
        ready = sorted(
            (j for j in state['jobs'].values() if j['subject'] not in active_subjects
             and j['ready_at'] <= command.at),
            key=lambda j: (PRIORITY[j['trigger']], j['at'], j['subject']),
        )
        leases = []
        slots = min(command.available_slots, max(0, 8 - len(state['active'])))
        for index, job in enumerate(ready[:slots]):
            lease = hashlib.sha256(f'{command.command_id}:{index}'.encode()).hexdigest()
            state['active'][lease] = dict(job, attempts=job['attempts'] + 1,
                                          lease_until=min(job['expires_at'], command.at + 60))
            del state['jobs'][job['subject']]
            if 'target_proximity' in job:
                state['position_targets'][job['subject']]['reviewed'] = True
            leases.append(dict(lease=lease, **state['active'][lease]))
        return dict(status='dispatched' if leases else 'waiting', leases=leases)

    def _review_result(self, state, command, raw):
        job = state['active'].pop(command.lease, None)
        if job is None:
            return dict(status='stale_lease')
        if (state['latest_review'][job['subject']]['id'] != job['command_id']
                or not self._position_plan_current(state, job)):
            return dict(status='superseded')
        if command.action == 'unavailable':
            ready_at = command.at + command.retry_after_seconds
            if job['attempts'] < MAX_ATTEMPTS and ready_at < job['expires_at']:
                state['jobs'][job['subject']] = dict(job, ready_at=ready_at)
                return dict(status='provider_wait', retry_at=ready_at)
        if command.action == 'trade' and job['trigger'] != 'entry':
            raise ValueError('position_review_cannot_open_entry')
        if command.action in ('reduce', 'close', 'revise') and job['trigger'] == 'entry':
            raise ValueError('entry_review_cannot_manage_position')
        if (command.action == 'reduce') != (command.close_fraction is not None):
            raise ValueError('reduce_requires_fraction')
        decision = dict(raw, evidence=job['evidence'], subject=job['subject'],
                        review_id=job['command_id'], revision=job['revision'], trigger=job['trigger'],
                        expires_at=job['expires_at'])
        if 'target_proximity' in job:
            decision['target_proximity'] = job['target_proximity']
        state['decisions'][command.command_id] = decision
        return dict(status='recorded', decision_id=command.command_id)

    def _reserve(self, state, command, raw):
        decision = state['decisions'].get(command.decision_id)
        balance = state['balance']
        if not decision or decision['action'] != 'trade' or decision['expires_at'] <= command.at:
            raise ValueError('fresh_trade_decision_required')
        if state['latest_review'][decision['subject']]['id'] != decision['review_id']:
            raise ValueError('entry_decision_superseded')
        if command.decision_id in state['reservations'] or command.decision_id in state['outcomes']:
            raise ValueError('entry_already_reserved_or_settled')
        if not balance or balance['version'] != command.balance_version or command.at - balance['observed_at'] > 30:
            raise ValueError('fresh_balance_revision_required')
        if not decision['at'] <= command.quote_at <= command.at or command.at - command.quote_at > 30:
            raise ValueError('fresh_execution_quote_required')
        if command.reduction_id:
            confirmed_at = state['reductions'].get(command.reduction_id)
            if (confirmed_at is None or balance['observed_at'] <= confirmed_at
                    or decision['evidence']['observed_at'] <= confirmed_at):
                raise ValueError('reduction_and_fresh_balance_and_decision_required')
        for name in ('margin', 'risk'):
            reserved = sum(Decimal(r[name]) for r in state['reservations'].values())
            if reserved + getattr(command, name) > Decimal(balance['available_' + name]):
                raise ValueError('insufficient_unreserved_' + name)
        state['reservations'][command.decision_id] = dict(margin=raw['margin'], risk=raw['risk'])
        return dict(status='reserved')

    def _entry_result(self, state, command, raw):
        reservation = state['reservations'].get(command.decision_id)
        if reservation is None or command.decision_id in state['outcomes']:
            raise ValueError('unsettled_reservation_required')
        if (command.initial_risk_usdt is not None) != (command.outcome == 'filled'):
            raise ValueError('initial_risk_required_only_for_fill')
        if command.outcome == 'unknown':
            return dict(status='awaiting_reconciliation')
        state['outcomes'][command.decision_id] = raw
        if command.outcome == 'filled':
            reservation['filled_at'] = command.at
        else:
            del state['reservations'][command.decision_id]
        return dict(status=command.outcome)

    def _retrieve(self, state, command):
        evidence = command.evidence
        cutoff = evidence.observed_at
        if cutoff > command.at:
            raise ValueError('retrieval_cutoff_in_future')
        examples = []
        for decision_id, decision in state['decisions'].items():
            past = decision['evidence']
            if decision['at'] >= cutoff or any(
                past[field] != getattr(evidence, field) for field in ('family', 'timeframe', 'regime')
            ):
                continue
            outcome = state['outcomes'].get(decision_id)
            if outcome and outcome['at'] >= cutoff:
                outcome = None
            if outcome and outcome.get('closed_at', cutoff) >= cutoff:
                outcome = {key: outcome[key] for key in ('outcome', 'at')}
            examples.append(dict(decision_id=decision_id, at=decision['at'], action=decision['action'],
                                 evidence=past, reason=decision['reason'], outcome=outcome))
        examples.sort(key=lambda row: (row['at'], row['decision_id']), reverse=True)
        return dict(status='retrieved', matched=len(examples), examples=examples[:command.limit])

    def snapshot(self):
        state = self.store.get(self.key, {})
        return dict(runner_id=RUNNER_ID, runner_name=RUNNER_NAME, mode='SHADOW',
                    pending_reviews=len(state.get('jobs', {})), active_reviews=len(state.get('active', {})),
                    decisions=len(state.get('decisions', {})), reservations=state.get('reservations', {}),
                    balance=state.get('balance'))
