"""Credential pool with shared quota groups, conservative local reservations.

A second API key does not imply a second provider quota. Configure distinct
quota groups only for genuinely separate projects/organizations. Local token
estimates and rolling-24h limits are safeguards, not provider quota telemetry.
All routes receive identical context/images. Valid HOLD never triggers fallback.
"""
from dataclasses import dataclass, field
import copy
import hashlib
import json
import math
import sqlite3
import time
from uuid import uuid4

import httpx

from trading.agents.anthropic_client import AnthropicClient
from trading.agents.provider_router import ProviderRouter


class PrefixStore:
    """Namespace subrouter cooldown/usage; outer regulator owns all quota SQL."""
    def __init__(self, store, prefix):
        self.store, self.prefix = store, prefix
        self.lock, self.db = store.lock, store.db

    def get(self, key, default=None):
        return self.store.get(self.prefix+key, default)

    def put(self, key, value):
        return self.store.put(self.prefix+key, value)


@dataclass
class _Slot:
    provider: str
    label: str
    group: str
    router: object = field(repr=False)


class ProviderPool:
    def __init__(self, settings, budget, store, client=None):
        self.settings, self.store, self._paid_budget = settings, store, budget
        self.budget = None
        self.client = client or httpx.Client(timeout=settings.ai_provider_timeout_seconds)
        self._owns_client = client is None
        self.claude = AnthropicClient(settings=settings, budget=budget)
        self.can_call = lambda: True
        self.deadline = None
        self.response_validator = None
        self.decision_schema = None
        self.on_trace = None
        self.last_status = 'not_called'
        self.last_provider = 'none'
        self.last_model = ''
        self.last_fallback_reason = ''
        self.last_credential_id = ''
        self.last_quota_group = ''
        self.successful_calls = 0
        self.last_trace = {}
        self._slots = []
        self._sleep = time.sleep
        seen = set()
        for provider in ('groq','gemini'):
            for number in (1,2):
                suffix = '' if number == 1 else '_2'
                credential = getattr(settings, provider+'_api_key'+suffix, '')
                if not credential:
                    continue
                # Ephemeral fingerprint only; never persist the key or key hash.
                identity=(provider, hashlib.sha256(credential.encode()).digest())
                if identity in seen:
                    continue
                seen.add(identity)
                main=getattr(settings, provider+'_quota_group', provider+'-main')
                group=getattr(settings,provider+'_quota_group'+suffix,main) or main
                group_id=hashlib.sha256((provider+'\0'+group).encode()).hexdigest()
                label=provider+'-'+str(number)
                overrides=dict(groq_api_key='',gemini_api_key='',anthropic_api_key='',
                    ai_primary_provider=provider,ai_vision_primary_provider=provider,
                    ai_claude_fallback_enabled=False,groq_request_token_budget=0)
                overrides[provider+'_api_key']=credential
                child_settings=settings.model_copy(update=overrides)
                prefix='pool:route:'+group_id+':'+label+':'
                router=ProviderRouter(child_settings,None,PrefixStore(store,prefix),self.client)
                self._slots.append(_Slot(provider,label,group_id,router))

    def fork(self):
        """Fresh request-local state/clients; quota leases and paid budget remain shared."""
        child=ProviderPool(self.settings,self._paid_budget,self.store,
                           client=None if self._owns_client else self.client)
        child.can_call=self.can_call
        child.deadline=self.deadline
        child.response_validator=self.response_validator
        child.decision_schema=copy.deepcopy(self.decision_schema)
        return child

    def close(self):
        if self._owns_client:
            self.client.close()
        sdk=getattr(self.claude,'_client',None)
        if sdk is not None and hasattr(sdk,'close'):
            sdk.close()

    def _allowed(self):
        return self.can_call() and (self.deadline is None or time.time()<self.deadline)

    def _primary(self, images):
        return (self.settings.ai_vision_primary_provider or self.settings.ai_primary_provider) if images else self.settings.ai_primary_provider

    @property
    def available(self):
        if self._primary(self.settings.analysis_version=='vlm')=='anthropic':
            return self.claude.available
        return bool(self._slots or (self.settings.ai_claude_fallback_enabled and self.claude.available))

    def _model(self, slot, images):
        return getattr(self.settings, slot.provider+('_vision_model' if images else '_text_model'))

    def _estimate(self, slot, system, user, max_tokens, images):
        completion=max(max_tokens,getattr(self.settings,slot.provider+'_max_completion_tokens'))
        return math.ceil(len((system+user).encode('utf-8'))/3)+512+2048*len(images or [])+completion

    def _quota_key(self,slot,images):
        return 'pool:quota:'+slot.group+':'+hashlib.sha256(self._model(slot,images).encode()).hexdigest()

    def _limits(self,slot):
        defaults = (30,8000,1000,200000) if slot.provider=='groq' else (15,250000,500,0)
        return tuple(getattr(self.settings,slot.provider+'_'+name,default)
                     for name,default in zip(('rpm','tpm','rpd','tpd'),defaults))

    def _cooldown(self, slot, images):
        model=self._model(slot,images)
        return slot.router.store.get(slot.provider+':retry_at:'+model,0)

    def _reserve(self, candidates, system, user, max_tokens, images):
        """Choose and lease one group atomically; never hold DB locks during HTTP."""
        now=time.time()
        with self.store.lock,self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            choices=[]
            for slot in candidates:
                key=self._quota_key(slot,images)
                if self._cooldown(slot,images)>now:
                    continue
                state=self.store.get(key,dict(rows=[],last_used=0,lease=None))
                rows=[row for row in state['rows'] if now-row['at']<86400]
                recent=[row for row in rows if now-row['at']<60]
                lease=state.get('lease')
                if state.get('retry_at',0)>now:
                    continue
                if lease and lease['until']>now:
                    continue
                tokens=self._estimate(slot,system,user,max_tokens,images)
                rpm,tpm,rpd,tpd=self._limits(slot)
                minute_tokens=sum(row['tokens'] for row in recent)
                daily_tokens=sum(row['tokens'] for row in rows)
                if ((rpm and len(recent)+1>rpm) or (tpm and minute_tokens+tokens>tpm)
                        or (rpd and len(rows)+1>rpd) or (tpd and daily_tokens+tokens>tpd)):
                    continue
                utilization=max((len(recent)/rpm if rpm else 0),
                    minute_tokens/tpm if tpm else 0,len(rows)/rpd if rpd else 0,
                    daily_tokens/tpd if tpd else 0)
                priority=int(slot.provider!=getattr(self,'_preferred_provider','groq'))
                choices.append((priority,utilization,state.get('last_used',0),slot.label,slot,key,state,rows,tokens))
            if not choices:
                return None
            _,_,_,_,slot,key,state,rows,tokens=min(choices,key=lambda row:row[:4])
            lease_id=uuid4().hex
            state.update(rows=rows+[dict(id=lease_id,at=now,tokens=tokens)],last_used=now,
                lease=dict(id=lease_id,until=now+self.settings.ai_provider_timeout_seconds+15))
            self.store.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(key,json.dumps(state)))
            return slot,key,lease_id,tokens

    def _release(self,key,lease_id,attempt=None,cooldown=0):
        with self.store.lock,self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            state=self.store.get(key)
            if state and (state.get('lease') or {}).get('id')==lease_id:
                state['lease']=None
                if cooldown:
                    state['retry_at']=max(state.get('retry_at',0),cooldown)
                if attempt and all(isinstance(attempt.get(field),int) and attempt[field]>=0 for field in ('input_tokens','output_tokens')):
                    for row in state['rows']:
                        if row.get('id')==lease_id:
                            row['tokens']=attempt['input_tokens']+attempt['output_tokens']
                            row['usage_known']=True
                self.store.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(key,json.dumps(state)))

    def snapshot(self):
        """Safe local quota estimates; no provider credit or remaining-quota claim."""
        now=time.time()
        rows=[]
        seen=set()
        for slot in self._slots:
            for images in (False,True):
                key=self._quota_key(slot,images)
                if key in seen:
                    continue
                seen.add(key)
                state=self.store.get(key,dict(rows=[],lease=None))
                day=[r for r in state['rows'] if now-r['at']<86400]
                minute=[r for r in day if now-r['at']<60]
                rpm,tpm,rpd,tpd=self._limits(slot)
                rows.append(dict(provider=slot.provider,model=self._model(slot,images),quota_group=slot.group[:12],
                    requests_minute=len(minute),tokens_minute_estimate=sum(r['tokens'] for r in minute),
                    requests_rolling_24h=len(day),tokens_rolling_24h_estimate=sum(r['tokens'] for r in day),
                    requests_minute_remaining=max(0,rpm-len(minute)) if rpm else None,
                    tokens_minute_remaining_estimate=max(0,tpm-sum(r['tokens'] for r in minute)) if tpm else None,
                    requests_rolling_24h_remaining=max(0,rpd-len(day)) if rpd else None,
                    tokens_rolling_24h_remaining_estimate=max(0,tpd-sum(r['tokens'] for r in day)) if tpd else None,
                    cooldown_seconds=max(0,state.get('retry_at',0)-now,self._cooldown(slot,images)-now),
                    in_flight=bool(state.get('lease') and state['lease']['until']>now),
                    local_estimate=True))
        return dict(groups=rows,quota_window='rolling_24h',provider_reported=False)

    def _notify(self):
        self.last_trace.update(status=self.last_status,provider=self.last_provider,
            model=self.last_model,fallback_reason=self.last_fallback_reason,
            credential_id=self.last_credential_id,quota_group=self.last_quota_group)
        if self.on_trace:
            self.on_trace(copy.deepcopy(self.last_trace))

    def _finish(self,result):
        with self.store.lock:
            stats=self.store.get('ai_request_stats',{})
            for attempt in self.last_trace['attempts']:
                row=stats.setdefault(attempt['provider'],dict(generation_requests=0,count_requests=0,successes=0,failures=0))
                row['generation_requests']+=attempt.get('generation_requests',0)
                row['count_requests']+=attempt.get('count_requests',0)
                row['successes']+=int(attempt.get('status')=='success')
                row['failures']+=int(attempt.get('status') in ('failed','rejected','invalid_schema','error_or_invalid_output','provider_credit_exhausted','output_truncated','model_refusal'))
            self.store.put('ai_request_stats',stats)
            for provider in ('groq','gemini'):
                usage=self.store.get(provider+'_usage',dict(successful_calls=0,input_tokens=0,output_tokens=0))
                for attempt in self.last_trace['attempts']:
                    if attempt.get('provider')!=provider:
                        continue
                    usage['successful_calls']+=int(attempt.get('status')=='success')
                    for field in ('input_tokens','output_tokens'):
                        count=attempt.get(field)
                        if isinstance(count,int) and count>=0:
                            usage[field]+=count
                self.store.put(provider+'_usage',usage)
        self._notify()
        return result

    def _attempt(self,reservation,system,user,max_tokens,images):
        slot,key,lease_id,tokens=reservation
        self.last_credential_id=slot.label
        self.last_quota_group=slot.group[:12]
        self.last_provider=slot.provider
        self.last_model=self._model(slot,images)
        router=slot.router
        router.can_call=self._allowed
        router.deadline=self.deadline
        router.response_validator=self.response_validator
        router.decision_schema=copy.deepcopy(self.decision_schema)
        base=len(self.last_trace['attempts'])
        def progress(trace):
            attempts=copy.deepcopy(trace.get('attempts',[]))
            for item in attempts:
                item.update(slot=slot.label,credential_id=slot.label,quota_group=slot.group[:12],quota_estimated_tokens=tokens)
            self.last_trace['attempts']=self.last_trace['attempts'][:base]+attempts
            self._notify()
        router.on_trace=progress
        try:
            result=router.complete_json(system=system,user=user,max_tokens=max_tokens,images=images)
            self.last_status=router.last_status
            self.last_fallback_reason=router.last_fallback_reason
            # Subrouter's generic final status is not the useful underlying failure.
            if result is None and router.last_trace.get('attempts'):
                self.last_status=router.last_trace['attempts'][-1].get('status',router.last_status)
            return result
        finally:
            latest=router.last_trace.get('attempts',[])
            attempt=latest[-1] if latest else {}
            cooldown=self._cooldown(slot,images) if attempt.get('http_status')==429 else 0
            self._release(key,lease_id,attempt,cooldown)

    def _wait(self,seconds):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            if not self._allowed():
                return False
            self._sleep(min(.2,max(0,until-time.monotonic())))
        return self._allowed()

    def _busy(self,slots,images):
        now=time.time()
        return any((self.store.get(self._quota_key(slot,images),{}).get('lease') or {}).get('until',0)>now
                   for slot in slots)

    def _retryable(self,slot,images):
        attempts=self.last_trace.get('attempts',[])
        if not attempts:
            return False
        attempt=attempts[-1]
        status=attempt.get('http_status')
        transient=(status==429 or isinstance(status,int) and status>=500
            or attempt.get('error_category') in ('network_timeout','network_error'))
        if not transient:
            return False
        seconds=max(0,min(10,getattr(self.settings,'ai_pool_retry_seconds',5)))
        # Do not bypass a provider's Retry-After. Longer delays use future cycles.
        return status!=429 or self._cooldown(slot,images)<=time.time()+seconds

    def _paid(self,system,user,max_tokens,images):
        self.last_credential_id='anthropic-1'
        self.last_quota_group='paid-shared'
        self.last_provider='anthropic'
        self.last_model=self.settings.anthropic_model
        self.claude.can_call=self._allowed
        self.claude.decision_schema=copy.deepcopy(self.decision_schema)
        index=len(self.last_trace['attempts'])
        self.last_trace['attempts'].append(dict(provider='anthropic',model=self.last_model,status='requesting',
            credential_id=self.last_credential_id,quota_group=self.last_quota_group,generation_requests=0,count_requests=0))
        self._notify()
        result=self.claude.complete_json(system=system,user=user,max_tokens=max_tokens,images=images)
        attempt=copy.deepcopy(self.claude.last_attempt)
        attempt.update(credential_id=self.last_credential_id,quota_group=self.last_quota_group)
        self.last_trace['attempts'][index]=attempt
        self.last_status=self.claude.last_status
        if result is not None:
            try:
                if not isinstance(result,dict):
                    raise ValueError('object required')
                if self.response_validator:
                    self.response_validator(result)
            except Exception as exc:
                self.last_status='error_or_invalid_output'
                helper=self._slots[0].router if self._slots else ProviderRouter(self.settings,None,PrefixStore(self.store,'pool:validation:'),self.client)
                helper.decision_schema=self.decision_schema
                attempt.update(status='invalid_schema',error_category='invalid_decision_schema',validation_issues=helper._validation_issues(exc))
                return None
            self.successful_calls+=1
        return result

    def complete_json(self, *, system, user, max_tokens=800, images=None):
        self.last_trace=dict(attempts=[],status='pending')
        self.last_status='not_called'
        self.last_credential_id=''
        self.last_quota_group=''
        self.last_provider='none'
        self.last_model=''
        self.last_fallback_reason=''
        if not self._allowed():
            self.last_status='stopped'
            return self._finish(None)
        primary=self._primary(images)
        self._preferred_provider=primary
        remaining=list(self._slots) if primary!='anthropic' else []
        retry=None
        waited_for_capacity=False
        while remaining and self._allowed():
            reservation=self._reserve(remaining,system,user,max_tokens,images)
            if reservation is None:
                self.last_fallback_reason='free_pool_quota_or_cooldown'
                if (not waited_for_capacity and getattr(self.settings,'ai_pool_max_retry_attempts',1)
                        and self._busy(remaining,images)):
                    waited_for_capacity=True
                    if not self._wait(max(0,min(10,self.settings.ai_pool_retry_seconds))):
                        self.last_status='stopped'
                        return self._finish(None)
                    continue
                break
            slot=reservation[0]
            # One attempt per quota group/model per pass, not one per nominal key.
            group_key=self._quota_key(slot,images)
            remaining=[other for other in remaining if other is not slot]
            result=self._attempt(reservation,system,user,max_tokens,images)
            if result is not None:
                self.successful_calls+=1
                return self._finish(result)
            if retry is None and self._retryable(slot,images):
                retry=slot
        if not self._allowed():
            self.last_status='stopped'
            return self._finish(None)
        if retry is not None and getattr(self.settings,'ai_pool_max_retry_attempts',1):
            delay=max(0,min(10,getattr(self.settings,'ai_pool_retry_seconds',5)))
            if not self._wait(delay):
                self.last_status='stopped'
                return self._finish(None)
            # Router's generic 60s network/server cooldown is a local scheduling
            # hint; only HTTP429 Retry-After must remain binding.
            previous=[a for a in self.last_trace['attempts'] if a.get('slot')==retry.label][-1]
            if previous.get('http_status')!=429:
                retry.router.store.put(retry.provider+':retry_at:'+self._model(retry,images),0)
            reservation=self._reserve([retry],system,user,max_tokens,images)
            if reservation:
                result=self._attempt(reservation,system,user,max_tokens,images)
                if result is not None:
                    self.successful_calls+=1
                    return self._finish(result)
        if not self._allowed():
            self.last_status='stopped'
            return self._finish(None)
        if primary!='anthropic' and not self.settings.ai_claude_fallback_enabled:
            self.last_status='primary_unavailable_fallback_disabled'
            return self._finish(None)
        if primary!='anthropic':
            self.last_fallback_reason=self.last_fallback_reason or 'free_pool_unavailable'
        return self._finish(self._paid(system,user,max_tokens,images))
