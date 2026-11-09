"""Credential pool with shared quota groups, conservative local reservations.

A second API key does not imply a second provider quota. Configure distinct
quota groups only for genuinely separate projects/organizations. Local token
estimates and provider-specific daily windows are safeguards, not live quota telemetry.
All routes receive identical context/images. Valid HOLD never triggers fallback.
A local free-route estimate of zero is not proof those routes are exhausted.
"""
from dataclasses import dataclass, field
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import copy
import hashlib
import json
import math
import sqlite3
import time
from uuid import uuid4

import httpx

from trading.agents.anthropic_client import AnthropicClient
from trading.agents.provider_limits import blocked
from trading.agents.provider_router import ProviderRouter
from trading.agents.telemetry import is_model_output_failure


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
    model: str | None = None
    modes: tuple = ('text','vlm')
    limits: tuple | None = None
    quota_bucket: str | None = None


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
        self._external_signature = None
        self._external_expires = 0
        self._sleep = time.sleep
        self._probe_system = 'Return JSON only. No trading actions.'
        self._probe_user = 'Return exactly {"action":"hold"}.'
        self._probe_tokens = 256
        seen = set()
        from trading.agents.quota_routes import parse_routes
        routes=parse_routes(getattr(settings,'ai_free_model_routes','[]'))
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
                for index,route in enumerate(routes):
                    if route.provider!=provider:continue
                    extra=settings.model_copy(update={**overrides,provider+'_text_model':route.model,provider+'_vision_model':route.model})
                    route_label=label+'-model-'+str(index+1)
                    child=ProviderRouter(extra,None,PrefixStore(store,'pool:route:'+group_id+':'+route_label+':'),self.client)
                    self._slots.append(_Slot(provider,route_label,group_id,child,route.model,tuple(route.modes),
                        (route.rpm,route.tpm,route.rpd,route.tpd),route.quota_bucket))
        self._reload_external()

    def _reload_external(self):
        """Hourly discovery may replace a manifest; existing strategies stay intact."""
        if not getattr(self.settings,'external_models_enabled',False):return
        from pathlib import Path
        from trading.agents.external_catalog import load_routes
        from trading.agents.external_client import ExternalClient
        path=Path(self.settings.external_models_file)
        try:signature=(path.stat().st_mtime_ns,int(time.time()/3600))
        except OSError:signature=None
        if signature==self._external_signature and time.time()<self._external_expires:return
        self._external_signature=signature
        self._external_expires=time.time()+60
        verified_limit=50
        try:
            manifest=json.loads(path.read_text())
            self._external_expires=min(self._external_expires,manifest['observed_at']+86400)
            for report in manifest.get('reports',[]):
                if report.get('provider')=='openrouter':
                    limit=report.get('daily_quota',{}).get('limit')
                    if type(limit) is int and 0<limit<=1000:verified_limit=limit
        except (OSError,ValueError,KeyError,TypeError,AttributeError):pass
        slots=[s for s in self._slots if s.provider not in ('cloudflare','openrouter')]
        for route in load_routes(path):
            if route.provider=='openrouter' and not self.settings.openrouter_api_key:continue
            if route.provider=='cloudflare' and not (self.settings.cloudflare_api_token and self.settings.cloudflare_free_plan_confirmed):continue
            group=hashlib.sha256((route.provider+'\0'+(self.settings.cloudflare_account_id if route.provider=='cloudflare' else 'account')).encode()).hexdigest()
            router=ExternalClient(self.settings,route,self.store,self.client)
            # OpenRouter has a common 20 RPM / baseline 50 RPD free account cap.
            # A larger allowance needs an authenticated discovery report.
            # Cloudflare 4 RPM is an operational guard;
            # its actual admission unit is account-wide neurons in the client.
            limits=(20,0,verified_limit,0) if route.provider=='openrouter' else (4,0,0,0)
            label=route.provider+'-'+hashlib.sha256(route.model.encode()).hexdigest()[:8]
            slots.append(_Slot(route.provider,label,group,router,route.model,
                ('text','vlm') if route.vision else ('text',),limits,'account-all-models'))
        self._slots=slots

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
        self._reload_external()
        if self._primary(self.settings.analysis_version=='vlm')=='anthropic':
            return self.claude.available
        return bool(self._slots or (self.settings.ai_claude_fallback_enabled and self.claude.available))

    def _model(self, slot, images):
        return slot.model or getattr(self.settings, slot.provider+('_vision_model' if images else '_text_model'))

    def _estimate(self, slot, system, user, max_tokens, images):
        completion=max(max_tokens,getattr(self.settings,slot.provider+'_max_completion_tokens',self.settings.external_max_completion_tokens))
        return math.ceil(len((system+user).encode('utf-8'))/3)+512+2048*len(images or [])+completion

    def _quota_key(self,slot,images):
        return 'pool:quota:'+slot.group+':'+hashlib.sha256((slot.quota_bucket or self._model(slot,images)).encode()).hexdigest()

    def _raw_limits(self,slot):
        if slot.limits is not None:return slot.limits
        defaults = (30,8000,1000,200000) if slot.provider=='groq' else (15,250000,500,0)
        return tuple(getattr(self.settings,slot.provider+'_'+name,default)
                     for name,default in zip(('rpm','tpm','rpd','tpd'),defaults))

    def _limits(self,slot,images=False):
        # Aliases explicitly sharing a quota bucket never multiply allowance.
        caps=[self._raw_limits(other) for other in self._slots
              if self._supports(other,images) and self._quota_key(other,images)==self._quota_key(slot,images)]
        return tuple(min((cap[i] for cap in caps if cap[i]>0),default=0) for i in range(4))

    @staticmethod
    def _supports(slot,images):
        return ('vlm' if images else 'text') in slot.modes

    @staticmethod
    def _daily_rows(slot,rows,now):
        if slot.provider=='gemini':
            local=datetime.fromtimestamp(now,ZoneInfo('America/Los_Angeles'))
            start=local.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        elif slot.provider in ('openrouter','cloudflare'):
            start=datetime.fromtimestamp(now,ZoneInfo('UTC')).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        else:start=now-86400
        return [row for row in rows if start<=row['at']<=now]

    @staticmethod
    def _daily_reset(slot,rows,now):
        if slot.provider=='gemini':
            local=datetime.fromtimestamp(now,ZoneInfo('America/Los_Angeles'))
            return (local.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)).timestamp()
        if slot.provider in ('openrouter','cloudflare'):
            from trading.agents.external_client import utc_reset
            return utc_reset(now)
        return min((row['at']+86400 for row in rows if row['at']+86400>now),default=None)

    def _cooldown(self, slot, images):
        model=self._model(slot,images)
        if hasattr(slot.router,'retry_at'):return slot.router.retry_at()
        return slot.router.store.get(slot.provider+':retry_at:'+model,0)

    def _admitted_probe(self):
        state=self.store.get('pool:capacity_probe') or {}
        if state.get('used') or not state.get('label') or float(state.get('until') or 0)<=time.time():
            return None
        return state.get('label')

    def _consume_probe_locked(self, label):
        state=self.store.get('pool:capacity_probe') or {}
        if state.get('label')!=label or state.get('used'):
            return
        state['used']=True
        self.store.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',('pool:capacity_probe',json.dumps(state)))

    def _reserve(self, candidates, system, user, max_tokens, images):
        """Choose and lease one group atomically; never hold DB locks during HTTP."""
        now=time.time()
        with self.store.lock,self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            admit=self._admitted_probe()
            choices=[]
            for slot in candidates:
                if not self._supports(slot,images):continue
                probe=slot.label==admit
                if not probe and hasattr(slot.router,'capacity_available') and not slot.router.capacity_available():continue
                if hasattr(slot.router,'request_fits'):
                    slot.router.decision_schema=self.decision_schema
                    if not slot.router.request_fits(system,user,max_tokens,images):continue
                key=self._quota_key(slot,images)
                if not probe and self._cooldown(slot,images)>now:
                    continue
                state=self.store.get(key,dict(rows=[],last_used=0,lease=None))
                rows=[row for row in state['rows'] if now-row['at']<86400]
                recent=[row for row in rows if 0<=now-row['at']<60]
                daily=self._daily_rows(slot,rows,now)
                lease=state.get('lease')
                if not probe and state.get('retry_at',0)>now:
                    continue
                if lease and lease['until']>now:
                    continue
                tokens=self._estimate(slot,system,user,max_tokens,images)
                if not probe and blocked(state.get('provider_reported',{}),tokens,now):continue
                rpm,tpm,rpd,tpd=self._limits(slot,images)
                minute_tokens=sum(row['tokens'] for row in recent)
                daily_tokens=sum(row['tokens'] for row in daily)
                if not probe and ((rpm and len(recent)+1>rpm) or (tpm and minute_tokens+tokens>tpm)
                        or (rpd and len(daily)+1>rpd) or (tpd and daily_tokens+tokens>tpd)):
                    continue
                utilization=max((len(recent)/rpm if rpm else 0),
                    minute_tokens/tpm if tpm else 0,len(daily)/rpd if rpd else 0,
                    daily_tokens/tpd if tpd else 0)
                priority=-1 if probe else int(slot.provider!=getattr(self,'_preferred_provider','groq'))
                choices.append((priority,utilization,state.get('last_used',0),slot.label,slot,key,state,rows,tokens))
            if not choices:
                return None
            _,_,_,_,slot,key,state,rows,tokens=min(choices,key=lambda row:row[:4])
            if slot.label==admit:
                self._consume_probe_locked(slot.label)
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
                if attempt and attempt.get('provider_reported'):state['provider_reported']=copy.deepcopy(attempt['provider_reported'])
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
        self._reload_external()
        now=time.time()
        rows=[]
        seen=set()
        for slot in self._slots:
            for images in (False,True):
                if not self._supports(slot,images):continue
                key=self._quota_key(slot,images)
                if key in seen:
                    continue
                seen.add(key)
                state=self.store.get(key,dict(rows=[],lease=None))
                day=self._daily_rows(slot,state['rows'],now)
                minute=[r for r in state['rows'] if 0<=now-r['at']<60]
                rpm,tpm,rpd,tpd=self._limits(slot,images)
                rows.append(dict(provider=slot.provider,model=self._model(slot,images),quota_group=slot.group[:12],
                    requests_minute=len(minute),tokens_minute_estimate=sum(r['tokens'] for r in minute),
                    requests_daily=len(day),tokens_daily_estimate=sum(r['tokens'] for r in day),
                    daily_window='pacific_calendar_day' if slot.provider=='gemini' else 'utc_calendar_day' if slot.provider in ('openrouter','cloudflare') else 'rolling_24h_conservative',
                    next_daily_reset_at=self._daily_reset(slot,day,now),
                    requests_minute_remaining=max(0,rpm-len(minute)) if rpm else None,
                    tokens_minute_remaining_estimate=max(0,tpm-sum(r['tokens'] for r in minute)) if tpm else None,
                    requests_daily_remaining=max(0,rpd-len(day)) if rpd else None,
                    tokens_daily_remaining_estimate=max(0,tpd-sum(r['tokens'] for r in day)) if tpd else None,
                    cooldown_seconds=max(0,state.get('retry_at',0)-now,self._cooldown(slot,images)-now),
                    in_flight=bool(state.get('lease') and state['lease']['until']>now),
                    provider_reported=copy.deepcopy(state.get('provider_reported',{})),local_estimate=True))
                if slot.provider=='cloudflare':rows[-1].update(slot.router.accounting())
        paid=self.store.get(self._paid_guard_key(),{})
        return dict(groups=rows,quota_window='provider_specific',provider_reported=False,
            paid_fallback_enabled=self.settings.ai_claude_fallback_enabled,
            paid_guard=dict(model=self.settings.anthropic_model,operational_guard=True,
                max_in_flight=1,in_flight=(paid.get('lease') or {}).get('until',0)>now,
                cooldown_seconds=max(0,paid.get('retry_at',0)-now)))

    def _probe_lease(self, slot, images):
        now=time.time()
        key=self._quota_key(slot, images)
        with self.store.lock, self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            state=self.store.get(key, dict(rows=[], lease=None))
            if not isinstance(state, dict):
                state=dict(rows=[], lease=None)
            state.setdefault('rows', [])
            lease=state.get('lease') or {}
            if lease.get('until', 0)>now:
                return None
            lease_id=uuid4().hex
            state['lease']=dict(id=lease_id, until=now+23)
            self.store.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(key, json.dumps(state)))
            return key, lease_id

    def _send_capacity_probe(self, slot, images):
        """One short generation on this free route. Paid Claude is not in the call."""
        router=slot.router
        store=getattr(router, 'store', None)
        cooldown_key=slot.provider+':retry_at:'+self._model(slot, images)
        prior=store.get(cooldown_key, 0) if store is not None else 0
        prior_external=None
        if store is not None:
            store.put(cooldown_key, 0)
            if hasattr(router, 'prefix'):
                prior_external=store.get(router.prefix+'retry_at', 0)
                store.put(router.prefix+'retry_at', 0)
        saved=(getattr(router, 'response_validator', None), getattr(router, 'decision_schema', None), getattr(router, 'deadline', None))
        router.response_validator=lambda payload: None if isinstance(payload, dict) else (_ for _ in ()).throw(ValueError('probe'))
        router.decision_schema=dict(type='object', properties={'action': {'type': 'string'}}, required=['action'])
        router.deadline=time.time()+8
        router._probe_vision=bool(images)
        answered=False
        try:
            result=router.complete_json(system=self._probe_system, user=self._probe_user,
                max_tokens=self._probe_tokens, images=None, capacity_probe=True)
            answered=isinstance(result, dict)
            return answered
        finally:
            router.response_validator, router.decision_schema, router.deadline=saved
            router._probe_vision=False
            if hasattr(router, 'gemini'):
                router.gemini._probe_vision=False
            if store is not None and not answered:
                store.put(cooldown_key, max(store.get(cooldown_key, 0), prior or 0))
                if hasattr(router, 'prefix'):
                    store.put(router.prefix+'retry_at', max(store.get(router.prefix+'retry_at', 0), prior_external or 0))

    def _clear_stale_capacity(self, slot, images):
        key=self._quota_key(slot, images)
        state=self.store.get(key)
        if isinstance(state, dict):
            state['retry_at']=0
            state['provider_reported']={}
            self.store.put(key, state)
        router=slot.router
        store=getattr(router, 'store', None)
        if store is None:
            return
        for flag in (False, bool(images)):
            store.put(slot.provider+':retry_at:'+self._model(slot, flag), 0)
        if hasattr(router, 'prefix'):
            window=time.time()+max(self.settings.decision_ttl_seconds, self.settings.ai_provider_timeout_seconds+15)
            store.put(router.prefix+'retry_at', 0)
            store.put(router.prefix+'probe_admit_until', window)

    def probe_free_capacity(self, *, images=False):
        """Ask one free route whether it answers. Never calls paid Claude.

        A JSON answer, including a valid BEKLE, admits that route for the next
        analysis and does not try another route. The caller spaces and caps probes.
        """
        self._reload_external()
        if not self._allowed():
            return dict(admitted=None, skipped=True)
        now=time.time()
        state=self.store.get('pool:capacity_probe') or {}
        if state.get('label') and not state.get('used') and float(state.get('until') or 0)>now:
            return dict(admitted=state['label'], probed=False)
        slots=[slot for slot in self._slots if self._supports(slot, images)]
        if not slots:
            return dict(admitted=None, skipped=True)
        cursor=int(state.get('cursor') or 0) % len(slots)
        chosen=None
        leased=None
        for offset in range(len(slots)):
            slot=slots[(cursor+offset) % len(slots)]
            leased=self._probe_lease(slot, images)
            if leased is None:
                continue
            chosen=slot
            cursor=(cursor+offset+1) % len(slots)
            break
        if chosen is None:
            return dict(admitted=None, skipped=True)
        answered=False
        try:
            answered=bool(self._send_capacity_probe(chosen, images))
        finally:
            key, lease_id=leased
            attempts=(getattr(chosen.router, 'last_trace', None) or {}).get('attempts') or []
            attempt=attempts[-1] if attempts else {}
            cooldown=self._cooldown(chosen, images) if attempt.get('http_status')==429 else 0
            self._release(key, lease_id, attempt, cooldown)
        saved=dict(cursor=cursor, label=None, used=True, until=0)
        if answered:
            self._clear_stale_capacity(chosen, images)
            saved.update(label=chosen.label, used=False,
                until=now+max(self.settings.decision_ttl_seconds, self.settings.ai_provider_timeout_seconds+15))
        self.store.put('pool:capacity_probe', saved)
        return dict(admitted=chosen.label if answered else None, probed=True)

    def analysis_capacity(self,*,prompt_bytes=18000,max_tokens=1100,images=False,ceiling=8):
        """Advisory current slots, not a quota guarantee; _reserve remains atomic authority.

        Each quota-group/model already allows one in-flight operation. Never count
        two credentials for the same quota group twice or invent extra paid slots.
        """
        self._reload_external()
        now=time.time();free=0;details=[];seen=set();admit=self._admitted_probe()
        for slot in self._slots:
            if not self._supports(slot,images):continue
            key=self._quota_key(slot,images)
            if key in seen:continue
            seen.add(key)
            state=self.store.get(key,dict(rows=[]))
            day=self._daily_rows(slot,state['rows'],now)
            minute=[row for row in state['rows'] if 0<=now-row['at']<60]
            known=sorted(row['tokens'] for row in day[-20:] if row.get('usage_known'))
            observed=known[min(len(known)-1,math.ceil(.9*len(known))-1)] if known else 0
            estimate=max(observed,self._estimate(slot,'','x'*min(200000,max(0,int(prompt_bytes))+5000),max_tokens,[None]*3 if images else None))
            rpm,tpm,rpd,tpd=self._limits(slot,images)
            busy=(state.get('lease') or {}).get('until',0)>now
            cooling=max(state.get('retry_at',0),self._cooldown(slot,images))>now
            external_ok=not hasattr(slot.router,'capacity_available') or slot.router.capacity_available()
            if external_ok and hasattr(slot.router,'request_fits'):
                external_ok=slot.router.request_fits('','x'*min(200000,max(0,int(prompt_bytes))+5000),max_tokens,[None]*3 if images else None)
            opened=slot.label==admit and not busy
            allowed=opened or (external_ok and not busy and not cooling and not blocked(state.get('provider_reported',{}),estimate,now)
                and (not rpm or len(minute)<rpm) and (not tpm or sum(row['tokens'] for row in minute)+estimate<=tpm)
                and (not rpd or len(day)<rpd) and (not tpd or sum(row['tokens'] for row in day)+estimate<=tpd))
            free+=int(allowed)
            details.append(dict(provider=slot.provider,quota_group=slot.group[:12],estimated_request_tokens=estimate,
                available=allowed,reason='probe_admitted' if opened else 'available' if allowed else 'in_flight' if busy else 'cooldown' if cooling else 'quota_or_request_size'))
        paid=self.store.get(self._paid_guard_key(),{})
        paid_available=bool(self.settings.ai_claude_fallback_enabled and self.claude.available
            and (self._paid_budget is None or self._paid_budget.can_spend())
            and paid.get('retry_at',0)<=now and (paid.get('lease') or {}).get('until',0)<=now)
        if self._primary(images)=='anthropic':free=0
        count=min(max(1,int(ceiling)),free+int(paid_available))
        return dict(parallel_candidates=count,free_slots=free,paid_slots=int(paid_available),ceiling=ceiling,
            groups=details,local_estimate=True,reason='capacity_available' if count else 'quota_cooldown_or_in_flight')

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
            for provider in ('groq','gemini','openrouter','cloudflare'):
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

    def _attempt(self,reservation,system,user,max_tokens,images,*,vision_probe=False):
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
            complete = (router.complete_vision_probe if vision_probe and isinstance(router,ProviderRouter)
                        else router.complete_json)
            result=complete(system=system,user=user,max_tokens=max_tokens,images=images)
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

    def _unavailable_routes(self,slots,system,user,max_tokens,images):
        """Explain local admission without sending HTTP or revealing credentials.

        In-flight work is contention, not evidence that a paid model is needed.
        A busy slot only qualifies for waiting if this request can fit its own
        limits and its applicable daily allowance has room for another request.
        """
        now=time.time();rows=[];seen=set()
        for slot in slots:
            key=self._quota_key(slot,images)
            if key in seen:continue
            seen.add(key)
            state=self.store.get(key,dict(rows=[]))
            day=self._daily_rows(slot,state.get('rows',[]),now)
            minute=[row for row in state.get('rows',[]) if 0<=now-row['at']<60]
            tokens=self._estimate(slot,system,user,max_tokens,images)
            rpm,tpm,rpd,tpd=self._limits(slot,images)
            cooling=max(state.get('retry_at',0),self._cooldown(slot,images))>now
            busy=(state.get('lease') or {}).get('until',0)>now
            external_ok=not hasattr(slot.router,'capacity_available') or slot.router.capacity_available()
            external_fits=not hasattr(slot.router,'request_fits') or slot.router.request_fits(system,user,max_tokens,images)
            if not external_ok:reason='external_account_budget_or_cooldown'
            elif not external_fits:reason='external_context_or_neuron_budget'
            elif (tpm and tokens>tpm) or (tpd and tokens>tpd):reason='request_too_large'
            elif rpd and len(day)>=rpd:reason='daily_requests' if slot.provider=='gemini' else 'rolling_daily_requests'
            elif tpd and sum(row['tokens'] for row in day)+tokens>tpd:reason='rolling_daily_tokens'
            elif blocked(state.get('provider_reported',{}),tokens,now):reason='provider_reported_limit'
            elif cooling:reason='provider_cooldown'
            elif busy:reason='free_capacity_in_flight'
            elif rpm and len(minute)>=rpm:reason='minute_requests'
            elif tpm and sum(row['tokens'] for row in minute)+tokens>tpm:reason='minute_tokens'
            else:reason='reservation_race'
            rows.append(dict(provider=slot.provider,model=self._model(slot,images),credential_id=slot.label,
                quota_group=slot.group[:12],status='quota_skipped',phase='routing',error_category=reason,
                generation_requests=0,count_requests=0,estimated_input_tokens=tokens,
                token_budget=tpm))
        return rows

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

    def _generation_requests(self):
        return sum(attempt.get('generation_requests', 0) for attempt in self.last_trace['attempts'])

    def _paid_guard_key(self):
        # Scope to model and shared budget DB, not account/fork or API key.
        return 'pool:paid:guard:'+hashlib.sha256(self.settings.anthropic_model.encode()).hexdigest()

    def _claim_paid(self):
        now=time.time()
        with self.store.lock,self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            key=self._paid_guard_key()
            state=self.store.get(key,{})
            if state.get('retry_at',0)>now:
                return None,'cooldown_skipped',state['retry_at']-now
            if (state.get('lease') or {}).get('until',0)>now:
                return None,'paid_in_flight',0
            ident=uuid4().hex
            # Count and generation are two sequential HTTP operations. This is
            # crash recovery for the local mutex, not a provider rate allowance.
            state['lease']=dict(id=ident,until=now+2*self.settings.ai_provider_timeout_seconds+30)
            self.store.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(key,json.dumps(state)))
            return ident,'',0

    def _release_paid(self,ident,result):
        attempt=self.last_trace['attempts'][-1] if self.last_trace.get('attempts') else {}
        category=attempt.get('error_category','')
        status=attempt.get('http_status')
        delay=0
        if result is None:
            if status==429 or category=='rate_limit':
                raw=attempt.get('retry_after_seconds',60)
                delay=max(1,float(raw)) if isinstance(raw,(int,float)) and math.isfinite(raw) else 60
            elif status in (401,403) or category in ('authentication_or_permission','credit_exhausted') or self.last_status=='provider_credit_exhausted':
                delay=300
            elif (attempt.get('generation_requests',0) or attempt.get('count_requests',0)) and self.last_status not in (
                    'stopped','missing_key','budget_exhausted','budget_or_interval_hold','context_too_large'):
                delay=30
        with self.store.lock,self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            key=self._paid_guard_key()
            state=self.store.get(key,{})
            if (state.get('lease') or {}).get('id')==ident:
                state['lease']=None
                if delay:
                    state['retry_at']=max(state.get('retry_at',0),time.time()+delay)
                self.store.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(key,json.dumps(state)))

    def _paid(self,system,user,max_tokens,images):
        self.last_provider='anthropic'
        self.last_model=self.settings.anthropic_model
        self.last_credential_id='anthropic-1'
        self.last_quota_group='paid-shared'
        if not self._allowed():
            self.last_status='stopped'
            return None
        ident,skip,remaining=self._claim_paid()
        if ident is None:
            self.last_status=skip
            self.last_trace['attempts'].append(dict(provider='anthropic',model=self.last_model,
                credential_id=self.last_credential_id,quota_group=self.last_quota_group,
                status=skip,error_category=skip,generation_requests=0,count_requests=0,
                retry_after_seconds=remaining,operational_guard=True))
            return None
        result=None
        try:
            result=self._paid_request(system,user,max_tokens,images)
            return result
        finally:
            self._release_paid(ident,result)

    def _paid_request(self,system,user,max_tokens,images):
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

    def complete_vision_probe(self, *, system, user, max_tokens, images):
        return self.complete_json(system=system,user=user,max_tokens=max_tokens,images=images,vision_probe=True)

    def complete_json(self, *, system, user, max_tokens=800, images=None, vision_probe=False):
        self._reload_external()
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
        remaining=[slot for slot in self._slots if self._supports(slot,images)] if vision_probe or primary!='anthropic' else []
        retry=None
        waited_for_capacity=False
        generation_limit=1 if vision_probe else self.settings.ai_pool_max_generation_requests
        reserve_paid=(not vision_probe and self.settings.ai_claude_fallback_enabled and self.settings.anthropic_api_key
                      and generation_limit>1)
        free_limit=generation_limit-int(bool(reserve_paid))
        while remaining and self._allowed() and self._generation_requests()<free_limit:
            reservation=self._reserve(remaining,system,user,max_tokens,images)
            if reservation is None:
                self.last_fallback_reason='free_pool_quota_or_cooldown'
                if (not vision_probe and not waited_for_capacity and getattr(self.settings,'ai_pool_max_retry_attempts',1)
                        and self._busy(remaining,images)):
                    waited_for_capacity=True
                    if not self._wait(max(0,min(10,self.settings.ai_pool_retry_seconds))):
                        self.last_status='stopped'
                        return self._finish(None)
                    continue
                unavailable=self._unavailable_routes(remaining,system,user,max_tokens,images)
                self.last_trace['attempts'].extend(unavailable)
                if any(row['error_category']=='free_capacity_in_flight' for row in unavailable):
                    # Five seconds expiring is not consent to convert parallel
                    # free requests into paid traffic. Yield to the scheduler.
                    self.last_status='quota_skipped'
                    self.last_fallback_reason='free_capacity_in_flight'
                    return self._finish(None)
                break
            slot=reservation[0]
            remaining=[other for other in remaining if other is not slot]
            result=self._attempt(reservation,system,user,max_tokens,images,vision_probe=vision_probe)
            if result is not None:
                self.successful_calls+=1
                return self._finish(result)
            attempts=self.last_trace['attempts']
            if attempts and is_model_output_failure(attempts[-1]):
                # Changing a credential cannot fix this response's output contract.
                model=self._model(slot,images)
                remaining=[other for other in remaining if self._model(other,images)!=model]
                if retry is not None and self._model(retry,images)==model:
                    retry=None
                self.last_fallback_reason='invalid_model_output'
            elif retry is None and self._retryable(slot,images):
                retry=slot
        if vision_probe:
            return self._finish(None)
        if not self._allowed():
            self.last_status='stopped'
            return self._finish(None)
        if (retry is not None and getattr(self.settings,'ai_pool_max_retry_attempts',1)
                and self._generation_requests()<free_limit):
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
        if self._generation_requests()>=generation_limit:
            self.last_status='generation_limit_reached'
            return self._finish(None)
        if primary!='anthropic' and not self.settings.ai_claude_fallback_enabled:
            self.last_status='primary_unavailable_fallback_disabled'
            return self._finish(None)
        if primary!='anthropic':
            self.last_fallback_reason=self.last_fallback_reason or 'free_pool_unavailable'
        return self._finish(self._paid(system,user,max_tokens,images))
