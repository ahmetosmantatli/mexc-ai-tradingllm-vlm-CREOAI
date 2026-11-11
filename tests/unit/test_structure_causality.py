"""Prefix invariance: bars after the observation time never change a card."""
import random

from trading.analysis.market import build_bundle
from trading.quant.structure_setups import SECONDS, build_htf_zone_setups

INTERVALS = {'Min5': '5m', 'Min15': '15m', 'Min60': '1h', 'Hour4': '4h'}
START = 1_700_000_000 - 1_700_000_000 % 14400


def _series(seed=7, days=24):
    rng = random.Random(seed)
    price, drift, rows = 100., 0., []
    for index in range(days * 288):
        if index % 144 == 0:
            drift = rng.choice((-1, 1)) * rng.uniform(.0002, .0012)
        close = price * (1 + drift + rng.gauss(0, .003))
        high = max(price, close) * (1 + abs(rng.gauss(0, .0015)))
        low = min(price, close) * (1 - abs(rng.gauss(0, .0015)))
        rows.append([START + index * 300, price, high, low, close, rng.uniform(1, 9)])
        price = close
    frames = {'5m': rows}
    for name in ('15m', '1h', '4h'):
        step = SECONDS[name] // 300
        frames[name] = [[chunk[0][0], chunk[0][1], max(r[2] for r in chunk), min(r[3] for r in chunk),
                         chunk[-1][4], sum(r[5] for r in chunk)]
                        for chunk in (rows[i:i + step] for i in range(0, len(rows) - step + 1, step))]
    return frames


class _Client:
    def __init__(self, frames, cutoff=None):
        self.frames, self.cutoff = frames, cutoff

    def get_klines(self, symbol, interval, limit):
        name = INTERVALS[interval]
        rows = self.frames[name]
        if self.cutoff is not None:
            rows = [row for row in rows if row[0] + SECONDS[name] <= self.cutoff]
        return dict(symbol=symbol, interval=interval, candles=[list(row) for row in rows[-limit:]]
                    if self.cutoff is not None else [list(row) for row in rows])


def _cards(client, observed, price):
    market = build_bundle(client, 'TEST_USDT', now=observed, include_hourly=True).to_context(ict=True, htf_history=True)
    return build_htf_zone_setups(market, current_price=price, price_step=.0001)


def _references(card):
    evidence = card['evidence']
    for ref in (card['stop_reference'], card['target_reference'], *card['opposing_target_levels'],
                evidence.get('shift'), evidence.get('candidate_block'), evidence.get('trigger_candle')):
        if isinstance(ref, dict):
            stamp = ref.get('open_time', ref.get('at'))
            if stamp is not None:
                yield ref.get('timeframe', card['trigger_timeframe']), stamp


def test_future_bars_never_change_htf_zone_or_early_cards():
    frames = _series()
    full = _Client(frames)
    five = frames['5m']
    seen = dict(ready=0, internal=0, total=0)
    for row in five[-288 * 2:-12:3]:
        observed = row[0] + 300 + 1
        price = row[4]
        truncated = _cards(_Client(frames, cutoff=observed), observed, price)
        assert _cards(full, observed, price) == truncated
        for card in truncated:
            seen['total'] += 1
            seen['ready'] += card['state'] == 'ready'
            seen['internal'] += card['evidence'].get('kind') == 'htf_zone_internal'
            for timeframe, stamp in _references(card):
                assert stamp + SECONDS[timeframe] <= observed
    assert seen['total'] and seen['ready'] and seen['internal']
