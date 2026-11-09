"""Shared operator visibility. Counts plans, never promises future orders."""
import time


def zone_alert_text(event):
    """Touch warns. A closed CHoCH/MSS is the turn. Neither text is an order."""
    evidence = event.get('evidence') or {}
    stage = evidence.get('stage')
    symbol = event.get('symbol', '?')
    lane = f"{event.get('context_timeframe', '?')} → {event.get('trigger_timeframe', '?')}"
    if stage == 'awaiting_confirmation':
        return (f"CANLI | {symbol} | Bölge veya blok teması ({lane}). "
                "Algoritma uyardı; temas tek başına emir değil. Kapanmış CHoCH/MSS ile işlem sırası gelir.")
    if stage == 'confirmed':
        return (f"CANLI | {symbol} | Kapanmış CHoCH/MSS ({lane}). "
                "İşlem sırası geldi; teyit olmadan emir yok.")
    return None


def live_risk_block_lines(blocks):
    labels = {'daily_loss_pct':'Günlük kayıp','weekly_loss_pct':'Haftalık kayıp','drawdown_pct':'Zirveden düşüş'}
    return [f"{label} {blocks[key]['observed']:.2%} | sınır {blocks[key]['limit']:.2%}."
            for key, label in labels.items() if key in blocks]


