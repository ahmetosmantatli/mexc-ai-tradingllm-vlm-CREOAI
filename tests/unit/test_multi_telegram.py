import json
from pathlib import Path

import httpx
import pytest

from trading.control import Account, TelegramController, load_accounts
from trading.runtime.store import RuntimeStore


def accounts():
    return [Account('a',11,11,'key-a','secret-a'),Account('b',22,22,'key-b','secret-b')]


def controller(tmp_path, snapshots=None):
    calls = []
    def handler(request):
        if 'application/json' in request.headers.get('content-type',''):
            data = json.loads(request.content)
        else:
            data = dict(raw=request.content.decode(errors='replace'))
        calls.append((request.url.path.rsplit('/',1)[-1],data))
        return httpx.Response(200,json={'ok':True,'result':[]})
    ctl=TelegramController('fake',accounts(),RuntimeStore(str(tmp_path/'s.sqlite3')),
        snapshot_provider=lambda ident:(snapshots or {}).get(ident),
        client=httpx.Client(transport=httpx.MockTransport(handler)))
    return ctl,calls


def ready(ctl, ident):
    ctl.store.put(f'account:{ident}:linked',True)
    ctl.set_control(ident,capital_mode='account',capital_amount=1000.,allocation_mode='fixed',capital_chosen=True)
    ctl._command(ident,'vlm')
    ctl._command(ident,'trader_balanced')


def message(user,chat,text,chat_type='private'):
    return dict(message={'from':dict(id=user), 'chat':dict(id=chat,type=chat_type),'text':text})


def callback(user,chat,data):
    return {'callback_query':dict(id='cb1',data=data,**{'from':dict(id=user)},message=dict(chat=dict(id=chat,type='private')))}


def test_registry_only_env_secrets_and_no_secret_repr(tmp_path):
    path=tmp_path/'accounts.json'
    path.write_text(json.dumps({'accounts':[dict(id='a',telegram_user_id=11,telegram_chat_id=11,mexc_key_env='A_KEY',mexc_secret_env='A_SECRET')]}))
    result=load_accounts(str(path),{'A_KEY':'private-key','A_SECRET':'private-secret'})
    assert result[0].mexc_api_secret=='private-secret'
    assert 'private-secret' not in repr(result[0])
    assert 'private-key' not in repr(result[0])
    assert load_accounts(str(path),{})[0].mexc_api_key==''
    with pytest.raises(ValueError,match='partial'):
        load_accounts(str(path),{'A_KEY':'key-only'})
    path.write_text(json.dumps({'accounts':[dict(id='a',telegram_user_id=11,telegram_chat_id=11,mexc_api_key='bad')]}))
    with pytest.raises(ValueError,match='references'):
        load_accounts(str(path),{})


def test_registry_rejects_duplicate_users_group_chats(tmp_path):
    row=dict(id='a',telegram_user_id=11,telegram_chat_id=11,mexc_key_env='A_KEY',mexc_secret_env='A_SECRET')
    path=tmp_path/'accounts.json'
    path.write_text(json.dumps({'accounts':[row,dict(row,id='b')]}))
    with pytest.raises(ValueError,match='duplicate'):
        load_accounts(str(path),{'A_KEY':'k','A_SECRET':'s'})
    path.write_text(json.dumps({'accounts':[dict(row,telegram_chat_id=-123)]}))
    with pytest.raises(ValueError,match='private'):
        load_accounts(str(path),{'A_KEY':'k','A_SECRET':'s'})


def test_controls_are_persistent_and_account_isolated(tmp_path):
    ctl,calls=controller(tmp_path)
    ready(ctl,'a')
    assert ctl.control('a')['enabled'] is False
    ctl.dispatch(message(11,11,'/run'))
    assert ctl.control('a')['enabled']
    assert not ctl.control('b')['enabled']
    ctl.dispatch(callback(11,11,'control:vlm'))
    assert ctl.control('a')['version']=='vlm'
    assert not ctl.control('a')['enabled']
    assert ctl.control('b')['version']=='text'
    restored=TelegramController('fake',accounts(),RuntimeStore(str(tmp_path/'s.sqlite3')))
    assert restored.control('a')['version']=='vlm'
    assert calls[-2][0]=='sendMessage'
    assert calls[-1][0]=='answerCallbackQuery'


