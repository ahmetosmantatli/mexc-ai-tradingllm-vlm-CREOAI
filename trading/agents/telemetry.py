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
    if status==413 and (code=='rate_limit_exceeded' or 'token' in text):
        category='request_token_budget'
    elif status==429 or code=='rate_limit_exceeded':
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


def human_reason(reason):
    """Translate runtime codes without exposing opaque diagnostics in normal chat."""
    known={
        'HOLD_EVALUATION_INTERVAL':'Bir sonraki analiz zamanı bekleniyor; şu anda yeni AI isteği gönderilmiyor.',
        'HOLD_SCAN_INTERVAL':'Yeni piyasa taraması bekleniyor.',
        'HOLD_TELEGRAM_PAUSED':'Yeni işlem arayışı duraklatıldı.',
        'HOLD_NO_QUALIFIED_SIGNAL':'Henüz kurallara uyan yeni işlem adayı yok.',
        'HOLD_CLAUDE_DECISION_OR_FAILURE':'Son analiz işlem açılmasına izin vermedi; son analiz sonucunu kontrol et.',
        'HOLD_AI_NOT_CONFIGURED':'AI sağlayıcısı ayarlı değil.',
        'HOLD_CLAUDE_NOT_CONFIGURED':'AI sağlayıcısı ayarlı değil.',
        'HOLD_RISK_REJECT':'Öneri risk kontrolünden geçmedi.',
        'HOLD_PRICE_DRIFT':'Analiz sırasında fiyat fazla değişti; öneri kullanılmadı.',
        'HOLD_DECISION_EXPIRED':'Analiz sonucu eskidi; işlem açılmadı.',
        'HOLD_SETTINGS_CHANGED':'Analiz sürerken ayarlar değişti veya bot durduruldu; işlem açılmadı.',
        'HOLD_RISK_CHANGED':'Güncel kasa veya açık risk değişti; öneri kullanılmadı.',
        'HOLD_POSITION_LIMIT':'Açık işlem sınırı dolu; yeni giriş bekliyor.',
        'HOLD_LOSS_CIRCUIT_BREAKER':'Kayıp sınırına ulaşıldı; yeni girişler durdu.',
        'HOLD_ACCOUNT_UNAVAILABLE':'MEXC hesabı okunamadı; yeni giriş bekliyor.',
        'HOLD_WAITING_FOR_FUNDS':'İşlem için kullanılabilir kasa bekleniyor.',
        'PAPER_OPENED':'Sanal işlem açıldı. Açık işlemler panelinden takip edebilirsin.',
        'HOLD_KILL_SWITCH':'Sunucudaki durdurma anahtarı etkin; yeni giriş yok.',
    }
    if reason in known:
        return known[reason]
    if str(reason).startswith('HOLD_'):
        return 'Yeni işlem açılmadı; kontrol veya veri bekleniyor. Ayrıntı: Son analiz / AI istekleri.'
    return 'İşlem ve piyasa takibi sürüyor; ayrıntıyı Son analiz ekranından görebilirsin.'


def trace_outcome(trace):
    execution=trace.get('execution_status')
    if execution=='opened':
        return 'Sanal işlem açıldı'
    if execution in ('rejected','not_opened'):
        if trace.get('status')=='invalid_or_unavailable_model':
            return 'İşlem açılmadı — analiz başarısız'
        if trace.get('action')=='watch':
            return 'İşlem açılmadı — aday izlemeye alındı'
        if trace.get('action')=='hold':
            return 'İşlem açılmadı — AI beklemeyi seçti'
        return 'İşlem açılmadı'
    if trace.get('status')=='invalid_or_unavailable_model':
        return 'İşlem açılmadı — analiz başarısız'
    if trace.get('action')=='trade':
        return 'AI işlem önerdi; risk kontrolü bekleniyor'
    if trace.get('action')=='watch':
        return 'İşlem açılmadı — aday izlemeye alındı'
    if trace.get('action')=='hold':
        return 'İşlem açılmadı — AI beklemeyi seçti'
    return 'Analiz sürüyor — henüz işlem açılmadı'


def provider_result(attempt):
    status=attempt.get('status')
    category=attempt.get('error_category')
    if status=='success':
        return 'yanıt alındı'
    if status=='quota_skipped' and not attempt.get('generation_requests',0):
        if category=='shared_minute_token_budget':
            return 'ortak dakika kotası dolu; bu sağlayıcıya yeni istek gönderilmedi'
        return 'analiz bu modelin token bütçesine sığmadığı için istek gönderilmedi'
    if status=='cooldown_skipped':
        return 'limit sonrası beklemede; yeni istek gönderilmedi'
    if attempt.get('http_status')==413 or attempt.get('error_code')=='rate_limit_exceeded' or category in ('rate_limit','token_limit','request_token_limit','request_token_budget'):
        return 'istek bu modelin token/kota sınırına takıldı'
    labels={
        'network_timeout':'yanıt zamanında gelmedi',
        'ambiguous_timeout':'yanıt zamanında gelmedi',
        'transport_or_local_error':'bağlantı veya yerel işlem hatası; yanıt alınamadı',
        'network_error':'bağlantı hatası',
        'json_generation_failed':'istenen karar biçiminde yanıt üretemedi',
        'invalid_decision_schema':'karar biçimi geçersizdi',
        'invalid_output_or_local_validation':'yanıt geçersizdi veya yerel kontrolden geçmedi',
        'authentication_or_permission':'anahtar veya erişim izni reddedildi',
        'model_unavailable':'model bu hesapta kullanılamıyor',
        'context_limit':'gönderilen analiz bağlamı model sınırını aştı',
        'provider_server_error':'sağlayıcıda geçici hata',
        'credit_exhausted':'sağlayıcı kredisi yetersiz',
        'reasoning_format_conflict':'istek biçimi modelle uyumsuz',
        'unsupported_parameter_or_format':'istek biçimi modelle uyumsuz',
    }
    if category in labels:
        return labels[category]
    if status in ('requesting','preparing'):
        return 'yanıt bekleniyor'
    if status=='stopped':
        return 'durduruldu; yeni çağrı yok'
    return 'geçerli yanıt alınamadı'


def format_trace(trace):
    """Simple outcome notification. Technical request details stay in diagnostics."""
    strategy={'trend':'trend takibi','momentum':'ivme takibi','mean_reversion':'ortalamaya dönüş'}.get(trace.get('strategy'),'aday incelemesi')
    text=f"{trace_outcome(trace)}\n{trace.get('symbol','?')} | {strategy}\n"
    if trace.get('execution_status')=='rejected':
        text+=human_reason(trace.get('execution_reason'))+'\n'
    for attempt in trace.get('attempts',[]):
        provider={'groq':'Groq','anthropic':'Claude'}.get(attempt.get('provider'),'AI')
        text+=f"• {provider} ({attempt.get('model','model bilgisi yok')}): {provider_result(attempt)}.\n"
    known=[a['estimated_cost_usd'] for a in trace.get('attempts',[]) if a.get('estimated_cost_usd') is not None]
    pending=sum(a.get('pending_usd') or 0 for a in trace.get('attempts',[]))
    if known:
        text+=f"Bu analizde bilinen Claude tahmini: ${sum(known):.6f}.\n"
    if pending:
        text+=f"Yanıtı belirsiz istek için ayrılan tahmin: ${pending:.6f}; kesin harcama değil.\n"
    elif not known:
        text+='Bu analizin ücreti doğrulanmadı; sıfır ücret anlamına gelmez.\n'
    text+='Açık işlemler: /positions · Teknik ayrıntı: /diagnostics'
    return text[:3500]
