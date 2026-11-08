"""Explicitly approved LIVE execution research adapter; never used by PAPER.

No worker or import activates this adapter. Each exact proposal requires an
operator approval and fresh deterministic gates. Exchange acknowledgements are
not fills. Ambiguous writes are reconciled by the original external ID, never
blindly retried. Native protection must be read back before an exposure is
reported protected. This module does not guarantee exchange execution or PnL.
Entries are immediate-or-cancel limits at the worst price that keeps stop
risk inside the approved loss. A market fill is not used for entries.
"""
from dataclasses import dataclass
from datetime import datetime,timezone
import copy
import hashlib
import json
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from math import isfinite
from time import time
from uuid import uuid4

from trading.models.domain import TradeProposal
from trading.risk.engine import RiskContext
from trading.runtime.live_account import LiveAccountReconciler,LiveAccountError


class LiveExecutionError(RuntimeError):
    """Safe fixed error category; never a raw exchange response or key."""


def entry_stop_loss(side, entry, stop, quantity, fee_rate, slippage_rate):
    """Loss if `entry` fills and the stop then slips against the position.

    Includes two taker fees. Does not add entry slippage: pass the price that
    can actually trade. The pre-trade estimate passes a slipped reference.
    """
    if side not in ('long', 'short'):
        raise LiveExecutionError('invalid_entry_side')
    try:
        entry, stop, quantity, fee, slip = (float(value) for value in (entry, stop, quantity, fee_rate, slippage_rate))
    except (TypeError, ValueError):
        raise LiveExecutionError('invalid_numeric_exchange_field') from None
    if not all(isfinite(value) for value in (entry, stop, quantity, fee, slip)) or min(entry, stop, quantity) <= 0:
        raise LiveExecutionError('invalid_numeric_exchange_field')
    if not 0 <= fee < .1 or not 0 <= slip < 1:
        raise LiveExecutionError('invalid_execution_cost')
    worst_stop = stop * (1 - slip if side == 'long' else 1 + slip)
    if worst_stop <= 0:
        raise LiveExecutionError('invalid_numeric_exchange_field')
    return abs(entry - worst_stop) * quantity + fee * quantity * (entry + worst_stop)


def worst_entry_price(side, stop, quantity, max_loss, fee_rate, slippage_rate, price_step):
    """Worst fill that keeps stop risk inside the approved max loss.

    Solved from that loss and the stop, then rounded to the contract price
    step toward the safer side. A long limit is at or below this price. A
    short limit is at or above it. The rounded price is checked again.
    """
    if side not in ('long', 'short'):
        raise LiveExecutionError('invalid_entry_side')
    try:
        budget = Decimal(str(max_loss))
        qty = Decimal(str(quantity))
        fee = Decimal(str(fee_rate))
        slip = Decimal(str(slippage_rate))
        stop_price = Decimal(str(stop))
        step = Decimal(str(price_step))
    except (ArithmeticError, ValueError):
        raise LiveExecutionError('entry_price_cap_unavailable') from None
    if min(budget, qty, stop_price, step) <= 0 or fee < 0 or fee >= 1 or slip < 0 or slip >= 1:
        raise LiveExecutionError('entry_price_cap_unavailable')
    worst_stop = stop_price * (1 - slip if side == 'long' else 1 + slip)
    if worst_stop <= 0:
        raise LiveExecutionError('entry_price_cap_unavailable')
    if side == 'long':
        raw = (budget / qty + (1 - fee) * worst_stop) / (1 + fee)
        price = (raw / step).to_integral_value(rounding=ROUND_DOWN) * step
        if price <= stop_price:
            raise LiveExecutionError('entry_price_cap_unavailable')
    else:
        raw = ((1 + fee) * worst_stop - budget / qty) / (1 - fee)
        price = (raw / step).to_integral_value(rounding=ROUND_CEILING) * step
        if price <= 0 or price >= stop_price:
            raise LiveExecutionError('entry_price_cap_unavailable')
    capped = float(price)
    if entry_stop_loss(side, capped, stop, quantity, fee_rate, slippage_rate) > float(max_loss) + 1e-8:
        raise LiveExecutionError('entry_price_cap_unavailable')
    return capped


@dataclass(frozen=True)
class LiveExecutionGates:
    live_trading_enabled: bool = False
    broker_mode: str = 'paper'
    operator_ids: tuple[str,...] = ()
    reviewed: bool = False


