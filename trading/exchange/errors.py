"""Safe operator diagnostics: never reflect provider messages, headers or secrets."""
class ConnectionIssue(ValueError):
    pass


def exchange_issue(code, stage):
    # Only controlled messages and integer codes are exposed to Telegram.
    try:
        code = int(code)
    except (ValueError, TypeError, OverflowError):
        code = None
    descriptions = {
        401:'API anahtarı doğrulanamadı. Key ve secret aynı kayda ait olmalı.',
        402:'API anahtarının süresi dolmuş; MEXC üzerinden yenile.',
        406:'Sunucunun çıkış IP adresi bu API anahtarının izin listesinde değil.',
        513:'Sunucu saati veya istek zaman penceresi uyuşmuyor. Sunucu saat senkronizasyonunu kontrol et.',
        602:'İmza doğrulanamadı. Key/secret eşleşmesini ve sunucu saatini kontrol et.',
        701:'Futures hesap okuma izni eksik: View Account Details seçeneğini aç.',
        703:'Futures emir/pozisyon okuma izni eksik: View Order Details seçeneğini aç.',
        510:'MEXC istek sınırına ulaşıldı. Biraz bekleyip tekrar dene.',
        511:'Bu API uç noktasına erişim reddedildi. Futures API yetkisini ve API adresini kontrol et.',
    }
    label = f'MEXC {stage}' + (f' (kod {code})' if code is not None else '')
    return ConnectionIssue(label+': '+descriptions.get(code,'İstek reddedildi; MEXC izinlerini ve hesap erişimini kontrol et.'))
