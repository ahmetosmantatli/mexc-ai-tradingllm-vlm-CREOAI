"""Explicitly approved LIVE execution research adapter; never used by PAPER.

No worker or import activates this adapter. Each exact proposal requires an
operator approval and fresh deterministic gates. Exchange acknowledgements are
not fills. Ambiguous writes are reconciled by the original external ID, never
blindly retried. Native protection must be read back before an exposure is
reported protected. This module does not guarantee exchange execution or PnL.
"""
from dataclasses import dataclass
import copy
import hashlib
import json
from math import isfinite
from time import time
from uuid import uuid4

from trading.brokers.live.management import PositionManagementMixin
from trading.models.domain import TradeProposal
from trading.risk.engine import RiskContext
from trading.runtime.live_account import LiveAccountReconciler,LiveAccountError,observe_risk_reference


class LiveExecutionError(RuntimeError):
    """Safe fixed error category; never a raw exchange response or key."""
    def __init__(self,code,*,reason_codes=()):
        super().__init__(code)
        self.reason_codes=tuple(reason_codes)


@dataclass(frozen=True)
class LiveExecutionGates:
    live_trading_enabled: bool = False
    broker_mode: str = 'paper'
    operator_ids: tuple[str,...] = ()
    reviewed: bool = False


class ApprovedLiveExecutor(PositionManagementMixin):
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

    def _gate(self,*,entry=True,write=True):
        mode=getattr(self.gates.broker_mode,'value',self.gates.broker_mode)
        if not isinstance(self.binding_id,str) or not self.binding_id or len(self.binding_id)>128:
            raise LiveExecutionError('credential_binding_generation_required')
        if mode!='live' or self.gates.reviewed is not True:
            raise LiveExecutionError('live_gates_disabled')
        if entry and (self.gates.live_trading_enabled is not True or not self.can_execute()):
            raise LiveExecutionError('operator_execution_paused')
        if write and getattr(self.private,'writes_enabled',False) is not True:
            raise LiveExecutionError('private_writes_disabled')

    def _authorized(self,row):
        return row.get('status')=='approved'

    def _managed(self):
        return []

    def intents(self):
        with self.store.lock:
            rows=[json.loads(payload) for _,payload in self.store.db.execute("SELECT key,value FROM state WHERE key LIKE 'live:intent:%'")]
        return [row for row in rows if row.get('binding_id')==self.binding_id]

    def reconcile_all(self):
        results=[]
        for row in self.intents():
            if row.get('entry_write_attempted') and (row.get('status') not in ('closed','entry_rejected') or row.get('status')=='closed' and not row.get('realized_complete')):
                results.append(self.reconcile(row['intent_id']))
        return results

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

    def _resolved(self,row):
        halt=self.store.get('live:halt',{})
        if halt.get('intent_id')==row['intent_id']:
            self.store.put('live:halt',{})
        return self._save(row)

    def _halt(self,row,reason):
        self.store.put('live:halt',dict(intent_id=row['intent_id'],reason=reason,at=time()))
        row.update(halt_reason=reason)
        return self._save(row)

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
        self._gate()
        if str(operator_id) not in {str(v) for v in self.gates.operator_ids}:
            raise LiveExecutionError('operator_not_allowed')
        with self.store.lock:
            row=self.state(intent_id)
            if row['proposal_fingerprint']!=proposal_fingerprint:raise LiveExecutionError('approval_proposal_mismatch')
            if row['expires_at']<=time():raise LiveExecutionError('approval_expired')
            if row['status']!='awaiting_manual_approval':raise LiveExecutionError('intent_not_awaiting_approval')
            if not allow_emergency_close:raise LiveExecutionError('explicit_emergency_close_permission_required')
            row.update(status='approved',approved_by=str(operator_id),approved_at=time(),allow_emergency_close=True)
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
        managed=self._managed()
        managed_positions=[r['position_id'] for r in managed if r.get('position_id')]
        managed_orders=[r['exchange_order_id'] for r in managed if r.get('exchange_order_id')]
        readiness=self.reconciler.preflight(symbol=proposal.symbol,side=1 if proposal.side.value=='long' else 2,
            contracts=contracts,leverage=proposal.leverage,notional=notional,allowed_capital=self.allowed_capital() if callable(self.allowed_capital) else self.allowed_capital,
            expected_generation=snapshot['generation'],position_mode=mode,managed_position_ids=managed_positions,managed_order_ids=managed_orders)
        if not readiness.ready:raise LiveExecutionError('private_preflight_rejected',reason_codes=readiness.reasons)
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
        state=observe_risk_reference(self.store,self.binding_id,equity)
        verified_leverage_cap=min(rules.max_leverage,*(r['max_leverage'] for group in ('risk_limits','leverage')
            for r in snapshot[group][proposal.symbol] if r['side']==side))
        context=RiskContext(equity=equity,starting_capital=state['initial'],capital_floor=0,
            exchange_max_leverage=verified_leverage_cap,
            open_positions=len(snapshot['positions']),open_risk=sum(r.get('planned_loss',r['proposal']['max_loss_if_stopped']) for r in managed),
            daily_loss_pct=state['daily_loss_pct'],weekly_loss_pct=state['weekly_loss_pct'],drawdown_pct=state['drawdown_pct'])
        decision=self.risk.evaluate(proposal,context)
        if not decision.accepted:raise LiveExecutionError('deterministic_risk_rejected',reason_codes=getattr(decision,'reason_codes',()))
        row.update(planned_loss=planned_loss,equity_before=equity,entry_notional=notional,contract_size=rules.size,margin=readiness.planned_margin)
        return proposal,contracts,mode

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
        self._gate()
        token=self._acquire_lease(intent_id)
        try:
            row=self.state(intent_id)
            if row.get('binding_id')!=self.binding_id:raise LiveExecutionError('credential_binding_changed')
            if row.get('entry_write_attempted'):
                return self._reconcile(row,token)
            if self.store.get('live:halt'):raise LiveExecutionError('live_halt_latched')
            if not self._authorized(row) or row['expires_at']<=time():raise LiveExecutionError('fresh_exact_manual_approval_required')
            with self.store.lock:
                pending=[json.loads(payload) for _,payload in self.store.db.execute("SELECT key,value FROM state WHERE key LIKE 'live:intent:%'")]
            if any(value['intent_id']!=intent_id and value.get('entry_write_attempted') and value.get('status') not in ('closed','entry_rejected') and value['intent_id'] not in {r['intent_id'] for r in self._managed()} for value in pending):
                raise LiveExecutionError('unresolved_live_intent_blocks_entry')
            proposal,contracts,mode=self._preflight(row)
            self._gate()
            if not self._authorized(row):raise LiveExecutionError('entry_authorization_changed')
            self._check_lease(token)
            if row['expires_at']<=time():raise LiveExecutionError('approval_expired_during_preflight')
            row.update(status='entry_write_unknown',entry_write_attempted=True,contracts=contracts,position_mode=mode)
            self._save(row) # Durable before POST; crash requires read-only reconciliation.
            try:
                self.private.submit_market(symbol=proposal.symbol,side=proposal.side.value,contracts=contracts,
                    leverage=int(proposal.leverage),external_id=row['external_id'],stop_loss=proposal.stop_loss.price,
                    take_profit=proposal.take_profit_price,position_mode=mode)
            except Exception as exc:
                if getattr(exc,'ambiguous',True) is False:
                    from trading.runtime.execution_diagnostics import safe_execution_error
                    row.update(status='entry_rejected',entry_rejection={
                        **safe_execution_error(exc),'stage':'submit_market'})
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
            and p.get('autoAddIm') in (False,0) and self._number(p.get('holdVol'))==self.remaining_contracts(row)]
        return matches[0] if len(matches)==1 else None

    def _protected(self,row,position):
        """True=matching native pair, False=complete list no pair, None=unknown."""
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
            related=[item for item in protections if isinstance(item,dict)
                and str(item.get('positionId'))==str(position['positionId'])]
            if len(related)>1:return None # Extra native orders can conflict or reverse exposure.
            for protection in related:
                try:
                    if str(protection.get('positionId'))!=str(position['positionId']):
                        continue
                    volumes=protection.get('profitLossVolType')
                    if volumes not in ('SAME','SEPARATE'):
                        return None
                    volume_ok=(self._number(protection.get('vol'))==self.remaining_contracts(row) if volumes=='SAME' else
                        volumes=='SEPARATE' and self._number(protection.get('stopLossVol'))==self.remaining_contracts(row)
                        and self._number(protection.get('takeProfitVol'))==self.remaining_contracts(row))
                    if (str(protection.get('positionId'))==str(position['positionId']) and protection.get('state')==1
                            and protection.get('isFinished')==0 and protection.get('lossTrend')==2 and protection.get('profitTrend')==2
                            and protection.get('stopLossType')==0 and protection.get('takeProfitType')==0
                            and protection.get('stopLossReverse')==2 and protection.get('takeProfitReverse')==2
                            and volume_ok
                            and self._number(protection.get('stopLossPrice'))==self.protection_levels(row)[0]
                            and self._number(protection.get('takeProfitPrice'))==self.protection_levels(row)[1]):
                        return True
                    return None # Existing same-position protection is not safe to duplicate.
                except (AttributeError,LiveExecutionError):
                    return None
        except Exception:
            return None
        return False

    def reconcile(self,intent_id):
        """Read/reconcile the original entry. Never resubmit an uncertain entry."""
        self._gate(entry=False,write=False)
        token=self._acquire_lease(intent_id)
        try:
            row=self.state(intent_id)
            if row.get('binding_id')!=self.binding_id:raise LiveExecutionError('credential_binding_changed')
            return self._reconcile(row,token)
        finally:
            self._release_lease(token)

    def _reconcile(self,row,token):
        self._check_lease(token)
        if not row.get('entry_write_attempted'):raise LiveExecutionError('entry_not_submitted')
        if row['status']=='entry_rejected':return row
        if row['status']=='closed':
            if not row.get('realized_complete'):
                try:return self._historical_close(row,dict(positionId=row['position_id'])) or row
                except Exception:return row
            return row
        try:
            if row.get('management_pending'):
                return self._reconcile_management(row,token)
            if row.get('close_write_attempted') or row.get('emergency_close_attempted'):
                close=self.private.order_by_external_id(row['proposal']['symbol'],'close-'+row['intent_id'][:24])
                expected_close_side=4 if row['proposal']['side']=='long' else 2
                remaining=self.private.open_positions(row['proposal']['symbol'])
                if (isinstance(close,dict) and close.get('externalOid')=='close-'+row['intent_id'][:24]
                        and close.get('symbol')==row['proposal']['symbol'] and close.get('side')==expected_close_side
                        and close.get('state')==3 and self._number(close.get('dealVol'))==row.get('close_contracts',self.remaining_contracts(row))
                        and not any(str(p.get('positionId'))==row['position_id'] and self._number(p.get('holdVol'))>0 for p in remaining)):
                    closed=self._historical_close(row,dict(positionId=row['position_id']))
                    if closed:return self._resolved(closed)
                    row.update(status='closed',realized_complete=False,net_pnl=None)
                    return self._resolved(row)
                return self._halt(row,'emergency_close_not_confirmed')
            order=self.private.order_by_external_id(row['proposal']['symbol'],row['external_id'])
            if not isinstance(order,dict) or str(order.get('externalOid'))!=row['external_id']:
                return self._halt(row,'entry_order_not_reconciled')
            expected_side=1 if row['proposal']['side']=='long' else 3
            if (order.get('symbol')!=row['proposal']['symbol'] or order.get('side')!=expected_side
                    or order.get('openType')!=1 or order.get('positionMode')!=row['position_mode']
                    or self._number(order.get('vol'))!=row['contracts']):
                return self._halt(row,'entry_order_identity_mismatch')
            filled=self._number(order.get('dealVol'))
            if filled<0 or filled>row['contracts']:
                return self._halt(row,'invalid_entry_filled_volume')
            if 0<filled<row['contracts'] and order.get('state') in (1,2):
                # Cancel remaining size once; a timeout is reconciled, not retried.
                if not row.get('cancel_write_attempted'):
                    row.update(cancel_write_attempted=True,status='partial_entry_cancel_unknown')
                    self._save(row)
                    try:
                        self._gate(entry=False)
                        self.private.cancel_order(order['orderId'])
                    except Exception:
                        return self._halt(row,'partial_entry_cancel_unknown')
                return self._halt(row,'partial_entry_cancel_confirmation_required')
            if filled==0 and order.get('state') in (4,5):
                row.update(status='entry_rejected',entry_rejection={
                    'code':'EXCHANGE_ORDER_UNFILLED','stage':'order_reconciliation',
                    'order_state':order['state']})
                return self._save(row)
            if not (filled==row['contracts'] and order.get('state')==3 or 0<filled<row['contracts'] and order.get('state') in (4,5)):
                return self._halt(row,'entry_fill_not_fully_confirmed')
            row.update(effective_contracts=filled,exchange_order_id=str(order['orderId']),
                entry_fill_price=self._number(order.get('dealAvgPrice')),partial_fill=filled<row['contracts'])
            position=self._position(row,order)
            if position is None:
                closed=self._historical_close(row,order)
                if closed:return closed
                return self._halt(row,'entry_position_not_identified')
            row.update(position_id=str(position['positionId']),status='entry_filled_unprotected')
            self._save(row)
            fill_price=row['entry_fill_price']
            liquidation=self._number(position.get('liquidatePrice'))
            stop=self.protection_levels(row)[0]
            stop_buffer=(stop-liquidation)/fill_price if row['proposal']['side']=='long' else (liquidation-stop)/fill_price
            worst_stop=stop*(1-self.slip if row['proposal']['side']=='long' else 1+self.slip)
            qty=self.remaining_contracts(row)*row['contract_size']
            actual_risk=max(0,fill_price-worst_stop if row['proposal']['side']=='long' else worst_stop-fill_price)*qty+self.fee*qty*(fill_price+worst_stop)
            if liquidation<=0 or stop_buffer<self.liq_buffer or actual_risk>row['proposal']['max_loss_if_stopped']+1e-8:
                self._halt(row,'filled_position_risk_exceeds_plan')
                return self._close_once(row,position,token,'EMERGENCY')
            protection_state=self._protected(row,position)
            if protection_state is True:
                row.update(status='protected')
                return self._resolved(row)
            if protection_state is None:
                return self._emergency_close(row,position,token)
            if not row.get('protection_write_attempted') and not row.get('emergency_close_attempted'):
                self._gate(entry=False)
                self._check_lease(token)
                row.update(protection_write_attempted=True,status='protection_write_unknown')
                self._save(row)
                try:
                    self._gate(entry=False)
                    self._check_lease(token)
                    self.private.place_protection(position_id=row['position_id'],contracts=self.remaining_contracts(row),
                        stop_loss=self.protection_levels(row)[0],take_profit=self.protection_levels(row)[1],
                        request_id='sl-'+row['intent_id'][:24])
                except Exception:
                    pass # One write only; confirm by read before deciding exposure status.
                if self._protected(row,position):
                    row.update(status='protected')
                    return self._resolved(row)
            return self._emergency_close(row,position,token)
        except Exception:
            return self._halt(row,'reconciliation_incomplete')

    def _historical_close(self,row,order):
        if not callable(getattr(self.private,'historical_positions',None)):
            return None
        position_id=str(order.get('positionId'))
        for page in range(1,11):
            rows=self.private.historical_positions(row['proposal']['symbol'],page_num=page,page_size=100)
            if not isinstance(rows,list):return None
            matches=[p for p in rows if str(p.get('positionId'))==position_id and p.get('symbol')==row['proposal']['symbol']]
            for closed in matches:
                if (closed.get('state')!=3 or closed.get('positionType')!=(1 if row['proposal']['side']=='long' else 2)
                        or self._number(closed.get('holdVol'))!=0
                        or self._number(closed.get('closeVol'))!=row.get('effective_contracts',row['contracts'])):
                    continue
                # Exchange-reported realised net is authoritative; do not infer a
                # TP/SL cause merely from a chart or an entry order acknowledgement.
                net=self._number(closed.get('realised'))
                closed_at=self._number(closed.get('updateTime'))
                if closed_at<row['staged_at']*1000:return None
                reason=closed.get('closeReason')
                reason=reason if reason in ('TP','SL','LIQ','MANUAL') else row.get('close_reason','EXCHANGE_CLOSE')
                row.update(status='closed',position_id=position_id,net_pnl=net,net_pnl_source='MEXC historical_positions.realised',
                    closed_at=closed_at,close_reason=reason,realized_complete=True)
                for source,target in (('closeProfitLoss','gross_pnl'),('totalFee','total_fee'),('holdFee','funding_signed')):
                    if source in closed:row[target]=self._number(closed[source])
                return self._resolved(row)
            if len(rows)<100:break
        return None

    def request_close(self,intent_id):
        """Explicit operator close of one exactly identified managed exposure."""
        self._gate(entry=False)
        token=self._acquire_lease(intent_id)
        try:
            row=self.state(intent_id)
            if row.get('binding_id')!=self.binding_id:raise LiveExecutionError('credential_binding_changed')
            if row.get('management_pending'):
                row=self._reconcile_management(row,token)
                if row.get('management_pending') or row.get('status')=='closed':return row
            if row.get('close_write_attempted') or row.get('emergency_close_attempted'):
                return self._reconcile(row,token)
            order=self.private.order_by_external_id(row['proposal']['symbol'],row['external_id'])
            position=self._position(row,order)
            if position is None:raise LiveExecutionError('managed_close_position_not_identified')
            return self._close_once(row,position,token,'MANUAL')
        finally:
            self._release_lease(token)

    def _close_once(self,row,position,token,reason):
        if row.get('close_write_attempted') or row.get('emergency_close_attempted'):
            return self.state(row['intent_id'])
        self._gate(entry=False)
        self._check_lease(token)
        row.update(close_write_attempted=True,close_reason=reason,status='close_write_unknown',close_contracts=self.remaining_contracts(row))
        if reason=='EMERGENCY':row['emergency_close_attempted']=True
        self._save(row)
        try:
            self._gate(entry=False)
            self._check_lease(token)
            self.private.close_position(symbol=row['proposal']['symbol'],side=row['proposal']['side'],
                contracts=self.remaining_contracts(row),position_id=row['position_id'],external_id='close-'+row['intent_id'][:24],
                position_mode=row['position_mode'])
        except Exception:
            return self.state(row['intent_id'])
        return self.state(row['intent_id']) # An acknowledgement is not a close fill.

    def _emergency_close(self,row,position,token):
        self._halt(row,'native_protection_not_confirmed')
        if not row.get('allow_emergency_close'):
            return self.state(row['intent_id'])
        return self._close_once(row,position,token,'EMERGENCY')
