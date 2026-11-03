"""Fill-pricing arithmetic shared by every execution pipeline.

Legacy-model fill fraction, slippage, and fee constants live here so the
replay simulator and the live trader cannot drift apart.
"""
from __future__ import annotations

import math

# Reference constants of the legacy fill model: a 0.0001 SOL per-tx fee and
# a 0.1 SOL typical retail order size.  Part of the execution contract.
REFERENCE_FEE = 0.0001
REFERENCE_SIZE = 0.1


def fill_fraction(priority_fee: float, bribe_fee: float, buy_size_sol: float,
                  slippage_pct: float) -> float:
    """How far into the execution candle (0 to 1) a legacy-model fill lands.

    Bounded product of fee competitiveness (high fee fills early), order
    size (larger sits deeper in the queue), and slippage tolerance.
    """
    total_fee = max(priority_fee + bribe_fee, 1e-12)
    base_delay = REFERENCE_FEE / (REFERENCE_FEE + total_fee)
    size_penalty = 1.0 + math.log10(max(1.0, buy_size_sol / REFERENCE_SIZE))
    slippage_factor = 1.0 + slippage_pct / 100.0
    frac = base_delay * size_penalty * slippage_factor
    return max(0.02, min(0.98, frac))
