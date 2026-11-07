"""Reserve estimated upper-bound spend before requests; retain on uncertain failure."""
from datetime import datetime
from time import time
from zoneinfo import ZoneInfo
from uuid import uuid4


class Budget:
    def __init__(self, store, cfg):
        self.store, self.cfg = store, cfg

    def can_spend(self):
        day = datetime.now(ZoneInfo('Europe/Istanbul')).date().isoformat()
        state = self.store.get('ai_budget', {})
        if not self.cfg.ai_budget_enabled:
            return True
        return (state.get('total', 0) < self.cfg.anthropic_total_budget_usd
                and state.get('days', {}).get(day, 0) < self.cfg.anthropic_daily_budget_usd)

    def reserve(self, amount):
        day = datetime.now(ZoneInfo('Europe/Istanbul')).date().isoformat()
        db = self.store.db
        with self.store.lock, db:
            db.execute('BEGIN IMMEDIATE')
            state = self.store.get('ai_budget', dict(total=0., days={}, reservations={}, input_tokens=0, output_tokens=0, successful_calls=0, last_call=0))
            if self.cfg.ai_budget_enabled and (state['total']+amount > self.cfg.anthropic_total_budget_usd
                    or state['days'].get(day, 0)+amount > self.cfg.anthropic_daily_budget_usd):
                return None
            ident = uuid4().hex
            state['total'] += amount
            state['days'][day] = state['days'].get(day, 0)+amount
            state['reservations'][ident] = dict(day=day, amount=amount)
            state['last_call'] = time()
            import json
            db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', ('ai_budget', json.dumps(state)))
            return ident

    def release(self, ident):
        """Release only a pending reservation known not to have generated usage.

        Timeouts and server failures remain pending because billing is uncertain.
        Returns False if already settled/released, so retries cannot subtract twice.
        """
        db = self.store.db
        with self.store.lock, db:
            db.execute('BEGIN IMMEDIATE')
            state = self.store.get('ai_budget', {})
            row = state.get('reservations', {}).pop(ident, None)
            if row is None:
                return False
            state['total'] -= row['amount']
            state['days'][row['day']] -= row['amount']
            import json
            db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', ('ai_budget', json.dumps(state)))
            return True

    def settle(self, ident, input_tokens, output_tokens):
        with self.store.lock:
            self._settle(ident, input_tokens, output_tokens)

    def _settle(self, ident, input_tokens, output_tokens):
        state = self.store.get('ai_budget')
        row = state['reservations'].pop(ident)
        actual = (input_tokens*self.cfg.anthropic_input_usd_per_million + output_tokens*self.cfg.anthropic_output_usd_per_million)/1e6
        difference = actual-row['amount']
        state['total'] += difference
        state['days'][row['day']] += difference
        state['input_tokens'] += input_tokens
        state['output_tokens'] += output_tokens
        state['successful_calls'] += 1
        self.store.put('ai_budget', state)
