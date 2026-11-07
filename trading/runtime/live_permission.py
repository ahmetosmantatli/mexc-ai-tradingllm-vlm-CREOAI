"""Explicit arm for one live generation. Paper is not an armed state.

The first time this process records live mode, entries are disarmed.
Any change of broker mode or of LIVE_TRADING_ENABLED clears both the armed
and the running flags, including live → paper → live.
"""
from trading.config import BrokerMode


class LivePermissionError(RuntimeError):
    """Stable permission code. Never a credential or an exchange body."""


class LivePermission:
    KEY = 'live:permission'

    def __init__(self, store):
        self.store = store

    def sync(self, *, broker_mode, live_trading_enabled):
        mode = broker_mode.value if isinstance(broker_mode, BrokerMode) else str(broker_mode)
        enabled = live_trading_enabled is True
        live = enabled and mode == 'live'
        row = self.store.get(self.KEY)
        if not isinstance(row, dict):
            self.store.put(self.KEY, dict(generation=1, broker_mode=mode, live_trading_enabled=enabled,
                effective_live=live, armed=False, running=False, armed_generation=None, armed_by=None))
            return self.view()
        changed = (row.get('broker_mode') != mode or bool(row.get('live_trading_enabled')) != enabled
            or bool(row.get('effective_live')) != live)
        if changed:
            row = dict(row)
            row.update(generation=int(row.get('generation') or 0)+1, broker_mode=mode,
                live_trading_enabled=enabled, effective_live=live, armed=False, running=False,
                armed_generation=None, armed_by=None)
            self.store.put(self.KEY, row)
        return self.view()

    def view(self):
        row = dict(self.store.get(self.KEY) or {})
        armed = bool(row.get('armed')) and row.get('armed_generation') == row.get('generation') and row.get('effective_live') is True
        running = bool(row.get('running')) and armed
        row.update(armed=armed, running=running)
        return row

    def arm(self, operator_id, *, allowed):
        row = dict(self.store.get(self.KEY) or {})
        if row.get('effective_live') is not True:
            raise LivePermissionError('live_gates_required')
        allowed_ids = {str(item) for item in allowed}
        if not allowed_ids or str(operator_id) not in allowed_ids:
            raise LivePermissionError('operator_not_allowed')
        row.update(armed=True, running=True, armed_generation=row.get('generation'), armed_by=str(operator_id))
        self.store.put(self.KEY, row)
        return self.view()

    def stop_entries(self):
        """Stop new entries. Does not clear the arm and does not stop protection."""
        row = dict(self.store.get(self.KEY) or {})
        row['running'] = False
        self.store.put(self.KEY, row)
        return self.view()

    def resume_entries(self):
        """Resume new entries only for the current armed generation."""
        if not self.view().get('armed'):
            return self.view()
        row = dict(self.store.get(self.KEY) or {})
        row['running'] = True
        self.store.put(self.KEY, row)
        return self.view()