def activity_summary(snapshot, now=None):
    now=time.time() if now is None else now
    control=snapshot.get('control') or {}
    enabled=bool(control.get('enabled') and not control.get('stopped'))
    rows=[r for r in snapshot.get('watchlist',[]) if r.get('status') in ('watching','triggered','reviewing','waiting_candidate')
          and (not r.get('expires_at') or r['expires_at']>now)]
    waiting=sum(r.get('status')=='watching' for r in rows)
    new_data=sum(r.get('status')=='triggered' and r.get('last_action')=='hold' for r in rows)
    review=len(rows)-waiting-new_data
    queue=snapshot.get('queue_status') or {}
    scan=snapshot.get('scan_summary') or {}
    queued=queue.get('size',scan.get('queue_size'))
    capacity=snapshot.get('analysis_capacity') or {}
    stage=snapshot.get('stage')
    reason=(snapshot.get('entry_status') or {}).get('reason') or snapshot.get('reason')
    health=snapshot.get('worker_health') or {}
    delayed=health.get('delayed_jobs') or []
    if delayed:
        category='delayed'
        label='Bir görev beklenenden uzun sürüyor; teknik kontrol gerekiyor. Bu, görevin kesin kilitlendiği anlamına gelmez.'
    elif not enabled:
        category='paused';label='Yeni işlem arayışı kapalı.'
    elif snapshot.get('live_halt') or snapshot.get('halt'):
        category='blocked';label='Hesapta teknik giriş engeli var; yeni emir bekliyor.'
    elif reason in ('HOLD_LIVE_LOSS_CIRCUIT_BREAKER','HOLD_LOSS_CIRCUIT_BREAKER','HOLD_LIVE_NO_FUNDS','HOLD_LIVE_START_REQUIRED','HOLD_LIVE_POSITION_LIMIT'):
        from trading.agents.telemetry import human_reason
        category='entry_gate'
        label={'HOLD_LIVE_LOSS_CIRCUIT_BREAKER':'Kayıp/düşüş kontrolü yeni girişleri bekletiyor.',
               'HOLD_LIVE_NO_FUNDS':'Kullanılabilir bakiye bekleniyor.',
               'HOLD_LIVE_START_REQUIRED':'Başlat yetkisi bekleniyor.',
               'HOLD_LIVE_POSITION_LIMIT':'Açık pozisyon sınırı dolu.'}.get(reason,human_reason(reason))
    elif stage in ('model_evaluation','fetching_multitimeframe_context'):
        category='analyzing';label='AI analizi veya güncel verisi hazırlanıyor; henüz işlem açıldığı anlamına gelmez.'
    elif stage in ('model_invalid_or_unavailable_model','analysis_error_backoff'):
        category='analysis_failed';label='Son AI analizi tamamlanamadı; hata/fallback ayrıntısı Son analiz ekranında. Bu, geçerli BEKLE kararı değildir.'
    elif stage=='model_provider_wait' or reason in ('HOLD_LIVE_AI_CAPACITY','HOLD_PROVIDER_POOL_WAIT') and stage not in ('model_completed','unchanged_closed_candles'):
        category='capacity';label='Bu analiz için uygun AI rotası/kota bekleniyor. Bütün modellerin kotası bittiği anlamına gelmez.'
    elif review:
        category='review';label='Takip koşulu/yapı değişikliği sonrası güncel inceleme bekleniyor.'
    elif waiting or new_data:
        category='watching';label='Uygun giriş veya yeni veri bekleniyor; takip listesi izleniyor.'
    elif stage=='unchanged_closed_candles':
        category='unchanged';label='Kurulum değişmedi; tekrar AI isteği gönderilmiyor.'
    elif reason in ('HOLD_LIVE_MODEL','HOLD_CLAUDE_DECISION_OR_FAILURE'):
        category='decision';label='Son değerlendirme işlem açılmasına dönüşmedi; Son analiz karar/ret nedenini gösterir.'
    else:
        category='searching';label='Yeni uygun aday aranıyor; işlem açılması zorunlu değil.'
    text=(f'İşlem arayışı özeti: {label}\n'
          f'Giriş koşulu bekleyen takip planı: {waiting}\n'
          f'Tetiklenmiş / yeniden inceleme bekleyen plan: {review}\n'
          f'AI bekle dedi; yeni veri bekleyen plan: {new_data}\n')
    if queued is not None:text+=f'Analiz kuyruğundaki aday: {queued} (açık emir değil)\n'
    if capacity:
        text+=(f"Yerel uygun analiz kapasitesi: ücretsiz {capacity.get('free_slots',0)}, ücretli yedek {capacity.get('paid_slots',0)}. "
               'Bu sayı kalan günlük istek sayısı değildir.\n')
    pool=snapshot.get('provider_pool') or {}
    if 'paid_fallback_enabled' in pool:
        text+='Sağlayıcı hatası/kota durumunda uygun diğer rota denenir; geçerli BEKLE kararı fallback başlatmaz.\n'
        text+='Ücretli Claude yedeği: '+('açık' if pool['paid_fallback_enabled'] else 'kapalı')+'.\n'
    traces=list(snapshot.get('analysis_history') or [])+[snapshot.get('analysis_trace') or {}]
    finished=[t for t in traces if t.get('completed_at') or t.get('status')=='completed']
    if finished:
        from trading.agents.telemetry import trace_outcome,human_reason
        latest=max(finished,key=lambda t:t.get('completed_at') or t.get('started_at') or 0)
        text+=f"Son tamamlanan analiz: {latest.get('symbol','?')} · {trace_outcome(latest)}\n"
        if latest.get('execution_reason'):
            text+='Son yürütme kontrolü: '+human_reason(latest['execution_reason'])+'\n'
    for job in delayed:
        label={'analysis':'AI analizi','scan':'piyasa taraması','monitor':'borsa uzlaştırması','management':'pozisyon yönetimi'}.get(job.get('kind'),'arka plan görevi')
        text+=f"Geciken görev: {label}, {job.get('age_seconds',0):.0f} sn.\n"
    if scan.get('at'):
        age=max(0,now-scan['at'])
        if age>180:text+=f'Son tarama {age:.0f} sn önce; bu sayı tek başına kota tükendiğini göstermez.\n'
    return dict(category=category,text=text,waiting=waiting,review=review,new_data=new_data,queued=queued,enabled=enabled)


def publish_activity(controller, store, account_id, snapshot, settings, now=None):
    """Bounded periodic/change alerts, shared by LIVE and PAPER. No AI call."""
    now=time.time() if now is None else now
    summary=activity_summary(snapshot,now)
    if not summary['enabled'] and not snapshot.get('positions'):return
    key=f'account:{account_id}:activity_notice'
    prior=store.get(key,{})
    interval=getattr(settings,'telegram_activity_interval_seconds',900)
    changed=summary['category']!=prior.get('category')
    # Suppress fast transitions; do not flood a message for every scan/AI call.
    due=now-prior.get('at',0)>=interval or changed and now-prior.get('at',0)>=120
    if not due:return
    mode=snapshot.get('mode','PAPER')
    text=f'Sistem durumu · {mode} · {account_id}\nAçık pozisyon: {len(snapshot.get("positions") or {})}\n'+summary['text']
    text+='Ayrıntı: /status · /takip · /diagnostics · /quota'
    controller.enqueue(account_id,text,event_id=f'activity:{mode}:{account_id}:{int(now)}')
    store.put(key,dict(at=now,category=summary['category']))
