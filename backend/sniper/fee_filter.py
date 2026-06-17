"""
FeeFilter — Global fees paid hard filter (≥ 0.5 SOL threshold).

Provides both a live-computed value (from LaunchDetector trade accumulation)
and an on-chain derivation, with fee_threshold configurability.
"""

from __future__ import annotations


def passes_fee_filter(fees_paid_sol: float, threshold: float = 0.5) -> bool:
    """
    Check if a token has accumulated enough pump.fun fees to qualify.

    Pump.fun charges 1% on buys. For fees >= 0.5 SOL, the token must have
    generated >= 50 SOL in cumulative buy volume, proving real trading activity.
    """
    return fees_paid_sol >= threshold


def estimate_fees_from_reserves(real_sol_reserves: float) -> float:
    """
    On-chain derivation: every buy deposits 99% to the curve; 1% goes as fee.
    So if curve has R SOL, total buys = R/0.99, fees = R * 0.01 / 0.99
    """
    if real_sol_reserves <= 0:
        return 0.0
    return real_sol_reserves * (0.01 / 0.99)
