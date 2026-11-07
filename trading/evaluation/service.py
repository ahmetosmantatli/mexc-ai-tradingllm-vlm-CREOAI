import copy
from pathlib import Path
import time
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from trading.agents.schemas import EvaluationResult, GraphAction, MarketAnalystNote, StrategyReviewNote, PortfolioAnalystNote
from trading.agents.graph import size_proposal_from_signal
from trading.evaluation.context import POLICY_VERSION, build_prompt, change_summary, digest, position_cards, setup_card


class ModelDecision(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    action: str = Field(pattern='^(trade|watch|hold)$')
    signal_id: str | None
    rationale: str = Field(max_length=1200)
    invalidation: str = Field(max_length=500)
    watch_note: str = Field(max_length=1000)


SYSTEM_POLICY = (
    'Evaluate the supplied MEXC USDT perpetual candidate using the same policy for text and VLM. '
    'Each request is self-contained: current closed 4h/15m/5m evidence, account state and a durable setup card. '
    'Tables use explicit column names. What_changed explains why this review was scheduled; do not assume hidden chat memory. '
    'Treat confirmed swing/BOS/sweep/FVG evidence as hypotheses, never guaranteed edge. HOLD is normal. '
    'Never force a weekly return, raise risk after losses, invent prices/IDs, or place orders. '
    'Only the supplied candidate may be traded and only when cross-timeframe evidence is coherent. '
    'Return exactly JSON: action (trade/watch/hold), signal_id (supplied candidate ID or null), '
    'rationale, invalidation, watch_note. Write concise Turkish rationale explaining the concrete missing confirmation '
    'or rejection, and a watch_note describing what would justify reconsideration. Do not just say insufficient evidence. '
    'Keep rationale within1200 characters, invalidation within500 and watch_note within1000. '
    'VLM annotations are deterministic observations, not proof of profit.'
)


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
        scope = f'{candidate.strategy.value}:{candidate.side.value}'
        self.store.put('stage','fetching_multitimeframe_context')
        bundle = build_bundle(self.public, symbol)
        chart_context = bundle.to_context()
        fingerprint = bundle.fingerprint
        key = f'memory:{symbol}:{scope}'
        legacy = self.store.get('memory:'+symbol, {})
        memory = self.store.get(key)
        if memory is None:
            # Old unscoped memory is migrated only when the scope is compatible.
            memory=copy.deepcopy(legacy) if legacy.get('scope') in (None,scope) else {}
        memory.setdefault('history',[])
        memory.setdefault('watch_note','')
        memory.setdefault('last_fingerprint',None)
        if legacy.get('last_outcome') is not None:
            memory['last_outcome']=copy.deepcopy(legacy['last_outcome'])
        positions = self.store.get('paper', {}).get('positions', {})
        account=dict(equity=equity,risk_per_trade_pct=risk_per_trade_pct,leverage=target_leverage,**context)
        evaluation_key=digest(dict(market=fingerprint,scope=scope,account=account,positions=position_cards(positions)))
        if memory.get('last_evaluation_key')==evaluation_key:
            self.store.put('stage','unchanged_closed_candles')
            return EvaluationResult(action=GraphAction.HOLD,hold_reason='unchanged_closed_candles',
                metadata={'provider_called':False,'fingerprint':fingerprint})
        if self.llm.budget is not None and not self.llm.budget.can_spend():
            self.llm.last_status='budget_exhausted'
            return EvaluationResult(action=GraphAction.HOLD,hold_reason='shared_budget_exhausted',metadata={'provider_called':False})
        changes=change_summary(memory.get('last_market'),chart_context,memory.get('last_fingerprint'),fingerprint)
        prompt=build_prompt(candidate,chart_context,memory,positions,account,self.cfg.analysis_version,changes)
        charts=render_bundle(bundle,Path(self.cfg.analysis_chart_dir)/symbol/fingerprint[:16])
        paths=[str(c.path) for c in charts]
        started=time.time()
        trace=dict(id=uuid4().hex,symbol=symbol,strategy=candidate.strategy.value,confidence=candidate.confidence,
            started_at=started,status='pending',attempts=[],provider='none',model='',
            why_reconsidered=changes,market_fingerprint=fingerprint,input_fingerprint=digest(prompt),
            context_policy=POLICY_VERSION,prompt_bytes=len(prompt.encode()))
        self.store.put('analysis_trace',trace)
        self.store.put('stage','model_evaluation')
        if hasattr(self.llm,'deadline'):
            self.llm.deadline=started+self.cfg.decision_ttl_seconds
        if hasattr(self.llm,'on_trace'):
            self.llm.on_trace=lambda update:self.store.put('analysis_trace',{**trace,**copy.deepcopy(update)})
        def validate(payload):
            decision=ModelDecision.model_validate(payload)
            if decision.action=='trade' and decision.signal_id!=candidate.signal_id:
                raise ValueError('unknown signal')
            return decision
        if hasattr(self.llm,'response_validator'):
            self.llm.response_validator=validate
        payload=self.llm.complete_json(system=SYSTEM_POLICY,user=prompt,max_tokens=700,
            images=paths if self.cfg.analysis_version=='vlm' else None)
        provider_trace=copy.deepcopy(getattr(self.llm,'last_trace',{}) or {})
        trace.update(provider_trace)
        attempts=provider_trace.get('attempts')
        provider_called=(any(a.get('generation_requests',0) or a.get('count_requests',0) for a in attempts)
                         if attempts is not None else True)
        trace['completed_at']=time.time()
        metadata=dict(charts=paths,analysis_version=self.cfg.analysis_version,provider_called=provider_called,
            decision_created_at=time.time(),decision_started_at=started,fingerprint=fingerprint,
            input_fingerprint=trace['input_fingerprint'],context_policy=POLICY_VERSION,
            why_reconsidered=changes,prompt_bytes=trace['prompt_bytes'])
        try:
            decision=validate(payload)
        except Exception:
            trace.update(status='invalid_or_unavailable_model',action='hold',execution_status='not_opened')
            self._save_trace(trace)
            return EvaluationResult(action=GraphAction.HOLD,hold_reason='invalid_or_unavailable_model',metadata=metadata)
        trace.update(status='completed',action=decision.action,rationale=decision.rationale,
            invalidation=decision.invalidation,watch_note=decision.watch_note,
            execution_status='awaiting_risk' if decision.action=='trade' else 'not_opened')
        self._save_trace(trace)
        decision_ai=dict(provider=getattr(self.llm,'last_provider','unknown'),model=getattr(self.llm,'last_model','unknown'),
            fallback_reason=getattr(self.llm,'last_fallback_reason',''))
        record=dict(at=time.time(),action=decision.action,rationale=decision.rationale,decision_ai=decision_ai,
            invalidation=decision.invalidation,charts=paths,fingerprint=fingerprint)
        card=setup_card(memory)
        card.update(first_reason=card.get('first_reason') or decision.rationale,last_reason=decision.rationale,
            last_action=decision.action,invalidation=decision.invalidation,watch_note=decision.watch_note,
            strategy=candidate.strategy.value,side=candidate.side.value,last_review_at=time.time())
        memory.update(scope=scope,setup_card=card,watch_note=decision.watch_note,last_fingerprint=fingerprint,
            last_evaluation_key=evaluation_key,last_market=chart_context,
            history=(memory['history']+[record])[-6:])
        self.store.put(key,memory)
        # Symbol mirror supports the existing close-outcome writer and operator tools.
        self.store.put('memory:'+symbol,memory)
        self.store.put('last_analysis',record)
        metadata.update(decision_ai=decision_ai,invalidation=decision.invalidation,rationale=decision.rationale,
            watch_note=decision.watch_note)
        if time.time()-started>self.cfg.decision_ttl_seconds:
            return EvaluationResult(action=GraphAction.HOLD,hold_reason='decision_expired',metadata=metadata)
        if decision.action!='trade':
            return EvaluationResult(action=GraphAction.HOLD,hold_reason=decision.action,metadata=metadata)
        proposal=size_proposal_from_signal(candidate,equity=equity,risk_per_trade_pct=risk_per_trade_pct,target_leverage=target_leverage)
        proposal.rationale=decision.rationale
        return EvaluationResult(action=GraphAction.TRADE,proposal=proposal,selected_signal=candidate,
            market=MarketAnalystNote(mode='llm'),strategy=StrategyReviewNote(mode='llm'),
            portfolio=PortfolioAnalystNote(mode='llm'),metadata=metadata)

    def _save_trace(self,trace):
        from trading.runtime.usage import archive_trace
        archive_trace(self.store,trace)
        self.store.put('analysis_trace',trace)
        self.store.put('analysis_history',(self.store.get('analysis_history',[])+[copy.deepcopy(trace)])[-30:])
        self.store.put('stage','model_'+trace['status'])
