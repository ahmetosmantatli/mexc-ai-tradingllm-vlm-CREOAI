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
