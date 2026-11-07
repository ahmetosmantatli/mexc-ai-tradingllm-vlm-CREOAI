"""Real-account orchestration. No paper ledger, synthetic fills or seeded cash.

Network clients and execution policy are injected. Entry requires a configured
AutomaticLiveExecutor and an existing explicit start grant. Monitoring remains
available while new entries are paused. Every final proposal uses freshly read
exchange equity; account versions share this runner's one canonical store.
"""
import copy
from concurrent.futures import ThreadPoolExecutor,as_completed
import hashlib
from math import isfinite
from pathlib import Path
import sqlite3
import threading
import time

from trading.config import RiskProfile
from trading.evaluation.planning import apply_decision_sizing,PlanSizingError
from trading.evaluation.service import ContextEvaluator,finalize_trace
from trading.evaluation.watch import WatchCoordinator,watch_snapshot,TIMEFRAMES
from trading.quant.scanner import MarketScanner,LiquidityFilter
from trading.risk.engine import RiskEngine
from trading.runtime.candidates import CandidateQueue
from trading.runtime.live_account import LiveAccountReconciler,observe_risk_reference


class LiveRunner:
    def __init__(self,settings,*,store,account_id,binding_id,private,public,llm,
                 external_control=None,executor=None,**unused):
        self.cfg,self.store,self.account_id,self.binding_id=settings,store,account_id,binding_id
        self.private,self.public,self.llm=private,public,llm
        self.external_control=external_control or (lambda:dict(enabled=False))
        self.executor=executor
        self.lock=store.lock
        self.external_scan=True
        self.reconciler=LiveAccountReconciler(account_id,store,binding_id=binding_id,
            max_age_seconds=settings.market_max_age_seconds)
        self.risk=RiskEngine(settings=settings)
        self.graph=ContextEvaluator(llm,store,public,settings)
        self._cycle_lock,self._monitor_lock,self._scan_lock=threading.Lock(),threading.Lock(),threading.Lock()

    def candidate_queue(self):
        return CandidateQueue(self.store)

    def _control(self):
        return dict(self.external_control())

    def _enabled(self,control=None):
        control=self._control() if control is None else control
        return bool(control.get('enabled') and not control.get('stopped') and not Path(self.cfg.kill_switch_path).exists())

    def configure(self,control):
        """Called only between analysis jobs; policy limits remain deterministic."""
        profile=control.get('risk_profile',getattr(self.cfg.risk_profile,'value',self.cfg.risk_profile))
        if profile not in ('capital_preservation','balanced','aggressive_growth'):
            profile='balanced'
        self.cfg.risk_profile=RiskProfile(profile)
        self.risk=RiskEngine(settings=self.cfg)
        self.cfg.analysis_version=control.get('version',self.cfg.analysis_version)
        if self.cfg.analysis_version not in ('text','vlm'):
            raise ValueError('invalid analysis version')
        if self.executor is not None:
            self.executor.risk=self.risk

    def _risk_fraction(self,control):
        requested=control.get('risk_per_trade_pct',self.cfg.risk_per_trade_pct)
        if isinstance(requested,bool) or not isinstance(requested,(int,float)) or not isfinite(requested) or requested<=0:
            raise ValueError('invalid live risk')
        return min(requested,self.cfg.risk_per_trade_pct,self.risk.preset.risk_per_trade_pct)

    def _leverage_cap(self,control):
        cap=min(getattr(self.cfg,'live_max_leverage',10),
            self.risk.preset.max_leverage_majors,self.risk.preset.max_leverage_alts)
        requested=control.get('max_leverage',cap)
        if isinstance(requested,bool) or not isinstance(requested,(int,float)) or not isfinite(requested) or requested<1:
            raise ValueError('invalid live leverage')
        return min(cap,requested)

    @staticmethod
    def _capital(snapshot,control):
        equity=snapshot['balance']['equity']
        if control.get('allocation_mode') in ('full','all','entire'):
            return equity
        amount=control.get('capital_amount')
        if amount is None:
            raise ValueError('explicit live allocation required')
        if isinstance(amount,bool) or not isinstance(amount,(int,float)) or not isfinite(amount) or amount<=0:
            raise ValueError('invalid live allocation')
        return min(equity,amount)

    def quote(self,symbol,client=None):
        ticker=(client or self.public).get_ticker(symbol)
        age=time.time()-float(ticker.raw['timestamp'])/1000
        values=(ticker.bid1,ticker.ask1,ticker.last_price,ticker.quote_volume_24h,ticker.raw.get('fairPrice'))
        if not all(isinstance(v,(int,float)) and not isinstance(v,bool) and isfinite(v) and v>0 for v in values):
            raise ValueError('invalid live quote')
        if not -5<=age<=self.cfg.market_max_age_seconds or ticker.ask1<ticker.bid1:
            raise ValueError('stale live quote')
        return ticker

    def _refresh(self,symbols=()):
        snapshot=self.reconciler.refresh(self.private,symbols=symbols)
        observe_risk_reference(self.store,self.binding_id,snapshot['balance']['equity'])
        self.store.put('live_account',snapshot)
        return snapshot

    def _positions(self,snapshot):
        intents=self._intents()
        result={}
        for row in snapshot.get('positions',[]):
            intent=next((value for value in intents if str(value.get('position_id'))==str(row['position_id'])),{})
            proposal=intent.get('proposal',{})
            metadata=proposal.get('metadata',{})
            result[row['symbol']]=dict(row,symbol=row['symbol'],side='long' if row['side']==1 else 'short',
                entry=row['entry'],leverage=row['leverage'],unrealized=row['unrealized'],
                stop=proposal.get('stop_loss',{}).get('price'),target=proposal.get('take_profit_price'),
                risk=proposal.get('max_loss_if_stopped'),notional=proposal.get('notional'),
                initial_stop=proposal.get('stop_loss',{}).get('price'),decision_ai=metadata.get('decision_ai',{}),
                strategy=metadata.get('strategy'),analysis_version=metadata.get('analysis_version'))
        if len(result)!=len(snapshot.get('positions',[])):
            raise ValueError('multiple live positions for symbol')
        return result

    def _live_context(self,snapshot,control):
        balance=snapshot['balance'];now=time.time()
        closed=[row for row in self._intents() if row.get('status')=='closed']
        confirmed=[row for row in closed if row.get('realized_complete') and isinstance(row.get('net_pnl'),(int,float))]
        with self.lock:
            reference=self.store.get('live:research_reference:'+self.binding_id)
            if reference is None:
                reference=dict(initial=balance['equity'],started_at=now)
                self.store.put('live:research_reference:'+self.binding_id,reference)
        counts={reason:sum(row.get('close_reason')==reason for row in closed) for reason in ('TP','SL','MANUAL','LIQ')}
        counts['UNKNOWN']=sum(row.get('close_reason') not in counts for row in closed)
        recent=[dict(symbol=row.get('proposal',{}).get('symbol'),side=row.get('proposal',{}).get('side'),
            closed_at=row.get('closed_at'),reason=row.get('close_reason','UNKNOWN'),net_pnl_usdt=row.get('net_pnl'),
            net_complete=bool(row.get('realized_complete'))) for row in sorted(closed,key=lambda row:row.get('closed_at') or 0,reverse=True)[:5]]
        initial=reference['initial']
        objective=dict(enabled=False)
        if self.cfg.research_goal_enabled and initial>0:
            target=initial*self.cfg.research_goal_multiple
            deadline=reference['started_at']+self.cfg.research_goal_days*86400
            objective=dict(enabled=True,kind='live_research_objective_only',guaranteed=False,
                must_not_increase_risk=True,target_usdt=target,deadline=deadline,
                remaining_seconds=max(0,deadline-now),remaining_usdt=max(0,target-balance['equity']))
        return dict(mode='LIVE',currency='USDT',source='MEXC authenticated futures endpoints',
            accounting_scope='managed_closed_records_only; wallet transfers and unmanaged trade outcomes are not reconstructed',
            equity=balance['equity'],current_equity=balance['equity'],available=balance['available'],
            position_margin=balance['position_margin'],unrealized=balance['unrealized'],
            initial_usdt=initial,equity_snapshot_usdt=balance['equity'],cash_usdt=balance.get('cash'),
            available_usdt=balance['available'],open_margin_usdt=balance['position_margin'],
            open_unrealized_usdt=balance['unrealized'],managed_capital=self._capital(snapshot,control),
            realized_net_total_usdt=sum(row['net_pnl'] for row in confirmed) if len(confirmed)==len(closed) else None,
            fees_total_usdt=sum(row['total_fee'] for row in closed) if all('total_fee' in row for row in closed) else None,
            funding_total_usdt=sum(row['funding_signed'] for row in closed) if all('funding_signed' in row for row in closed) else None,
            closed_trade_count=len(closed),close_reason_counts=counts,recent_closed=recent,objective=objective,
            observed_at=snapshot['observed_at'],generation=snapshot['generation'],
            risk_limits=snapshot['risk_limits'],leverage=snapshot['leverage'])

    def monitor(self):
        if not self._monitor_lock.acquire(blocking=False):
            return self.status('LIVE_MONITOR_BUSY')
        try:
            if self.executor is not None:
                self.executor.reconcile_all()
            self._refresh()
            return self.status('LIVE_RECONCILED')
        except sqlite3.Error:
            raise
        except Exception as exc:
            self.store.put('live_monitor_error',dict(at=time.time(),type=type(exc).__name__))
            return self.status('HOLD_LIVE_RECONCILIATION')
        finally:
            self._monitor_lock.release()

    def _watch_candidates(self):
        coordinator=WatchCoordinator(self.store)
        produced=[]
        for summary in [row for row in watch_snapshot(self.store) if row['status'] in ('watching','triggered')][:40]:
            symbol=summary['symbol']
            coordinator.observe(symbol)
            card=coordinator.card(symbol)
            if card['status'] not in ('watching','triggered'):
                continue
            ticker=self.quote(symbol)
            plan=card['plan'];closed=None
            if plan['trigger_condition'].startswith('closed_candle'):
                tf=plan['timeframe'];seconds=TIMEFRAMES[tf]
                rows=self.public.get_klines(symbol,interval={'5m':'Min5','15m':'Min15','4h':'Hour4'}[tf],limit=4)['candles']
                now=time.time()
                observations=[dict(close=float(row[4]),closed_at=float(row[0])+seconds,complete=True)
                    for row in rows if isinstance(row,(list,tuple)) and len(row)>=5 and float(row[0])+seconds<=now]
                if observations and now-observations[-1]['closed_at']<seconds*2:
                    closed={tf:observations}
            coordinator.observe(symbol,price=ticker.last_price,closed_candles=closed,expected_revision=card['revision'])
            card=coordinator.card(symbol)
            if card['status']!='triggered':
                continue
            scan=MarketScanner(self.public,max_symbols=1,max_workers=1).scan(symbols=[symbol])
            latest=coordinator.card(symbol)
            if latest['revision']!=card['revision']:
                continue
            produced.extend(candidate for candidate in scan.candidates if candidate.side.value==card['side']
                and candidate.strategy.value==card['candidate']['strategy'])
        return produced

    def scan_market(self):
        if not self._scan_lock.acquire(blocking=False):
            return None
        try:
            if not self._enabled():
                return None
            scan=MarketScanner(self.public,liquidity=LiquidityFilter(self.cfg.scanner_min_quote_volume_24h,
                self.cfg.scanner_max_spread_bps),max_symbols=self.cfg.scanner_max_symbols).scan()
            errors=dict(scan.errors)
            try:
                scan.candidates.extend(self._watch_candidates())
            except sqlite3.Error:
                raise
            except Exception as exc:
                errors['watch']=type(exc).__name__
            eligible=[]
            for candidate in scan.candidates:
                closed=candidate.features.get('source_closed_at')
                opened=candidate.features.get('source_open_at')
                if candidate.confidence<.65 or not isinstance(closed,(int,float)) or opened is None or not 0<=time.time()-closed<=1200:
                    continue
                key=hashlib.sha256(f'{candidate.symbol}:{candidate.strategy.value}:{candidate.side.value}:{int(float(opened))}'.encode()).hexdigest()
                candidate.features['dedup_key']=key
                eligible.append(candidate)
            if not self._enabled():
                return None
            size=self.candidate_queue().refresh(eligible)
            self.store.put('last_scan_at',time.time())
            summary=dict(at=time.time(),scanned=scan.scanned,kept=scan.kept,eligible=len(eligible),queue_size=size,errors=errors)
            self.store.put('scan_summary',summary)
            return summary
        finally:
            self._scan_lock.release()

    def _evaluate(self,candidate,snapshot,control):
        child=self.llm.fork() if callable(getattr(self.llm,'fork',None)) else self.llm
        try:
            child.can_call=lambda:self._enabled() and self._control().get('updated_at')==control.get('updated_at')
            evaluator=ContextEvaluator(child,self.store,self.public,self.cfg) if child is not self.llm else self.graph
            capital=self._capital(snapshot,control)
            live_context=self._live_context(snapshot,control)
            return evaluator.evaluate_candidates([candidate],equity=capital,risk_per_trade_pct=self._risk_fraction(control),
                target_leverage=self._leverage_cap(control),risk_profile=self.cfg.risk_profile.value,
                live_account_context=live_context,live_positions=self._positions(snapshot))
        finally:
            if child is not self.llm and callable(getattr(child,'close',None)):
                child.close()

    def _final_proposal(self,candidate,result,snapshot,control):
        rules=self.public.get_contract_rules(candidate.symbol)
        side=1 if candidate.side.value=='long' else 2
        private_risk=next(row for row in snapshot['risk_limits'][candidate.symbol] if row['side']==side)
        private_leverage=next(row for row in snapshot['leverage'][candidate.symbol] if row['side']==side)
        exchange_cap=min(rules.max_leverage,private_risk['max_leverage'],private_leverage['max_leverage'])
        proposal=apply_decision_sizing(candidate,result.metadata.get('sizing_plan'),self._capital(snapshot,control),
            self._risk_fraction(control),self._leverage_cap(control),exchange_cap)
        ticker=self.quote(candidate.symbol)
        if abs(ticker.last_price/candidate.entry_price-1)>min(candidate.stop_distance_pct/2,.005):
            raise ValueError('live entry price drift')
        entry=ticker.ask1 if candidate.side.value=='long' else ticker.bid1
        slip=self.cfg.paper_slippage_bps/10000;fee=self.cfg.paper_fee_rate
        worst_entry=entry*(1+slip if candidate.side.value=='long' else 1-slip)
        stop=proposal.stop_loss.price*(1-slip if candidate.side.value=='long' else 1+slip)
        loss_fraction=(abs(worst_entry-stop)+fee*(worst_entry+stop))/entry
        max_notional=min(proposal.notional,proposal.max_loss_if_stopped/loss_fraction,
            snapshot['balance']['available']/(max(1/proposal.leverage,private_risk['initial_rate'])+fee)*.95)
        _,notional=rules.size_order(max_notional,entry,proposal.leverage)
        proposal.notional=notional
        proposal.rationale=result.metadata.get('rationale',candidate.rationale)
        proposal.metadata.update(contract_size=rules.size,analysis_version=self.cfg.analysis_version,charts=result.metadata.get('charts',[]),
            decision_ai=result.metadata.get('decision_ai',{}),account_generation=snapshot['generation'])
        return proposal

    def _apply(self,candidate,result,control):
        trace_id=result.metadata.get('analysis_trace_id')
        def finish(reason):
            if trace_id:
                finalize_trace(self.store,trace_id,'opened' if reason=='LIVE_PROTECTED' else 'rejected',reason)
            return self.status(reason)
        if result.action.value!='trade':
            return finish('HOLD_LIVE_MODEL')
        if (result.selected_signal is None or result.selected_signal.signal_id!=candidate.signal_id
                or not all(note and note.mode=='llm' for note in (result.market,result.strategy,result.portfolio))):
            return finish('HOLD_LIVE_UNVALIDATED_MODEL')
        if not self._enabled() or self._control().get('updated_at')!=control.get('updated_at'):
            return finish('HOLD_LIVE_CONTROL_CHANGED')
        if self.executor is None:
            return finish('HOLD_LIVE_EXECUTOR_DISABLED')
        if self.store.get('live:halt'):
            return finish('HOLD_LIVE_HALT')
        started=result.metadata.get('decision_started_at',0)
        if not started or time.time()-started>self.cfg.decision_ttl_seconds:
            return finish('HOLD_LIVE_DECISION_EXPIRED')
        if not self.candidate_queue().is_current(candidate):
            return finish('HOLD_LIVE_CANDIDATE_STALE')
        coordinator=WatchCoordinator(self.store)
        card=coordinator.card(candidate.symbol)
        if (result.metadata.get('watch_revision')!=card['revision'] or card.get('last_action')!='trade'
                or card.get('candidate',{}).get('signal_id')!=candidate.signal_id):
            return finish('HOLD_LIVE_WATCH_CHANGED')
        snapshot=self._refresh([candidate.symbol])
        if candidate.symbol in self._positions(snapshot):
            return finish('HOLD_LIVE_SYMBOL_OPEN')
        if len(snapshot['positions'])>=min(control.get('max_positions',getattr(self.cfg,'live_max_positions',4)),getattr(self.cfg,'live_max_positions',4),self.risk.preset.max_concurrent_positions):
            return finish('HOLD_LIVE_POSITION_LIMIT')
        proposal=self._final_proposal(candidate,result,snapshot,control)
        # Execution performs its own fresh account/risk/quote and grant checks.
        self.executor.allowed_capital=self._capital(snapshot,control)
        intent=self.executor.authorize(proposal,control_revision=control.get('updated_at'))
        observed=next(row for row in snapshot['leverage'][candidate.symbol] if row['side']==(1 if candidate.side.value=='long' else 2))
        if observed['leverage']!=proposal.leverage:
            if not self._enabled() or self._control().get('updated_at')!=control.get('updated_at'):
                return finish('HOLD_LIVE_CONTROL_CHANGED')
            self.private.change_leverage(symbol=candidate.symbol,side=candidate.side.value,leverage=int(proposal.leverage),
                request_id='lev-'+intent['intent_id'][:24])
            self._refresh([candidate.symbol]) # Never assume the leverage write applied.
        # Revision/control may have changed during HTTP. Executor calls this guard
        # again at its final entry gate, after its own preflight network reads.
        original=getattr(self.executor,'can_execute',None)
        def current():
            latest=coordinator.card(candidate.symbol)
            return bool((original is None or original()) and self._enabled()
                and self._control().get('updated_at')==control.get('updated_at')
                and self.candidate_queue().is_current(candidate)
                and latest['revision']==card['revision'] and latest.get('last_action')=='trade'
                and time.time()-started<=self.cfg.decision_ttl_seconds)
        self.executor.can_execute=current
        try:
            state=self.executor.execute(intent['intent_id'])
        finally:
            self.executor.can_execute=original
        if state.get('status')=='protected':
            with self.lock:
                self.store.put('live_seen',(self.store.get('live_seen',[])+[candidate.features['dedup_key']])[-2000:])
                coordinator.terminate(candidate.symbol,'position_opened',expected_revision=card['revision'])
            try:
                self._refresh()
            except sqlite3.Error:
                raise
            except Exception:
                pass # Protection is confirmed; failed balance refresh stays explicitly unready.
            return finish('LIVE_PROTECTED')
        return finish('HOLD_LIVE_'+str(state.get('status','UNKNOWN')).upper())

    def cycle(self):
        if not self._cycle_lock.acquire(blocking=False):
            return self.status('LIVE_ANALYSIS_BUSY')
        selected=[]
        try:
            control=self._control()
            self.configure(control)
            if not self._enabled(control):
                return self.status('LIVE_PAUSED')
            if self.executor is None:
                return self.status('HOLD_LIVE_EXECUTOR_DISABLED')
            if self.store.get('live:halt'):
                return self.status('HOLD_LIVE_HALT')
            if callable(getattr(self.executor,'entry_ready',None)) and not self.executor.entry_ready():
                return self.status('HOLD_LIVE_START_REQUIRED')
            snapshot=self._refresh()
            if snapshot['balance']['equity']<=0 or snapshot['balance']['available']<=0:
                return self.status('HOLD_LIVE_NO_FUNDS')
            risk=observe_risk_reference(self.store,self.binding_id,snapshot['balance']['equity'])
            if (risk['daily_loss_pct']>=self.risk.preset.daily_loss_halt_pct or risk['weekly_loss_pct']>=self.risk.preset.weekly_loss_halt_pct
                    or risk['drawdown_pct']>=self.risk.preset.max_drawdown_kill_pct):
                return self.status('HOLD_LIVE_LOSS_CIRCUIT_BREAKER')
            if not self.external_scan:
                self.scan_market()
            waiting={row['symbol'] for row in watch_snapshot(self.store) if row['status']=='watching'}
            count=min(4,getattr(self.cfg,'ai_parallel_candidates',1)) if callable(getattr(self.llm,'fork',None)) else 1
            queue=self.candidate_queue()
            for _ in range(count):
                candidate=queue.next(open_symbols=set(self._positions(snapshot))|waiting|{c.symbol for c in selected},
                    seen=self.store.get('live_seen',[]),lease_seconds=self.cfg.decision_ttl_seconds+60)
                if candidate is None:
                    break
                selected.append(candidate)
            if not selected:
                return self.status('HOLD_LIVE_NO_CANDIDATE')
            snapshot=self._refresh([candidate.symbol for candidate in selected])
            outcomes=[]
            with ThreadPoolExecutor(max_workers=len(selected),thread_name_prefix='live-analysis') as pool:
                jobs={pool.submit(self._evaluate,candidate,snapshot,control):candidate for candidate in selected}
                for future in as_completed(jobs):
                    candidate=jobs[future]
                    try:
                        result=future.result()
                        if result.metadata.get('provider_called') is not False:
                            queue.evaluated(candidate)
                        outcomes.append(self._apply(candidate,result,control))
                    except sqlite3.Error:
                        raise
                    except Exception as exc:
                        self.store.put('live_candidate_error:'+candidate.symbol,dict(at=time.time(),type=type(exc).__name__))
                        outcomes.append(self.status('HOLD_LIVE_CANDIDATE_ERROR'))
            return next((row for row in outcomes if row['reason']=='LIVE_PROTECTED'),outcomes[-1])
        except sqlite3.Error:
            raise
        except Exception as exc:
            self.store.put('live_cycle_error',dict(at=time.time(),type=type(exc).__name__))
            return self.status('HOLD_LIVE_ERROR')
        finally:
            for candidate in selected:
                self.candidate_queue().release(candidate)
            self._cycle_lock.release()

    def _intents(self):
        return self.executor.intents() if self.executor is not None and callable(getattr(self.executor,'intents',None)) else []

    def can_disconnect(self):
        try:
            snapshot=self._refresh()
            return not snapshot['positions'] and not snapshot['orders'] and not any(
                row.get('entry_write_attempted') and row.get('status') not in ('closed','entry_rejected') for row in self._intents())
        except sqlite3.Error:
            raise
        except Exception:
            return False

    def request_close(self,command):
        if self.executor is None or not callable(getattr(self.executor,'request_close',None)):
            raise ValueError('native live manual close unavailable')
        value=command.get('intent_id') or command.get('symbol') if isinstance(command,dict) else command
        intents=[row for row in self._intents() if row.get('entry_write_attempted')
            and row.get('status') not in ('closed','entry_rejected')
            and (row['intent_id']==value or row.get('proposal',{}).get('symbol')==value)]
        if len(intents)!=1:
            raise ValueError('one identified managed live position required')
        return self.executor.request_close(intents[0]['intent_id'])

    def status(self,reason):
        account=self.store.get(self.reconciler.key,{})
        intents=self._intents()
        positions={}
        # Keep hedge legs individually visible, even though entry analysis rejects
        # multiple exposures for one symbol until explicitly reconciled.
        for row in account.get('positions',[]):
            intent=next((value for value in intents if str(value.get('position_id'))==str(row['position_id'])),{})
            proposal=intent.get('proposal',{})
            metadata=proposal.get('metadata',{})
            key=row['symbol'] if row['symbol'] not in positions else row['symbol']+':'+str(row['position_id'])
            positions[key]=dict(row,symbol=row['symbol'],side='long' if row['side']==1 else 'short',
                stop=proposal.get('stop_loss',{}).get('price'),target=proposal.get('take_profit_price'),
                opened_at=row.get('opened_at'),intent_id=intent.get('intent_id'),
                protection_status=intent.get('status','unmanaged'),analysis_version=metadata.get('analysis_version'),
                decision_ai=metadata.get('decision_ai',{}))
        closed=[row for row in intents if row.get('status')=='closed']
        snapshot=dict(mode='LIVE',live_orders_enabled=bool(self.executor is not None),observed_at=time.time(),reason=reason,
            account=dict(ready=account.get('ready',False),**account.get('balance',{})),
            live_account=account,real_account=account,balance=account.get('balance'),positions=positions,closed=closed,intents=intents,
            analysis_trace=self.store.get('analysis_trace',{}),analysis_history=self.store.get('analysis_history',[]),
            watchlist=watch_snapshot(self.store),analysis_version=self.cfg.analysis_version,
            ai_status=getattr(self.llm,'last_status','not_called'),live_halt=self.store.get('live:halt'),is_simulated=False)
        with self.lock:
            events=self.store.get('live_events',[])
            known={event['id'] for event in events}
            for intent in intents:
                status=intent.get('status')
                if status not in ('protected','closed','emergency_close_unknown','manual_close_unknown','close_write_unknown'):
                    continue
                ident='live:'+intent['intent_id']+':'+status
                if ident not in known:
                    events.append(dict(id=ident,status=status,intent_id=intent['intent_id'],
                        symbol=intent.get('proposal',{}).get('symbol'),at=time.time(),sent=False,
                        proposal=copy.deepcopy(intent.get('proposal',{})),net_pnl=intent.get('net_pnl'),
                        close_reason=intent.get('close_reason'),is_simulated=False))
            self.store.put('live_events',events[-200:])
            self.store.put('status',snapshot)
        return snapshot

    @staticmethod
    def status_text(snapshot):
        balance=snapshot.get('balance') or {}
        return ('Canlı MEXC hesabı\n'+f"Durum: {snapshot.get('reason','')}\n"+
            f"Bakiye: {balance.get('equity','bilinmiyor')} USDT\nKullanılabilir: {balance.get('available','bilinmiyor')} USDT\n"+
            f"Açık pozisyon: {len(snapshot.get('positions') or [])}")

    def close(self):
        for client in (self.llm,self.private,self.public):
            if callable(getattr(client,'close',None)):
                client.close()
