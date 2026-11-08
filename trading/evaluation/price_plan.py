"""Grounded market-entry price proposals; no orders or risk-cap bypass.

The model references only the closed candles it was shown. It may propose
wick-based stops/targets, but cannot create pending/limit orders or change the
selected symbol/side. Validation is repeated against immutable evidence and
current executable price before LIVE sizing.
"""
from decimal import Decimal
from math import isfinite
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field
from trading.quant.signals import CandidateSignal


class CandleReference(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    timeframe: Literal['5m','15m','4h']
    open_time: int = Field(ge=0)
    field: Literal['low','high']


class MarketPricePlan(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    entry_type: Literal['market']
    entry_price: float = Field(gt=0)
    stop_loss_price: float = Field(gt=0)
    take_profit_price: float = Field(gt=0)
    entry_timeframe: Literal['5m','15m','4h']
    stop_reference: CandleReference
    target_reference: CandleReference
    reason: str = Field(min_length=12,max_length=600)


class PricePlanError(ValueError):pass


def number(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not isfinite(value) or value<=0:
        raise PricePlanError('invalid_price_evidence')
    return float(value)


def evidence_from_market(market):
    """Immutable, bounded evidence exactly matching the model's recent OHLCV."""
    result={}
    for view in market.get('timeframes',[]):
        tf=view.get('timeframe')
        if tf not in ('5m','15m','4h'):continue
        rows=view.get('recent_ohlcv',[])
        if len(rows)>12:rows=rows[-12:]
        result[tf]=[list(row[:6]) for row in rows if isinstance(row,(list,tuple)) and len(row)>=6]
    return result


def _reference(ref,evidence,now):
    rows=evidence.get(ref.timeframe,[])
    interval={'5m':300,'15m':900,'4h':14400}[ref.timeframe]
    matches=[row for row in rows if type(row[0]) in (int,float) and row[0]==ref.open_time]
    if len(matches)!=1 or ref.open_time+interval>now:
        raise PricePlanError('missing_or_open_candle_reference')
    ranges=[];previous=None
    for row in rows:
        high,low,close=number(row[2]),number(row[3]),number(row[4])
        if high<low:raise PricePlanError('invalid_candle_range')
        ranges.append(max(high-low,abs(high-previous),abs(low-previous)) if previous is not None else high-low)
        previous=close
    atr=sum(ranges)/len(ranges) if ranges else 0
    row=matches[0]
    return number(row[3] if ref.field=='low' else row[2]),atr


def apply_market_price_plan(candidate,raw,evidence,*,price_step,current_price,now,max_entry_drift=.005):
    """Return a validated clone, retaining original signal identity/queue key."""
    plan=MarketPricePlan.model_validate(raw.model_dump() if isinstance(raw,MarketPricePlan) else raw)
    step=number(price_step);current=number(current_price)
    if not evidence.get(plan.entry_timeframe):raise PricePlanError('missing_entry_timeframe')
    for price in (plan.entry_price,plan.stop_loss_price,plan.take_profit_price):
        units=Decimal(str(price))/Decimal(str(step))
        if abs(units-units.to_integral_value())>Decimal('0.000001'):
            raise PricePlanError('price_tick_mismatch')
    if abs(plan.entry_price/current-1)>max_entry_drift:
        raise PricePlanError('market_entry_reference_too_far')
    long=candidate.side.value=='long'
    stop,target=plan.stop_loss_price,plan.take_profit_price
    if not (stop<plan.entry_price<target and stop<current<target if long else target<plan.entry_price<stop and target<current<stop):
        raise PricePlanError('price_plan_direction_invalid')
    stop_anchor,stop_atr=_reference(plan.stop_reference,evidence,now)
    target_anchor,target_atr=_reference(plan.target_reference,evidence,now)
    if plan.stop_reference.field!=('low' if long else 'high') or plan.target_reference.field!=('high' if long else 'low'):
        raise PricePlanError('wrong_reference_direction')
    if not (stop<=stop_anchor<plan.entry_price if long else stop>=stop_anchor>plan.entry_price):
        raise PricePlanError('stop_not_beyond_observed_wick')
    if abs(stop-stop_anchor)>max(stop_atr,step)+step*1e-6:
        raise PricePlanError('stop_too_far_from_reference')
    if not (target_anchor>plan.entry_price if long else target_anchor<plan.entry_price):
        raise PricePlanError('target_reference_wrong_side')
    if abs(target-target_anchor)>max(target_atr,step)+step*1e-6:
        raise PricePlanError('target_too_far_from_reference')
    clone=CandidateSignal.model_validate(candidate.model_dump())
    clone.entry_price=plan.entry_price;clone.stop_loss_price=stop;clone.take_profit_price=target
    clone.rationale=plan.reason
    return clone
