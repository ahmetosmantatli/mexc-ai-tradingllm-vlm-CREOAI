"""Gemini's OpenAI-compatible endpoint, with complete immutable analysis inputs.

No SDK retries, prompt truncation or image dropping. Diagnostics contain only
allowlisted numeric/status fields; raw provider errors are never retained.
"""
import base64
import json
import logging
import math
import sqlite3
import time
from pathlib import Path

import httpx

from trading.agents.telemetry import safe_error,validation_issues


class GeminiClient:
    ENDPOINT='https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'

    def __init__(self, settings, store, client):
        self.settings, self.store, self.client=settings, store, client
        self.response_validator=None
        self.decision_schema=None
        self.can_call=lambda:True
        self.last_status='not_called'
        self.last_attempt={}
        self.successful_calls=0
        logging.getLogger('httpx').setLevel(logging.WARNING)
        logging.getLogger('httpcore').setLevel(logging.WARNING)

    @property
    def available(self):
        return bool(self.settings.gemini_api_key)

    def _cooldown(self, key, seconds=60):
        self.store.put(key,time.time()+seconds)

    def complete_json(self, *, system, user, max_tokens=800, images=None, request_timeout=None, capacity_probe=False):
        model=self.settings.gemini_vision_model if images or (capacity_probe and getattr(self,'_probe_vision',False)) else self.settings.gemini_text_model
        key='gemini:retry_at:'+model
        started=time.monotonic()
        attempt=dict(provider='gemini',model=model,status='preparing',phase='prepare',
            http_status=None,error_category='',latency_ms=0,generation_requests=0,
            count_requests=0,input_tokens=None,output_tokens=None,
            estimated_cost_usd=None,pending_usd=0.)
        self.last_attempt=attempt
        try:
            if not self.can_call():
                self.last_status='stopped'
                return None
            if not self.available:
                self.last_status='missing_key'
                return None
            if not capacity_probe and time.time()<self.store.get(key,0):
                self.last_status='cooldown_skipped'
                attempt['error_category']='rate_limit_cooldown'
                return None
            content=[{'type':'text','text':user}]
            for path in images or []:
                content.append({'type':'image_url','image_url':{'url':
                    'data:image/png;base64,'+base64.b64encode(Path(path).read_bytes()).decode('ascii')}})
            completion=max_tokens if capacity_probe else max(self.settings.gemini_max_completion_tokens,max_tokens)
            payload=dict(model=model,messages=[{'role':'system','content':system},
                {'role':'user','content':content}],response_format={'type':'json_object'},
                max_tokens=completion)
            if not self.can_call():
                self.last_status='stopped'
                return None
            native=model.startswith('gemma-4-')
            endpoint=self.ENDPOINT
            headers={'Authorization':'Bearer '+self.settings.gemini_api_key}
            if native:
                parts=[{'text':user}]
                for path in images or []:
                    parts.append({'inlineData':{'mimeType':'image/png','data':base64.b64encode(Path(path).read_bytes()).decode('ascii')}})
                payload=dict(systemInstruction={'parts':[{'text':system}]},contents=[{'role':'user','parts':parts}],
                    generationConfig={'maxOutputTokens':completion,
                                      'thinkingConfig':{'thinkingLevel':'minimal'}})
                endpoint='https://generativelanguage.googleapis.com/v1beta/models/'+model+':generateContent'
                headers={'x-goog-api-key':self.settings.gemini_api_key}
            attempt.update(phase='generation',status='requesting',generation_requests=1)
            response=self.client.post(endpoint,headers=headers,json=payload,
                **({'timeout':request_timeout} if request_timeout is not None else {}))
            if response.status_code==429:
                from trading.agents.provider_limits import gemini_retry
                retry,daily=gemini_retry(response)
                attempt.update(retry_after_seconds=retry,daily_quota_exhausted=daily)
                self._cooldown(key,retry)
            response.raise_for_status()
            attempt['http_status']=response.status_code
            data=response.json()
            usage=data.get('usage',{})
            if native:
                usage=data.get('usageMetadata',{})
                output=usage.get('candidatesTokenCount')
                thought=usage.get('thoughtsTokenCount',0)
                usage=dict(prompt_tokens=usage.get('promptTokenCount'),completion_tokens=output+thought if type(output) is int and type(thought) is int else None)
            attempt.update(input_tokens=usage.get('prompt_tokens'),output_tokens=usage.get('completion_tokens'))
            if native:
                selected=data['candidates'][0]
                if selected.get('finishReason')!='STOP':raise ValueError('native output incomplete')
                content=''.join(part.get('text','') for part in selected.get('content',{}).get('parts',[]) if not part.get('thought'))
            else:content=data['choices'][0]['message']['content']
            result=json.loads(content)
            if not isinstance(result,dict):
                raise ValueError('expected JSON object')
            if self.response_validator:
                self.response_validator(result)
            with self.store.lock:
                stats=self.store.get('gemini_usage',dict(successful_calls=0,input_tokens=0,output_tokens=0))
                stats['successful_calls']+=1
                stats['input_tokens']+=int(usage.get('prompt_tokens') or 0)
                stats['output_tokens']+=int(usage.get('completion_tokens') or 0)
                self.store.put('gemini_usage',stats)
            self.successful_calls+=1
            self.last_status='success'
            return result
        except sqlite3.Error:
            self.last_status='persistence_error'
            attempt['error_category']='persistence_error'
            raise
        except httpx.HTTPStatusError as exc:
            attempt.update(**safe_error(exc.response))
            self.last_status='rejected'
            if exc.response.status_code==413 or attempt.get('error_category')=='request_token_budget':
                attempt['error_category']='does-not-fit'
            elif exc.response.status_code!=429:
                self._cooldown(key)
            return None
        except Exception as exc:
            self.last_status='failed'
            attempt['error_category']=('network_timeout' if isinstance(exc,httpx.TimeoutException)
                else 'network_error' if isinstance(exc,httpx.TransportError)
                else 'invalid_output_or_local_validation')
            if attempt['error_category']=='invalid_output_or_local_validation':
                logging.warning('AI doğrulama: %s: %s', type(exc).__name__, str(exc)[:200])
                attempt['validation_issues']=validation_issues(exc,self.decision_schema)
            self._cooldown(key)
            return None
        finally:
            attempt['status']=self.last_status
            attempt['latency_ms']=round((time.monotonic()-started)*1000)
