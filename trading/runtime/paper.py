"""Persistent paper positions; conservative isolated liquidation approximation.

This models observations, not guaranteed stop execution or exchange liquidation parity.
"""
from datetime import datetime
from math import isfinite
from zoneinfo import ZoneInfo
from time import time
from functools import wraps

def synchronized(fn):
    @wraps(fn)
    def call(self, *args, **kwargs):
        with self.store.lock:
            return fn(self, *args, **kwargs)
    return call



def day_key():
    return datetime.now(ZoneInfo('Europe/Istanbul')).date().isoformat()


class PaperLedger:
    def __init__(self, store, seed, fee_rate=0.0006, slippage_bps=5):
        self.store, self.fee, self.slip = store, fee_rate, slippage_bps / 10000
        self.state = store.get('paper')
        if self.state is None:
            if not isfinite(seed) or seed < 0:
                raise ValueError('invalid seed')
            self.state = dict(currency='USDT', cash=seed, initial=seed, positions={}, seen=[],
                              events=[], fees=0., funding=0., peak=seed, day=day_key(), day_start=seed,
                              realized=0., liquidation_count=0)
            self.save()

    @synchronized
    def save(self):
        self.store.put('paper', self.state)

    @property
    def positions(self):
        return self.state['positions']

    @property
    @synchronized
    def equity(self):
        return self.state['cash'] + sum(p['unrealized'] for p in self.positions.values())

    @property
    @synchronized
    def available(self):
        return max(0, self.equity - sum(p['margin'] for p in self.positions.values()))

    @synchronized
    def metrics(self):
        equity = self.equity
        return dict(currency='USDT', equity=equity, available=self.available,
                    realized=self.state['realized'], unrealized=equity-self.state['cash'],
                    fees=self.state['fees'], funding=self.state['funding'],
                    open_positions=len(self.positions),
                    daily_loss_pct=max(0, 1-equity/self.state['day_start']) if self.state['day_start'] else 0,
                    drawdown_pct=max(0, 1-equity/self.state['peak']) if self.state['peak'] else 0,
                    liquidation_count=self.state['liquidation_count'])

    @synchronized
    def event(self, kind, **data):
        from uuid import uuid4
        self.state['events'].append(dict(id=uuid4().hex, kind=kind, sent=False, **data))

    @synchronized
    def open(self, proposal, rules, bid, ask, signal_key, max_risk_pct, liquidation_buffer):
        if signal_key in self.state['seen'] or proposal.symbol in self.positions:
            return None
        if not all(isfinite(v) and v > 0 for v in (bid, ask)) or ask < bid:
            raise ValueError('invalid spread')
        long = proposal.side.value == 'long'
        entry = ask * (1+self.slip) if long else bid * (1-self.slip)
        sl = proposal.stop_loss.price
        tp = proposal.take_profit_price
        if sl is None or tp is None or not (sl < entry < tp if long else tp < entry < sl):
            raise ValueError('invalid stop/target direction')
        # Round size down using actual executable entry, including fees and stop slippage.
        exit_sl = sl*(1-self.slip if long else 1+self.slip)
        loss_fraction = abs(entry-exit_sl)/entry + self.fee*(1+exit_sl/entry)
        budget = min(proposal.max_loss_if_stopped, self.equity*max_risk_pct)
        notional = min(proposal.notional, budget/loss_fraction,
                       self.available / (1/proposal.leverage + self.fee) * 0.95)
        contracts, notional = rules.size_order(notional, entry, proposal.leverage)
        quantity = notional / entry
        margin = notional / proposal.leverage
        entry_fee = notional*self.fee
        if margin+entry_fee > self.available:
            raise ValueError('insufficient margin')
        # Upper-tier risk is not inferred: only the base tier is accepted by the runner.
        distance = 1/proposal.leverage-rules.maintenance-self.fee
        if distance <= 0:
            raise ValueError('invalid liquidation distance')
        liq = entry*(1-distance if long else 1+distance)
        stop_buffer = (sl-liq)/entry if long else (liq-sl)/entry
        if stop_buffer < liquidation_buffer:
            raise ValueError('stop too close to estimated liquidation')
        if abs(entry-exit_sl)*quantity + entry_fee + exit_sl*quantity*self.fee > budget+1e-8:
            raise ValueError('stop loss exceeds risk budget')
        self.state['cash'] -= entry_fee
        self.state['fees'] += entry_fee
        self.positions[proposal.symbol] = dict(symbol=proposal.symbol, side=proposal.side.value,
            entry=entry, stop=sl, target=tp, quantity=quantity, contracts=contracts,
            notional=notional, leverage=proposal.leverage, margin=margin, liquidation=liq,
            opened_at=int(time()*1000), unrealized=0., risk=budget, entry_fee=entry_fee, funding=0., funding_time=None)
        self.state['seen'].append(signal_key)
        self.event('OPEN', **self.positions[proposal.symbol], equity=self.equity)
        self.save()
        return self.positions[proposal.symbol]

    @synchronized
    def mark(self, symbol, bid, ask, fair):
        if not all(isfinite(v) and v > 0 for v in (bid, ask, fair)) or ask < bid:
            raise ValueError('invalid position market data')
        p = self.positions[symbol]
        long = p['side'] == 'long'
        price = bid if long else ask
        p['unrealized'] = (price-p['entry'])*p['quantity']*(1 if long else -1)
        # Liquidation wins over stop on a gap through both levels.
        liquidated = fair <= p['liquidation'] if long else fair >= p['liquidation']
        stop = price <= p['stop'] if long else price >= p['stop']
        target = price >= p['target'] if long else price <= p['target']
        if liquidated or stop or target:
            exit_price = price*(1-self.slip if long else 1+self.slip)
            pnl = (exit_price-p['entry'])*p['quantity']*(1 if long else -1)
            fee = exit_price*p['quantity']*self.fee
            if liquidated:
                # Conservative isolated collateral loss; liquidation fees not modeled exactly.
                pnl = -p['margin']
                self.state['liquidation_count'] += 1
            self.state['cash'] += pnl-fee
            self.state['fees'] += fee
            net = pnl-fee-p['entry_fee']-p['funding']
            self.state['realized'] += net
            p['closed_at'] = int(time()*1000)
            p['close_net'] = net
            self.state.setdefault('closed_positions', []).append(dict(p))
            del self.positions[symbol]
            self.event('CLOSE', symbol=symbol, side=p['side'], reason='LIQ' if liquidated else 'SL' if stop else 'TP',
                       price=exit_price, net_pnl=net, equity=self.equity)
        self.state['peak'] = max(self.state['peak'], self.equity)
        self.save()

    @synchronized
    def settle_funding(self, symbol, timestamp, rate, fair):
        p = self.positions[symbol]
        if p['funding_time'] is not None and timestamp <= p['funding_time']:
            return
        if not all(isfinite(float(v)) for v in (timestamp, rate, fair)) or fair <= 0:
            raise ValueError('invalid funding')
        charge = p['quantity']*fair*rate*(1 if p['side']=='long' else -1)
        self.state['cash'] -= charge
        self.state['funding'] += charge
        p['funding'] += charge
        p['funding_time'] = timestamp
        self.save()

    @synchronized
    def close_manual(self, symbol, bid, ask, opened_at=None):
        position = self.positions.get(symbol)
        if position is None or (opened_at is not None and position['opened_at'] != opened_at):
            raise ValueError('position no longer matches confirmed request')
        if not all(isfinite(v) and v>0 for v in (bid,ask)) or ask<bid:
            raise ValueError('invalid close quote')
        long=position['side']=='long'
        price=(bid*(1-self.slip)) if long else (ask*(1+self.slip))
        pnl=(price-position['entry'])*position['quantity']*(1 if long else -1)
        fee=price*position['quantity']*self.fee
        self.state['cash']+=pnl-fee
        self.state['fees']+=fee
        net=pnl-fee-position['entry_fee']-position['funding']
        self.state['realized']+=net
        position['closed_at']=int(time()*1000)
        position['close_net']=net
        self.state.setdefault('closed_positions',[]).append(dict(position))
        del self.positions[symbol]
        self.event('CLOSE',symbol=symbol,side=position['side'],reason='MANUAL',price=price,net_pnl=net,equity=self.equity)
        self.save()

    @synchronized
    def reconcile_closed_funding(self, symbol, opened_at, timestamp, rate, fair):
        position = next(p for p in self.state.get('closed_positions', [])
                        if p['symbol']==symbol and p['opened_at']==opened_at)
        if not position['opened_at'] < timestamp <= position['closed_at']:
            return
        if position['funding_time'] is not None and timestamp <= position['funding_time']:
            return
        if not all(isfinite(float(v)) for v in (timestamp, rate, fair)) or fair <= 0:
            raise ValueError('invalid funding')
        charge = position['quantity']*fair*rate*(1 if position['side']=='long' else -1)
        self.state['cash'] -= charge
        self.state['funding'] += charge
        self.state['realized'] -= charge
        position['funding'] += charge
        position['funding_time'] = timestamp
        self.event('FUNDING_ADJUSTMENT', symbol=symbol, charge=charge, equity=self.equity)
        self.save()

    @synchronized
    def roll_day(self):
        if self.state['day'] != day_key():
            self.state['day'], self.state['day_start'] = day_key(), self.equity
            self.save()
