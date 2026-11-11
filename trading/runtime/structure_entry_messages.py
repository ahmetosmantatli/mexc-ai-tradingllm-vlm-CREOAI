"""Telegram copy + greppable logs for Runner3 structure auto-entry.

Plan-built text must not say a trade opened. PASS/REJECT outcomes carry the
gate result after entry controls. Risk gates themselves are unchanged.
"""
from __future__ import annotations

import logging
import time
from math import isfinite

from trading.agents.telemetry import human_reason

STRUCTURE_OUTCOME_KEY = 'live:structure_entry_outcomes'

# Evaluate-time entry_veto codes and common apply/execution codes.
STRUCTURE_REJECT_TR = {
    'cost_adjusted_r': 'Maliyet sonrası hedef R (1.25) geçilmedi.',
    'free_margin': 'Kullanılabilir teminat yetersiz.',
    'equity_stop': 'Stop riski özsermaye tavanını aşıyor.',
    'HOLD_LIVE_EXECUTION_INSUFFICIENT_MARGIN': 'Kullanılabilir teminat işlem için yetersiz.',
    'HOLD_LIVE_EXECUTION_PRIVATE_PREFLIGHT': 'Hesap/marjin ön kontrolü reddetti.',
    'HOLD_LIVE_EXECUTION_ENTRY_PRICE_DRIFT': 'Analiz sonrası fiyat sapması; işlem açılmadı.',
    'HOLD_LIVE_EXECUTION_CONTRACT_QUANTITY_LIMIT': 'Kontrat adedi borsa sınırına uymadı (0 veya min altı).',
    'HOLD_LIVE_EXECUTION_CONTRACT_RISK_TIER': 'Kontrat adedi desteklenen risk kademesini aşıyor.',
    'HOLD_LIVE_EXECUTION_RUNNER3_NET_TARGET_R': 'Maliyet sonrası hedef R, Runner 3 alt sınırının altında.',
    'HOLD_LIVE_EXECUTION_RUNNER3_MARGIN_BELOW_MINIMUM': 'Hesaplanan teminat minimumun altında.',
    'HOLD_LIVE_EXECUTION_RUNNER3_MARGIN_RESERVE': 'Kalan teminat rezerv ve emir maliyetine sığmadı.',
    'HOLD_LIVE_DECISION_EXPIRED': 'Karar süresi doldu veya fiyat planı geçersiz kaldı.',
    'HOLD_LIVE_ENTRY_REJECTED': 'Borsa emri reddedildi veya IOC dolmadı.',
    'INSUFFICIENT_FREE_MARGIN': 'Kullanılabilir teminat yetersiz.',
    'ENTRY_PRICE_DRIFT': 'Fiyat sapması giriş planını bozdu.',
    'CONTRACT_QUANTITY_LIMIT': 'Kontrat adedi borsa min/max sınırına uymadı.',
    'RUNNER3_NET_TARGET_R': 'Net R kapısı (1.25) geçilmedi.',
}


def structure_plan_rationale(symbol, path):
    label = path or 'confirm_bar/ob_retest'
    return (f'{symbol} {label} hazır kurulumdan auto-entry plan hazır. Model sorulmadı. '
            'Stop karttan; hedef 1.25 geçen yapı seviyesi.')


def structure_ready_card_rationale(symbol):
    return (f'{symbol} hazır kurulumdan güncel fiyattan plan kuruldu. '
            'Kapalı CHoCH, blok veya akış ve stop vardı. Model yeniden sorulmadı.')


def structure_plan_telegram_line(path):
    label = path or 'confirm_bar/ob_retest'
    return f'Yapı auto-entry planı kuruldu ({label}); giriş kontrolleri yapılıyor. Emir henüz gönderilmedi.'


def reject_code_tr(reason, details=None):
    details = details or {}
    codes = tuple(details.get('reason_codes') or ())
    for code in codes:
        if code in STRUCTURE_REJECT_TR:
            return code, STRUCTURE_REJECT_TR[code]
    detail_code = details.get('code')
    if detail_code in STRUCTURE_REJECT_TR:
        return detail_code, STRUCTURE_REJECT_TR[detail_code]
    if reason in STRUCTURE_REJECT_TR:
        return reason, STRUCTURE_REJECT_TR[reason]
    text = human_reason(reason)
    return reason, text


def _fmt_num(value, digits=4):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        return None
    if float(value).is_integer() and abs(value) < 1e9:
        return f'{int(value)}'
    return f'{float(value):.{digits}g}'


