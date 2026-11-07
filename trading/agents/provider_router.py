"""Groq-first evaluation with the identical request forwarded to Claude on failure.

No prompt truncation or image dropping. Provider replies are validated before
fallback. A stop guard is checked before each new network request.
"""
import base64
import copy
import json
import logging
import sqlite3
import time
from pathlib import Path

import httpx

from trading.agents.anthropic_client import AnthropicClient
from trading.agents.telemetry import safe_error


class ProviderRouter:
    def __init__(self, settings, budget, store, client=None):
        self.settings, self.store = settings, store
        self.claude = AnthropicClient(settings=settings, budget=budget)
        # Claude budget must not block a Groq attempt.
        self.budget = None
        self.client = client or httpx.Client(timeout=15)
        logging.getLogger('httpx').setLevel(logging.WARNING)
        self.can_call = lambda: True
        self.response_validator = None
        self.last_status = 'not_called'
        self.last_provider = 'none'
        self.last_model = ''
        self.last_fallback_reason = ''
        self.successful_calls = 0
        self.last_trace = {}
        self.on_trace = None

    def close(self):
        self.client.close()

    @property
    def available(self):
        if self.settings.ai_primary_provider=='anthropic':
            return self.claude.available
        return bool(self.settings.groq_api_key or (self.settings.ai_claude_fallback_enabled and self.claude.available))

    def valid(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('expected JSON object')
        if self.response_validator:
            self.response_validator(payload)
        return payload

    def _cooldown(self, seconds):
        self.store.put('groq:retry_at', time.time()+seconds)

    def _trace(self):
        if self.on_trace:
            self.on_trace(copy.deepcopy(self.last_trace))

    def _finish(self, result):
        self.last_trace['status']=self.last_status
        self.last_trace['provider']=self.last_provider
        self.last_trace['model']=self.last_model
        self.last_trace['fallback_reason']=self.last_fallback_reason
        with self.store.lock:
            stats=self.store.get('ai_request_stats',{})
            for attempt in self.last_trace['attempts']:
                provider=attempt['provider']
                row=stats.setdefault(provider,dict(generation_requests=0,count_requests=0,successes=0,failures=0))
                row['generation_requests']+=attempt.get('generation_requests',0)
                row['count_requests']+=attempt.get('count_requests',0)
                row['successes']+=int(attempt.get('status')=='success')
                row['failures']+=int(attempt.get('status') in ('failed','rejected','invalid_schema','error_or_invalid_output','provider_credit_exhausted'))
            self.store.put('ai_request_stats',stats)
        self._trace()
        return result

    def complete_json(self, *, system, user, max_tokens=800, images=None):
        self.last_trace={'attempts':[], 'status':'pending'}
        self.last_fallback_reason = ''
        if not self.can_call():
            self.last_status = 'stopped'
            return self._finish(None)
        if self.settings.ai_primary_provider=='groq' and self.settings.groq_api_key:
            if time.time() < self.store.get('groq:retry_at', 0):
                self.last_fallback_reason = 'groq_cooldown'
                self.last_trace['attempts'].append(dict(provider='groq',model=self.settings.groq_vision_model if images else self.settings.groq_text_model,status='cooldown_skipped',generation_requests=0,count_requests=0,latency_ms=0))
            else:
                self.last_provider = 'groq'
                self.last_model = self.settings.groq_vision_model if images else self.settings.groq_text_model
                attempt=dict(provider='groq',model=self.last_model,status='preparing',generation_requests=0,count_requests=0,latency_ms=0,input_tokens=None,output_tokens=None,estimated_cost_usd=None)
                self.last_trace['attempts'].append(attempt)
                started=time.monotonic()
                try:
                    content = [{'type':'text', 'text':user}]
                    for path in images or []:
                        raw = Path(path).read_bytes()
                        content.append({'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(raw).decode('ascii')}})
                    payload = dict(model=self.last_model,
                        messages=[{'role':'system','content':system},{'role':'user','content':content}],
                        response_format={'type':'json_object'}, max_completion_tokens=max(4096,max_tokens))
                    if self.last_model.startswith('openai/gpt-oss-'):
                        payload.update(include_reasoning=False,reasoning_effort='low')
                    # Reject overlarge envelopes rather than dropping context.
                    if len(json.dumps(payload).encode()) > 19_000_000:
                        raise ValueError('request too large')
                    if not self.can_call():
                        self.last_status='stopped'
                        return self._finish(None)
                    attempt.update(status='requesting',generation_requests=1)
                    self._trace()
                    response=self.client.post('https://api.groq.com/openai/v1/chat/completions',
                        headers={'Authorization':'Bearer '+self.settings.groq_api_key},json=payload)
                    if response.status_code==429:
                        try:
                            retry=float(response.headers.get('retry-after','60'))
                        except ValueError:
                            retry=60
                        self._cooldown(max(1,min(retry,86400)))
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
                    attempt.update(status='success',latency_ms=(time.monotonic()-started)*1000)
                    return self._finish(result)
                except sqlite3.Error:
                    raise
                except httpx.HTTPStatusError as exc:
                    attempt.update(status='rejected',**safe_error(exc.response))
                    self.last_fallback_reason='groq_http_'+str(exc.response.status_code)
                    if exc.response.status_code!=429:
                        self._cooldown(60)
                except Exception as exc:
                    attempt.update(status='failed',error_category='network_timeout' if isinstance(exc,httpx.TimeoutException) else 'network_error' if isinstance(exc,httpx.TransportError) else 'invalid_output_or_local_validation')
                    self.last_fallback_reason='groq_error_or_invalid_output'
                    self._cooldown(60)
                finally:
                    attempt['latency_ms']=(time.monotonic()-started)*1000
                    self._trace()
        if self.settings.ai_primary_provider=='groq' and not self.settings.ai_claude_fallback_enabled:
            self.last_status='primary_unavailable_fallback_disabled'
            return self._finish(None)
        if not self.can_call():
            self.last_status='stopped'
            return self._finish(None)
        self.last_provider='anthropic'
        self.last_model=self.settings.anthropic_model
        self.claude.can_call=self.can_call
        self.last_trace['attempts'].append(dict(provider='anthropic',model=self.last_model,status='requesting',generation_requests=0,count_requests=0,latency_ms=0))
        self._trace()
        result=self.claude.complete_json(system=system,user=user,max_tokens=max_tokens,images=images)
        self.last_trace['attempts'][-1]=copy.deepcopy(getattr(self.claude,'last_attempt',None) or dict(provider='anthropic',model=self.last_model,status=self.claude.last_status,generation_requests=0,count_requests=0,latency_ms=0))
        self.last_status=self.claude.last_status
        if result is not None:
            try:
                result=self.valid(result)
            except Exception:
                self.last_status='error_or_invalid_output'
                self.last_trace['attempts'][-1].update(status='invalid_schema',error_category='invalid_decision_schema')
                return self._finish(None)
            self.successful_calls+=1
        return self._finish(result)
