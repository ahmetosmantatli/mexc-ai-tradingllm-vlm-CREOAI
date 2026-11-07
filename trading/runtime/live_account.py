"""Read-only MEXC Futures reconciliation and account-scoped live preflight.

No trading action is performed here. Equity and available margin come directly
from the authenticated exchange, never from a paper ledger or an env seed.
The caller binds a credential generation to an account when it connects a key.

Sources (MEXC official Futures docs, reviewed 2026-11-07): account/assets,
position/open_positions, order/list/open_orders, account/risk_limit,
position/leverage, position/position_mode at https://www.mexc.com/api-docs/futures/.
MEXC's position-mode example is inconsistent with its field description: only an
explicit integer1/2 is accepted; arbitrary example arrays are never guessed.
"""
from dataclasses import asdict, dataclass
from math import isfinite
from time import time
from uuid import uuid4
import copy
import re
import sqlite3


class LiveAccountError(ValueError):
    """Stable diagnostic codes only; raw exchange responses never become errors."""


def _number(value,field,*,minimum=None,positive=False):
    if isinstance(value,bool):
        raise LiveAccountError('INVALID_'+field.upper())
    try:
        value=float(value)
    except (TypeError,ValueError,OverflowError):
        raise LiveAccountError('INVALID_'+field.upper()) from None
    if not isfinite(value) or minimum is not None and value<minimum or positive and value<=0:
        raise LiveAccountError('INVALID_'+field.upper())
    return value


def _integer(value,field,allowed=None):
    number=_number(value,field)
    if number!=int(number) or allowed is not None and int(number) not in allowed:
        raise LiveAccountError('INVALID_'+field.upper())
    return int(number)


def _symbol(value):
    if not isinstance(value,str) or not re.fullmatch('[A-Z0-9]+_USDT',value):
        raise LiveAccountError('UNSUPPORTED_SYMBOL')
    return value


def _identifier(value,field):
    if isinstance(value,bool) or not isinstance(value,(str,int)) or not str(value).strip() or len(str(value))>128:
        raise LiveAccountError('INVALID_'+field.upper())
    return str(value)


def _rows(value,field):
    if not isinstance(value,list) or not all(isinstance(row,dict) for row in value):
        raise LiveAccountError('INVALID_'+field.upper())
    return value


def _balance(assets):
    rows=[row for row in _rows(assets,'assets') if row.get('currency')=='USDT']
    if len(rows)!=1:
        raise LiveAccountError('USDT_ASSET_MISSING_OR_DUPLICATED')
    row=rows[0]
    balance=dict(currency='USDT',equity=_number(row.get('equity'),'equity',minimum=0),
        available=_number(row.get('availableBalance'),'available',minimum=0),
        position_margin=_number(row.get('positionMargin'),'position_margin',minimum=0),
        unrealized=_number(row.get('unrealized'),'unrealized'))
    for raw,canonical in [('cashBalance','cash'),('frozenBalance','frozen'),('availableOpen','available_open')]:
        balance[canonical]=_number(row[raw],canonical,minimum=0) if raw in row else None
    # Do not infer equity from available+margin: collateral/bonus/frozen/funding
    # semantics vary. Preserve distinct exchange-reported values.
    return balance


def _positions(rows):
    result=[]
    for row in _rows(rows,'positions'):
        state=_integer(row.get('state'),'position_state',{1,2,3})
        volume=_number(row.get('holdVol'),'position_volume',minimum=0)
        if state==3 or volume==0:
            continue
        result.append(dict(position_id=_identifier(row.get('positionId'),'position_id'),
            symbol=_symbol(row.get('symbol')),side=_integer(row.get('positionType'),'position_side',{1,2}),
            open_type=_integer(row.get('openType'),'open_type',{1,2}),state=state,contracts=volume,
            leverage=_number(row.get('leverage'),'position_leverage',positive=True),
            entry=_number(row.get('holdAvgPrice'),'position_entry',positive=True),
            margin=_number(row.get('im'),'position_margin',minimum=0),
            unrealized=_number(row['unRealizedPnl'],'position_unrealized') if 'unRealizedPnl' in row else None,
            liquidation_price=_number(row['liquidatePrice'],'liquidation_price',minimum=0) if 'liquidatePrice' in row else None))
    ids=[row['position_id'] for row in result]
    if len(ids)!=len(set(ids)):
        raise LiveAccountError('DUPLICATE_POSITION_IDS')
    return result


