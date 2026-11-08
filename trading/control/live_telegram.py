"""LIVE control presentation; never submits exchange orders in the poller."""
import time
from uuid import uuid4
from trading.control.telegram import TelegramController,TRADER_MODES
from trading.agents.telemetry import human_reason


def number(value, digits=4):
    if value is None:return 'doğrulanmadı'
    try:return f'{float(value):.{digits}f}'
    except (ValueError,TypeError):return 'doğrulanmadı'


class LiveTelegramController(TelegramController):
    live_mode=True
    def capital_ready(self,account_id):
        state=self.control(account_id)
        return bool(self.linked(account_id) and state.get('capital_chosen') and not state.get('capital_pending')
                    and state.get('capital_mode')=='account' and state.get('allocation_mode')=='full')

    def capital_text(self,account_id):
        return ('Canlı MEXC USDT kasası: borsadan güncel equity ve kullanılabilir marjin okunur. '
                'Kasayı görmek, tamamını tek işlemde kullanmak değildir.')

    def keyboard(self,account_id):
        state=self.control(account_id)
        grant=(self.snapshot_provider(account_id) or {}).get('automatic_grant') or {}
        expired=bool(grant.get('expires_at') and grant['expires_at']<=time.time())
        rows=[]
        if not self.linked(account_id):
            rows.append([{'text':'MEXC hesabını bağla','callback_data':'control:connect'}])
        elif not self.capital_ready(account_id):
            rows.append([{'text':'Güncel MEXC kasasını kullan','callback_data':'control:capital_full'}])
        elif not state.get('version_chosen'):
            rows.append([{'text':'Görselsiz','callback_data':'control:text'},{'text':'VLM (grafikli)','callback_data':'control:vlm'}])
        elif not state.get('profile_chosen'):
            rows.extend([[{'text':v['label'],'callback_data':'control:'+k}] for k,v in TRADER_MODES.items()])
        else:
            rows.append([{'text':'✓ Canlı arayış açık' if state.get('enabled') and not expired else '▶ Canlı arayışı başlat',
                          'callback_data':'control:status' if state.get('enabled') and not expired else 'control:start'}])
            rows.append([{'text':'Yeni işlem arayışını durdur','callback_data':'control:pause'},
                         {'text':'Bot / AI durdur','callback_data':'control:stop'}])
            rows.append([{'text':'Ayarları değiştir','callback_data':'control:settings'}])
        rows.append([{'text':'Durum / Bakiye','callback_data':'control:status'},
                     {'text':'Açık işlemler','callback_data':'control:positions'}])
        rows.append([{'text':'Takip listesi','callback_data':'control:watchlist'},
                     {'text':'Son analiz / AI','callback_data':'control:diagnostics'},
                     {'text':'İşlem raporu','callback_data':'control:report'}])
        if self.linked(account_id):rows.append([{'text':'Bağlantıyı kaldır','callback_data':'control:disconnect'}])
        return {'inline_keyboard':rows}

    def settings_text(self,account_id):
        state=self.control(account_id)
        text='LIVE · Gerçek MEXC hesabı\n'+self.capital_text(account_id)+'\n'
        if not self.linked(account_id):return text+'Önce MEXC hesabını bağla.'
        if not self.capital_ready(account_id):return text+'Güncel kasa kullanımını seç. Bu seçim para transfer etmez.'
        if not state.get('version_chosen'):return text+'Analizi seç: görselsiz veya VLM.'
        if not state.get('profile_chosen'):return text+'Trader profilini seç. Profil sınırları model önerilerinde uygulanır.'
        mode=next((v for v in TRADER_MODES.values() if v['risk_profile']==state['risk_profile']),{})
        leverage_mode=(self.snapshot_provider(account_id) or {}).get('leverage_mode','profile')
        leverage_text=('Kaldıraç: parite ve hesabın güncel borsa üst sınırı; model seçim yapar. Stop/likidasyon, marjin ve risk kontrolleri geçerlidir.\n'
            if leverage_mode=='exchange' else f"Profil kaldıraç üst sınırı: {state.get('max_leverage','?')}x; gerçek hesap/borsa sınırı daha düşük olabilir.\n")
        price_planning=(self.snapshot_provider(account_id) or {}).get('price_planning_enabled',False)
        price_text=('Giriş/SL/TP: AI kapanmış mumlara referanslı plan önerebilir; piyasa ve risk doğrulaması zorunlu.\n'
            if price_planning else 'Giriş/SL/TP: algoritma seviyeleri.\n')
        return (text+f"Analiz: {'VLM' if state['version']=='vlm' else 'Görselsiz'} | Profil: {mode.get('label','?')}\n"
            f"Planlanan işlem riski üst sınırı: %{state['risk_per_trade_pct']*100:g}\n"
            +leverage_text+price_text+('Açık pozisyon: olay bazlı AI stop taşıma/kısmi kapatma/kapatma yönetimi etkin.\n'
                if (self.snapshot_provider(account_id) or {}).get('position_management_enabled') else '')+
            'Başlat sonrasında model ve risk motoru işlem başına Telegram onayı beklemez. Başlat yetkisi 7 gün geçerlidir.\n'
            'Durdur yeni girişleri/AI çağrılarını keser; açık pozisyonların uzlaştırması ve borsa koruması sürer.')

    def request_capital(self,account_id,mode,amount=None,allocation_mode='fixed'):
        if mode!='account' or allocation_mode!='full':
            self.send(account_id,'LIVE modunda demo kasa veya sabit env seed kullanılmaz. Güncel MEXC kasasını seç.',buttons=True)
            return
        if not self.linked(account_id):
            self.send(account_id,'Önce MEXC hesabını bağla.',buttons=True);return
        state=self.control(account_id)
        if state.get('capital_pending'):
            self.send(account_id,'Önceki kasa isteği kontrol ediliyor. /status');return
        command=dict(id=uuid4().hex,kind='capital_reset',status='pending',created_at=time.time(),
                     mode='account',amount=None,allocation_mode='full')
        key=f'account:{account_id}:commands'
        self.store.put(key,self.store.get(key,[])+[command])
        self.set_control(account_id,enabled=False,stopped=True,capital_pending=command['id'],capital_input=False)
        self.send(account_id,'Canlı kasa erişimi kontrol ediliyor; sonuç ayrıca bildirilecek.')

    def positions_text(self,account_id,snapshot):
        positions=snapshot.get('positions') or {}
        text=f'Borsadan izlenen açık pozisyon: {len(positions)}\n'
        if not positions:
            if not (snapshot.get('live_account') or {}).get('ready'):
                return 'Pozisyon bilgisi henüz doğrulanmadı; sıfır pozisyon kabul edilmez.\n'
            return text+'Şu anda doğrulanmış açık pozisyon görünmüyor.\n'
        for symbol,p in list(positions.items())[:12]:
            text+=(f"\n{symbol} · {p.get('side','?')} · {p.get('leverage','?')}x\n"
                   f"Giriş: {number(p.get('entry'),6)} | SL: {number(p.get('stop'),6)} | TP: {number(p.get('target'),6)}\n"
                   f"Açık PnL: {number(p.get('unrealized'))} USDT\n")
            if p.get('price_source'):
                text+='Seviyeler: '+('AI planı' if p['price_source']=='model' else 'algoritma')+'\n'
            origin=p.get('decision_ai') or {}
            if origin:text+=f"Karar: {origin.get('provider','?')} / {origin.get('model','?')}\n"
            text+=f"Koruma: {p.get('protection_status','borsa uzlaştırması bekleniyor')}\n"
            if p.get('remaining_contracts') is not None:text+=f"Kalan kontrat: {p['remaining_contracts']} | Yönetim sürümü: {p.get('management_revision',0)}\n"
        return text

    def status_text(self,account_id,positions=False):
        snapshot=self.snapshot_provider(account_id) or {}
        state=self.control(account_id)
        account=snapshot.get('live_account') or {}
        balance=account.get('balance') or snapshot.get('balance') or {}
        text=(f"LIVE · {account_id}\nGerçek hesap özkaynağı: {number(balance.get('equity'))} USDT\n"
              f"Kullanılabilir marjin: {number(balance.get('available'))} USDT\n"
              f"Pozisyon marjini: {number(balance.get('position_margin'))} USDT\n"
              f"Açık PnL: {number(balance.get('unrealized'))} USDT\n")
        text+=self.positions_text(account_id,snapshot)
        scan=snapshot.get('scan_summary') or {}
        universe=scan.get('universe') or {}
        if universe:text+=f"Tarama: bu tur {scan.get('scanned','?')} / evren {universe.get('total_symbols','?')} parite; dönüşümlü kapsam.\n"
        capacity=snapshot.get('analysis_capacity') or {}
        if capacity:text+=f"Yerel kota tahminine göre paralel analiz: {capacity.get('parallel_candidates',0)} (ücretsiz {capacity.get('free_slots',0)}, ücretli yedek {capacity.get('paid_slots',0)}).\n"

        management=snapshot.get('management_status') or {}
        if snapshot.get('position_management_enabled'):
            text+='Açık pozisyon AI yönetimi etkin (olay bazlı).\n'
            if management:text+=f"Son yönetim: {management.get('symbol','?')} · {management.get('reason','henüz değerlendirme yok')}\n"
        text+=('Yeni AI/işlem arayışı açık.\n' if state.get('enabled') and not state.get('stopped') else
               'Yeni AI/işlem arayışı kapalı; açık pozisyon uzlaştırması sürer.\n')
        reason=snapshot.get('reason','')
        labels={'HOLD_LIVE_AI_CAPACITY':'AI kapasitesi/kota bekleniyor; yeni istek gönderilmiyor.', 'LIVE_RECONCILED':'Hesap ve işlemler borsayla uzlaştırıldı.', 'LIVE_PROTECTED':'Gerçek işlem açıldı; borsa SL/TP koruması doğrulandı.',
            'LIVE_PAUSED':'Yeni girişler kapalı; uzlaştırma devam ediyor.', 'HOLD_LIVE_LOSS_CIRCUIT_BREAKER':'Kayıp/düşüş sınırı aşıldı; yeni giriş ve analiz bekliyor.', 'HOLD_LIVE_NO_FUNDS':'Kullanılabilir gerçek bakiye bekleniyor.', 'HOLD_LIVE_NO_CANDIDATE':'Güncel uygun aday bekleniyor.',
            'HOLD_LIVE_MODEL':'AI işlem açmadı; son analiz sonucuna bakabilirsin.', 'HOLD_LIVE_START_REQUIRED':'Başlat yetkisi yok veya süresi doldu; yeni analiz yapılmıyor. Yeniden Başlat kullan.', 'HOLD_LIVE_ERROR':'Yeni işlem açılmadı; veri veya yürütme kontrolü bekliyor.',
            'HOLD_LIVE_RECONCILIATION':'Borsa durumu doğrulanamadı; yeni girişler bekliyor.', 'HOLD_LIVE_POSITION_LIMIT':'Açık pozisyon sınırına ulaşıldı.',
            'HOLD_LIVE_WATCH_CHANGED':'Parite planı değişti; eski kararla işlem açılmadı.', 'HOLD_LIVE_CONTROL_CHANGED':'Ayarlar değişti; eski karar uygulanmadı.'}
        entry_reason=(snapshot.get('entry_status') or {}).get('reason')
        if entry_reason and entry_reason!=reason:text+='İşlem arayışı: '+labels.get(entry_reason,human_reason(entry_reason))+'\n'
        text+=f"Son durum: {snapshot.get('message') or labels.get(reason,'Canlı veri ve risk kontrolleri sürüyor; ayrıntı Son analiz ekranında.')}\n"
        if account.get('observed_at'):text+=f"Borsa verisinin yaşı: {max(0,time.time()-account['observed_at']):.0f} sn\n"
        if snapshot.get('halt') or snapshot.get('live_halt'):text+='Yeni işlem engeli var; belirsiz emir/koruma durumunu kontrol et.\n'
        return text+self.progress_text(snapshot)

    def watchlist_text(self,account_id):
        return super().watchlist_text(account_id).replace('Takip listesi · PAPER','Takip listesi · LIVE',1)

    def diagnostics_text(self,account_id):
        text=super().diagnostics_text(account_id).replace('PAPER','LIVE')
        trace=(self.snapshot_provider(account_id) or {}).get('management_trace') or {}
        if trace:
            text+=f"\nAçık pozisyon AI yönetimi: {trace.get('symbol','?')}\nModel: {trace.get('provider','?')} / {trace.get('model','?')}\n"
            actions={'hold':'Koru / bekle','move_stop':'Stop taşı','reduce':'Kısmi kapat','close':'Kapat'}
            text+=f"Karar: {actions.get(trace.get('action'), 'Yanıt veya doğrulama bekleniyor')}\n{trace.get('rationale','')}\n"
            for attempt in trace.get('attempts',[]):
                text+=f"{attempt.get('provider','?')}: {attempt.get('status','?')} | Girdi/çıktı token: {attempt.get('input_tokens','?')}/{attempt.get('output_tokens','?')}\n"
            text+='Yönetim kararı, borsada gerçekleşme anlamına gelmez; doğrulanmış aksiyon ayrıca bildirilir.\n'
        return text

    def report_text(self,account_id):
        rows=(self.snapshot_provider(account_id) or {}).get('closed',[])
        text='LIVE · Borsada doğrulanan kapanışlar\n'
        if not rows:return text+'Henüz doğrulanmış kapanış kaydı yok.'
        for row in rows[-12:]:
            text+=f"{row.get('symbol','?')} | Net: {number(row.get('net_pnl',row.get('realised')))} USDT\n"
        return text+'Eksik ücret/funding bilgisi sıfır kabul edilmez.'

    def _command(self,account_id,command):
        if command.startswith(('confirmclose:','requestclose:')) or command=='closeall':
            return super()._command(account_id,command)
        if self.action_handler and self.action_handler(account_id,command):return
        if command in ('capital','capital_full','capital_account'):
            return self.request_capital(account_id,'account',allocation_mode='full')
        if command in ('capital_demo','capital_amount') or command.startswith('demo_amount:'):
            self.send(account_id,'Bu LIVE panelinde yalnız güncel MEXC kasası kullanılır.',buttons=True);return
        if command in ('start','run'):
            state=self.control(account_id)
            if not (self.capital_ready(account_id) and state.get('version_chosen') and state.get('profile_chosen')):
                self.send(account_id,self.settings_text(account_id),buttons=True);return
            self.set_control(account_id,enabled=True,stopped=False)
            text='Canlı işlem arayışı istendi. Güncel hesap, risk ve koruma kontrolleri geçerse model önerileri yürütülür.'
        elif command in ('pause','stop'):
            self.set_control(account_id,enabled=False,stopped=command=='stop')
            text='Yeni girişler ve açık pozisyonlar için AI yönetim çağrıları durdu. Açık pozisyonların borsa SL/TP koruması ve uzlaştırması sürer; bu düğme pozisyonları kapatmaz.'
        elif command in ('text','vlm'):
            self.set_control(account_id,enabled=False,version=command,version_chosen=True,profile_chosen=False)
            text=self.settings_text(account_id)
        elif command in TRADER_MODES:
            state=self.control(account_id)
            if state.get('version_chosen') and self.capital_ready(account_id):
                self.set_control(account_id,enabled=False,profile_chosen=True,**{k:v for k,v in TRADER_MODES[command].items() if k!='label'})
            text=self.settings_text(account_id)
        elif command=='settings':
            self.set_control(account_id,enabled=False,version_chosen=False,profile_chosen=False)
            text=self.settings_text(account_id)
        elif command=='positions':
            self.send(account_id,self.status_text(account_id,True),buttons=True)
            self.position_buttons(account_id);return
        elif command=='status':text=self.status_text(account_id)
        elif command=='report':text=self.report_text(account_id)
        elif command=='diagnostics':text=self.diagnostics_text(account_id)
        elif command=='watchlist':text=self.watchlist_text(account_id)
        else:text=self.settings_text(account_id)
        self.send(account_id,text,buttons=True)

    def dispatch(self,update):
        message=update.get('message',{})
        account=self._authorize(message,message.get('from',{})) if message else None
        text=message.get('text','').strip()
        if account and text.split(' ',1)[0].split('@')[0].lower() in ('/history','/report','/rapor'):
            self.send(account.account_id,self.report_text(account.account_id),buttons=True);return
        return super().dispatch(update)

    def position_targets(self,account_id):
        snapshot=self.snapshot_provider(account_id) or {}
        return [dict(version=p.get('analysis_version',self.control(account_id)['version']),symbol=symbol,
            opened_at=int(p.get('opened_at') or 0),intent_id=p['intent_id'],position_id=p.get('position_id')) for symbol,p in snapshot.get('positions',{}).items()
            if p.get('intent_id')]

    def position_buttons(self,account_id):
        rows=[]
        for target in self.position_targets(account_id):
            token=uuid4().hex[:16];self.store.put(f'account:{account_id}:closebutton:{token}',target)
            rows.append([{'text':'Kapat: '+target['symbol'],'callback_data':'control:requestclose:'+token}])
        if rows:self.send(account_id,'Gerçek pozisyon kapatma seçimi; ayrıca onay istenir.',keyboard={'inline_keyboard':rows})

    def request_close(self,account_id,version=None,symbol=None):
        targets=[p for p in self.position_targets(account_id) if version is None or (p['version']==version and p['symbol']==symbol)]
        if not targets:
            self.send(account_id,'Eşleşen doğrulanmış pozisyon yok. /positions');return
        token=uuid4().hex[:16]
        self.store.put(f'account:{account_id}:closeconfirm:{token}',dict(targets=targets,expires_at=time.time()+120,queued=False))
        self.send(account_id,'Gerçek pozisyon kapatma isteği: '+', '.join(t['symbol'] for t in targets),
            keyboard={'inline_keyboard':[[{'text':'Kapatmayı onayla','callback_data':'control:confirmclose:'+token}]]})
