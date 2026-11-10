"""Capability discovery, not trading quality certification or access guarantees."""
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import time
import math
from email.utils import parsedate_to_datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

MIN_CONTEXT=32768
# Free-plan eligible models and published neuron rates, checked 2026-11-08.
# Unknown Cloudflare models are reported, never auto-certified from a name.
CF_MODELS={
    '@cf/zai-org/glm-4.7-flash':(131072,False,5500.,36400.),
    '@cf/google/gemma-4-26b-a4b-it':(256000,True,9091.,27273.),
    '@cf/nvidia/nemotron-3-120b-a12b':(131072,False,45455.,136364.),
}


class CatalogHTTPError(ValueError):
    def __init__(self,response):
        self.status=response.status_code
        try:
            raw=response.headers.get('retry-after','60')
            delay=float(raw) if raw.replace('.','',1).isdigit() else parsedate_to_datetime(raw).timestamp()-time.time()
            self.retry_after=max(1,min(86400,delay)) if math.isfinite(delay) else 60
        except Exception:self.retry_after=60
        super().__init__('catalog_http_error')


class ExternalRoute(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    provider: Literal['openrouter','cloudflare','nvidia']
    model: str=Field(pattern=r'^[A-Za-z0-9@._/:-]{1,150}$')
    context_length: int=Field(ge=MIN_CONTEXT,le=10000000)
    max_output: int=Field(ge=700,le=1000000)
    vision: bool=False
    format: Literal['json_schema','json_object','tool']
    neuron_input: float=Field(default=0,ge=0)
    neuron_output: float=Field(default=0,ge=0)


def zero_pricing(pricing):
    if not isinstance(pricing,dict) or not all(k in pricing for k in ('prompt','completion')):return False
    try:
        return all(Decimal(str(v)).is_finite() and Decimal(str(v))==0 for v in pricing.values())
    except (InvalidOperation,ValueError,TypeError):return False


def openrouter_routes(models):
    accepted=[];excluded=[]
    for row in models:
        if not isinstance(row,dict):continue
        ident=row.get('id','')
        if not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z0-9._/:-]{1,150}',ident):continue
        reason=None
        arch=row.get('architecture') or {}
        params=row.get('supported_parameters') or []
        outputs=arch.get('output_modalities') or []
        inputs=arch.get('input_modalities') or []
        context=row.get('context_length')
        output=(row.get('top_provider') or {}).get('max_completion_tokens')
        if not ident.endswith(':free') or not zero_pricing(row.get('pricing')):reason='not_verified_zero_price_free_variant'
        elif any(term in ident.lower() for term in ('prompt-guard','safeguard','moderation','embedding','rerank')):reason='specialized_non_decision_model'
        elif outputs!=['text'] or 'text' not in inputs:reason='not_text_decision_output'
        elif row.get('endpoints') and '/api/v1/chat/completions' not in row['endpoints']:reason='not_chat_endpoint'
        elif type(context) is not int or context<MIN_CONTEXT:reason='context_too_small_or_unknown'
        elif type(output) is not int or output<700:reason='output_limit_unknown_or_too_small'
        elif not any(p in params for p in ('structured_outputs','response_format','tools')):reason='no_json_or_tool_capability'
        if reason:
            excluded.append(dict(model=ident,reason=reason));continue
        fmt='json_schema' if 'structured_outputs' in params else 'json_object' if 'response_format' in params else 'tool'
        accepted.append(ExternalRoute(provider='openrouter',model=ident,context_length=context,
            max_output=min(output,8192),vision='image' in inputs,format=fmt).model_dump())
    # Prefer native structured output and vision; use deterministic model IDs.
    accepted.sort(key=lambda r:(r['format']!='json_schema',not r['vision'],r['model']))
    return accepted,excluded


def cloudflare_routes(models):
    accepted=[];excluded=[]
    for row in models:
        ident=row.get('name') if isinstance(row,dict) else None
        if not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z0-9@._/:-]{1,150}',ident):continue
        if ident not in CF_MODELS:
            excluded.append(dict(model=ident,reason='free_plan_capability_and_neuron_rates_not_verified'));continue
        context,vision,inp,out=CF_MODELS[ident]
        accepted.append(ExternalRoute(provider='cloudflare',model=ident,context_length=context,
            max_output=8192,vision=vision,format='json_schema',neuron_input=inp,neuron_output=out).model_dump())
    accepted.sort(key=lambda r:(r['neuron_input'],r['model']))
    return accepted,excluded


