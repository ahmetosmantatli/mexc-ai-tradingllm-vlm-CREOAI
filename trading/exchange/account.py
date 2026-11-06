"""Strict USDT futures account reads, separate from the paper ledger."""
from dataclasses import asdict, dataclass
from math import isfinite
from time import time


def number(value):
    result = float(value)
    if not isfinite(result):
        raise ValueError('non-finite account number')
    return result


@dataclass(frozen=True)
class FuturesAccount:
    equity: float
    available: float
    position_margin: float
    unrealized: float
    observed_at: float
    currency: str = 'USDT'

    @classmethod
    def parse(cls, payload):
        if not isinstance(payload, dict) or payload.get('success') is not True or payload.get('code', 0) != 0:
            raise ValueError('MEXC account response unsuccessful')
        rows = payload.get('data')
        if not isinstance(rows, list):
            raise ValueError('MEXC assets missing')
        rows = [r for r in rows if isinstance(r, dict) and r.get('currency') == 'USDT']
        if len(rows) != 1:
            raise ValueError('expected one USDT futures asset')
        row = rows[0]
        equity, available = number(row['equity']), number(row['availableBalance'])
        if equity < 0 or available < 0:
            raise ValueError('negative account balance')
        return cls(equity, available, number(row['positionMargin']), number(row['unrealized']), time())

    def public(self):
        return asdict(self)
