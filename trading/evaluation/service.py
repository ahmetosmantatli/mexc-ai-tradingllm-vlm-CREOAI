import hashlib
import json
from pathlib import Path
import time
from uuid import uuid4
import copy

from pydantic import BaseModel, ConfigDict, Field
from trading.agents.schemas import EvaluationResult, GraphAction, MarketAnalystNote, StrategyReviewNote, PortfolioAnalystNote
from trading.agents.graph import size_proposal_from_signal


class ModelDecision(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    action: str = Field(pattern='^(trade|watch|hold)$')
    signal_id: str | None
    rationale: str = Field(max_length=1200)
    invalidation: str = Field(max_length=500)
    watch_note: str = Field(max_length=1000)


class ContextEvaluator:
    """Identical policy for both variants; VLM adds only chart image blocks.

    Persistent bounded journal is supplied on every call; API calls themselves
    are stateless. No hidden claim that context lasts forever.
    """
    def __init__(self, llm, store, public, settings):
        self.llm, self.store, self.public, self.cfg = llm, store, public, settings
        if hasattr(llm, "response_validator"):
            llm.response_validator = ModelDecision.model_validate

    def evaluate_candidates(self, candidates, *, equity, risk_per_trade_pct, target_leverage, **context):
        from trading.analysis import build_bundle, render_bundle
        if not candidates:
            return EvaluationResult(action=GraphAction.HOLD, hold_reason='no_candidates')
        candidate = max(candidates, key=lambda c:c.confidence)
        symbol = candidate.symbol
        self.store.put('stage','fetching_multitimeframe_context')
        bundle = build_bundle(self.public, symbol)
        chart_context = bundle.to_context()
        fingerprint = bundle.fingerprint
        key = 'memory:'+symbol
        memory = self.store.get(key, {'history':[], 'watch_note':'', 'last_fingerprint':None})
        if memory.get('last_fingerprint') == fingerprint:
            self.store.put('stage','unchanged_closed_candles')
            return EvaluationResult(action=GraphAction.HOLD, hold_reason='unchanged_closed_candles')
        if self.llm.budget is not None and not self.llm.budget.can_spend():
            self.llm.last_status = 'budget_exhausted'
            return EvaluationResult(action=GraphAction.HOLD, hold_reason='shared_budget_exhausted')
        charts = render_bundle(bundle, Path(self.cfg.analysis_chart_dir)/symbol/fingerprint[:16])
        paths = [str(c.path) for c in charts]
        started = time.time()
        prompt = json.dumps(dict(symbol=symbol, version=self.cfg.analysis_version,
            candidate=candidate.model_dump(mode='json'), market=chart_context,
            account=dict(equity=equity, risk_per_trade_pct=risk_per_trade_pct, leverage=target_leverage, **context),
            memory=memory, positions=self.store.get('paper', {}).get('positions', {})), default=str,separators=(',',':'))
        trace=dict(id=uuid4().hex,symbol=symbol,strategy=candidate.strategy.value,confidence=candidate.confidence,
            started_at=started,status='pending',attempts=[],provider='none',model='')
        self.store.put('analysis_trace',trace)
        self.store.put('stage','model_evaluation')
        if hasattr(self.llm,'deadline'):
            self.llm.deadline=started+self.cfg.decision_ttl_seconds
        if hasattr(self.llm,'on_trace'):
            self.llm.on_trace=lambda update:self.store.put('analysis_trace',{**trace,**copy.deepcopy(update)})
        payload = self.llm.complete_json(system=(
            'Evaluate this USDT perpetual candidate. Both variants use the same strict policy. '
            'Use confirmed swings, BOS, FVG and sweep evidence as hypotheses, never a promised edge. '
            'HOLD is normal. Never force a weekly return, increase risk after losses or place orders. '
            'The candidate is the only permitted trade; do not invent IDs or prices. '
            'Return exactly JSON: action (trade/watch/hold), signal_id (candidate ID or null), '
            'rationale (string), invalidation (string), watch_note (brief bounded working memory). '
            'Only trade if evidence across Hour4, Min15 and Min5 is coherent. '
            'Chart annotations are deterministic observations, not proof of profit.'),
            user=prompt, max_tokens=700, images=paths if self.cfg.analysis_version=='vlm' else None)
        trace.update(copy.deepcopy(getattr(self.llm,'last_trace',{}) or {}))
        trace['completed_at']=time.time()
        try:
            decision = ModelDecision.model_validate(payload)
            if decision.action=='trade' and decision.signal_id != candidate.signal_id:
                raise ValueError('unknown signal')
        except Exception:
            trace.update(status='invalid_or_unavailable_model',action='hold',execution_status='not_opened')
            self._save_trace(trace)
            return EvaluationResult(action=GraphAction.HOLD, hold_reason='invalid_or_unavailable_model')
        trace.update(status='completed',action=decision.action,execution_status='awaiting_risk' if decision.action=='trade' else 'not_opened')
        self._save_trace(trace)
        decision_ai=dict(provider=getattr(self.llm,'last_provider','unknown'),
            model=getattr(self.llm,'last_model','unknown'),
            fallback_reason=getattr(self.llm,'last_fallback_reason',''))
        record=dict(at=time.time(), action=decision.action, rationale=decision.rationale, decision_ai=decision_ai,
                    invalidation=decision.invalidation, charts=paths, fingerprint=fingerprint)
        memory.update(watch_note=decision.watch_note, last_fingerprint=fingerprint,
                      history=(memory['history']+[record])[-6:])
        self.store.put(key, memory)
        self.store.put('last_analysis', record)
        metadata=dict(charts=paths, analysis_version=self.cfg.analysis_version, decision_ai=decision_ai,
                      decision_created_at=time.time(), decision_started_at=started,
                      fingerprint=fingerprint, invalidation=decision.invalidation)
        if time.time()-started > self.cfg.decision_ttl_seconds:
            return EvaluationResult(action=GraphAction.HOLD, hold_reason='decision_expired',metadata=metadata)
        if decision.action!='trade':
            return EvaluationResult(action=GraphAction.HOLD,hold_reason=decision.action,metadata=metadata)
        proposal=size_proposal_from_signal(candidate,equity=equity,risk_per_trade_pct=risk_per_trade_pct,target_leverage=target_leverage)
        proposal.rationale=decision.rationale
        return EvaluationResult(action=GraphAction.TRADE,proposal=proposal,selected_signal=candidate,
            market=MarketAnalystNote(mode='llm'), strategy=StrategyReviewNote(mode='llm'),
            portfolio=PortfolioAnalystNote(mode='llm'),metadata=metadata)

    def _save_trace(self,trace):
        from trading.runtime.usage import archive_trace
        archive_trace(self.store,trace)
        self.store.put('analysis_trace',trace)
        self.store.put('analysis_history',(self.store.get('analysis_history',[])+[copy.deepcopy(trace)])[-30:])
        self.store.put('stage','model_'+trace['status'])
