"""Real-account orchestration. No paper ledger, synthetic fills or seeded cash.

Network clients and execution policy are injected. Entry requires a configured
AutomaticLiveExecutor and an existing explicit start grant. Monitoring remains
available while new entries are paused. Every final proposal uses freshly read
exchange equity; account versions share this runner's one canonical store.
"""
from trading.runtime.live_capabilities import STRUCTURAL_IOC_RUNNERS
from trading.runtime import runner3_policy
import copy
from concurrent.futures import ThreadPoolExecutor,as_completed
from dataclasses import replace
import hashlib
from math import isfinite
from pathlib import Path
import sqlite3
import threading
import time

from trading.config import RiskProfile
from trading.evaluation.price_plan import MarketPricePlan,apply_market_price_plan
from trading.evaluation.planning import apply_decision_sizing,PlanSizingError
from trading.evaluation.service import ContextEvaluator,finalize_trace
from trading.evaluation.watch import WatchCoordinator,watch_snapshot,TIMEFRAMES
from trading.quant.scanner import MarketScanner,LiquidityFilter
from trading.risk.engine import RiskEngine
from trading.risk.presets import get_preset
from trading.runtime.candidates import CandidateQueue
from trading.runtime.execution_diagnostics import EntryPriceDrift
from trading.runtime.live_account import LiveAccountReconciler,observe_risk_reference


