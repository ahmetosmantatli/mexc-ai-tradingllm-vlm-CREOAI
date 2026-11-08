"""Durable, single-write management of already-owned LIVE exposure.

No cancel/replace gap: price changes modify the identified native pair in place.
Partial exits retain protection and resize the same-price pair after a verified
reduce fill. Unknown POST outcomes are read back, never blindly submitted twice.
"""
import copy
from decimal import Decimal, ROUND_DOWN
from time import time
from uuid import uuid4


class PositionManagementMixin:
    @staticmethod
    def remaining_contracts(row):
        return row.get('managed_contracts',row.get('effective_contracts',row['contracts']))

    @staticmethod
    def protection_levels(row):
        levels=row.get('current_protection')
        return (levels['stop'],levels['target']) if levels else (row['proposal']['stop_loss']['price'],row['proposal']['take_profit_price'])

    def _management_error(self,reason):
        from .execution import LiveExecutionError
        raise LiveExecutionError(reason)

    def _management_authorized(self,guard):
        automatic=callable(getattr(self,'_current_grant',None))
        # An entry-specific candidate guard may be temporarily installed by the
        # entry worker. Management has its own revision/deadline/control guard.
        self._gate(entry=not automatic)
        if self.gates.live_trading_enabled is not True:self._management_error('management_live_disabled')
        if automatic:self._current_grant()
        if not guard():self._management_error('management_control_changed')

    def management_ready(self):
        try:
            self._management_authorized(lambda:True)
            return True
        except Exception:return False

    def manage_position(self,intent_id,decision,*,expected_revision,decision_id,guard):
        """Caller validates typed AI output. Revalidate ownership/size/price here."""
        self._management_authorized(guard)
        token=self._acquire_lease(intent_id)
        try:
            row=self.state(intent_id)
            if row.get('binding_id')!=self.binding_id:self._management_error('credential_binding_changed')
            if row.get('management_revision',0)!=expected_revision:self._management_error('management_revision_changed')
            if row.get('management_pending') or row.get('status')!='protected' or row.get('close_write_attempted'):
                self._management_error('management_position_busy')
            if any(event['decision_id']==decision_id for event in row.get('management_events',[])):
                return row
            order=self.private.order_by_external_id(row['proposal']['symbol'],row['external_id'])
            position=self._position(row,order)
            if position is None or self._protected(row,position) is not True:
                self._management_error('management_owned_protection_required')
            ticker=self.public.get_ticker(row['proposal']['symbol'])
            age=time()-self._number(ticker.raw.get('timestamp'))/1000
            bid,ask=self._number(ticker.bid1),self._number(ticker.ask1)
            if not -5<=age<=self.max_age or bid<=0 or ask<bid:self._management_error('management_fresh_quote_required')
            stop,target=self.protection_levels(row)
            fair=self._number(ticker.raw.get('fairPrice'))
            long=row['proposal']['side']=='long'
            if fair<=0 or not (stop<min(bid,fair) and target>max(ask,fair) if long else target<min(bid,fair) and stop>max(ask,fair)):
                self._management_error('management_protection_already_triggerable')
            rules=self.public.get_contract_rules(row['proposal']['symbol'])
            action=decision['action']
            pending=dict(decision_id=decision_id,action=action,reason=decision['reason'],ai=decision.get('ai',{}),
                at=time(),before_contracts=self.remaining_contracts(row),old_stop=stop,target=target,
                external_id='mg-'+uuid4().hex[:24],write_attempted=True)
            if action=='move_stop':
                new=self._number(decision['stop_price'])
                units=Decimal(str(new))/Decimal(str(rules.price_step))
                if abs(units-units.to_integral_value())>Decimal('.000001'):self._management_error('management_tick_mismatch')
                if not (stop<new<min(bid,fair) if long else stop>new>max(ask,fair)):
                    self._management_error('management_stop_must_tighten')
                liquidation=self._number(position.get('liquidatePrice'))
                entry=self._number(position.get('holdAvgPrice'))
                buffer=(new-liquidation)/entry if long else (liquidation-new)/entry
                if liquidation<=0 or buffer<self.liq_buffer:self._management_error('management_liquidation_buffer')
                protections=self.private.protection_orders(row['proposal']['symbol'])
                matches=[p for p in protections if str(p.get('positionId'))==row['position_id']]
                if len(matches)!=1:self._management_error('management_one_native_pair_required')
                ident=matches[0].get('id') or matches[0].get('stopPlanOrderId')
                if ident is None:self._management_error('management_protection_id_missing')
                pending.update(stop=new,protection_id=ident)
            elif action in ('reduce','close'):
                before=pending['before_contracts']
                fraction=self._number(decision['close_fraction'])
                if action=='close' and fraction!=1 or action=='reduce' and not 0<fraction<1:
                    self._management_error('management_invalid_exit_fraction')
                contracts=float((Decimal(str(before))*Decimal(str(fraction))/Decimal(str(rules.step))).to_integral_value(rounding=ROUND_DOWN)*Decimal(str(rules.step)))
                if contracts<=0 or contracts<rules.minimum or action=='reduce' and before-contracts<rules.minimum:
                    self._management_error('management_exit_below_contract_minimum')
                pending['requested_contracts']=contracts
            else:self._management_error('management_action_not_supported')
            self._management_authorized(guard)
            self._check_lease(token)
            row.update(management_pending=pending,status='management_pending')
            self._save(row) # Durable BEFORE the only POST, including permission revocation races.
            try:
                self._management_authorized(guard)
            except Exception:
                row.pop('management_pending',None);row['status']='protected';self._save(row)
                raise
            try:
                if action=='move_stop':
                    self.private.modify_protection(protection_id=pending['protection_id'],stop_loss=pending['stop'],
                        take_profit=target,request_id=pending['external_id'])
                else:
                    self.private.close_position(symbol=row['proposal']['symbol'],side=row['proposal']['side'],
                        contracts=pending['requested_contracts'],position_id=row['position_id'],
                        external_id=pending['external_id'],position_mode=row['position_mode'])
            except Exception as exc:
                if getattr(exc,'ambiguous',True) is False:
                    return self._management_complete(row,'rejected',error='exchange_rejected')
            return self._reconcile_management(row,token)
        finally:self._release_lease(token)

    def _management_complete(self,row,status,**extra):
        pending=row.pop('management_pending')
        event=dict(pending,status=status,confirmed_at=time(),**extra)
        row['management_events']=(row.get('management_events',[])+[event])[-50:]
        row['management_revision']=row.get('management_revision',0)+1
        if status=='confirmed':
            stop,_=self.protection_levels(row)
            entry=row['entry_fill_price'];long=row['proposal']['side']=='long'
            worst=stop*(1-self.slip if long else 1+self.slip)
            remaining=extra.get('remaining_contracts',self.remaining_contracts(row))
            row['planned_loss']=(max(0,entry-worst if long else worst-entry)+self.fee*(entry+worst))*remaining*row['contract_size']
        row['status']='protected'
        return self._resolved(row)

    def _reconcile_management(self,row,token):
        """Returns only after read-back; an unknown outcome blocks subsequent writes."""
        pending=row['management_pending'];self._check_lease(token)
        try:
            positions=self.private.open_positions(row['proposal']['symbol'])
            if pending['action']=='move_stop' and not any(str(p.get('positionId'))==row['position_id'] and self._number(p.get('holdVol'))>0 for p in positions):
                closed=self._management_native_closed(row)
                if closed:return closed
            if pending['action']=='move_stop':
                expected=copy.deepcopy(row)
                expected['current_protection']=dict(stop=pending['stop'],target=pending['target'])
                order=self.private.order_by_external_id(row['proposal']['symbol'],row['external_id'])
                position=self._position(row,order)
                if position is not None and self._protected(expected,position) is True:
                    row['current_protection']=expected['current_protection']
                    return self._management_complete(row,'confirmed',new_stop=pending['stop'])
                if position is not None and time()-pending['at']>=self.max_age and self._protected(row,position) is not True and row.get('allow_emergency_close'):
                    row=self._management_complete(row,'protection_failed')
                    self._halt(row,'management_stop_protection_failed')
                    return self._close_once(row,position,token,'EMERGENCY')
            else:
                close=self.private.order_by_external_id(row['proposal']['symbol'],pending['external_id'])
                expected_side=4 if row['proposal']['side']=='long' else 2
                if not (isinstance(close,dict) and close.get('externalOid')==pending['external_id'] and close.get('symbol')==row['proposal']['symbol']
                        and close.get('side')==expected_side and close.get('state') in (3,4,5)):
                    if not any(str(p.get('positionId'))==row['position_id'] and self._number(p.get('holdVol'))>0 for p in positions):
                        closed=self._management_native_closed(row)
                        if closed:return closed
                    return self._halt(row,'management_exit_unconfirmed')
                filled=self._number(close.get('dealVol'))
                if not 0<=filled<=pending['requested_contracts']:self._management_error('management_fill_mismatch')
                if filled==0:return self._management_complete(row,'rejected',error='zero_exit_fill')
                remaining=float(Decimal(str(pending['before_contracts']))-Decimal(str(filled)))
                rows=self.private.open_positions(row['proposal']['symbol'])
                matching=[p for p in rows if str(p.get('positionId'))==row['position_id'] and self._number(p.get('holdVol'))>0]
                if remaining==0 and not matching:
                    # Preserve original effective volume for cumulative historical PnL.
                    row['close_reason']='AI_CLOSE'
                    row=self._management_complete(row,'confirmed',filled_contracts=filled,remaining_contracts=0)
                    closed=self._historical_close(row,dict(positionId=row['position_id']))
                    if closed:return closed
                    row.update(status='closed',realized_complete=False,net_pnl=None,close_reason='AI_CLOSE',closed_at=time()*1000)
                    return self._resolved(row)
                if len(matching)!=1 or self._number(matching[0].get('holdVol'))!=remaining:
                    if not matching:
                        closed=self._management_native_closed(row)
                        if closed:return closed
                    return self._halt(row,'management_remaining_not_confirmed')
                row['managed_contracts']=remaining
                position=self._position(row,dict(positionId=row['position_id']))
                if position is None:return self._halt(row,'management_remaining_identity_mismatch')
                if self._protected(row,position) is True:
                    return self._management_complete(row,'confirmed',filled_contracts=filled,remaining_contracts=remaining)
                if time()-pending['at']>=self.max_age:
                    # Confirmed remainder still lacks verified protection after the read-back window.
                    # Emergency exit is distinct from (and never repeats) the original partial exit.
                    if row.get('allow_emergency_close'):
                        row=self._management_complete(row,'protection_failed',filled_contracts=filled,remaining_contracts=remaining)
                        self._halt(row,'management_remaining_protection_failed')
                        return self._close_once(row,position,token,'EMERGENCY')
                if not pending.get('resize_attempted'):
                    # Documented same-price placement updates quantity asynchronously.
                    # Keep the old reverse-disabled pair; never cancel protection first.
                    stop,target=self.protection_levels(row)
                    old=copy.deepcopy(row);old['managed_contracts']=pending['before_contracts']
                    if self._protected(old,position) is not True:
                        return self._halt(row,'management_old_protection_not_identified')
                    self._gate(entry=False);self._check_lease(token)
                    pending['resize_attempted']=True
                    row['management_pending']=pending;self._save(row)
                    try:self.private.place_protection(position_id=row['position_id'],contracts=remaining,
                        stop_loss=stop,take_profit=target,request_id=pending['external_id'])
                    except Exception:pass
                    if self._protected(row,position) is True:
                        return self._management_complete(row,'confirmed',filled_contracts=filled,remaining_contracts=remaining)
            # A native exit can race any change. History is authoritative.
            if not any(str(p.get('positionId'))==row['position_id'] and self._number(p.get('holdVol'))>0
                       for p in self.private.open_positions(row['proposal']['symbol'])):
                closed=self._management_native_closed(row)
                if closed:return closed
            return self._halt(row,'management_write_unconfirmed')
        except Exception:return self._halt(row,'management_reconciliation_incomplete')

    def _management_native_closed(self,row):
        closed=self._historical_close(row,dict(positionId=row['position_id']))
        if not closed:return None
        pending=closed.pop('management_pending')
        closed['management_events']=(closed.get('management_events',[])+[dict(pending,status='position_closed',confirmed_at=time())])[-50:]
        closed['management_revision']=closed.get('management_revision',0)+1
        return self._resolved(closed)
