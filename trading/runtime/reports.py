"""Read-only paper trade reports, grouped by closure date in the operator timezone.

Closed-trade costs are attributed to the closure date, not the date they were
booked in cash. Snapshot equity/unrealized are current, never labelled daily PnL.
Incomplete older ledger rows are disclosed rather than reconstructed from charts.
"""
from datetime import date, datetime
from math import isfinite
from zoneinfo import ZoneInfo


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _when(value, tz):
    number = _number(value)
    if number is None or number <= 0:
        return None
    if number >= 100_000_000_000:
        number /= 1000
    try:
        return datetime.fromtimestamp(number, tz)
    except (OverflowError, OSError, ValueError):
        return None


def _net(row):
    """Return net and whether its funding basis is demonstrably current."""
    gross, entry_fee, exit_fee, funding = [_number(row.get(key)) for key in
                                         ('gross_pnl', 'entry_fee', 'exit_fee', 'funding')]
    if all(value is not None for value in (gross, entry_fee, exit_fee, funding)):
        return gross-entry_fee-exit_fee-funding, True
    net = _number(row.get('close_net'))
    at_close = _number(row.get('funding_at_close'))
    if net is not None and at_close is not None and funding is not None:
        # Only use this basis if producer explicitly retains original close_net.
        if row.get('close_net_basis') == 'at_close':
            return net-(funding-at_close), True
    if net is not None and row.get('close_net_basis') == 'current':
        return net, True
    return net, False


def daily_trade_report(ledger_state, timezone='Europe/Istanbul', day=None, max_rows=20):
    """Summarize closures for one local day without mutating the ledger.

    Unknown closure times cannot be assigned to a day and are counted separately.
    max_rows bounds Telegram detail, while totals include all matched records.
    """
    if not isinstance(ledger_state, dict):
        raise ValueError('ledger state must be a dictionary')
    tz = ZoneInfo(timezone)
    if day is None:
        selected = datetime.now(tz).date()
    elif isinstance(day, datetime):
        selected = day.astimezone(tz).date() if day.tzinfo else day.date()
    elif isinstance(day, date):
        selected = day
    elif isinstance(day, str):
        selected = date.fromisoformat(day)
    else:
        raise ValueError('day must be an ISO date or date object')
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= 50:
        raise ValueError('max_rows must be between 1 and 50')
    rows = ledger_state.get('closed_positions', [])
    if not isinstance(rows, list):
        raise ValueError('closed_positions must be a list')
    matched, unassigned, duplicates, seen = [], 0, 0, set()
    for record in rows:
        if not isinstance(record, dict):
            unassigned += 1
            continue
        closed = _when(record.get('closed_at'), tz)
        if closed is None:
            unassigned += 1
            continue
        if closed.date() != selected:
            continue
        ident = (str(record.get('symbol', 'unknown')), str(record.get('opened_at')), closed.timestamp())
        if ident in seen:
            duplicates += 1
            continue
        seen.add(ident)
        net, current_basis = _net(record)
        matched.append(dict(
            symbol=str(record.get('symbol') or 'unknown'),
            side=str(record.get('side') or 'unknown'),
            strategy=str(record.get('strategy') or 'unknown'),
            reason=str(record.get('close_reason') or record.get('reason') or 'unknown'),
            leverage=_number(record.get('leverage')),
            entry=_number(record.get('entry')),
            exit_price=_number(record.get('exit_price')),
            closed_at=closed.isoformat(),
            net_pnl=net,
            net_complete=current_basis,
            entry_fee=_number(record.get('entry_fee')),
            exit_fee=_number(record.get('exit_fee')),
            funding=_number(record.get('funding')),
            mfe_r=_number(record.get('mfe_r', record.get('max_favorable_r'))),
            mae_r=_number(record.get('mae_r', record.get('max_adverse_r'))),
        ))
    matched.sort(key=lambda record: record['closed_at'], reverse=True)
    known = [record for record in matched if record['net_pnl'] is not None]
    positions = ledger_state.get('positions', {})
    positions = positions if isinstance(positions, dict) else {}
    unrealized_values = [_number(record.get('unrealized')) for record in positions.values() if isinstance(record, dict)]
    unrealized_complete = len(unrealized_values) == len(positions) and all(value is not None for value in unrealized_values)
    unrealized = sum(value for value in unrealized_values if value is not None)
    cash = _number(ledger_state.get('cash'))
    current_equity = cash+unrealized if cash is not None and unrealized_complete else None
    cost_fields = ('entry_fee', 'exit_fee', 'funding')
    costs = {key: sum(record[key] for record in matched if record[key] is not None) for key in cost_fields}
    warnings = []
    if unassigned:
        warnings.append(f'{unassigned} eski kayıt kapanış zamanı eksik/geçersiz olduğu için günlere atanamadı.')
    if any(not record['net_complete'] for record in matched):
        warnings.append('Bazı eski işlemlerin net PnL/funding temeli eksik; gösterilen toplam doğrulanmış tam günlük sonuç değildir.')
    if any(record[key] is None for record in matched for key in cost_fields):
        warnings.append('Bazı işlemlerde ücret veya funding ayrıntısı eksik; maliyet toplamları yalnız bilinen alanları içerir.')
    if duplicates:
        warnings.append(f'{duplicates} yinelenen kapanış kaydı toplamdan çıkarıldı.')
    if not unrealized_complete:
        warnings.append('Açık PnL alanları eksik; güncel equity hesaplanamadı.')
    return dict(
        mode='PAPER', day=selected.isoformat(), timezone=timezone,
        currency=str(ledger_state.get('currency') or 'USDT'),
        closed_trades=len(matched), known_pnl_trades=len(known),
        wins=sum(record['net_pnl'] > 0 for record in known),
        losses=sum(record['net_pnl'] < 0 for record in known),
        breakeven=sum(record['net_pnl'] == 0 for record in known),
        net_pnl=sum(record['net_pnl'] for record in known),
        pnl_complete=all(record['net_complete'] for record in matched),
        cost_attribution='trades_closed_on_selected_day',
        closed_trade_entry_fees=costs['entry_fee'],
        closed_trade_exit_fees=costs['exit_fee'],
        closed_trade_funding=costs['funding'],
        strategies={name:sum(record['strategy'] == name for record in matched) for name in sorted({record['strategy'] for record in matched})},
        sides={name:sum(record['side'] == name for record in matched) for name in sorted({record['side'] for record in matched})},
        trades=matched[:max_rows], omitted_detail_rows=max(0,len(matched)-max_rows),
        unassigned_legacy_records=unassigned,
        current_equity=current_equity,
        current_unrealized=unrealized if unrealized_complete else None,
        current_open_positions=len(positions),
        all_time_realized=_number(ledger_state.get('realized')),
        warnings=warnings,
    )


