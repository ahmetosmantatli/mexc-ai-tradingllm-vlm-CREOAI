"""Explicit private-chat onboarding; secrets are never echoed or stored as plaintext.

Telegram bot messages are cloud messages. Deletion is best effort, not a claim
of end-to-end secrecy. Users may instead provision secrets on the server.
"""
import json
import time


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
                payload=json.loads(command)
                if not isinstance(payload,dict) or set(payload)!={'api_key','api_secret'}:
                    raise ValueError('format')
                self.bind(ident,payload['api_key'],payload['api_secret'])
            except Exception:
                self.controller.send(ident,'Bağlama başarısız. Açık pozisyon/analiz, anahtar izinleri, IP ve JSON biçimini kontrol et. Yeniden denemek için /connect.')
                return True
            self.controller.send(ident,'MEXC hesabı doğrulandı ve bağlandı. Yeni paper oturumu; başlatmak için /run.')
        else:
            return False
        if callback:
            self.controller.call('answerCallbackQuery',callback_query_id=callback['id'])
        return True
