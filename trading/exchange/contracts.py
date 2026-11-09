"""Linear USDT contract sizing; fail closed on incomplete exchange rules."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from math import isfinite


class ContractQuantityError(ValueError):
    def __init__(self, message, *, quantity, minimum, maximum, step):
        super().__init__(message)
        self.quantity = quantity
        self.minimum = minimum
        self.maximum = maximum
        self.step = step


@dataclass(frozen=True)
class ContractRules:
    size: float
    step: float
    minimum: float
    maximum: float
    max_leverage: float
    maintenance: float
    price_step: float
    base_tier_max: float = 0

    @classmethod
    def parse(cls, row):
        if row.get('settleCoin') != 'USDT' or row.get('quoteCoin') != 'USDT' or row.get('state') != 0:
            raise ValueError('unsupported or inactive USDT contract')
        # Tiered risk requires its own exact exchange model; do not guess a tier.
        if row.get('riskLimitType') == 'CUSTOM':
            raise ValueError('custom risk tiers unsupported')
        vals = [float(row[k]) for k in ('contractSize', 'volUnit', 'minVol', 'maxVol', 'maxLeverage', 'maintenanceMarginRate', 'priceUnit')]
        if not all(isfinite(v) and v > 0 for v in vals) or vals[5] >= 1:
            raise ValueError('invalid contract rules')
        base_tier_max = float(row['riskBaseVol'])
        if not isfinite(base_tier_max) or base_tier_max <= 0:
            raise ValueError('base risk tier unavailable')
        return cls(*vals, base_tier_max)

    def size_order(self, notional, price, leverage):
        if leverage > self.max_leverage:
            raise ValueError('exchange leverage exceeded')
        qty = Decimal(str(notional)) / Decimal(str(price)) / Decimal(str(self.size))
        step = Decimal(str(self.step))
        qty = (qty / step).to_integral_value(rounding=ROUND_DOWN) * step
        if not Decimal(str(self.minimum)) <= qty <= Decimal(str(self.maximum)):
            raise ContractQuantityError('contract quantity outside exchange limits',
                quantity=float(qty), minimum=self.minimum, maximum=self.maximum, step=self.step)
        if self.base_tier_max and qty > Decimal(str(self.base_tier_max)):
            raise ContractQuantityError('position exceeds supported base risk tier',
                quantity=float(qty), minimum=self.minimum, maximum=self.base_tier_max, step=self.step)
        return float(qty), float(qty * Decimal(str(self.size)) * Decimal(str(price)))
