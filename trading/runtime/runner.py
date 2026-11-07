"""Paper worker orchestration. Private access is read-only; HOLD is normal."""
import hashlib
import logging
import math
from pathlib import Path
import time
import sqlite3
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed

from trading.agents.anthropic_client import AnthropicClient
from trading.agents.graph import EvaluationGraph
from trading.evaluation.planning import apply_decision_sizing, PlanSizingError
from trading.agents.schemas import GraphAction
from trading.config import BrokerMode
from trading.exchange.mexc_public import MexcPublicClient
from trading.exchange.mexc_readonly import MexcReadOnlyClient
from trading.quant.scanner import MarketScanner, LiquidityFilter
from trading.risk.engine import RiskEngine, RiskContext
from trading.runtime.budget import Budget
from trading.runtime.paper import PaperLedger
from trading.runtime.store import RuntimeStore
from trading.runtime.candidates import CandidateQueue
from trading.runtime.telegram import Telegram

logger = logging.getLogger(__name__)


class PaperRunner:
    def __init__(self, settings, store=None, public=None, account_client=None, llm=None):
        self.cfg = settings
        if settings.live_trading_enabled or settings.broker_mode != BrokerMode.PAPER:
            raise ValueError('worker requires PAPER and LIVE_TRADING_ENABLED=false')
        if settings.equity_reset:
            raise ValueError('persistent worker refuses automatic equity resets')
        self.store = store or RuntimeStore(settings.runtime_db_path)
        self.lock = self.store.lock
        self.protection = None
        self.external_control = None
        self.external_scan = False
        self.external_scan_control = None
        self.public = public or MexcPublicClient(base_url=settings.mexc_base_url)
        self.account_client = account_client or MexcReadOnlyClient(api_key=settings.mexc_api_key,
            api_secret=settings.mexc_api_secret, base_url=settings.mexc_base_url,
            recv_window_ms=settings.mexc_recv_window_ms)
        self.llm = llm or AnthropicClient(settings=settings, budget=Budget(self.store, settings))
        self.graph = EvaluationGraph(llm=self.llm)
        self.risk = RiskEngine(settings=settings)
        self.ledger = None
        if self.store.get('paper') is not None:
            self.ledger = PaperLedger(self.store, 0, settings.paper_fee_rate, settings.paper_slippage_bps)
        self.telegram = Telegram(settings, self.store)

    def quote(self, symbol, client=None):
        ticker = (client or self.public).get_ticker(symbol)
        timestamp = float(ticker.raw['timestamp']) / 1000
        age = time.time()-timestamp
        values = (ticker.bid1, ticker.ask1, ticker.last_price, ticker.quote_volume_24h, ticker.raw.get('fairPrice'))
        if not all(v is not None and math.isfinite(float(v)) and float(v)>0 for v in values):
            raise ValueError('market data incomplete')
        if age < -5 or age > self.cfg.market_max_age_seconds or ticker.ask1 < ticker.bid1:
            raise ValueError('market data stale or crossed')
        return ticker

    def account(self):
        if not self.cfg.require_mexc_balance:
            return None
        if not self.cfg.mexc_api_key_configured:
            raise ValueError('MEXC read-only credentials missing')
        account = self.account_client.get_futures_account()
        positions = self.account_client.get_open_positions()
        snapshot = account.public()
        snapshot['open_positions'] = positions
        self.store.put('real_account', snapshot)
        return account

    def monitor(self):
        if self.ledger is None:
            return
        errors = []
        for symbol in list(self.ledger.positions):
            try:
                ticker = self.quote(symbol)
                p = self.ledger.positions[symbol]
                # Replay missed settled funding exactly once per position. Price is an
                # observation approximation, explicitly not a historical settlement mark.
                try:
                    history = self.public.get_funding_history(symbol)
                    if history and min(int(r['settleTime']) for r in history) > p['opened_at']:
                        raise ValueError('funding history does not cover position lifetime')
                    for row in sorted(history, key=lambda r: r['settleTime']):
                        timestamp = int(row['settleTime'])
                        if p['opened_at'] < timestamp <= time.time()*1000:
                            self.ledger.settle_funding(symbol, timestamp, float(row['fundingRate']), float(ticker.raw['fairPrice']))
                except sqlite3.Error:
                    raise
                except Exception as exc:
                    errors.append(f'{symbol}:funding:{type(exc).__name__}')
                self.ledger.mark(symbol, ticker.bid1, ticker.ask1, float(ticker.raw['fairPrice']))
            except sqlite3.Error:
                raise
            except Exception as exc:
                # Continue protecting other symbols even if one market is unavailable.
                errors.append(f'{symbol}:{type(exc).__name__}')
        if errors:
            raise ValueError('position monitoring incomplete: '+','.join(errors))
        self.ledger.roll_day()

    def status(self, reason, trace_id=None, finalize_latest=True):
        if trace_id:
            from trading.evaluation.service import finalize_trace
            finalize_trace(self.store,trace_id,'opened' if reason=='PAPER_OPENED' else 'rejected',reason)
        trace=self.store.get('analysis_trace',{})
        if not trace_id and finalize_latest and trace.get('execution_status')=='awaiting_risk':
            from trading.runtime.usage import archive_trace
            trace.update(execution_status='opened' if reason=='PAPER_OPENED' else 'rejected',execution_reason=reason)
            self.store.put('analysis_trace',trace)
            history=self.store.get('analysis_history',[])
            for row in history:
                if row['id']==trace['id']:
                    row.update(trace)
            self.store.put('analysis_history',history)
            archive_trace(self.store,trace)
        with self.lock:
            paper_snapshot = self.ledger.metrics() if self.ledger else None
            positions_snapshot = copy.deepcopy(self.ledger.positions) if self.ledger else {}
        snapshot = dict(mode='PAPER', live_orders_enabled=False, observed_at=time.time(), reason=reason,
            real_account=self.store.get('real_account'), paper=paper_snapshot,
            positions=positions_snapshot,
            ai_status=self.llm.last_status, ai_provider=getattr(self.llm,'last_provider','anthropic'), ai_model=getattr(self.llm,'last_model',''), ai_fallback=getattr(self.llm,'last_fallback_reason',''), ai_budget=self.store.get('ai_budget', {}))
        self.store.put('status', snapshot)
        if self.protection:
            snapshot['protection'] = self.protection.snapshot()
        if self.ledger and self.external_control is None:
            self.telegram.tick(self.ledger, self.status_text(snapshot))
        return snapshot

    @staticmethod
    def status_text(s):
        from trading.agents.telemetry import human_reason,trace_outcome
        paper=s.get('paper') or {}
        count=paper.get('open_positions',0)
        text=f"Sanal işlem durumu\nAçık işlem: {count}\n"
        if not count:
            text+='Şu anda açık işlem yok.\n'
        if paper:
            text+=(f"Güncel sanal kasa: {paper['equity']:.4f} USDT\n"
                   f"Açık işlemlerin kâr/zararı: {paper.get('unrealized',0):+.4f} USDT\n"
                   f"Kapanan işlemlerin net sonucu: {paper.get('realized',0):+.4f} USDT\n")
        text+=f"Bot ne yapıyor? {human_reason(s.get('reason',''))}\n"
        trace=s.get('analysis_trace') or {}
        if trace:
            text+=f"Son aday: {trace.get('symbol','?')} — {trace_outcome(trace)}\n"
        budget=s.get('ai_budget',{})
        pending=sum(row.get('amount',0) for row in budget.get('reservations',{}).values())
        text+=(f"Claude kullanım tahmini: ${max(0,budget.get('total',0)-pending):.4f}\n"
               f"Sonucu bilinmeyen eski/yeni istek rezervasyonu: ${pending:.4f} (kesilmiş ücret değildir).\n"
               'İşlemleri görmek için /positions, teknik ayrıntı için /diagnostics.\n'
               'Bu bir durum mesajı; yeni AI isteği veya işlem açılışı bildirimi değil. Gerçek emir yok.')
        return text

    def cycle(self):
        # Monitoring is independent of account/LLM/entry failures and kill switch.
        try:
            if self.protection is None:
                self.monitor()
            elif self.ledger:
                with self.lock:
                    self.ledger.roll_day()
        except sqlite3.Error:
            raise
        except Exception as exc:
            return self.status(f'HOLD_MONITOR_{type(exc).__name__}')
        try:
            account = self.account()
        except sqlite3.Error:
            raise
        except Exception:
            return self.status('HOLD_ACCOUNT_UNAVAILABLE')
        if self.ledger is None:
            seed = account.available if account else self.cfg.paper_seed_usdt
            if account and self.cfg.paper_allocation_usdt is not None:
                if self.cfg.paper_allocation_usdt > account.available:
                    return self.status('HOLD_ALLOCATION_EXCEEDS_AVAILABLE')
                seed = self.cfg.paper_allocation_usdt
            # Wait for a positive observed deposit; never fall back to 700 TRY.
            if seed <= 0:
                return self.status('HOLD_WAITING_FOR_FUNDS')
            self.ledger = PaperLedger(self.store, seed, self.cfg.paper_fee_rate, self.cfg.paper_slippage_bps)
        try:
            return self._entries(account)
        except sqlite3.Error:
            raise
        except Exception as exc:
            logger.warning('paper entry held (%s)', type(exc).__name__)
            return self.status(f'HOLD_ENTRY_{type(exc).__name__}')

    def candidate_queue(self):
        return CandidateQueue(self.store,getattr(self.cfg,'candidate_ttl_seconds',900),getattr(self.cfg,'candidate_queue_limit',80))

    def scan_market(self):
        """Refresh durable candidates without account/model calls or ledger locks.

        Fleet calls this on a separate executor. A dedicated public client avoids
        sharing the analysis client's network operations; fixture clients stay local.
        """
        scan_control=self.external_scan_control or self.external_control
        if scan_control and not scan_control().get('enabled'):
            return None
        if not self.cfg.always_on_scan or Path(self.cfg.kill_switch_path).exists():
            return None
        started=time.monotonic()
        observed=time.time()
        self.store.put('last_scan_at',observed)
        self.store.put('scan_stage','scanning_market')
        client=self.public
        owned=type(client) is MexcPublicClient and not client.use_fixtures and client.transport is None
        if owned:
            client=MexcPublicClient(base_url=self.cfg.mexc_base_url,timeout_seconds=3)
        try:
            scanner=MarketScanner(client,liquidity=LiquidityFilter(self.cfg.scanner_min_quote_volume_24h,self.cfg.scanner_max_spread_bps),max_symbols=self.cfg.scanner_max_symbols)
            scan=scanner.scan()
            with self.lock:
                positions=set(self.ledger.positions) if self.ledger else set()
                seen=set(self.ledger.state['seen']) if self.ledger else set()
            eligible=[]
            errors=dict(getattr(scan,'errors',{}))
            for candidate in scan.candidates:
                try:
                    if candidate.symbol in positions or not candidate.symbol.endswith('_USDT') or candidate.confidence<0.65:
                        continue
                    ticker=self.quote(candidate.symbol,client)
                    if ticker.quote_volume_24h<self.cfg.scanner_min_quote_volume_24h or ticker.spread_bps>self.cfg.scanner_max_spread_bps:
                        continue
                    closed_at=candidate.features.get('source_closed_at')
                    source_open=candidate.features.get('source_open_at')
                    if closed_at is None:
                        rows=client.get_klines(candidate.symbol,interval='Min15',limit=60)['candles']
                        closed=[c for c in rows if float(c[0])+900<=time.time()]
                        if not closed:
                            continue
                        source_open=closed[-1][0]
                        closed_at=float(source_open)+900
                    if closed_at>time.time() or time.time()-closed_at>1200:
                        continue
                    if source_open is None:
                        source_open=int(closed_at-900) if float(closed_at-900).is_integer() else closed_at-900
                    prefix=f'{candidate.symbol}:{candidate.strategy.value}:{candidate.side.value}:'
                    key=hashlib.sha256(f'{prefix}{source_open}'.encode()).hexdigest()
                    legacy_keys={hashlib.sha256(f'{prefix}{stamp}'.encode()).hexdigest() for stamp in (source_open,float(source_open),int(float(source_open)))}
                    if legacy_keys & seen:
                        continue
                    candidate.features.update(dedup_key=key,source_open_at=source_open,source_closed_at=closed_at)
                    eligible.append(candidate)
                except sqlite3.Error:
                    raise
                except Exception as exc:
                    errors[candidate.symbol]=type(exc).__name__
            if scan_control and not scan_control().get('enabled'):
                self.store.put('scan_stage','scan_discarded_after_pause')
                return None
            size=self.candidate_queue().refresh(eligible)
            summary=dict(at=time.time(),scanned=scan.scanned,kept=scan.kept,candidates=len(scan.candidates),eligible=len(eligible),queue_size=size,errors=errors,duration_seconds=time.monotonic()-started)
            self.store.put('scan_summary',summary)
            self.store.put('scan_stage','scan_completed')
            return summary
        except sqlite3.Error:
            raise
        except Exception as exc:
            self.store.put('scan_stage','scan_failed')
            self.store.put('scan_error',dict(at=time.time(),type=type(exc).__name__))
            return None
        finally:
            if owned:
                client.close()

    def _entries(self, account):
        ledger = self.ledger
        if getattr(self,'management_fatal',False):
            return self.status('HOLD_MANAGEMENT_PERSISTENCE_FAILURE')
        evaluation_control = self.external_control() if self.external_control else None
        if evaluation_control and not evaluation_control.get('enabled'):
            return self.status('HOLD_TELEGRAM_PAUSED')
        if self.protection:
            self.protection.assert_ready()
        with self.lock:
            metrics = ledger.metrics()
            risk_positions = copy.deepcopy(ledger.positions)
            analysis_equity = ledger.equity
        if not self.cfg.always_on_scan:
            return self.status('HOLD_SCANNING_DISABLED')
        if Path(self.cfg.kill_switch_path).exists():
            return self.status('HOLD_KILL_SWITCH')
        if (metrics['daily_loss_pct'] >= self.cfg.daily_loss_limit_pct
                or metrics['drawdown_pct'] >= self.cfg.drawdown_limit_pct
                or metrics['liquidation_count']):
            return self.status('HOLD_LOSS_CIRCUIT_BREAKER')
        if account and account.available <= 0:
            return self.status('HOLD_NO_AVAILABLE_MARGIN')
        if len(risk_positions) >= self.cfg.paper_max_positions:
            return self.status('HOLD_POSITION_LIMIT')
        if not self.llm.available:
            return self.status('HOLD_AI_NOT_CONFIGURED')
        queue=self.candidate_queue()
        if not self.external_scan and time.time()-self.store.get('last_scan_at',0)>=self.cfg.worker_loop_interval_seconds:
            self.scan_market()
        if self.cfg.ai_provider_pool_enabled and time.time()<self.store.get('provider_defer_until',0):
            return self.status('HOLD_PROVIDER_POOL_WAIT',finalize_latest=False)
        if (self.cfg.ai_schedule_mode=='interval'
                and time.time()-self.store.get('last_evaluation_at',0)<self.cfg.anthropic_min_interval_seconds):
            return self.status('HOLD_EVALUATION_INTERVAL')
        with self.lock:
            open_symbols=set(ledger.positions)
            seen=set(ledger.state['seen'])
        parallel=(getattr(self.cfg,'ai_provider_pool_enabled',False)
            and getattr(self.cfg,'ai_parallel_candidates',1)>1 and callable(getattr(self.llm,'fork',None)))
        count=min(getattr(self.cfg,'ai_parallel_candidates',1),4) if parallel else 1
        selected=[]
        price_drift=False
        for _ in range(count):
            candidate=queue.next(open_symbols=open_symbols|{c.symbol for c in selected},seen=seen,
                lease_seconds=max(120,self.cfg.decision_ttl_seconds+30) if parallel else 0)
            if candidate is None:
                break
            ticker=self.quote(candidate.symbol)
            if abs(ticker.last_price/candidate.entry_price-1)>min(candidate.stop_distance_pct/2,0.005):
                queue.drop(candidate)
                price_drift=True
                continue
            selected.append(candidate)
        if not selected:
            return self.status('HOLD_CANDIDATE_PRICE_DRIFT' if price_drift else 'HOLD_NO_QUALIFIED_SIGNAL')
        evaluation_started=time.time()
        risk_pct=min(self.cfg.risk_per_trade_pct,self.risk.preset.risk_per_trade_pct)
        parameters=dict(equity=analysis_equity,
            open_risk=sum(p['risk'] for p in risk_positions.values()),open_positions=len(risk_positions),
            risk_profile=self.cfg.risk_profile.value,risk_per_trade_pct=risk_pct,
            target_leverage=min(self.cfg.default_target_leverage,self.cfg.paper_max_leverage))
        if not parallel:
            result=self.graph.evaluate_candidates(selected,**parameters)
            return self._apply_evaluation(selected[0],result,account,evaluation_control,risk_pct,evaluation_started)
        # Network/model work overlaps, but every ledger mutation/risk decision is
        # applied serially on this account's worker after a fresh state read.
        outcomes=[]
        with ThreadPoolExecutor(max_workers=len(selected),thread_name_prefix='candidate-ai') as pool:
            jobs={pool.submit(self._evaluate_in_fork,candidate,parameters):candidate for candidate in selected}
            for future in as_completed(jobs):
                candidate=jobs[future]
                result=None
                try:
                    result,client_state=future.result()
                    for key,value in client_state.items():
                        setattr(self.llm,key,value)
                    outcomes.append(self._apply_evaluation(candidate,result,account,evaluation_control,risk_pct,evaluation_started))
                except sqlite3.Error:
                    raise
                except Exception as exc:
                    logger.warning('candidate evaluation held (%s)',type(exc).__name__)
                    outcomes.append(self.status('HOLD_CANDIDATE_EVALUATION_ERROR',
                        trace_id=result.metadata.get('analysis_trace_id') if result is not None else None,finalize_latest=False))
                finally:
                    queue.release(candidate)
        # Do not conceal a successful entry just because the other candidate held.
        if any(outcome['reason']=='PAPER_OPENED' for outcome in outcomes):
            return self.status('PAPER_OPENED',finalize_latest=False)
        return outcomes[-1]

    def _evaluate_in_fork(self,candidate,parameters):
        from trading.evaluation.service import ContextEvaluator
        llm=self.llm.fork()
        public=self.public
        owned=type(public) is MexcPublicClient and not public.use_fixtures and public.transport is None
        if owned:
            public=MexcPublicClient(base_url=self.cfg.mexc_base_url,timeout_seconds=3)
        try:
            evaluator=ContextEvaluator(llm,self.store,public,self.cfg)
            result=evaluator.evaluate_candidates([candidate],**parameters)
            state={name:getattr(llm,name) for name in ('last_status','last_provider','last_model','last_fallback_reason') if hasattr(llm,name)}
            return result,state
        finally:
            if owned:
                public.close()
            close=getattr(llm,'close',None)
            if close:
                close()

    def _apply_evaluation(self,selected,result,account,evaluation_control,risk_pct,evaluation_started):
        ledger=self.ledger
        queue=self.candidate_queue()
        trace_id=result.metadata.get('analysis_trace_id')
        def finish(reason):
            return self.status(reason,trace_id=trace_id,finalize_latest=False)
        if (self.cfg.ai_provider_pool_enabled and result.metadata.get('provider_called') is False
                and result.hold_reason=='invalid_or_unavailable_model'):
            # Quota/cooldown without any HTTP is not a new paid attempt. Avoid
            # re-rendering and notifying every worker tick while scans continue.
            delay=5 if result.metadata.get('provider_busy') else 60
            self.store.put('provider_defer_until',time.time()+delay)
        if result.metadata.get('provider_called') is not False:
            self.store.put('last_evaluation_at',evaluation_started)
            queue.evaluated(selected)
        with self.lock:
            metrics=ledger.metrics()
            risk_positions=copy.deepcopy(ledger.positions)
            analysis_equity=ledger.equity
        if result.action != GraphAction.TRADE or not all(note and note.mode=='llm' for note in (result.market, result.strategy, result.portfolio)):
            return finish('HOLD_CLAUDE_DECISION_OR_FAILURE')
        signal = result.selected_signal
        if signal is None:
            return finish('HOLD_MISSING_SELECTED_SIGNAL')
        ticker = self.quote(signal.symbol)
        # Do not execute a proposal after material price drift during AI latency.
        if abs(ticker.last_price/signal.entry_price-1) > min(signal.stop_distance_pct/2, 0.005):
            return finish('HOLD_PRICE_DRIFT')
        if account:
            account = self.account()
            if account.available <= 0 or time.time()-account.observed_at > self.cfg.market_max_age_seconds:
                return finish('HOLD_ACCOUNT_STALE_OR_NO_MARGIN')
        rules = self.public.get_contract_rules(signal.symbol)
        def fresh_proposal():
            proposal=apply_decision_sizing(signal,result.metadata.get('sizing_plan'),ledger.equity,risk_pct,
                min(self.cfg.paper_max_leverage,self.cfg.default_target_leverage),rules.max_leverage)
            proposal.rationale=result.metadata.get('rationale',signal.rationale)
            return proposal
        try:
            proposal=fresh_proposal()
        except PlanSizingError:
            return finish('HOLD_INVALID_MODEL_SIZING')
        context = RiskContext(equity=analysis_equity, starting_capital=ledger.state['initial'], capital_floor=0,
            open_risk=sum(p['risk'] for p in risk_positions.values()), open_positions=len(risk_positions),
            daily_loss_pct=metrics['daily_loss_pct'], drawdown_pct=metrics['drawdown_pct'])
        decision = self.risk.evaluate(proposal, context)
        self.store.put('last_risk_decision', decision.model_dump(mode='json'))
        if not decision.accepted:
            return finish('HOLD_RISK_REJECT')
        ticker = self.quote(signal.symbol)
        if abs(ticker.last_price/signal.entry_price-1) > min(signal.stop_distance_pct/2, 0.005):
            return finish('HOLD_PRICE_DRIFT')
        with self.lock:
            if result.metadata.get('decision_started_at') and time.time()-result.metadata['decision_started_at'] > self.cfg.decision_ttl_seconds:
                return finish('HOLD_DECISION_EXPIRED')
            if self.external_control:
                latest_control=self.external_control()
                if not latest_control.get('enabled') or latest_control.get('updated_at')!=evaluation_control.get('updated_at'):
                    return finish('HOLD_SETTINGS_CHANGED')
            if Path(self.cfg.kill_switch_path).exists():
                return finish('HOLD_KILL_SWITCH')
            if not queue.is_current(signal):
                return finish('HOLD_CANDIDATE_STALE')
            if self.protection:
                self.protection.assert_ready()
            if getattr(self,'management_fatal',False):
                return finish('HOLD_MANAGEMENT_PERSISTENCE_FAILURE')
            current = ledger.metrics()
            if current['liquidation_count'] or current['daily_loss_pct'] >= self.cfg.daily_loss_limit_pct or current['drawdown_pct'] >= self.cfg.drawdown_limit_pct:
                return finish('HOLD_LOSS_CIRCUIT_BREAKER')
            # Recheck risk against fresh equity after AI, not the original snapshot.
            try:
                proposal=fresh_proposal()
            except PlanSizingError:
                return finish('HOLD_INVALID_MODEL_SIZING')
            context.equity = ledger.equity
            context.open_risk = sum(p['risk'] for p in ledger.positions.values())
            context.open_positions = len(ledger.positions)
            if context.open_positions >= self.cfg.paper_max_positions or not self.risk.evaluate(proposal, context).accepted:
                return finish('HOLD_RISK_CHANGED')
            opened = ledger.open(proposal, rules, ticker.bid1, ticker.ask1, signal.features['dedup_key'], proposal.metadata['risk_per_trade_pct'], self.cfg.liquidation_buffer_pct)
            if opened is None:
                return finish('HOLD_DUPLICATE_OR_ALREADY_OPEN')
            if opened:
                opened['decision_ai'] = copy.deepcopy(result.metadata.get('decision_ai',dict(provider='unknown',model='unknown',fallback_reason='')))
                ledger.state['events'][-1]['decision_ai'] = copy.deepcopy(opened['decision_ai'])
                opened['charts'] = result.metadata.get('charts', [])
                opened['analysis_version'] = self.cfg.analysis_version
                opened['rationale'] = proposal.rationale
                opened['invalidation'] = result.metadata.get('invalidation', '')
                ledger.state['events'][-1]['rationale'] = proposal.rationale
                ledger.state['events'][-1]['charts'] = opened['charts']
                ledger.state['events'][-1]['analysis_version'] = self.cfg.analysis_version
                ledger.save()
        return finish('PAPER_OPENED')
