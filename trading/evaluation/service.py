from trading.runtime.live_capabilities import STRUCTURAL_IOC_RUNNERS
from trading.runtime import runner3_policy
import copy
import logging
from pathlib import Path
import sqlite3
import threading
import time
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from trading.agents.schemas import EvaluationResult, GraphAction, MarketAnalystNote, StrategyReviewNote, PortfolioAnalystNote
from trading.evaluation.watch import WatchPlan, WatchCoordinator, ACTIVE, watch_event_key
from trading.evaluation.price_plan import CandleReference, MarketPricePlan,apply_market_price_plan,evidence_from_market
from trading.evaluation.planning import PlanSizing, PlanSizingError, apply_decision_sizing
from trading.evaluation.context import POLICY_VERSION, build_prompt, change_summary, digest, position_cards, setup_card, account_research_context, economic_review_key, canonical_watch_card, watch_review_key, runner3_model_view


class ModelDecision(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    setup_id: str | None = Field(default=None,max_length=64)
    sizing_plan: PlanSizing | None = None
    price_plan: MarketPricePlan | None = None
    watch_plan: WatchPlan | None = None
    reviewed_watch_revision: int | None = Field(default=None,ge=0,strict=True)
    limit_price: float | None = Field(default=None, gt=0)
    release_symbol: str | None = Field(default=None, max_length=40)
    thesis_invalidated: bool = False
    action: str = Field(pattern='^(trade|watch|hold|cancel_watch)$')
    signal_id: str | None
    rationale: str = Field(max_length=1200)
    invalidation: str = Field(max_length=500)
    watch_note: str | None = Field(default='', max_length=1000)

    @field_validator('watch_note')
    @classmethod
    def normalize_watch_note(cls, value):
        return '' if value is None else value

    @field_validator('release_symbol')
    @classmethod
    def blank_release(cls, value):
        if value is None:
            return None
        value = value.strip()
        return value or None


VLM_DECISION_TIMEOUT_SECONDS = 120


def _has_trade_prices(decision, candidate):
    """Entry, stop, and target from the decision, or from the candidate it prices."""
    source = decision.price_plan
    if source is not None:
        entry, stop, target = source.entry_price, source.stop_loss_price, source.take_profit_price
    else:
        entry, stop, target = candidate.entry_price, candidate.stop_loss_price, candidate.take_profit_price
    return all(isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
               for value in (entry, stop, target))


def _decision_packet(decision):
    if decision is None:
        return dict(action=None, rationale='Metin modeli geçerli bir karar üretemedi.',
                    limit_price=None, release_symbol=None, thesis_invalidated=False,
                    setup_id=None, signal_id=None)
    return dict(action=decision.action, rationale=decision.rationale, setup_id=decision.setup_id,
                signal_id=decision.signal_id, invalidation=decision.invalidation,
                limit_price=decision.limit_price, release_symbol=decision.release_symbol,
                thesis_invalidated=bool(decision.thesis_invalidated),
                price_plan=decision.price_plan.model_dump() if decision.price_plan else None,
                sizing_plan=decision.sizing_plan.model_dump(exclude_none=True) if decision.sizing_plan else None,
                watch_plan=decision.watch_plan.model_dump() if decision.watch_plan else None)


SYSTEM_POLICY = (
    'Evaluate the supplied MEXC USDT perpetual candidate using the same policy for text and VLM. '
    'Each request is self-contained: current closed 4h/15m/5m evidence, account state and a durable setup card. '
    'Tables use explicit column names. What_changed explains why this review was scheduled; do not assume hidden chat memory. '
    'Treat confirmed swing/BOS/sweep/FVG evidence as hypotheses, never guaranteed edge. HOLD is normal. '
    'Never force a weekly return, raise risk after losses, invent evidence/IDs, or place orders. '
    'Account operating_context identifies PAPER or LIVE mode, observed balance, closed outcomes and an optional research objective. '
    'A research objective is reporting context only, never a reason to force a trade or exceed risk limits. '
    'Only the supplied symbol may be traded. The seed direction is a screening hypothesis, not a mandatory bias. '
    'When account.setup_selection.enabled, every TRADE MUST select a supplied READY setup_id and provide a grounded price_plan. '
    'This chooses its direction/family; the seed is not a substitute. With no ready setup choose HOLD or a future WATCH. Never invent a setup ID. '
    'Trend continuation needs confirmed directional BOS and structural invalidation; pullback needs a macro-direction retest/reclaim, '
    'so 5m may temporarily oppose 4h. Countertrend needs an actual sweep plus closed reclaim and explicit invalidation, not an overextended indicator. '
    'Universal agreement of 4h/15m/5m is NOT required: explain the relationship for the selected family. '
    'A countertrend selection must stay within setup_selection.countertrend_max_risk_fraction. '
    'READY hypotheses are evidence, not a command to trade; WATCH hypotheses lack the named trigger and cannot be traded yet. '
    'A watch_review candidate is an independent watch/structure event, not a fresh SMA/ROC signal. '
    'Its old SL/TP are context only. Trading watch_review requires a new validated price_plan; otherwise HOLD or update the watch. '
    'canonical_watch.reason=structure_changed means reconsider the thesis, not that the entry threshold was reached. '
    'Return exactly JSON: action (trade/watch/hold/cancel_watch), signal_id (supplied candidate ID or null), '
    'rationale, invalidation, watch_note, sizing_plan, watch_plan, reviewed_watch_revision; optional setup_id selects a supplied setup. '
    'For an actionable watch return watch_plan: trigger_condition (price_above/price_below/closed_candle_cross_above/closed_candle_cross_below), '
    'trigger_price, timeframe (5m/15m/4h, required for candle triggers), invalidation_condition (above/below), '
    'invalidation_price, expires_at (UTC epoch seconds within24h). A watch without watch_plan is a passive note. '
    'For HOLD, TRADE and CANCEL_WATCH always return watch_plan: null, even when canonical_watch contains a plan. '
    'If you need to create or revise a future trigger, use WATCH. HOLD preserves the current watch as-is. '
    'canonical_watch is the current cross-strategy plan for this symbol. If proposing a trade or new watch opposite to its active side, '
    'explicitly set reviewed_watch_revision to that revision and explain concretely in Turkish why the old plan is invalid or superseded. '
    'Use cancel_watch when the canonical active thesis is invalid: signal_id, sizing_plan, price_plan and watch_plan must be null; '
    'reviewed_watch_revision must exactly match the canonical revision and explain why. HOLD preserves an active watch; '
    'writing invalid/cancelled in rationale alone never cancels it. '
    'Never pretend to supersede an unseen plan. For trade choose sizing_plan with integer leverage. '
    f'sizing_plan.risk_per_trade_pct is required. Set it from the account equity and the account target, as a fraction of equity (.01 means 1%), inside the {runner3_policy.CONTINUOUS_STOP_RISK_CAP:.0%} cap. '
    'In LIVE exchange leverage mode an explicit sizing_plan is mandatory: never default to the maximum. '
    'Only when account.price_planning.enabled is true, return a price_plan for the selected setup side and supplied symbol; '
    'without setup selection the original seed side applies. '
    'Its fields: entry_type=market, entry_price, stop_loss_price, take_profit_price, entry_timeframe=5m/15m/4h, '
    'stop_reference and target_reference (timeframe, open_time UTC candle-open seconds, field low/high), reason. '
    'References must exist in supplied recent_ohlcv. LONG stop must be at/below a referenced low, SHORT at/above a high, '
    'at most one recent average true range beyond it. Target must be within one recent ATR of a referenced opposite high/low '
    'on the profit side. Prices must respect supplied tick step. Entry is only a current market reference, not a pending order; '
    'for a distant entry use WATCH. A null price_plan keeps quant levels. When price planning is disabled keep price_plan null. '
    'Never change prices just to force higher leverage or hit a money target. Write concise Turkish rationale explaining the concrete missing confirmation '
    'or rejection. Use watch_note for a brief text note on what would justify reconsideration, or null when inapplicable. '
    'Do not put a watch plan or any structured value in watch_note. Do not just say insufficient evidence. '
    'Keep rationale within1200 characters, invalidation within500 and watch_note within1000. '
    'VLM annotations are deterministic observations, not proof of profit.'
)


def _publish_latest(store, trace):
    """Keep the newest-started review visible without reverting a finished one.

    Caller owns store.lock. Provider callbacks can arrive after another review
    started or the final execution check completed.
    """
    current=store.get('analysis_trace',{})
    if (trace.get('started_at',0),trace.get('id','')) < (current.get('started_at',0),current.get('id','')):
        return
    if current.get('id')==trace.get('id') and current.get('completed_at') and not trace.get('completed_at'):
        return
    if current.get('id')==trace.get('id') and current.get('execution_status') in ('opened','rejected'):
        trace={**trace,'execution_status':current['execution_status'],'execution_reason':current.get('execution_reason')}
    store.put('analysis_trace',copy.deepcopy(trace))


def publish_progress(store, trace):
    with store.lock:
        _publish_latest(store,trace)


def finalize_trace(store, trace_id, status, reason, details=None):
    """Apply an execution result to exactly the proposed review, never its sibling.

    Returns False for an absent/already-finalized/non-trade review. No model HOLD
    or unfinished provider call can accidentally become an OPEN notification.
    """
    if status not in ('opened','rejected','not_opened'):
        raise ValueError('invalid execution status')
    from trading.runtime.usage import archive_trace
    with store.lock:
        history=store.get('analysis_history',[])
        trace=next((row for row in history if row.get('id')==trace_id),None)
        if trace is None:
            latest=store.get('analysis_trace',{})
            if latest.get('id')==trace_id:
                trace=latest
        if trace is None:
            table=store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_usage_events'").fetchone()
            row=store.db.execute('SELECT payload FROM ai_usage_events WHERE id=?',(trace_id,)).fetchone() if table else None
            if row:
                import json
                trace=json.loads(row[0])
        if trace is None or trace.get('action')!='trade' or trace.get('execution_status')!='awaiting_risk':
            return False
        trace=copy.deepcopy(trace)
        trace.update(execution_status=status,execution_reason=reason)
        if details is not None:trace['execution_details']=copy.deepcopy(details)
        archive_trace(store,trace)
        for row in history:
            if row.get('id')==trace_id:
                row.update(trace)
        store.put('analysis_history',history)
        _publish_latest(store,trace)
        return True


def _ground_card_plan(setup, candidate, chart_context, *, entry, fee, slippage, price_step, now, allow_blocked=False):
    """A price plan from the card's stop and a supplied target. None when it cannot be grounded."""
    evidence = evidence_from_market(chart_context)
    levels = list(runner3_policy.clearing_supplied_targets(
        setup, entry=entry, fee=fee, slippage=slippage, price_step=price_step))
    if allow_blocked and not levels:
        for level in setup.get('opposing_target_levels') or []:
            price = level.get('price') if isinstance(level, dict) else None
            if isinstance(price, (int, float)) and not isinstance(price, bool) and price > 0:
                levels.append(dict(level, take_profit_price=price))
    for level in levels:
        if any(level.get(key) is None for key in ('timeframe', 'open_time', 'field', 'take_profit_price')):
            continue
        try:
            plan = MarketPricePlan.model_validate(dict(
                entry_type='market', entry_price=entry,
                stop_loss_price=setup['stop_loss_price'],
                take_profit_price=level['take_profit_price'],
                entry_timeframe=setup['trigger_timeframe'],
                stop_reference=setup['stop_reference'],
                target_reference={key: level[key] for key in ('timeframe', 'open_time', 'field')},
                reason='Hazır kurulumun verilen hedefi. Stop aynı kaldı.'))
            apply_market_price_plan(candidate, plan, evidence, price_step=price_step,
                                    current_price=entry, now=now)
            runner3_policy.validate_evidence(setup, plan, now)
        except (ValueError, TypeError):
            continue
        return plan
    return None


def _ready_hold_trade(decision, candidate, options, chart_context, prior_watch, *, entry, fee, slippage, price_step, now):
    """A valid hold becomes a trade at the farthest supplied level that clears 1.25.

    The setup stop stays. A level that was not supplied is not created. No
    qualifying level, a plan that does not ground, or a conflicting watch
    leaves the hold. Silence never reaches this function.
    """
    if decision.action != 'hold' or decision.price_plan is not None or decision.limit_price is not None:
        return None
    setup = runner3_policy.selected_closed_setup(candidate, options)
    if setup is None:
        return None
    if (prior_watch.get('status') in ACTIVE and prior_watch.get('side') != setup.get('side')
            and (decision.reviewed_watch_revision != prior_watch.get('revision')
                 or len(decision.rationale.strip()) < 12)):
        return None
    plan = _ground_card_plan(
        setup, candidate, chart_context, entry=entry, fee=fee, slippage=slippage,
        price_step=price_step, now=now)
    if plan is None:
        return None
    return decision.model_copy(update=dict(
        action='trade', signal_id=candidate.signal_id, setup_id=setup['setup_id'],
        price_plan=plan, watch_plan=None, limit_price=None, release_symbol=None,
        thesis_invalidated=False))


def _ready_retrigger_card(card):
    """Closed CHoCH at a block or flow, with a stop. Awaiting touch is not this card."""
    if not runner3_policy.closed_choch_at_block_or_flow(card):
        return False
    stop = card.get('stop_loss_price', card.get('stop'))
    return isinstance(stop, (int, float)) and not isinstance(stop, bool) and stop > 0


def _retrigger_card(candidate, options):
    """The current card for this hold. A ready card on the same side wins."""
    side = getattr(candidate.side, 'value', None)
    features = candidate.features if isinstance(candidate.features, dict) else {}
    rows = [row for row in options or [] if isinstance(row, dict) and row.get('side') == side]
    ready = [row for row in rows if _ready_retrigger_card(row)]
    ident = features.get('setup_id')
    if ready:
        named = next((row for row in ready if row.get('setup_id') == ident), None)
        if named is not None:
            return named
        timed = [row for row in ready if row.get('trigger_timeframe') == features.get('seed_timeframe')]
        return (timed or ready)[0]
    named = next((row for row in rows if row.get('setup_id') == ident), None)
    if named is not None:
        return named
    return rows[0] if rows else None


def _rearm_printed_hold(evaluator, candidate, card, chart_context, price_rules):
    """Keep the hold and store a new numeric trigger. The model is not asked."""
    from trading.evaluation.hold_triggers import save_hold, snapshot_market, trigger_levels
    prior = candidate.features.get('prior_hold') if isinstance(candidate.features.get('prior_hold'), dict) else {}
    rationale = str(prior.get('rationale') or candidate.rationale or 'Kapalı CHoCH yok.')
    levels = trigger_levels(card if isinstance(card, dict) else None, candidate)
    if not levels:
        return EvaluationResult(action=GraphAction.HOLD, hold_reason='hold',
            metadata={'provider_called': False, 'model_not_asked': True})
    held = save_hold(
        evaluator.store, symbol=candidate.symbol, rationale=rationale, levels=levels,
        side=(card or {}).get('side') if isinstance(card, dict) else candidate.side.value,
        setup_id=None if not isinstance(card, dict) else card.get('setup_id'),
        old_market=snapshot_market(chart_context),
        trigger_timeframe=None if not isinstance(card, dict) else card.get('trigger_timeframe'),
        price=candidate.entry_price, tick=price_rules.price_step if price_rules is not None else 0)
    return EvaluationResult(action=GraphAction.HOLD, hold_reason='hold',
        metadata={'provider_called': False, 'model_not_asked': True, 'price_action_hold': held})


def _ready_card_decision(candidate, card, chart_context, *, entry, price_step, now):
    """A trade from the ready card at the current price, or None when it will not ground."""
    target, reference, _level = runner3_policy.retrigger_target(card, price_step=price_step)
    stop = card.get('stop_loss_price')
    if not isinstance(reference, dict) or any(reference.get(key) is None for key in ('timeframe', 'open_time', 'field')):
        return None
    if not runner3_policy._positive_number(target) or not runner3_policy._positive_number(stop):
        return None
    try:
        opened = int(reference['open_time'])
    except (TypeError, ValueError):
        return None
    try:
        plan = MarketPricePlan.model_validate(dict(
            entry_type='market', entry_price=entry, stop_loss_price=stop, take_profit_price=target,
            entry_timeframe=card['trigger_timeframe'], stop_reference=card['stop_reference'],
            target_reference=dict(timeframe=reference['timeframe'], open_time=opened, field=reference['field']),
            reason='Hazır kurulumun verilen hedefi. Stop aynı kaldı.'))
        from trading.evaluation.setup_selection import selected_candidate
        probe = selected_candidate(candidate, card)
        apply_market_price_plan(probe, plan, evidence_from_market(chart_context),
            price_step=price_step, current_price=entry, now=now)
        runner3_policy.validate_evidence(card, plan, now)
    except (KeyError, TypeError, ValueError):
        return None
    rationale = (
        f'{candidate.symbol} hazır kurulumdan güncel fiyattan girildi. '
        'Kapalı CHoCH, blok veya akış ve stop vardı. Model yeniden sorulmadı.')
    return ModelDecision(
        action='trade', signal_id=candidate.signal_id, setup_id=card['setup_id'],
        rationale=rationale[:1200], invalidation='Kapalı CHoCH stopu bozulursa işlem biter.',
        price_plan=plan)


def _early_card(card):
    if not isinstance(card, dict):
        return False
    evidence = card.get('evidence') if isinstance(card.get('evidence'), dict) else {}
    return card.get('state') == 'ready' and evidence.get('kind') == 'htf_zone_internal'


def _near(left, right):
    if not runner3_policy._positive_number(left) or not runner3_policy._positive_number(right):
        return left is None and right is None
    return abs(float(left) - float(right)) <= max(1e-8, abs(float(right)) * 1e-8)


def _matches_acceptance(saved, card):
    """The model already agreed to this weak-swing plan."""
    if not isinstance(saved, dict) or not _early_card(card):
        return False
    evidence = card['evidence']
    # The stop buffer moves with ATR. Agreement is the weak level and the side.
    return (saved.get('side') == card.get('side')
            and _near(saved.get('weak_level'), evidence.get('weak_level')))


def _acceptance_snapshot(card):
    evidence = card.get('evidence') if isinstance(card.get('evidence'), dict) else {}
    return dict(side=card.get('side'), kind=evidence.get('kind'),
                weak_level=evidence.get('weak_level'), stop=card.get('stop_loss_price'),
                target=card.get('take_profit_price'), setup_id=card.get('setup_id'))


def _structure_review(candidate, prior_watch):
    reason = candidate.features.get('review_reason') if isinstance(candidate.features, dict) else None
    return reason == 'structure_changed' or (isinstance(prior_watch, dict) and prior_watch.get('reason') == 'structure_changed')


def _awaiting_only(options):
    staged = [row for row in options or [] if isinstance(row, dict) and isinstance(row.get('evidence'), dict)
              and row['evidence'].get('stage')]
    return bool(staged) and all(row['evidence'].get('stage') == 'awaiting_touch' for row in staged) and not any(
        row.get('state') == 'ready' for row in options or [] if isinstance(row, dict))


def _one_touch_level(options, price):
    waiting = [row for row in options or [] if isinstance(row, dict) and isinstance(row.get('evidence'), dict)
               and row['evidence'].get('stage') == 'awaiting_touch' and runner3_policy._positive_number(row.get('trigger'))]
    if not waiting:
        return None
    row = min(waiting, key=lambda item: abs(float(item['trigger']) - float(price or 0)))
    return row, dict(kind='zone_edge', price=float(row['trigger']))


def _consume_structure(coordinator, candidate, prior_watch):
    if _structure_review(candidate, prior_watch):
        coordinator.consume_structure_review(candidate.symbol)


def _internal_entry_decision(candidate, card, chart_context, *, entry, stop, fee, slippage,
                             price_step, equity, free_margin, now):
    """A second look at a plan the model already accepted. The first look still asks."""
    chosen = runner3_policy.choose_zone_target(
        card, entry=entry, stop=stop, fee=fee, slippage=slippage, price_step=price_step)
    if chosen is None:
        return None, None
    target, _ratio, level = chosen
    if not isinstance(level, dict):
        return None, None
    failed = runner3_policy.entry_veto(
        side=card.get('side'), entry=entry, stop=stop, target=target,
        equity=equity, free_margin=free_margin, fee=fee, slippage=slippage)
    if failed:
        return None, failed
    reference = {key: level.get(key) for key in ('timeframe', 'open_time', 'field')}
    if any(reference[key] is None for key in reference):
        return None, None
    try:
        opened = int(reference['open_time'])
        plan = MarketPricePlan.model_validate(dict(
            entry_type='market', entry_price=entry, stop_loss_price=stop, take_profit_price=target,
            entry_timeframe=card['trigger_timeframe'], stop_reference=card['stop_reference'],
            target_reference=dict(timeframe=reference['timeframe'], open_time=opened, field=reference['field']),
            reason='Model bu planı kabul etmişti. Stop aynı kaldı.'))
        from trading.evaluation.setup_selection import selected_candidate
        probe = selected_candidate(candidate, card)
        apply_market_price_plan(probe, plan, evidence_from_market(chart_context),
            price_step=price_step, current_price=entry, now=now)
        runner3_policy.validate_evidence(card, plan, now)
    except (KeyError, TypeError, ValueError):
        return None, None
    rationale = f'{candidate.symbol} için model bu planı kabul etmişti. Model yeniden sorulmadı.'
    return ModelDecision(
        action='trade', signal_id=candidate.signal_id, setup_id=card['setup_id'],
        rationale=rationale[:1200], invalidation='5m swing stopu bozulursa işlem biter.',
        price_plan=plan), None


class ContextEvaluator:
    """Self-contained current evidence + bounded application-owned setup memory.

    Full journals/charts remain server-side. Table encoding preserves supplied
    numerical evidence; working memory is explicitly a summary, not a full chat.
    """
    def __init__(self, llm, store, public, settings):
        self.llm, self.store, self.public, self.cfg = llm, store, public, settings
        self.on_live_review = None
        if hasattr(llm, 'response_validator'):
            llm.response_validator = ModelDecision.model_validate
        if hasattr(llm, 'decision_schema'):
            llm.decision_schema = ModelDecision.model_json_schema()

    def evaluate_candidates(self, candidates, *, equity, risk_per_trade_pct, target_leverage, **context):
        from trading.analysis import build_bundle, render_bundle
        if not candidates:
            return EvaluationResult(action=GraphAction.HOLD, hold_reason='no_candidates',metadata={'provider_called':False})
        candidate = max(candidates, key=lambda c:c.confidence)
        symbol = candidate.symbol
        live_account=context.pop('live_account_context',None)
        if live_account is not None and (not isinstance(live_account,dict) or live_account.get('mode')!='LIVE' or live_account.get('currency')!='USDT'):
            return EvaluationResult(action=GraphAction.HOLD,hold_reason='invalid_live_account_context',metadata={'provider_called':False})
        runner2=bool(live_account and live_account.get('runner_id') in STRUCTURAL_IOC_RUNNERS)
        runner3=bool(live_account and live_account.get('runner_id')=='runner3')
        selection_enabled=bool(live_account is not None and getattr(self.cfg,'live_structure_setups_enabled',False) and getattr(self.cfg,'live_price_plan_enabled',False))
        scope = 'symbol_setups' if selection_enabled else f'{candidate.strategy.value}:{candidate.side.value}'
        if live_account is not None:
            scope=f'{self.cfg.analysis_version}:'+scope
        if runner2:
            scope=live_account['runner_id']+':'+scope
        self.store.put('stage','fetching_multitimeframe_context')
        bundle = build_bundle(self.public, symbol, include_hourly=True) if runner3 else build_bundle(self.public, symbol)
        chart_context = (bundle.to_context(ict=True, htf_history=True) if runner3
                         else bundle.to_context(ict=True) if runner2 else bundle.to_context())
        fingerprint = bundle.fingerprint
        if runner2:
            from trading.analysis.market import benchmark_context
            chart_context['benchmarks'] = (benchmark_context(self.public,chart_context,include_structure=True)
                                           if runner3 else benchmark_context(self.public,chart_context))
        key = f'memory:{symbol}:{scope}'
        legacy = self.store.get('memory:'+symbol, {})
        memory = self.store.get(key)
        if memory is None:
            # Old unscoped memory is migrated only when the scope is compatible.
            memory=copy.deepcopy(legacy) if not runner2 and legacy.get('scope') in (None,scope) else {}
        memory.setdefault('history',[])
        memory.setdefault('watch_note','')
        memory.setdefault('last_fingerprint',None)
        if legacy.get('last_outcome') is not None:
            memory['last_outcome']=copy.deepcopy(legacy['last_outcome'])
        coordinator=WatchCoordinator(self.store)
        prior_watch=coordinator.card(symbol)
        if candidate.features.get('watch_review') and (prior_watch.get('status')!='triggered'
                or prior_watch.get('plan',{}).get('expires_at',0)<=time.time()
                or candidate.features.get('review_event_key')!=watch_event_key(prior_watch)):
            return EvaluationResult(action=GraphAction.HOLD,hold_reason='stale_watch_review',metadata={'provider_called':False})
        live_positions=context.pop('live_positions',None)
        positions = live_positions if live_account is not None and isinstance(live_positions,dict) else self.store.get('paper', {}).get('positions', {})
        account=dict(equity=equity,risk_per_trade_pct=risk_per_trade_pct,leverage=target_leverage,**context)
        exchange_cap=target_leverage
        if callable(getattr(self.public,'get_contract_rules',None)):
            exchange_cap=self.public.get_contract_rules(symbol).max_leverage
        account['sizing_limits']=dict(max_risk_fraction=risk_per_trade_pct,policy_max_leverage=target_leverage,exchange_max_leverage=exchange_cap)
        account['operating_context']=copy.deepcopy(live_account) if live_account is not None else account_research_context(self.store,equity,self.cfg)
        price_planning=bool(live_account is not None and getattr(self.cfg,'live_price_plan_enabled',False))
        price_rules=self.public.get_contract_rules(symbol) if price_planning else None
        account['price_planning']=dict(enabled=price_planning,price_step=price_rules.price_step if price_rules else None,
            market_entry_only=True,reference_policy='closed recent OHLCV wick + at most1 recent ATR buffer')
        account['canonical_watch']=canonical_watch_card(prior_watch)
        options=[]
        if selection_enabled:
            from trading.quant.structure_setups import build_htf_zone_setups, build_structure_setups
            if runner3:
                options=build_htf_zone_setups(chart_context,current_price=candidate.entry_price,price_step=price_rules.price_step)
            else:
                frames=('5m','15m','4h') if runner2 else (None,)
                options=[setup for frame in frames for setup in build_structure_setups(chart_context,
                    current_price=candidate.entry_price,price_step=price_rules.price_step,trigger_timeframe=frame)]
        model_sizing = bool(runner3 and live_account.get('aggressive_model_sizing'))
        continuous = bool(runner3 and live_account.get('continuous_aggressive'))
        countertrend_cap = risk_per_trade_pct if continuous else risk_per_trade_pct * .5
        if runner3:
            fee, slippage = runner3_policy.prompt_entry_costs(
                self.cfg, live_account.get('runner3_entry_costs'))
            account['runner3_entry_gate'] = runner3_policy.entry_gate_instruction(fee=fee, slippage=slippage)
            balance=live_account['current_equity']
            for option in options:
                grade=runner3_policy.evidence_grade(option)
                allocation=runner3_policy.margin_policy(balance,equity,live_account['available_usdt'],
                    grade,option['horizon'],model_sizing=model_sizing)
                ceiling=countertrend_cap if option['family']=='countertrend_sweep_reclaim' else risk_per_trade_pct
                target_risk=None if model_sizing else runner3_policy.small_account_risk_target(balance,ceiling,grade)
                option['allocation']=dict(allocation,
                    suggested_risk_fraction=target_risk if target_risk is not None else ceiling*(.8 if grade=='strong' else .5),
                    **({'margin_fraction_role':'silent_fallback'} if grade=='strong' else {}))
                if model_sizing:
                    continue
                choice=runner3_policy.guclu_margin_request(
                    option, equity=balance,
                    free_margin=max(0., float(live_account['available_usdt'])-allocation['reserve_usdt']),
                    open_positions=list(live_account.get('open_position_briefs') or []),
                    loss_ceiling_usdt=float(equity)*ceiling)
                if choice is not None:
                    option['guclu_margin_choice']=choice
            equity_now = live_account.get('current_equity')
            account['capital_allocation']=dict(
                equity_usdt=equity_now, available_usdt=live_account.get('available_usdt'),
                small_account=isinstance(equity_now,(int,float)) and not isinstance(equity_now,bool)
                    and 0 < equity_now < runner3_policy.SMALL_ACCOUNT_EQUITY_USDT,
                open_positions=list(live_account.get('open_position_briefs') or []),
                queued_confirmed_plans=runner3_policy.rank_confirmed_plans(live_account.get('queued_confirmed_plans') or []),
                note=('Confirmed plans share one balance. Simultaneous local limits are ranked by cost-adjusted R. '
                      'A local limit does not reserve margin. Equity under 100 USDT still needs a valid zone, '
                      'a closed touch, and a later closed CHoCH/MSS. It is not a BEKLE reason when a supplied target clears cost-adjusted R 1.25. Touch alone never places an order. '
                      'Balance, free margin, and leverage (the multiplier) stay in this packet so you can size. They are not BEKLE reasons. '
                      'When 1.25 would otherwise block, you may choose a farther supplied opposing target you judge has more potential. That choice stands. '
                      'The risk engine still caps size and loss. Open positions include unrealized pnl, thesis, mark, stop, and remaining room. '
                      'release_symbol closes one different open position only when its thesis is invalidated and free margin cannot fund the new plan. '
                      'The close uses the single writer and the new entry waits until the exchange confirms that close.'))
        account['setup_selection']=dict(enabled=selection_enabled,options=options,
            countertrend_max_risk_fraction=countertrend_cap,
            note='Select setup_id and provide a fresh grounded price_plan for TRADE. No hypothesis means no forced trade.')
        if runner3:
            waiting=[]
            for row in options:
                if runner3_policy.closed_choch_at_block_or_flow(row):
                    copy_row=dict(row)
                    copy_row['symbol']=symbol
                    waiting.append(copy_row)
            ranked=runner3_policy.rank_setups(waiting, fee=fee, slippage=slippage)
            required=None if not ranked else (ranked[0].get('allocation') or {}).get('target_margin_usdt')
            report=runner3_policy.swap_candidate(
                waiting, live_account.get('open_position_briefs') or [],
                free_margin=live_account.get('available_usdt'), required_margin=required,
                fee=fee, slippage=slippage)
            if report:
                account['swap_candidate']=report
                self.store.put('swap_candidate:'+symbol, report)
                logging.getLogger('worker').info(
                    'SWAP %s expected_r=%s open=%s', report['waiting'].get('symbol'),
                    report['waiting'].get('expected_r'),
                    ' '.join(str(row.get('symbol')) for row in report['open_positions']))
        economic_basis=economic_review_key(account,positions)
        economic_basis['account']['canonical_watch']=watch_review_key(prior_watch)
        if runner2:
            economic_basis['benchmarks']=chart_context['benchmarks']
        policy_version=POLICY_VERSION
        if runner3:
            policy_version=(runner3_policy.CONTINUOUS_COHORT if continuous else
                            runner3_policy.MODEL_SIZING_COHORT if model_sizing else runner3_policy.COHORT)
        evaluation_key=digest(dict(policy=policy_version,market=fingerprint,scope=scope,economic_basis=economic_basis))
        if memory.get('last_evaluation_key')==evaluation_key and not candidate.features.get('hold_review'):
            self.store.put('stage','unchanged_closed_candles')
            return EvaluationResult(action=GraphAction.HOLD,hold_reason='unchanged_closed_candles',
                metadata={'provider_called':False,'fingerprint':fingerprint})
        failure=memory.get('last_failure') or {}
        if failure.get('evaluation_key')==evaluation_key and failure.get('retry_at',0)>time.time():
            self.store.put('stage','analysis_error_backoff')
            return EvaluationResult(action=GraphAction.HOLD,hold_reason='analysis_error_backoff',
                metadata={'provider_called':False,'fingerprint':fingerprint,'retry_at':failure['retry_at']})
        if self.llm.budget is not None and not self.llm.budget.can_spend():
            self.llm.last_status='budget_exhausted'
            return EvaluationResult(action=GraphAction.HOLD,hold_reason='shared_budget_exhausted',metadata={'provider_called':False})
        changes=change_summary(memory.get('last_market'),chart_context,memory.get('last_fingerprint'),fingerprint)
        if candidate.features.get('watch_review'):
            structural=prior_watch.get('reason')=='structure_changed'
            changes.update(reason='watch_structure_changed' if structural else 'watch_triggered',
                summary='Takip gerekçesine karşı kapanmış yapısal kırılım oluştu; plan yeniden inceleniyor.' if structural else 'Takip koşulu gerçekleşti; eski algoritmadan yeni sinyal beklenmeden plan inceleniyor.')
        prior_hold=candidate.features.get('prior_hold') if candidate.features.get('hold_review') else None
        if isinstance(prior_hold, dict):
            account['prior_hold']=copy.deepcopy(prior_hold)
            changes.update(reason='hold_trigger_printed',
                summary='Bekleme tetiki basıldı. Eski gerekçe: '+str(prior_hold.get('rationale') or '')[:240])
        direct_decision = None
        model_not_asked = False
        if runner3 and candidate.features.get('hold_review') and price_rules is not None:
            # A printed number enters only when it matches a plan the model already accepted.
            card = _retrigger_card(candidate, options)
            prior = candidate.features.get('prior_hold') if isinstance(candidate.features.get('prior_hold'), dict) else {}
            accepted_print = bool(prior.get('model_accepted')) and any(
                _near(prior.get('printed_trigger'), item.get('price'))
                for item in (prior.get('triggers') or []) if isinstance(item, dict))
            if _early_card(card) and accepted_print:
                cash_equity, cash_free = runner3_policy.cash_for_veto(equity, live_account)
                direct_decision, failed = _internal_entry_decision(
                    candidate, card, chart_context, entry=candidate.entry_price,
                    stop=card.get('stop_loss_price'), fee=fee, slippage=slippage,
                    price_step=price_rules.price_step, equity=cash_equity, free_margin=cash_free,
                    now=time.time())
                if failed:
                    logging.getLogger('worker').info('VETO %s check=%s', symbol, failed)
                    _consume_structure(coordinator, candidate, prior_watch)
                    return EvaluationResult(action=GraphAction.HOLD, hold_reason=failed,
                        metadata={'provider_called': False, 'model_not_asked': True, 'veto': failed})
                if direct_decision is None:
                    return _rearm_printed_hold(self, candidate, card, chart_context, price_rules)
                model_not_asked = True
            elif not _early_card(card):
                if not _ready_retrigger_card(card):
                    return _rearm_printed_hold(self, candidate, card, chart_context, price_rules)
                direct_decision = _ready_card_decision(
                    candidate, card, chart_context, entry=candidate.entry_price,
                    price_step=price_rules.price_step, now=time.time())
                if direct_decision is None:
                    return _rearm_printed_hold(self, candidate, card, chart_context, price_rules)
                model_not_asked = True
        if (runner3 and not model_not_asked and price_rules is not None
                and not candidate.features.get('hold_review')):
            saved = self.store.get('accepted_entry:' + symbol)
            match = next((row for row in options if _matches_acceptance(saved, row)), None)
            if match is not None and symbol in (live_positions or {}):
                return EvaluationResult(action=GraphAction.HOLD, hold_reason='open_position',
                    metadata={'provider_called': False, 'model_not_asked': True})
            if match is not None:
                cash_equity, cash_free = runner3_policy.cash_for_veto(equity, live_account)
                direct_decision, failed = _internal_entry_decision(
                    candidate, match, chart_context, entry=candidate.entry_price,
                    stop=match.get('stop_loss_price'), fee=fee, slippage=slippage,
                    price_step=price_rules.price_step, equity=cash_equity, free_margin=cash_free,
                    now=time.time())
                if failed:
                    logging.getLogger('worker').info('VETO %s check=%s', symbol, failed)
                    return EvaluationResult(action=GraphAction.HOLD, hold_reason=failed,
                        metadata={'provider_called': False, 'model_not_asked': True, 'veto': failed})
                if direct_decision is not None:
                    model_not_asked = True
        if (runner3 and not model_not_asked and price_rules is not None
                and _structure_review(candidate, prior_watch) and _awaiting_only(options)):
            from trading.evaluation.hold_triggers import save_hold, snapshot_market
            picked = _one_touch_level(options, candidate.entry_price)
            if picked is not None:
                row, level = picked
                held = save_hold(
                    self.store, symbol=symbol, rationale='Bölgeye temas yok. Tek tetik saklandı.',
                    levels=[level], side=row.get('side') or candidate.side.value,
                    setup_id=row.get('setup_id'), old_market=snapshot_market(chart_context),
                    trigger_timeframe=row.get('trigger_timeframe'), price=candidate.entry_price,
                    tick=price_rules.price_step, single_trigger=True, model_accepted=False)
                _consume_structure(coordinator, candidate, prior_watch)
                logging.getLogger('worker').info('HOLD %s single trigger=%s', symbol, level['price'])
                return EvaluationResult(action=GraphAction.HOLD, hold_reason='hold',
                    metadata={'provider_called': False, 'model_not_asked': True, 'price_action_hold': held})
        if runner2:
            from trading.analysis.market import trade_flow_context
            chart_context['recent_trade_flow']=trade_flow_context(self.public,symbol)
        if runner3 and any(option['state']=='ready' and option.get('context_timeframe')=='1h' for option in options):
            from trading.analysis.market import entry_refinement_context
            chart_context['entry_refinement_1m'] = entry_refinement_context(self.public,chart_context)
        model_account, model_market = (runner3_model_view(account, chart_context) if runner3 else (account, chart_context))
        prompt=build_prompt(candidate,model_market,memory,positions,model_account,self.cfg.analysis_version,changes)
        try:
            charts=render_bundle(bundle,Path(self.cfg.analysis_chart_dir)/symbol/fingerprint[:16])
        except (OSError, ValueError, RuntimeError):
            # A missing chart must not block Runner 3. The text decision stands.
            if not runner3:
                raise
            charts=[]
        paths=[str(c.path) for c in charts]
        started=time.time()
        trace=dict(id=uuid4().hex,mode='LIVE' if live_account is not None else 'PAPER',symbol=symbol,strategy=candidate.strategy.value,confidence=candidate.confidence,
            runner_id=(live_account or {}).get('runner_id','baseline-v1'),
            analysis_version=self.cfg.analysis_version,started_at=started,status='pending',attempts=[],provider='none',model='',
            why_reconsidered=changes,seed_timeframe=candidate.features.get('seed_timeframe'),
            market_fingerprint=fingerprint,input_fingerprint=digest(prompt),
            context_policy=POLICY_VERSION,prompt_bytes=len(prompt.encode()))
        if runner2:
            trace['strategy_cohort']=policy_version if runner3 else 'runner2-multihorizon-ict-v1'
        publish_progress(self.store,trace)
        if live_account is not None and self.on_live_review is not None:
            self.on_live_review(trace)
        self.store.put('stage','model_evaluation')
        deadline=started+self.cfg.decision_ttl_seconds
        reentry_note={}
        def validate(payload):
            decision=ModelDecision.model_validate(payload)
            from trading.evaluation.setup_selection import awaiting_touch, closed_structure_card, validate_selection
            if selection_enabled and isinstance(decision.setup_id, str):
                named=next((row for row in options if isinstance(row, dict) and row.get('setup_id')==decision.setup_id), None)
                if named is not None and decision.action=='trade' and awaiting_touch(named):
                    # Touch has not happened. Keep the parseable answer as a hold.
                    decision=decision.model_copy(update=dict(
                        action='hold', price_plan=None, sizing_plan=None, limit_price=None,
                        watch_plan=None, release_symbol=None, thesis_invalidated=False))
                elif (named is not None and decision.action=='trade' and decision.price_plan is None
                        and (closed_structure_card(named) or _early_card(named))
                        and runner3 and price_rules is not None):
                    # The card already has the stop and a supplied target. A missing
                    # plan is filled from that card. It is not a discarded response.
                    try:
                        plan=_ground_card_plan(
                            named, candidate, chart_context, entry=candidate.entry_price, fee=fee, slippage=slippage,
                            price_step=price_rules.price_step, now=time.time(), allow_blocked=True)
                    except (KeyError, TypeError, ValueError):
                        plan=None
                    if plan is None:
                        decision=decision.model_copy(update=dict(
                            action='hold', price_plan=None, sizing_plan=None, limit_price=None))
                    else:
                        decision=decision.model_copy(update=dict(price_plan=plan))
            if model_sizing and decision.action == 'trade' and decision.sizing_plan is None:
                raise ValueError('aggressive Runner 3 requires explicit sizing')
            chosen,option=validate_selection(decision,candidate,options,selection_enabled)
            if (decision.action=='trade' and decision.sizing_plan is not None
                    and decision.sizing_plan.risk_per_trade_pct is None and _has_trade_prices(decision, candidate)):
                # The model did not set the share. Fill it from equity sizing inside
                # the cap. A share the model already sent is left unchanged.
                ceiling=countertrend_cap if option and option['family']=='countertrend_sweep_reclaim' else risk_per_trade_pct
                from trading.evaluation.planning import filled_risk_share
                decision=decision.model_copy(update={'sizing_plan': decision.sizing_plan.model_copy(
                    update={'risk_per_trade_pct': filled_risk_share(ceiling)})})
            share=None if decision.sizing_plan is None else decision.sizing_plan.risk_per_trade_pct
            if (option and option['family']=='countertrend_sweep_reclaim' and decision.action=='trade'
                    and (decision.sizing_plan is None or (share is not None and share>countertrend_cap))):
                raise ValueError('countertrend sizing exceeds reduced risk limit')
            if decision.action=='cancel_watch':
                if (prior_watch.get('status') not in ACTIVE or decision.reviewed_watch_revision!=prior_watch['revision']
                        or len(decision.rationale.strip())<12 or any(v is not None for v in (decision.signal_id,decision.sizing_plan,decision.price_plan,decision.watch_plan))):
                    raise ValueError('cancel_watch requires active reviewed revision and no order fields')
            if (decision.action=='trade' and live_account is not None
                    and (live_account.get('leverage_policy') or {}).get('mode')=='exchange' and decision.sizing_plan is None):
                raise ValueError('explicit model sizing required for exchange leverage mode')
            if decision.action=='trade' and candidate.features.get('watch_review') and decision.price_plan is None:
                raise ValueError('independent watch trade requires a fresh price plan')
            if decision.action=='trade' and runner2 and decision.price_plan is None:
                raise ValueError('Runner 2 requires a candle-grounded price plan')
            if runner3 and decision.action=='trade':
                runner3_policy.validate_evidence(option,decision.price_plan)
            if decision.limit_price is not None:
                if decision.action != 'trade' or decision.release_symbol:
                    raise ValueError('limit price is a local entry, not a close and not a non-trade')
                side = (option or {}).get('side') or candidate.side.value
                if runner3 and (option is None or option.get('state') != 'ready'):
                    raise ValueError('limit price requires a ready setup')
                if side == 'long' and not decision.limit_price < candidate.entry_price:
                    raise ValueError('long limit must be below the market')
                if side != 'long' and not decision.limit_price > candidate.entry_price:
                    raise ValueError('short limit must be above the market')
            if decision.release_symbol:
                if not runner3 or decision.action != 'trade':
                    raise ValueError('release_symbol is only for a runner3 market entry')
                opens = [row for row in (account.get('capital_allocation') or {}).get('open_positions') or []
                         if isinstance(row, dict)]
                position = next((row for row in opens if row.get('symbol') == decision.release_symbol), None)
                if position is None or decision.release_symbol == symbol:
                    raise ValueError('release_symbol must be a different open position')
                if len(decision.rationale.strip()) < 12:
                    raise ValueError('release requires a rationale')
                # A more profitable plan is not a close. Both gates are required.
                cost = ((option or {}).get('allocation') or {}).get('max_entry_cost_usdt')
                funded = not (isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost <= 0)
                if funded or not decision.thesis_invalidated or not runner3_policy.thesis_invalidated(position, position.get('mark')):
                    decision.release_symbol = None
                    decision.thesis_invalidated = False
            if (runner3 and candidate.features.get('hold_review') and decision.action=='trade'
                    and isinstance(option, dict) and decision.price_plan is not None):
                cash_equity, cash_free = runner3_policy.cash_for_veto(equity, live_account)
                prices, failed = runner3_policy.priced_reentry(
                    option, price=candidate.entry_price, equity=cash_equity, free_margin=cash_free,
                    fee=fee, slippage=slippage, price_step=price_rules.price_step)
                if failed:
                    reentry_note['veto']=failed
                else:
                    update=dict(entry_price=prices['entry'], stop_loss_price=prices['stop'],
                                take_profit_price=prices['target'])

                    def _reference(source):
                        if not isinstance(source, dict) or any(source.get(key) is None for key in ('timeframe', 'open_time', 'field')):
                            return None
                        try:
                            opened=int(source['open_time'])
                        except (TypeError, ValueError):
                            return None
                        return dict(timeframe=source['timeframe'], open_time=opened, field=source['field'])

                    target_ref=_reference(prices.get('target_reference'))
                    stop_ref=_reference(prices.get('stop_reference'))
                    if target_ref is not None:
                        update['target_reference']=CandleReference(**target_ref)
                    if stop_ref is not None:
                        update['stop_reference']=CandleReference(**stop_ref)
                    decision=decision.model_copy(update={'price_plan': decision.price_plan.model_copy(update=update)})
            if decision.price_plan is not None and not reentry_note.get('veto'):
                if not price_planning or decision.action!='trade':
                    raise ValueError('price planning disabled or not trade')
                if option and option.get('horizon')=='swing' and not option.get('context_timeframe') and (
                        decision.price_plan.stop_reference.timeframe!='4h'
                        or decision.price_plan.target_reference.timeframe!='4h'):
                    raise ValueError('swing requires 4h stop and target references')
                apply_market_price_plan(chosen,decision.price_plan,evidence_from_market(chart_context),
                    price_step=price_rules.price_step,current_price=candidate.entry_price,now=time.time())
            if decision.action=='trade' and decision.signal_id!=candidate.signal_id:
                raise ValueError('unknown signal')
            if decision.watch_plan is not None and decision.action!='watch':
                raise ValueError('watch plan only allowed for watch action')
            if (runner3 and decision.action == 'watch' and option and option.get('context_timeframe')
                    and option['state'] == 'watch'):
                trigger = option.get('trigger_condition')
                if trigger is not None:
                    expected = dict(trigger_condition=trigger, trigger_price=option['trigger'],
                                    invalidation_condition=option['invalidation_condition'],
                                    invalidation_price=option['invalidation_price'])
                    if trigger.startswith('closed_candle'):
                        expected['timeframe'] = option['trigger_timeframe']
                    if decision.watch_plan is None:
                        from trading.evaluation.watch import WatchPlan
                        decision.watch_plan = WatchPlan(expires_at=time.time()+86400, **expected)
                    elif any(getattr(decision.watch_plan, field) != value for field, value in expected.items()):
                        raise ValueError('HTF watch plan differs from supplied trigger or invalidation')
                elif decision.watch_plan is not None:
                    raise ValueError('HTF watch has no supplied executable trigger')
            if decision.watch_plan is not None and decision.watch_plan.trigger_condition in ('price_above','price_below'):
                price=decision.watch_plan.trigger_price
                already_met=(price<=candidate.entry_price if decision.watch_plan.trigger_condition=='price_above' else price>=candidate.entry_price)
                if already_met:
                    raise ValueError('watch price condition already satisfied; choose a future condition or trade/hold')
            if decision.watch_plan is not None and not time.time()<decision.watch_plan.expires_at<=time.time()+86400:
                raise ValueError('watch expiry must be within24h')
            return decision
        if hasattr(self.llm,'response_validator'):
            self.llm.response_validator=validate
        policy=SYSTEM_POLICY+(' Runner 2 trade requires a grounded price_plan. Use market as the reference entry_type; '
                              'execution independently enforces a fresh IOC price limit. Compare scalp, intraday, and swing as separate theses; '
                              'a swing uses the supplied timeframe and candle references. '
                              'CHoCH is an opposite closed swing break; MSS adds recent sweep and displacement. '
                              'Order block candidates are opposing candles before displacement and may be retested or invalidated. '
                              'Use visible wick references for levels and confirmed structure for invalidation. '
                              'BTC/ETH relative returns and recent_trade_flow are optional context, never an automatic veto. The flow is a bounded exchange trade-side sample; null means unavailable.' if runner2 else '')
        if runner3:
            policy+='\n'+account['runner3_entry_gate']+'\n'
            policy+=('Runner 3 requires the selected READY setup ID, its exact stop, and a target that is one of the supplied opposing target levels. '
                     'Use the selected 4h→15m or 1h→5m lane. Both directions use the same rule. '
                     'Entry is a closed CHoCH on the trigger timeframe at a block or a flow, with the trigger candle closing through that CHoCH level. '
                     'Early path: a 1h weak low, a bullish candle there, and a 5m trigger. A short is the weak high. Stop under that 5m swing. The target is the next 1h block. A 4h block is only the better target, not a reason to wait for a 15m CHoCH. '
                     'A weak low is liquidity that has not been taken. Do not wait for that sweep. '
                     'The packet lists swings, highs, lows, the weak-low level, the trigger candle, and the candidate block. The read clause is how the algorithm read them. You may reject that read. '
                     'That 1h card stays ready after the 5m CHoCH. It does not become an awaiting-confirmation watch and it does not wait for a 15m close. '
                     'The stop is the CHoCH swing: a short uses the CHoCH high and a long uses the CHoCH low. '
                     'When the trade is against the higher timeframe, target only the next opposing block or flow on the trigger timeframe. Do not hold that trade toward a far higher-timeframe target. '
                     'When the trade is with the higher timeframe, a farther target is allowed. '
                     'A hold is not the default when the CHoCH, the block or flow, and a target exist. '
                     'Require the valid HTF zone, a closed LTF touch, '
                     'and a later closed LTF CHoCH/MSS; use the supplied LTF stop and one supplied opposing target level. '
                     'Optional 1m timing evidence never substitutes for the 5m confirmation or supplies price references. '
                     'A zone awaiting touch or confirmation can only be watched. Preserve its supplied trigger and '
                     'invalidation. BTC/ETH structure provides context; an altcoin can diverge if its own closed evidence supports it. '
                     'There is no separate balance split and no numeric confidence score. '
                     'Güçlü is only a liquidity sweep plus MSS plus an FVG retest, or a closed break in the higher-timeframe direction with at least 2R. Every other valid setup is Standart. '
                     + ('Choose sizing_plan.margin_share and integer leverage for either evidence grade. '
                        'A valid share is an equity fraction and a margin target. The executor prefers your leverage '
                        'when it fits verified exchange limits, available margin, and the liquidation buffer. '
                        f'sizing_plan.risk_per_trade_pct is required. Set it from the account equity and the account target, inside the {runner3_policy.CONTINUOUS_STOP_RISK_CAP:.0%} cap, including fees and estimated exit slippage. '
                        'If a setup is READY and you would otherwise trade it, return TRADE with a grounded plan. '
                        'Do not choose HOLD solely because the account is small, there are open positions, or the desired size '
                        'may not fit free margin. The executor sizes the order down to actual available margin and exchange minimums. '
                        if model_sizing else runner3_policy.GUCLU_MARGIN_INSTRUCTION +
                        'A Güçlü setup may use more of the existing per-trade loss ceiling. '
                        'Leverage is the lowest value that fits that share and the CHoCH stop. '
                        'Do not ask for a margin below 1 USDT while equity is under 100. ') +
                     'Do not narrow or move the CHoCH stop to make the position bigger. '
                     'For a long, the initial stop is just beyond the low of the CHoCH candle that confirmed the entry. '
                     'For a short, it is just beyond that CHoCH candle high. A 4h setup uses the 15m CHoCH. '
                     'A 1h setup uses the 5m CHoCH. Do not use another candle wick on that timeframe. '
                     'The target is one of the supplied opposing levels. Against the higher timeframe that is only the next block or flow on the trigger timeframe. With the higher timeframe a farther supplied level is allowed. Do not invent a level. Later management may only tighten that stop, '
                     'never widen it past this invalidation. Do not open a hedge. '
                     'Decide from the numeric setup: zone, touch, closed CHoCH/MSS, price-action and ICT labels, and the algorithm reason. '
                     'Swing stays 4h zone then 15m confirmation. Intraday stays 1h zone then 5m confirmation. Touch alone is not an entry. '
                     'Equity under 100 USDT still needs the valid HTF zone, a closed touch, '
                     'and a later closed CHoCH/MSS. It is not a BEKLE reason when a supplied target clears cost-adjusted R 1.25. Touch alone never places an order. The risk engine still caps size and loss. '
                     'Balance, free margin, and leverage (the multiplier) stay in the packet so you can size. They are not BEKLE reasons. '
                     'When 1.25 would otherwise block, you may choose a farther supplied opposing target you judge has more potential. That choice stands. '
                     'Queued confirmed plans share one balance. Simultaneous local limits are ranked by cost-adjusted R, not by preference. '
                     'Open positions include unrealized pnl, thesis, mark, stop, and remaining room. '
                     'release_symbol may name one different open position only when thesis_invalidated is true and that thesis is already invalidated, '
                     'and free margin cannot fund this confirmed plan. A higher payoff alone is not a close. '
                     'The close uses the single order writer and the new entry waits until the exchange confirms that close. '
                     'limit_price is optional. Omit it or null for a market-now entry. A number is stored locally and is not a resting exchange order. '
                     'A real setup (zone, touch, and a closed CHoCH/MSS) with at least one supplied opposing target whose cost-adjusted R clears 1.25 must be ENTER at that target. '
                     'When 1.25 would otherwise block, you may choose a farther supplied opposing target you judge has more potential. That choice stands. '
                     'If your action is trade and the target you name is one of the supplied opposing levels, the engine does not reject that choice for cost-adjusted R. '
                     'A valid hold on that selected setup is entered at the farthest supplied level that clears 1.25. Silence still opens nothing. '
                     'Balance, free margin, and leverage (the multiplier) stay in the packet so you can size. They are not BEKLE reasons. '
                     'Account balance, free margin, leverage, open positions, a higher-timeframe trend that disagrees, consolidation or a correction, and "watch already exists" are not BEKLE reasons. '
                     'Do not move the stop to manufacture R. Do not invent a level. Do not invent a trade if you are silent. '
                     'When prior_hold is present this is a hold retrigger. A stored number printed. '
                     'prior_hold.rationale is the short prior rationale. '
                     'prior_hold.old_market is the market from that hold. '
                     'market is the new data. Answer briefly whether the trade is taken now. '
                     'You may hold again. A new hold stores the new zone edge, CHoCH level, and block or flow price. '
                     'When a chart is attached, this review uses it.')
        decision_mode = None
        winning_identity = {}
        share_decision = None

        def run_review(mode, *, client=None, prompt_account=None, base_attempts=(), deadline_at=None,
                       publish=True, cancel=None):
            client = self.llm if client is None else client
            if hasattr(client, 'deadline') and deadline_at is not None:
                client.deadline = deadline_at
            owned = []

            def review_trace(update):
                combined = copy.deepcopy(update)
                combined['attempts'] = list(base_attempts) + [
                    dict(attempt, review_mode=mode) for attempt in combined.get('attempts', [])
                ]
                owned[:] = combined['attempts']
                return combined

            def emit(update):
                if cancel is not None and cancel.is_set():
                    return None
                if not publish:
                    return None
                return publish_progress(self.store, {**trace, **review_trace(update)})

            if hasattr(client, 'on_trace'):
                client.on_trace = emit
            review_policy = policy
            if mode == 'vlm':
                review_policy += (' text_model_decision is the text model choice and why, already made from the numeric setup: '
                                  'zone, touch, closed CHoCH/MSS, price-action and ICT labels, and the algorithm reason. '
                                  'You have that same packet plus the chart. Text and this review run together. '
                                  'You have two minutes to answer. A valid answer in that time is the decision. '
                                  'You may rebuild the plan onto a supplied opposing target, take the market-now setup, or name limit_price. '
                                  'Do not drop or refuse a real setup when a supplied opposing target clears cost-adjusted R 1.25. '
                                  'When 1.25 would otherwise block, you may choose a farther supplied opposing target you judge has more potential. That choice stands. '
                                  'Balance, free margin, and leverage (the multiplier) stay in the packet so you can size. They are not reasons to drop the setup. '
                                  'Account balance, free margin, leverage, open positions, a higher-timeframe trend that disagrees, consolidation or a correction, and "watch already exists" are not reasons to drop it. '
                                  'An answer after the order is already sent does not unwind it. It may only update the saved local plan for the next trigger. '
                                  'limit_price is stored locally and is not a resting exchange order. '
                                  'Price reaching it only reopens analysis; confirmation is still required. '
                                  'A resting local limit does not reserve margin. '
                                  'The text model chooses sizing_plan.margin_share for a Güçlü plan. Do not replace that share. '
                                  'A missing text share stays the 35% fallback. Do not invent a larger share. '
                                  'Chart annotations cannot replace candle references or mandatory price and risk checks. '
                                  'Equity under 100 USDT still needs zone, touch, and closed CHoCH/MSS. It is not a reason to drop a real setup whose supplied target clears cost-adjusted R 1.25. Touch alone never places an order.')
            review_account = model_account if prompt_account is None else prompt_account
            review_market = model_market
            if runner3 and prompt_account is not None:
                review_account, review_market = runner3_model_view(prompt_account, chart_context)
            review_prompt = build_prompt(candidate, review_market, memory, positions, review_account, mode, changes)
            payload = client.complete_json(system=review_policy, user=review_prompt,
                                           max_tokens=1100 if price_planning else 700,
                                           images=paths if mode == 'vlm' else None)
            raw_trace = copy.deepcopy(getattr(client, 'last_trace', {}) or {})
            review_trace(raw_trace)
            called = ('attempts' not in raw_trace or any(
                attempt.get('generation_requests', 0) or attempt.get('count_requests', 0)
                for attempt in raw_trace.get('attempts') or []))
            try:
                reviewed = validate(payload)
            except (ValueError, TypeError):
                reviewed = None
            identity = dict(provider=getattr(client, 'last_provider', 'unknown'),
                            model=getattr(client, 'last_model', 'unknown'),
                            fallback_reason=getattr(client, 'last_fallback_reason', ''),
                            credential_id=getattr(client, 'last_credential_id', ''),
                            quota_group=getattr(client, 'last_quota_group', ''))
            return dict(decision=reviewed, attempts=list(owned), identity=identity, prompt=review_prompt,
                        called=called, raw=raw_trace)

        provider_called = False
        pending = None

        def failed_review(mode):
            return dict(decision=None, called=False,
                        attempts=[dict(review_mode=mode, status='error', generation_requests=0)],
                        identity={}, prompt='', raw={})

        if model_not_asked:
            decision = direct_decision
            share_decision = None
            decision_mode = 'rule'
            winning_identity = {}
            winning_prompt = prompt
            attempts = []
            raw_trace = {}
        elif runner3 and paths:
            try:
                text_result = run_review('text', deadline_at=deadline)
            except sqlite3.Error:
                raise
            except Exception:
                text_result = failed_review('text')
            provider_called = bool(text_result['called'])
            text_decision = text_result['decision'] if time.time() - started <= self.cfg.decision_ttl_seconds else None
            share_decision = text_decision
            text_attempts = text_result['attempts']
            decision = text_decision
            decision_mode = 'text' if text_decision is not None else None
            winning_identity = text_result['identity']
            winning_prompt = text_result['prompt']
            attempts = list(text_attempts)
            raw_trace = text_result['raw']
            vlm_account = copy.deepcopy(account)
            vlm_account['text_model_decision'] = _decision_packet(text_decision)
            vlm_client = self.llm.fork() if callable(getattr(self.llm, 'fork', None)) else self.llm
            from trading.evaluation.pending_vlm import PendingVlm
            pending = PendingVlm(self.store, fee=self.cfg.paper_fee_rate,
                                 slippage=self.cfg.paper_slippage_bps / 10000)
            finished = threading.Event()

            def vlm_work():
                try:
                    review = run_review('vlm', client=vlm_client, prompt_account=vlm_account,
                                        base_attempts=list(text_attempts),
                                        deadline_at=time.time() + VLM_DECISION_TIMEOUT_SECONDS,
                                        cancel=pending.cancelled)
                    review['symbol'] = symbol
                    pending.complete(review)
                except Exception:
                    pending.complete(dict(decision=None, symbol=symbol))
                finally:
                    finished.set()
                    if vlm_client is not self.llm and callable(getattr(vlm_client, 'close', None)):
                        vlm_client.close()

            # Text already decided. VLM is asked the same question and has two minutes.
            threading.Thread(target=vlm_work, name='vlm-decision', daemon=True).start()
            # Cap is the VLM deadline. An empty or invalid answer ends the wait immediately.
            arrived = finished.wait(VLM_DECISION_TIMEOUT_SECONDS)
            vlm_result = pending.take() if arrived else None
            if vlm_result is not None:
                provider_called = provider_called or bool(vlm_result.get('called'))
                attempts = vlm_result.get('attempts') or attempts
                raw_trace = vlm_result.get('raw') or raw_trace
                decision = vlm_result['decision']
                decision_mode = 'vlm'
                winning_identity = vlm_result.get('identity') or winning_identity
                winning_prompt = vlm_result.get('prompt') or winning_prompt
            elif text_decision is not None:
                # Silence, timeout, or unusable chart output: the text decision stands.
                if not arrived:
                    pending.close_deadline()
                    if hasattr(vlm_client, 'on_trace'):
                        vlm_client.on_trace = lambda update: None
                    attempts = list(text_attempts) + [dict(review_mode='vlm', status='timeout', generation_requests=0)]
                    raw_trace = text_result['raw']
                else:
                    review = pending.box.get('value') or {}
                    pending.close_deadline()
                    if review.get('attempts'):
                        attempts = review['attempts']
                        raw_trace = review.get('raw') or raw_trace
                        provider_called = provider_called or bool(review.get('called'))
                decision = text_decision
                decision_mode = 'text'
                winning_identity = text_result['identity'] or winning_identity
                winning_prompt = text_result['prompt'] or winning_prompt
            else:
                review = pending.box.get('value') or {}
                pending.close_deadline()
                if not arrived and hasattr(vlm_client, 'on_trace'):
                    vlm_client.on_trace = lambda update: None
                if review.get('attempts'):
                    attempts = review['attempts']
                    raw_trace = review.get('raw') or raw_trace
                    provider_called = provider_called or bool(review.get('called'))
                decision = None
                decision_mode = None
        else:
            mode = 'vlm' if self.cfg.analysis_version == 'vlm' and paths else 'text'
            try:
                single = run_review(mode, deadline_at=deadline)
            except sqlite3.Error:
                raise
            except Exception:
                if not runner3:
                    raise
                single = failed_review(mode)
            provider_called = bool(single['called'])
            decision = single['decision']
            share_decision = decision
            decision_mode = mode if decision is not None else None
            winning_identity = single['identity']
            winning_prompt = single['prompt']
            attempts = single['attempts']
            raw_trace = single['raw']
        trace.update(raw_trace)
        trace['attempts'] = attempts
        if decision is not None:
            trace.update(decision_mode=decision_mode, input_fingerprint=digest(winning_prompt),
                         prompt_bytes=len(winning_prompt.encode()),
                         provider=winning_identity.get('provider') or trace.get('provider') or 'none',
                         model=winning_identity.get('model') or trace.get('model') or '')
        # The VLM wait is outside the execution clock. A winner is fresh when it is chosen.
        decision_clock = time.time() if runner3 else started
        trace['completed_at'] = time.time()
        metadata=dict(charts=paths,analysis_version=self.cfg.analysis_version,provider_called=provider_called,analysis_trace_id=trace['id'],
            decision_created_at=time.time(),decision_started_at=decision_clock,fingerprint=fingerprint,
            input_fingerprint=trace['input_fingerprint'],context_policy=POLICY_VERSION,
            why_reconsidered=changes,prompt_bytes=trace['prompt_bytes'],strategy_cohort=trace.get('strategy_cohort'),
            pending_vlm=pending)
        if model_not_asked:
            metadata['model_not_asked'] = True
        if decision is None:
            metadata['provider_busy']=getattr(self.llm,'last_status','')=='paid_in_flight'
            wait_statuses={'cooldown_skipped','quota_skipped','paid_in_flight','stopped','missing_key','budget_exhausted','budget_or_interval_hold'}
            waiting_only=(attempts is not None and not provider_called and (not attempts or all(a.get('status') in wait_statuses for a in attempts)))
            trace.update(status='provider_wait' if waiting_only else 'invalid_or_unavailable_model',action='hold',execution_status='not_opened')
            self._save_trace(trace)
            if live_account is not None and provider_called:
                from trading.runtime.edge_research import record_review
                record_review(self.store,trace,candidate,None,self.cfg)
            if not waiting_only:
                logging.getLogger('worker').info('UNPARSED %s review failed', symbol)
                if runner3:
                    _consume_structure(coordinator, candidate, prior_watch)
            if provider_called:
                # This is an error retry delay for identical inputs, never an
                # interval imposed on valid HOLD/WATCH or newly changed evidence.
                memory['last_failure']=dict(evaluation_key=evaluation_key,retry_at=time.time()+60)
                self.store.put(key,memory)
                metadata['retry_at']=memory['last_failure']['retry_at']
            return EvaluationResult(action=GraphAction.HOLD,hold_reason='invalid_or_unavailable_model',metadata=metadata)
        ready_setup_after_hold = False
        if runner3 and decision.action == 'hold' and price_rules is not None and not candidate.features.get('hold_review'):
            promoted = _ready_hold_trade(
                decision, candidate, options, chart_context, prior_watch,
                entry=candidate.entry_price, fee=fee, slippage=slippage,
                price_step=price_rules.price_step, now=time.time())
            if promoted is not None:
                decision = promoted
                ready_setup_after_hold = True
        metadata['decision_mode']=decision_mode
        trace['model_action']='hold' if ready_setup_after_hold else decision.action
        if ready_setup_after_hold:
            metadata['ready_setup_after_hold'] = True
        source_candidate=candidate
        from trading.evaluation.setup_selection import validate_selection
        candidate,selection=validate_selection(decision,candidate,options,selection_enabled)
        trace.update(setup_id=decision.setup_id,setup_family=selection['family'] if selection else candidate.features.get('setup_family'),side=candidate.side.value)
        metadata['setup_selection']=copy.deepcopy(selection)
        if runner3 and selection:
            trace['evidence_grade']=runner3_policy.evidence_grade(selection)
            if selection.get('context_timeframe'):
                trace['htf_evidence']={key:copy.deepcopy(selection[key])
                    for key in ('context_timeframe','trigger_timeframe','evidence')}
        if runner2 and decision.price_plan:
            from trading.quant.structure_setups import trade_horizon
            frame=selection['trigger_timeframe'] if selection else decision.price_plan.entry_timeframe
            if runner3 and selection and selection.get('context_timeframe') in ('4h', '1h'):
                frame=selection['context_timeframe']
            metadata['trade_horizon']=trade_horizon(frame,time.time())
            trace['trade_horizon']=metadata['trade_horizon']['style']
        shadow_plan=None
        def reject_coordinated(reason):
            trace.update(status=reason,action='hold',execution_status='not_opened',rationale=decision.rationale)
            self._save_trace(trace)
            if live_account is not None:
                from trading.runtime.edge_research import record_review
                record_review(self.store,trace,source_candidate,shadow_plan,self.cfg)
            if provider_called:
                memory['last_failure']=dict(evaluation_key=evaluation_key,retry_at=time.time()+60)
                self.store.put(key,memory)
                metadata['retry_at']=memory['last_failure']['retry_at']
            return EvaluationResult(action=GraphAction.HOLD,hold_reason=reason,metadata=metadata)
        # Coordinate model output against the exact symbol plan it reviewed.
        # A watch without a structured condition remains passive for legacy models.
        conflict=prior_watch.get('status') in ACTIVE and prior_watch.get('side')!=candidate.side.value
        if (conflict and decision.action in ('trade','watch')
                and (decision.reviewed_watch_revision!=prior_watch['revision'] or len(decision.rationale.strip())<12)):
            metadata['watch_revision']=prior_watch['revision']
            return reject_coordinated('watch_conflict')
        if time.time()-decision_clock>self.cfg.decision_ttl_seconds:
            trace.update(status='decision_expired',action='hold',execution_status='not_opened')
            self._save_trace(trace)
            return EvaluationResult(action=GraphAction.HOLD,hold_reason='decision_expired',metadata=metadata)
        planned_candidate=candidate
        entry_block=reentry_note.get('veto')
        if (runner3 and candidate.features.get('hold_review') and decision.action=='trade'
                and isinstance(selection, dict)):
            cash_equity, cash_free = runner3_policy.cash_for_veto(equity, live_account)
            prices, entry_block = runner3_policy.priced_reentry(
                selection, price=candidate.entry_price, equity=cash_equity, free_margin=cash_free,
                fee=fee, slippage=slippage, price_step=price_rules.price_step)
            if entry_block is None and prices is not None and decision.price_plan is not None:
                update=dict(entry_price=prices['entry'], stop_loss_price=prices['stop'],
                            take_profit_price=prices['target'])

                def _reference(source):
                    if not isinstance(source, dict) or any(source.get(key) is None for key in ('timeframe', 'open_time', 'field')):
                        return None
                    try:
                        opened=int(source['open_time'])
                    except (TypeError, ValueError):
                        return None
                    return dict(timeframe=source['timeframe'], open_time=opened, field=source['field'])

                target_ref=_reference(prices.get('target_reference'))
                stop_ref=_reference(prices.get('stop_reference'))
                if target_ref is not None:
                    update['target_reference']=CandleReference(**target_ref)
                if stop_ref is not None:
                    update['stop_reference']=CandleReference(**stop_ref)
                decision=decision.model_copy(update={'price_plan': decision.price_plan.model_copy(update=update)})
        price_evidence=evidence_from_market(chart_context) if decision.price_plan is not None and entry_block is None else None
        if decision.price_plan is not None and entry_block is None:
            try:
                planned_candidate=apply_market_price_plan(candidate,decision.price_plan,price_evidence,
                    price_step=price_rules.price_step,current_price=candidate.entry_price,now=time.time())
            except ValueError:
                return reject_coordinated('invalid_model_price_plan')
        metadata.update(price_plan=decision.price_plan.model_dump() if decision.price_plan else None,
            price_evidence=price_evidence,price_plan_enabled=price_planning)
        shadow_plan=planned_candidate if decision.action=='trade' else None
        prepared_proposal=None
        if decision.action=='trade' and entry_block is None:
            try:
                prepared_proposal=apply_decision_sizing(planned_candidate,decision.sizing_plan,equity,risk_per_trade_pct,target_leverage,exchange_cap)
            except PlanSizingError:
                return reject_coordinated('invalid_model_sizing')
        local_limit=bool(runner3 and decision.limit_price is not None)
        coordinated_action='hold' if local_limit or (decision.action=='watch' and decision.watch_plan is None) else decision.action
        try:
            committed=coordinator.commit(planned_candidate if decision.action=='trade' else candidate,coordinated_action,prior_watch['revision'],
                watch_plan=decision.watch_plan,rationale=decision.rationale,reviewed_revision=decision.reviewed_watch_revision)
        except ValueError:
            committed=False
        if not committed:
            return reject_coordinated('watch_revision_changed')
        committed_revision=prior_watch['revision']+1
        metadata.update(watch_revision=committed_revision,watch_plan=decision.watch_plan.model_dump() if decision.watch_plan else None)
        current_watch=coordinator.card(symbol)
        if current_watch['revision']!=committed_revision:
            return reject_coordinated('watch_revision_changed')
        trace.update(watch_revision=committed_revision,watch_plan=metadata['watch_plan'],price_plan=metadata.get('price_plan'))
        # Save dedup against the state our own successful commit created so HOLD
        # cannot invalidate its own cache through a coordinator revision bump.
        economic_basis['account']['canonical_watch']=watch_review_key(current_watch)
        if runner2:
            economic_basis['benchmarks']=chart_context['benchmarks']
        evaluation_key=digest(dict(policy=policy_version,market=fingerprint,scope=scope,economic_basis=economic_basis))
        reported_action='local_limit' if local_limit else decision.action
        if ready_setup_after_hold:
            trace['ready_setup_after_hold'] = True
        trace.update(status='completed',action=reported_action,
            model_action='hold' if ready_setup_after_hold else reported_action,rationale=decision.rationale,
            invalidation=decision.invalidation,watch_note=decision.watch_note,
            execution_status='not_opened' if local_limit or decision.action!='trade' else 'awaiting_risk')
        self._save_trace(trace)
        if live_account is not None:
            from trading.runtime.edge_research import record_review
            record_review(self.store,trace,source_candidate,planned_candidate,self.cfg)
        decision_ai=dict(winning_identity)
        if runner3:
            decision_ai['review_mode']=decision_mode
        record=dict(at=time.time(),action=reported_action,rationale=decision.rationale,decision_ai=decision_ai,
            invalidation=decision.invalidation,price_plan=metadata.get('price_plan'),charts=paths,fingerprint=fingerprint)
        card=setup_card(memory)
        card.update(first_reason=card.get('first_reason') or decision.rationale,last_reason=decision.rationale,
            last_action=reported_action,invalidation=decision.invalidation,watch_note=decision.watch_note,
            strategy=candidate.strategy.value,side=candidate.side.value,last_review_at=time.time())
        if decision.action=='trade':card['last_price_plan']=metadata.get('price_plan')
        memory.pop('last_failure',None)
        memory.update(scope=scope,setup_card=card,watch_note=decision.watch_note,last_fingerprint=fingerprint,
            last_evaluation_key=evaluation_key,last_market=chart_context,
            history=(memory['history']+[record])[-6:])
        self.store.put(key,memory)
        # Symbol mirror supports the existing close-outcome writer and operator tools.
        self.store.put('memory:'+symbol,memory)
        self.store.put('last_analysis',record)
        metadata.update(decision_ai=decision_ai,invalidation=decision.invalidation,rationale=decision.rationale,
            watch_note=decision.watch_note,sizing_plan=decision.sizing_plan.model_dump(exclude_none=True) if decision.sizing_plan else None,
            release_symbol=decision.release_symbol,
            thesis_invalidated=bool(decision.release_symbol and decision.thesis_invalidated))
        if account.get('swap_candidate'):
            metadata['swap_candidate']=account['swap_candidate']
        if runner3 and not model_sizing:
            text_plan = getattr(share_decision, 'sizing_plan', None) if share_decision is not None else None
            metadata['guclu_margin_share'] = None if text_plan is None else text_plan.margin_share
        if local_limit:
            from trading.evaluation.local_limits import save_local_limit
            plan = decision.price_plan
            side_name = selection['side'] if selection else candidate.side.value
            adjusted = None
            if plan is not None:
                adjusted = runner3_policy.cost_adjusted_r(
                    side=side_name, entry=decision.limit_price, stop=plan.stop_loss_price,
                    target=plan.take_profit_price, fee=self.cfg.paper_fee_rate,
                    slippage=self.cfg.paper_slippage_bps / 10000)
            metadata['local_limit']=save_local_limit(
                self.store, symbol=symbol, side=side_name,
                limit_price=decision.limit_price, setup_id=decision.setup_id, rationale=decision.rationale,
                decision_mode=decision_mode, trace_id=trace['id'],
                trigger_timeframe=(selection or {}).get('trigger_timeframe'),
                cost_adjusted_r=adjusted, stop=None if plan is None else plan.stop_loss_price,
                target=None if plan is None else plan.take_profit_price)
        if time.time()-decision_clock>self.cfg.decision_ttl_seconds:
            return EvaluationResult(action=GraphAction.HOLD,hold_reason='decision_expired',metadata=metadata)
        if local_limit:
            return EvaluationResult(action=GraphAction.HOLD,hold_reason='local_limit',metadata=metadata)
        if runner3 and decision.action=='hold':
            from trading.evaluation.hold_triggers import save_hold, snapshot_market, trigger_levels
            setup=selection if isinstance(selection, dict) else None
            if setup is None:
                ident=candidate.features.get('setup_id')
                setup=next((row for row in options if isinstance(row, dict) and row.get('setup_id')==ident), None)
            if setup is None:
                setup=next((row for row in options if isinstance(row, dict) and row.get('state')=='ready'), None)
            levels=trigger_levels(setup, candidate)
            if levels:
                step=price_rules.price_step if price_rules is not None else 0
                metadata['price_action_hold']=save_hold(
                    self.store, symbol=symbol, rationale=decision.rationale, levels=levels,
                    side=(setup or {}).get('side') or candidate.side.value,
                    setup_id=None if setup is None else setup.get('setup_id'),
                    old_market=snapshot_market(chart_context),
                    trigger_timeframe=None if setup is None else setup.get('trigger_timeframe'),
                    price=candidate.entry_price, tick=step,
                    model_accepted=not _early_card(setup))
        if runner3 and decision.action=='trade' and entry_block is None and prepared_proposal is not None:
            cash_equity, cash_free = runner3_policy.cash_for_veto(equity, live_account)
            entry_block = runner3_policy.entry_veto(
                side=prepared_proposal.side, entry=prepared_proposal.entry_price,
                stop=prepared_proposal.stop_loss.price, target=prepared_proposal.take_profit_price,
                equity=cash_equity, free_margin=cash_free, fee=fee, slippage=slippage,
                notional=prepared_proposal.notional, leverage=prepared_proposal.leverage)
        if entry_block:
            metadata['veto']=entry_block
            logging.getLogger('worker').info('VETO %s check=%s', symbol, entry_block)
            if candidate.features.get('hold_review'):
                from trading.evaluation.hold_triggers import save_hold, snapshot_market, trigger_levels
                setup=selection if isinstance(selection, dict) else None
                levels=trigger_levels(setup, candidate)
                if levels:
                    step=price_rules.price_step if price_rules is not None else 0
                    metadata['price_action_hold']=save_hold(
                        self.store, symbol=symbol, rationale=decision.rationale, levels=levels,
                        side=(setup or {}).get('side') or candidate.side.value,
                        setup_id=None if setup is None else setup.get('setup_id'),
                        old_market=snapshot_market(chart_context),
                        trigger_timeframe=None if setup is None else setup.get('trigger_timeframe'),
                        price=candidate.entry_price, tick=step)
            return EvaluationResult(action=GraphAction.HOLD, hold_reason=entry_block, metadata=metadata)
        if runner3 and decision.action=='trade':
            from trading.evaluation.hold_triggers import clear_hold
            clear_hold(self.store, symbol)
        if decision.action!='trade':
            return EvaluationResult(action=GraphAction.HOLD,hold_reason=decision.action,metadata=metadata)
        proposal=prepared_proposal
        proposal.metadata.update(setup_family=trace.get('setup_family'),setup_id=decision.setup_id)
        proposal.metadata.update(
            price_source='ready_card' if model_not_asked else 'model' if decision.price_plan else 'quant',
            price_plan=metadata.get('price_plan'))
        proposal.rationale=decision.rationale
        if model_not_asked:
            logging.getLogger('worker').info(
                'ENTRY %s model was not asked again reason=%s', symbol, decision.rationale)
        elif isinstance(selection, dict) and _early_card(selection):
            self.store.put('accepted_entry:' + symbol, _acceptance_snapshot(selection))
            logging.getLogger('worker').info('ENTRY %s model agreed', symbol)
        return EvaluationResult(action=GraphAction.TRADE,proposal=proposal,selected_signal=candidate,
            market=MarketAnalystNote(mode='llm'),strategy=StrategyReviewNote(mode='llm'),
            portfolio=PortfolioAnalystNote(mode='llm'),metadata=metadata)

    def _save_trace(self,trace):
        from trading.runtime.usage import archive_trace
        with self.store.lock:
            archive_trace(self.store,trace)
            _publish_latest(self.store,trace)
            history=[row for row in self.store.get('analysis_history',[]) if row.get('id')!=trace['id']]
            history.append(copy.deepcopy(trace))
            history.sort(key=lambda row:(row.get('started_at',0),row['id']))
            self.store.put('analysis_history',history[-30:])
            self.store.put('stage','model_'+trace['status'])
        if trace.get('mode') == 'LIVE' and self.on_live_review is not None:
            self.on_live_review(trace)