def test_unauthorized_users_chats_and_callbacks_never_read_or_mutate(tmp_path):
    ctl,calls=controller(tmp_path)
    for update in [message(99,99,'/status'), message(11,22,'/run'),message(11,11,'/run','group'), callback(99,11,'control:start'),callback(11,22,'control:positions')]:
        ctl.dispatch(update)
    assert not ctl.control('a')['enabled'] and not ctl.control('b')['enabled']
    assert all(method=='answerCallbackQuery' for method,data in calls)
    assert all('equity' not in str(data) for method,data in calls)


def test_secret_submission_never_echoed_or_stored(tmp_path):
    ctl,calls=controller(tmp_path)
    ctl.dispatch(message(11,11,'my-api-secret-value'))
    assert 'my-api-secret-value' not in str(calls)
    rows=ctl.store.db.execute('SELECT value FROM state').fetchall()
    assert 'my-api-secret-value' not in str(rows)


def test_private_status_contains_only_owners_data(tmp_path):
    snapshot=lambda equity:dict(observed_at=1,reason='HOLD',real_account=dict(equity=equity,available=equity,observed_at=1),paper=dict(equity=equity-1),positions={},ai_budget=dict(total=2))
    ctl,calls=controller(tmp_path,{'a':snapshot(10),'b':snapshot(999)})
    ctl.dispatch(message(11,11,'/balance'))
    data=calls[-1][1]
    assert data['chat_id']==11
    assert '10.0000' in data['text']
    assert '999' not in data['text']


def test_event_outbox_dedup_photo_upload_and_account_route(tmp_path):
    ctl,calls=controller(tmp_path)
    photos=[]
    for i in range(3):
        photo=tmp_path/f'{i}.png';photo.write_bytes(b'fake-png')
        photos.append(photo)
    ctl.enqueue('b','Trade open',photos,event_id='event1')
    ctl.enqueue('b','Trade open',photos,event_id='event1')
    ctl.flush()
    assert [method for method,_ in calls]==['sendMessage','sendMediaGroup']
    assert calls[0][1]['chat_id']==22
    assert 'attach://image2' in calls[1][1]['raw']
    ctl.flush()
    assert len(calls)==2
    assert ctl.store.get('account:a:outbox') is None


def test_delivery_failure_retains_pending_event(tmp_path):
    ctl,_=controller(tmp_path)
    ctl.client=httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(500)))
    ctl.enqueue('a','Open',event_id='trade')
    ctl.tick()
    assert not ctl.store.get('account:a:outbox')[0]['sent']
    assert not ctl.store.get('control:telegram_health')['ok']


def test_pause_does_not_claim_positions_closed(tmp_path):
    ctl,calls=controller(tmp_path)
    ctl.dispatch(callback(11,11,'control:pause'))
    assert 'pozisyonlar otomatik kapatılmaz' in calls[0][1]['text']


def test_one_recipient_failure_does_not_block_another(tmp_path):
    ctl,calls=controller(tmp_path)
    def handler(request):
        data=json.loads(request.content)
        if data['chat_id']==11:
            return httpx.Response(403,json={'ok':False})
        calls.append(('sendMessage',data))
        return httpx.Response(200,json={'ok':True,'result':{}})
    ctl.client=httpx.Client(transport=httpx.MockTransport(handler))
    ctl.enqueue('a','first')
    ctl.enqueue('b','second')
    assert not ctl.flush()
    assert not ctl.store.get('account:a:outbox')[0]['sent']
    assert ctl.store.get('account:b:outbox')[0]['sent']


