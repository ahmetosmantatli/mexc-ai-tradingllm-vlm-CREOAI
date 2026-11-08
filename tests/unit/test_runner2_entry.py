from decimal import Decimal

import pytest

from trading.runtime.runner2_entry import ioc_price_limit


def test_hot_price_jump_is_outside_risk_bounded_long_limit():
    limit = ioc_price_limit(side='long', stop=.0004266, target=.0004533,
                           contracts=18607, contract_size=1, price_step=.0000001,
                           loss_budget=.334134912, fee_rate=.0006, exit_slippage_bps=5)
    assert Decimal('.0004436') <= limit < Decimal('.0004494')
    stop = Decimal('.0004266') * Decimal('.9995')
    risk = lambda price: ((price-stop) + Decimal('.0006')*(price+stop))*18607
    assert risk(limit) <= Decimal('.334134912') < risk(limit+Decimal('.0000001'))


def test_short_rounds_up_and_rejects_infeasible_budget():
    inputs = dict(side='short', stop=105, target=90, contracts=10, contract_size=1,
                  price_step=.1, fee_rate=.001, exit_slippage_bps=10)
    limit = ioc_price_limit(**inputs, loss_budget=50)
    assert limit == Decimal('100.4')
    with pytest.raises(ValueError, match='no_risk_bounded_entry_tick'):
        ioc_price_limit(**inputs, loss_budget=.01)
