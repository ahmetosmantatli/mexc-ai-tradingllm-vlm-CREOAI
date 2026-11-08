"""Durable conditional watches. No market API calls or executable orders.

Watch revisions coordinate parallel analyses of a symbol across strategies.
An opposite-side trade requires explicit acknowledgement of the prior revision;
merely finishing later does not authorize replacing a previously reviewed plan.
Closed candles supplied by the caller must actually be complete observations.
"""
import copy
import hashlib
import json
from math import isfinite
from time import time
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


TIMEFRAMES = {'5m':300,'15m':900,'4h':14400}
ACTIVE = {'watching','triggered'}


class WatchPlan(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    trigger_condition: Literal['price_above','price_below','closed_candle_cross_above','closed_candle_cross_below']
    trigger_price: float = Field(gt=0)
    timeframe: Literal['5m','15m','4h'] | None = None
    invalidation_condition: Literal['above','below']
    invalidation_price: float = Field(gt=0)
    expires_at: float = Field(gt=0)

    @model_validator(mode='after')
    def coherent(self):
        if self.trigger_condition.startswith('closed_candle') and self.timeframe is None:
            raise ValueError('candle trigger requires timeframe')
        if self.invalidation_condition=='above' and self.invalidation_price<=self.trigger_price:
            raise ValueError('trigger inside invalidation region')
        if self.invalidation_condition=='below' and self.invalidation_price>=self.trigger_price:
            raise ValueError('trigger inside invalidation region')
        return self


def watch_event_key(card):
    event=[card.get('created_at'),card.get('triggered_at'),card.get('structure_event')]
    return hashlib.sha256(json.dumps(event,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _price(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and isfinite(value) and value>0


def watch_triggered(plan: WatchPlan, *, price=None, closed_candles=None, now=None) -> bool:
    """Pure threshold/cross test; incomplete/future/unsorted candles never trigger."""
    now=time() if now is None else now
    if now>=plan.expires_at:
        return False
    condition=plan.trigger_condition
    if condition.startswith('price_'):
        return bool(_price(price) and (price>plan.trigger_price if condition.endswith('above') else price<plan.trigger_price))
    candles=(closed_candles or {}).get(plan.timeframe,[]) if isinstance(closed_candles,dict) else closed_candles or []
    if len(candles)<2:
        return False
    first,last=candles[-2:]
    before,after=first.get('close'),last.get('close')
    first_at=first.get('closed_at',first.get('time'))
    last_at=last.get('closed_at',last.get('time'))
    if not all(_price(v) for v in (before,after,first_at,last_at)) or not first_at<last_at<=now:
        return False
    if abs((last_at-first_at)-TIMEFRAMES[plan.timeframe])>1e-6:
        return False
    if first.get('complete',True) is not True or last.get('complete',True) is not True:
        return False
    return before<=plan.trigger_price<after if condition.endswith('above') else before>=plan.trigger_price>after


def watch_invalidated(plan: WatchPlan, *, price=None) -> bool:
    return bool(_price(price) and (price>=plan.invalidation_price if plan.invalidation_condition=='above' else price<=plan.invalidation_price))


def _snapshot(candidate):
    return dict(signal_id=candidate.signal_id,symbol=candidate.symbol,side=candidate.side.value,
        strategy=candidate.strategy.value,entry_price=candidate.entry_price,
        stop_loss_price=candidate.stop_loss_price,take_profit_price=candidate.take_profit_price,
        confidence=candidate.confidence,rationale=candidate.rationale[:1200],
        created_at=candidate.created_at.isoformat(),features={k:candidate.features[k] for k in
            ('dedup_key','source_closed_at') if isinstance(candidate.features.get(k),(str,int,float,bool))
            and len(str(candidate.features[k]))<=256})


class WatchCoordinator:
    def __init__(self,store,max_active=40):
        self.store=store
        self.max_active=max(1,min(int(max_active),100))

    @staticmethod
    def _key(symbol):
        return 'watch:'+symbol.strip().upper()

    def card(self,symbol):
        return copy.deepcopy(self.store.get(self._key(symbol),dict(symbol=symbol.strip().upper(),revision=0,status='none')))

    begin=card

    def _put(self,key,value):
        self.store.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(key,json.dumps(value,allow_nan=False)))

    def _event(self,state,reason,now):
        event=dict(id=uuid4().hex,symbol=state['symbol'],status=state['status'],reason=reason,at=now,revision=state['revision'])
        self._put('watch_events',(self.store.get('watch_events',[])+[event])[-100:])
        return event

    def commit(self,candidate,action,observed_revision,watch_plan=None,rationale='',*,reviewed_revision=None,now=None):
        """CAS decision; False means stale/conflicting. Validate before mutation.

        Caller must include card() in the model context, and pass reviewed_revision
        only when the model explicitly acknowledges that same prior plan revision.
        HOLD preserves an active watch; TRADE supersedes it only after this guard.
        """
        now=time() if now is None else now
        if action not in ('watch','trade','hold','cancel_watch'):
            raise ValueError('unknown decision')
        plan=WatchPlan.model_validate(watch_plan.model_dump() if isinstance(watch_plan,WatchPlan) else watch_plan) if action=='watch' else None
        if plan and not now<plan.expires_at<=now+86400:
            raise ValueError('watch expiry must be within 24 hours')
        with self.store.lock,self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            current=self.card(candidate.symbol)
            if current['revision']!=observed_revision:
                return False
            if action=='watch' and current.get('status') not in ACTIVE:
                count=self.store.db.execute("SELECT COUNT(*) FROM state WHERE key LIKE 'watch:%' AND json_extract(value,'$.status') IN ('watching','triggered')").fetchone()[0]
                if count>=self.max_active:
                    return False
            conflict=current.get('status') in ACTIVE and current.get('side')!=candidate.side.value
            if conflict and action in ('trade','watch') and reviewed_revision!=current['revision']:
                return False
            if action=='cancel_watch' and (current.get('status') not in ACTIVE or reviewed_revision!=current['revision'] or len(rationale.strip())<12):
                return False
            state=copy.deepcopy(current)
            state.update(revision=current['revision']+1,last_action=action,last_rationale=rationale[:1200],updated_at=now)
            if action=='watch':
                state.update(status='watching',side=candidate.side.value,candidate=_snapshot(candidate),
                    plan=plan.model_dump(),created_at=now,reason='model_watch',last_closed_at=None,triggered_at=None,structure_event=None,structure_events={})
            elif action=='cancel_watch':
                state.update(status='cancelled',reason='model_cancelled')
            elif action=='trade':
                state.update(status='superseded',reason='trade_reviewed',side=candidate.side.value,candidate=_snapshot(candidate))
            self._put(self._key(candidate.symbol),state)
            if action!='hold':
                self._event(state,state.get('reason',action),now)
            return True

    def observe(self,symbol,*,price=None,closed_candles=None,now=None,expected_revision=None):
        now=time() if now is None else now
        with self.store.lock,self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            state=self.card(symbol)
            if expected_revision is not None and state['revision']!=expected_revision:
                return None
            if state['status'] not in ACTIVE:
                return None
            plan=WatchPlan.model_validate(state['plan'])
            reason=None
            if now>=plan.expires_at:
                status,reason='expired','expiry'
            elif watch_invalidated(plan,price=price):
                status,reason='invalidated','invalidation_price'
            elif state['status']=='watching' and self._fresh_trigger(state,plan,price,closed_candles,now):
                status,reason='triggered','trigger_confirmed'
            if reason is None:
                return None
            state.update(status=status,reason=reason,revision=state['revision']+1,updated_at=now)
            if status=='triggered':
                state['triggered_at']=now
            self._put(self._key(symbol),state)
            return self._event(state,reason,now)

    @staticmethod
    def _fresh_trigger(state,plan,price,candles,now):
        if plan.trigger_condition.startswith('closed_candle'):
            rows=(candles or {}).get(plan.timeframe,[]) if isinstance(candles,dict) else candles or []
            if not rows:
                return False
            at=rows[-1].get('closed_at',rows[-1].get('time'))
            if not _price(at) or at<=state['created_at']:
                return False
        return watch_triggered(plan,price=price,closed_candles=candles,now=now)

    def request_structure_review(self,symbol,event,*,expected_revision,now=None):
        """Reconsider a changed thesis; this is not a price-trigger confirmation."""
        now=time() if now is None else now
        with self.store.lock,self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            state=self.card(symbol)
            if state.get('status') not in ACTIVE or state['revision']!=expected_revision:return False
            events=state.get('structure_events',{})
            if events.get(event.get('timeframe'))==event:return False
            available=event.get('available_at')
            if not _price(available) or not state.get('created_at',0)<available<=now:return False
            if event.get('timeframe') not in ('15m','4h') or event.get('direction')!=('bearish' if state['side']=='long' else 'bullish'):
                return False
            events[event['timeframe']]=copy.deepcopy(event)
            state.update(status='triggered',reason='structure_changed',structure_event=copy.deepcopy(event),structure_events=events,
                triggered_at=now,revision=state['revision']+1,updated_at=now)
            self._put(self._key(symbol),state)
            self._event(state,'structure_changed',now)
            return True

    def terminate(self,symbol,reason='cancelled',*,now=None,expected_revision=None):
        if reason not in ('cancelled','position_opened','invalidated','expired'):
            raise ValueError('invalid terminal reason')
        now=time() if now is None else now
        with self.store.lock,self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            state=self.card(symbol)
            if expected_revision is not None and state['revision']!=expected_revision:
                return None
            if state['status'] not in ACTIVE and not (reason=='position_opened' and state['status']=='superseded'):
                return None
            state.update(status=reason,reason=reason,revision=state['revision']+1,updated_at=now)
            self._put(self._key(symbol),state)
            return self._event(state,reason,now)


def watch_snapshot(store):
    """Bounded safe Telegram/operator summary; full candidate remains server-side."""
    with store.lock:
        keys=[r[0] for r in store.db.execute("SELECT key FROM state WHERE key LIKE 'watch:%' ORDER BY (json_extract(value,'$.status') IN ('watching','triggered')) DESC, key LIMIT 100")]
        result=[]
        for key in keys:
            row=store.get(key,{})
            plan=row.get('plan',{})
            result.append(dict(symbol=row.get('symbol'),status=row.get('status'),side=row.get('side'),
                revision=row.get('revision',0),trigger_type=plan.get('trigger_condition'),trigger_price=plan.get('trigger_price'),
                timeframe_seconds=TIMEFRAMES.get(plan.get('timeframe')),expires_at=plan.get('expires_at'),
                reason=row.get('last_rationale') or row.get('reason',''),review_reason=row.get('reason'),last_action=row.get('last_action'),invalidation=dict(condition=plan.get('invalidation_condition'),price=plan.get('invalidation_price'))))
        return result