def test_stop_pauses_without_closing_and_start_resumes(tmp_path):
    ctl,calls=controller(tmp_path)
    ready(ctl,'a')
    ctl.dispatch(message(11,11,'/run'))
    ctl.dispatch(message(11,11,'/stop'))
    assert not ctl.control('a')['enabled'] and ctl.control('a')['stopped']
    assert ctl.store.get('account:a:commands') is None
    ctl.dispatch(message(11,11,'/run'))
    assert ctl.control('a')['enabled'] and not ctl.control('a')['stopped']


def test_manual_close_requires_owner_confirmation_and_survives_restart(tmp_path):
    snapshots={'a':dict(variants={'text':dict(positions={'BTC_USDT':dict(opened_at=1000)})})}
    ctl,calls=controller(tmp_path,snapshots)
    ctl.dispatch(message(11,11,'/close text BTC_USDT'))
    assert ctl.store.get('account:a:commands') is None
    button=calls[-1][1]['reply_markup']['inline_keyboard'][0][0]['callback_data']
    ctl.dispatch(callback(22,22,button))
    assert ctl.store.get('account:a:commands') is None
    ctl.dispatch(callback(11,11,button))
    queue=ctl.store.get('account:a:commands')
    assert len(queue)==1
    assert queue[0]['kind']=='close' and queue[0]['status']=='pending'
    assert queue[0]['version']=='text' and queue[0]['symbol']=='BTC_USDT' and queue[0]['opened_at']==1000
    assert not ctl.control('a')['enabled']
    ctl.dispatch(callback(11,11,button))
    assert len(ctl.store.get('account:a:commands'))==1
    assert RuntimeStore(str(tmp_path/'s.sqlite3')).get('account:a:commands')==queue


def test_stale_close_confirmation_cannot_close_replacement_position(tmp_path):
    snapshots={'a':dict(variants={'text':dict(positions={'BTC_USDT':dict(opened_at=1000)})})}
    ctl,calls=controller(tmp_path,snapshots)
    ctl.dispatch(message(11,11,'/close text BTC_USDT'))
    button=calls[-1][1]['reply_markup']['inline_keyboard'][0][0]['callback_data']
    snapshots['a']['variants']['text']['positions']['BTC_USDT']['opened_at']=2000
    ctl.dispatch(callback(11,11,button))
    assert ctl.store.get('account:a:commands') is None
    assert 'değişti' in calls[-2][1]['text']


def test_closeall_captures_each_version_and_other_account_untouched(tmp_path):
    snapshots={'a':dict(variants={'text':dict(positions={'BTC_USDT':dict(opened_at=1000)}),'vlm':dict(positions={'ETH_USDT':dict(opened_at=2000)})})}
    ctl,calls=controller(tmp_path,snapshots)
    ctl.dispatch(message(11,11,'/closeall'))
    button=calls[-1][1]['reply_markup']['inline_keyboard'][0][0]['callback_data']
    ctl.dispatch(callback(11,11,button))
    queue=ctl.store.get('account:a:commands')
    assert {(row['version'],row['symbol']) for row in queue}=={('text','BTC_USDT'),('vlm','ETH_USDT')}
    assert ctl.store.get('account:b:commands') is None


def test_expired_confirmation_does_not_queue(tmp_path,monkeypatch):
    snapshots={'a':dict(variants={'text':dict(positions={'BTC_USDT':dict(opened_at=1000)})})}
    ctl,calls=controller(tmp_path,snapshots)
    ctl.dispatch(message(11,11,'/closeall'))
    button=calls[-1][1]['reply_markup']['inline_keyboard'][0][0]['callback_data']
    token=button.split(':')[-1]
    key=f'account:a:closeconfirm:{token}'
    state=ctl.store.get(key);state['expires_at']=0;ctl.store.put(key,state)
    ctl.dispatch(callback(11,11,button))
    assert ctl.store.get('account:a:commands') is None


