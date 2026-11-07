"""Explicit private-chat onboarding; secrets are never echoed or stored as plaintext.

Telegram bot messages are cloud messages. Deletion is best effort, not a claim
of end-to-end secrecy. Users may instead provision secrets on the server.
"""
import json
import time
from trading.exchange.errors import ConnectionIssue


class ConnectionFlow:
    def __init__(self, controller, vault, bind, disconnect):
        self.controller, self.vault = controller, vault
        self.bind, self.disconnect = bind, disconnect

    def handle(self, update):
        callback=update.get('callback_query')
        message=callback.get('message',{}) if callback else update.get('message',{})
        user=callback.get('from',{}) if callback else message.get('from',{})
        account=self.controller._authorize(message,user)
        if account is None:
            return False
        ident=account.account_id
        command=callback.get('data','') if callback else message.get('text','').strip()
        if command in ('/connect','/bagla','control:connect'):
            if self.controller.store.get(f'account:{ident}:linked',False) and not self.controller.store.get(f'account:{ident}:disconnected',False):
                self.controller.send(ident,'MEXC zaten bağlı. Hesap değiştirmek için önce bağlantıyı kaldır.',buttons=True)
                return True
            if self.vault is None:
                self.controller.send(ident,'Hesap bağlama için sunucuda ACCOUNT_VAULT_KEY ayarlanmalı.')
                return True
            self.controller.set_control(ident,enabled=False)
            self.controller.store.put(f'account:{ident}:connect_until',time.time()+180)
            self.controller.send(ident,'MEXC bağlama: 3 dakika içinde tek mesajda JSON gönder:\n'
                '{"api_key":"...","api_secret":"..."}\n'
                'Telegram bot mesajları uçtan uca şifreli değildir. Mesajı silmeyi deneyeceğim; '
                'sunucuda şifreli saklanır. Yalnız futures okuma izinli anahtar kullan. '
                'Alternatif: anahtarları sunucudan tanımla. İptal: /cancel')
        elif command in ('/disconnect','/baglantiyikes','control:disconnect'):
            self.controller.set_control(ident,enabled=False)
            try:
                self.disconnect(ident)
            except Exception as exc:
                self.controller.send(ident,str(exc) if isinstance(exc,ValueError) else 'Bağlantı kaldırılamadı; mevcut pozisyonları ve işlemi kontrol et.')
                return True
            self.controller.store.put(f'account:{ident}:linked',False)
            self.controller.send(ident,'MEXC bağlantısı kaldırıldı. Yeni anahtar için /connect. Borsadaki API anahtarı iptal edilmez.')
        elif command=='/cancel':
            self.controller.store.put(f'account:{ident}:connect_until',0)
            self.controller.send(ident,'Hesap bağlama iptal edildi.')
        elif not callback and self.controller.store.get(f'account:{ident}:connect_until',0)>time.time() and not command.startswith('/'):
            # Forget pending state before validation; never retain secret input in queues.
            self.controller.store.put(f'account:{ident}:connect_until',0)
            try:
                self.controller.call('deleteMessage',chat_id=account.telegram_chat_id,message_id=message['message_id'])
            except Exception:
                self.controller.send(ident,'Anahtar mesajı silinemedi; sohbetinden elle sil.')
            try:
                raw=command.strip()
                if raw.startswith('```') and raw.endswith('```'):
                    raw=raw.split('\n',1)[-1].rsplit('```',1)[0].strip()
                try:
                    payload=json.loads(raw)
                except (ValueError,TypeError):
                    raise ConnectionIssue('JSON okunamadı. Tek mesajda düz çift tırnaklarla api_key ve api_secret alanlarını gönder.') from None
                if not isinstance(payload,dict) or set(payload)!={'api_key','api_secret'}:
                    raise ConnectionIssue('JSON içinde yalnız api_key ve api_secret alanları olmalı.')
                values=[payload['api_key'],payload['api_secret']]
                if not all(isinstance(v,str) and 8<=len(v.strip())<=512 and not any(ch.isspace() for ch in v.strip()) for v in values):
                    raise ConnectionIssue('Key/secret boş, çok kısa veya içinde boşluk var. İki değeri MEXC kaydından yeniden kopyala.')
                self.bind(ident,values[0].strip(),values[1].strip())
            except ConnectionIssue as exc:
                self.controller.send(ident,'Bağlama başarısız: '+str(exc)+' Yeniden denemek için /connect.')
                return True
            except Exception:
                self.controller.send(ident,'Bağlama başarısız. Açık pozisyon/analiz, anahtar izinleri, IP ve JSON biçimini kontrol et. Yeniden denemek için /connect.')
                return True
            self.controller.store.put(f'account:{ident}:linked',True)
            self.controller.set_control(ident,enabled=False,version_chosen=False,profile_chosen=False,risk_profile=None)
            self.controller.send(ident,'MEXC hesabı doğrulandı ve bağlandı. Şimdi analiz sürümünü seç: VLM veya Görselsiz.',buttons=True)
        elif not callback and ('api_secret' in command or 'api_key' in command):
            try:
                self.controller.call('deleteMessage',chat_id=account.telegram_chat_id,message_id=message['message_id'])
            except Exception:
                self.controller.send(ident,'Anahtar mesajını sohbetinden elle sil.')
            self.controller.send(ident,'Bu bilgiler kaydedilmedi: bağlantı oturumu yok veya 3 dakikalık süre dolmuş. Önce /connect yaz, sonra bilgileri gönder.')
            return True
        else:
            return False
        if callback:
            self.controller.call('answerCallbackQuery',callback_query_id=callback['id'])
        return True
