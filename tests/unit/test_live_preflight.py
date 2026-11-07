import pytest
from trading.control.live_preflight import inspect_account


class ReadOnly:
    def account_assets(self):
        return [dict(currency='USDT',equity='11.5854',availableBalance='10',positionMargin='1',unrealized='.5854')]
    def open_positions(self):return []
    def position_mode(self):return 1
    def leverage(self,symbol):return [dict(symbol=symbol,leverage=3)]
    def risk_limits(self,symbol):return {symbol:[dict(maxLeverage=10)]}
    def current_orders(self,**kwargs):return []


def test_preflight_reports_real_equity_separately_from_available_without_writes():
    result=inspect_account(ReadOnly(),'BTC_USDT')
    assert result['equity']==11.5854 and result['available']==10
    assert result['writes_enabled'] is False
    assert result['automatic_live_worker_available'] is False


def test_preflight_never_treats_full_unknown_pagination_as_flat():
    class Incomplete(ReadOnly):
        def current_orders(self,**kwargs):return [{}]*100
    with pytest.raises(ValueError,match='pagination_incomplete'):
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
