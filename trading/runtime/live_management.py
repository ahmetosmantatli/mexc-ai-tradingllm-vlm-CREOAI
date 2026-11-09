"""Event-driven AI reviews of owned positions, independent of entry scanning."""
from trading.runtime.live_capabilities import STRUCTURAL_IOC_RUNNERS, management_runner
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
from trading.quant.structure_setups import trade_horizon

TIMEFRAME_SECONDS={'5m':300,'15m':900,'4h':14400}

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


class Runner2ManagementDecision(ManagementDecision):
    action: str = Field(pattern='^(hold|move_stop|move_target|reduce|close)$')
    target_price: float | None = Field(default=None,gt=0)
    target_reference: CandleReference | None = None

    @model_validator(mode='after')
    def consistent_target(self):
        has_target = self.target_price is not None or self.target_reference is not None
        if self.action=='move_target':
            if self.target_price is None or self.target_reference is None:
                raise ValueError('target action requires price and candle reference')
        elif has_target:
            raise ValueError('target fields allowed only for move_target')
        return self


COMMON_POLICY=(
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
    'Money objectives never justify raising risk or forcing trades. Charts if supplied are the same candle evidence. '
    'No guarantee of execution. Server revalidates ownership, revision, age, tick/quantity and protection before writes.'
)
POLICY=COMMON_POLICY+' Preserve native target; reduce/close when the thesis or target is no longer appropriate.'
RUNNER2_POLICY=COMMON_POLICY+(
    ' Runner 2 also supports action move_target once, on a near-target event before the native target touches. '
    'Keep the stop unchanged. Supply target_price and target_reference only for move_target, both null otherwise. '
    'Target must extend in the profitable direction and anchor to a supplied closed high for long or low for short. '
    'Explain why confirmed continuation supports the extension; otherwise hold, reduce, or close. '
    'To promote an existing horizon, propose move_target with a higher review_timeframe and target reference in that timeframe. '
    'Promotion requires a recent directional break on that timeframe; the native stop stays unchanged. '
    'A horizon_due event asks for review, never automatic closing. CHoCH/MSS and candidate order blocks are closed-candle evidence.'
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
        return quote

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
        observed_quote=self.observe(intent)
        card=r.store.get(key,{})
        if now-card.get('last_attempt_at',0)<r.cfg.live_management_min_interval_seconds:return
        proposal=intent['proposal'];symbol=proposal['symbol']
        metadata=proposal.get('metadata') or {}
        opening_runner=metadata.get('runner_id','baseline-v1')
        runner_id=management_runner(opening_runner,control.get('runner_id'))
        runner2=runner_id in STRUCTURAL_IOC_RUNNERS
        variant=metadata.get('analysis_version') if 'runner_id' in metadata else None
        variant=variant if variant in ('text','vlm') else control.get('version',r.cfg.analysis_version)
        stop,target=r.executor.protection_levels(intent)
        long=proposal['side']=='long'
        near_target=False
        if runner2 and observed_quote is not None:
            fair=observed_quote.raw['fairPrice']
            distance=target-max(observed_quote.ask1,fair) if long else min(observed_quote.bid1,fair)-target
            if distance<=0:
                return # The native target may already be executing; the monitor reconciles it.
            initial_risk=abs(intent['entry_fill_price']-proposal['stop_loss']['price'])
            near_target=(card.get('last_near_target')!=target and
                         distance<=min(.25*initial_risk,.5*card.get('review_atr',0)))
        entry_timeframe=(metadata.get('price_plan') or {}).get('entry_timeframe')
        horizon=(intent.get('trade_horizon') or metadata.get('trade_horizon')) if runner2 else None
        timeframe=(horizon or {}).get('review_timeframe') or card.get('review_timeframe') or entry_timeframe or '15m'
        due_at=(horizon or {}).get('review_due_at')
        horizon_due=due_at is not None and now>=due_at and card.get('last_horizon_due')!=due_at
        milestone=min(100,max(0,int(math.floor(card.get('mfe_r',0)))))
        previous=card.get('last_event') or {}
        retry=card.get('provider_retry') or {}
        revision=intent.get('management_revision',0)
        retry_due=(retry.get('revision')==revision and retry.get('control_revision')==control.get('updated_at')
                   and retry.get('at',0)<=now<retry.get('expires_at',0))
        # No reason to download three histories again before either trigger can change.
        if (previous.get('management_runner',opening_runner)==runner_id and previous.get('timeframe')==timeframe and previous.get('favorable_r_milestone')==milestone
                and now<previous.get('closed_at',0)+TIMEFRAME_SECONDS[timeframe] and not retry_due and not near_target and not horizon_due):return
        bundle=build_bundle(r.public,symbol)
        market=bundle.to_context(ict=True) if runner2 else bundle.to_context()
        view=next((v for v in market['timeframes'] if v['timeframe']==timeframe),None)
        if view is None:raise ValueError('management entry timeframe evidence missing')
        event=dict(timeframe=timeframe,closed_at=view['closed_at'],favorable_r_milestone=milestone)
        if runner_id!=opening_runner:
            event['management_runner']=runner_id
        if horizon_due or (retry_due and due_at is not None and previous.get('horizon_due')==due_at):
            event['horizon_due']=due_at
        if runner2:
            evidence=evidence_from_market(market)
            ref=CandleReference(timeframe=timeframe,open_time=int(evidence[timeframe][-1][0]),field='high')
            _,review_atr=_reference(ref,evidence,now)
            if observed_quote is not None and (card.get('last_near_target')!=target
                    or retry_due and previous.get('near_target')==target):
                near_target=distance<=min(.25*initial_risk,.5*review_atr)
            if near_target:
                event['near_target']=target
        same_event=previous==event
        if same_event and not retry_due:return
        # Durable attempt reservation prevents restart/network failures causing call storms.
        with r.store.lock:
            current=r.store.get(key,{})
            current.update(last_attempt_at=now,last_event=event)
            if runner2:
                current['review_atr']=review_atr
                if near_target:
                    current['last_near_target']=target
            if horizon_due:
                current['last_horizon_due']=due_at
            current.pop('provider_retry',None)
            r.store.put(key,current)
            card=current
        snapshot=r._refresh([symbol])
        position=next((p for p in snapshot['positions'] if str(p['position_id'])==intent['position_id']),None)
        if position is None:return
        opened_ms=position.get('opened_at')
        opened_at=opened_ms/1000 if isinstance(opened_ms,(int,float)) and 0<opened_ms/1000<=now else None
        elapsed_bars=(int((now-opened_at)/TIMEFRAME_SECONDS[entry_timeframe])
                      if opened_at is not None and entry_timeframe in TIMEFRAME_SECONDS else None)
        position_card=dict(position,initial_entry=proposal.get('entry_price'),filled_entry=intent['entry_fill_price'],
            initial_stop=proposal['stop_loss']['price'],current_stop=stop,current_target=target,
            remaining_contracts=r.executor.remaining_contracts(intent),original_thesis=proposal.get('rationale'),
            original_price_plan=metadata.get('price_plan'),opened_at=opened_at,
            entry_timeframe=entry_timeframe,review_timeframe=timeframe,elapsed_entry_bars=elapsed_bars,
            initial_risk_usdt=proposal.get('max_loss_if_stopped'),remaining_planned_loss_usdt=intent.get('planned_loss'),excursions={k:card.get(k) for k in ('current_r','mfe_r','mae_r','sampled_at','excursion_source')},
            previous_decisions=card.get('decisions',[])[-5:],confirmed_actions=intent.get('management_events',[])[-5:],
            management_revision=revision,opening_runner_id=opening_runner,management_runner_id=runner_id)
        if runner2:
            position_card['trade_horizon']=horizon
            position_card['target_extension_available']=bool(near_target and not intent.get('target_extensions'))
        quote=r.quote(symbol);rules=r.public.get_contract_rules(symbol)
        if runner2:
            from trading.analysis.market import benchmark_context, trade_flow_context
            market['benchmarks']=benchmark_context(r.public,market)
            market['recent_trade_flow']=trade_flow_context(r.public,symbol)
        prompt=json.dumps(dict(task='manage_existing_position',event=event,position=position_card,
            account=r._live_context(snapshot,{**control,'runner_id':runner_id}),market=compact_market(market),
            current_quote=dict(bid=quote.bid1,ask=quote.ask1,fair=quote.raw['fairPrice']),price_step=rules.price_step),
            ensure_ascii=False,separators=(',',':'),allow_nan=False)
        llm=r.management_llm()
        trace=dict(id=uuid4().hex,started_at=now,symbol=symbol,strategy='position_management',mode='LIVE',
            runner_id=runner_id,opening_runner_id=opening_runner,
            analysis_version=variant,status='pending',attempts=[],prompt_bytes=len(prompt.encode()),input_fingerprint=digest(prompt))
        def archive(update=None):
            if update:trace.update(copy.deepcopy(update))
            with r.store.lock:
                r.store.put('management_trace',trace)
                archive_trace(r.store,trace)
        schema=Runner2ManagementDecision if runner2 else ManagementDecision
        def validate(raw):
            decision=schema.model_validate(raw)
            long=proposal['side']=='long'
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
            if decision.action=='move_target':
                if not position_card['target_extension_available']:
                    raise ValueError('management target extension unavailable')
                reference=decision.target_reference
                anchor,atr=_reference(reference,evidence,time.time())
                if (reference.field!=('high' if long else 'low')
                        or not (decision.target_price>target and anchor>target if long else decision.target_price<target and anchor<target)
                        or abs(decision.target_price-anchor)>max(atr,rules.price_step)+rules.price_step*1e-6):
                    raise ValueError('management target requires a grounded extension')
                if horizon and TIMEFRAME_SECONDS[decision.review_timeframe]>TIMEFRAME_SECONDS[timeframe]:
                    higher=next((v for v in market['timeframes'] if v['timeframe']==decision.review_timeframe),{})
                    features=higher.get('features',{})
                    direction='bullish' if long else 'bearish'
                    recent=higher.get('recent_ohlcv',[])[-3:]
                    if (reference.timeframe!=decision.review_timeframe or features.get('trend')!=direction
                            or not any(br['direction']==direction and br['at'] in {bar[0] for bar in recent}
                                       for br in features.get('breaks',[]))):
                        raise ValueError('horizon promotion requires recent higher timeframe continuation')

            return decision
        try:
            llm.response_validator=validate
            llm.decision_schema=schema.model_json_schema()
            llm.can_call=lambda:self._permitted(control.get('updated_at'))
            llm.deadline=now+r.cfg.decision_ttl_seconds
            llm.on_trace=archive
            paths=[]
            if variant=='vlm':
                paths=[str(c.path) for c in render_bundle(bundle,Path(r.cfg.analysis_chart_dir)/symbol/('management-'+bundle.fingerprint[:16]))]
            policy=RUNNER2_POLICY if runner2 else POLICY
            if runner_id=='runner3':
                policy+=(' Runner 3 manages the supplied existing position without re-sizing the entry. '
                         'Compare the remaining net payoff, current structural evidence and open portfolio stop risk. '
                         'The daily objective is reporting context. Do not increase exposure or loosen protection.')
            raw=llm.complete_json(system=policy,user=prompt,max_tokens=850,images=paths or None)
            trace.update(copy.deepcopy(getattr(llm,'last_trace',{})))
            trace.update(completed_at=time.time(),provider=getattr(llm,'last_provider','none'),model=getattr(llm,'last_model',''),
                status='failed' if raw is None else 'completed')
            if raw is None:
                if not same_event and self._permitted(control.get('updated_at')):
                    retry_at=max(now+r.cfg.live_management_min_interval_seconds,time.time()+5)
                    with r.store.lock:
                        current=r.store.get(key,{})
                        current['provider_retry']=dict(at=retry_at,expires_at=retry_at+r.cfg.decision_ttl_seconds,
                            revision=revision,control_revision=control.get('updated_at'))
                        r.store.put(key,current)
                r.store.put('management_status',dict(at=time.time(),symbol=symbol,status='ai_unavailable',
                    reason='AI yönetim yanıtı alınamadı; mevcut SL/TP sürüyor.'))
                return
            decision=validate(raw)
            ai=dict(provider=trace['provider'],model=trace['model'])
            trace.update(action=decision.action,rationale=decision.reason)
            if not self._permitted(control.get('updated_at')) or time.time()-now>r.cfg.decision_ttl_seconds:
                trace['execution_reason']='management_control_changed_or_expired';return
            data=decision.model_dump();data['ai']=ai
            data['management_runner_id']=runner_id
            if (horizon and decision.action=='move_target'
                    and TIMEFRAME_SECONDS[decision.review_timeframe]>TIMEFRAME_SECONDS[timeframe]):
                data['trade_horizon']=trade_horizon(decision.review_timeframe,time.time())
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