def safe_get(client,url,headers,params=None,timeout=15):
    response=client.get(url,headers=headers,params=params,timeout=timeout,follow_redirects=False)
    if response.status_code!=200:raise CatalogHTTPError(response)
    result=response.json()
    if not isinstance(result,dict):raise ValueError('catalog_schema')
    return result


def openrouter_key(client,key,timeout=15):
    data=safe_get(client,'https://openrouter.ai/api/v1/key',{'Authorization':'Bearer '+key},timeout=timeout).get('data',{})
    quota=data.get('free_model_daily_requests') or {}
    if not all(type(quota.get(k)) is int and quota[k]>=0 for k in ('used','remaining','limit')):
        raise ValueError('openrouter_daily_quota_unknown')
    if quota['remaining']>quota['limit']:raise ValueError('openrouter_daily_quota_unknown')
    return {k:quota[k] for k in ('used','remaining','limit')}


def discover(settings,client):
    routes=[];reports=[]
    if settings.openrouter_api_key:
        try:
            headers={'Authorization':'Bearer '+settings.openrouter_api_key}
            quota=openrouter_key(client,settings.openrouter_api_key)
            data=safe_get(client,'https://openrouter.ai/api/v1/models/user',headers)
            models=data.get('data')
            if not isinstance(models,list):raise ValueError('catalog_schema')
            good,bad=openrouter_routes(models);routes+=good
            reports.append(dict(provider='openrouter',catalog_models=len(models),eligible=len(good),excluded=bad,
                daily_quota=quota,quota_scope='account_all_free_variants',access='catalog_eligible_not_generation_tested'))
        except Exception as exc:
            reports.append(dict(provider='openrouter',status='discovery_failed',error_type=type(exc).__name__,
                http_status=exc.status if isinstance(exc,CatalogHTTPError) else None))
    if settings.cloudflare_api_token:
        if not re.fullmatch('[a-fA-F0-9]{32}',settings.cloudflare_account_id):
            reports.append(dict(provider='cloudflare',status='invalid_account_id'))
        else:
            try:
                models=[];complete=False
                for page in range(1,21):
                    data=safe_get(client,f'https://api.cloudflare.com/client/v4/accounts/{settings.cloudflare_account_id}/ai/models/search',
                        {'Authorization':'Bearer '+settings.cloudflare_api_token},dict(page=page,per_page=100))
                    batch=data.get('result')
                    if data.get('success') is not True or not isinstance(batch,list):raise ValueError('catalog_schema')
                    models.extend(batch)
                    info=data.get('result_info') or {}
                    if len(batch)<100 or type(info.get('total_pages')) is int and page>=info['total_pages']:
                        complete=True;break
                good,bad=cloudflare_routes(models)
                if settings.cloudflare_free_plan_confirmed:routes+=good
                reports.append(dict(provider='cloudflare',catalog_models=len(models),complete=complete,eligible=len(good),excluded=bad,
                    free_plan_confirmed=settings.cloudflare_free_plan_confirmed,
                    enabled=bool(settings.cloudflare_free_plan_confirmed),
                    quota_scope='account_daily_neurons',quota='local_estimate_only_not_provider_balance',
                    access='catalog_eligible_not_generation_tested'))
            except Exception as exc:
                reports.append(dict(provider='cloudflare',status='discovery_failed',error_type=type(exc).__name__,
                    http_status=exc.status if isinstance(exc,CatalogHTTPError) else None))
    if len(routes)>64:
        reports.append(dict(status='manifest_limit',excluded_models=[r['model'] for r in routes[64:]]))
        routes=routes[:64]
    return dict(version=1,observed_at=time.time(),routes=routes,reports=reports)


def load_routes(path,now=None):
    now=time.time() if now is None else now
    try:
        data=json.loads(Path(path).read_text())
        if data.get('version')!=1 or not 0<=now-data['observed_at']<86400 or len(data['routes'])>64:return []
        result=[ExternalRoute.model_validate(r) for r in data['routes']]
        for route in result:
            if route.provider=='nvidia':return []
            if route.provider=='openrouter' and not route.model.endswith(':free'):return []
            if route.provider=='cloudflare':
                expected=CF_MODELS.get(route.model)
                if not expected or (route.context_length,route.vision,route.neuron_input,route.neuron_output)!=expected:return []
        return result
    except Exception:return []
