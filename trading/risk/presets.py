"""Risk profile presets. Aggressive Growth is the product default."""

from __future__ import annotations

from dataclasses import dataclass

from trading.config import RiskProfile


@dataclass(frozen=True)
class RiskPreset:
    profile: RiskProfile
    risk_per_trade_pct: float
    abs_max_loss_pct: float
    residual_min_pct: float
    max_leverage_majors: float
    max_leverage_alts: float
    max_concurrent_positions: int
    total_open_risk_pct: float
    daily_loss_halt_pct: float
    weekly_loss_halt_pct: float
    max_drawdown_kill_pct: float
    always_on_scan: bool
    fee_free_preference: bool
    capital_floor_enabled: bool
    stop_required: bool = True
    weekly_loss_halt_enabled: bool = True
    daily_loss_halt_enabled: bool = True


PRESETS: dict[RiskProfile, RiskPreset] = {
    RiskProfile.CAPITAL_PRESERVATION: RiskPreset(
        profile=RiskProfile.CAPITAL_PRESERVATION,
        risk_per_trade_pct=0.005,
        abs_max_loss_pct=0.01,
        residual_min_pct=0.70,
        max_leverage_majors=3.0,
        max_leverage_alts=2.0,
        max_concurrent_positions=2,
        total_open_risk_pct=0.015,
        daily_loss_halt_pct=0.02,
        weekly_loss_halt_pct=0.045,
        max_drawdown_kill_pct=0.08,
        always_on_scan=False,
        fee_free_preference=False,
        capital_floor_enabled=False,
    ),
    RiskProfile.BALANCED: RiskPreset(
        profile=RiskProfile.BALANCED,
        risk_per_trade_pct=0.01,
        abs_max_loss_pct=0.025,
        residual_min_pct=0.55,
        max_leverage_majors=8.0,
        max_leverage_alts=10.0,
        max_concurrent_positions=3,
        total_open_risk_pct=0.03,
        daily_loss_halt_pct=0.035,
        weekly_loss_halt_pct=0.07,
        max_drawdown_kill_pct=0.12,
        always_on_scan=True,
        fee_free_preference=True,
        capital_floor_enabled=True,
    ),
    RiskProfile.AGGRESSIVE_GROWTH: RiskPreset(
        profile=RiskProfile.AGGRESSIVE_GROWTH,
        risk_per_trade_pct=0.03,
        abs_max_loss_pct=0.05,
        residual_min_pct=0.40,
        max_leverage_majors=15.0,
        max_leverage_alts=25.0,
        max_concurrent_positions=4,
        total_open_risk_pct=0.10,
        daily_loss_halt_pct=0.055,
        weekly_loss_halt_pct=0.135,
        max_drawdown_kill_pct=0.225,
        always_on_scan=True,
        fee_free_preference=True,
        capital_floor_enabled=True,
    ),
    RiskProfile.USER_CUSTOM: RiskPreset(
        profile=RiskProfile.USER_CUSTOM,
        risk_per_trade_pct=0.02,
        abs_max_loss_pct=0.05,
        residual_min_pct=0.40,
        max_leverage_majors=15.0,
        max_leverage_alts=25.0,
        max_concurrent_positions=4,
        total_open_risk_pct=0.10,
        daily_loss_halt_pct=0.055,
        weekly_loss_halt_pct=0.135,
        max_drawdown_kill_pct=0.225,
        always_on_scan=True,
        fee_free_preference=True,
        capital_floor_enabled=True,
    ),
}


def get_preset(profile: RiskProfile) -> RiskPreset:
    return PRESETS[profile]