def test_external_command_hook_receives_only_authorized_messages(tmp_path):
    ctl,calls=controller(tmp_path)
    seen=[]
    ctl.command_handler=lambda account,text,message:seen.append((account,text)) or True
    ctl.dispatch(message(99,99,'/link'))
    ctl.dispatch(message(11,11,'/link'))
    assert seen==[('a','/link')]


def test_wizard_requires_verified_link_then_version_then_mode(tmp_path):
    ctl,calls=controller(tmp_path)
    ctl.dispatch(message(11,11,'/run'))
    assert not ctl.control('a')['enabled']
    assert 'Demo' in str(calls[-1][1]['reply_markup'])
    ctl.dispatch(callback(11,11,'control:capital_account'))
    assert 'MEXC hesabını bağla' in str(calls[-2][1]['reply_markup'])
    # Neither arbitrary snapshot nor an old profile callback bypasses linking.
    ctl.dispatch(callback(11,11,'control:trader_aggressive'))
    assert not ctl.control('a')['profile_chosen']
    ctl.store.put('account:a:linked',True)
    ctl.set_control('a',capital_mode='account',capital_amount=1000.,allocation_mode='fixed',capital_chosen=True)
    ctl.dispatch(message(11,11,'/start'))
    keyboard=str(calls[-1][1]['reply_markup'])
    assert 'MEXC hesabını bağla' not in keyboard
    assert 'MEXC bağlı' in keyboard
    assert 'VLM' in keyboard
    ctl.dispatch(callback(11,11,'control:vlm'))
    assert 'Agresif' in str(calls[-2][1]['reply_markup'])
    ctl.dispatch(message(11,11,'/run'))
    assert not ctl.control('a')['enabled']
    ctl.dispatch(callback(11,11,'control:trader_aggressive'))
    state=ctl.control('a')
    assert state['risk_profile']=='aggressive_growth'
    assert state['risk_per_trade_pct']==.03 and state['max_leverage']==10
    assert not state['enabled']
    assert '30 USDT planlanan stop riski' in calls[-2][1]['text']
    assert '15 TL' not in calls[-2][1]['text']
    ctl.dispatch(message(11,11,'/run'))
    assert ctl.control('a')['enabled']
    assert not ctl.control('b')['enabled']


def test_stale_start_after_disconnect_and_version_change_is_blocked(tmp_path):
    ctl,calls=controller(tmp_path)
    ready(ctl,'a')
    ctl.dispatch(message(11,11,'/run'))
    ctl.dispatch(callback(11,11,'control:text'))
    assert not ctl.control('a')['enabled'] and not ctl.control('a')['profile_chosen']
    ctl.dispatch(callback(11,11,'control:start'))
    assert not ctl.control('a')['enabled']
    ctl.dispatch(callback(11,11,'control:trader_preservation'))
    assert ctl.control('a')['risk_per_trade_pct']==.005
    ctl.store.put('account:a:disconnected',True)
    ctl.dispatch(callback(11,11,'control:start'))
    assert not ctl.control('a')['enabled']
    assert 'MEXC hesabını bağla' in str(calls[-2][1]['reply_markup'])


def test_legacy_risk_callback_cannot_override_mode(tmp_path):
    ctl,calls=controller(tmp_path)
    ready(ctl,'a')
    before=ctl.control('a')['risk_per_trade_pct']
    ctl.dispatch(callback(11,11,'control:risk005'))
    assert ctl.control('a')['risk_per_trade_pct']==before
    assert not ctl.control('a')['enabled']


def test_demo_capital_queues_without_mexc_and_start_requires_applied_reset(tmp_path):
    ctl,calls=controller(tmp_path)
    ctl.dispatch(message(11,11,'/demo 1000'))
    request=ctl.store.get('account:a:commands')[0]
    assert request['kind']=='capital_reset' and request['mode']=='demo' and request['amount']==1000
    assert not ctl.control('a')['enabled'] and not ctl.control('a')['capital_chosen']
    assert ctl.control('a')['capital_pending']==request['id']
    ctl.dispatch(callback(11,11,'control:start'))
    assert not ctl.control('a')['enabled']
    # Fleet confirms application; demo then needs no linked MEXC account.
    ctl.set_control('a',capital_mode='demo',capital_amount=1000.,allocation_mode='fixed',capital_chosen=True,capital_pending=None)
    ctl.dispatch(callback(11,11,'control:vlm'))
    ctl.dispatch(callback(11,11,'control:trader_balanced'))
    ctl.dispatch(message(11,11,'/run'))
    assert ctl.control('a')['enabled'] and not ctl.linked('a')
    assert not ctl.control('b')['enabled']


