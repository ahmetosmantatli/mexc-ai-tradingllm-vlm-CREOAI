"""Structure auto-entry Telegram copy must not claim a fill before an order opens."""
import logging

from trading.runtime.structure_entry_messages import (
    format_pass_telegram, format_reject_telegram, log_structure_attempt,
    structure_plan_rationale, structure_plan_telegram_line, structure_ready_card_rationale)


def test_plan_copy_does_not_say_girildi():
    rationale = structure_plan_rationale('TSMSTOCK_USDT', 'confirm_bar')
    assert 'girildi' not in rationale.lower()
    assert 'plan hazır' in rationale
    assert 'Model sorulmadı' in rationale
    ready = structure_ready_card_rationale('BTC_USDT')
    assert 'girildi' not in ready.lower()
    assert 'plan kuruldu' in ready
    line = structure_plan_telegram_line('ob_retest')
    assert 'AI işlem önerdi' not in line
    assert 'planı kuruldu' in line
    assert 'Emir henüz gönderilmedi' in line


def test_reject_telegram_includes_code_and_numbers():
    text = format_reject_telegram(
        symbol='TSMSTOCK_USDT', reason='cost_adjusted_r', path='confirm_bar', side='long',
        details=dict(net_r=0.9, free_margin=2.5, entry=1.2, stop=1.1, target=1.35))
    assert text.startswith('REJECT | yapı auto-entry | TSMSTOCK_USDT')
    assert 'Kod: cost_adjusted_r' in text
    assert '1.25' in text
    assert 'net R=0.9' in text
    assert 'serbest marjin=2.5' in text


def test_reject_margin_and_drift_codes():
    margin = format_reject_telegram(
        symbol='AAVE_USDT', reason='HOLD_LIVE_EXECUTION_PRIVATE_PREFLIGHT', path='ob_retest',
        side='short', details=dict(reason_codes=['INSUFFICIENT_FREE_MARGIN'], free_margin=0.4,
                                   required_margin=1.2))
    assert 'INSUFFICIENT_FREE_MARGIN' in margin or 'Kullanılabilir teminat' in margin
    assert 'serbest marjin=0.4' in margin
    drift = format_reject_telegram(
        symbol='ETH_USDT', reason='HOLD_LIVE_EXECUTION_ENTRY_PRICE_DRIFT', path='confirm_bar',
        details=dict(code='ENTRY_PRICE_DRIFT',
                     price_drift=dict(stage='planning', planned=100., observed=101.,
                                      deviation=0.01, tolerance=0.02)))
    assert 'ENTRY_PRICE_DRIFT' in drift or 'sapma' in drift


def test_pass_telegram_lists_fill_fields():
    text = format_pass_telegram(
        symbol='SOL_USDT', side='long', path='confirm_bar', leverage=10, entry=150.2,
        stop=148., target=155., contracts=3, notional=450.6, order_id='998877')
    assert text.startswith('PASS | yapı auto-entry | SOL_USDT')
    assert 'confirm_bar' in text
    assert 'kontrat=3' in text
    assert 'kaldıraç=10x' in text
    assert 'Emir id: 998877' in text
    assert 'girildi' not in text.lower()


def test_structured_log_is_greppable(caplog):
    with caplog.at_level(logging.INFO, logger='worker'):
        log_structure_attempt(
            outcome='REJECT', symbol='TSMSTOCK_USDT', path='confirm_bar',
            reason='free_margin', side='long',
            details=dict(free_margin=0.5, contracts=dict(quantity=0, minimum=1, maximum=100)))
    joined = ' '.join(record.getMessage() for record in caplog.records)
    assert 'STRUCTURE_AUTO_ENTRY outcome=REJECT' in joined
    assert 'symbol=TSMSTOCK_USDT' in joined
    assert 'path=confirm_bar' in joined
    assert 'reason=free_margin' in joined
    assert 'contracts=0' in joined
