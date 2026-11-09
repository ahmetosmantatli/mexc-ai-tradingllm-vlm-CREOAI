"""Late VLM answers do not unwind an order that already left.

Text and VLM are asked together. A valid VLM answer inside the two-minute
deadline is the decision. After that deadline, or after the order is sent, the
text decision stands and a late chart answer may only update the next local plan.
"""
import threading

from trading.runtime import runner3_policy


class PendingVlm:
    def __init__(self, store, *, fee, slippage):
        self.store = store
        self.fee = fee
        self.slippage = slippage
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.settled = threading.Event()
        self.box = {}
        self.sending = False
        self.consumed = False
        self.deadline_passed = False
        self.late_action = None

    def complete(self, review):
        with self.lock:
            self.box['value'] = review
            if self.consumed:
                self.settled.set()
                return
            if self.deadline_passed or self.sending:
                self._late(review)
            else:
                self.settled.set()

    def take(self):
        """Use a valid VLM review that arrived inside the deadline, before send."""
        with self.lock:
            if self.consumed or self.deadline_passed or self.sending:
                return None
            review = self.box.get('value')
            if not review or review.get('decision') is None:
                return None
            self.consumed = True
            return review

    def close_deadline(self):
        """The two minutes passed. A later answer can only update the local plan."""
        with self.lock:
            self.deadline_passed = True
            self.cancelled.set()
            review = self.box.get('value')
            if review and not self.consumed:
                self._late(review)

    def arm_send(self):
        """The order is leaving. A later VLM answer must not unwind it."""
        with self.lock:
            self.sending = True
            self.cancelled.set()
            if self.consumed:
                return
            review = self.box.get('value')
            if review:
                self._late(review)

    def _late(self, review):
        decision = review.get('decision') if isinstance(review, dict) else None
        plan = getattr(decision, 'price_plan', None)
        if decision is None or decision.action != 'trade' or plan is None:
            self.late_action = 'ignored'
            self.settled.set()
            return
        side = 'long' if plan.take_profit_price > plan.stop_loss_price else 'short'
        entry = decision.limit_price if decision.limit_price is not None else plan.entry_price
        adjusted = runner3_policy.cost_adjusted_r(
            side=side, entry=entry, stop=plan.stop_loss_price, target=plan.take_profit_price,
            fee=self.fee, slippage=self.slippage)
        from trading.evaluation.local_limits import save_local_plan
        save_local_plan(self.store, symbol=review.get('symbol'), side=side, limit_price=decision.limit_price,
                        stop=plan.stop_loss_price, target=plan.take_profit_price, entry=entry,
                        cost_adjusted_r=adjusted, rationale=decision.rationale,
                        decision_mode='vlm')
        self.late_action = 'local_plan'
        self.settled.set()