def _orders(rows):
    result=[]
    for row in _rows(rows,'orders'):
        state=_integer(row.get('state'),'order_state',{1,2,3,4,5})
        if state not in (1,2):
            continue
        vol=_number(row.get('vol'),'order_volume',positive=True)
        filled=_number(row.get('dealVol'),'filled_volume',minimum=0)
        if filled>vol:
            raise LiveAccountError('FILLED_VOLUME_EXCEEDS_ORDER')
        result.append(dict(order_id=_identifier(row.get('orderId'),'order_id'),symbol=_symbol(row.get('symbol')),
            state=state,side=_integer(row.get('side'),'order_side',{1,2,3,4}),contracts=vol,filled_contracts=filled,
            leverage=_number(row.get('leverage'),'order_leverage',positive=True),
            open_type=_integer(row.get('openType'),'order_open_type',{1,2}),
            position_mode=_integer(row.get('positionMode'),'order_position_mode',{1,2}),
            order_margin=_number(row.get('orderMargin'),'order_margin',minimum=0)))
    ids=[row['order_id'] for row in result]
    if len(ids)!=len(set(ids)):
        raise LiveAccountError('DUPLICATE_ORDER_IDS')
    return result


def _risk(rows,symbol):
    if isinstance(rows,dict):
        rows=rows.get(symbol)
    result=[]
    for row in _rows(rows,'risk_limits'):
        if _symbol(row.get('symbol'))!=symbol:
            raise LiveAccountError('RISK_SYMBOL_MISMATCH')
        result.append(dict(symbol=symbol,side=_integer(row.get('positionType'),'risk_side',{1,2}),
            level=_integer(row.get('level'),'risk_level'),max_contracts=_number(row.get('maxVol'),'risk_volume',positive=True),
            max_leverage=_number(row.get('maxLeverage'),'risk_leverage',positive=True),
            maintenance_rate=_number(row.get('mmr'),'maintenance_rate',positive=True),
            initial_rate=_number(row.get('imr'),'initial_rate',positive=True)))
    if any(row['level']<1 or row['maintenance_rate']>=1 or row['initial_rate']>1 for row in result):
        raise LiveAccountError('UNSUPPORTED_RISK_LIMIT')
    if len({row['side'] for row in result})!=len(result) or {row['side'] for row in result}!={1,2}:
        raise LiveAccountError('RISK_SIDES_MISSING_OR_DUPLICATED')
    return result


def _leverage(rows,symbol):
    result=[]
    for row in _rows(rows,'leverage'):
        if row.get('symbol',symbol)!=symbol:
            raise LiveAccountError('LEVERAGE_SYMBOL_MISMATCH')
        # The user leverage endpoint returns side-specific settings; symbol can
        # be omitted because it is bound in the authenticated request parameter.
        result.append(dict(symbol=symbol,side=_integer(row.get('positionType'),'leverage_side',{1,2}),
            open_type=_integer(row.get('openType'),'leverage_open_type',{1,2}),
            leverage=_number(row.get('leverage'),'configured_leverage',positive=True),
            max_contracts=_number(row.get('maxVol'),'leverage_volume',positive=True),
            maintenance_rate=_number(row.get('currentMmr',row.get('mmr')),'current_maintenance_rate',positive=True),
            max_leverage=_number(row.get('maxLeverageView',row.get('maxLeverage')),'configured_max_leverage',positive=True)))
    if len({row['side'] for row in result})!=len(result) or {row['side'] for row in result}!={1,2}:
        raise LiveAccountError('LEVERAGE_SIDES_MISSING_OR_DUPLICATED')
    if any(row['maintenance_rate']>=1 for row in result):
        raise LiveAccountError('UNSUPPORTED_MAINTENANCE_RATE')
    return result


@dataclass(frozen=True)
class LivePreflight:
    ready: bool
    reasons: tuple[str,...]
    account_id: str
    generation: str | None
    equity: float | None
    available: float | None
    allowed_capital: float | None
    planned_margin: float | None

    def context(self):
        return asdict(self)


