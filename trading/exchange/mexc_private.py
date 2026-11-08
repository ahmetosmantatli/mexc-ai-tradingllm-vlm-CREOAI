"""Official signed MEXC futures adapter; writes disabled unless explicitly enabled.

No automatic retries, browser tokens, undocumented endpoints or transfers. A
successful submit is only an acknowledgement; callers must reconcile fills and
native protection. Uncertain writes MUST be queried by external ID, not retried.
Verified official docs (2026-11-07):
https://www.mexc.com/api-docs/futures/integration-guide
https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/place-order
https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/place-tpsl-order-by-position
"""
import hashlib
import hmac
import json
from math import isfinite
import re
import time
from urllib.parse import urlencode
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator


class MexcPrivateError(RuntimeError):
    def __init__(self, operation, *, code=None, status_code=None, ambiguous=False):
        self.operation=operation
        self.code=code if isinstance(code,int) and not isinstance(code,bool) else None
        self.status_code=status_code if isinstance(status_code,int) else None
        self.ambiguous=bool(ambiguous)
        super().__init__(f'MEXC private {operation}: code={self.code}, http={self.status_code}, ambiguous={self.ambiguous}')


class MarketOrder(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    symbol: str = Field(pattern=r'^[A-Z0-9]{1,30}_USDT$')
    side: Literal['long','short']
    contracts: float = Field(gt=0)
    leverage: int = Field(ge=1)
    external_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    stop_loss: float = Field(gt=0)
    take_profit: float = Field(gt=0)
    entry_price: float = Field(gt=0)
    position_mode: Literal[1,2] = 1

    @field_validator('position_mode',mode='before')
    @classmethod
    def explicit_mode(cls,value):
        if type(value) is not int:
            raise ValueError('integer position mode required')
        return value


def _symbol(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Z0-9]{1,30}_USDT',value):
        raise ValueError('invalid USDT contract')
    return value


def _identifier(value):
    if isinstance(value,bool) or not isinstance(value,(str,int)) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',str(value)):
        raise ValueError('invalid request identifier')
    return str(value)


def _positive(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not isfinite(value) or value<=0:
        raise ValueError('positive finite number required')
    return value


def _position_id(value):
    if isinstance(value,bool) or not isinstance(value,(str,int)) or not re.fullmatch(r'[1-9][0-9]{0,25}',str(value)):
        raise ValueError('invalid position identifier')
    return int(value)


class MexcPrivateClient:
    def __init__(self, api_key, api_secret, *, base_url='https://api.mexc.com', writes_enabled=False,
                 client=None, timeout_seconds=5, recv_window_seconds=10, can_write=None):
        if base_url.rstrip('/')!='https://api.mexc.com':
            raise ValueError('official MEXC futures API host required')
        if type(writes_enabled) is not bool or not 1<=recv_window_seconds<=30:
            raise ValueError('invalid private client configuration')
        self._api_key,self._api_secret=api_key,api_secret
        self.base_url=base_url.rstrip('/')
        self.writes_enabled=writes_enabled
        self.can_write=can_write or (lambda:True)
        self.recv_window_seconds=recv_window_seconds
        self.client=client or httpx.Client(timeout=timeout_seconds,follow_redirects=False,trust_env=False)
        self._owned=client is None

    def __repr__(self):
        return f'MexcPrivateClient(writes_enabled={self.writes_enabled})'

    def close(self):
        if self._owned:
            self.client.close()

    def _request(self,method,path,*,operation,params=None,body=None,expected=None):
        write=method!='GET'
        if write and (not self.writes_enabled or not self.can_write()):
            raise MexcPrivateError('writes_disabled')
        if not self._api_key or not self._api_secret:
            raise MexcPrivateError('credentials_missing')
        query=urlencode(sorted((key,value) for key,value in (params or {}).items() if value is not None))
        content=json.dumps(body,separators=(',',':'),ensure_ascii=False,allow_nan=False) if body is not None else ''
        stamp=str(int(time.time()*1000))
        signed=self._api_key+stamp+(content if write else query)
        signature=hmac.new(self._api_secret.encode(),signed.encode(),hashlib.sha256).hexdigest()
        headers={'ApiKey':self._api_key,'Request-Time':stamp,'Signature':signature,
                 'Recv-Window':str(self.recv_window_seconds),'Content-Type':'application/json'}
        if write and (not self.writes_enabled or not self.can_write()):
            raise MexcPrivateError('writes_disabled')
        try:
            response=self.client.request(method,self.base_url+path+('?' + query if query else ''),
                headers=headers,content=content.encode() if write else None,follow_redirects=False)
        except httpx.TransportError:
            raise MexcPrivateError(operation,ambiguous=write) from None
        if response.status_code<200 or response.status_code>=300:
            # Never expose a provider response body, URL or request headers.
            raise MexcPrivateError(operation,status_code=response.status_code,
                ambiguous=write and (response.status_code>=500 or response.status_code in (408,499))) from None
        try:
            payload=response.json()
        except (ValueError,UnicodeError):
            raise MexcPrivateError(operation,status_code=response.status_code,ambiguous=write) from None
        valid_envelope=isinstance(payload,dict) and type(payload.get('success')) is bool and type(payload.get('code')) is int
        if not valid_envelope or payload['success'] is not True or payload['code']!=0:
            raise MexcPrivateError(operation,code=payload.get('code') if isinstance(payload,dict) else None,
                status_code=response.status_code,ambiguous=write and not valid_envelope)
        data=payload.get('data')
        if expected is not None and not isinstance(data,expected):
            raise MexcPrivateError(operation,status_code=response.status_code,ambiguous=write)
        return data

    @staticmethod
    def _page(page_num,page_size):
        if type(page_num) is not int or page_num<1 or type(page_size) is not int or not 1<=page_size<=100:
            raise ValueError('invalid pagination')
        return dict(page_num=page_num,page_size=page_size)

    def account_assets(self):
        return self._request('GET','/api/v1/private/account/assets',operation='assets',expected=list)

    def open_positions(self,symbol=None):
        return self._request('GET','/api/v1/private/position/open_positions',operation='positions',
            params={'symbol':_symbol(symbol) if symbol else None},expected=list)

    def current_orders(self,symbol=None,page_num=1,page_size=100):
        if symbol is not None:
            _symbol(symbol)
        rows=self._request('GET','/api/v1/private/order/list/open_orders',operation='current_orders',
            params=self._page(page_num,page_size),expected=list)
        return [row for row in rows if row.get('symbol')==symbol] if symbol else rows

    def order(self,order_id):
        return self._request('GET','/api/v1/private/order/get/'+_identifier(order_id),operation='order',expected=dict)

    def order_by_external_id(self,symbol,external_id):
        return self._request('GET','/api/v1/private/order/external/'+_symbol(symbol)+'/'+_identifier(external_id),
            operation='external_order',expected=dict)

    def historical_positions(self,symbol=None,page_num=1,page_size=100):
        params={**self._page(page_num,page_size),'symbol':_symbol(symbol) if symbol else None}
        data=self._request('GET','/api/v1/private/position/list/history_positions',
            operation='historical_positions',params=params,expected=(list,dict))
        rows=data.get('resultList') if isinstance(data,dict) else data
        if not isinstance(rows,list):raise MexcPrivateError('historical_positions')
        return rows

    def order_deals(self,order_id):
        return self._request('GET','/api/v1/private/order/deal_details/'+_identifier(order_id),
            operation='order_deals',expected=list)

    def leverage(self,symbol):
        return self._request('GET','/api/v1/private/position/leverage',operation='leverage',params={'symbol':_symbol(symbol)},expected=list)

    def position_mode(self):
        data=self._request('GET','/api/v1/private/position/position_mode',operation='position_mode',expected=int)
        if type(data) is not int or data not in (1,2):
            raise MexcPrivateError('invalid_position_mode')
        return data

    def risk_limits(self,symbol=None):
        return self._request('GET','/api/v1/private/account/risk_limit',operation='risk_limits',
            params={'symbol':_symbol(symbol) if symbol else None},expected=(dict,list))

    def protection_orders(self,symbol=None,page_num=1,page_size=100,*,finished=0):
        if type(finished) is not int or finished not in (0,1):
            raise ValueError('invalid protection completion filter')
        return self._request('GET','/api/v1/private/stoporder/list/orders',operation='protection_orders',
            params=dict(self._page(page_num,page_size),symbol=_symbol(symbol) if symbol else None,is_finished=finished),expected=list)

    def submit_market(self,*,symbol,side,contracts,leverage,external_id,stop_loss,take_profit,entry_price,position_mode=1):
        order=MarketOrder(symbol=symbol,side=side,contracts=contracts,leverage=leverage,external_id=external_id,
            stop_loss=stop_loss,take_profit=take_profit,entry_price=entry_price,position_mode=position_mode)
        if not (stop_loss<entry_price<take_profit if side=='long' else take_profit<entry_price<stop_loss):
            raise ValueError('invalid protection direction')
        return self._request('POST','/api/v1/private/order/create',operation='submit_market',expected=dict,
            body=dict(symbol=order.symbol,price=order.entry_price,vol=order.contracts,leverage=order.leverage,
                side=1 if side=='long' else 3,type=5,openType=1,externalOid=external_id,
                stopLossPrice=order.stop_loss,takeProfitPrice=order.take_profit,lossTrend=2,profitTrend=2,
                positionMode=order.position_mode,reduceOnly=False))

    def close_position(self,*,symbol,side,contracts,position_id,external_id,position_mode=1):
        if side not in ('long','short') or type(position_mode) is not int or position_mode not in (1,2):
            raise ValueError('invalid close direction/mode')
        body=dict(symbol=_symbol(symbol),price=0,vol=_positive(contracts),side=4 if side=='long' else 2,
            type=5,openType=1,positionId=_position_id(position_id),externalOid=_identifier(external_id),positionMode=position_mode)
        if position_mode==2:
            body['reduceOnly']=True
        return self._request('POST','/api/v1/private/order/create',operation='close_position',body=body,expected=dict)

    def place_protection(self,*,position_id,contracts,stop_loss,take_profit,request_id):
        _identifier(request_id) # Local journal correlation; this endpoint has no externalOid.
        return self._request('POST','/api/v1/private/stoporder/place',operation='place_protection',
            body=dict(positionId=_position_id(position_id),vol=_positive(contracts),stopLossPrice=_positive(stop_loss),
                takeProfitPrice=_positive(take_profit),lossTrend=2,profitTrend=2,volType=1,
                takeProfitReverse=2,stopLossReverse=2,takeProfitType=0,takeProfitOrderPrice=0,
                stopLossType=0,stopLossOrderPrice=0),expected=(str,int,dict,list))

    def modify_protection(self,*,protection_id,stop_loss,take_profit,request_id):
        _identifier(request_id)
        return self._request('POST','/api/v1/private/stoporder/change_plan_price',operation='modify_protection',
            body=dict(stopPlanOrderId=_position_id(protection_id),stopLossPrice=_positive(stop_loss),
                takeProfitPrice=_positive(take_profit),lossTrend=2,profitTrend=2,takeProfitReverse=2,stopLossReverse=2))

    def cancel_order(self,order_id):
        return self._request('POST','/api/v1/private/order/cancel',operation='cancel_order',
            body={'orderIds':[_position_id(order_id)]},expected=list)

    def change_leverage(self,*,symbol,side,leverage,request_id):
        _identifier(request_id)
        if side not in ('long','short') or type(leverage) is not int or leverage<1:
            raise ValueError('invalid leverage request')
        return self._request('POST','/api/v1/private/position/change_leverage',operation='change_leverage',
            body=dict(symbol=_symbol(symbol),positionType=1 if side=='long' else 2,openType=1,leverage=leverage))
