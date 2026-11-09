from trading.runtime.live_capabilities import STRUCTURAL_IOC_RUNNERS
from trading.runtime import runner3_policy
import copy
from pathlib import Path
import time
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from trading.agents.schemas import EvaluationResult, GraphAction, MarketAnalystNote, StrategyReviewNote, PortfolioAnalystNote
from trading.evaluation.watch import WatchPlan, WatchCoordinator, ACTIVE, watch_event_key
from trading.evaluation.price_plan import MarketPricePlan,apply_market_price_plan,evidence_from_market
from trading.evaluation.planning import PlanSizing, PlanSizingError, apply_decision_sizing
from trading.evaluation.context import POLICY_VERSION, build_prompt, change_summary, digest, position_cards, setup_card, account_research_context, economic_review_key, canonical_watch_card, watch_review_key


class ModelDecision(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    setup_id: str | None = Field(default=None,max_length=64)
    sizing_plan: PlanSizing | None = None
    price_plan: MarketPricePlan | None = None
    watch_plan: WatchPlan | None = None
    reviewed_watch_revision: int | None = Field(default=None,ge=0,strict=True)
    action: str = Field(pattern='^(trade|watch|hold|cancel_watch)$')
    signal_id: str | None
    rationale: str = Field(max_length=1200)
    invalidation: str = Field(max_length=500)
    watch_note: str | None = Field(default='', max_length=1000)

    @field_validator('watch_note')
    @classmethod
    def normalize_watch_note(cls, value):
        return '' if value is None else value


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
    'A countertrend selection must choose risk_per_trade_pct no more than half the supplied max_risk_fraction. '
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
    'Never pretend to supersede an unseen plan. For trade choose sizing_plan with integer leverage and '
    'risk_per_trade_pct as an account fraction (.01 means 1%), within supplied limits; null retains policy sizing. '
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


class ContextEvaluator:
    """Self-contained current evidence + bounded application-owned setup memory.

    Full journals/charts remain server-side. Table encoding preserves supplied
    numerical evidence; working memory is explicitly a summary, not a full chat.
    """
    def __init__(self, llm, store, public, settings):
        self.llm, self.store, self.public, self.cfg = llm, store, public, settings
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
        bundle = build_bundle(self.public, symbol)
        chart_context = bundle.to_context(ict=True) if runner2 else bundle.to_context()
        fingerprint = bundle.fingerprint
        if runner2:
            from trading.analysis.market import benchmark_context
            chart_context['benchmarks']=benchmark_context(self.public,chart_context)
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
            from trading.quant.structure_setups import build_structure_setups
            frames=('5m','15m','4h') if runner2 else (None,)
            options=[setup for frame in frames for setup in build_structure_setups(chart_context,
                current_price=candidate.entry_price,price_step=price_rules.price_step,trigger_timeframe=frame,strict_ict=runner3)]
        if runner3:
            balance=live_account['current_equity']
            for option in options:
                grade=runner3_policy.evidence_grade(option)
                allocation=runner3_policy.margin_policy(balance,equity,live_account['available_usdt'],
                    grade,option['horizon'])
                ceiling=risk_per_trade_pct*(.5 if option['family']=='countertrend_sweep_reclaim' else 1)
                target_risk=runner3_policy.small_account_risk_target(balance,ceiling,grade)
                option['allocation']=dict(allocation,
                    suggested_risk_fraction=target_risk if target_risk is not None else ceiling*(.8 if grade=='strong' else .5))
        account['setup_selection']=dict(enabled=selection_enabled,options=options,
            countertrend_max_risk_fraction=risk_per_trade_pct*.5,
            note='Select setup_id and provide a fresh grounded price_plan for TRADE. No hypothesis means no forced trade.')
        economic_basis=economic_review_key(account,positions)
        economic_basis['account']['canonical_watch']=watch_review_key(prior_watch)
        if runner2:
            economic_basis['benchmarks']=chart_context['benchmarks']
        evaluation_key=digest(dict(policy=POLICY_VERSION,market=fingerprint,scope=scope,economic_basis=economic_basis))
        if memory.get('last_evaluation_key')==evaluation_key:
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
        if runner2:
            from trading.analysis.market import trade_flow_context
            chart_context['recent_trade_flow']=trade_flow_context(self.public,symbol)
        prompt=build_prompt(candidate,chart_context,memory,positions,account,self.cfg.analysis_version,changes)
        charts=render_bundle(bundle,Path(self.cfg.analysis_chart_dir)/symbol/fingerprint[:16])
        paths=[str(c.path) for c in charts]
        started=time.time()
        trace=dict(id=uuid4().hex,mode='LIVE' if live_account is not None else 'PAPER',symbol=symbol,strategy=candidate.strategy.value,confidence=candidate.confidence,
            runner_id=(live_account or {}).get('runner_id','baseline-v1'),
            analysis_version=self.cfg.analysis_version,started_at=started,status='pending',attempts=[],provider='none',model='',
            why_reconsidered=changes,market_fingerprint=fingerprint,input_fingerprint=digest(prompt),
            context_policy=POLICY_VERSION,prompt_bytes=len(prompt.encode()))
        if runner2:
            trace['strategy_cohort']=runner3_policy.COHORT if runner3 else 'runner2-multihorizon-ict-v1'
        publish_progress(self.store,trace)
        self.store.put('stage','model_evaluation')
        if hasattr(self.llm,'deadline'):
            self.llm.deadline=started+self.cfg.decision_ttl_seconds
        if hasattr(self.llm,'on_trace'):
            self.llm.on_trace=lambda update:publish_progress(self.store,{**trace,**copy.deepcopy(update)})
        def validate(payload):
            decision=ModelDecision.model_validate(payload)
            from trading.evaluation.setup_selection import validate_selection
            chosen,option=validate_selection(decision,candidate,options,selection_enabled)
            if (option and option['family']=='countertrend_sweep_reclaim' and decision.action=='trade'
                    and (decision.sizing_plan is None or decision.sizing_plan.risk_per_trade_pct>risk_per_trade_pct*.5)):
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
            if decision.price_plan is not None:
                if not price_planning or decision.action!='trade':
                    raise ValueError('price planning disabled or not trade')
                if option and option.get('horizon')=='swing' and (
                        decision.price_plan.stop_reference.timeframe!='4h'
                        or decision.price_plan.target_reference.timeframe!='4h'):
                    raise ValueError('swing requires 4h stop and target references')
                apply_market_price_plan(chosen,decision.price_plan,evidence_from_market(chart_context),
                    price_step=price_rules.price_step,current_price=candidate.entry_price,now=time.time())
            if decision.action=='trade' and decision.signal_id!=candidate.signal_id:
                raise ValueError('unknown signal')
            if decision.watch_plan is not None and decision.action!='watch':
                raise ValueError('watch plan only allowed for watch action')
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
                              'a swing uses 4h stop and target candle references even if execution uses a finer entry. '
                              'CHoCH is an opposite closed swing break; MSS adds recent sweep and displacement. '
                              'Order block candidates are opposing candles before displacement and may be retested or invalidated. '
                              'Use visible wick references for levels and confirmed structure for invalidation. '
                              'BTC/ETH relative returns and recent_trade_flow are optional context, never an automatic veto. The flow is a bounded exchange trade-side sample; null means unavailable.' if runner2 else '')
        if runner3:
            policy+=(' Runner 3 requires the selected READY setup ID and its exact stop_reference. '
                     'Use allocation as a margin target ceiling, separate from stop risk. '
                     'Use the suggested risk fraction as a starting point; adjust down for portfolio risk, liquidity, and weak payoff. Choose leverage from current account and price evidence. '
                     'Do not narrow the structural stop to fit the desired size.')
        payload=self.llm.complete_json(system=policy,user=prompt,max_tokens=1100 if price_planning else 700,
            images=paths if self.cfg.analysis_version=='vlm' else None)
        provider_trace=copy.deepcopy(getattr(self.llm,'last_trace',{}) or {})
        trace.update(provider_trace)
        attempts=provider_trace.get('attempts')
        provider_called=(any(a.get('generation_requests',0) or a.get('count_requests',0) for a in attempts)
                         if attempts is not None else True)
        trace['completed_at']=time.time()
        metadata=dict(charts=paths,analysis_version=self.cfg.analysis_version,provider_called=provider_called,analysis_trace_id=trace['id'],
            decision_created_at=time.time(),decision_started_at=started,fingerprint=fingerprint,
            input_fingerprint=trace['input_fingerprint'],context_policy=POLICY_VERSION,
            why_reconsidered=changes,prompt_bytes=trace['prompt_bytes'],strategy_cohort=trace.get('strategy_cohort'))
        try:
            decision=validate(payload)
        except Exception:
            metadata['provider_busy']=getattr(self.llm,'last_status','')=='paid_in_flight'
            wait_statuses={'cooldown_skipped','quota_skipped','paid_in_flight','stopped','missing_key','budget_exhausted','budget_or_interval_hold'}
            waiting_only=(attempts is not None and not provider_called and (not attempts or all(a.get('status') in wait_statuses for a in attempts)))
            trace.update(status='provider_wait' if waiting_only else 'invalid_or_unavailable_model',action='hold',execution_status='not_opened')
            self._save_trace(trace)
            if live_account is not None and provider_called:
                from trading.runtime.edge_research import record_review
                record_review(self.store,trace,candidate,None,self.cfg)
            if provider_called:
                # This is an error retry delay for identical inputs, never an
                # interval imposed on valid HOLD/WATCH or newly changed evidence.
                memory['last_failure']=dict(evaluation_key=evaluation_key,retry_at=time.time()+60)
                self.store.put(key,memory)
                metadata['retry_at']=memory['last_failure']['retry_at']
            return EvaluationResult(action=GraphAction.HOLD,hold_reason='invalid_or_unavailable_model',metadata=metadata)
        trace['model_action']=decision.action
        source_candidate=candidate
        from trading.evaluation.setup_selection import validate_selection
        candidate,selection=validate_selection(decision,candidate,options,selection_enabled)
        trace.update(setup_id=decision.setup_id,setup_family=selection['family'] if selection else candidate.features.get('setup_family'),side=candidate.side.value)
        metadata['setup_selection']=copy.deepcopy(selection)
        if runner3 and selection:
            trace['evidence_grade']=runner3_policy.evidence_grade(selection)
        if runner2 and decision.price_plan:
            from trading.quant.structure_setups import trade_horizon
            frame=selection['trigger_timeframe'] if selection else decision.price_plan.entry_timeframe
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
        if time.time()-started>self.cfg.decision_ttl_seconds:
            trace.update(status='decision_expired',action='hold',execution_status='not_opened')
            self._save_trace(trace)
            return EvaluationResult(action=GraphAction.HOLD,hold_reason='decision_expired',metadata=metadata)
        planned_candidate=candidate
        price_evidence=evidence_from_market(chart_context) if decision.price_plan is not None else None
        if decision.price_plan is not None:
            try:
                planned_candidate=apply_market_price_plan(candidate,decision.price_plan,price_evidence,
                    price_step=price_rules.price_step,current_price=candidate.entry_price,now=time.time())
            except ValueError:
                return reject_coordinated('invalid_model_price_plan')
        metadata.update(price_plan=decision.price_plan.model_dump() if decision.price_plan else None,
            price_evidence=price_evidence,price_plan_enabled=price_planning)
        shadow_plan=planned_candidate if decision.action=='trade' else None
        prepared_proposal=None
        if decision.action=='trade':
            try:
                prepared_proposal=apply_decision_sizing(planned_candidate,decision.sizing_plan,equity,risk_per_trade_pct,target_leverage,exchange_cap)
            except PlanSizingError:
                return reject_coordinated('invalid_model_sizing')
        coordinated_action='hold' if decision.action=='watch' and decision.watch_plan is None else decision.action
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
        evaluation_key=digest(dict(policy=POLICY_VERSION,market=fingerprint,scope=scope,economic_basis=economic_basis))
        trace.update(status='completed',action=decision.action,rationale=decision.rationale,
            invalidation=decision.invalidation,watch_note=decision.watch_note,
            execution_status='awaiting_risk' if decision.action=='trade' else 'not_opened')
        self._save_trace(trace)
        if live_account is not None:
            from trading.runtime.edge_research import record_review
            record_review(self.store,trace,source_candidate,planned_candidate,self.cfg)
        decision_ai=dict(provider=getattr(self.llm,'last_provider','unknown'),model=getattr(self.llm,'last_model','unknown'),
            fallback_reason=getattr(self.llm,'last_fallback_reason',''),
            credential_id=getattr(self.llm,'last_credential_id',''),quota_group=getattr(self.llm,'last_quota_group',''))
        record=dict(at=time.time(),action=decision.action,rationale=decision.rationale,decision_ai=decision_ai,
            invalidation=decision.invalidation,price_plan=metadata.get('price_plan'),charts=paths,fingerprint=fingerprint)
        card=setup_card(memory)
        card.update(first_reason=card.get('first_reason') or decision.rationale,last_reason=decision.rationale,
            last_action=decision.action,invalidation=decision.invalidation,watch_note=decision.watch_note,
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
            watch_note=decision.watch_note,sizing_plan=decision.sizing_plan.model_dump() if decision.sizing_plan else None)
        if time.time()-started>self.cfg.decision_ttl_seconds:
            return EvaluationResult(action=GraphAction.HOLD,hold_reason='decision_expired',metadata=metadata)
        if decision.action!='trade':
            return EvaluationResult(action=GraphAction.HOLD,hold_reason=decision.action,metadata=metadata)
        proposal=prepared_proposal
        proposal.metadata.update(setup_family=trace.get('setup_family'),setup_id=decision.setup_id)
        proposal.metadata.update(price_source='model' if decision.price_plan else 'quant',price_plan=metadata.get('price_plan'))
        proposal.rationale=decision.rationale
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
