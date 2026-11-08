"""Closed-set execution diagnostics. Never persist arbitrary exception text."""
CODES={
    'live entry price drift':'ENTRY_PRICE_DRIFT',
    'contract quantity outside exchange limits':'CONTRACT_QUANTITY_LIMIT',
    'position exceeds supported base risk tier':'CONTRACT_RISK_TIER',
    'exchange leverage exceeded':'EXCHANGE_LEVERAGE_CAP',
    'model target does not cover expected trading costs':'TARGET_COSTS',
    'model must explicitly choose live leverage and risk':'MODEL_SIZING_REQUIRED',
    'invalid account leverage cap':'ACCOUNT_LEVERAGE_UNAVAILABLE',
    'stale live quote':'STALE_QUOTE','invalid live quote':'INVALID_QUOTE',
    'private_preflight_rejected':'PRIVATE_PREFLIGHT',
    'deterministic_risk_rejected':'RISK_REJECTED',
    'insufficient_available_margin':'INSUFFICIENT_MARGIN',
    'insufficient_liquidation_buffer':'LIQUIDATION_BUFFER',
    'fees_slippage_exceed_approved_risk':'COSTS_EXCEED_RISK',
    'approved_entry_price_drift':'ENTRY_PRICE_DRIFT',
    'available_balance_required':'NO_AVAILABLE_FUNDS',
    'fresh_executable_quote_required':'STALE_QUOTE',
    'operator_execution_paused':'CONTROL_CHANGED',
    'automatic_start_grant_not_current':'START_GRANT',
    'automatic_control_revision_changed':'CONTROL_CHANGED',
    'live_executor_busy':'EXECUTOR_BUSY',
    'live_halt_latched':'LIVE_HALT',
    'private_account_reconciliation_failed':'ACCOUNT_RECONCILIATION',
    'automatic_position_cap':'POSITION_LIMIT',
}
REASONS=set('''NONFINITE_RISK_INPUT UNVERIFIED_EXCHANGE_LEVERAGE_CAP LEVERAGE_CAP INSUFFICIENT_MARGIN
INVALID_STOP_DIRECTION UNDERSTATED_STOP_LOSS MISSING_STOP_LOSS INVALID_MAX_LOSS EQUITY_ZERO_HALT
MAX_LOSS_EXCEEDS_CAP RESIDUAL_CAPITAL_TOO_LOW SINGLE_TRADE_WIPE_FORBIDDEN CAPITAL_FLOOR_THREAT HALT_NEW_RISK
MAX_CONCURRENT_POSITIONS TOTAL_OPEN_RISK_CAP DAILY_LOSS_HALT WEEKLY_LOSS_HALT MAX_DRAWDOWN_KILL
ALLOCATION_EXCEEDS_CURRENT_EQUITY POSITION_MODE_MISMATCH OPEN_ORDERS_INCOMPLETE
UNRECONCILED_EXTERNAL_POSITIONS UNRECONCILED_EXTERNAL_ORDERS UNSUPPORTED_POSITION_STATE_OR_MARGIN_MODE
UNSUPPORTED_ORDER_MARGIN_OR_POSITION_MODE SYMBOL_ALREADY_OPEN ISOLATED_MARGIN_REQUIRED
EXCHANGE_LEVERAGE_CAP LEVERAGE_SETTING_MISMATCH EXCHANGE_RISK_VOLUME_CAP INSUFFICIENT_FREE_MARGIN
NO_CURRENT_AVAILABLE_FUNDS SYMBOL_CONFIGURATION_UNAVAILABLE'''.split())


def safe_execution_error(exc):
    result=dict(code=CODES.get(str(exc),'UNCLASSIFIED_EXECUTION_ERROR'),type=type(exc).__name__)
    codes=getattr(exc,'reason_codes',())
    result['reason_codes']=[code for code in codes if code in REASONS]
    for source,target in [('code','exchange_code'),('status_code','http_status')]:
        value=getattr(exc,source,None)
        if type(value) is int and 0<=value<=1000000000:result[target]=value
    return result
