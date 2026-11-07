from trading.agents.telemetry import format_trace


def test_failed_analysis_notification_does_not_look_like_a_fill():
    trace=dict(id='example',symbol='LIT_USDT',strategy='trend',status='invalid_or_unavailable_model',action='hold',
        execution_status='not_opened', attempts=[
            dict(provider='groq',model='gpt-oss',status='rejected',http_status=413,error_code='rate_limit_exceeded'),
            dict(provider='anthropic',model='sonnet',status='error_or_invalid_output',error_category='ambiguous_timeout',pending_usd=.039081)])
    text=format_trace(trace)
    assert text.startswith('İşlem açılmadı — analiz başarısız')
    assert 'LIT_USDT' in text and 'token/kota sınırına' in text
    assert 'yanıt zamanında gelmedi' in text
    assert '$0.039081' in text and 'kesin harcama değil' in text
    assert 'invalid_or_unavailable_model' not in text
    assert 'HTTP' not in text and 'rate_limit_exceeded' not in text


def test_trade_recommendation_is_not_reported_as_open_until_execution():
    trace=dict(id='a',symbol='BTC_USDT',strategy='momentum',status='completed',action='trade',attempts=[])
    assert format_trace(trace).startswith('AI işlem önerdi; risk kontrolü bekleniyor')
    trace.update(execution_status='rejected',execution_reason='HOLD_PRICE_DRIFT')
    assert format_trace(trace).startswith('İşlem açılmadı')
    assert 'fiyat fazla değişti' in format_trace(trace)
    trace['execution_status']='opened'
    assert format_trace(trace).startswith('Sanal işlem açıldı')


def test_quota_skip_distinguishes_temporary_shared_limit_from_rejection():
    base=dict(symbol='BTC_USDT',status='invalid_or_unavailable_model',action='hold',execution_status='not_opened')
    skipped=dict(provider='groq',model='model',status='quota_skipped',generation_requests=0,error_category='shared_minute_token_budget')
    assert 'ortak dakika kotası dolu' in format_trace(dict(base,attempts=[skipped]))
    rejected=dict(provider='groq',model='model',status='rejected',generation_requests=1,http_status=413,error_category='request_token_budget')
    text=format_trace(dict(base,attempts=[rejected]))
    assert 'token/kota sınırına takıldı' in text
    assert 'istek gönderilmedi' not in text