@pytest.mark.parametrize('amount',['0','-1','nan','inf','1,000','abc'])
def test_invalid_demo_amount_does_not_enqueue(tmp_path,amount):
    ctl,_=controller(tmp_path)
    ctl.dispatch(message(11,11,'/demo '+amount))
    assert ctl.store.get('account:a:commands') is None
    assert not ctl.control('a')['capital_chosen']


def test_account_allocation_requires_link_and_supports_full_and_fixed(tmp_path):
    ctl,_=controller(tmp_path)
    ctl.dispatch(message(11,11,'/capital full'))
    assert ctl.store.get('account:a:commands') is None
    ctl.store.put('account:a:linked',True)
    ctl.dispatch(message(11,11,'/capital full'))
    request=ctl.store.get('account:a:commands')[0]
    assert request['mode']=='account' and request['allocation_mode']=='full' and request['amount'] is None
    ctl.dispatch(message(11,11,'/capital 500'))
    assert len(ctl.store.get('account:a:commands'))==1  # pending request cannot be replaced silently


def test_capital_button_accepts_manual_amount_in_private_owner_chat(tmp_path):
    ctl,_=controller(tmp_path)
    ctl.dispatch(callback(11,11,'control:capital_demo'))
    ctl.dispatch(message(22,22,'1234'))
    assert ctl.store.get('account:a:commands') is None
    ctl.dispatch(message(11,11,'1234'))
    request=ctl.store.get('account:a:commands')[0]
    assert request['amount']==1234 and request['mode']=='demo'


def test_report_command_renders_only_owner_reports(tmp_path):
    ctl,calls=controller(tmp_path,{'a':{'reports':[{'date':'2026-11-07','version':'vlm','trades':2,'realized_pnl':3.4,'fees':.1,'types':{'long':2},'leverages':[3]}]},'b':{'reports':'PRIVATE B REPORT'}})
    ctl.dispatch(message(11,11,'/report'))
    output=calls[-1][1]['text']
    assert '2026-11-07' in output and '+3.4000' in output
    assert 'PRIVATE B REPORT' not in output


def test_demo_amount_presets_and_stale_button_guard(tmp_path):
    ctl,calls=controller(tmp_path)
    ctl.dispatch(callback(11,11,'control:capital_demo'))
    body=calls[-1][1] if calls[-1][0]=='sendMessage' else next(body for method,body in reversed(calls) if method=='sendMessage')
    assert 'yazarak gönder' in body['text']
    actions={b['callback_data'] for row in body['reply_markup']['inline_keyboard'] for b in row}
    assert {'control:demo_amount:100','control:demo_amount:500','control:demo_amount:1000'} <= actions
    ctl.dispatch(callback(11,11,'control:demo_amount:500'))
    commands=ctl.store.get('account:a:commands')
    assert len(commands)==1 and commands[0]['amount']==500
    ctl.dispatch(callback(11,11,'control:demo_amount:500'))
    assert len(ctl.store.get('account:a:commands'))==1


