"""Paper worker orchestration. Private access is read-only; HOLD is normal."""
import hashlib
import logging
import math
from pathlib import Path
import time
import sqlite3
import copy

from trading.agents.anthropic_client import AnthropicClient
from trading.agents.graph import EvaluationGraph, size_proposal_from_signal
from trading.agents.schemas import GraphAction
from trading.config import BrokerMode
from trading.exchange.mexc_public import MexcPublicClient
from trading.exchange.mexc_readonly import MexcReadOnlyClient
from trading.quant.scanner import MarketScanner, LiquidityFilter
from trading.risk.engine import RiskEngine, RiskContext
from trading.runtime.budget import Budget
from trading.runtime.paper import PaperLedger
from trading.runtime.store import RuntimeStore
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

    def quote(self, symbol):
        ticker = self.public.get_ticker(symbol)
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

    def status(self, reason):
        with self.lock:
            paper_snapshot = self.ledger.metrics() if self.ledger else None
            positions_snapshot = copy.deepcopy(self.ledger.positions) if self.ledger else {}
        snapshot = dict(mode='PAPER', live_orders_enabled=False, observed_at=time.time(), reason=reason,
            real_account=self.store.get('real_account'), paper=paper_snapshot,
            positions=positions_snapshot,
            ai_status=self.llm.last_status, ai_budget=self.store.get('ai_budget', {}))
        self.store.put('status', snapshot)
        if self.protection:
            snapshot['protection'] = self.protection.snapshot()
        if self.ledger and self.external_control is None:
            self.telegram.tick(self.ledger, self.status_text(snapshot))
        return snapshot

    @staticmethod
    def status_text(s):
        real, paper = s.get('real_account'), s.get('paper')
        text = f"MEXC AI | PAPER\nDurum: {s['reason']}\n"
        if real:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            observed = datetime.fromtimestamp(real['observed_at'], ZoneInfo('Europe/Istanbul')).strftime('%d.%m %H:%M:%S')
            text += (f"Gerçek futures: {real['equity']:.4f} USDT\n"
                     f"Kullanılabilir: {real['available']:.4f} USDT\nSon hesap okuması: {observed}\n")
        else:
            text += 'Gerçek futures: doğrulanmadı\n'
        if paper:
            text += (f"Simülasyon: {paper['equity']:.4f} USDT | Açık: {paper['open_positions']}\n"
                     f"Gerçekleşen net PnL: {paper['realized']:+.4f} USDT\n"
                     f"Açık PnL: {paper['unrealized']:+.4f} USDT\n"
                     f"Ücret: {paper['fees']:.4f} | Funding: {paper['funding']:+.4f} USDT\n")
        text += f"Claude: {s['ai_status']} | Harcanan/ayrılan tahmini: ${s['ai_budget'].get('total',0):.4f}"
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
            return self.status('HOLD_CLAUDE_NOT_CONFIGURED')
        if time.time()-self.store.get('last_evaluation_at', 0) < self.cfg.anthropic_min_interval_seconds:
            return self.status('HOLD_EVALUATION_INTERVAL')
        if time.time()-self.store.get('last_scan_at', 0) < self.cfg.worker_loop_interval_seconds:
            return self.status('HOLD_SCAN_INTERVAL')
        scanner = MarketScanner(self.public, liquidity=LiquidityFilter(
            self.cfg.scanner_min_quote_volume_24h, self.cfg.scanner_max_spread_bps),
            max_symbols=self.cfg.scanner_max_symbols)
        self.store.put('last_scan_at', time.time())
        scan = scanner.scan()
        eligible = []
        for candidate in scan.candidates:
            if candidate.symbol in risk_positions or not candidate.symbol.endswith('_USDT') or candidate.confidence < 0.65:
                continue
            ticker = self.quote(candidate.symbol)
            if ticker.quote_volume_24h < self.cfg.scanner_min_quote_volume_24h or ticker.spread_bps > self.cfg.scanner_max_spread_bps:
                continue
            # Tie idempotency to the actual closed candle, not a random signal UUID.
            candles = self.public.get_klines(candidate.symbol, interval='Min15', limit=60)['candles']
            closed = [c for c in candles if float(c[0])+900 <= time.time()]
            if not closed or time.time()-(float(closed[-1][0])+900) > 1200:
                continue
            key = hashlib.sha256(f'{candidate.symbol}:{candidate.strategy.value}:{candidate.side.value}:{closed[-1][0]}'.encode()).hexdigest()
            if key in ledger.state['seen']:
                continue
            candidate.features['dedup_key'] = key
            eligible.append(candidate)
        if not eligible:
            return self.status('HOLD_NO_QUALIFIED_SIGNAL')
        self.store.put('last_evaluation_at', time.time())
        risk_pct = min(self.cfg.risk_per_trade_pct, self.risk.preset.risk_per_trade_pct)
        result = self.graph.evaluate_candidates(eligible, equity=analysis_equity,
            open_risk=sum(p['risk'] for p in risk_positions.values()), open_positions=len(risk_positions),
            risk_profile=self.cfg.risk_profile.value, risk_per_trade_pct=risk_pct,
            target_leverage=min(self.cfg.default_target_leverage, self.cfg.paper_max_leverage))
        if result.action != GraphAction.TRADE or not all(note and note.mode=='llm' for note in (result.market, result.strategy, result.portfolio)):
            return self.status('HOLD_CLAUDE_DECISION_OR_FAILURE')
        signal = result.selected_signal
        if signal is None:
            return self.status('HOLD_MISSING_SELECTED_SIGNAL')
        ticker = self.quote(signal.symbol)
        # Do not execute a proposal after material price drift during AI latency.
        if abs(ticker.last_price/signal.entry_price-1) > min(signal.stop_distance_pct/2, 0.005):
            return self.status('HOLD_PRICE_DRIFT')
        if account:
            account = self.account()
            if account.available <= 0 or time.time()-account.observed_at > self.cfg.market_max_age_seconds:
                return self.status('HOLD_ACCOUNT_STALE_OR_NO_MARGIN')
        rules = self.public.get_contract_rules(signal.symbol)
        proposal = size_proposal_from_signal(signal, equity=ledger.equity, risk_per_trade_pct=risk_pct,
            target_leverage=min(self.cfg.paper_max_leverage, self.cfg.default_target_leverage, rules.max_leverage))
        context = RiskContext(equity=analysis_equity, starting_capital=ledger.state['initial'], capital_floor=0,
            open_risk=sum(p['risk'] for p in risk_positions.values()), open_positions=len(risk_positions),
            daily_loss_pct=metrics['daily_loss_pct'], drawdown_pct=metrics['drawdown_pct'])
        decision = self.risk.evaluate(proposal, context)
        self.store.put('last_risk_decision', decision.model_dump(mode='json'))
        if not decision.accepted:
            return self.status('HOLD_RISK_REJECT')
        ticker = self.quote(signal.symbol)
        if abs(ticker.last_price/signal.entry_price-1) > min(signal.stop_distance_pct/2, 0.005):
            return self.status('HOLD_PRICE_DRIFT')
        with self.lock:
            if result.metadata.get('decision_started_at') and time.time()-result.metadata['decision_started_at'] > self.cfg.decision_ttl_seconds:
                return self.status('HOLD_DECISION_EXPIRED')
            if self.external_control:
                latest_control=self.external_control()
                if not latest_control.get('enabled') or latest_control.get('updated_at')!=evaluation_control.get('updated_at'):
                    return self.status('HOLD_SETTINGS_CHANGED')
            if self.protection:
                self.protection.assert_ready()
            if getattr(self,'management_fatal',False):
                return self.status('HOLD_MANAGEMENT_PERSISTENCE_FAILURE')
            current = ledger.metrics()
            if current['liquidation_count'] or current['daily_loss_pct'] >= self.cfg.daily_loss_limit_pct or current['drawdown_pct'] >= self.cfg.drawdown_limit_pct:
                return self.status('HOLD_LOSS_CIRCUIT_BREAKER')
            # Recheck risk against fresh equity after AI, not the original snapshot.
            context.equity = ledger.equity
            context.open_risk = sum(p['risk'] for p in ledger.positions.values())
            context.open_positions = len(ledger.positions)
            if context.open_positions >= self.cfg.paper_max_positions or not self.risk.evaluate(proposal, context).accepted:
                return self.status('HOLD_RISK_CHANGED')
            opened = ledger.open(proposal, rules, ticker.bid1, ticker.ask1, signal.features['dedup_key'], risk_pct, self.cfg.liquidation_buffer_pct)
            if opened:
                opened['charts'] = result.metadata.get('charts', [])
                opened['analysis_version'] = self.cfg.analysis_version
                opened['rationale'] = proposal.rationale
                opened['invalidation'] = result.metadata.get('invalidation', '')
                ledger.state['events'][-1]['rationale'] = proposal.rationale
                ledger.state['events'][-1]['charts'] = opened['charts']
                ledger.state['events'][-1]['analysis_version'] = self.cfg.analysis_version
                ledger.save()
        return self.status('PAPER_OPENED')
