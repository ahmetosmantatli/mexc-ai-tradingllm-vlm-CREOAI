"""Groq-first evaluation with unchanged inputs forwarded to Claude on failure.

Local quota estimates are conservative scheduling hints, not provider tokenizer
counts. Requests never silently lose prompt fields or chart images to fit quota.
"""
import base64
import copy
import json
import logging
import math
import sqlite3
import time
from pathlib import Path

import httpx

from trading.agents.anthropic_client import AnthropicClient
from trading.agents.telemetry import safe_error
from trading.agents.gemini_client import GeminiClient


class ProviderRouter:
    def __init__(self, settings, budget, store, client=None):
        self.settings, self.store = settings, store
        self.claude = AnthropicClient(settings=settings, budget=budget)
        self.budget = None  # Claude's dollar cap does not block Groq.
        self.client = client or httpx.Client(timeout=settings.ai_provider_timeout_seconds)
        self.gemini=GeminiClient(settings,store,self.client)
        logging.getLogger('httpx').setLevel(logging.WARNING)
        self.can_call = lambda: True
        self.response_validator = None
        self.decision_schema = None
        self.last_status = 'not_called'
        self.last_provider = 'none'
        self.last_model = ''
        self.last_fallback_reason = ''
        self.successful_calls = 0
        self.last_trace = {}
        self.on_trace = None
        self.deadline = None

    def close(self):
        self.client.close()

    def _primary(self, images):
        return (self.settings.ai_vision_primary_provider or self.settings.ai_primary_provider) if images else self.settings.ai_primary_provider

    def _allowed(self):
        return self.can_call() and (self.deadline is None or time.time()<self.deadline)

    @property
    def available(self):
        if self._primary(self.settings.analysis_version=='vlm')=='anthropic':
            return self.claude.available
        return bool(self.settings.groq_api_key or self.settings.gemini_api_key or (self.settings.ai_claude_fallback_enabled and self.claude.available))

    def valid(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('expected JSON object')
        if self.response_validator:
            self.response_validator(payload)
        return payload

    def _validation_issues(self, exc):
        from trading.agents.telemetry import validation_issues
        return validation_issues(exc,self.decision_schema)

    def _cooldown(self, model, seconds):
        self.store.put('groq:retry_at:'+model, time.time()+seconds)

    def _reserve_window(self, model, tokens):
        """Share a conservative rolling minute across accounts using this store.

        Reservations remain for 60s even if a request fails: a rejected request
        may still consume provider quota. No sleeping or tight retry loop.
        """
        budget=self.settings.groq_request_token_budget
        if not budget:
            return True
        key='groq:window:'+model
        now=time.time()
        db=self.store.db
        with self.store.lock,db:
            db.execute('BEGIN IMMEDIATE')
            rows=[r for r in self.store.get(key,[]) if now-r['at']<60]
            if len(rows)>=30 or sum(r['tokens'] for r in rows)+tokens>budget:
                return False
            rows.append(dict(at=now,tokens=tokens))
            db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(key,json.dumps(rows)))
            return True

    def _trace(self):
        if self.on_trace:
            self.on_trace(copy.deepcopy(self.last_trace))

    def _finish(self, result):
        self.last_trace.update(status=self.last_status,provider=self.last_provider,
            model=self.last_model,fallback_reason=self.last_fallback_reason)
        with self.store.lock:
            stats=self.store.get('ai_request_stats',{})
            for attempt in self.last_trace['attempts']:
                row=stats.setdefault(attempt['provider'],dict(generation_requests=0,count_requests=0,successes=0,failures=0))
                row['generation_requests']+=attempt.get('generation_requests',0)
                row['count_requests']+=attempt.get('count_requests',0)
                row['successes']+=int(attempt.get('status')=='success')
                row['failures']+=int(attempt.get('status') in ('failed','rejected','invalid_schema','error_or_invalid_output','provider_credit_exhausted','output_truncated','model_refusal'))
            self.store.put('ai_request_stats',stats)
        self._trace()
        return result

    def _groq(self, system, user, max_tokens, images, *, request_timeout=None, capacity_probe=False):
        self.last_provider='groq'
        model=self.settings.groq_vision_model if images or (capacity_probe and getattr(self,'_probe_vision',False)) else self.settings.groq_text_model
        self.last_model=model
        attempt=dict(provider='groq',model=model,status='preparing',generation_requests=0,count_requests=0,
            latency_ms=0,input_tokens=None,output_tokens=None,estimated_cost_usd=None)
        self.last_trace['attempts'].append(attempt)
        if not capacity_probe and time.time()<self.store.get('groq:retry_at:'+model,0):
            self.last_fallback_reason='groq_cooldown'
            attempt['status']='cooldown_skipped'
            return None
        completion=max_tokens if capacity_probe else max(self.settings.groq_max_completion_tokens,max_tokens)
        estimated_input=math.ceil(len((system+user).encode('utf-8'))/3)+512+2048*len(images or [])
        total=estimated_input+completion
        budget=self.settings.groq_request_token_budget
        attempt.update(estimated_input_tokens=estimated_input,token_budget=budget,
                       completion_token_limit=completion)
        if budget and total>budget:
            attempt.update(status='quota_skipped',error_category='request_token_budget')
            self.last_fallback_reason='groq_request_token_budget'
            return None
        started=time.monotonic()
        try:
            content=[{'type':'text','text':user}]
            for path in images or []:
                content.append({'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(Path(path).read_bytes()).decode('ascii')}})
            payload=dict(model=model,messages=[{'role':'system','content':system},{'role':'user','content':content}],
                         response_format={'type':'json_object'},max_completion_tokens=completion)
            if model.startswith('openai/gpt-oss-'):
                payload.update(include_reasoning=False,reasoning_effort='low')
            if len(json.dumps(payload).encode())>19_000_000:
                raise ValueError('request too large')
            if not self._allowed():
                self.last_status='stopped'
                attempt['status']='stopped'
                return None
            if not self._reserve_window(model,total):
                attempt.update(status='quota_skipped',error_category='shared_minute_token_budget')
                self.last_fallback_reason='groq_shared_minute_token_budget'
                return None
            attempt.update(status='requesting',generation_requests=1)
            self._trace()
            response=self.client.post('https://api.groq.com/openai/v1/chat/completions',
                headers={'Authorization':'Bearer '+self.settings.groq_api_key},json=payload,
                **({'timeout':request_timeout} if request_timeout is not None else {}))
            from trading.agents.provider_limits import read_groq_headers
            attempt['provider_reported']=read_groq_headers(response.headers)
            if response.status_code==429:
                try:
                    retry=float(response.headers.get('retry-after','60'))
                    if not math.isfinite(retry):retry=60
                except ValueError:
                    retry=60
                self._cooldown(model,max(1,min(retry,86400)))
            response.raise_for_status()
            data=response.json()
            usage=data.get('usage',{})
            attempt.update(http_status=response.status_code,input_tokens=usage.get('prompt_tokens'),output_tokens=usage.get('completion_tokens'))
            result=self.valid(json.loads(data['choices'][0]['message']['content']))
            with self.store.lock:
                stats=self.store.get('groq_usage',dict(successful_calls=0,input_tokens=0,output_tokens=0))
                stats['successful_calls']+=1
                stats['input_tokens']+=int(usage.get('prompt_tokens',0))
                stats['output_tokens']+=int(usage.get('completion_tokens',0))
                self.store.put('groq_usage',stats)
            self.successful_calls+=1
            self.last_status='success'
            attempt['status']='success'
            return result
        except sqlite3.Error:
            raise
        except httpx.HTTPStatusError as exc:
            attempt.update(status='rejected',**safe_error(exc.response))
            self.last_fallback_reason='groq_http_'+str(exc.response.status_code)
            if exc.response.status_code==413 or attempt.get('error_category')=='request_token_budget':
                # The packet does not fit this model. That is not a window hold.
                attempt['error_category']='does-not-fit'
            elif exc.response.status_code!=429:
                self._cooldown(model,60)
        except Exception as exc:
            attempt.update(status='failed',error_category='network_timeout' if isinstance(exc,httpx.TimeoutException) else 'network_error' if isinstance(exc,httpx.TransportError) else 'invalid_output_or_local_validation')
            self.last_fallback_reason='groq_error_or_invalid_output'
            self._cooldown(model,60)
        finally:
            attempt['latency_ms']=(time.monotonic()-started)*1000
            self._trace()
        return None

    def complete_vision_probe(self, *, system, user, max_tokens, images):
        return self.complete_json(system=system,user=user,max_tokens=max_tokens,images=images,vision_probe=True)

    def complete_json(self, *, system, user, max_tokens=800, images=None, vision_probe=False, capacity_probe=False):
        self.last_trace={'attempts':[],'status':'pending'}
        self.last_fallback_reason=''
        self.last_provider='none'
        self.last_model=''
        self.last_status='not_called'
        if not self._allowed():
            self.last_status='stopped'
            return self._finish(None)
        self.gemini._probe_vision=bool(capacity_probe and getattr(self,'_probe_vision',False))
        primary=self._primary(images or self.gemini._probe_vision)
        if primary=='anthropic' and (vision_probe or capacity_probe):
            primary='gemini'
        order=[] if primary=='anthropic' else [primary, 'gemini' if primary=='groq' else 'groq']
        request_timeout = max(.01, self.deadline-time.time()) if (vision_probe or capacity_probe) and self.deadline is not None else None
        for provider in order:
            if not self._allowed():
                self.last_status='stopped'
                return self._finish(None)
            if provider=='groq' and self.settings.groq_api_key:
                result=self._groq(system,user,max_tokens,images,request_timeout=request_timeout,capacity_probe=capacity_probe)
            elif provider=='gemini' and self.settings.gemini_api_key:
                self.last_provider='gemini'
                self.last_model=self.settings.gemini_vision_model if images or self.gemini._probe_vision else self.settings.gemini_text_model
                self.gemini.can_call=self._allowed
                self.gemini.response_validator=self.response_validator
                self.gemini.decision_schema=copy.deepcopy(self.decision_schema)
                self.last_trace['attempts'].append(dict(provider='gemini',model=self.last_model,status='requesting',generation_requests=0,count_requests=0,latency_ms=0))
                self._trace()
                result=self.gemini.complete_json(system=system,user=user,max_tokens=max_tokens,images=images,
                    capacity_probe=capacity_probe,
                    **({'request_timeout':request_timeout} if request_timeout is not None else {}))
                self.last_trace['attempts'][-1]=copy.deepcopy(self.gemini.last_attempt)
                self.last_status=self.gemini.last_status
                if result is not None:
                    self.successful_calls+=1
                else:
                    attempt=self.gemini.last_attempt
                    self.last_fallback_reason='gemini_http_'+str(attempt['http_status']) if attempt.get('http_status') else 'gemini_'+self.last_status
            else:
                continue
            if result is not None:
                return self._finish(result)
            if self.last_status=='stopped' or (vision_probe or capacity_probe) and any(a.get('generation_requests') for a in self.last_trace['attempts']):
                return self._finish(None)
        if vision_probe or capacity_probe:
            return self._finish(None)
        if not self._allowed():
            self.last_status='stopped'
            return self._finish(None)
        if primary!='anthropic' and not self.settings.ai_claude_fallback_enabled:
            self.last_status='primary_unavailable_fallback_disabled'
            return self._finish(None)
        self.last_provider='anthropic'
        self.last_model=self.settings.anthropic_model
        self.claude.can_call=self._allowed
        self.claude.decision_schema=copy.deepcopy(self.decision_schema)
        self.last_trace['attempts'].append(dict(provider='anthropic',model=self.last_model,status='requesting',generation_requests=0,count_requests=0,latency_ms=0))
        self._trace()
        result=self.claude.complete_json(system=system,user=user,max_tokens=max_tokens,images=images)
        self.last_trace['attempts'][-1]=copy.deepcopy(getattr(self.claude,'last_attempt',None) or dict(provider='anthropic',model=self.last_model,status=self.claude.last_status,generation_requests=0,count_requests=0,latency_ms=0))
        self.last_status=self.claude.last_status
        if result is not None:
            try:
                result=self.valid(result)
            except Exception as exc:
                self.last_status='error_or_invalid_output'
                self.last_trace['attempts'][-1].update(status='invalid_schema',error_category='invalid_decision_schema',
                    validation_issues=self._validation_issues(exc))
                return self._finish(None)
            self.successful_calls+=1
        return self._finish(result)