def test_pause_and_stop_have_distinct_buttons_and_explain_no_new_ai(tmp_path):
    ctl,calls=controller(tmp_path)
    ready(ctl,'a')
    buttons={b['callback_data']:b['text'] for row in ctl.keyboard('a')['inline_keyboard'] for b in row}
    assert 'arayışını duraklat' in buttons['control:pause']
    assert 'AI durdur' in buttons['control:stop']
    ctl.dispatch(message(11,11,'/stop'))
    response=next(body['text'] for method,body in reversed(calls) if method=='sendMessage')
    assert 'yeni model çağrısı' in response and 'bunlar AI çağırmaz' in response
    assert 'gönderilmiş bir istek' in response
    assert 'Bot / AI durduruldu' in ctl.status_text('a')
    ctl.dispatch(message(11,11,'/pause'))
    assert not ctl.control('a')['stopped'] and not ctl.control('a')['enabled']
    assert 'İşlem arayışı duraklatıldı' in ctl.status_text('a')


def test_budget_status_separates_settled_estimate_from_pending_reservations(tmp_path):
    ctl,_=controller(tmp_path,{'a':dict(observed_at=1,ai_budget=dict(total=.107,reservations={'pending':dict(amount=.107)}))})
    text=ctl.status_text('a')
    assert 'tahmini maliyet: $0.0000' in text
    assert 'bekleyen rezervasyon: $0.1070' in text
    assert 'gerçek bakiyesi/faturası' in text


def test_settings_risk_uses_selected_variant_equity_not_fixed_example(tmp_path):
    snapshots={'a':{'version':'text','paper':{'equity':900},'variants':{'text':{'paper':{'equity':900}},'vlm':{'paper':{'equity':400}}}}}
    ctl,calls=controller(tmp_path,snapshots)
    ready(ctl,'a')
    ctl._command('a','trader_aggressive')
    text=ctl.settings_text('a')
    assert 'Son gözlenen paper özkaynak: 400 USDT' in text
    assert '12 USDT planlanan stop riski' in text
    assert '%3' in text and 'TL eşdeğeri' not in text
    assert 'Yüzde profil sınırıdır' in text
    assert 'bu kaldıracı kullanmak zorunlu değil' in text
    assert 'açık işlemleri zorla kapatmaz' in text
    assert 'Sabit yüzde ağırlık yok' in text
    snapshots['a']['variants']['vlm']['paper']['equity']=300
    assert '9 USDT planlanan stop riski' in ctl.settings_text('a')


def test_diagnostics_routes_only_owner_and_shows_safe_trace_metadata(tmp_path):
    trace=dict(id='analysis-123',symbol='BTC_USDT',strategy='momentum',confidence=.72,status='hold',
        attempts=[dict(provider='groq',model='openai/gpt-oss-20b',status='http_error',http_status=400,
                       error_category='invalid_request',error_code='json_validate_failed',error_param='response_format',
                       latency_ms=25,input_tokens=None,output_tokens=None,estimated_cost_usd=None,
                       raw_error='DO_NOT_SHOW_SECRET'),
                  dict(provider='anthropic',model='claude-sonnet-4-6',status='success',latency_ms=100,
                       input_tokens=100,output_tokens=20,estimated_cost_usd=.0006)])
    snapshots={'a':dict(observed_at=1,reason='HOLD',stage='waiting',scan_summary=dict(scanned=40,kept=20,candidates=4,eligible=2,at=1),analysis_trace=trace)}
    ctl,calls=controller(tmp_path,snapshots)
    ctl.dispatch(message(22,22,'/diagnostics'))
    assert 'BTC_USDT' not in calls[-1][1]['text']
    ctl.dispatch(message(11,11,'/diagnostics'))
    data=calls[-1][1]
    assert data['chat_id']==11
    assert 'BTC_USDT' in data['text'] and 'HTTP: 400' in data['text']
    assert 'Sağlayıcı denemesi: 2' in data['text']
    assert 'json_validate_failed' in data['text']
    assert '$0.000600' in data['text']
    assert 'DO_NOT_SHOW_SECRET' not in data['text']
    assert 'Son analiz / AI istekleri' in str(data['reply_markup'])
    text=ctl.status_text('a')
    assert 'Parite 40' in text and 'AI için uygun 2' in text
    assert 'Son aday: BTC_USDT' in text


