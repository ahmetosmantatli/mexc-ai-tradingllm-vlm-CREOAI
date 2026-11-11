"""Opt-in structure auto-entry: confirm_bar/ob_retest READY without asking AI."""
from copy import deepcopy
import time
from types import SimpleNamespace

import pytest

from trading.config import Settings
from trading.evaluation.service import ContextEvaluator, _confirm_path_ready_card
from trading.models.domain import Side
from trading.runtime.ready_fill import map_ai_decision
from trading.runtime.store import RuntimeStore
from .test_runner3_llm_vlm_limits import DecisionModel
from .test_watch_service import signal
from .test_zone_entry import _answer, _internal, _rebase, weak_long_market


@pytest.fixture(autouse=True)
def _wide_entry_cap(monkeypatch):
    monkeypatch.setattr('trading.quant.structure_setups.WEAK_ENTRY_MAX_ATR', 2.)


def _run(tmp_path, monkeypatch, *, auto_entry, market=None, text=None, equity=11.58):
    market = _rebase(weak_long_market() if market is None else market)
    market['symbol'] = 'BTC_USDT'

    def default_text(packet):
        return _answer(packet)

    def vision(model, packet):
        model.vision_answer = None
        model.last_trace = {'attempts': [{'generation_requests': 1, 'status': 'invalid_schema'}]}

    model = DecisionModel(text or default_text, vision)
    monkeypatch.setattr('trading.analysis.build_bundle', lambda *args, **kwargs:
                        SimpleNamespace(fingerprint='lane', to_context=lambda **kw: deepcopy(market)))
    monkeypatch.setattr('trading.analysis.render_bundle', lambda *args, **kwargs:
                        [SimpleNamespace(path=tmp_path / 'chart.png')])
    settings = Settings(
        _env_file=None, analysis_version='text', live_structure_setups_enabled=True,
        live_price_plan_enabled=True, runner3_vlm_after_valid_text=False,
        live_runner3_structure_auto_entry_enabled=auto_entry)
    store = RuntimeStore(str(tmp_path / 'state'))
    reviewed = ContextEvaluator(
        model, store,
        SimpleNamespace(get_contract_rules=lambda symbol: SimpleNamespace(max_leverage=20, price_step=.01)),
        settings)
    seed = signal(Side.LONG)
    seed.symbol = 'BTC_USDT'
    seed.entry_price = 100.
    seed.features.update(htf_zone_seed=True, source_closed_at=time.time() - 1, dedup_key='seed')
    result = reviewed.evaluate_candidates(
        [seed], equity=equity, risk_per_trade_pct=.03, target_leverage=20,
        live_account_context=dict(mode='LIVE', currency='USDT', runner_id='runner3',
                                  current_equity=equity, available_usdt=equity),
        live_positions={})
    return result, model


def test_confirm_path_helper_picks_entry_path():
    card, = _internal(weak_long_market())
    assert card['evidence']['entry_path'] == 'confirm_bar'
    assert _confirm_path_ready_card([card], side='long')['setup_id'] == card['setup_id']
    assert _confirm_path_ready_card([card], side='short') is None


def test_flag_default_off_still_asks_model(tmp_path, monkeypatch):
    result, model = _run(tmp_path, monkeypatch, auto_entry=False)
    assert model.calls
    assert result.action.value == 'trade'
    assert not result.metadata.get('model_not_asked')
    assert not result.metadata.get('structure_auto_entry')
    assert Settings(_env_file=None).live_runner3_structure_auto_entry_enabled is False


def test_flag_on_confirm_bar_skips_model(tmp_path, monkeypatch):
    def boom(packet):
        raise AssertionError('model must not be asked')

    result, model = _run(tmp_path, monkeypatch, auto_entry=True, text=boom)
    assert model.calls == []
    assert result.metadata.get('model_not_asked') is True
    assert result.metadata.get('structure_auto_entry') is True
    assert result.action.value == 'trade'
    assert result.proposal is not None
    label, detail = map_ai_decision(
        dict(status='completed', action='trade', model_action='trade', structure_auto_entry=True),
        selected=True, model_not_asked=True)
    assert label == 'trade' and detail == 'structure_auto_entry'


def test_under_one_point_two_five_vetoes_without_model(tmp_path, monkeypatch):
    """choose_zone_target may return a short R; entry_veto still blocks; no AI call."""
    def boom(packet):
        raise AssertionError('model must not be asked after structure veto')

    market = weak_long_market(target_high=103., target_low=101.5)
    result, model = _run(tmp_path, monkeypatch, auto_entry=True, market=market, text=boom)
    assert model.calls == []
    assert result.action.value == 'hold'
    assert result.hold_reason == 'cost_adjusted_r'
    assert result.metadata.get('structure_auto_entry') is True
    assert result.metadata.get('veto') == 'cost_adjusted_r'
