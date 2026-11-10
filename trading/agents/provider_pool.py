"""Credential pool. Each API key is its own account, and each model on that key is its own pool.

Quota-group names do not merge two keys. A request has to fit one key's model TPM;
a second account is the next request, or the same request after the first key's
window or daily count is full. Local estimates are not live quota telemetry.
8K TPM models may receive a compact packet. Wider models receive the full packet.
Valid HOLD never triggers fallback. A local free-route estimate of zero is not
proof those routes are exhausted, and a does-not-fit skip is not exhaustion.
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
from trading.agents.model_limits import COMPACT_TPM, cap_limits, estimate_input_tokens
from trading.agents.provider_limits import blocked
from trading.agents.provider_router import ProviderRouter
from trading.agents.telemetry import is_model_output_failure
from trading.evaluation.context import COMPACT_SYSTEM, compact_decision_packet


GROQ_DAILY_PROBE_KEY = 'pool:groq:daily_probe_at'
GROQ_DAILY_PROBE_INTERVAL = 60


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
    key_id: str = ''


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
                key_id=hashlib.sha256(credential.encode()).hexdigest()
                # The key fingerprint is the pool, not the shared default quota-group name.
                group_id=hashlib.sha256((provider+'\0'+key_id).encode()).hexdigest()
                label=provider+'-'+str(number)
                overrides=dict(groq_api_key='',gemini_api_key='',anthropic_api_key='',
                    ai_primary_provider=provider,ai_vision_primary_provider=provider,
                    ai_claude_fallback_enabled=False,groq_request_token_budget=0)
                overrides[provider+'_api_key']=credential
                child_settings=settings.model_copy(update=overrides)
                prefix='pool:route:'+group_id+':'+label+':'
                router=ProviderRouter(child_settings,None,PrefixStore(store,prefix),self.client)
                self._slots.append(_Slot(provider,label,group_id,router,key_id=key_id))
                for index,route in enumerate(routes):
                    if route.provider!=provider:continue
                    extra=settings.model_copy(update={**overrides,provider+'_text_model':route.model,provider+'_vision_model':route.model})
                    route_label=label+'-model-'+str(index+1)
                    child=ProviderRouter(extra,None,PrefixStore(store,'pool:route:'+group_id+':'+route_label+':'),self.client)
                    self._slots.append(_Slot(provider,route_label,group_id,child,route.model,tuple(route.modes),
                        (route.rpm,route.tpm,route.rpd,route.tpd),route.quota_bucket,key_id))
        if settings.nvidia_api_key:
            from trading.agents.external_catalog import ExternalRoute
            from trading.agents.external_client import ExternalClient
            models = json.loads(settings.nvidia_models)
            if not isinstance(models, list):
                raise ValueError('NVIDIA_MODELS must be a JSON list')
            for model in models:
                route = ExternalRoute(provider='nvidia', **model)
                label = 'nvidia-' + hashlib.sha256(route.model.encode()).hexdigest()[:8]
                if any(slot.label == label for slot in self._slots):
                    raise ValueError('Duplicate NVIDIA model')
                router = ExternalClient(settings, route, self.store, self.client)
                self._slots.append(_Slot('nvidia', label, 'nvidia-account', router, route.model,
                    ('text', 'vlm') if route.vision else ('text',),
                    (settings.nvidia_local_rpm, 0, 0, 0), 'account-all-models'))
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

    def _completion(self, slot, max_tokens):
        return max(max_tokens, getattr(self.settings, slot.provider+'_max_completion_tokens', self.settings.external_max_completion_tokens))

    def _estimate(self, slot, system, user, max_tokens, images):
        return estimate_input_tokens(system, user, images)+self._completion(slot, max_tokens)

    def _pool_id(self, slot, images):
        bucket=slot.quota_bucket or self._model(slot, images)
        identity=slot.key_id or slot.group
        return hashlib.sha256((slot.provider+'\0'+identity+'\0'+bucket).encode()).hexdigest()

    def _group_label(self, slot, images):
        return self._pool_id(slot, images)[:12]

    def _quota_key(self,slot,images):
        return 'pool:quota:'+self._pool_id(slot, images)

    def _raw_limits(self,slot,images=False):
        if slot.limits is not None:
            base=slot.limits
        else:
            defaults = (30,8000,1000,200000) if slot.provider=='groq' else (15,250000,500,0)
            base=tuple(getattr(self.settings,slot.provider+'_'+name,default)
                       for name,default in zip(('rpm','tpm','rpd','tpd'),defaults))
        if slot.provider in ('groq','gemini'):
            return cap_limits(slot.provider, self._model(slot, images), base)
        return base

    def _limits(self,slot,images=False):
        # Aliases on the same key that share a quota bucket never multiply allowance.
        # A second API key is a different pool and is not added into this TPM.
        caps=[self._raw_limits(other, images) for other in self._slots
              if self._supports(other,images) and self._quota_key(other,images)==self._quota_key(slot,images)]
        return tuple(min((cap[i] for cap in caps if cap[i]>0),default=0) for i in range(4))

    def _compact_class(self, slot, images):
        tpm=self._limits(slot, images)[1]
        return bool(tpm and tpm<=COMPACT_TPM and not images)

    def _prepared(self, slot, system, user, max_tokens, images):
        """Full packet for models that can hold it. Compact user text only for 8K TPM."""
        if not self._compact_class(slot, images):
            return system, user
        compact=compact_decision_packet(user)
        if not compact:
            return system, user
        completion=self._completion(slot, max_tokens)
        if estimate_input_tokens(system, compact, images)+completion<=self._limits(slot, images)[1]:
            return system, compact
        if estimate_input_tokens(COMPACT_SYSTEM, compact, images)+completion<=self._limits(slot, images)[1]:
            return COMPACT_SYSTEM, compact
        return system, user

    def _sized(self, slot, system, user, max_tokens, images, prompt_bytes=None):
        """Tokens this slot would actually send, using a compact packet only on 8K models."""
        if prompt_bytes is not None and system=='' and user=='':
            body='x'*min(200000, max(0, int(prompt_bytes))+5000)
            full=self._estimate(slot, '', body, max_tokens, [None]*3 if images else None)
            if self._compact_class(slot, images):
                return min(full, self._completion(slot, max_tokens)+3200)
            return full
        outbound_system, outbound_user=self._prepared(slot, system, user, max_tokens, images)
        return self._estimate(slot, outbound_system, outbound_user, max_tokens, images)

    def _fits_model(self, slot, tokens, images, state=None):
        _rpm, tpm, _rpd, tpd=self._limits(slot, images)
        if tpm and tokens>tpm:
            return False
        if tpd and tokens>tpd:
            return False
        reject=(state or {}).get('reject_tokens')
        if reject and tokens>=reject:
            return False
        return True

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

    def _groq_daily_probe_due(self, now):
        return (self.settings.groq_daily_probe_enabled
                and now >= self.store.get(GROQ_DAILY_PROBE_KEY, 0) + GROQ_DAILY_PROBE_INTERVAL)

    def _reserve(self, candidates, system, user, max_tokens, images):
        """Choose and lease one model pool atomically; never hold DB locks during HTTP."""
        now=time.time()
        preferred_name=getattr(self,'_preferred_provider','groq')+('_vision_model' if images else '_text_model')
        preferred_model=getattr(self.settings, preferred_name, '')
        with self.store.lock,self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            admit=self._admitted_probe()
            daily_probe_due=self._groq_daily_probe_due(now)
            choices=[]
            for slot in candidates:
                if not self._supports(slot,images):continue
                probe=slot.label==admit and not (slot.provider=='groq' and self.settings.groq_daily_probe_enabled)
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
                tokens=self._sized(slot,system,user,max_tokens,images)
                # A short probe admission must not send a packet this model cannot hold.
                if not self._fits_model(slot, tokens, images, state):
                    continue
                if not probe and blocked(state.get('provider_reported',{}),tokens,now):continue
                rpm,tpm,rpd,tpd=self._limits(slot,images)
                minute_tokens=sum(row['tokens'] for row in recent)
                daily_tokens=sum(row['tokens'] for row in daily)
                minute_full = (rpm and len(recent)>=rpm) or (tpm and minute_tokens+tokens>tpm)
                daily_full = (rpd and len(daily)>=rpd) or (tpd and daily_tokens+tokens>tpd)
                daily_probe = bool(not probe and daily_full and slot.provider=='groq' and not images
                                   and daily_probe_due)
                if not probe and (minute_full or (daily_full and not daily_probe)):
                    continue
                utilization=max((len(recent)/rpm if rpm else 0),
                    minute_tokens/tpm if tpm else 0,len(daily)/rpd if rpd else 0,
                    daily_tokens/tpd if tpd else 0)
                priority=-1 if probe else int(slot.provider!=getattr(self,'_preferred_provider','groq'))
                if not probe and slot.provider == 'nvidia':
                    priority = 2
                same_model=0 if self._model(slot,images)==preferred_model else 1
                # A due Groq check uses an existing analysis, even when another provider is available.
                if daily_probe:
                    priority, same_model, utilization = -1, 0, 0
                choices.append((priority,same_model,utilization,state.get('last_used',0),slot.label,slot,key,state,rows,tokens,daily_probe))
            if not choices:
                return None
            _,_,_,_,_,slot,key,state,rows,tokens,daily_probe=min(choices,key=lambda row:row[:5])
            if daily_probe:
                self.store.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',
                                      (GROQ_DAILY_PROBE_KEY,json.dumps(now)))
            if slot.label==admit:
                self._consume_probe_locked(slot.label)
            lease_id=uuid4().hex
            state.update(rows=rows+[dict(id=lease_id,at=now,tokens=tokens)],last_used=now,
                lease=dict(id=lease_id,until=now+self.settings.ai_provider_timeout_seconds+15))
            self.store.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(key,json.dumps(state)))
            return slot,key,lease_id,tokens,daily_probe

    def _release(self,key,lease_id,attempt=None,cooldown=0,*,forget=False,hold_slot=None):
        with self.store.lock,self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            state=self.store.get(key)
            if state and (state.get('lease') or {}).get('id')==lease_id:
                state['lease']=None
                if forget:
                    rejected=next((row.get('tokens') for row in state.get('rows',[]) if row.get('id')==lease_id), None)
                    state['rows']=[row for row in state.get('rows',[]) if row.get('id')!=lease_id]
                    if rejected:
                        previous=state.get('reject_tokens')
                        state['reject_tokens']=rejected if not previous else min(previous, rejected)
                else:
                    if attempt and attempt.get('provider_reported'):state['provider_reported']=copy.deepcopy(attempt['provider_reported'])
                    if cooldown:
                        state['retry_at']=max(state.get('retry_at',0),cooldown)
                    if attempt and all(isinstance(attempt.get(field),int) and attempt[field]>=0 for field in ('input_tokens','output_tokens')):
                        for row in state['rows']:
                            if row.get('id')==lease_id:
                                row['tokens']=attempt['input_tokens']+attempt['output_tokens']
                                row['usage_known']=True
                self.store.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(key,json.dumps(state)))
            if cooldown and hold_slot is not None and hold_slot.key_id:
                for other in self._slots:
                    if other.key_id!=hold_slot.key_id or other.provider!=hold_slot.provider:
                        continue
                    for flag in (False, True):
                        if not self._supports(other, flag):
                            continue
                        other_key=self._quota_key(other, flag)
                        other_state=self.store.get(other_key, dict(rows=[]))
                        if not isinstance(other_state, dict):
                            continue
                        other_state['retry_at']=max(other_state.get('retry_at',0), cooldown)
                        self.store.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(other_key, json.dumps(other_state)))

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
                rows.append(dict(provider=slot.provider,model=self._model(slot,images),quota_group=self._group_label(slot,images),
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

    def probe_free_capacity(self, *, images=False, prompt_bytes=None):
        """Ask one free route whether it answers. Never calls paid Claude.

        A JSON answer, including a valid BEKLE, admits that route for the next
        analysis and does not try another route. The caller spaces and caps probes.
        A model that cannot hold this packet is not a failed probe of a fitting model.
        """
        self._reload_external()
        if not self._allowed():
            return dict(admitted=None, skipped=True)
        now=time.time()
        state=self.store.get('pool:capacity_probe') or {}
        if state.get('label') and not state.get('used') and float(state.get('until') or 0)>now:
            return dict(admitted=state['label'], probed=False)
        slots=[]
        for slot in self._slots:
            if (slot.provider == 'nvidia' or not self._supports(slot, images)
                    or slot.provider == 'groq' and self.settings.groq_daily_probe_enabled):
                continue
            if prompt_bytes is not None:
                tokens=self._sized(slot, '', '', 1100, images, prompt_bytes=prompt_bytes)
                recorded=self.store.get(self._quota_key(slot, images), {})
                if not self._fits_model(slot, tokens, images, recorded if isinstance(recorded, dict) else None):
                    continue
            slots.append(slot)
        if not slots:
            return dict(admitted=None, skipped=True, does_not_fit=prompt_bytes is not None)
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
        oversized=False
        try:
            answered=bool(self._send_capacity_probe(chosen, images))
        finally:
            key, lease_id=leased
            attempts=(getattr(chosen.router, 'last_trace', None) or {}).get('attempts') or []
            attempt=attempts[-1] if attempts else {}
            oversized=attempt.get('http_status')==413 or attempt.get('error_category') in ('does-not-fit','request_token_budget')
            if oversized:
                self._release(key, lease_id, attempt, forget=True)
            else:
                cooldown=self._cooldown(chosen, images) if attempt.get('http_status')==429 else 0
                self._release(key, lease_id, attempt, cooldown, hold_slot=chosen if cooldown else None)
        if oversized:
            self.store.put('pool:capacity_probe', dict(cursor=cursor, label=None, used=True, until=0))
            return dict(admitted=None, probed=True, does_not_fit=True)
        saved=dict(cursor=cursor, label=None, used=True, until=0)
        if answered:
            self._clear_stale_capacity(chosen, images)
            saved.update(label=chosen.label, used=False,
                until=now+max(self.settings.decision_ttl_seconds, self.settings.ai_provider_timeout_seconds+15))
        self.store.put('pool:capacity_probe', saved)
        return dict(admitted=chosen.label if answered else None, probed=True)

    def analysis_capacity(self,*,prompt_bytes=18000,max_tokens=1100,images=False,ceiling=8):
        """Advisory current slots, not a quota guarantee; _reserve remains atomic authority.

        Each key and model already allows one in-flight operation. Two API keys are
        two pools. Do not invent extra paid slots.
        """
        self._reload_external()
        now=time.time();free=0;details=[];seen=set();admit=self._admitted_probe()
        daily_probe_due=self._groq_daily_probe_due(now)
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
            estimate=self._sized(slot,'','',max_tokens,images,prompt_bytes=prompt_bytes)
            if observed and not self._compact_class(slot, images):
                estimate=max(observed, estimate)
            rpm,tpm,rpd,tpd=self._limits(slot,images)
            busy=(state.get('lease') or {}).get('until',0)>now
            cooling=max(state.get('retry_at',0),self._cooldown(slot,images))>now
            external_ok=not hasattr(slot.router,'capacity_available') or slot.router.capacity_available()
            if external_ok and hasattr(slot.router,'request_fits'):
                external_ok=slot.router.request_fits('','x'*min(200000,max(0,int(prompt_bytes))+5000),max_tokens,[None]*3 if images else None)
            fits=self._fits_model(slot, estimate, images, state)
            opened=(fits and slot.label==admit and not busy
                    and not (slot.provider=='groq' and self.settings.groq_daily_probe_enabled))
            minute_ok=(not rpm or len(minute)<rpm) and (not tpm or sum(row['tokens'] for row in minute)+estimate<=tpm)
            daily_ok=(not rpd or len(day)<rpd) and (not tpd or sum(row['tokens'] for row in day)+estimate<=tpd)
            daily_probe = not daily_ok and slot.provider=='groq' and not images and daily_probe_due
            allowed=opened or (fits and external_ok and not busy and not cooling and not blocked(state.get('provider_reported',{}),estimate,now)
                               and minute_ok and (daily_ok or daily_probe))
            free+=int(allowed)
            if allowed and daily_probe and not opened:
                daily_probe_due=False
            if not fits:reason='does-not-fit'
            elif opened:reason='probe_admitted'
            elif allowed:reason='local_daily_probe' if daily_probe else 'available'
            elif busy:reason='in_flight'
            elif cooling or blocked(state.get('provider_reported',{}),estimate,now) or (rpm and len(minute)>=rpm) or (tpm and sum(row['tokens'] for row in minute)+estimate>tpm):reason='window-full'
            elif (rpd and len(day)>=rpd) or (tpd and sum(row['tokens'] for row in day)+estimate>tpd):reason='daily-full'
            else:reason='quota_or_request_size'
            details.append(dict(provider=slot.provider,quota_group=self._group_label(slot, images),estimated_request_tokens=estimate,
                available=allowed,reason=reason))
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
            for provider in ('groq','gemini','openrouter','cloudflare','nvidia'):
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
        slot,key,lease_id,tokens,daily_probe=reservation
        self.last_credential_id=slot.label
        self.last_quota_group=self._group_label(slot, images)
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
                item.update(slot=slot.label,credential_id=slot.label,quota_group=self._group_label(slot, images),quota_estimated_tokens=tokens)
                if daily_probe:
                    item['local_daily_probe'] = True
            self.last_trace['attempts']=self.last_trace['attempts'][:base]+attempts
            self._notify()
        router.on_trace=progress
        outbound_system, outbound_user=self._prepared(slot, system, user, max_tokens, images)
        try:
            complete = (router.complete_vision_probe if vision_probe and isinstance(router,ProviderRouter)
                        else router.complete_json)
            result=complete(system=outbound_system,user=outbound_user,max_tokens=max_tokens,images=images)
            self.last_status=router.last_status
            self.last_fallback_reason=router.last_fallback_reason
            # Subrouter's generic final status is not the useful underlying failure.
            if result is None and router.last_trace.get('attempts'):
                self.last_status=router.last_trace['attempts'][-1].get('status',router.last_status)
            return result
        finally:
            latest=router.last_trace.get('attempts',[])
            attempt=latest[-1] if latest else {}
            oversized=attempt.get('http_status')==413 or attempt.get('error_category') in ('does-not-fit','request_token_budget')
            if oversized:
                attempt['error_category']='does-not-fit'
                if self.last_trace.get('attempts'):
                    self.last_trace['attempts'][-1]['error_category']='does-not-fit'
                self._release(key,lease_id,attempt,forget=slot.provider!='nvidia')
            else:
                cooldown=self._cooldown(slot,images) if attempt.get('http_status')==429 and slot.provider!='nvidia' else 0
                self._release(key,lease_id,attempt,cooldown,hold_slot=slot if cooldown else None)

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
            tokens=self._sized(slot,system,user,max_tokens,images)
            rpm,tpm,rpd,tpd=self._limits(slot,images)
            cooling=max(state.get('retry_at',0),self._cooldown(slot,images))>now
            busy=(state.get('lease') or {}).get('until',0)>now
            external_ok=not hasattr(slot.router,'capacity_available') or slot.router.capacity_available()
            external_fits=not hasattr(slot.router,'request_fits') or slot.router.request_fits(system,user,max_tokens,images)
            minute_tokens=sum(row['tokens'] for row in minute)
            daily_tokens=sum(row['tokens'] for row in day)
            if not external_ok:reason='external_account_budget_or_cooldown'
            elif not external_fits:reason='external_context_or_neuron_budget'
            elif not self._fits_model(slot, tokens, images, state):reason='does-not-fit'
            elif (rpd and len(day)>=rpd) or (tpd and daily_tokens+tokens>tpd):reason='daily-full'
            elif blocked(state.get('provider_reported',{}),tokens,now) or cooling or (rpm and len(minute)>=rpm) or (tpm and minute_tokens+tokens>tpm):reason='window-full'
            elif busy:reason='free_capacity_in_flight'
            else:reason='reservation_race'
            rows.append(dict(provider=slot.provider,model=self._model(slot,images),credential_id=slot.label,
                quota_group=self._group_label(slot, images),status='quota_skipped',phase='routing',error_category=reason,
                generation_requests=0,count_requests=0,estimated_input_tokens=tokens,
                token_budget=tpm,
                requests_daily_remaining=max(0,rpd-len(day)) if rpd else None,
                requests_minute_remaining=max(0,rpm-len(minute)) if rpm else None))
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