class LiveAccountReconciler:
    """Account/credential-generation scoped read-only state; never place orders."""
    def __init__(self,account_id,store,*,binding_id,max_age_seconds=30,clock=time):
        if not re.fullmatch('[A-Za-z0-9_-]{1,64}',account_id):
            raise LiveAccountError('INVALID_ACCOUNT_ID')
        if not isinstance(binding_id,str) or not binding_id or len(binding_id)>128:
            raise LiveAccountError('INVALID_BINDING_ID')
        if not isfinite(max_age_seconds) or max_age_seconds<=0:
            raise LiveAccountError('INVALID_MAX_AGE')
        self.account_id,self.store,self.binding_id=account_id,store,binding_id
        self.max_age,self.clock=max_age_seconds,clock
        self.key='account:'+account_id+':live_snapshot'

    def _all_orders(self,client):
        orders=[]
        for page in range(1,11):
            rows=_rows(client.current_orders(page_num=page,page_size=100),'order_page')
            orders.extend(rows)
            if len(rows)<100:
                return orders
        raise LiveAccountError('ORDER_PAGINATION_INCOMPLETE')

    def refresh(self,client,*,symbols=()):
        started=self.clock()
        try:
            symbols=tuple(dict.fromkeys(_symbol(symbol) for symbol in symbols))
            if len(symbols)>40:
                raise LiveAccountError('TOO_MANY_SYMBOLS')
            balance=_balance(client.account_assets())
            positions=_positions(client.open_positions())
            orders=_orders(self._all_orders(client))
            mode=client.position_mode()
            # Do not coerce a contradictory documentation example into a mode.
            if type(mode) is not int or mode not in (1,2):
                raise LiveAccountError('UNKNOWN_POSITION_MODE')
            risks={symbol:_risk(client.risk_limits(symbol=symbol),symbol) for symbol in symbols}
            leverage={symbol:_leverage(client.leverage(symbol),symbol) for symbol in symbols}
            finished=self.clock()
            if finished<started or finished-started>self.max_age:
                raise LiveAccountError('RECONCILIATION_TOO_SLOW')
            snapshot=dict(schema='mexc-live-account-v1',mode='LIVE_READ_ONLY',account_id=self.account_id,
                binding_id=self.binding_id,generation=uuid4().hex,observed_at=finished,started_at=started,
                source='MEXC authenticated futures endpoints',currency='USDT',balance=balance,
                position_mode=mode,positions=positions,orders=orders,risk_limits=risks,leverage=leverage,
                ready=True,orders_complete=True)
            self.store.put(self.key,snapshot)
            return copy.deepcopy(snapshot)
        except sqlite3.Error:
            raise
        except Exception as exc:
            # A failed refresh revokes readiness. It never falls back to env or
            # returns a previous successful snapshot as if it were current.
            reason=str(exc) if isinstance(exc,LiveAccountError) else 'ACCOUNT_READ_FAILED'
            self.store.put(self.key,dict(schema='mexc-live-account-v1',mode='LIVE_READ_ONLY',account_id=self.account_id,
                binding_id=self.binding_id,generation=uuid4().hex,observed_at=self.clock(),ready=False,reason=reason))
            raise LiveAccountError(reason) from None

    def snapshot(self):
        row=self.store.get(self.key)
        if not isinstance(row,dict) or row.get('account_id')!=self.account_id or row.get('binding_id')!=self.binding_id:
            raise LiveAccountError('ACCOUNT_BINDING_MISMATCH')
        return copy.deepcopy(row)

    def preflight(self,*,symbol,side,contracts,leverage,notional,allowed_capital=None,
                  expected_generation=None,position_mode=2,managed_position_ids=(),managed_order_ids=()):
        """Conservative entry readiness; cannot authorize a submit by itself.

        requested position_mode is explicit (default one-way2); no mode/leverage
        mutation occurs. Only isolated margin is supported. External positions or
        orders require separate operator reconciliation, never silent adoption.
        """
        reasons=[]
        try:
            symbol=_symbol(symbol)
            side=_integer(side,'requested_side',{1,2})
            contracts=_number(contracts,'requested_contracts',positive=True)
            leverage=_number(leverage,'requested_leverage',positive=True)
            notional=_number(notional,'requested_notional',positive=True)
            if type(position_mode) is not int or position_mode not in (1,2):
                raise LiveAccountError('UNKNOWN_REQUESTED_POSITION_MODE')
            snapshot=self.snapshot()
            if not snapshot.get('ready'):
                raise LiveAccountError(snapshot.get('reason') or 'ACCOUNT_NOT_READY')
            now=self.clock()
            if now<snapshot['observed_at']-5 or now-snapshot['started_at']>self.max_age:
                raise LiveAccountError('ACCOUNT_SNAPSHOT_STALE')
            if expected_generation is not None and snapshot['generation']!=expected_generation:
                raise LiveAccountError('ACCOUNT_GENERATION_CHANGED')
            balance=snapshot['balance']
            equity,available=balance['equity'],balance['available']
            cap=equity if allowed_capital is None else _number(allowed_capital,'allowed_capital',positive=True)
            if cap>equity:
                reasons.append('ALLOCATION_EXCEEDS_CURRENT_EQUITY')
            if balance.get('available_open') is not None:
                available=min(available,balance['available_open'])
            if snapshot['position_mode']!=position_mode:
                reasons.append('POSITION_MODE_MISMATCH')
            if not snapshot.get('orders_complete'):
                reasons.append('OPEN_ORDERS_INCOMPLETE')
            known_positions={str(value) for value in managed_position_ids}
            known_orders={str(value) for value in managed_order_ids}
            if any(row['position_id'] not in known_positions for row in snapshot['positions']):
                reasons.append('UNRECONCILED_EXTERNAL_POSITIONS')
            if any(row['order_id'] not in known_orders for row in snapshot['orders']):
                reasons.append('UNRECONCILED_EXTERNAL_ORDERS')
            if any(row['open_type']!=1 or row['state']!=1 for row in snapshot['positions']):
                reasons.append('UNSUPPORTED_POSITION_STATE_OR_MARGIN_MODE')
            if any(row['open_type']!=1 or row['position_mode']!=position_mode for row in snapshot['orders']):
                reasons.append('UNSUPPORTED_ORDER_MARGIN_OR_POSITION_MODE')
            if any(row['symbol']==symbol for row in snapshot['positions']):
                reasons.append('SYMBOL_ALREADY_OPEN')
            risk=next(row for row in snapshot['risk_limits'].get(symbol,[]) if row['side']==side)
            setting=next(row for row in snapshot['leverage'].get(symbol,[]) if row['side']==side)
            if setting['open_type']!=1:
                reasons.append('ISOLATED_MARGIN_REQUIRED')
            if leverage>min(risk['max_leverage'],setting['max_leverage']):
                reasons.append('EXCHANGE_LEVERAGE_CAP')
            if abs(leverage-setting['leverage'])>1e-9:
                reasons.append('LEVERAGE_SETTING_MISMATCH')
            pending=sum(max(0,row['contracts']-row['filled_contracts']) for row in snapshot['orders'] if row['symbol']==symbol and row['side'] in (1,3))
            if contracts+pending>min(risk['max_contracts'],setting['max_contracts']):
                reasons.append('EXCHANGE_RISK_VOLUME_CAP')
            # Include documented initial rate if stricter than leverage-derived
            # margin. Fees/SL/liquidation and contract rounding remain broker gates.
            margin=notional*max(1/leverage,risk['initial_rate'])
            managed_margin=sum(row['margin'] for row in snapshot['positions'] if row['position_id'] in known_positions)
            managed_margin+=sum(row['order_margin'] for row in snapshot['orders'] if row['order_id'] in known_orders)
            if margin>=available or margin+managed_margin>cap:
                reasons.append('INSUFFICIENT_FREE_MARGIN')
            if equity<=0 or available<=0:
                reasons.append('NO_CURRENT_AVAILABLE_FUNDS')
            return LivePreflight(not reasons,tuple(reasons),self.account_id,snapshot['generation'],equity,available,cap,margin)
        except (LiveAccountError,KeyError,TypeError,StopIteration) as exc:
            reason=str(exc) if isinstance(exc,LiveAccountError) else 'SYMBOL_CONFIGURATION_UNAVAILABLE'
            return LivePreflight(False,(reason,),self.account_id,None,None,None,None,None)
