"""Allowlisted last-response quota metadata; never retain arbitrary headers."""
import math
import re
from time import time


def duration(value):
    if not isinstance(value,str) or len(value)>80:return None
    pieces=re.findall(r'(\d+(?:\.\d+)?)(ms|s|m|h|d)',value)
    if not pieces or ''.join(a+b for a,b in pieces)!=value:return None
    result=sum(float(n)*{'ms':.001,'s':1,'m':60,'h':3600,'d':86400}[unit] for n,unit in pieces)
    return result if math.isfinite(result) and 0<=result<=172800 else None


def read_groq_headers(headers,*,now=None):
    now=time() if now is None else now
    result={}
    for resource in ('requests','tokens'):
        value=headers.get('x-ratelimit-remaining-'+resource,'')
        if isinstance(value,str) and re.fullmatch(r'\d{1,12}',value):result[resource+'_remaining']=int(value)
        delay=duration(headers.get('x-ratelimit-reset-'+resource))
        if delay is not None:result[resource+'_reset_at']=now+delay
    if result:result['observed_at']=now
    return result


def blocked(reported,tokens,now):
    # A stale/missing reset time is not an infinite lock or fresh quota promise.
    for resource,needed in (('requests',1),('tokens',tokens)):
        remaining=reported.get(resource+'_remaining')
        until=reported.get(resource+'_reset_at',reported.get('observed_at',0)+60)
        if type(remaining) is int and remaining<needed and now<until:return True
    return False


def gemini_retry(response,*,now=None):
    """Use structured RetryInfo/QuotaFailure only, never parse free-form messages."""
    from datetime import datetime,timedelta
    from zoneinfo import ZoneInfo
    now=time() if now is None else now
    delay=None
    raw=response.headers.get('retry-after')
    try:
        value=float(raw)
        if math.isfinite(value) and value>=0:delay=value
    except (TypeError,ValueError):pass
    daily=False
    try:
        for detail in response.json().get('error',{}).get('details',[]):
            if detail.get('@type','').endswith('RetryInfo'):
                value=duration(detail.get('retryDelay'))
                if value is not None:delay=max(delay or 0,value)
            if detail.get('@type','').endswith('QuotaFailure'):
                for violation in detail.get('violations',[]):
                    ident=violation.get('quotaId','').lower()
                    metric=violation.get('quotaMetric','').lower()
                    if 'perday' in ident or 'per_day' in metric:daily=True
    except Exception:pass
    if daily:
        local=datetime.fromtimestamp(now,ZoneInfo('America/Los_Angeles'))
        reset=(local.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)).timestamp()
        delay=max(delay or 0,reset-now)
    return max(1,min(delay if delay is not None else 60,172800)),daily
