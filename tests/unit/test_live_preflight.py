import pytest
from trading.control.live_preflight import inspect_account


class ReadOnly:
    def account_assets(self):
        return [dict(currency='USDT',equity='11.5854',availableBalance='10',positionMargin='1',unrealized='.5854')]
    def open_positions(self):return []
    def position_mode(self):return 1
    def leverage(self,symbol):return [dict(symbol=symbol,positionType=side,openType=1,leverage=3,maxVol=100,maxLeverageView=10,currentMmr=.005) for side in (1,2)]
    def risk_limits(self,symbol=None):return {symbol:[dict(symbol=symbol,positionType=side,level=1,maxVol=100,maxLeverage=10,mmr=.005,imr=.1) for side in (1,2)]}
    def current_orders(self,**kwargs):return []


def test_preflight_reports_real_equity_separately_from_available_without_writes():
    result=inspect_account(ReadOnly(),'BTC_USDT')
    assert result['equity']==11.5854 and result['available']==10
    assert result['writes_enabled'] is False
    assert result['automatic_live_worker_available'] is True
    assert result['execution_enabled_by_this_check'] is False
    assert result['account_leverage'][0]['account_max_leverage']==10


def test_preflight_never_treats_full_unknown_pagination_as_flat():
    class Incomplete(ReadOnly):
        def current_orders(self,**kwargs):return [{}]*100
    with pytest.raises(ValueError,match='(?i)order.*incomplete'):
        inspect_account(Incomplete(),'BTC_USDT')


def test_list_accounts_reads_no_exchange_credentials_or_http(tmp_path,monkeypatch,capsys):
    import json
    from types import SimpleNamespace
    import trading.control.live_preflight as command
    monkeypatch.setattr(command,'Settings',lambda **kw:SimpleNamespace(accounts_file='registry'))
    monkeypatch.setattr(command,'load_accounts',lambda *a,**kw:[SimpleNamespace(account_id='ahmet'),SimpleNamespace(account_id='friend')])
    monkeypatch.setattr(command,'MexcPrivateClient',lambda *a,**kw:pytest.fail('listing must not contact exchange'))
    assert command.main(['--list-accounts','--env-file',str(tmp_path/'missing')])==0
    assert json.loads(capsys.readouterr().out)==['ahmet','friend']


def test_cli_errors_do_not_echo_transport_secrets(monkeypatch,capsys):
    import trading.control.live_preflight as command
    monkeypatch.setattr(command,'Settings',lambda **kw:(_ for _ in ()).throw(ValueError('secret-should-not-be-printed')))
    assert command.main(['--account','ahmet'])==2
    output=capsys.readouterr().out
    assert 'secret-should-not-be-printed' not in output and 'writes_enabled' in output


@pytest.mark.parametrize('reason',['account_credentials_missing','account_disconnected','unknown_account'])
def test_local_failure_exposes_fixed_reason_and_stage_without_raw_credentials(tmp_path,monkeypatch,capsys,reason):
    import json
    from types import SimpleNamespace
    import trading.control.live_preflight as command
    monkeypatch.setattr(command,'Settings',lambda **kw:SimpleNamespace())
    monkeypatch.setattr(command,'resolve_account',lambda *a,**kw:(_ for _ in ()).throw(command.PreflightIssue(reason)))
    monkeypatch.setattr(command,'MexcPrivateClient',lambda *a,**kw:pytest.fail('No exchange request before resolution'))
    assert command.main(['--account','friend'])==2
    result=json.loads(capsys.readouterr().out)
    assert result['reason']==reason and result['stage']=='account_resolution'
    assert result['hint'] and result['writes_enabled'] is False


def test_local_missing_credentials_are_not_replaced_by_unrelated_global_key(tmp_path,monkeypatch):
    import json
    from trading.config import Settings
    from trading.control.live_preflight import resolve_account,PreflightIssue
    registry=tmp_path/'accounts.json'
    registry.write_text(json.dumps({'accounts':[dict(id='friend',telegram_user_id=7,telegram_chat_id=7,mexc_key_env='FRIEND_KEY',mexc_secret_env='FRIEND_SECRET')]}))
    monkeypatch.delenv('FRIEND_KEY',raising=False);monkeypatch.delenv('FRIEND_SECRET',raising=False)
    monkeypatch.setenv('MEXC_API_KEY','unrelated-key');monkeypatch.setenv('MEXC_API_SECRET','unrelated-secret')
    cfg=Settings(_env_file=None,accounts_file=str(registry),runtime_db_path=str(tmp_path/'data/runtime.sqlite3'),account_vault_key='')
    with pytest.raises(PreflightIssue,match='account_credentials_missing'):
        resolve_account(cfg,'friend',str(tmp_path/'absent.env'))