class ApprovedLiveExecutor:
    def __init__(self,gates,store,private,public,risk_engine,*,account_id='manual',
                 max_quote_age_seconds=30,max_price_drift_pct=.005,fee_rate=.0006,
                 slippage_bps=5,liquidation_buffer_pct=.02,can_execute=None,binding_id=None,allowed_capital=None):
        self.gates,self.store,self.private,self.public,self.risk=gates,store,private,public,risk_engine
        self.can_execute=can_execute or (lambda:True)
        self.account_id=account_id
        self.binding_id=binding_id
        self.allowed_capital=allowed_capital
        self.reconciler=LiveAccountReconciler(account_id,store,binding_id=binding_id or 'unconfigured',max_age_seconds=max_quote_age_seconds)
        self.max_age,self.max_drift=max_quote_age_seconds,max_price_drift_pct
        self.fee,self.slip,self.liq_buffer=fee_rate,slippage_bps/10000,liquidation_buffer_pct

    @staticmethod
    def _number(value):
        if isinstance(value,bool):raise LiveExecutionError('invalid_numeric_exchange_field')
        try:number=float(value)
        except (TypeError,ValueError):raise LiveExecutionError('invalid_numeric_exchange_field') from None
        if not isfinite(number):raise LiveExecutionError('invalid_numeric_exchange_field')
        return number

    def _gate(self,*,entries=False):
        mode=getattr(self.gates.broker_mode,'value',self.gates.broker_mode)
        if not isinstance(self.binding_id,str) or not self.binding_id or len(self.binding_id)>128:
            raise LiveExecutionError('credential_binding_generation_required')
        if self.gates.live_trading_enabled is not True or mode!='live' or self.gates.reviewed is not True:
            raise LiveExecutionError('live_gates_disabled')
        if getattr(self.private,'writes_enabled',False) is not True:
            raise LiveExecutionError('private_writes_disabled')
        # Pause and the armed flag block new entries only. Reconciliation and
        # protection of an order that was already sent must keep running.
        if not entries:
            return
        if not self.can_execute():
            raise LiveExecutionError('operator_execution_paused')
        armed=getattr(self,'entries_armed',None)
        if armed is not None and armed() is not True:
            raise LiveExecutionError('live_not_armed')

    @staticmethod
    def _fingerprint(proposal):
        return hashlib.sha256(json.dumps(proposal.model_dump(mode='json'),sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

    def _key(self,intent_id):return 'live:intent:'+intent_id

    def state(self,intent_id):
        row=self.store.get(self._key(intent_id))
        if row is None:raise LiveExecutionError('unknown_intent')
        return copy.deepcopy(row)

    def _save(self,row):
        self.store.put(self._key(row['intent_id']),row)
        return copy.deepcopy(row)

    def _halt(self,row,reason,*,account=True):
        # account=False keeps the intent unresolved without latching every future entry.
        # Unknown writes still latch: a second open is worse than a stopped account.
        # A fill that exceeded the approved loss stays latched for review. Nothing here clears it.
        if account:
            current=self.store.get('live:halt') or {}
            if current.get('requires_review') is not True:
                self.store.put('live:halt',dict(intent_id=row['intent_id'],reason=reason,at=time()))
        if row.get('halt_reason')!='filled_position_risk_exceeds_plan':
            row.update(halt_reason=reason)
        return self._save(row)

    def _optional_number(self,value):
        try:
            return self._number(value)
        except LiveExecutionError:
            return None

    def _book_exchange_costs(self,row,*sources):
        """Persist fee and funding only when the exchange report includes them."""
        costs=dict(row.get('exchange_costs') or {})
        for source in sources:
            if not isinstance(source,dict):
                continue
            if 'fee' in source and source.get('fee') is not None:
                fee=self._optional_number(source.get('fee'))
                if fee is not None:
                    costs['fee']=fee
            elif 'takerFee' in source or 'makerFee' in source:
                taker=self._optional_number(source.get('takerFee')) if 'takerFee' in source else 0
                maker=self._optional_number(source.get('makerFee')) if 'makerFee' in source else 0
                if taker is not None and maker is not None:
                    costs['fee']=taker+maker
            if 'holdFee' in source and source.get('holdFee') is not None:
                funding=self._optional_number(source.get('holdFee'))
                if funding is not None:
                    costs['funding']=funding
        if not costs:
            return costs
        row['exchange_costs']=costs
        self.store.put('live:costs:'+row['intent_id'],dict(intent_id=row['intent_id'],
            symbol=row['proposal']['symbol'],source='exchange',fee=costs.get('fee'),funding=costs.get('funding')))
        return costs

    def stage(self,proposal,*,ttl_seconds=60):
        """Create a reviewable immutable intent without any exchange request."""
        if not isinstance(proposal,TradeProposal):raise LiveExecutionError('proposal_schema_required')
        if not 5<=ttl_seconds<=300:raise LiveExecutionError('invalid_approval_ttl')
        fingerprint=self._fingerprint(proposal)
        intent_id=hashlib.sha256((self.account_id+':'+str(self.binding_id)+':'+proposal.proposal_id).encode()).hexdigest()[:32]
        with self.store.lock:
            old=self.store.get(self._key(intent_id))
            if old:
                if old['proposal_fingerprint']!=fingerprint:raise LiveExecutionError('intent_id_reused_with_changed_proposal')
                return copy.deepcopy(old)
            return self._save(dict(intent_id=intent_id,proposal_fingerprint=fingerprint,proposal=proposal.model_dump(mode='json'),
                external_id='creo-'+intent_id[:24],binding_id=self.binding_id,status='awaiting_manual_approval',is_simulated=False,
                staged_at=time(),expires_at=time()+ttl_seconds,entry_write_attempted=False,
                protection_write_attempted=False,emergency_close_attempted=False))

    def approve(self,intent_id,proposal_fingerprint,operator_id,*,allow_emergency_close=False):
        """Only a caller-authenticated, allowlisted human operator may invoke this."""
        self._gate(entries=True)
        if str(operator_id) not in {str(v) for v in self.gates.operator_ids}:
            raise LiveExecutionError('operator_not_allowed')
        with self.store.lock:
            row=self.state(intent_id)
            if row['proposal_fingerprint']!=proposal_fingerprint:raise LiveExecutionError('approval_proposal_mismatch')
            if row['expires_at']<=time():raise LiveExecutionError('approval_expired')
            if row['status']!='awaiting_manual_approval':raise LiveExecutionError('intent_not_awaiting_approval')
            row.update(status='approved',approved_by=str(operator_id),approved_at=time(),
                allow_emergency_close=bool(allow_emergency_close))
            return self._save(row)

    def _preflight(self,row):
        proposal=TradeProposal.model_validate(row['proposal'])
        if self._fingerprint(proposal)!=row['proposal_fingerprint']:raise LiveExecutionError('stored_proposal_changed')
        try:
            snapshot=self.reconciler.refresh(self.private,symbols=[proposal.symbol])
        except LiveAccountError:
            raise LiveExecutionError('private_account_reconciliation_failed') from None
        equity=snapshot['balance']['equity']
        available=snapshot['balance']['available']
        if equity<=0 or available<=0:raise LiveExecutionError('available_balance_required')
        mode=snapshot['position_mode']
        if proposal.leverage!=int(proposal.leverage):raise LiveExecutionError('integer_leverage_required')
        rules=self.public.get_contract_rules(proposal.symbol)
        if not rules.base_tier_max:raise LiveExecutionError('base_risk_tier_required')
        ticker=self.public.get_ticker(proposal.symbol)
        age=time()-self._number(ticker.raw.get('timestamp'))/1000
        bid,ask=self._number(ticker.bid1),self._number(ticker.ask1)
        if not -5<=age<=self.max_age or bid<=0 or ask<bid:raise LiveExecutionError('fresh_executable_quote_required')
        entry=ask if proposal.side.value=='long' else bid
        if not proposal.entry_price or abs(entry/proposal.entry_price-1)>self.max_drift:raise LiveExecutionError('approved_entry_price_drift')
        sl,tp=proposal.stop_loss.price,proposal.take_profit_price
        if sl is None or tp is None or not (sl<entry<tp if proposal.side.value=='long' else tp<entry<sl):
            raise LiveExecutionError('native_stop_and_target_required')
        contracts,notional=rules.size_order(proposal.notional,entry,proposal.leverage)
        readiness=self.reconciler.preflight(symbol=proposal.symbol,side=1 if proposal.side.value=='long' else 2,
            contracts=contracts,leverage=proposal.leverage,notional=notional,allowed_capital=self.allowed_capital,
            expected_generation=snapshot['generation'],position_mode=mode)
        if not readiness.ready:raise LiveExecutionError('private_preflight_rejected')
        available=readiness.available
        if abs(notional-proposal.notional)>max(1e-8,entry*rules.size*rules.step):raise LiveExecutionError('approved_notional_changed')
        worst_entry=entry*(1+self.slip if proposal.side.value=='long' else 1-self.slip)
        worst_stop=sl*(1-self.slip if proposal.side.value=='long' else 1+self.slip)
        quantity=contracts*rules.size
        planned_loss=abs(worst_entry-worst_stop)*quantity+self.fee*quantity*(worst_entry+worst_stop)
        if planned_loss>proposal.max_loss_if_stopped+1e-8:raise LiveExecutionError('fees_slippage_exceed_approved_risk')
        if notional/proposal.leverage+notional*self.fee>available:raise LiveExecutionError('insufficient_available_margin')
        side=1 if proposal.side.value=='long' else 2
        private_maintenance=max(row['maintenance_rate'] for group in ('risk_limits','leverage')
            for row in snapshot[group][proposal.symbol] if row['side']==side)
        distance=1/proposal.leverage-max(rules.maintenance,private_maintenance)-self.fee
        liquidation=entry*(1-distance if proposal.side.value=='long' else 1+distance)
        buffer=(sl-liquidation)/entry if proposal.side.value=='long' else (liquidation-sl)/entry
        if distance<=0 or buffer<self.liq_buffer:raise LiveExecutionError('insufficient_liquidation_buffer')
        day=datetime.now(timezone.utc).date().isoformat()
        state=self.store.get('live:risk_reference:'+self.binding_id,dict(initial=equity,peak=equity,day=day,day_start=equity))
        if state['day']!=day:state.update(day=day,day_start=equity)
        state['peak']=max(state['peak'],equity)
        self.store.put('live:risk_reference:'+self.binding_id,state)
        context=RiskContext(equity=equity,starting_capital=state['initial'],capital_floor=0,
            daily_loss_pct=max(0,1-equity/state['day_start']),drawdown_pct=max(0,1-equity/state['peak']))
        decision=self.risk.evaluate(proposal,context)
        if not decision.accepted:raise LiveExecutionError('deterministic_risk_rejected')
        quantity=contracts*rules.size
        cap=worst_entry_price(proposal.side.value,sl,quantity,proposal.max_loss_if_stopped,self.fee,self.slip,rules.price_step)
        if not (sl<cap<tp if proposal.side.value=='long' else tp<cap<sl):
            raise LiveExecutionError('entry_price_cap_unavailable')
        return dict(proposal=proposal,contracts=contracts,mode=mode,worst_allowed_price=cap,
            planned_loss=planned_loss,equity=equity,quantity=quantity)

    def _acquire_lease(self,intent_id):
        with self.store.lock,self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            old=self.store.get('live:executor_lease',{})
            if old.get('until',0)>time():raise LiveExecutionError('live_executor_busy')
            token=uuid4().hex
            value=dict(token=token,intent_id=intent_id,until=time()+600)
            self.store.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',('live:executor_lease',json.dumps(value)))
            return token

    def _check_lease(self,token):
        lease=self.store.get('live:executor_lease',{})
        if lease.get('token')!=token or lease.get('until',0)<=time():raise LiveExecutionError('live_executor_lease_lost')

    def _release_lease(self,token):
        with self.store.lock:
            if self.store.get('live:executor_lease',{}).get('token')==token:
                self.store.put('live:executor_lease',{})

    def execute(self,intent_id):
        self._gate(entries=False)
        token=self._acquire_lease(intent_id)
        try:
            row=self.state(intent_id)
            if row.get('binding_id')!=self.binding_id:raise LiveExecutionError('credential_binding_changed')
            if row.get('entry_write_attempted'):
                return self._reconcile(row,token)
            self._gate(entries=True)
            if self.store.get('live:halt'):raise LiveExecutionError('live_halt_latched')
            if row['status']!='approved' or row['expires_at']<=time():raise LiveExecutionError('fresh_exact_manual_approval_required')
            with self.store.lock:
                pending=[json.loads(payload) for _,payload in self.store.db.execute("SELECT key,value FROM state WHERE key LIKE 'live:intent:%'")]
            if any(value['intent_id']!=intent_id and value.get('entry_write_attempted') and value.get('status') not in ('closed','entry_rejected') for value in pending):
                raise LiveExecutionError('unresolved_live_intent_blocks_entry')
            plan=self._preflight(row)
            proposal=plan['proposal']
            self._gate()
            self._check_lease(token)
            if row['expires_at']<=time():raise LiveExecutionError('approval_expired_during_preflight')
            row.update(status='entry_write_unknown',entry_write_attempted=True,contracts=plan['contracts'],
                position_mode=plan['mode'],worst_allowed_price=plan['worst_allowed_price'],
                planned_loss=plan['planned_loss'],equity=plan['equity'],quantity=plan['quantity'])
            self._save(row) # Durable before POST; crash requires read-only reconciliation.
            try:
                # IOC at the loss cap. Type 5 market orders can fill through the plan.
                self.private.submit_ioc(symbol=proposal.symbol,side=proposal.side.value,contracts=plan['contracts'],
                    leverage=int(proposal.leverage),external_id=row['external_id'],stop_loss=proposal.stop_loss.price,
                    take_profit=proposal.take_profit_price,price=plan['worst_allowed_price'],position_mode=plan['mode'])
            except Exception as exc:
                if getattr(exc,'ambiguous',True) is False:
                    row.update(status='entry_rejected')
                    return self._save(row)
                self._halt(row,'entry_write_outcome_unknown')
                return self.state(intent_id)
            return self._reconcile(row,token)
        finally:
            self._release_lease(token)

    def _position(self,row,order):
        position_id=order.get('positionId')
        if position_id is None:return None
        proposal=row['proposal']
        expected=1 if proposal['side']=='long' else 2
        matches=[p for p in self.private.open_positions(proposal['symbol'])
            if str(p.get('positionId'))==str(position_id) and p.get('symbol')==proposal['symbol']
            and p.get('positionType')==expected and p.get('state')==1 and p.get('openType')==1
            and p.get('autoAddIm') in (False,0) and self._number(p.get('holdVol'))==row['contracts']]
        return matches[0] if len(matches)==1 else None

    def _protected(self,row,position,volume=None):
        """True=matching native pair, False=complete list no pair, None=unknown."""
        expected=row['contracts'] if volume is None else volume
        protections=[]
        try:
            complete=False
            for page in range(1,11):
                rows=self.private.protection_orders(row['proposal']['symbol'],page_num=page,page_size=100)
                if not isinstance(rows,list):return None
                protections.extend(rows)
                if len(rows)<100:
                    complete=True
                    break
            if not complete:return None
            for protection in protections:
                try:
                    volumes=protection.get('profitLossVolType')
                    volume_ok=(self._number(protection.get('vol'))==expected if volumes=='SAME' else
                        volumes=='SEPARATE' and self._number(protection.get('stopLossVol'))==expected
                        and self._number(protection.get('takeProfitVol'))==expected)
                    if (str(protection.get('positionId'))==str(position['positionId']) and protection.get('state')==1
                            and protection.get('isFinished')==0 and protection.get('lossTrend')==2 and protection.get('profitTrend')==2
                            and protection.get('stopLossType')==0 and protection.get('takeProfitType')==0
                            and protection.get('stopLossReverse')==2 and protection.get('takeProfitReverse')==2
                            and volume_ok
                            and self._number(protection.get('stopLossPrice'))==row['proposal']['stop_loss']['price']
                            and self._number(protection.get('takeProfitPrice'))==row['proposal']['take_profit_price']):
                        return True
                except (AttributeError,LiveExecutionError):
                    continue
        except Exception:
            return None
        return False

    def reconcile(self,intent_id):
        """Read/reconcile the original entry. Never resubmit an uncertain entry."""
        self._gate(entries=False)
        token=self._acquire_lease(intent_id)
        try:
            row=self.state(intent_id)
            if row.get('binding_id')!=self.binding_id:raise LiveExecutionError('credential_binding_changed')
            return self._reconcile(row,token)
        finally:
            self._release_lease(token)

    def _mark_closed(self,row,report,*,reason):
        self._book_exchange_costs(row,report)
        # A fill that broke the plan stays marked after the emergency close. The account halt is separate.
        kept=row.get('halt_reason') if row.get('halt_reason')=='filled_position_risk_exceeds_plan' else None
        row.update(status='closed',close_reason=reason,halt_reason=kept)
        return self._save(row)

    def _confirmed_close(self,row):
        """Exchange-confirmed flat position. None when the close is not proven."""
        position_id=row.get('position_id')
        if not position_id:
            return None
        symbol=row['proposal']['symbol']
        try:
            open_rows=self.private.open_positions(symbol)
            if any(str(item.get('positionId'))==str(position_id) and self._optional_number(item.get('holdVol')) not in (None,0)
                    and self._optional_number(item.get('holdVol'))>0 for item in open_rows):
                return None
            method=getattr(self.private,'history_positions',None)
            if method is None:
                return None
            history=method(symbol)
            if not isinstance(history,list):
                return None
            matches=[item for item in history if isinstance(item,dict) and str(item.get('positionId'))==str(position_id)
                and item.get('symbol')==symbol and item.get('state')==3 and (self._optional_number(item.get('closeVol')) or 0)>0]
            return matches[0] if len(matches)==1 else None
        except Exception:
            return None

    def _fill_price(self,order):
        if not isinstance(order,dict) or 'dealAvgPrice' not in order:
            return None
        price=self._optional_number(order.get('dealAvgPrice'))
        if price is None or price<=0:
            return None
        return price

    def _filled_quantity(self,row,filled):
        if row.get('quantity') is None or not row.get('contracts'):
            return None
        try:
            return float(Decimal(str(filled))/Decimal(str(row['contracts']))*Decimal(str(row['quantity'])))
        except (ArithmeticError, ValueError):
            return None

    def _accepts_fill(self,row,fill,quantity):
        """False when the fill is missing, worse than the cap, or over the approved loss."""
        proposal=row.get('proposal') or {}
        worst=row.get('worst_allowed_price')
        stop=(proposal.get('stop_loss') or {}).get('price')
        side=proposal.get('side')
        cap=proposal.get('max_loss_if_stopped')
        if fill is None or worst is None or quantity is None or stop is None or side not in ('long','short') or cap is None:
            return False,None
        try:
            computed=entry_stop_loss(side,fill,stop,quantity,self.fee,self.slip)
        except LiveExecutionError:
            return False,None
        fill_price,worst_price=Decimal(str(fill)),Decimal(str(worst))
        worse=fill_price>worst_price if side=='long' else fill_price<worst_price
        return (not worse) and computed<=float(cap)+1e-8,computed

    def _cancel_remainder(self,row,order,token):
        """One cancel of a still-open entry. Does not submit a second order."""
        if order.get('state') not in (1,2) or row.get('remainder_cancel_attempted'):
            return order
        order_id=order.get('orderId')
        row['remainder_cancel_attempted']=True
        self._save(row)
        if order_id is None:
            return order
        try:
            self._check_lease(token)
            self.private.cancel_order(order_id)
        except Exception:
            pass
        try:
            fresh=self.private.order_by_external_id(row['proposal']['symbol'],row['external_id'])
        except Exception:
            return order
        return fresh if isinstance(fresh,dict) else order

    def _matching_position(self,row,filled):
        positions=self.private.open_positions(row['proposal']['symbol'])
        expected=1 if row['proposal']['side']=='long' else 2
        matches=[item for item in positions if item.get('symbol')==row['proposal']['symbol']
            and item.get('positionType')==expected and item.get('state')==1 and item.get('openType')==1
            and self._optional_number(item.get('holdVol'))==filled]
        return matches[0] if len(matches)==1 else None

    def _reject_fill(self,row,position,token,fill,computed):
        """Persist the prices that failed the plan and latch review. Do not clear that latch."""
        numbers=dict(fill_price=fill,worst_allowed_price=row.get('worst_allowed_price'),
            planned_loss=row.get('planned_loss'),computed_loss=computed,equity=row.get('equity'))
        row.update(numbers,halt_reason='filled_position_risk_exceeds_plan',status='filled_risk_rejected')
        self._save(row)
        self.store.put('live:halt',dict(intent_id=row['intent_id'],reason='emergency_close_requires_review',
            requires_review=True,at=time(),**numbers))
        hold=None if position is None else self._optional_number(position.get('holdVol'))
        if hold is None or hold<=0 or not row.get('allow_emergency_close') or row.get('emergency_close_attempted'):
            return self.state(row['intent_id'])
        row.update(position_id=str(position['positionId']),close_contracts=hold,
            emergency_close_attempted=True,status='emergency_close_unknown')
        self._save(row)
        try:
            self._check_lease(token)
            self.private.close_position(symbol=row['proposal']['symbol'],side=row['proposal']['side'],
                contracts=hold,position_id=row['position_id'],external_id='close-'+row['intent_id'][:24],
                position_mode=row['position_mode'])
        except Exception:
            return self.state(row['intent_id'])
        return self.state(row['intent_id'])

    def _partial(self,row,order,filled,token):
        """Keep the original external id. Do not latch the account or send a second entry.

        An unfilled order is not a position. A still-open remainder is cancelled once.
        A partial that cannot be protected is closed at the filled size only.
        """
        row.update(status='partial_fill',filled_contracts=filled,halt_reason='entry_fill_not_fully_confirmed')
        self._save(row)
        try:
            if order.get('state') in (1,2):
                # Cancel already ran once. An unfilled working order is not a position.
                # A fill beside a still-open remainder is closed at the filled size only.
                row.update(halt_reason='entry_remainder_open',filled_contracts=filled)
                self._save(row)
                if filled<=0:
                    return self.state(row['intent_id'])
                position=self._matching_position(row,filled)
                if position is None:
                    return self.state(row['intent_id'])
                return self._emergency_close(row,position,token,filled,reason='entry_remainder_open')
            if filled<=0 and order.get('state') in (3,4,5):
                row.update(status='entry_rejected',halt_reason=None,filled_contracts=0)
                return self._save(row)
            if filled<=0 or filled>row['contracts']:
                return self.state(row['intent_id'])
            position=self._matching_position(row,filled)
            fill=self._fill_price(order)
            ok,computed=self._accepts_fill(row,fill,self._filled_quantity(row,filled))
            if not ok:
                return self._reject_fill(row,position,token,fill,computed)
            if position is None:
                return self.state(row['intent_id'])
            row.update(position_id=str(position['positionId']),fill_price=fill,computed_loss=computed)
            self._book_exchange_costs(row,order,position)
            self._save(row)
            if self._protected(row,position,filled) is True:
                row.update(status='partial_protected')
                return self._save(row)
            if not row.get('partial_protection_attempted'):
                row.update(partial_protection_attempted=True)
                self._save(row)
                self._check_lease(token)
                self.private.place_protection(position_id=row['position_id'],contracts=filled,
                    stop_loss=row['proposal']['stop_loss']['price'],take_profit=row['proposal']['take_profit_price'],
                    request_id='psl-'+row['intent_id'][:24])
                if self._protected(row,position,filled) is True:
                    row.update(status='partial_protected')
                    return self._save(row)
            return self._emergency_close(row,position,token,filled)
        except Exception:
            return self.state(row['intent_id'])

    def _reconcile(self,row,token):
        self._check_lease(token)
        if not row.get('entry_write_attempted'):raise LiveExecutionError('entry_not_submitted')
        if row['status'] in ('closed','entry_rejected'):return row
        try:
            if row.get('emergency_close_attempted'):
                close=self.private.order_by_external_id(row['proposal']['symbol'],'close-'+row['intent_id'][:24])
                expected_close_side=4 if row['proposal']['side']=='long' else 2
                remaining=self.private.open_positions(row['proposal']['symbol'])
                if (isinstance(close,dict) and close.get('externalOid')=='close-'+row['intent_id'][:24]
                        and close.get('symbol')==row['proposal']['symbol'] and close.get('side')==expected_close_side
                        and close.get('state')==3 and self._number(close.get('dealVol'))==row.get('close_contracts',row['contracts'])
                        and not any(str(p.get('positionId'))==row['position_id'] and self._number(p.get('holdVol'))>0 for p in remaining)):
                    return self._mark_closed(row,close,reason='emergency_close')
                return self._halt(row,'emergency_close_not_confirmed')
            if row.get('position_id'):
                confirmed=self._confirmed_close(row)
                if confirmed is not None:
                    return self._mark_closed(row,confirmed,reason='exchange_confirmed')
            order=self.private.order_by_external_id(row['proposal']['symbol'],row['external_id'])
            if not isinstance(order,dict) or str(order.get('externalOid'))!=row['external_id']:
                return self._halt(row,'entry_order_not_reconciled')
            expected_side=1 if row['proposal']['side']=='long' else 3
            if (order.get('symbol')!=row['proposal']['symbol'] or order.get('side')!=expected_side
                    or order.get('openType')!=1 or order.get('positionMode')!=row['position_mode']
                    or self._number(order.get('vol'))!=row['contracts']):
                return self._halt(row,'entry_order_identity_mismatch')
            filled=self._number(order.get('dealVol'))
            if filled>row['contracts']:
                return self._halt(row,'entry_order_identity_mismatch')
            if order.get('state') in (1,2):
                order=self._cancel_remainder(row,order,token)
                if not isinstance(order,dict) or str(order.get('externalOid'))!=row['external_id']:
                    return self._halt(row,'entry_order_not_reconciled')
                if (order.get('symbol')!=row['proposal']['symbol'] or order.get('side')!=expected_side
                        or order.get('openType')!=1 or order.get('positionMode')!=row['position_mode']
                        or self._number(order.get('vol'))!=row['contracts']):
                    return self._halt(row,'entry_order_identity_mismatch')
                filled=self._number(order.get('dealVol'))
                if filled>row['contracts']:
                    return self._halt(row,'entry_order_identity_mismatch')
            if filled!=row['contracts'] or order.get('state')!=3:
                return self._partial(row,order,filled,token)
            position=self._position(row,order)
            fill=self._fill_price(order)
            accepted,computed=self._accepts_fill(row,fill,row.get('quantity'))
            if not accepted:
                return self._reject_fill(row,position,token,fill,computed)
            row.update(fill_price=fill,computed_loss=computed)
            self._save(row)
            if position is None:
                confirmed=self._confirmed_close(row)
                if confirmed is not None:
                    return self._mark_closed(row,confirmed,reason='exchange_confirmed')
                # A missing position is not proof of a close. Keep the intent open so a
                # later history read can mark the TP/SL closed without latching the account.
                return self._halt(row,'entry_position_not_identified',account=False)
            self._book_exchange_costs(row,order,position)
            row.update(position_id=str(position['positionId']),status='entry_filled_unprotected')
            self._save(row)
            protection_state=self._protected(row,position)
            if protection_state is True:
                row.update(status='protected')
                return self._save(row)
            if protection_state is None:
                return self._emergency_close(row,position,token)
            if not row.get('protection_write_attempted') and not row.get('emergency_close_attempted'):
                row.update(protection_write_attempted=True,status='protection_write_unknown')
                self._save(row)
                try:
                    self._check_lease(token)
                    self.private.place_protection(position_id=row['position_id'],contracts=row['contracts'],
                        stop_loss=row['proposal']['stop_loss']['price'],take_profit=row['proposal']['take_profit_price'],
                        request_id='sl-'+row['intent_id'][:24])
                except Exception:
                    pass # One write only; confirm by read before deciding exposure status.
                if self._protected(row,position):
                    row.update(status='protected')
                    return self._save(row)
            return self._emergency_close(row,position,token)
        except Exception:
            if row.get('halt_reason')!='filled_position_risk_exceeds_plan':
                row.update(halt_reason='reconciliation_incomplete')
            return self._save(row)

    def _emergency_close(self,row,position,token,contracts=None,reason='native_protection_not_confirmed'):
        contracts=row['contracts'] if contracts is None else contracts
        self._halt(row,reason)
        if not row.get('allow_emergency_close') or row.get('emergency_close_attempted'):
            return self.state(row['intent_id'])
        row.update(emergency_close_attempted=True,status='emergency_close_unknown',close_contracts=contracts,
            position_id=str(position['positionId']))
        self._save(row)
        try:
            self._check_lease(token)
            self.private.close_position(symbol=row['proposal']['symbol'],side=row['proposal']['side'],
                contracts=contracts,position_id=row['position_id'],external_id='close-'+row['intent_id'][:24],
                position_mode=row['position_mode'])
        except Exception:
            return self.state(row['intent_id'])
        # An acknowledgement is never treated as a confirmed close.
        return self.state(row['intent_id'])
