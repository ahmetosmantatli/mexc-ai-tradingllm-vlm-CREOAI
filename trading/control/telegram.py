"""One poller per bot token. Durable allowlisted commands and at-least-once outbox.

Run on a dedicated control thread with its own RuntimeStore connection, never on
position protection's hot path. Account status is supplied by the orchestrator.
"""
from contextlib import ExitStack
from datetime import datetime
from zoneinfo import ZoneInfo
import json
import logging
from math import isfinite
from pathlib import Path
import time
from uuid import uuid4

import httpx

logger = logging.getLogger(__name__)

TRADER_MODES = {
    'trader_preservation': dict(label='Sermayeyi koru', risk_profile='capital_preservation', risk_per_trade_pct=.005, max_leverage=3., max_positions=2, daily_loss_limit_pct=.02, drawdown_limit_pct=.08),
    'trader_balanced': dict(label='Dengeli', risk_profile='balanced', risk_per_trade_pct=.01, max_leverage=5., max_positions=3, daily_loss_limit_pct=.035, drawdown_limit_pct=.12),
    'trader_aggressive': dict(label='Agresif', risk_profile='aggressive_growth', risk_per_trade_pct=.03, max_leverage=10., max_positions=4, daily_loss_limit_pct=.055, drawdown_limit_pct=.225),
}