def test_plain_position_panel_distinguishes_analysis_from_open_trade(tmp_path):
    trace=dict(id='last-analysis',symbol='LIT_USDT',status='invalid_or_unavailable_model',action='hold',execution_status='not_opened')
    snapshots={'a':dict(observed_at=1,reason='HOLD_EVALUATION_INTERVAL',paper=dict(equity=500,unrealized=0,realized=0),positions={},analysis_trace=trace)}
    ctl,calls=controller(tmp_path,snapshots)
    ctl.dispatch(message(11,11,'/positions'))
    text=calls[-1][1]['text']
    assert text.startswith('Sanal işlem paneli')
    assert 'Açık sanal işlemler: 0' in text
    assert 'Şu anda açık işlem yok' in text
    assert 'İşlem açılmadı — analiz başarısız' in text
    assert 'HOLD_EVALUATION_INTERVAL' not in text
    assert 'invalid_or_unavailable_model' not in text
    assert 'Açık işlemleri yenile' in str(calls[-1][1]['reply_markup'])
    assert 'Açık işlemler (0)' in str(ctl.keyboard('a'))


def test_position_cards_include_both_variants_and_entry_model(tmp_path):
    def position(side,model):
        return dict(side=side,entry=100,stop=98 if side=='long' else 102,target=104 if side=='long' else 96,
                    leverage=3,unrealized=2.5,opened_at=1,decision_ai=dict(provider='groq',model=model))
    snapshots={'a':dict(observed_at=1,reason='HOLD',variants={
        'text':dict(positions={'BTC_USDT':position('long','text-model')}),
        'vlm':dict(positions={'ETH_USDT':position('short','vision-model')})})}
    ctl,_=controller(tmp_path,snapshots)
    text=ctl.status_text('a',True)
    assert 'Açık sanal işlemler: 2' in text
    assert 'Yükseliş (LONG)' in text and 'Düşüş (SHORT)' in text
    assert 'text-model' in text and 'vision-model' in text
    assert 'Zarar durdur (SL)' in text and 'Kâr al (TP)' in text
    assert '+2.5000 USDT' in text
    assert 'Açık işlemler (2)' in str(ctl.keyboard('a'))


def test_scan_and_queue_progress_show_independent_refresh_and_elapsed_time():
    text=TelegramController.progress_text(dict(stage='model_evaluation',scan_stage='scanning_market',
        scan_summary=dict(at=0,scanned=40,kept=39,candidates=21,eligible=19,duration_seconds=1.25),
        queue_status=dict(size=19,next_symbol='ONE_USDT')))
    assert 'AI yanıtı bekleniyor' in text
    assert 'Tarama süresi: 1.25 sn' in text
    assert 'Kuyruktaki güncel aday: 19' in text
    assert 'Sıradaki aday: ONE_USDT' in text
    assert 'AI bekleme aralığından bağımsız' in text


def test_quota_progress_has_no_fake_fixed_tour_countdown():
    text=TelegramController.progress_text(dict(stage='model_completed',ai_schedule_mode='quota',
        parallel_candidates=4,next_evaluation_at=None))
    assert 'sabit tur beklemesi yok' in text
    assert 'en az' not in text
    assert '4 farklı aday' in text


def test_watchlist_plain_language_and_account_isolation(tmp_path):
    import time
    snapshots={'a':{'watchlist':[dict(symbol='AAA_USDT',side='long',status='watching',
        trigger_type='close_above',trigger_price=12,timeframe_seconds=900,
        expires_at=time.time()+600,reason='Yapı teyidi bekleniyor',invalidation='11 altı')]}}
    ctl,calls=controller(tmp_path,snapshots)
    text=ctl.watchlist_text('a')
    assert 'AAA_USDT' in text and 'Mum üzerinde kapanınca: 12' in text
    assert '15 dk' in text and 'işlem açıldığı anlamına gelmez' in text
    assert 'AAA_USDT' not in ctl.watchlist_text('b')
    ctl.dispatch(message(11,11,'/takip'))
    assert 'AAA_USDT' in calls[-1][1]['text']
