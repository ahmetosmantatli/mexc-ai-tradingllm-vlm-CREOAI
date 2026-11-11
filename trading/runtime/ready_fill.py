"""Live READY→fill measurement journal for Runner 3.

Appends durable rows into the runtime SQLite store (same process as usage
events). Read-only export lives beside usage_report. No risk-gate changes and
no per-card Telegram spam — only aggregate summary fields for operators.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path

from trading.runtime.runner3_policy import cost_adjusted_r

TABLE = 'ready_fill_events'
SUMMARY_KEY = 'ready_fill_summary'

OUTCOMES = ('target_hit', 'stop_hit', 'open', 'manual', 'liq', 'exchange_close', 'unknown')


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value > 0


def card_kind(setup):
    """Stable label for early weak-swing vs zone (and future zone kinds)."""
    if not isinstance(setup, dict):
        return 'unknown'
    evidence = setup.get('evidence') if isinstance(setup.get('evidence'), dict) else {}
    kind = evidence.get('kind')
    if kind == 'htf_zone_internal':
        return 'early_weak_swing'
    if kind in ('htf_zone_choch', 'htf_zone_mss'):
        return 'zone'
    if kind:
        return str(kind)
    family = setup.get('family')
    return str(family) if family else 'unknown'


def _target_price(setup, entry, fee, slippage):
    """Prefer the card take-profit; else farthest opposing level that yields a finite R."""
    if not isinstance(setup, dict):
        return None
    direct = setup.get('take_profit_price', setup.get('target'))
    if _number(direct):
        return float(direct)
    levels = setup.get('opposing_target_levels')
    if not isinstance(levels, list):
        return None
    side = setup.get('side')
    stop = setup.get('stop_loss_price', setup.get('stop'))
    best = None
    best_r = None
    for level in levels:
        price = level.get('price') if isinstance(level, dict) else None
        if not _number(price) or not _number(entry) or not _number(stop) or side not in ('long', 'short'):
            continue
        ratio = cost_adjusted_r(side=side, entry=float(entry), stop=float(stop),
                                target=float(price), fee=fee, slippage=slippage)
        if ratio is None:
            continue
        if best_r is None or ratio > best_r:
            best, best_r = float(price), ratio
    return best


def gate_snapshot(setup, *, entry, fee, slippage):
    """Entry/stop/target and cost-adjusted net R at the gate (may be under floor)."""
    stop = setup.get('stop_loss_price', setup.get('stop')) if isinstance(setup, dict) else None
    target = _target_price(setup, entry, fee, slippage)
    side = setup.get('side') if isinstance(setup, dict) else None
    net_r = None
    if side in ('long', 'short') and _number(entry) and _number(stop) and _number(target):
        net_r = cost_adjusted_r(side=side, entry=float(entry), stop=float(stop),
                                target=float(target), fee=fee, slippage=slippage)
    return dict(
        entry=float(entry) if _number(entry) else None,
        stop=float(stop) if _number(stop) else None,
        target=float(target) if _number(target) else None,
        net_r_at_gate=net_r,
    )


def map_ai_decision(trace, *, veto_reason=None, selected=False, model_not_asked=False):
    """Operator-facing decision label for one READY card row."""
    if veto_reason:
        return 'veto', str(veto_reason)
    status = (trace or {}).get('status')
    if status and status not in ('completed', 'pending') and status not in (
            'provider_wait',):
        if status.startswith('invalid') or status in (
                'watch_conflict', 'decision_expired', 'watch_revision_changed',
                'invalid_model_price_plan', 'invalid_model_sizing'):
            return 'reject', status
    action = (trace or {}).get('model_action')
    if action is None and (trace or {}).get('status') == 'completed':
        action = (trace or {}).get('action')
    if model_not_asked and action == 'hold' and (trace or {}).get('action') == 'trade':
        # Promoted ready-card entry after BEKLE; still record the model BEKLE.
        return 'BEKLE', 'ready_setup_after_hold'
    if (trace or {}).get('ready_setup_after_hold') and selected:
        return 'BEKLE', 'ready_setup_after_hold'
    if action == 'hold':
        return 'BEKLE', None
    if action in ('trade', 'local_limit', 'watch', 'cancel_watch'):
        if not selected and action == 'trade':
            return 'trade', 'other_setup_selected'
        return action if action != 'local_limit' else 'trade', (
            'local_limit' if action == 'local_limit' else None)
    if status == 'provider_wait':
        return 'reject', 'provider_wait'
    if action:
        return str(action), None
    return 'reject', status or 'unknown'


def _ensure(store):
    store.db.execute(
        f'CREATE TABLE IF NOT EXISTS {TABLE} (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')


def _load(store, ident):
    row = store.db.execute(f'SELECT payload FROM {TABLE} WHERE id=?', (ident,)).fetchone()
    return json.loads(row[0]) if row else None


def _save(store, row):
    store.db.execute(
        f'INSERT OR REPLACE INTO {TABLE} VALUES (?,?)',
        (row['id'], json.dumps(row, allow_nan=False)))


def _event_id(trace_id, setup_id):
    return f'{trace_id}:{setup_id}'


def ready_options(options):
    rows = []
    for option in options or []:
        if not isinstance(option, dict) or option.get('state') != 'ready':
            continue
        ident = option.get('setup_id')
        if not isinstance(ident, str) or not ident:
            continue
        rows.append(option)
    return rows


def record_ready_review(
        store, *, symbol, options, entry, fee, slippage, trace,
        selected_setup_id=None, veto_reason=None, model_not_asked=False,
        runner_id='runner3', order_sent=False):
    """Upsert one row per READY card seen in this Runner 3 review."""
    if not isinstance(trace, dict) or not trace.get('id'):
        return []
    cards = ready_options(options)
    if not cards:
        return []
    now = float(trace.get('completed_at') or trace.get('started_at') or 0) or None
    written = []
    with store.lock, store.db:
        _ensure(store)
        for setup in cards:
            ident = _event_id(trace['id'], setup['setup_id'])
            selected = selected_setup_id is not None and setup['setup_id'] == selected_setup_id
            decision, detail = map_ai_decision(
                trace, veto_reason=veto_reason if selected else None,
                selected=selected, model_not_asked=model_not_asked)
            levels = gate_snapshot(setup, entry=entry, fee=fee, slippage=slippage)
            prior = _load(store, ident) or {}
            row = dict(
                id=ident,
                at=prior.get('at') or now,
                updated_at=now,
                symbol=symbol,
                setup_id=setup['setup_id'],
                card_kind=card_kind(setup),
                family=setup.get('family'),
                side=setup.get('side'),
                evidence_kind=(setup.get('evidence') or {}).get('kind')
                if isinstance(setup.get('evidence'), dict) else None,
                entry=levels['entry'],
                stop=levels['stop'],
                target=levels['target'],
                net_r_at_gate=levels['net_r_at_gate'],
                trace_id=trace['id'],
                ai_decision=decision,
                decision_detail=detail or prior.get('decision_detail'),
                veto_reason=(str(veto_reason) if selected and veto_reason else prior.get('veto_reason')),
                selected=selected,
                order_sent=bool(prior.get('order_sent') or (order_sent and selected)),
                fill_status=prior.get('fill_status') or 'none',
                execution_reason=prior.get('execution_reason'),
                intent_id=prior.get('intent_id'),
                outcome=prior.get('outcome') or 'open',
                realized_r=prior.get('realized_r'),
                close_reason=prior.get('close_reason'),
                runner_id=runner_id or prior.get('runner_id') or 'runner3',
                model_not_asked=bool(model_not_asked and selected) or bool(prior.get('model_not_asked')),
            )
            # Preserve terminal fill/outcome if a later review rewrites the same id.
            for key in ('fill_status', 'execution_reason', 'intent_id', 'outcome',
                        'realized_r', 'close_reason', 'order_sent'):
                if prior.get(key) not in (None, 'none', 'open', False):
                    if key == 'order_sent':
                        row['order_sent'] = bool(prior['order_sent']) or row['order_sent']
                    elif key == 'outcome' and prior.get('outcome') not in (None, 'open'):
                        row[key] = prior[key]
                    elif key != 'outcome':
                        row[key] = prior[key]
            _save(store, row)
            written.append(row)
        _refresh_summary(store)
    return written


def mark_execution(store, *, trace_id, fill_status, execution_reason=None,
                   intent_id=None, order_sent=None, setup_id=None):
    """Attach order/fill result to the selected READY row for this analysis."""
    if not trace_id or fill_status not in ('filled', 'rejected', 'none', 'awaiting'):
        return None
    with store.lock, store.db:
        _ensure(store)
        rows = list(store.db.execute(f'SELECT id, payload FROM {TABLE}'))
        target = None
        for ident, payload in rows:
            row = json.loads(payload)
            if row.get('trace_id') != trace_id:
                continue
            if setup_id and row.get('setup_id') != setup_id:
                continue
            if setup_id is None and not row.get('selected') and target is not None:
                continue
            if row.get('selected') or (setup_id and row.get('setup_id') == setup_id):
                target = row
                break
            if target is None:
                target = row
        if target is None:
            return None
        target = dict(target)
        target['updated_at'] = datetime.now(timezone.utc).timestamp()
        if order_sent is not None:
            target['order_sent'] = bool(order_sent)
        elif fill_status in ('filled', 'rejected', 'awaiting'):
            target['order_sent'] = True
        target['fill_status'] = fill_status
        if execution_reason is not None:
            target['execution_reason'] = execution_reason
        if intent_id is not None:
            target['intent_id'] = intent_id
        if fill_status == 'filled' and target.get('outcome') in (None, 'open'):
            target['outcome'] = 'open'
        if fill_status == 'rejected':
            target['outcome'] = 'unknown'
            if not target.get('ai_decision') or target['ai_decision'] == 'trade':
                # Keep trade as AI decision; reject is the fill_status.
                pass
        _save(store, target)
        _refresh_summary(store)
        return target


def outcome_from_close(close_reason):
    mapping = {'TP': 'target_hit', 'SL': 'stop_hit', 'MANUAL': 'manual', 'LIQ': 'liq',
               'EXCHANGE_CLOSE': 'exchange_close', 'EMERGENCY': 'manual'}
    if close_reason in mapping:
        return mapping[close_reason]
    return 'unknown'


def realized_r_from_intent(intent):
    if not isinstance(intent, dict) or not intent.get('realized_complete'):
        return None
    net = intent.get('net_pnl')
    risk = (intent.get('entry_metrics') or {}).get('initial_stop_risk_usdt')
    if not (isinstance(net, (int, float)) and not isinstance(net, bool) and isfinite(net)):
        return None
    if not (isinstance(risk, (int, float)) and not isinstance(risk, bool) and isfinite(risk) and risk > 0):
        return None
    return net / risk


def mark_outcome(store, intent):
    """Update READY row when a live intent fills or closes."""
    if not isinstance(intent, dict):
        return None
    intent_id = intent.get('intent_id')
    proposal = intent.get('proposal') if isinstance(intent.get('proposal'), dict) else {}
    metadata = proposal.get('metadata') if isinstance(proposal.get('metadata'), dict) else {}
    trace_id = metadata.get('analysis_trace_id')
    setup_id = metadata.get('setup_id')
    status = intent.get('status')
    with store.lock, store.db:
        _ensure(store)
        row = None
        if intent_id:
            for ident, payload in store.db.execute(f'SELECT id, payload FROM {TABLE}'):
                parsed = json.loads(payload)
                if parsed.get('intent_id') == intent_id:
                    row = parsed
                    break
        if row is None and trace_id:
            for ident, payload in store.db.execute(f'SELECT id, payload FROM {TABLE}'):
                parsed = json.loads(payload)
                if parsed.get('trace_id') != trace_id:
                    continue
                if setup_id and parsed.get('setup_id') != setup_id and not parsed.get('selected'):
                    continue
                if setup_id is None and not parsed.get('selected'):
                    continue
                if setup_id and parsed.get('setup_id') == setup_id:
                    row = parsed
                    break
                if parsed.get('selected'):
                    row = parsed
                    break
        if row is None:
            return None
        row = dict(row)
        row['updated_at'] = datetime.now(timezone.utc).timestamp()
        if intent_id:
            row['intent_id'] = intent_id
        if status == 'protected' or (intent.get('entry_fill_price') and intent.get('position_id')):
            row['order_sent'] = True
            row['fill_status'] = 'filled'
            if row.get('outcome') in (None, 'unknown'):
                row['outcome'] = 'open'
        if status == 'entry_rejected':
            row['order_sent'] = True
            row['fill_status'] = 'rejected'
            row['execution_reason'] = intent.get('reject_reason') or row.get('execution_reason')
            row['outcome'] = 'unknown'
        if status == 'closed':
            row['order_sent'] = True
            row['fill_status'] = 'filled'
            reason = intent.get('close_reason')
            row['close_reason'] = reason
            row['outcome'] = outcome_from_close(reason)
            ratio = realized_r_from_intent(intent)
            if ratio is not None:
                row['realized_r'] = ratio
        _save(store, row)
        _refresh_summary(store)
        return row


def _refresh_summary(store):
    rows = [json.loads(payload) for _, payload in store.db.execute(f'SELECT id, payload FROM {TABLE}')]
    summary = summarize(rows)
    store.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',
                     (SUMMARY_KEY, json.dumps(summary, allow_nan=False)))


def summarize(rows):
    """Compact counts for Telegram status / usage_report — not per-card spam."""
    decisions = Counter()
    fills = Counter()
    outcomes = Counter()
    kinds = Counter()
    realized = []
    for row in rows:
        decisions[row.get('ai_decision') or 'unknown'] += 1
        fills[row.get('fill_status') or 'none'] += 1
        outcomes[row.get('outcome') or 'unknown'] += 1
        kinds[row.get('card_kind') or 'unknown'] += 1
        if isinstance(row.get('realized_r'), (int, float)) and isfinite(row['realized_r']):
            realized.append(row['realized_r'])
    return dict(
        cards=len(rows),
        by_kind=dict(kinds),
        by_ai_decision=dict(decisions),
        by_fill_status=dict(fills),
        by_outcome=dict(outcomes),
        orders_sent=sum(1 for row in rows if row.get('order_sent')),
        filled=sum(1 for row in rows if row.get('fill_status') == 'filled'),
        rejected_fills=sum(1 for row in rows if row.get('fill_status') == 'rejected'),
        realized_r_samples=len(realized),
        mean_realized_r=(sum(realized) / len(realized)) if realized else None,
        note='READY→fill journal only; not a profitability claim. Missing outcomes stay open/unknown.',
    )


def list_events(store):
    with store.lock:
        tables = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if TABLE not in tables:
            return []
        return [json.loads(payload) for _, payload in store.db.execute(
            f'SELECT id, payload FROM {TABLE} ORDER BY id')]


def read_events(root):
    """Scan runtime DBs under a data root (same layout as usage_report)."""
    rows = []
    root = Path(root)
    for path in sorted(root.rglob('*.sqlite3')):
        relative = path.relative_to(root)
        parts = relative.parts
        if path.name.startswith('runner2-shadow-'):
            continue
        if len(parts) < 3 or parts[-2] not in ('text', 'vlm', 'live'):
            continue
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
        try:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if TABLE not in tables:
                continue
            account = parts[0]
            mode = 'LIVE' if parts[-2] == 'live' else 'PAPER'
            for payload, in db.execute(f'SELECT payload FROM {TABLE}'):
                row = json.loads(payload)
                row.setdefault('account', account)
                row.setdefault('mode', mode)
                row.setdefault('db_path', str(relative))
                rows.append(row)
        finally:
            db.close()
    return sorted(rows, key=lambda r: (r.get('at') or 0, r.get('id') or ''))


def summary_line(summary):
    """One short operator line; safe for status panels."""
    if not isinstance(summary, dict) or not summary.get('cards'):
        return None
    text = (
        f"READY→fill: {summary['cards']} kart | "
        f"emir {summary.get('orders_sent', 0)} | "
        f"dolum {summary.get('filled', 0)} | "
        f"ret {summary.get('rejected_fills', 0)}"
    )
    mean = summary.get('mean_realized_r')
    if isinstance(mean, (int, float)) and isfinite(mean):
        text += f' | ort. R {mean:.2f}'
    return text
