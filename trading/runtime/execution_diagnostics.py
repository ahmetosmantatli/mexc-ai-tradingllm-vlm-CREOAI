"""Closed-set execution diagnostics. Never persist arbitrary exception text."""
from math import isfinite

from trading.exchange.contracts import ContractQuantityError

class EntryPriceDrift(ValueError):
    def __init__(self, message, *, stage, planned, observed, tolerance):
        super().__init__(message)
        self.price_drift = dict(stage=stage, planned=planned, observed=observed,
                                deviation=observed / planned - 1, tolerance=tolerance)


CODES={
    'runner3_requires_verified_setup':'RUNNER3_HTF_SETUP_REQUIRED',
    'runner3_trigger_timeframe_mismatch':'RUNNER3_HTF_TRIGGER_TIMEFRAME',
    'runner3_target_reference_mismatch':'RUNNER3_HTF_TARGET_REFERENCE',
    'runner3_net_target_r_too_low':'RUNNER3_NET_TARGET_R',
    'runner3_proposal_rejected':'RUNNER3_PROPOSAL_REJECTED',
    'runner3_stop_loss_above_15pct_equity':'RUNNER3_STOP_LOSS_EQUITY',
    'runner3_cost_share_too_high':'RUNNER3_COST_SHARE',
    'runner3_margin_reserve':'RUNNER3_MARGIN_RESERVE',
    'runner3_margin_below_minimum':'RUNNER3_MARGIN_BELOW_MINIMUM',
    'runner3_evidence_expired':'RUNNER3_EVIDENCE_EXPIRED',
    'runner3_stop_reference_mismatch':'RUNNER3_STOP_REFERENCE',
    'runner3_structural_stop_too_tight':'RUNNER3_STRUCTURAL_STOP',

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
    'runner3_boost_same_side_exposure':'RUNNER3_BOOST_CONCENTRATION',
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
    if isinstance(exc, ContractQuantityError):
        result['contracts'] = {field: value for field in ('quantity','minimum','maximum','step')
            if isfinite(value := getattr(exc, field))}
    checks = getattr(exc, 'checks', None)
    if isinstance(checks, tuple) and checks and all(
            item in ('cost_adjusted_r', 'stop_loss_equity', 'free_margin') for item in checks):
        result['failed_checks'] = list(checks)
    drift = getattr(exc, 'price_drift', None)
    if (isinstance(drift, dict) and drift.get('stage') in ('planning', 'preflight')
            and all(type(drift.get(field)) in (int, float) and isfinite(drift[field])
                    for field in ('planned','observed','deviation','tolerance'))):
        result['price_drift'] = {field: drift[field]
                                 for field in ('stage','planned','observed','deviation','tolerance')}
    return result
