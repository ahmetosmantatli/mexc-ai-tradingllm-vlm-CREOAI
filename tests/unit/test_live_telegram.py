import json
import httpx
from trading.control.accounts import Account
from trading.control.live_telegram import LiveTelegramController
from trading.runtime.store import RuntimeStore


def setup(tmp_path,snapshots=None):
    calls=[]
    def send(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200,json={'ok':True,'result':[]})
    accounts=[Account('a',11,11,'',''),Account('b',22,22,'','')]
    controller=LiveTelegramController('fake',accounts,RuntimeStore(str(tmp_path/'control')),
        snapshot_provider=lambda ident:(snapshots or {}).get(ident,{}),client=httpx.Client(transport=httpx.MockTransport(send)))
    return controller,calls


def ready(controller):
    controller.store.put('account:a:linked',True)
    controller.set_control('a',capital_chosen=True,capital_pending=None,capital_mode='account',allocation_mode='full',
        version='text',version_chosen=True,profile_chosen=True,risk_profile='balanced')


def test_live_setup_has_no_demo_or_paper_cash_and_requires_explicit_start(tmp_path):
    ctl,calls=setup(tmp_path)
    ctl._command('a','start')
    assert not ctl.control('a')['enabled']
    assert 'PAPER' not in ctl.settings_text('a')
    ready(ctl)
    ctl._command('a','start')
    assert ctl.control('a')['enabled']
    assert 'işlem başına Telegram onayı beklemez' in ctl.settings_text('a')
    ctl._command('a','vlm')
    assert not ctl.control('a')['enabled']
    ctl._command('a','trader_aggressive')
    assert not ctl.control('a')['enabled']


def test_live_stop_blocks_ai_and_keeps_monitoring_description(tmp_path):
    ctl,calls=setup(tmp_path);ready(ctl)
    ctl._command('a','start');ctl._command('a','stop')
    assert ctl.control('a')['enabled'] is False and ctl.control('a')['stopped'] is True
    assert 'uzlaştırması sürer' in calls[-1]['text']


def test_live_balance_is_reported_without_static_seed_and_unknown_is_not_zero(tmp_path):
    ctl,calls=setup(tmp_path,{'a':{'mode':'LIVE','live_account':{'balance':{'equity':11.5854,'available':10.2,'unrealized':.3,'position_margin':1}}}})
    text=ctl.status_text('a')
    assert '11.5854' in text and '10.2000' in text and 'Sanal' not in text
    assert 'doğrulanmadı' in ctl.status_text('b')


def test_live_demo_rejected_and_full_capital_queues_no_fabricated_balance(tmp_path):
    ctl,calls=setup(tmp_path);ready(ctl)
    ctl.request_capital('a','demo',500)
    assert not ctl.store.get('account:a:commands',[])
    ctl.request_capital('a','account',allocation_mode='full')
    command=ctl.store.get('account:a:commands')[0]
    assert command['amount'] is None and command['allocation_mode']=='full'
    assert ctl.control('a')['enabled'] is False


def test_unauthorized_live_start_never_changes_account_state(tmp_path):
    ctl,calls=setup(tmp_path);ready(ctl)
    ctl.dispatch({'message':{'chat':{'id':33,'type':'private'},'from':{'id':33},'text':'/run'}})
    assert not ctl.control('a')['enabled'] and not ctl.control('b')['enabled']
    assert not calls


def test_native_close_button_does_not_require_missing_exchange_create_time(tmp_path):
    ctl,calls=setup(tmp_path,{'a':{'mode':'LIVE','positions':{'BTC_USDT':dict(symbol='BTC_USDT',intent_id='intent',position_id='42',analysis_version='text')}}})
    targets=ctl.position_targets('a')
    assert targets[0]['intent_id']=='intent' and targets[0]['position_id']=='42'
    ctl.position_buttons('a')
    assert calls[-1]['reply_markup']['inline_keyboard'][0][0]['text']=='Kapat: BTC_USDT'