def _append_numbers(parts, details):
    details = details or {}
    mapping = (
        ('free_margin', 'serbest marjin'),
        ('required_margin', 'gerekli marjin'),
        ('net_r', 'net R'),
        ('contracts', 'kontrat'),
        ('quantity', 'kontrat'),
        ('leverage', 'kaldıraç'),
        ('entry', 'giriş'),
        ('stop', 'stop'),
        ('target', 'hedef'),
    )
    for key, label in mapping:
        raw = details.get(key)
        if key == 'contracts' and isinstance(raw, dict):
            qty = _fmt_num(raw.get('quantity'))
            minimum = _fmt_num(raw.get('minimum'))
            maximum = _fmt_num(raw.get('maximum'))
            if qty is not None:
                chunk = f'kontrat={qty}'
                if minimum is not None and maximum is not None:
                    chunk += f' (izin {minimum}–{maximum})'
                parts.append(chunk)
            continue
        rendered = _fmt_num(raw)
        if rendered is not None:
            parts.append(f'{label}={rendered}')
    drift = details.get('price_drift')
    if isinstance(drift, dict):
        planned = _fmt_num(drift.get('planned'), 10)
        observed = _fmt_num(drift.get('observed'), 10)
        deviation = drift.get('deviation')
        if planned is not None and observed is not None:
            chunk = f'sapma plan={planned} güncel={observed}'
            if isinstance(deviation, (int, float)) and isfinite(deviation):
                chunk += f' ({deviation:+.3%})'
            parts.append(chunk)


def format_reject_telegram(*, symbol, reason, details=None, path=None, side=None):
    details = dict(details or {})
    code, human = reject_code_tr(reason, details)
    path_label = path or details.get('entry_path') or 'confirm_bar/ob_retest'
    side_label = side or details.get('side') or '?'
    lines = [
        f'REJECT | yapı auto-entry | {symbol}',
        f'Kaynak: {path_label} | Yön: {side_label}',
        f'Kod: {code} — {human}',
    ]
    extras = []
    _append_numbers(extras, details)
    if extras:
        lines.append('Sayılar: ' + ' | '.join(extras))
    return '\n'.join(lines)


def format_pass_telegram(*, symbol, side, path, leverage=None, entry=None, stop=None,
                         target=None, contracts=None, notional=None, order_id=None):
    path_label = path or 'confirm_bar/ob_retest'
    lines = [
        f'PASS | yapı auto-entry | {symbol}',
        f'Kaynak: {path_label} | Yön: {side or "?"}',
    ]
    bits = []
    for label, value in (
        ('kontrat', contracts), ('notional', notional), ('kaldıraç', leverage),
        ('giriş', entry), ('stop', stop), ('hedef', target),
    ):
        rendered = _fmt_num(value)
        if rendered is not None:
            if label == 'kaldıraç':
                bits.append(f'{label}={rendered}x')
            elif label == 'notional':
                bits.append(f'{label}={rendered} USDT')
            else:
                bits.append(f'{label}={rendered}')
    if bits:
        lines.append(' | '.join(bits))
    if order_id:
        lines.append(f'Emir id: {order_id}')
    return '\n'.join(lines)


def log_structure_attempt(*, outcome, symbol, path=None, reason=None, side=None,
                          details=None, order_id=None):
    """One greppable worker line per structure auto-entry attempt."""
    details = details or {}
    parts = [
        f'STRUCTURE_AUTO_ENTRY outcome={outcome}',
        f'symbol={symbol}',
        f'path={path or details.get("entry_path") or "-"}',
        f'side={side or details.get("side") or "-"}',
    ]
    if reason:
        parts.append(f'reason={reason}')
    code = details.get('code')
    if code:
        parts.append(f'code={code}')
    codes = details.get('reason_codes')
    if codes:
        parts.append(f'reason_codes={",".join(map(str, codes))}')
    for key in ('free_margin', 'required_margin', 'net_r', 'leverage', 'entry',
                'stop', 'target', 'contracts', 'quantity', 'notional'):
        raw = details.get(key)
        if key == 'contracts' and isinstance(raw, dict):
            qty = raw.get('quantity')
            if isinstance(qty, (int, float)) and isfinite(qty):
                parts.append(f'contracts={qty:g}')
            continue
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and isfinite(raw):
            parts.append(f'{key}={raw:g}')
    if order_id:
        parts.append(f'order_id={order_id}')
    logging.getLogger('worker').info('%s', ' '.join(parts))


def enqueue_structure_outcome(store, *, binding_id, outcome, symbol, reason=None,
                              details=None, path=None, side=None, text=None,
                              order_id=None):
    notice = dict(
        at=time.time(), binding_id=binding_id, outcome=outcome, symbol=symbol,
        reason=reason, details=dict(details or {}), path=path, side=side,
        text=text, order_id=order_id)
    with store.lock:
        pending = store.get(STRUCTURE_OUTCOME_KEY, [])
        store.put(STRUCTURE_OUTCOME_KEY, (pending + [notice])[-100:])
    return notice


def entry_path_from_metadata(metadata):
    metadata = metadata or {}
    path = metadata.get('entry_path')
    if path in ('confirm_bar', 'ob_retest'):
        return path
    setup = metadata.get('setup_selection') or metadata.get('htf_setup') or {}
    evidence = setup.get('evidence') if isinstance(setup, dict) else {}
    if isinstance(evidence, dict) and evidence.get('entry_path') in ('confirm_bar', 'ob_retest'):
        return evidence['entry_path']
    return None
