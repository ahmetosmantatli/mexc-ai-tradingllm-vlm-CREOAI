"""One poller per bot token. Durable allowlisted commands and at-least-once outbox.

Run on a dedicated control thread with its own RuntimeStore connection, never on
position protection's hot path. Account status is supplied by the orchestrator.
"""
from contextlib import ExitStack
import json
import logging
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
        defaults=dict(enabled=False, version='text', version_chosen=False, profile_chosen=False, risk_profile=None, risk_per_trade_pct=.01)
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

    def keyboard(self, account_id):
        control=self.control(account_id)
        rows=[]
        if not self.linked(account_id):
            rows.append([{'text':'1 · MEXC hesabını bağla', 'callback_data':'control:connect'}])
        elif not control['version_chosen']:
            rows.append([{'text':'2 · Görselsiz', 'callback_data':'control:text'}, {'text':'2 · VLM (grafikli)', 'callback_data':'control:vlm'}])
        elif not control['profile_chosen']:
            rows.extend([[{'text':mode['label'], 'callback_data':'control:'+key}] for key,mode in TRADER_MODES.items()])
        else:
            rows.append([{'text':'✓ Çalışıyor' if control['enabled'] else '▶ Ayarlarla başlat', 'callback_data':'control:status' if control['enabled'] else 'control:start'}, {'text':'⏹ Botu durdur', 'callback_data':'control:stop'}])
            rows.append([{'text':'Ayarları değiştir', 'callback_data':'control:settings'}])
        rows.append([{'text':'Durum / Bakiye', 'callback_data':'control:status'}, {'text':'Pozisyonlar', 'callback_data':'control:positions'}])
        if self.linked(account_id):
            rows.append([{'text':'✓ MEXC bağlı', 'callback_data':'control:status'}, {'text':'Bağlantıyı kaldır', 'callback_data':'control:disconnect'}])
        return {'inline_keyboard':rows}

    def settings_text(self, account_id):
        state=self.control(account_id)
        if not self.linked(account_id):
            return '1/4 · MEXC bağlantısı\nÖnce MEXC hesabını bağla. Bağlantı doğrulanmadan işlem başlatılmaz.'
        if not state['version_chosen']:
            return '2/4 · Analiz sürümü\nGörselsiz: sayısal mum/strateji verileri.\nVLM: aynı veriler + 4s/15dk/5dk grafikleri. Birini seç.'
        if not state['profile_chosen']:
            return f"3/4 · Trader modu\nAnaliz: {'VLM (grafikli)' if state['version']=='vlm' else 'Görselsiz'}\nSermayeyi koru, Dengeli veya Agresif seç. Modlar kazanç garantisi değildir."
        mode=next((m for m in TRADER_MODES.values() if m['risk_profile']==state['risk_profile']),None)
        if mode is None:
            return 'Trader modu yeniden seçilmeli. /settings'
        risk=state['risk_per_trade_pct']*100
        return (f"4/4 · Ayar özeti | PAPER\nMEXC: bağlı\nAnaliz: {'VLM (grafikli)' if state['version']=='vlm' else 'Görselsiz'}\n"
                f"Trader modu: {mode['label']}\nİşlem başına planlanan azami stop kaybı: %{risk:g}\n"
                f"Örnek: 500 TL eşdeğeri kasada yaklaşık {500*state['risk_per_trade_pct']:g} TL planlanan risk. Bu, yatırılacak teminat yüzdesi değildir.\n"
                f"Kaldıraç üst sınırı: {state['max_leverage']:g}x; parite/teminat/stop koşulları daha düşük değer gerektirebilir.\n"
                f"Azami açık pozisyon: {state['max_positions']}\nGünlük kayıp eşiği: %{state['daily_loss_limit_pct']*100:g} | Zirveden düşüş: %{state['drawdown_limit_pct']*100:g}\n"
                'Fiyat boşluklarında kayıp planı aşabilir. Uygun fırsatta işlem açılır; işlem zorunlu değil. '
                + ('Bot bu ayarlarla çalışıyor.' if state['enabled'] else 'Başlat ile onayla.'))

    def send(self, account_id, text, buttons=False, keyboard=None):
        data = dict(chat_id=self.accounts[account_id].telegram_chat_id, text=text[:4000])
        if buttons or keyboard is not None:
            data['reply_markup'] = keyboard if keyboard is not None else self.keyboard(account_id)
        return self.call('sendMessage', **data)

    def status_text(self, account_id, positions=False):
        account = self.accounts[account_id]
        control = self.control(account_id)
        snapshot = self.snapshot_provider(account_id)
        text = (f'Hesap: {account.account_id} | PAPER\n'
                f"MEXC: {'bağlı' if self.linked(account_id) else 'bağlı değil'}\n"
                f"Yeni işlem: {'açık' if control['enabled'] else 'duraklatıldı'} | Sürüm: {control['version']}\n"
                f"Planlanan risk: %{100*control['risk_per_trade_pct']:g}\n")
        if not snapshot:
            return text+'Henüz worker gözlemi yok.\nBaşlat düğmesi canlı emri etkinleştirmez.'
        observed = snapshot.get('observed_at', 0)
        age = max(0, time.time()-observed)
        text += f"Son gözlem: {age:.0f} saniye önce | {snapshot.get('reason','bilinmiyor')}\n"
        real = snapshot.get('real_account')
        if real:
            account_age = max(0, time.time()-real.get('observed_at', 0))
            text += (f"Gerçek futures: {real['equity']:.4f} USDT | Kullanılabilir: {real['available']:.4f} USDT\n"
                     f"Hesap verisi yaşı: {account_age:.0f} saniye\n")
        paper = snapshot.get('paper')
        if paper:
            text += (f"Paper: {paper['equity']:.4f} USDT | Açık PnL: {paper.get('unrealized',0):+.4f}\n"
                     f"Gerçekleşen: {paper.get('realized',0):+.4f} | Açık pozisyon: {paper.get('open_positions',0)}\n")
        budget = snapshot.get('ai_budget', {})
        text += f"Ortak AI harcanan/ayrılan tahmini: ${budget.get('total',0):.4f}\n"
        for version, view in snapshot.get('variants', {}).items():
            metric=view.get('paper')
            if metric:
                text += f"{version}: {metric['equity']:.4f} USDT | Pozisyon: {metric['open_positions']}\n"
        if positions:
            for version, view in snapshot.get('variants', {}).items():
                if version != control['version']:
                    for symbol, p in view.get('positions', {}).items():
                        text += f"{version} {symbol} {p['side']} | PnL {p.get('unrealized',0):+.4f} | SL {p.get('stop')} TP {p.get('target')}\n"
            for symbol, p in snapshot.get('positions', {}).items():
                text += (f"{symbol} {p['side']} | Giriş {p['entry']:.6g} | SL {p['stop']:.6g} | TP {p['target']:.6g}\n"
                         f"Açık PnL {p.get('unrealized',0):+.4f} USDT\n")
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
                   '/bakiye':'status', '/settings':'settings', '/ayarlar':'settings', '/positions':'positions', '/pozisyonlar':'positions',
                   '/run':'start', '/baslat':'start', '/pause':'pause', '/durdur':'stop', '/stop':'stop'}
        if self.command_handler and self.command_handler(account.account_id, text, message):
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
        if command=='start':
            state=self.control(account_id)
            if not (self.linked(account_id) and state['version_chosen'] and state['profile_chosen']):
                self.set_control(account_id,enabled=False)
                message='Başlatılmadı: önce bağlantı, analiz sürümü ve trader modu seçilmeli.\n'+self.settings_text(account_id)
            else:
                self.set_control(account_id,enabled=True,stopped=False)
                message='Seçilen ayarlarla yeni paper girişleri açıldı.\n'+self.settings_text(account_id)
        elif command in ('pause','stop'):
            self.set_control(account_id,enabled=False,stopped=command=='stop')
            message='Yeni girişler duraklatıldı. Mevcut pozisyon takibi devam eder; pozisyonlar otomatik kapatılmaz.'
        elif command in ('settings','menu'):
            if command=='settings':
                self.set_control(account_id,enabled=False,version_chosen=False,profile_chosen=False)
            message=self.settings_text(account_id)
        elif command in ('text','vlm'):
            if not self.linked(account_id):
                message=self.settings_text(account_id)
            else:
                self.set_control(account_id,version=command,version_chosen=True,profile_chosen=False,enabled=False)
                message=self.settings_text(account_id)
        elif command in TRADER_MODES:
            if not self.linked(account_id) or not self.control(account_id)['version_chosen']:
                message='Önce analiz sürümünü seç.\n'+self.settings_text(account_id)
            else:
                mode={k:v for k,v in TRADER_MODES[command].items() if k!='label'}
                self.set_control(account_id,**mode,profile_chosen=True,enabled=False)
                message=self.settings_text(account_id)
        elif command in ('risk005','risk01'):
            self.set_control(account_id,enabled=False)
            message='Eski risk düğmesi kullanılmıyor. /settings ile analiz sürümü ve trader modunu sırayla seç.'
        elif command in ('status','positions'):
            message=self.status_text(account_id,command=='positions')
        else:
            message=self.settings_text(account_id)
        self.send(account_id, message, buttons=True)
        if command=='positions':
            self.position_buttons(account_id)

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

    def enqueue(self, account_id, text, photos=(), event_id=None):
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
        rows.append(dict(id=ident, text=text[:4000], photos=[str(p) for p in photos], sent=False))
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
                self.send(account_id, row['text'])
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