class LiveRunner:
    # A stuck local zero is not a reason to stay silent for the whole wait.
    # Three probes, at least one scan apart, then the hold stands.
    capacity_probe_cap=3
    capacity_probe_gap_seconds=180

    def __init__(self,settings,*,store,account_id,binding_id,private,public,llm,
                 external_control=None,executor=None,**unused):
        self.cfg,self.store,self.account_id,self.binding_id=settings,store,account_id,binding_id
        self.private,self.public,self.llm=private,public,llm
        self.external_control=external_control or (lambda:dict(enabled=False))
        self.executor=executor
        self.runner_id='baseline-v1'
        self.lock=store.lock
        self.external_scan=True
        self.reconciler=LiveAccountReconciler(account_id,store,binding_id=binding_id,
            max_age_seconds=settings.market_max_age_seconds)
        self.risk=self._risk_engine()
        self.graph=ContextEvaluator(llm,store,public,settings)
        self._cycle_lock,self._monitor_lock,self._scan_lock=threading.Lock(),threading.Lock(),threading.Lock()
        self._management_lock=threading.Lock()
        from trading.runtime.live_management import LivePositionManager
        self.position_manager=LivePositionManager(self)

    def candidate_queue(self):
        return CandidateQueue(self.store)

    def _control(self):
        return dict(self.external_control())

    def _enabled(self,control=None):
        control=self._control() if control is None else control
        return bool(control.get('enabled') and not control.get('stopped') and not Path(self.cfg.kill_switch_path).exists())

    def _risk_engine(self):
        preset=get_preset(self.cfg.risk_profile)
        limit=self.cfg.live_aggressive_daily_loss_limit_pct
        if preset.profile==RiskProfile.AGGRESSIVE_GROWTH:
            preset=replace(preset,
                daily_loss_halt_pct=preset.daily_loss_halt_pct if limit is None else limit,
                weekly_loss_halt_enabled=self.cfg.live_aggressive_weekly_loss_limit_enabled)
        return RiskEngine(settings=self.cfg,preset=preset)

    def configure(self,control):
        """Called only between analysis jobs; policy limits remain deterministic."""
        profile=control.get('risk_profile',getattr(self.cfg.risk_profile,'value',self.cfg.risk_profile))
        if profile not in ('capital_preservation','balanced','aggressive_growth'):
            profile='balanced'
        self.cfg.risk_profile=RiskProfile(profile)
        self.runner_id=control.get('runner_id','baseline-v1')
        self.risk=self._risk_engine()
        self.cfg.analysis_version=control.get('version',self.cfg.analysis_version)
        if self.cfg.analysis_version not in ('text','vlm'):
            raise ValueError('invalid analysis version')
        if self.executor is not None:
            self.executor.risk=self.risk

    def _risk_fraction(self,control,equity=0):
        if runner3_policy.aggressive_model_sizing(self.cfg, self.risk.preset.profile, control.get('runner_id'), equity):
            return runner3_policy.AGGRESSIVE_STOP_RISK_CAP
        requested=control.get('risk_per_trade_pct',self.cfg.risk_per_trade_pct)
        if isinstance(requested,bool) or not isinstance(requested,(int,float)) or not isfinite(requested) or requested<=0:
            raise ValueError('invalid live risk')
        limit = min(requested, self.cfg.risk_per_trade_pct, self.risk.preset.risk_per_trade_pct)
        if control.get('runner_id') == 'runner3':
            limit = min(limit, self.cfg.abs_max_loss_pct, self.risk.preset.abs_max_loss_pct)
        return limit

    def _leverage_cap(self,control,exchange_cap=None):
        if self.cfg.live_leverage_mode=='exchange':
            if isinstance(exchange_cap,bool) or not isinstance(exchange_cap,(int,float)) or not isfinite(exchange_cap) or exchange_cap<1:
                raise ValueError('verified exchange leverage cap required')
            return exchange_cap
        cap=min(getattr(self.cfg,'live_max_leverage',10),
            self.risk.preset.max_leverage_majors,self.risk.preset.max_leverage_alts)
        requested=control.get('max_leverage',cap)
        if isinstance(requested,bool) or not isinstance(requested,(int,float)) or not isfinite(requested) or requested<1:
            raise ValueError('invalid live leverage')
        return min(cap,requested)

    def _symbol_leverage_cap(self,candidate,snapshot):
        rules=self.public.get_contract_rules(candidate.symbol)
        side=1 if candidate.side.value=='long' else 2
        risk=next(row for row in snapshot['risk_limits'][candidate.symbol] if row['side']==side)
        setting=next(row for row in snapshot['leverage'][candidate.symbol] if row['side']==side)
        values=(rules.max_leverage,risk['max_leverage'],setting['max_leverage'])
        if not all(isinstance(v,(int,float)) and not isinstance(v,bool) and isfinite(v) and v>=1 for v in values):
            raise ValueError('invalid account leverage cap')
        return min(values)

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
            levels=intent.get('current_protection') or {}
            stop,target=levels.get('stop',proposal.get('stop_loss',{}).get('price')),levels.get('target',proposal.get('take_profit_price'))
            side_name='long' if row['side']==1 else 'short'
            entry=row.get('entry')
            remaining=None
            if all(isinstance(value,(int,float)) and not isinstance(value,bool) for value in (entry,stop,target)):
                span=abs(entry-stop)
                room=(target-entry) if side_name=='long' else (entry-target)
                remaining=dict(to_target_price=room, to_target_r=(room/span) if span else None)
            thesis=proposal.get('rationale') or (metadata.get('price_plan') or {}).get('reason') or ''
            result[row['symbol']]=dict(row,symbol=row['symbol'],side=side_name,
                entry=entry,leverage=row['leverage'],unrealized=row['unrealized'],
                stop=stop,target=target,thesis=str(thesis)[:500],remaining_room=remaining,
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
        runner3_context={}
        if control.get('runner_id')=='runner3':
            runner3_context['daily_objective']=runner3_policy.daily_objective(closed,
                self.cfg.live_runner3_daily_net_target_usdt,now)
            intents_by_position={str(row['position_id']):row for row in self._intents() if row.get('position_id')}
            risks=[intents_by_position.get(str(row['position_id']),{}).get('planned_loss')
                   for row in snapshot.get('positions',[])]
            runner3_context['open_stop_risk_usdt']=sum(risks) if all(risk is not None for risk in risks) else None
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
            risk_limits=snapshot['risk_limits'],leverage=snapshot['leverage'],**runner3_context)

    def monitor(self):
        if not self._monitor_lock.acquire(blocking=False):
            return self.status('LIVE_MONITOR_BUSY')
        try:
            if self.executor is not None:
                self.executor.reconcile_all()
            self._refresh()
            if self.cfg.live_position_management_enabled:
                for intent in self._intents():
                    try:self.position_manager.observe(intent)
                    except Exception:pass # Quote sampling must not interrupt native reconciliation.
            return self.status('LIVE_RECONCILED')
        except sqlite3.Error:
            raise
        except Exception as exc:
            self.store.put('live_monitor_error',dict(at=time.time(),type=type(exc).__name__))
            return self.status('HOLD_LIVE_RECONCILIATION')
        finally:
            self._monitor_lock.release()

    def manage_positions(self):
        return self.position_manager.tick()

    def management_llm(self):
        if callable(getattr(self.llm,'fork',None)):
            return self.llm.fork()
        # Isolate validators/provider state from concurrent entry reviews.
        from trading.agents.provider_router import ProviderRouter
        if isinstance(self.llm,ProviderRouter):
            return ProviderRouter(self.cfg,self.llm.claude.budget,self.llm.store)
        raise ValueError('request-local management model required')

    def _watch_candidates(self):
        from trading.runtime.live_watch import LiveWatchReviewer
        return LiveWatchReviewer(self).collect()

    def _queued_confirmed_plans(self):
        plans=[]
        for row in self.store.get('candidate_queue', []):
            signal=row.get('signal') or {}
            features=signal.get('features') or {}
            if not features.get('htf_zone_seed') or features.get('local_limit_review'):
                continue
            plans.append(dict(symbol=signal.get('symbol'), side=signal.get('side'),
                setup_id=features.get('setup_id'), timeframe=features.get('seed_timeframe'),
                capital_rank=features.get('capital_rank'), reward_risk=features.get('reward_risk'),
                grade=features.get('evidence_grade'), evidence_kind=features.get('evidence_kind'),
                rationale=signal.get('rationale')))
        return runner3_policy.rank_confirmed_plans(plans)

    def _queue_reached_limits(self):
        from datetime import datetime, timezone
        from trading.evaluation.local_limits import claim_reached
        from trading.models.domain import Side
        from trading.quant.signals import CandidateSignal, StrategyName
        due=claim_reached(self.store, lambda symbol: self.quote(symbol).last_price)
        if not due:
            return
        now=time.time()
        signals=[]
        for row in due:
            price=row['trigger_price']
            long=row['side']=='long'
            signals.append(CandidateSignal(
                symbol=row['symbol'], side=Side(row['side']), strategy=StrategyName.STRUCTURE_SETUP,
                entry_price=price,
                stop_loss_price=price*(0.98 if long else 1.02),
                take_profit_price=price*(1.02 if long else 0.98),
                confidence=1.,
                rationale='Yerel limit fiyatına gelindi. Teyit olmadan emir yok. '+str(row.get('rationale') or ''),
                created_at=datetime.now(timezone.utc),
                features=dict(local_limit_review=True, htf_zone_seed=True, structure_seed=True,
                    seed_timeframe=row.get('trigger_timeframe') or '15m',
                    source_open_at=now-60, source_closed_at=now-1,
                    capital_rank=row.get('cost_adjusted_r') or 0,
                    dedup_key='local-limit:'+row['id'], setup_id=row.get('setup_id'))))
        self.candidate_queue().refresh(signals, refreshed_symbols=set())

    def _rotation_allowed(self, symbol, snapshot, result, control):
        """A market close needs an invalidated thesis and no free margin for the new plan."""
        if not result.metadata.get('thesis_invalidated'):
            return False
        position=self._positions(snapshot).get(symbol)
        if not position:
            return False
        try:
            price=self.quote(symbol).last_price
        except Exception:
            return False
        if not runner3_policy.thesis_invalidated(position, price):
            return False
        setup=result.metadata.get('setup_selection') or {}
        grade='standard'
        if setup.get('evidence'):
            try:
                grade=runner3_policy.evidence_grade(setup)
            except (KeyError, TypeError):
                grade='standard'
        horizon=setup.get('horizon') if setup.get('horizon') in ('scalp', 'intraday', 'swing') else 'intraday'
        try:
            allocation=runner3_policy.margin_policy(snapshot['balance']['equity'], self._capital(snapshot, control),
                snapshot['balance']['available'], grade, horizon)
        except ValueError:
            return False
        return allocation['max_entry_cost_usdt']<=0

    def _release_confirmed(self, symbol):
        """Close one open trade with the existing writer. True only after the exchange confirms."""
        try:
            state=self.request_close(symbol)
        except Exception:
            return False
        if state.get('status')!='closed' and state.get('intent_id') and callable(getattr(self.executor,'reconcile',None)):
            try:
                state=self.executor.reconcile(state['intent_id'])
            except Exception:
                return False
        if state.get('status')!='closed':
            return False
        snapshot=self._refresh()
        return not any(row.get('symbol')==symbol for row in snapshot.get('positions', []))

    def scan_market(self):
        if not self._scan_lock.acquire(blocking=False):
            return None
        try:
            if not self._enabled():
                return None
            runner_id=self._control().get('runner_id')
            runner2=runner_id in STRUCTURAL_IOC_RUNNERS
            scan=MarketScanner(self.public,liquidity=LiquidityFilter(self.cfg.scanner_min_quote_volume_24h,
                self.cfg.scanner_max_spread_bps,require_complete=True),max_symbols=self.cfg.scanner_max_symbols,rotation_store=self.store,bulk_tickers=True,structure_setups_enabled=self.cfg.live_structure_setups_enabled and self.cfg.live_price_plan_enabled, multihorizon=runner2,htf_zone_entries=runner_id=='runner3').scan()
            errors=dict(scan.errors)
            quant_count=len(scan.candidates)
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
                max_age=14400 if candidate.features.get('seed_timeframe')=='4h' else 1200
                if candidate.confidence<.65 or not isinstance(closed,(int,float)) or opened is None or not 0<=time.time()-closed<=max_age:
                    continue
                seed=candidate.features.get('seed_timeframe')
                identity=f'{candidate.symbol}:{candidate.strategy.value}:{candidate.side.value}:{int(float(opened))}:{candidate.features.get("review_event_key","")}'
                if seed:
                    identity+=f':{seed}'
                key=hashlib.sha256(identity.encode()).hexdigest()
                candidate.features['dedup_key']=key
                eligible.append(candidate)
            if not self._enabled():
                return None
            touched=set(self.store.get('universe_rotation',{}).get('selected',[])) if scan.scanned else set()
            touched.update(row['symbol'] for row in watch_snapshot(self.store) if row['status'] in ('watching','triggered'))
            size=self.candidate_queue().refresh(eligible,refreshed_symbols=touched)
            self.store.put('last_scan_at',time.time())
            summary=dict(at=time.time(),scanned=scan.scanned,kept=scan.kept,candidates=len(scan.candidates),
                quant_candidates=quant_count,watch_candidates=len(scan.candidates)-quant_count,eligible=len(eligible),queue_size=size,
                duration_seconds=scan.duration_seconds,filtered_out=scan.filtered_out,errors=errors,
                universe=self.store.get('universe_rotation',{}))
            self.store.put('scan_summary',summary)
            return summary
        finally:
            self._scan_lock.release()

    def _record_review_notice(self, trace):
        key = 'live:review_notices'
        phase = 'result' if trace.get('completed_at') else 'start'
        notice = dict(id=trace['id'], phase=phase, binding_id=self.binding_id, symbol=trace['symbol'],
                      status=trace['status'], action=trace.get('action'), model=trace.get('model'),
                      decision_mode=trace.get('decision_mode'),
                      rationale=' '.join((trace.get('rationale') or '').split())[:180],
                      strategy=trace.get('strategy'), seed_timeframe=trace.get('seed_timeframe'),
                      review_reason=(trace.get('why_reconsidered') or {}).get('reason'),
                      triggered=trace.get('strategy') == 'watch_review',
                      requests=sum(attempt.get('generation_requests', 0) for attempt in trace.get('attempts', [])))
        with self.store.lock:
            pending = self.store.get(key, [])
            self.store.put(key, (pending + [notice])[-200:])

    def _evaluate(self,candidate,snapshot,control):
        child=self.llm.fork() if callable(getattr(self.llm,'fork',None)) else self.llm
        try:
            child.can_call=lambda:self._enabled() and self._control().get('updated_at')==control.get('updated_at')
            evaluator=ContextEvaluator(child,self.store,self.public,self.cfg) if child is not self.llm else self.graph
            evaluator.on_live_review=self._record_review_notice
            capital=self._capital(snapshot,control)
            live_context=self._live_context(snapshot,control)
            live_context['runner_id']=control.get('runner_id','baseline-v1')
            account_equity = snapshot['balance']['equity']
            live_context['aggressive_model_sizing'] = runner3_policy.aggressive_model_sizing(
                self.cfg, self.risk.preset.profile, live_context['runner_id'], account_equity)
            # Other parallel candidates' leverage rows are not this symbol's
            # decision evidence. Portfolio positions/risk remain fully supplied.
            for field in ('risk_limits','leverage'):
                live_context[field]={candidate.symbol:copy.deepcopy(live_context.get(field,{}).get(candidate.symbol,[]))}
            exchange_cap=self._symbol_leverage_cap(candidate,snapshot)
            if self.cfg.live_structure_setups_enabled and self.cfg.live_price_plan_enabled:
                from trading.models.domain import Side
                opposite=candidate.model_copy(update={'side':Side.SHORT if candidate.side==Side.LONG else Side.LONG})
                exchange_cap=min(exchange_cap,self._symbol_leverage_cap(opposite,snapshot))
                candidate=candidate.model_copy(deep=True)
                ticker=self.quote(candidate.symbol)
                candidate.entry_price=ticker.last_price
            live_context['leverage_policy']=dict(mode=self.cfg.live_leverage_mode,account_exchange_max=exchange_cap,
                minimum_stop_liquidation_buffer_fraction=self.cfg.liquidation_buffer_pct,
                fee_rate=self.cfg.paper_fee_rate,slippage_bps=self.cfg.paper_slippage_bps,
                note='Maximum is an upper bound, not a target. Use a validated grounded price plan only when price planning is enabled, otherwise keep quant levels. Preserve loss budget and liquidation buffer; choose lower leverage or HOLD when incompatible.')
            live_positions=self._positions(snapshot)
            if control.get('runner_id')=='runner3':
                briefs=[]
                for row in live_positions.values():
                    brief=runner3_policy.position_brief(row)
                    try:
                        brief['mark']=self.quote(row['symbol']).last_price
                    except Exception:
                        brief['mark']=None
                    briefs.append(brief)
                live_context['open_position_briefs']=briefs
                live_context['queued_confirmed_plans']=self._queued_confirmed_plans()
            return evaluator.evaluate_candidates([candidate],equity=capital,risk_per_trade_pct=self._risk_fraction(control, account_equity),
                target_leverage=self._leverage_cap(control,exchange_cap),risk_profile=self.cfg.risk_profile.value,
                live_account_context=live_context,live_positions=live_positions)
        finally:
            if child is not self.llm and callable(getattr(child,'close',None)):
                child.close()

    def _final_proposal(self,candidate,result,snapshot,control):
        rules=self.public.get_contract_rules(candidate.symbol)
        side=1 if candidate.side.value=='long' else 2
        private_risk=next(row for row in snapshot['risk_limits'][candidate.symbol] if row['side']==side)
        private_leverage=next(row for row in snapshot['leverage'][candidate.symbol] if row['side']==side)
        exchange_cap=min(rules.max_leverage,private_risk['max_leverage'],private_leverage['max_leverage'])
        if self.cfg.live_leverage_mode=='exchange' and result.metadata.get('sizing_plan') is None:
            raise ValueError('model must explicitly choose live leverage and risk')
        ticker=self.quote(candidate.symbol)
        runner_id=control.get('runner_id','baseline-v1')
        planning_candidate=candidate
        if runner_id in STRUCTURAL_IOC_RUNNERS and result.metadata.get('price_plan') is None:
            raise ValueError('Structural IOC runner requires a candle-grounded price plan')
        setup=result.metadata.get('setup_selection')
        if runner_id=='runner3':
            if not setup or not setup.get('context_timeframe'):
                raise ValueError('runner3_requires_verified_setup')
            runner3_policy.validate_evidence(setup,MarketPricePlan.model_validate(result.metadata['price_plan']))
        if result.metadata.get('price_plan') is not None:
            if not self.cfg.live_price_plan_enabled:raise ValueError('live model price planning disabled')
            fresh_quote=ticker.ask1 if candidate.side.value=='long' else ticker.bid1
            drift_cap=.005
            if runner_id=='runner3':
                raw_plan=result.metadata['price_plan']
                planned=raw_plan.entry_price if hasattr(raw_plan,'entry_price') else raw_plan['entry_price']
                planned_stop=raw_plan.stop_loss_price if hasattr(raw_plan,'stop_loss_price') else raw_plan['stop_loss_price']
                drift_cap=abs(planned-planned_stop)/planned
            planning_candidate=apply_market_price_plan(candidate,result.metadata['price_plan'],result.metadata.get('price_evidence') or {},
                price_step=rules.price_step,current_price=fresh_quote,now=time.time(),max_entry_drift=drift_cap)
        account_equity = snapshot['balance']['equity']
        model_sizing = runner3_policy.aggressive_model_sizing(
            self.cfg, self.risk.preset.profile, runner_id, account_equity)
        if model_sizing and result.metadata.get('sizing_plan') is None:
            raise PlanSizingError('aggressive_runner3_requires_explicit_sizing')
        risk_fraction = self._risk_fraction(control, account_equity)
        if model_sizing and setup['family'] == 'countertrend_sweep_reclaim':
            risk_fraction *= .5
        proposal=apply_decision_sizing(planning_candidate,result.metadata.get('sizing_plan'),self._capital(snapshot,control),
            risk_fraction,self._leverage_cap(control,exchange_cap),exchange_cap)
        if runner_id!='runner3':
            drift_limit = min(planning_candidate.stop_distance_pct / 2, .005)
            if abs(ticker.last_price / planning_candidate.entry_price - 1) > drift_limit:
                raise EntryPriceDrift('live entry price drift', stage='planning', planned=planning_candidate.entry_price,
                                      observed=ticker.last_price, tolerance=drift_limit)
        entry=ticker.ask1 if candidate.side.value=='long' else ticker.bid1
        slip=self.cfg.paper_slippage_bps/10000;fee=self.cfg.paper_fee_rate
        planned_entry=planning_candidate.entry_price
        if runner_id == 'runner3':
            fee = getattr(self.executor, 'fee', fee)
            slip = getattr(self.executor, 'slip', slip)
            gross_target, expected_net, unit_loss = runner3_policy.ioc_target_economics(
                side=candidate.side.value, entry=entry, stop=proposal.stop_loss.price,
                target=proposal.take_profit_price, fee=fee, slippage=slip)
            _, _, planned_unit = runner3_policy.ioc_target_economics(
                side=candidate.side.value, entry=planned_entry, stop=proposal.stop_loss.price,
                target=proposal.take_profit_price, fee=fee, slippage=slip)
            loss_fraction = planned_unit / planned_entry
        else:
            worst_entry=entry*(1+slip if candidate.side.value=='long' else 1-slip)
            stop=proposal.stop_loss.price*(1-slip if candidate.side.value=='long' else 1+slip)
            loss_fraction=(abs(worst_entry-stop)+fee*(worst_entry+stop))/entry
        if runner_id != 'runner3' and result.metadata.get('price_plan') is not None:
            worst_target=proposal.take_profit_price*(1-slip if candidate.side.value=='long' else 1+slip)
            gross_target=abs(proposal.take_profit_price-entry)
            expected_net=(worst_target-worst_entry if candidate.side.value=='long' else worst_entry-worst_target)-fee*(worst_entry+worst_target)
            if expected_net<=0:raise ValueError('model target does not cover expected trading costs')
        margin_rate=max(1/proposal.leverage,private_risk['initial_rate'])+fee
        risk_adjustment = {}
        if runner_id == 'runner3':
            grade = runner3_policy.evidence_grade(setup)
            model_fraction = proposal.metadata['risk_per_trade_pct']
            ceiling = risk_fraction
            if not model_sizing and setup['family'] == 'countertrend_sweep_reclaim':
                ceiling *= .5
            target_risk = runner3_policy.small_account_risk_target(snapshot['balance']['equity'], ceiling, grade)
            effective = model_fraction
            if not model_sizing and target_risk is not None and not any(row['side'] == side for row in snapshot['positions']):
                effective = max(model_fraction, target_risk)
            if effective > model_fraction:
                multiplier = effective / model_fraction
                proposal.max_loss_if_stopped *= multiplier
                proposal.notional *= multiplier
                proposal.metadata['risk_per_trade_pct'] = effective
            risk_adjustment = dict(macro_trend=setup['macro_trend'], model_risk_fraction=model_fraction,
                effective_risk_fraction=effective, risk_ceiling=ceiling,
                risk_boost_applied=effective > model_fraction, small_account_policy=target_risk is not None)
        limits=dict(model_stop_budget=proposal.notional,stop_budget_with_costs=proposal.max_loss_if_stopped/loss_fraction)
        if runner_id!='runner3':
            limits['available_margin']=snapshot['balance']['available']/margin_rate*.95
        if runner_id=='runner3':
            runner3_policy.validate_economics(gross_target,expected_net,unit_loss)
            balance = snapshot['balance']
            available = balance['available']
            if model_sizing and balance.get('available_open') is not None:
                available = min(available, balance['available_open'])
            allocation=runner3_policy.apply_guclu_margin_share(
                runner3_policy.margin_policy(snapshot['balance']['equity'],self._capital(snapshot,control),
                    available,grade,setup['horizon'],model_sizing=model_sizing),
                equity=snapshot['balance']['equity'], available=available,
                raw_share=runner3_policy.chosen_margin_share(result.metadata), allow_standard=model_sizing)
            free_margin=available-allocation['reserve_usdt']
            if free_margin<=0:
                raise ValueError('insufficient_available_margin' if model_sizing else 'runner3_margin_reserve')
        _,notional=rules.size_order(min(limits.values()),planned_entry if runner_id=='runner3' else entry,1 if runner_id=='runner3' else proposal.leverage)
        proposal.notional=notional
        if runner_id=='runner3':
            quantity=notional/planned_entry
            plan=result.metadata['price_plan']
            reference=plan.stop_reference if hasattr(plan,'stop_reference') else plan['stop_reference']
            extreme=runner3_policy.confirming_extreme(reference,result.metadata.get('price_evidence') or {})
            if not runner3_policy.drift_respects_choch_risk(
                    side=candidate.side.value,fresh_entry=entry,stop=proposal.stop_loss.price,
                    target=proposal.take_profit_price,choch_extreme=extreme,quantity=quantity,
                    risk_budget=proposal.max_loss_if_stopped,fee=fee,slippage=slip):
                raise EntryPriceDrift('live entry price drift',stage='planning',planned=planned_entry,
                                      observed=entry,tolerance=abs(entry/planned_entry-1))
            proposal.notional=quantity*entry
            proposal.entry_price=entry
            requested_notional=proposal.notional
            model_leverage = (proposal.metadata.get('sizing_plan') or {}).get('leverage')
            leverage,margin,sized=runner3_policy.leverage_for_choch_loss(
                notional=proposal.notional,entry=entry,stop=proposal.stop_loss.price,free_margin=free_margin,
                margin_target=allocation['target_margin_usdt'],
                equity=snapshot['balance']['equity'],max_leverage=min(self._leverage_cap(control,exchange_cap),exchange_cap),
                maintenance=max(rules.maintenance,private_risk['maintenance_rate'],private_leverage['maintenance_rate']),
                initial_rate=private_risk['initial_rate'],fee=fee,liquidation_buffer=self.cfg.liquidation_buffer_pct,
                preferred_leverage=model_leverage if model_sizing else None, model_sizing=model_sizing)
            if sized<proposal.notional-1e-8:
                _,proposal.notional=rules.size_order(sized,entry,leverage)
            proposal.leverage=leverage
            if sized<requested_notional-1e-6:
                limiting_factor=('runner3_margin_policy' if allocation['target_margin_usdt']<=free_margin+1e-8
                                 else 'available_margin')
            else:
                limiting_factor=min(limits,key=limits.get)
            proposal.metadata['runner3_allocation']=dict(allocation,limiting_factor=limiting_factor,
                free_margin_usdt=free_margin,initial_margin_usdt=margin,chosen_leverage=leverage,
                aggressive_model_sizing=model_sizing,model_leverage=model_leverage,
                setup_id=setup['setup_id'],evidence_expires_at=setup['evidence'].get('expires_at'),
                calibrated_probability=None,expected_target_net_r=expected_net/unit_loss,**risk_adjustment)
        proposal.rationale=result.metadata.get('rationale',candidate.rationale)
        proposal.metadata.update(price_plan=result.metadata.get('price_plan'),price_source='model' if result.metadata.get('price_plan') else 'quant',
            runner_id=runner_id,
            setup_family=(result.metadata.get('setup_selection') or {}).get('family',candidate.features.get('setup_family')),
            setup_id=(result.metadata.get('setup_selection') or {}).get('setup_id'),
            trade_horizon=result.metadata.get('trade_horizon'),
            strategy_cohort=result.metadata.get('strategy_cohort'),
            analysis_trace_id=result.metadata.get('analysis_trace_id'),
            contract_size=rules.size,analysis_version=self.cfg.analysis_version,charts=result.metadata.get('charts',[]),
            decision_ai=result.metadata.get('decision_ai',{}),account_generation=snapshot['generation'])
        if runner_id == 'runner3':
            proposal.metadata['htf_setup'] = {key: copy.deepcopy(setup[key]) for key in
                ('context_timeframe','trigger_timeframe','evidence')}
        return proposal

    def _record_entry_rejection(self, candidate, trace_id, reason, details=None, *, notify=True):
        if not finalize_trace(self.store, trace_id, 'rejected', reason, details) or not notify:
            return
        with self.store.lock:
            pending = self.store.get('live:entry_rejection_notices', [])
            pending.append(dict(at=time.time(), binding_id=self.binding_id,
                                symbol=candidate.symbol, reason=reason, details=details or {}))
            self.store.put('live:entry_rejection_notices', pending[-100:])

    def _apply(self,candidate,result,control):
        trace_id=result.metadata.get('analysis_trace_id')
        def finish(reason, *, notify=True):
            if trace_id and result.action.value == 'trade':
                if reason == 'LIVE_PROTECTED':
                    finalize_trace(self.store, trace_id, 'opened', reason)
                else:
                    self._record_entry_rejection(candidate, trace_id, reason, notify=notify)
            return self.status(reason)
        if result.action.value!='trade':
            return finish('HOLD_LIVE_MODEL')
        if (result.selected_signal is None or result.selected_signal.signal_id!=candidate.signal_id
                or not all(note and note.mode=='llm' for note in (result.market,result.strategy,result.portfolio))):
            return finish('HOLD_LIVE_UNVALIDATED_MODEL')
        current_control=self._control()
        if (not self._enabled(current_control) or current_control.get('updated_at')!=control.get('updated_at')
                or current_control.get('runner_id','baseline-v1')!=control.get('runner_id','baseline-v1')):
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
        position_limit = min(control.get('max_positions', self.cfg.live_max_positions),
                             self.cfg.live_max_positions, self.risk.preset.max_concurrent_positions)
        if (runner3_policy.uses_fixed_position_limit(control.get('runner_id'), snapshot['balance']['equity'])
                and len(snapshot['positions']) >= position_limit):
            return finish('HOLD_LIVE_POSITION_LIMIT')
        selected=result.selected_signal
        if selected.symbol!=candidate.symbol:
            return finish('HOLD_LIVE_UNVALIDATED_MODEL')
        if selected.side!=candidate.side and not result.metadata.get('setup_selection'):
            return finish('HOLD_LIVE_UNVALIDATED_MODEL')
        proposal=self._final_proposal(selected,result,snapshot,control)
        release=result.metadata.get('release_symbol')
        if release and not self._rotation_allowed(release, snapshot, result, control):
            release=None
        if release:
            if not self._release_confirmed(release):
                return finish('HOLD_LIVE_CLOSE_UNCONFIRMED')
            snapshot=self._refresh([candidate.symbol])
            if snapshot['balance']['available']<=0:
                return finish('HOLD_LIVE_NO_FUNDS')
            proposal=self._final_proposal(selected,result,snapshot,control)
        # Execution performs its own fresh account/risk/quote and grant checks.
        self.executor.allowed_capital = (
            None if control.get('allocation_mode') in ('full','all','entire')
            else self._capital(snapshot,control)
        )
        intent=self.executor.authorize(proposal,control_revision=control.get('updated_at'))
        observed=next(row for row in snapshot['leverage'][candidate.symbol] if row['side']==(1 if selected.side.value=='long' else 2))
        if observed['leverage']!=proposal.leverage:
            if not self._enabled() or self._control().get('updated_at')!=control.get('updated_at'):
                return finish('HOLD_LIVE_CONTROL_CHANGED')
            self.private.change_leverage(symbol=candidate.symbol,side=selected.side.value,leverage=int(proposal.leverage),
                request_id='lev-'+intent['intent_id'][:24])
            self._refresh([candidate.symbol]) # Never assume the leverage write applied.
        # Revision/control may have changed during HTTP. Executor calls this guard
        # again at its final entry gate, after its own preflight network reads.
        original=getattr(self.executor,'can_execute',None)
        def current():
            latest=coordinator.card(candidate.symbol)
            current_control=self._control()
            return bool((original is None or original()) and self._enabled(current_control)
                and current_control.get('updated_at')==control.get('updated_at')
                and current_control.get('runner_id','baseline-v1')==control.get('runner_id','baseline-v1')
                and self.candidate_queue().is_current(candidate)
                and latest['revision']==card['revision'] and latest.get('last_action')=='trade'
                and time.time()-started<=self.cfg.decision_ttl_seconds)
        self.executor.can_execute=current
        pending=result.metadata.get('pending_vlm')
        if pending is not None:
            pending.arm_send()
        try:
            state=self.executor.execute(intent['intent_id'])
        finally:
            self.executor.can_execute=original
        if state.get('status')=='protected':
            from trading.evaluation.local_limits import consume_symbol
            consume_symbol(self.store, candidate.symbol)
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
        return finish('HOLD_LIVE_'+str(state.get('status','UNKNOWN')).upper(), notify=False)

    def _capacity_probe(self, images, prompt_bytes, ceiling):
        probe=getattr(self.llm,'probe_free_capacity',None)
        if not callable(probe):
            return False
        now=time.time()
        queued=self.store.get('candidate_queue',[]) or []
        if not any((row.get('expires_at') or 0)>now for row in queued):
            return False
        state=self.store.get('live_capacity_probe') or {}
        attempts=int(state.get('attempts') or 0)
        if attempts>=self.capacity_probe_cap or now-float(state.get('last_at') or 0)<self.capacity_probe_gap_seconds:
            return False
        try:
            try:
                outcome=probe(images=images, prompt_bytes=prompt_bytes)
            except TypeError:
                outcome=probe(images=images)
        except sqlite3.Error:
            raise
        except Exception:
            outcome=None
        if isinstance(outcome, dict) and outcome.get('does_not_fit'):
            # An oversized 413 is not one of the three failed probes of a fitting model.
            if outcome.get('probed'):
                state=self.store.get('live_capacity_probe') or {}
                self.store.put('live_capacity_probe', dict(
                    attempts=int(state.get('attempts') or 0), last_at=now, admitted=None))
            return False
        if isinstance(outcome, dict) and outcome.get('skipped'):
            return False
        admitted=bool(isinstance(outcome, dict) and outcome.get('admitted'))
        self.store.put('live_capacity_probe', dict(
            attempts=0 if admitted else attempts+1, last_at=now,
            admitted=outcome.get('admitted') if admitted else None))
        if admitted and callable(getattr(self.llm,'analysis_capacity',None)):
            capacity=self.llm.analysis_capacity(prompt_bytes=prompt_bytes,
                max_tokens=1100 if self.cfg.live_price_plan_enabled else 700,
                images=images, ceiling=ceiling)
            self.store.put('analysis_capacity', dict(capacity, probe_admitted=True))
        return admitted

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
            equity=snapshot['balance']['equity']
            available=snapshot['balance']['available']
            # Margin already in positions is not an empty book. Runner 3 can ask
            # which open trade to close. A resting local limit never reserves cash.
            if equity<=0 or (available<=0 and not (control.get('runner_id')=='runner3' and snapshot.get('positions'))):
                return self.status('HOLD_LIVE_NO_FUNDS')
            risk=observe_risk_reference(self.store,self.binding_id,snapshot['balance']['equity'])
            preset = self.risk.preset
            limits = {'daily_loss_pct': preset.daily_loss_halt_pct}
            if preset.weekly_loss_halt_enabled:
                limits['weekly_loss_pct'] = preset.weekly_loss_halt_pct
            risk_blocks = {key: dict(observed=risk[key], limit=limit) for key, limit in limits.items() if risk[key] >= limit}
            if risk_blocks:
                return self.status('HOLD_LIVE_LOSS_CIRCUIT_BREAKER', risk_blocks=risk_blocks)
            if not self.external_scan:
                self.scan_market()
            htf_zone_entries = control.get('runner_id') == 'runner3'
            waiting = set() if htf_zone_entries else {
                row['symbol'] for row in watch_snapshot(self.store) if row['status']=='watching'}

            ceiling=min(8,getattr(self.cfg,'ai_parallel_candidates',1)) if callable(getattr(self.llm,'fork',None)) else 1
            count=ceiling
            if self.cfg.ai_dynamic_parallel_enabled and callable(getattr(self.llm,'analysis_capacity',None)):
                history=self.store.get('analysis_history',[])[-10:]
                prompt_bytes=max([18000]+[row.get('prompt_bytes',0) for row in history])
                images=control.get('version',self.cfg.analysis_version)=='vlm'
                capacity=self.llm.analysis_capacity(prompt_bytes=prompt_bytes,
                    max_tokens=1100 if self.cfg.live_price_plan_enabled else 700,
                    images=images,ceiling=ceiling)
                self.store.put('analysis_capacity',capacity)
                count=capacity['parallel_candidates']
                if count<=0 and self._capacity_probe(images, prompt_bytes, ceiling):
                    count=1
                elif count<=0:
                    return self.status('HOLD_LIVE_AI_CAPACITY')
                elif self.store.get('live_capacity_probe'):
                    self.store.put('live_capacity_probe',{})
            if control.get('runner_id')=='runner3':
                self._queue_reached_limits()
            queue=self.candidate_queue()
            for _ in range(count):
                candidate=queue.next(open_symbols=set(self._positions(snapshot))|waiting|{c.symbol for c in selected},
                    seen=self.store.get('live_seen',[]),lease_seconds=self.cfg.decision_ttl_seconds+60,
                    htf_zone_entries=htf_zone_entries)
                if candidate is None:
                    break
                selected.append(candidate)
            if not selected:
                return self.status('HOLD_LIVE_NO_CANDIDATE')
            snapshot=self._refresh([candidate.symbol for candidate in selected])
            outcomes=[]
            finished=[]
            with ThreadPoolExecutor(max_workers=len(selected),thread_name_prefix='live-analysis') as pool:
                jobs={pool.submit(self._evaluate,candidate,snapshot,control):candidate for candidate in selected}
                for future in as_completed(jobs):
                    candidate=jobs[future]
                    try:
                        finished.append((candidate, future.result(), None))
                    except sqlite3.Error:
                        raise
                    except Exception as exc:
                        finished.append((candidate, None, exc))
            if control.get('runner_id')=='runner3':
                finished.sort(key=lambda item: (
                    -float(item[0].features.get('capital_rank') or 0),
                    item[0].symbol))
            for candidate, result, exc in finished:
                if exc is not None:
                    from trading.runtime.execution_diagnostics import safe_execution_error
                    details=safe_execution_error(exc)
                    reason='HOLD_LIVE_EXECUTION_'+details['code']
                    self.store.put('live_candidate_error:'+candidate.symbol,dict(at=time.time(),**details))
                    outcomes.append(self.status(reason))
                    continue
                try:
                    if result.metadata.get('provider_called') is not False:
                        queue.evaluated(candidate)
                    if getattr(result, 'hold_reason', None) == 'decision_expired' and result.metadata.get('analysis_trace_id'):
                        self._record_entry_rejection(candidate, result.metadata['analysis_trace_id'],
                                                     'HOLD_LIVE_DECISION_EXPIRED')
                    outcomes.append(self._apply(candidate,result,control))
                except sqlite3.Error:
                    raise
                except Exception as exc:
                    from trading.runtime.execution_diagnostics import safe_execution_error
                    details=safe_execution_error(exc)
                    reason='HOLD_LIVE_EXECUTION_'+details['code']
                    self.store.put('live_candidate_error:'+candidate.symbol,dict(at=time.time(),**details))
                    if result is not None and result.action.value=='trade':
                        self._record_entry_rejection(candidate,result.metadata.get('analysis_trace_id'),reason,details)
                    outcomes.append(self.status(reason))
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

    def status(self,reason,*,risk_blocks=None):
        if reason.startswith(('HOLD_LIVE','LIVE_PROTECTED','LIVE_PAUSED')) and reason!='HOLD_LIVE_RECONCILIATION':
            prior=self.store.get('live_entry_status',{})
            if prior.get('reason')!=reason or risk_blocks is not None:
                status = dict(reason=reason, at=time.time())
                if risk_blocks:
                    status['risk_blocks'] = risk_blocks
                self.store.put('live_entry_status',status)
        account=self.store.get(self.reconciler.key,{})
        intents=self._intents()
        positions={}
        # Keep hedge legs individually visible, even though entry analysis rejects
        # multiple exposures for one symbol until explicitly reconciled.
        for row in account.get('positions',[]):
            intent=next((value for value in intents if str(value.get('position_id'))==str(row['position_id'])),{})
            proposal=intent.get('proposal',{})
            metadata=proposal.get('metadata',{})
            levels=intent.get('current_protection') or {}
            stop,target=levels.get('stop',proposal.get('stop_loss',{}).get('price')),levels.get('target',proposal.get('take_profit_price'))
            key=row['symbol'] if row['symbol'] not in positions else row['symbol']+':'+str(row['position_id'])
            positions[key]=dict(row,symbol=row['symbol'],side='long' if row['side']==1 else 'short',
                stop=stop,target=target,management_revision=intent.get('management_revision',0),
                remaining_contracts=intent.get('managed_contracts',intent.get('effective_contracts',intent.get('contracts'))),
                opened_at=row.get('opened_at'),intent_id=intent.get('intent_id'),
                protection_status=intent.get('status','unmanaged'),analysis_version=metadata.get('analysis_version'),
                decision_ai=metadata.get('decision_ai',{}),price_source=metadata.get('price_source','quant'),
                runner_id=metadata.get('runner_id','baseline-v1'))
        closed=[row for row in intents if row.get('status')=='closed']
        public_data=self.public.rate_status() if callable(getattr(self.public,'rate_status',None)) else {}
        snapshot=dict(stage=self.store.get('stage'),public_data=public_data,mode='LIVE',live_orders_enabled=bool(self.executor is not None),observed_at=time.time(),reason=reason,
            account=dict(ready=account.get('ready',False),**account.get('balance',{})),
            live_account=account,real_account=account,balance=account.get('balance'),positions=positions,closed=closed,intents=intents,
            entry_status=self.store.get('live_entry_status',{}),analysis_capacity=self.store.get('analysis_capacity',{}),scan_summary=self.store.get('scan_summary',{}),
            management_status=self.store.get('management_status',{}),management_trace=self.store.get('management_trace',{}),
            analysis_trace=self.store.get('analysis_trace',{}),analysis_history=self.store.get('analysis_history',[]),
            watchlist=watch_snapshot(self.store),leverage_mode=self.cfg.live_leverage_mode,analysis_version=self.cfg.analysis_version,
            htf_zone_scan=self.store.get('htf_zone_scan', {}) if self.runner_id == 'runner3' else {},
            runner_id=self.runner_id,
            aggressive_model_sizing=runner3_policy.aggressive_model_sizing(
                self.cfg, self.risk.preset.profile, self.runner_id, (account.get('balance') or {}).get('equity')),
            entry_risk_policy=dict(profile=self.risk.preset.profile.value,daily_loss_limit_pct=self.risk.preset.daily_loss_halt_pct,
                weekly_loss_limit_enabled=self.risk.preset.weekly_loss_halt_enabled,
                weekly_loss_limit_pct=self.risk.preset.weekly_loss_halt_pct),
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