def format_daily_report(report):
    """Compact plain Telegram text. No Markdown injection or secret fields."""
    def fmt(value, signed=False):
        number = _number(value)
        return 'bilinmiyor' if number is None else format(number, '+.4f' if signed else '.4f')
    currency = report['currency']
    qualifier = 'Net sonuç' if report['pnl_complete'] else 'Bilinen/kısmi net sonuç'
    text = (f"PAPER günlük rapor | {report['day']} ({report['timezone']})\n"
            f"Kapanan: {report['closed_trades']} | Kâr: {report['wins']} | Zarar: {report['losses']} | Başabaş: {report['breakeven']}\n"
            f"{qualifier}: {fmt(report['net_pnl'], True)} {currency}\n"
            f"Kapanan işlemlerin ücretleri: {fmt(report['closed_trade_entry_fees']+report['closed_trade_exit_fees'])} {currency}\n"
            f"Kapanan işlemlerin funding'i: {fmt(report['closed_trade_funding'], True)} {currency}\n"
            f"Güncel kasa: {fmt(report['current_equity'])} | Açık PnL: {fmt(report['current_unrealized'], True)} {currency}\n")
    for row in report['trades']:
        leverage = fmt(row['leverage']).rstrip('0').rstrip('.') if row['leverage'] is not None else '?'
        text += (f"{row['closed_at'][11:16]} {row['symbol']} {row['side']} {leverage}x | {row['strategy']} | "
                 f"{row['reason']} | {fmt(row['net_pnl'], True)} {currency}\n")
        if row['mfe_r'] is not None or row['mae_r'] is not None:
            text += f"MFE {fmt(row['mfe_r'])}R | MAE {fmt(row['mae_r'])}R\n"
    if report['omitted_detail_rows']:
        text += f"+{report['omitted_detail_rows']} işlem ayrıntısı gösterilmedi; toplamda dâhil.\n"
    for warning in report['warnings']:
        text += warning+'\n'
    return text[:3900]