class TelegramController:
    def __init__(self, token, accounts, store, snapshot_provider=None, client=None):
        self.token, self.store = token, store
        self.command_handler = None
        self.action_handler = None
        self.accounts = {a.account_id: a for a in accounts}
        self.identities = {(a.telegram_user_id, a.telegram_chat_id): a for a in accounts}
        if len(self.accounts) != len(accounts) or len(self.identities) != len(accounts):
            raise ValueError('duplicate Telegram accounts')
        self.snapshot_provider = snapshot_provider or (lambda account_id: store.get(f'account:{account_id}:status'))
        self.client = client or httpx.Client(timeout=8)
        # URLs include the bot token; never allow INFO request URL logging.
        logging.getLogger('httpx').setLevel(logging.WARNING)
        logging.getLogger('httpcore').setLevel(logging.WARNING)

    def call(self, method, **data):
        result = self.client.post(f'https://api.telegram.org/bot{self.token}/{method}', json=data)
        result.raise_for_status()
        payload = result.json()
        if payload.get('ok') is not True:
            raise ValueError('Telegram API rejected request')
        return payload['result']

    def control(self, account_id):
        if account_id not in self.accounts:
            raise ValueError('unknown account')
        defaults=dict(enabled=False, version='text', version_chosen=False, profile_chosen=False, risk_profile=None, risk_per_trade_pct=.01, capital_mode=None, capital_amount=None, allocation_mode='fixed', capital_chosen=False, capital_pending=None, capital_input=False)
        return {**defaults, **self.store.get(f'account:{account_id}:control', {})}

    def set_control(self, account_id, **updates):
        state = self.control(account_id)
        state.update(updates)
        state['updated_at'] = time.time()
        self.store.put(f'account:{account_id}:control', state)
        return state

    def linked(self, account_id):
        return (self.store.get(f'account:{account_id}:linked', False) is True
                and not self.store.get(f'account:{account_id}:disconnected', False))

    def capital_ready(self, account_id):
        state=self.control(account_id)
        return (state['capital_chosen'] and not state['capital_pending']
                and (state['capital_mode']=='demo' or (state['capital_mode']=='account' and self.linked(account_id))))

    def capital_text(self, account_id):
        state=self.control(account_id)
        if state['capital_pending']:
            return 'Sermaye değişikliği bekliyor. Açık pozisyon/analiz varsa önce güvenli biçimde tamamlanmalı; henüz yeni kasa uygulanmadı.'
        if state['capital_mode']=='demo':
            return f"Demo kasa: {state['capital_amount']:g} USDT (sanal)" if state['capital_amount'] else 'Demo kasa tutarını USDT olarak yaz. Örnek: /demo 1000'
        if state['capital_mode']=='account':
            allocation='son doğrulanan kullanılabilir USDT bakiyesinin tamamı' if state['allocation_mode']=='full' else f"{state['capital_amount']:g} USDT" if state['capital_amount'] else 'seçilmedi'
            return f"MEXC hesabına referanslı PAPER kasa: {allocation}. Gerçek emir açılmaz."
        return 'Önce para yatırmadan Demo veya MEXC bakiyesine referanslı Paper seç.'

    def keyboard(self, account_id):
        control=self.control(account_id)
        rows=[]
        if not self.capital_ready(account_id):
            if not control['capital_pending']:
                if control['capital_mode']=='demo' and control['capital_input']:
                    rows.append([{'text':f'{amount} USDT sanal', 'callback_data':f'control:demo_amount:{amount}'} for amount in (100,500,1000)])
                rows.append([{'text':'Demo · Para yatırmadan', 'callback_data':'control:capital_demo'}, {'text':'MEXC bakiyesinden Paper', 'callback_data':'control:capital_account'}])
                if control['capital_mode']=='account' and self.linked(account_id):
                    rows.append([{'text':'Tüm kullanılabilir bakiyeyi seç', 'callback_data':'control:capital_full'}, {'text':'Tutarı elle yaz', 'callback_data':'control:capital_amount'}])
                if control['capital_mode']=='account' and not self.linked(account_id):
                    rows.append([{'text':'MEXC hesabını bağla', 'callback_data':'control:connect'}])
        elif not control['version_chosen']:
            rows.append([{'text':'Görselsiz', 'callback_data':'control:text'}, {'text':'VLM (grafikli)', 'callback_data':'control:vlm'}])
        elif not control['profile_chosen']:
            rows.extend([[{'text':mode['label'], 'callback_data':'control:'+key}] for key,mode in TRADER_MODES.items()])
        else:
            rows.append([{'text':'✓ Yeni analiz açık' if control['enabled'] else '▶ Ayarlarla başlat', 'callback_data':'control:status' if control['enabled'] else 'control:start'}])
            rows.append([{'text':'⏸ İşlem arayışını duraklat', 'callback_data':'control:pause'}, {'text':'⏹ Bot / AI durdur', 'callback_data':'control:stop'}])
            rows.append([{'text':'Ayarları değiştir', 'callback_data':'control:settings'}, {'text':'Kasa seçimini değiştir', 'callback_data':'control:capital'}])
        count=len(self.position_targets(account_id))
        rows.append([{'text':'Durum / Bakiye', 'callback_data':'control:status'}, {'text':f'Açık işlemler ({count})', 'callback_data':'control:positions'}, {'text':'Günlük rapor', 'callback_data':'control:report'}])
        rows.append([{'text':'Takip listesi', 'callback_data':'control:watchlist'}, {'text':'Son analiz / AI istekleri', 'callback_data':'control:diagnostics'}])
        if self.linked(account_id):
            rows.append([{'text':'✓ MEXC bağlı', 'callback_data':'control:status'}, {'text':'Bağlantıyı kaldır', 'callback_data':'control:disconnect'}])
        return {'inline_keyboard':rows}

    def settings_text(self, account_id):
        state=self.control(account_id)
        if not self.capital_ready(account_id):
            return '1/4 · İşlem yapılacak PAPER kasa\n'+self.capital_text(account_id)+'\nDemo için MEXC bağlantısı veya gerçek para gerekmez. Tüm seçenekler paper simülasyondur.'
        if not state['version_chosen']:
            return '2/4 · Analiz sürümü\n'+self.capital_text(account_id)+'\nGörselsiz: sayısal mum/strateji verileri.\nVLM: aynı veriler + 4s/15dk/5dk grafikleri. Birini seç.'
        if not state['profile_chosen']:
            return f"3/4 · Trader modu\nAnaliz: {'VLM (grafikli)' if state['version']=='vlm' else 'Görselsiz'}\nSermayeyi koru, Dengeli veya Agresif seç. Modlar kazanç garantisi değildir."
        mode=next((m for m in TRADER_MODES.values() if m['risk_profile']==state['risk_profile']),None)
        if mode is None:
            return 'Trader modu yeniden seçilmeli. /settings'
        risk=state['risk_per_trade_pct']*100
        snapshot=self.snapshot_provider(account_id) or {}
        variant=(snapshot.get('variants') or {}).get(state['version'],{})
        paper=variant.get('paper') or (snapshot.get('paper') if snapshot.get('version',state['version'])==state['version'] else {}) or {}
        equity=paper.get('equity')
        source='Son gözlenen paper özkaynak'
        if equity is None:
            equity=state.get('capital_amount') or 0
            source='Başlangıç paper kasası (güncel gözlem henüz yok)'
        risk_amount=float(equity)*state['risk_per_trade_pct']
        return (f"4/4 · Ayar özeti | PAPER\n{self.capital_text(account_id)}\nAnaliz: {'VLM (grafikli)' if state['version']=='vlm' else 'Görselsiz'}\n"
                f"Trader modu: {mode['label']}\nİşlem başına planlanan azami stop kaybı: %{risk:g}\n"
                f"{source}: {equity:g} USDT → en fazla {risk_amount:g} USDT planlanan stop riski.\n"
                'Yüzde profil sınırıdır; tutar her işlemde güncel özkaynağa göre yeniden hesaplanır. Teminat yüzdesi değildir.\n'
                f"Kaldıraç üst sınırı: {state['max_leverage']:g}x; bu kaldıracı kullanmak zorunlu değil. Parite/teminat/stop koşulları daha düşük değer gerektirebilir.\n"
                f"Azami açık pozisyon: {state['max_positions']}\nGünlük kayıp eşiği: %{state['daily_loss_limit_pct']*100:g} | Zirveden düşüş: %{state['drawdown_limit_pct']*100:g}\n"
                'Günlük eşik gün başı özkaynağa, düşüş eşiği özkaynak zirvesine göredir. Eşikler yeni girişleri durdurur; açık işlemleri zorla kapatmaz ve kesin kayıp tavanı değildir.\n'
                + self.algorithm_text() + '\n'
                'Fiyat boşluklarında kayıp planı aşabilir. Uygun fırsatta işlem açılır; işlem zorunlu değil. '
                + ('Yeni analizlere izin verildi. Gerçek ilerlemeyi Durum / Bakiye ve Son analiz ekranından kontrol et.' if state['enabled'] else 'Başlat ile onayla.'))

    @staticmethod
    def algorithm_text():
        return ('Aday algoritmaları: trend SMA8/21, momentum ROC12, ortalamaya dönüş SMA20 (15dk). '
                'Sabit yüzde ağırlık yok; güncel adaylar bekleme sırasına göre dönüşümlü değerlendirilir. '
                'Aday puanı başarı olasılığı değildir. AI ayrıca 4s/15dk/5dk yapısını değerlendirir. '
                'LIVE_STRUCTURE_SETUPS_ENABLED açıkken bağımsız yapısal ön eleme ve aynı paritede trend devamı/geri çekilme/karşı trend sweep seçenekleri eklenir. '
                'LIVE takip koşulu/yapı değişikliği, eski algoritmadan sinyal gerektirmeden ayrı değerlendirme oluşturabilir.')

    @staticmethod
    def progress_text(snapshot):
        stage=snapshot.get('stage')
        label={'fetching_multitimeframe_context':'4 saat / 15 dakika / 5 dakika verileri hazırlanıyor',
               'scanning_market':'Pariteler taranıyor', 'model_evaluation':'AI yanıtı bekleniyor',
               'unchanged_closed_candles':'Kurulum değişmedi; gereksiz AI çağrısı yapılmıyor',
               'analysis_error_backoff':'Bu analizde hata alındı; kısa hata beklemesi sürüyor',
               'candidates_filtered':'Aday filtreleri tamamlandı', 'model_completed':'AI analizi tamamlandı',
               'model_provider_wait':'AI sağlayıcı kapasitesi bekleniyor; yeni istek gönderilmiyor', 'model_invalid_or_unavailable_model':'AI analizi sonuçlanmadı', 'awaiting_scan':'İlk tarama bekleniyor'}.get(stage,'Piyasa ve işlem durumu izleniyor')
        text=f"Şu an: {label}\n"
        public=snapshot.get('public_data') or {}
        if public.get('cooldown_seconds',0)>0:text+=f"MEXC veri beklemesi: {public['cooldown_seconds']:.0f} sn; yeni public istekler bekletiliyor.\n"
        scan=snapshot.get('scan_summary') or {}
        if scan:
            age=max(0,time.time()-scan.get('at',time.time()))
            text += (f"Son tarama: {age:.0f} sn önce | Parite {scan.get('scanned',0)} | Likidite filtresini geçen {scan.get('kept',0)}\n"
                     f"Üretilen aday {scan.get('candidates',0)} | AI için uygun {scan.get('eligible',0)}\n")
            if scan.get('duration_seconds') is not None:
                text+=f"Tarama süresi: {scan['duration_seconds']:.2f} sn\n"
            if scan.get('queue_size') is not None:text+=f"Kuyrukta korunan güncel aday: {scan['queue_size']}\n"
            if scan.get('errors'):
                symbol_errors={key:value for key,value in scan['errors'].items() if key.endswith('_USDT')} if isinstance(scan['errors'],dict) else None
                text+=f"Veri hatası nedeniyle atlanan parite: {len(symbol_errors) if symbol_errors is not None else scan['errors']}\n"
                if isinstance(scan['errors'],dict):
                    from collections import Counter
                    counts=Counter(scan['errors'].values())
                    text+='Hata türleri: '+', '.join(f'{kind}: {count}' for kind,count in counts.most_common(5))+'\n'
                    text+='Örnekler: '+', '.join(f'{symbol} ({kind})' for symbol,kind in list(scan['errors'].items())[:3])+'\n'
                    extra=set(scan['errors'])-set(symbol_errors or {})
                    if extra:text+='Ek veri kontrollerinde sorun: '+', '.join(sorted(extra))+'\n'

        queue=snapshot.get('queue_status') or {}
        if queue:
            text+=f"Kuyruktaki güncel aday: {queue.get('size',0)}\n"
            if queue.get('next_symbol'):
                text+=f"Sıradaki aday: {queue['next_symbol']}\n"
        if snapshot.get('scan_stage')=='scanning_market':
            text+='Yeni tarama arka planda sürüyor; AI bekleme aralığından bağımsız.\n'
        if snapshot.get('parallel_candidates'):
            text+=f"Aynı anda en fazla {snapshot['parallel_candidates']} farklı aday analizi. İşlem açılışları ortak risk kontrolünden geçer.\n"
        if snapshot.get('ai_schedule_mode')=='quota':
            text+='AI zamanlaması: kota ve uygun aday odaklı; sabit tur beklemesi yok.\n'
            wait=max(0,snapshot.get('provider_wait_until',0)-time.time())
            if wait:
                text+=f"Sağlayıcı/hata beklemesi: yaklaşık {wait:.0f} sn; pozisyon koruması sürüyor.\n"
        next_at=snapshot.get('next_evaluation_at')
        if next_at:
            text += f"Sonraki AI değerlendirmesine en az {max(0,next_at-time.time()):.0f} sn; uygun yeni aday gerekir.\n"
        return text

    def watchlist_text(self, account_id):
        snapshot=self.snapshot_provider(account_id) or {}
        rows=snapshot.get('watchlist') or []
        text='Takip listesi · PAPER\nTakip koşulu gerçekleşmesi işlem açıldığı anlamına gelmez.\n'
        active=[row for row in rows if row.get('status') in ('watching','triggered','waiting_candidate','reviewing')]
        if not active:
            return text+'Şu anda aktif takip planı yok. AI uygun gördüğünde koşullu takip oluşturabilir.'
        labels={'price_above':'Fiyat üzerine çıkınca','price_below':'Fiyat altına inince',
                'close_above':'Mum üzerinde kapanınca','close_below':'Mum altında kapanınca',
                'closed_candle_cross_above':'Mum seviyeyi yukarı geçerek kapanınca','closed_candle_cross_below':'Mum seviyeyi aşağı geçerek kapanınca'}
        statuses={'watching':'Koşul bekleniyor','triggered':'Koşul gerçekleşti; yeniden analiz bekliyor',
                  'waiting_candidate':'Koşul gerçekleşti; güncel algoritma adayı bekleniyor',
                  'reviewing':'Yeniden inceleniyor'}
        for row in active[:12]:
            remaining=max(0,(row.get('expires_at') or 0)-time.time())
            text+=f"\n{row.get('symbol','?')} · {row.get('side','?')}\n"
            status_text=statuses.get(row.get('status'),row.get('status','?'))
            if row.get('review_reason')=='structure_changed':status_text='Yapı değişti; takip planı yeniden değerlendiriliyor'
            elif row.get('status')=='triggered' and row.get('last_action')=='hold':status_text='Koşul değerlendirildi; yeni veri bekleniyor'
            text+=status_text+'\n'
            text+=f"{labels.get(row.get('trigger_type'),'Tetikleyici')}: {row.get('trigger_price','?')}"
            if row.get('timeframe_seconds'):
                text+=f" | {row['timeframe_seconds']//60} dk"
            text+=f"\nKalan takip süresi: {remaining/60:.0f} dk\n"
            if row.get('reason'):
                reason={'model_watch':'AI teyit bekliyor','trigger_confirmed':'Takip koşulu gerçekleşti'}.get(row['reason'],row['reason'])
                text+=f"Gerekçe: {str(reason)[:240]}\n"
            if row.get('invalidation'):
                invalidation=row['invalidation']
                if isinstance(invalidation,dict):
                    direction='üstü' if invalidation.get('condition')=='above' else 'altı'
                    invalidation=f"{invalidation.get('price','?')} {direction}"
                text+=f"İptal: {invalidation}\n"
        if len(active)>12:
            text+=f"\nDiğer aktif takip: {len(active)-12}"
        return text

    def quota_text(self, account_id):
        snapshot=self.snapshot_provider(account_id) or {}
        groups=(snapshot.get('provider_pool') or {}).get('groups',[])
        text='AI kotaları · yerel tahmin; sağlayıcı bakiyesi değildir.\nGemini: Pasifik günü. Groq: korumacı son24s.\n'
        for group in groups[:24]:
            text+=(f"{group['provider']} / {group['model']} / {group['quota_group']}\n"
                   f"Kalan günlük istek {group.get('requests_daily_remaining','?')} | dk istek {group.get('requests_minute_remaining','?')} | "
                   f"dk token tahmini {group.get('tokens_minute_remaining_estimate','?')} | Meşgul: {'evet' if group.get('in_flight') else 'hayır'}\n")
        resets=sorted({g['next_daily_reset_at'] for g in groups if g.get('provider')=='gemini' and g.get('next_daily_reset_at')})
        if resets:text+='Gemini sonraki günlük reset: '+datetime.fromtimestamp(resets[0],ZoneInfo('Europe/Istanbul')).strftime('%d.%m %H:%M')+'\n'
        if len(groups)>24 or len(text)>3750:
            text=text[:3600]+'\nTam kota listesi: sunucuda python -m trading.control.provider_status\n'
        return text if groups else text+'Henüz kota kaydı yok.'

    def diagnostics_text(self, account_id):
        snapshot=self.snapshot_provider(account_id) or {}
        text='Analiz / AI istekleri | PAPER\n'+self.progress_text(snapshot)+'\n'
        for provider,counts in snapshot.get('ai_request_stats',{}).items():
            text+=(f"Ortak {provider}: üretim isteği {counts.get('generation_requests',0)}, token sayımı {counts.get('count_requests',0)}, "
                   f"başarılı {counts.get('successes',0)}, başarısız {counts.get('failures',0)}\n")
        text+='Kota ayrıntısı: /quota · Sayılar geçmiş toplamdır.\n'
        latest=snapshot.get('analysis_trace') or {}
        history=snapshot.get('analysis_history') or []
        traces=history[-2:] if history else ([latest] if latest else [])
        if latest and (not traces or traces[-1].get('id')!=latest.get('id')):
            traces=(traces+[latest])[-2:]
        if not traces:
            return text+'Henüz kayıtlı aday değerlendirmesi yok. Çalışıyor durumu, mutlaka AI isteği veya işlem oluştuğu anlamına gelmez.'
        for trace in reversed(traces):
            from trading.agents.telemetry import trace_outcome
            text+='\n'+trace_outcome(trace)+'\n'
            score=trace.get('confidence')
            score_text=f"{score:.2f}" if isinstance(score,(int,float)) else '?'
            text += (f"\nAnaliz {trace.get('id','?')} | {trace.get('symbol','?')} | {trace.get('strategy','?')}\n"
                     f"Aday puanı {score_text} (olasılık değil) | Sonuç: {trace.get('status','?')}\n")
            if trace.get('started_at'):
                text+='Analiz saati: '+datetime.fromtimestamp(trace['started_at'],ZoneInfo('Europe/Istanbul')).strftime('%d.%m %H:%M:%S')+'\n'
            if trace.get('rationale'):
                text+='Gerekçe: '+str(trace['rationale'])[:300]+'\n'
            model_action=trace.get('model_action') or (trace.get('action') if trace.get('status')=='completed' else None)
            if model_action:text+='Model kararı: '+{'trade':'İşlem öner','watch':'Takibe al','hold':'Bekle','cancel_watch':'Takibi iptal et'}.get(model_action,'Bilinmiyor')+'\n'
            if trace.get('setup_family'):text+='Seçilen kurulum: '+str(trace['setup_family'])+' | Yön: '+str(trace.get('side','?'))+'\n'
            if trace.get('execution_reason'):text+='Yürütme sonucu: '+str(trace['execution_reason'])+'\n'
            if trace.get('execution_details'):text+='Kontrol ayrıntısı: '+str(trace['execution_details'])[:400]+'\n'
            if trace.get('why_reconsidered'):
                changed=trace['why_reconsidered']
                text+='Değişiklik: '+str(changed.get('summary','') if isinstance(changed,dict) else changed)[:250]+'\n'
            if trace.get('context_policy'):
                text+=f"Bağlam: {trace['context_policy']} | Gönderi: {trace.get('prompt_bytes','?')} bayt\n"
            attempts=trace.get('attempts') or []
            text += f"Sağlayıcı denemesi: {len(attempts)}\n"
            if not attempts:text+='Bu değerlendirmede sağlayıcı isteği kaydedilmemiş; kota, bekleme veya durdurma nedeniyle olabilir.\n'
            sent=[a for a in attempts if a.get('generation_requests',0) or a.get('count_requests',0)]
            for attempt in (sent or attempts)[-2:]:
                text += (f"• {attempt.get('provider','?')} / {attempt.get('model','?')} | {attempt.get('status','?')}"
                         f" | {attempt.get('latency_ms',0):.0f} ms\n")
                errors=[f"{label}: {attempt[key]}" for key,label in (('http_status','HTTP'),('error_category','Tür'),('error_code','Kod'),('error_param','Alan')) if attempt.get(key) is not None]
                if errors:
                    text+=' | '.join(errors)+'\n'
                if attempt.get('phase'):text+=f"Aşama: {attempt['phase']}\n"
                text+=f"Üretim isteği {attempt.get('generation_requests',0)} | Token sayımı {attempt.get('count_requests',0)}\n"
                if attempt.get('output_stop_reason'):
                    text+=f"Yanıt bitiş nedeni: {attempt['output_stop_reason']}\n"
                if attempt.get('validation_issues'):
                    text+='Karar doğrulama alanları: '+str(attempt['validation_issues'])[:250]+'\n'
                if attempt.get('credential_id'):
                    text+=f"Erişim: {attempt['credential_id']} | Kota grubu: {attempt.get('quota_group','?')}\n"
                incoming,outgoing=attempt.get('input_tokens'),attempt.get('output_tokens')
                if incoming is not None or outgoing is not None:
                    text+=f"Token girdi/çıktı: {incoming if incoming is not None else '?'} / {outgoing if outgoing is not None else '?'}\n"
                estimate=attempt.get('estimated_cost_usd')
                text += (f"Yanıt kullanımından tahmini maliyet: ${estimate:.6f}\n" if estimate is not None else 'İstek maliyeti doğrulanmadı / hesaplanmadı.\n')
                if attempt.get('pending_usd'):
                    text+=f"Bekleyen/belirsiz rezervasyon: ${attempt['pending_usd']:.6f} (fatura değil)\n"
        footer='\nTam istek kaydı: sunucudaki usage_report. Kotalar: /quota. Bu kayıtlar sağlayıcı faturası değildir.'
        return (text if len(text)<=3800 else text[:3700]+'\nEski teknik ayrıntılar kısaltıldı.')+footer

    def request_capital(self, account_id, mode, amount=None, allocation_mode='fixed'):
        if mode not in ('demo','account') or allocation_mode not in ('fixed','full'):
            raise ValueError('invalid capital mode')
        if mode=='account' and not self.linked(account_id):
            self.set_control(account_id,enabled=False,capital_mode='account',capital_input=False,capital_chosen=False)
            self.send(account_id,'Önce MEXC hesabını bağla; sonra ayıracağın USDT tutarını veya tam bakiyeyi seç.',buttons=True)
            return
        if allocation_mode=='full' and mode!='account':
            raise ValueError('full allocation requires account')
        if allocation_mode=='fixed':
            try:
                amount=float(amount)
            except (ValueError,TypeError):
                amount=0
            if not isfinite(amount) or amount<=0 or amount>1e12:
                self.send(account_id,'Pozitif ve sonlu bir USDT tutarı yaz. Örnek: /demo 1000 veya /capital 1000. Nokta kullan; bu tutar gerçek emir açmaz.')
                return
        else:
            amount=None
        state=self.control(account_id)
        if state['capital_pending']:
            self.send(account_id,'Önceki kasa değişikliği henüz tamamlanmadı. /status ile kontrol et.')
            return
        command=dict(id=uuid4().hex,kind='capital_reset',status='pending',created_at=time.time(),mode=mode,amount=amount,allocation_mode=allocation_mode)
        key=f'account:{account_id}:commands'
        queue=self.store.get(key,[])
        queue.append(command)
        self.store.put(key,queue)
        self.set_control(account_id,enabled=False,capital_chosen=False,capital_pending=command['id'],capital_input=False,version_chosen=False,profile_chosen=False)
        self.send(account_id,'Kasa değişikliği istendi. Açık pozisyonlar veya analiz bitmeden uygulanmaz. Onaylanan yeni PAPER kasa ayrıca bildirilecek.',buttons=True)

    def report_text(self, account_id):
        snapshot=self.snapshot_provider(account_id) or {}
        reports=snapshot.get('reports')
        if isinstance(reports,str):
            return reports
        if not reports:
            return 'Henüz günlük işlem raporu yok. Bu bir PAPER raporudur; gerçek hesap hareketi değildir.'
        text='Günlük PAPER raporu | Net sonuçlar USDT\n'
        for row in reports[-14:]:
            if 'net_pnl' in row and 'pnl_complete' in row:
                from trading.runtime.reports import format_daily_report
                text += '\n'+row.get('version','')+'\n'+format_daily_report(row)+'\n'
                continue
            text+=(f"{row.get('date','?')} | {row.get('version','')} | İşlem {row.get('trades',0)} | Net PnL {float(row.get('realized_pnl',0)):+.4f} | Ücret {float(row.get('fees',0)):.4f}\n"
                   f"Türler: {row.get('types',{})} | Kaldıraçlar: {row.get('leverages',[])}\n")
        return text

    def send(self, account_id, text, buttons=False, keyboard=None):
        data = dict(chat_id=self.accounts[account_id].telegram_chat_id, text=text[:4000])
        if buttons or keyboard is not None:
            data['reply_markup'] = keyboard if keyboard is not None else self.keyboard(account_id)
        return self.call('sendMessage', **data)

    def _position_views(self, account_id, snapshot):
        variants=snapshot.get('variants') or {self.control(account_id)['version']:{'positions':snapshot.get('positions',{})}}
        return [(version,symbol,p) for version,view in variants.items() for symbol,p in view.get('positions',{}).items()]

    def positions_text(self, account_id, snapshot):
        items=self._position_views(account_id,snapshot)
        text=f"Açık sanal işlemler: {len(items)}\n"
        if not items:
            return text+'Şu anda açık işlem yok. Analiz mesajı, işlem açıldığı anlamına gelmez.\n'
        for version,symbol,p in items:
            side='Yükseliş (LONG)' if p.get('side')=='long' else 'Düşüş (SHORT)'
            label='Görselsiz' if version=='text' else 'VLM (grafikli)'
            text+=f"\n{symbol} · {side} · {p.get('leverage','?')}x\n"
            text+=f"Sürüm: {label}\n"
            text+=f"Giriş: {p.get('entry',0):.6g}\nZarar durdur (SL): {p.get('stop',0):.6g} | Kâr al (TP): {p.get('target',0):.6g}\n"
            text+=f"Şu anki kâr/zarar: {p.get('unrealized',0):+.4f} USDT\n"
            if p.get('sizing_risk_fraction') is not None:
                source='AI seçimi' if p.get('sizing_source')=='model' else 'profil ayarı'
                text+=f"Kaldıraç/risk: {source} | Planlanan kasa riski: %{p['sizing_risk_fraction']*100:g}\n"
            origin=p.get('decision_ai') or {}
            provider={'groq':'Groq','gemini':'Gemini','anthropic':'Claude'}.get(origin.get('provider'),'Kayıtlı değil')
            text+=f"Giriş kararını veren: {provider} / {origin.get('model') or 'model bilgisi yok'}\n"
        return text

    def status_text(self, account_id, positions=False):
        from trading.agents.telemetry import human_reason,trace_outcome
        account=self.accounts[account_id]
        control=self.control(account_id)
        snapshot=self.snapshot_provider(account_id) or {}
        text=f"Sanal işlem paneli · {account.account_id}\nGerçek para ile emir gönderilmiyor.\n"
        text+=self.positions_text(account_id,snapshot)
        paper=snapshot.get('paper') or {}
        if paper:
            text+=(f"\nGüncel sanal kasa: {paper.get('equity',0):.4f} USDT\n"
                   f"Açık işlemlerin kâr/zararı: {paper.get('unrealized',0):+.4f} USDT\n"
                   f"Kapanan işlemlerin net sonucu: {paper.get('realized',0):+.4f} USDT\n")
        else:
            text+='\n'+self.capital_text(account_id)+'\n'
        if control.get('stopped'):
            text+='Bot / AI durduruldu: yeni model çağrısı yok. Kod ile SL/TP koruması sürer.\n'
        elif not control['enabled']:
            text+='İşlem arayışı duraklatıldı: yeni analiz çağrısı yok. Kod ile pozisyon takibi sürer.\n'
        else:
            text+='Yeni işlem arayışı açık.\n'
        if not snapshot:
            return text+'Henüz piyasa gözlemi yok. Ayrıntı ve yenileme aşağıdaki düğmelerde.'
        age=max(0,time.time()-snapshot.get('observed_at',0))
        text+=f"Son durum: {human_reason(snapshot.get('reason'))}\nVeri yaşı: {age:.0f} saniye\n"
        text+=self.progress_text(snapshot)
        trace=snapshot.get('analysis_trace') or {}
        if trace:
            text+=f"Son aday: {trace.get('symbol','?')} · {trace_outcome(trace)}\n"
        real=snapshot.get('real_account')
        if real:
            text+=f"\nMEXC gerçek futures bakiyesi (yalnız okuma): {real['equity']:.4f} USDT\n"
        budget=snapshot.get('ai_budget',{})
        reserved=sum(float(row.get('amount',0)) for row in budget.get('reservations',{}).values())
        settled=max(0.,float(budget.get('total',0))-reserved)
        text+=(f"\nOrtak Claude tahmini maliyet: ${settled:.4f}\n"
               f"Sonucu belirsiz / bekleyen rezervasyon: ${reserved:.4f} (kesin harcama değil)\n"
               'Sağlayıcının gerçek bakiyesi/faturası burada okunmaz.\n')
        text+='Yenilemek için aşağıdaki düğmeleri kullan. İstek/token ayrıntısı: Son analiz / AI istekleri.'
        return text

    def _authorize(self, message, user):
        chat = message.get('chat', {})
        if chat.get('type') != 'private' or user.get('is_bot'):
            return None
        return self.identities.get((user.get('id'), chat.get('id')))

    def dispatch(self, update):
        callback = update.get('callback_query')
        if callback:
            account = self._authorize(callback.get('message', {}), callback.get('from', {}))
            if account is None:
                # Acknowledge without revealing account existence or status.
                self.call('answerCallbackQuery', callback_query_id=callback['id'], text='Yetkisiz işlem')
                return
            command = callback.get('data', '')
            if not command.startswith('control:'):
                return
            command = command.removeprefix('control:')
            self._command(account.account_id, command)
            self.call('answerCallbackQuery', callback_query_id=callback['id'])
            return
        message = update.get('message', {})
        account = self._authorize(message, message.get('from', {}))
        if account is None:
            return
        text = message.get('text', '').strip()
        parts = text.split()
        name = parts[0].split('@')[0].lower() if parts else ''
        mapping = {'/start':'menu', '/help':'menu', '/status':'status', '/durum':'status', '/balance':'status',
                   '/bakiye':'status', '/report':'report', '/history':'report', '/rapor':'report', '/settings':'settings', '/ayarlar':'settings', '/positions':'positions', '/pozisyonlar':'positions',
                   '/watchlist':'watchlist', '/takip':'watchlist', '/diagnostics':'diagnostics', '/quota':'quota', '/kotalar':'quota', '/events':'diagnostics', '/run':'start', '/baslat':'start', '/pause':'pause', '/durdur':'stop', '/stop':'stop'}
        if self.command_handler and self.command_handler(account.account_id, text, message):
            return
        if name in ('/demo','/capital'):
            if len(parts)!=2:
                self.send(account.account_id,'Kullanım: /demo 1000 (sanal USDT), /capital 1000 veya /capital full (MEXC referanslı paper).')
            elif name=='/capital' and parts[1].lower()=='full':
                self.request_capital(account.account_id,'account',allocation_mode='full')
            else:
                self.request_capital(account.account_id,'demo' if name=='/demo' else 'account',parts[1])
            return
        if self.control(account.account_id)['capital_input'] and name not in mapping and not text.startswith('/'):
            self.request_capital(account.account_id,self.control(account.account_id)['capital_mode'],text)
            return
        if name in ('/report','/rapor') and len(parts)==2:
            from datetime import date
            try:
                day=date.fromisoformat(parts[1]).isoformat()
            except ValueError:
                self.send(account.account_id,'Tarih biçimi: /report 2026-11-07')
                return
            rows=(self.snapshot_provider(account.account_id) or {}).get('reports_by_day',{}).get(day,[])
            if not rows:
                self.send(account.account_id,'Bu gün için rapor yok. Son 7 gün için /history kullan.')
                return
            from trading.runtime.reports import format_daily_report
            self.send(account.account_id,'\n'.join(row.get('version','')+'\n'+format_daily_report(row) for row in rows))
            return
        if name=='/history':
            history=(self.snapshot_provider(account.account_id) or {}).get('reports_by_day',{})
            text='Son 7 gün PAPER sonuçları (USDT)\n'
            for day,rows in sorted(history.items(),reverse=True):
                for row in rows:
                    text+=f"{day} | {row.get('version')} | Kapanan {row['closed_trades']} | Net {row['net_pnl']:+.4f}\n"
            self.send(account.account_id,text if history else 'Henüz günlük geçmiş yok.')
            return
        if name == '/close':
            if len(parts)==3:
                self.request_close(account.account_id, parts[1].lower(), parts[2].upper())
            else:
                self.send(account.account_id, 'Kullanım: /close text|vlm BTC_USDT')
            return
        if name == '/closeall':
            self.request_close(account.account_id)
            return
        if name == '/version':
            command = parts[1].lower() if len(parts)==2 else 'menu'
            self._command(account.account_id, command if command in ('text','vlm') else 'menu')
        else:
            self._command(account.account_id, mapping.get(name, 'unknown'))

    def _command(self, account_id, command):
        if command.startswith('demo_amount:'):
            state=self.control(account_id)
            amount=command.split(':',1)[1]
            if state['capital_mode']=='demo' and state['capital_input'] and amount in ('100','500','1000'):
                self.request_capital(account_id,'demo',float(amount))
            else:
                self.send(account_id,'Bu tutar düğmesi artık geçerli değil. Kasa seçimini aşağıdaki düğmelerle yenile.',buttons=True)
            return
        if command.startswith('confirmclose:'):
            self.confirm_close(account_id, command.split(':',1)[1])
            return
        if command.startswith('requestclose:'):
            token = command.split(':',1)[1]
            pending = self.store.get(f'account:{account_id}:closebutton:{token}')
            if pending and pending in self.position_targets(account_id):
                self.request_close(account_id, pending['version'], pending['symbol'])
            else:
                self.send(account_id, 'Pozisyon düğmesi geçersiz; /positions ile yenile.')
            return
        if command=='closeall':
            self.request_close(account_id)
            return
        if self.action_handler and self.action_handler(account_id, command):
            return
        if command in ('capital','capital_demo','capital_account','capital_amount','capital_full'):
            if self.control(account_id)['capital_pending']:
                self.send(account_id,'Önceki kasa değişikliği henüz tamamlanmadı. /status ile kontrol et.',buttons=True)
                return
            self.set_control(account_id,enabled=False)
            if command=='capital':
                self.set_control(account_id,capital_chosen=False,capital_input=False)
                message='Kasa seçimini değiştir. Mevcut pozisyonlar kapanmadan yeni kasa uygulanmaz. Demo veya MEXC referanslı Paper seç.'
            elif command=='capital_full':
                self.request_capital(account_id,'account',allocation_mode='full')
                return
            else:
                mode='demo' if command=='capital_demo' else 'account'
                if command=='capital_amount':
                    mode=self.control(account_id)['capital_mode'] or 'account'
                self.set_control(account_id,capital_mode=mode,capital_input=True,capital_chosen=False)
                message=('Aşağıdaki 100 / 500 / 1000 USDT sanal kasa düğmelerinden birini seç. Farklı tutar için Telegram’a yalnız sayıyı yazarak gönder. Örnek: 250. Birim USDT, TL değil. Para yatırılmaz, MEXC anahtarı gerekmez.' if mode=='demo' else
                         'Tam kullanılabilir bakiye düğmesini seç veya PAPER için ayıracağın tutarı Telegram’a yalnız sayı olarak yazarak gönder. Örnek: 1000. Birim USDT, TL değil. Gerçek emir açılmaz.')
        elif command=='start':
            state=self.control(account_id)
            if not (self.capital_ready(account_id) and state['version_chosen'] and state['profile_chosen']):
                self.set_control(account_id,enabled=False)
                message='Başlatılmadı: önce paper kasa, analiz sürümü ve trader modu seçilmeli.\n'+self.settings_text(account_id)
            else:
                self.set_control(account_id,enabled=True,stopped=False)
                message='Seçilen ayarlarla yeni paper girişleri açıldı.\n'+self.settings_text(account_id)
        elif command in ('pause','stop'):
            self.set_control(account_id,enabled=False,stopped=command=='stop')
            message=('Bot / AI durduruldu. Bu hesapta yeni model çağrısı ve yeni işlem arayışı yapılmaz. '
                     'Kod ile mevcut SL/TP koruması ve Telegram kontrolü devam eder; bunlar AI çağırmaz. '
                     'Açık pozisyonlar otomatik kapatılmaz. Tekrar başlatmak için Başlat düğmesini kullan.' if command=='stop' else
                     'İşlem arayışı duraklatıldı: yeni giriş analizi ve yeni işlem açılması durdu. '
                     'Kod ile mevcut pozisyon takibi sürer; pozisyonlar otomatik kapatılmaz. Bu takip AI çağırmaz.')
            message+=' Daha önce sağlayıcıya gönderilmiş bir istek tamamlanabilir ve ücretlenebilir; durdurma bunu geri alamaz. Sonucu yeni işlem açmak için kullanılmaz.'
        elif command in ('settings','menu'):
            if command=='settings':
                self.set_control(account_id,enabled=False,version_chosen=False,profile_chosen=False)
            message=self.settings_text(account_id)
        elif command in ('text','vlm'):
            if not self.capital_ready(account_id):
                message=self.settings_text(account_id)
            else:
                self.set_control(account_id,version=command,version_chosen=True,profile_chosen=False,enabled=False)
                message=self.settings_text(account_id)
        elif command in TRADER_MODES:
            if not self.capital_ready(account_id) or not self.control(account_id)['version_chosen']:
                message='Önce analiz sürümünü seç.\n'+self.settings_text(account_id)
            else:
                mode={k:v for k,v in TRADER_MODES[command].items() if k!='label'}
                self.set_control(account_id,**mode,profile_chosen=True,enabled=False)
                message=self.settings_text(account_id)
        elif command in ('risk005','risk01'):
            self.set_control(account_id,enabled=False)
            message='Eski risk düğmesi kullanılmıyor. /settings ile analiz sürümü ve trader modunu sırayla seç.'
        elif command=='watchlist':
            message=self.watchlist_text(account_id)
        elif command=='diagnostics':
            message=self.diagnostics_text(account_id)
        elif command=='quota':
            message=self.quota_text(account_id)
        elif command=='report':
            message=self.report_text(account_id)
        elif command in ('status','positions'):
            message=self.status_text(account_id,command=='positions')
        else:
            message=self.settings_text(account_id)
        if command=='positions':
            panel_keyboard={'inline_keyboard':[[{'text':'🔄 Açık işlemleri yenile','callback_data':'control:positions'}, {'text':'Ana menü','callback_data':'control:menu'}]]}
            self.send(account_id,message,keyboard=panel_keyboard)
            self.position_buttons(account_id)
        else:
            self.send(account_id, message, buttons=True)

    def position_targets(self, account_id):
        snapshot = self.snapshot_provider(account_id) or {}
        variants = snapshot.get('variants') or {self.control(account_id)['version']: {'positions':snapshot.get('positions',{})}}
        return [dict(version=version, symbol=symbol, opened_at=int(p['opened_at']))
                for version, view in variants.items() if version in ('text','vlm')
                for symbol,p in view.get('positions',{}).items() if p.get('opened_at') is not None]

    def position_buttons(self, account_id):
        rows = []
        for target in self.position_targets(account_id):
            token=uuid4().hex[:16]
            self.store.put(f'account:{account_id}:closebutton:{token}', target)
            rows.append([{'text': f"Kapat: {target['version']} {target['symbol']}", 'callback_data':'control:requestclose:'+token}])
        if rows:
            rows.append([{'text':'Tüm pozisyonları kapat', 'callback_data':'control:closeall'}])
            self.send(account_id,'Manuel paper kapatma: seçimin ardından onay gerekir.',keyboard={'inline_keyboard':rows})

    def request_close(self, account_id, version=None, symbol=None):
        targets=[p for p in self.position_targets(account_id) if version is None or (p['version']==version and p['symbol']==symbol)]
        if not targets:
            self.send(account_id,'Eşleşen açık pozisyon yok; /positions ile yenile.')
            return
        token=uuid4().hex[:16]
        self.store.put(f'account:{account_id}:closeconfirm:{token}',dict(targets=targets,expires_at=time.time()+120,queued=False))
        label=', '.join(p['version']+' '+p['symbol'] for p in targets)
        self.send(account_id,'PAPER pozisyon kapatma onayı: '+label+'\nGüncel yürütülebilir fiyat kullanılacak; sonuç değişebilir.',keyboard={'inline_keyboard':[[{'text':'Kapatmayı onayla','callback_data':'control:confirmclose:'+token}]]})

    def confirm_close(self, account_id, token):
        key=f'account:{account_id}:closeconfirm:{token}'
        pending=self.store.get(key)
        if not pending or pending['expires_at']<time.time():
            self.send(account_id,'Onay geçersiz veya süresi doldu; /positions ile yenile.')
            return
        if pending['queued']:
            self.send(account_id,'Kapatma isteği zaten kaydedildi.')
            return
        current=self.position_targets(account_id)
        if any(target not in current for target in pending['targets']):
            self.send(account_id,'Pozisyon değişti; kapatma iptal edildi. /positions ile yenile.')
            return
        queue=self.store.get(f'account:{account_id}:commands',[])
        for index,target in enumerate(pending['targets']):
            ident=token+':'+str(index)
            if not any(item['id']==ident for item in queue):
                queue.append(dict(id=ident,kind='close',status='pending',created_at=time.time(),**target))
        self.store.put(f'account:{account_id}:commands',queue)
        pending['queued']=True
        self.store.put(key,pending)
        self.set_control(account_id,enabled=False)
        self.send(account_id,'Manuel kapatma isteği kaydedildi; yeni girişler duraklatıldı. Sonuç ayrıca bildirilecek.')

    def enqueue(self, account_id, text, photos=(), event_id=None, buttons=False):
        """Persist a private notification. Caller supplies trusted local chart paths.

        Reuse stable event_id to avoid duplicate enqueue after an orchestrator restart.
        """
        if account_id not in self.accounts:
            raise ValueError('unknown account')
        if len(photos)>10:
            raise ValueError('maximum ten chart images')
        key = f'account:{account_id}:outbox'
        rows = self.store.get(key, [])
        ident = event_id or uuid4().hex
        if any(row['id']==ident for row in rows):
            return ident
        rows.append(dict(id=ident, text=text[:4000], photos=[str(p) for p in photos], sent=False, buttons=buttons))
        self.store.put(key, rows)
        return ident

    def _send_photos(self, account_id, paths):
        with ExitStack() as stack:
            files = {}
            for i, path in enumerate(paths):
                files[f'image{i}'] = (Path(path).name, stack.enter_context(open(path, 'rb')), 'image/png')
            if len(paths)==1:
                method = 'sendPhoto'
                data = dict(chat_id=str(self.accounts[account_id].telegram_chat_id), photo='attach://image0')
            else:
                method = 'sendMediaGroup'
                data = dict(chat_id=str(self.accounts[account_id].telegram_chat_id), media=json.dumps([
                    dict(type='photo', media=f'attach://image{i}') for i in range(len(paths))]))
            response = self.client.post(f'https://api.telegram.org/bot{self.token}/{method}', data=data, files=files)
            response.raise_for_status()
            if response.json().get('ok') is not True:
                raise ValueError('Telegram image delivery failed')

    def _flush_account(self, account_id):
        key = f'account:{account_id}:outbox'
        rows = self.store.get(key, [])
        for row in rows:
            if row['sent']:
                continue
            # Persist each delivery stage; ambiguous API timeout can duplicate a stage.
            if not row.get('text_sent'):
                self.send(account_id, row['text'], buttons=row.get('buttons',False))
                row['text_sent'] = True
                self.store.put(key, rows)
            if row['photos'] and not row.get('photos_sent'):
                self._send_photos(account_id, row['photos'])
                row['photos_sent'] = True
                self.store.put(key, rows)
            row['sent'] = True
            self.store.put(key, rows)

    def flush(self):
        failed = False
        for account_id in self.accounts:
            try:
                self._flush_account(account_id)
            except Exception as exc:
                failed = True
                logger.warning('Telegram private queue retry pending (%s)', type(exc).__name__)
                self.store.put(f'account:{account_id}:telegram_health', dict(ok=False, observed_at=time.time()))
            else:
                self.store.put(f'account:{account_id}:telegram_health', dict(ok=True, observed_at=time.time()))
        return not failed

    def tick(self):
        """One short poll iteration; exceptions are sanitized and queue is retained."""
        try:
            offset = self.store.get('control:telegram_offset', 0)
            updates = self.call('getUpdates', offset=offset, timeout=0, allowed_updates=['message','callback_query'])
            for update in updates:
                self.dispatch(update)
                self.store.put('control:telegram_offset', update['update_id']+1)
            delivered = self.flush()
            self.store.put('control:telegram_health', dict(ok=delivered, observed_at=time.time()))
        except Exception as exc:
            # Neither raw exceptions nor request URLs may expose the bot token.
            logger.warning('Telegram control retry pending (%s)', type(exc).__name__)
            self.store.put('control:telegram_health', dict(ok=False, observed_at=time.time()))
