"""Event-driven AI reviews of owned positions, independent of entry scanning."""
import copy
import json
import math
from pathlib import Path
import time
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from trading.evaluation.context import compact_market, digest
from trading.evaluation.price_plan import CandleReference, evidence_from_market, _reference
from trading.runtime.usage import archive_trace


class ManagementDecision(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    action: str = Field(pattern='^(hold|move_stop|reduce|close)$')
    position_id: str
    reason: str = Field(min_length=12,max_length=800)
    review_timeframe: str = Field(pattern='^(5m|15m|4h)$')
    stop_price: float | None = Field(default=None,gt=0)
    stop_reference: CandleReference | None = None
    close_fraction: float | None = Field(default=None,gt=0,le=1)

    @model_validator(mode='after')
    def consistent_action(self):
        if self.action=='move_stop':
            if self.stop_price is None or self.stop_reference is None or self.close_fraction is not None:
                raise ValueError('stop action requires only stop price and candle reference')
        elif self.stop_price is not None or self.stop_reference is not None:
            raise ValueError('stop fields allowed only for move_stop')
        if self.action in ('reduce','close'):
            if self.close_fraction is None or (self.action=='close' and self.close_fraction!=1) or (self.action=='reduce' and self.close_fraction>=1):
                raise ValueError('exit requires partial fraction or exactly one for close')
        elif self.close_fraction is not None:raise ValueError('no exit fraction for hold/stop')
        return self


POLICY=(
    'Manage only the supplied existing MEXC USDT perpetual position. Never open/add/reverse or change leverage. '
    'Reply JSON with action hold/move_stop/reduce/close, position_id (exact string), reason (concise Turkish), '
    'review_timeframe (5m/15m/4h), stop_price, stop_reference, close_fraction. Nonapplicable optional fields must be null. '
    'Each request contains current closed 4h/15m/5m context, original entry thesis, immutable initial risk, '
    'current native protection, actual remaining size, sampled MFE/MAE, previous decisions and current account. '
    'Do not assume hidden chat memory. Preserve a coherent thesis across provider changes. '
    'Prefer HOLD while the thesis remains valid; ordinary noise alone is not a reason to tighten. '
    'Choose review timeframe appropriate to the entry structure. Consider elapsed bars, new confirmed structure, '
    'volatility, distance to liquidity/target and R, rather than fixed profit percentages. '
    'A move_stop must tighten the current stop, never widen risk; supply stop_reference {timeframe,open_time,field} '
    'from observed recent_ohlcv UTC-open seconds, low for LONG/high for SHORT. Stop beyond that wick by at most '
    'one recent average true range and on the safe side of current executable AND fair price. '
    'reduce uses close_fraction of CURRENT remaining contracts (0<f<1), close uses 1. '
    'No direct TP modifications: preserve native target; reduce/close when the thesis or target is no longer appropriate. '
    'Money objectives never justify raising risk or forcing trades. Charts if supplied are the same candle evidence. '
    'No guarantee of execution. Server revalidates ownership, revision, age, tick/quantity and protection before writes.'
)


class LivePositionManager:
    def __init__(self,runner):self.runner=runner

    def key(self,intent):return 'live:management-card:'+intent['intent_id']

    def observe(self,intent):
        """Sample quote excursions; never present sampled values as tick-exact MAE/MFE."""
        r=self.runner;row=r.executor.state(intent['intent_id'])
        if row.get('status')!='protected':return
        quote=r.quote(row['proposal']['symbol'])
        entry=row['entry_fill_price'];initial_stop=row['proposal']['stop_loss']['price']
        initial_risk=abs(entry-initial_stop)
        if initial_risk<=0:return
        current=(quote.bid1-entry if row['proposal']['side']=='long' else entry-quote.ask1)/initial_risk
        with r.store.lock:
            card=r.store.get(self.key(row),{})
            card.update(current_r=current,mfe_r=max(0,current,card.get('mfe_r',0)),
                mae_r=min(0,current,card.get('mae_r',0)),sampled_at=time.time(),excursion_source='sampled executable quotes, not tick-exact')
            r.store.put(self.key(row),card)

    def _permitted(self,revision):
        r=self.runner
        return (r.cfg.live_position_management_enabled and r._enabled() and r._control().get('updated_at')==revision
            and r.executor is not None and r.executor.management_ready() and not r.store.get('live:halt'))

    def tick(self):
        r=self.runner
        if not r.cfg.live_position_management_enabled or not r._management_lock.acquire(blocking=False):return
        try:
            control=r._control();revision=control.get('updated_at')
            if not self._permitted(revision):return
            for row in r._intents():
                if not self._permitted(revision):break
                if row.get('status')!='protected' or row.get('management_pending'):continue
                try:self.review(row,control)
                except Exception as exc:
                    r.store.put('management_status',dict(at=time.time(),symbol=row['proposal']['symbol'],
                        status='review_failed',error_type=type(exc).__name__,reason='Yönetim değerlendirmesi tamamlanamadı; mevcut borsa koruması korunuyor.'))
        finally:r._management_lock.release()

    def review(self,intent,control):
        from trading.analysis import build_bundle,render_bundle
        r=self.runner;now=time.time();key=self.key(intent)
        self.observe(intent)
        card=r.store.get(key,{})
        if now-card.get('last_attempt_at',0)<r.cfg.live_management_min_interval_seconds:return
        proposal=intent['proposal'];symbol=proposal['symbol']
        timeframe=card.get('review_timeframe') or (proposal.get('metadata',{}).get('price_plan') or {}).get('entry_timeframe','15m')
        milestone=min(100,max(0,int(math.floor(card.get('mfe_r',0)))))
        previous=card.get('last_event') or {}
        # No reason to download three histories again before either trigger can change.
        if (previous.get('timeframe')==timeframe and previous.get('favorable_r_milestone')==milestone
                and now<previous.get('closed_at',0)+{'5m':300,'15m':900,'4h':14400}[timeframe]):return
        bundle=build_bundle(r.public,symbol);market=bundle.to_context()
        view=next((v for v in market['timeframes'] if v['timeframe']==timeframe),None)
        if view is None:raise ValueError('management entry timeframe evidence missing')
        event=dict(timeframe=timeframe,closed_at=view['closed_at'],favorable_r_milestone=milestone)
        if card.get('last_event')==event:return
        # Durable attempt reservation prevents restart/network failures causing call storms.
        with r.store.lock:
            current=r.store.get(key,{})
            current.update(last_attempt_at=now,last_event=event)
            r.store.put(key,current)
            card=current
        revision=intent.get('management_revision',0)
        snapshot=r._refresh([symbol])
        position=next((p for p in snapshot['positions'] if str(p['position_id'])==intent['position_id']),None)
        if position is None:return
        stop,target=r.executor.protection_levels(intent)
        position_card=dict(position,initial_entry=proposal.get('entry_price'),filled_entry=intent['entry_fill_price'],
            initial_stop=proposal['stop_loss']['price'],current_stop=stop,current_target=target,
            remaining_contracts=r.executor.remaining_contracts(intent),original_thesis=proposal.get('rationale'),
            original_price_plan=proposal.get('metadata',{}).get('price_plan'),opened_at=intent.get('staged_at'),
            elapsed_entry_bars=max(0,int((now-intent['staged_at'])/{'5m':300,'15m':900,'4h':14400}[timeframe])),
            initial_risk_usdt=proposal.get('max_loss_if_stopped'),remaining_planned_loss_usdt=intent.get('planned_loss'),excursions={k:card.get(k) for k in ('current_r','mfe_r','mae_r','sampled_at','excursion_source')},
            previous_decisions=card.get('decisions',[])[-5:],confirmed_actions=intent.get('management_events',[])[-5:],
            management_revision=revision)
        quote=r.quote(symbol);rules=r.public.get_contract_rules(symbol)
        prompt=json.dumps(dict(task='manage_existing_position',event=event,position=position_card,
            account=r._live_context(snapshot,control),market=compact_market(market),
            current_quote=dict(bid=quote.bid1,ask=quote.ask1,fair=quote.raw['fairPrice']),price_step=rules.price_step),
            ensure_ascii=False,separators=(',',':'),allow_nan=False)
        llm=r.management_llm()
        trace=dict(id=uuid4().hex,started_at=now,symbol=symbol,strategy='position_management',mode='LIVE',
            analysis_version=control.get('version',r.cfg.analysis_version),status='pending',attempts=[],prompt_bytes=len(prompt.encode()),input_fingerprint=digest(prompt))
        def archive(update=None):
            if update:trace.update(copy.deepcopy(update))
            with r.store.lock:
                r.store.put('management_trace',trace)
                archive_trace(r.store,trace)
        def validate(raw):
            decision=ManagementDecision.model_validate(raw)
            if decision.position_id!=intent['position_id']:raise ValueError('management position identity mismatch')
            if decision.action=='move_stop':
                anchor,atr=_reference(decision.stop_reference,evidence_from_market(market),time.time())
                long=proposal['side']=='long'
                if decision.stop_reference.field!=('low' if long else 'high') or not (
                        decision.stop_price<=anchor if long else decision.stop_price>=anchor):
                    raise ValueError('management stop wick reference invalid')
                if abs(decision.stop_price-anchor)>max(atr,rules.price_step)+rules.price_step*1e-6:
                    raise ValueError('management stop buffer invalid')
                if not (stop<decision.stop_price<min(quote.bid1,quote.raw['fairPrice']) if long else stop>decision.stop_price>max(quote.ask1,quote.raw['fairPrice'])):
                    raise ValueError('management stop must tighten')
            return decision
        try:
            llm.response_validator=validate
            llm.decision_schema=ManagementDecision.model_json_schema()
            llm.can_call=lambda:self._permitted(control.get('updated_at'))
            llm.deadline=now+r.cfg.decision_ttl_seconds
            llm.on_trace=archive
            paths=[]
            if control.get('version',r.cfg.analysis_version)=='vlm':
                paths=[str(c.path) for c in render_bundle(bundle,Path(r.cfg.analysis_chart_dir)/symbol/('management-'+bundle.fingerprint[:16]))]
            raw=llm.complete_json(system=POLICY,user=prompt,max_tokens=850,images=paths or None)
            trace.update(copy.deepcopy(getattr(llm,'last_trace',{})))
            trace.update(completed_at=time.time(),provider=getattr(llm,'last_provider','none'),model=getattr(llm,'last_model',''),
                status='failed' if raw is None else 'completed')
            if raw is None:
                r.store.put('management_status',dict(at=time.time(),symbol=symbol,status='ai_unavailable',
                    reason='AI yönetim yanıtı alınamadı; mevcut SL/TP sürüyor.'))
                return
            decision=validate(raw)
            ai=dict(provider=trace['provider'],model=trace['model'])
            trace.update(action=decision.action,rationale=decision.reason)
            if not self._permitted(control.get('updated_at')) or time.time()-now>r.cfg.decision_ttl_seconds:
                trace['execution_reason']='management_control_changed_or_expired';return
            data=decision.model_dump();data['ai']=ai
            latest=r.executor.state(intent['intent_id'])
            if latest.get('management_revision',0)!=revision or latest.get('status')!='protected':
                trace['execution_reason']='management_position_changed';return
            if decision.action!='hold':
                latest=r.executor.manage_position(intent['intent_id'],data,expected_revision=revision,decision_id=trace['id'],
                    guard=lambda:self._permitted(control.get('updated_at')) and time.time()-now<=r.cfg.decision_ttl_seconds)
                trace['execution_reason']=latest['status']
            with r.store.lock:
                current=r.store.get(key,{})
                current.update(review_timeframe=decision.review_timeframe,
                    decisions=(current.get('decisions',[])+[dict(at=time.time(),action=decision.action,reason=decision.reason,ai=ai)])[-10:])
                r.store.put(key,current)
            r.store.put('management_status',dict(at=time.time(),symbol=symbol,status=decision.action,reason=decision.reason,ai=ai))
        finally:
            archive()
            if llm is not r.llm and callable(getattr(llm,'close',None)):llm.close()
