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
        'HOLD_LIVE_EXECUTION_CONTRACT_QUANTITY_LIMIT':'Önerilen işlem miktarı borsanın kontrat sınırına uymadı; işlem açılmadı.',
        'HOLD_LIVE_EXECUTION_ENTRY_PRICE_DRIFT':'Analiz sonrasında fiyat değişti; işlem açılmadı.',
        'HOLD_LIVE_EXECUTION_LIQUIDATION_BUFFER':'Kaldıraç/stop planı likidasyon mesafesi kontrolünü geçmedi.',
        'HOLD_LIVE_EXECUTION_PRIVATE_PREFLIGHT':'Hesap, marjin veya parite ön kontrolü reddetti; ayrıntı Son analiz ekranında.',
        'HOLD_LIVE_EXECUTION_RISK_REJECTED':'İşlem önerisi risk kontrolünden geçmedi; ayrıntı Son analiz ekranında.',
        'HOLD_LIVE_EXECUTION_TARGET_COSTS':'Hedef tahmini işlem maliyetini karşılamıyor; işlem açılmadı.',
        'HOLD_EVALUATION_INTERVAL':'Bir sonraki analiz zamanı bekleniyor; şu anda yeni AI isteği gönderilmiyor.',
        'HOLD_SCAN_INTERVAL':'Yeni piyasa taraması bekleniyor.',
        'HOLD_PROVIDER_POOL_WAIT':'Ücretsiz sağlayıcı kotası veya erişimi bekleniyor; tarama ve açık işlem koruması sürüyor.',
        'HOLD_WATCH_CHANGED':'Parite planı analiz sırasında değişti; eski kararla işlem açılmadı.',
        'HOLD_WATCH_PLAN_CHANGED':'Parite planı analiz sırasında değişti; eski kararla işlem açılmadı.',
        'HOLD_WATCH_WAITING_CANDIDATE':'Takip koşulu gerçekleşti; güncel ve geçerli işlem adayı bekleniyor.',
        'HOLD_WATCH_CONFLICT':'Öneri mevcut parite planıyla çelişti; yeni işlem açılmadı.',
        'HOLD_QUEUE_EMPTY':'Güncel aday kuyruğu boş; bağımsız tarama sürüyor.',
        'HOLD_CANDIDATE_STALE':'Aday analiz sırasında eskidi veya taramada değişti; işlem açılmadı.',
        'HOLD_DUPLICATE_OR_ALREADY_OPEN':'Bu mum için işlem zaten kaydedilmiş veya paritede açık işlem var; yeni işlem açılmadı.',
        'HOLD_CANDIDATE_PRICE_DRIFT':'Sıradaki adayın fiyatı değişti; taramada güncellenmesi bekleniyor.',
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
    if trace.get('action')=='cancel_watch':return 'Takip planı AI kararıyla iptal edildi; işlem açılmadı'
    if trace.get('status')=='provider_wait':return 'Sağlayıcı kapasitesi bekleniyor; yeni AI isteği gönderilmedi'
    execution=trace.get('execution_status')
    if execution=='opened':
        return 'Gerçek işlem açıldı; borsa koruması doğrulandı' if trace.get('mode')=='LIVE' else 'Sanal işlem açıldı'
    if execution in ('rejected','not_opened'):
        if trace.get('model_action')=='trade':return 'AI işlem önerdi; doğrulama/yürütme aşamasında açılmadı'
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
        reasons={
            'shared_minute_token_budget':'ortak dakika kotası dolu; bu sağlayıcıya yeni istek gönderilmedi',
            'free_capacity_in_flight':'ücretsiz model başka analizi tamamlıyor; ücretli modele geçilmeden sıra bekleniyor',
            'rolling_daily_requests':'son 24 saatlik yerel istek kotası dolu; bu sağlayıcıya yeni istek gönderilmedi',
            'rolling_daily_tokens':'son 24 saatlik yerel token kotası dolu; bu sağlayıcıya yeni istek gönderilmedi',
            'minute_requests':'dakikalık yerel istek kotası dolu; yeni istek gönderilmedi',
            'minute_tokens':'dakikalık yerel token kotasında yer yok; yeni istek gönderilmedi',
            'provider_cooldown':'sağlayıcı bekleme süresinde; yeni istek gönderilmedi',
            'reservation_race':'kapasite yeniden kontrol edilecek; yeni istek gönderilmedi',
        }
        if category in reasons:return reasons[category]
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
        'output_truncated':'yanıt çıktı sınırında kesildi; eksik karar kullanılmadı',
        'missing_decision_tool':'istenen karar aracı yanıtı gelmedi',
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
    strategy={'trend':'trend takibi','momentum':'ivme takibi','mean_reversion':'ortalamaya dönüş','watch_review':'takip planı yeniden incelemesi'}.get(trace.get('strategy'),'aday incelemesi')
    text=f"{trace_outcome(trace)}\n{trace.get('symbol','?')} | {strategy}\n"
    if trace.get('rationale'):
        text+='AI gerekçesi: '+str(trace['rationale'])[:1200]+'\n'
    if trace.get('watch_note') and trace.get('action') in ('hold','watch'):
        text+='Takip notu: '+str(trace['watch_note'])[:600]+'\n'
    if trace.get('action')=='watch':
        text+='Koşullu takip ayrıntısı: /takip\n'
    if trace.get('invalidation'):
        text+='Planı geçersiz kılan koşul: '+str(trace['invalidation'])[:500]+'\n'
    if trace.get('why_reconsidered'):
        changed=trace['why_reconsidered']
        text+='Yeniden bakma nedeni: '+str(changed.get('summary','') if isinstance(changed,dict) else changed)[:400]+'\n'
    if trace.get('execution_status')=='rejected':
        text+=human_reason(trace.get('execution_reason'))+'\n'
    for attempt in trace.get('attempts',[]):
        provider={'groq':'Groq','gemini':'Gemini','anthropic':'Claude'}.get(attempt.get('provider'),'AI')
        label=f" / {attempt['credential_id']}" if attempt.get('credential_id') else ''
        text+=f"• {provider}{label} ({attempt.get('model','model bilgisi yok')}): {provider_result(attempt)}.\n"
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


