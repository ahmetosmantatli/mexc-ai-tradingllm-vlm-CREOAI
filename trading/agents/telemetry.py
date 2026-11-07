"""Safe provider diagnostics: classify errors without persisting raw provider text."""


def safe_error(response):
    status=response.status_code
    try:
        error=response.json().get('error',{})
        if not isinstance(error,dict):
            error={}
    except Exception:
        error={}
    code=error.get('code')
    param=error.get('param')
    known_codes={'json_validate_failed','model_not_found','model_decommissioned','rate_limit_exceeded',
                 'context_length_exceeded','invalid_api_key','invalid_request_error','insufficient_quota'}
    known_params={'response_format','reasoning_format','reasoning_effort','max_tokens',
                  'max_completion_tokens','messages','model','temperature','tools'}
    result=dict(http_status=status,error_code=code if isinstance(code,str) and code in known_codes else None,
                error_param=param if isinstance(param,str) and param in known_params else None,error_category='provider_rejected_request')
    # Raw text is used only for classification; never returned/logged/stored.
    text=str(error.get('message','')).lower()
    if status==429:
        category='rate_limit'
    elif status in (401,403):
        category='authentication_or_permission'
    elif code=='json_validate_failed' or 'failed to generate json' in text:
        category='json_generation_failed'
    elif code in ('model_not_found','model_decommissioned') or 'decommission' in text or 'does not exist' in text:
        category='model_unavailable'
    elif code=='context_length_exceeded' or 'context length' in text or 'too many tokens' in text:
        category='context_limit'
    elif 'reasoning_format' in text:
        category='reasoning_format_conflict'
    elif 'unsupported' in text or 'not supported' in text:
        category='unsupported_parameter_or_format'
    elif status>=500:
        category='provider_server_error'
    else:
        category='provider_rejected_request'
    result['error_category']=category
    return result


def format_trace(trace):
    """Compact private Telegram report for one candidate evaluation."""
    text=(f"Analiz #{trace.get('id','?')[:8]} | {trace.get('symbol','?')} | {trace.get('strategy','?')}\n"
          f"Sonuç: {trace.get('status','pending')} | Model kararı: {trace.get('action','henüz yok')}\n")
    for index,a in enumerate(trace.get('attempts',[]),1):
        text+=f"{index}. {a.get('provider','?')} / {a.get('model','?')} → {a.get('status','?')}"
        if a.get('http_status'):
            text+=f" | HTTP {a['http_status']}"
        text+=f" | {a.get('latency_ms',0):.0f} ms\n"
        if a.get('error_category'):
            text+=f"Neden: {a['error_category']}"
            if a.get('error_code'):text+=f" | Kod: {a['error_code']}"
            if a.get('error_param'):text+=f" | Alan: {a['error_param']}"
            text+='\n'
        text+=f"Üretim isteği: {a.get('generation_requests',0)} | Token sayım isteği: {a.get('count_requests',0)}\n"
        tokens=(a.get('input_tokens'),a.get('output_tokens'))
        text+=f"Girdi/çıktı tokenı: {tokens[0] if tokens[0] is not None else '?'} / {tokens[1] if tokens[1] is not None else '?'}\n"
        cost=a.get('estimated_cost_usd')
        text+=(f"Token bazlı Claude tahmini: ${cost:.6f}\n" if cost is not None else 'İstek ücreti: bilinmiyor; sağlayıcı panelinden doğrulanır.\n')
        if a.get('pending_usd'):
            text+=f"Belirsiz rezervasyon: ${a['pending_usd']:.6f}\n"
    if not trace.get('attempts'):
        text+='Henüz sağlayıcı isteği kaydı yok.\n'
    return text[:3500]
