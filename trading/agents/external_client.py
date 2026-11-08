"""Opt-in free routes. Context is preserved; admission never truncates a request.

Cloudflare accounting below is a local neuron estimate, NOT provider billing.
Only an explicitly confirmed Workers Free plan can use these routes.
"""
import base64
import copy
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import re
import time
from uuid import uuid4

import httpx

from trading.agents.external_catalog import CatalogHTTPError, openrouter_key, openrouter_routes, safe_get
from trading.agents.telemetry import validation_issues


def utc_reset(now):
    return (datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1)).timestamp()


class ExternalClient:
    def __init__(self, settings, route, store, client):
        self.settings, self.route, self.store, self.client = settings, route, store, client
        self.provider = route.provider
        self.can_call = lambda: True
        self.deadline = None
        self._request_deadline = None
        self.response_validator = None
        self.decision_schema = None
        self.on_trace = None
        self.last_trace = {}
        self.last_status = 'not_called'
        self.last_fallback_reason = ''
        # Account scope, never per model/key. No secret or secret hash is stored.
        account = settings.cloudflare_account_id if self.provider == 'cloudflare' else 'account'
        self.prefix = 'external:' + self.provider + ':' + hashlib.sha256(account.encode()).hexdigest()[:16] + ':'

    def _allowed(self):
        return (self.can_call() and (self.deadline is None or time.time() < self.deadline)
                and (self._request_deadline is None or time.time() < self._request_deadline))

    def _timeout(self):
        if not self._allowed():raise ValueError('stopped')
        return max(.01, min(self.settings.ai_provider_timeout_seconds,
                           self.deadline - time.time() if self.deadline else self.settings.ai_provider_timeout_seconds,
                           self._request_deadline-time.time() if self._request_deadline else self.settings.ai_provider_timeout_seconds))

    def _validation_issues(self, exc):
        return validation_issues(exc, self.decision_schema)

    def retry_at(self):
        return max(self.store.get(self.prefix + 'retry_at', 0),
                   self.store.get(self.provider + ':retry_at:' + self.route.model, 0))

    def accounting(self):
        state = self.store.get(self.prefix + 'neurons', {})
        rows = [r for r in state.get('rows', []) if r['reset_at'] > time.time()]
        return dict(neurons_reserved_or_estimated=sum(r['neurons'] for r in rows),
                    neuron_budget=self.settings.cloudflare_daily_neuron_budget,
                    local_estimate=True)

    def capacity_available(self):
        if self.retry_at() > time.time():
            return False
        if self.provider == 'cloudflare':
            return (self.settings.cloudflare_free_plan_confirmed
                    and self.accounting()['neurons_reserved_or_estimated'] < self.settings.cloudflare_daily_neuron_budget)
        cached = self.store.get(self.prefix + 'key', {})
        return not (cached.get('reset_at', 0) > time.time() and cached.get('quota', {}).get('remaining') == 0)

    def request_fits(self, system, user, max_tokens, images):
        completion=min(self.route.max_output,max(max_tokens,self.settings.external_max_completion_tokens))
        schema_bytes=len(json.dumps(self.decision_schema or {}).encode())
        text_bound=len((system+user).encode())+schema_bytes+2048
        if text_bound+completion>self.route.context_length:return False
        if images and not self.route.vision:return False
        if self.provider=='cloudflare':
            bound=self.route.context_length if images else text_bound
            neurons=(bound*self.route.neuron_input+completion*self.route.neuron_output)/1e6
            return self.accounting()['neurons_reserved_or_estimated']+neurons<=self.settings.cloudflare_daily_neuron_budget
        return True

    def _openrouter_admit(self):
        """Refresh account quota and free pricing; failures do not admit generation."""
        now = time.time()
        headers = {'Authorization': 'Bearer ' + self.settings.openrouter_api_key}
        cached = self.store.get(self.prefix + 'key', {})
        if cached.get('observed_at', 0) + 60 <= now or cached.get('reset_at', 0) <= now:
            quota = openrouter_key(self.client, self.settings.openrouter_api_key, timeout=self._timeout())
            cached = dict(observed_at=now, reset_at=utc_reset(now), quota=quota)
            self.store.put(self.prefix + 'key', cached)
        if cached['quota']['remaining'] <= 0:
            self.store.put(self.prefix + 'retry_at', cached['reset_at'])
            raise ValueError('external_daily_quota')
        catalog = self.store.get(self.prefix + 'catalog', {})
        if catalog.get('observed_at', 0) + 300 <= now:
            data = safe_get(self.client, 'https://openrouter.ai/api/v1/models/user', headers, timeout=self._timeout())
            if not isinstance(data.get('data'), list):
                raise ValueError('external_catalog_unavailable')
            routes, _ = openrouter_routes(data['data'])
            catalog = dict(observed_at=now, routes=routes)
            self.store.put(self.prefix + 'catalog', catalog)
        # Reject capability/price changes too, not just disappearance.
        if self.route.model_dump() not in catalog['routes']:
            raise ValueError('external_catalog_changed')
        return cached

    def _reserve_neurons(self, system, user, completion, images):
        # One token per UTF-8 byte is deliberately conservative for text.
        # For images reserve the full context input bound, not a guessed image size.
        input_bound = self.route.context_length if images else len((system + user).encode()) + len(json.dumps(self.decision_schema or {}).encode()) + 2048
        estimate = (input_bound * self.route.neuron_input + completion * self.route.neuron_output) / 1e6
        now = time.time()
        ident = uuid4().hex
        with self.store.lock, self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            key = self.prefix + 'neurons'
            state = self.store.get(key, dict(rows=[]))
            rows = [r for r in state['rows'] if r['reset_at'] > now]
            if sum(r['neurons'] for r in rows) + estimate > self.settings.cloudflare_daily_neuron_budget:
                raise ValueError('external_neuron_budget')
            rows.append(dict(id=ident, neurons=estimate, reset_at=utc_reset(now), usage_known=False))
            self.store.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, json.dumps(dict(rows=rows))))
        return ident, estimate

    def _settle_neurons(self, ident, usage):
        if not all(type(usage.get(k)) is int and usage[k] >= 0 for k in ('prompt_tokens', 'completion_tokens')):
            return  # Keep reservation on unknown outcome, including network failure.
        actual = (usage['prompt_tokens'] * self.route.neuron_input
                  + usage['completion_tokens'] * self.route.neuron_output) / 1e6
        with self.store.lock, self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            key = self.prefix + 'neurons'
            state = self.store.get(key, dict(rows=[]))
            for row in state['rows']:
                if row['id'] == ident:
                    row.update(neurons=actual, usage_known=True)
            self.store.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, json.dumps(state)))

    def _emit(self, attempt):
        self.last_status = attempt['status']
        self.last_trace = dict(attempts=[copy.deepcopy(attempt)])
        if self.on_trace:
            self.on_trace(copy.deepcopy(self.last_trace))

    def complete_json(self, *, system, user, max_tokens=800, images=None):
        started = time.time()
        # Metadata + generation share one bounded operation, not three full timeouts.
        self._request_deadline=started+self.settings.ai_provider_timeout_seconds
        a = dict(provider=self.provider, model=self.route.model, status='requesting', phase='admission',
                 generation_requests=0, count_requests=0)
        neuron_id = None
        try:
            if not self._allowed():
                raise ValueError('stopped')
            if not self.capacity_available():
                raise ValueError('external_capacity')
            if images and not self.route.vision:
                raise ValueError('external_vision_unsupported')
            completion = min(self.route.max_output, max(max_tokens, self.settings.external_max_completion_tokens))
            if not self.request_fits(system,user,max_tokens,images):
                raise ValueError('external_context_or_neuron_budget')
            content = [{'type': 'text', 'text': user}]
            for png in images or []:
                content.append(dict(type='image_url', image_url=dict(url='data:image/png;base64,' + base64.b64encode(png).decode())))
            payload = dict(model=self.route.model, stream=False, max_tokens=completion,
                           messages=[dict(role='system', content=system), dict(role='user', content=content)])
            schema = copy.deepcopy(self.decision_schema or dict(type='object', properties={'action': {'type': 'string'}},
                                                                required=['action'], additionalProperties=False))
            if self.route.format == 'tool':
                payload.update(tools=[dict(type='function', function=dict(name='return_decision', parameters=schema))],
                               tool_choice=dict(type='function', function=dict(name='return_decision')))
            elif self.route.format == 'json_object':
                payload['response_format'] = dict(type='json_object')
            else:
                payload['response_format'] = dict(type='json_schema', json_schema=dict(name='decision', strict=True, schema=schema))
            if self.provider == 'openrouter':
                cached = self._openrouter_admit()
                payload['provider'] = dict(require_parameters=True)
                url = 'https://openrouter.ai/api/v1/chat/completions'
                token = self.settings.openrouter_api_key
            else:
                if not re.fullmatch('[a-fA-F0-9]{32}', self.settings.cloudflare_account_id):
                    raise ValueError('external_invalid_account_id')
                url = f'https://api.cloudflare.com/client/v4/accounts/{self.settings.cloudflare_account_id}/ai/v1/chat/completions'
                token = self.settings.cloudflare_api_token
            if not self._allowed():
                raise ValueError('stopped')
            if self.provider == 'cloudflare':
                neuron_id, estimate = self._reserve_neurons(system, user, completion, images)
                a['neuron_reserved_estimate'] = estimate
            else:
                # Pool serializes the account bucket. Failed generations count too.
                cached = copy.deepcopy(cached)
                cached['quota']['remaining'] = max(0, cached['quota']['remaining'] - 1)
                cached['quota']['used'] += 1
                self.store.put(self.prefix + 'key', cached)
            a.update(phase='generation', generation_requests=1, completion_token_limit=completion)
            self._emit(a)
            response = self.client.post(url, headers={'Authorization': 'Bearer ' + token}, json=payload,
                                        timeout=self._timeout(), follow_redirects=False)
            a['http_status'] = response.status_code
            if response.status_code != 200:
                status = response.status_code
                delay = 5 if status >= 500 else 3600 if status in (400, 404, 422) else 60
                if status == 429:
                    try:
                        raw = response.headers.get('retry-after', '60')
                        delay = float(raw) if raw.replace('.', '', 1).isdigit() else parsedate_to_datetime(raw).timestamp() - time.time()
                        if not math.isfinite(delay): delay = 60
                    except Exception: delay = 60
                delay = max(1, min(86400, delay))
                if status in (401, 402, 403, 429):
                    self.store.put(self.prefix + 'retry_at', time.time() + delay)
                else:
                    self.store.put(self.provider + ':retry_at:' + self.route.model, time.time() + delay)
                a.update(status='rejected', error_category='rate_limit' if status == 429 else 'request_rejected', retry_after_seconds=delay)
                return None
            data = response.json()
            usage = data.get('usage') or {}
            for source, target in [('prompt_tokens', 'input_tokens'), ('completion_tokens', 'output_tokens')]:
                if type(usage.get(source)) is int and usage[source] >= 0:
                    a[target] = usage[source]
            if neuron_id:
                self._settle_neurons(neuron_id, usage)
            if data.get('error'):
                raise ValueError('external_embedded_error')
            choice = data['choices'][0]
            a['output_stop_reason'] = choice.get('finish_reason') if choice.get('finish_reason') in ('stop', 'length', 'tool_calls', 'content_filter') else 'unknown'
            if choice.get('finish_reason') in ('length', 'content_filter'):
                raise ValueError('external_truncated_or_refused')
            message = choice['message']
            if message.get('refusal'):
                raise ValueError('external_truncated_or_refused')
            if self.route.format == 'tool':
                calls = message.get('tool_calls') or []
                if len(calls) != 1 or calls[0]['function']['name'] != 'return_decision':
                    raise ValueError('external_invalid_tool')
                result = json.loads(calls[0]['function']['arguments'])
            else:
                result = json.loads(message['content'])
            if not isinstance(result, dict):
                raise ValueError('external_invalid_object')
            if self.response_validator:
                try: self.response_validator(result)
                except Exception as exc:
                    a['validation_issues'] = self._validation_issues(exc)
                    raise ValueError('external_invalid_decision') from None
            if not self._allowed():
                raise ValueError('stopped')
            a['status'] = 'success'
            return result
        except CatalogHTTPError as exc:
            delay=exc.retry_after if exc.status==429 else 300
            self.store.put(self.prefix+'retry_at',time.time()+delay)
            a.update(status='rejected',http_status=exc.status,error_category='catalog_admission_rejected',retry_after_seconds=delay)
            return None
        except Exception as exc:
            # No provider response/body, key, rejected input, or URL in the journal.
            known = {'stopped', 'external_capacity', 'external_vision_unsupported', 'external_context_or_neuron_budget',
                     'external_daily_quota', 'external_neuron_budget', 'external_catalog_changed', 'external_invalid_account_id',
                     'external_catalog_unavailable', 'external_embedded_error', 'external_truncated_or_refused',
                     'external_invalid_tool', 'external_invalid_object', 'external_invalid_decision'}
            category = str(exc) if type(exc) is ValueError and str(exc) in known else (
                'network_timeout' if isinstance(exc, httpx.TimeoutException) else 'network_error' if isinstance(exc, httpx.TransportError)
                else 'external_invalid_output_or_admission')
            a.update(status='failed', error_category=category)
            if category != 'stopped':
                self.store.put(self.provider + ':retry_at:' + self.route.model, time.time() + 30)
            return None
        finally:
            a['latency_ms'] = round((time.time() - started) * 1000)
            self._emit(a)