def validation_issues(exc,schema=None):
    """Only static field names/categories; never rejected values or provider text."""
    known={'response'}
    def collect(node):
        if isinstance(node,dict):
            known.update(node.get('properties',{}))
            for value in node.values():collect(value)
        elif isinstance(node,list):
            for value in node:collect(value)
    collect(schema)
    semantic={
        'unknown or inapplicable setup selection':'unknown_setup_id',
        'structure seed requires explicit setup selection':'structure_seed_requires_selection',
        'selected setup requires confirmed full context and fresh price plan':'setup_requires_ready_context_price_plan',
        'countertrend sizing exceeds reduced risk limit':'countertrend_risk_limit',
        'cancel_watch requires active reviewed revision and no order fields':'cancel_watch_revision_or_fields',
        'independent watch trade requires a fresh price plan':'watch_requires_fresh_price_plan',
        'explicit model sizing required for exchange leverage mode':'explicit_sizing_required',
        'watch price condition already satisfied; choose a future condition or trade/hold':'watch_condition_already_met',
        'unknown signal':'unknown_signal_id',
        'price planning disabled or not trade':'price_plan_not_allowed',
        'watch plan only allowed for watch action':'watch_plan_action_conflict',
        'watch expiry must be within24h':'watch_expiry_invalid',
        'missing_or_open_candle_reference':'price_reference_missing_or_open',
        'price_tick_mismatch':'price_tick_mismatch',
        'market_entry_reference_too_far':'entry_reference_too_far',
        'price_plan_direction_invalid':'price_direction_invalid',
        'wrong_reference_direction':'reference_direction_invalid',
        'stop_not_beyond_observed_wick':'stop_wick_invalid',
        'stop_too_far_from_reference':'stop_buffer_invalid',
        'target_reference_wrong_side':'target_direction_invalid',
        'target_too_far_from_reference':'target_buffer_invalid',
    }
    message=str(exc)
    if message in semantic:return [dict(field='response',type=semantic[message])]
    categories={'missing','extra_forbidden','string_type','string_pattern_mismatch','string_too_long','string_too_short',
        'float_type','float_parsing','finite_number','greater_than','greater_than_equal','less_than','less_than_equal',
        'int_type','literal_error','enum','dict_type','list_type','bool_type','value_error'}
    try:errors=exc.errors(include_input=False,include_context=False,include_url=False)
    except Exception:return [dict(field='response',type='invalid_json' if type(exc).__name__=='JSONDecodeError' else 'custom_validation')]
    return [dict(field=('.'.join(str(p) if p in known else 'field' for p in error.get('loc',())) or 'response')[:160],
        type=error.get('type') if error.get('type') in categories else 'schema_validation') for error in errors[:12]]
