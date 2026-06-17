"""
ChartValidator — Classifies a token's chart as REAL (organic) or FAKE (manipulated).

Uses the first 30–60 seconds of candle data after Act 1 detection to score the chart
against known fake/manipulated patterns.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import math

from candle_aggregator import Candle
from sniper.launch_detector import LaunchDetector


@dataclass
class ChartClassification:
    is_real: bool
    confidence: float       # 0.0 to 1.0
    flags: list[str]        # human-readable explanation of triggers


def classify_chart(
    candles: list[Candle],
    launch_detector: LaunchDetector,
) -> ChartClassification:
    """
    Classify chart as real vs fake using first 30–60s of candle data.

    NOTE: 'Dominant First Candle' is intentionally excluded — on pump.fun
    the dev/creator always makes the first buy, making this candle structurally
    large for every token. Penalising it would reject virtually all real tokens.

    NOTE: 'Sell Volume Absent in Dip' is excluded — this validation runs at
    the start of Act 2. At that moment the sell-off has just begun, so sell
    volume is naturally low. Checking it here gives a misleading signal.

    FAKE indicators add to fake_score:
      1. Staircase pattern (weight 2)   — price only stair-stepped up, no real pullbacks
      2. Uniform trade sizes (weight 1) — bot-like uniform buy sizes
      3. No genuine dip (weight 2)      — never left Act 1 (spike never retracted)

    Hard reject: fake_score >= 4
    Soft reject: fake_score == 3

    REAL indicators subtract from fake_score:
      R1. Deep dip (>= 40%): -1
      R2. High variance in trade sizes: -1
      R3. Alternating green/red candles in spike: -1
    """
    flags: list[str] = []
    fake_score = 0

    if not candles or len(candles) < 2:
        return ChartClassification(is_real=True, confidence=0.5, flags=["insufficient_data"])

    # ── FAKE Indicator 1: Staircase Pattern (weight 2) ──────────────────
    # In spike candles: >90% green with no red candle >2% body
    green_count = 0
    total_spike = min(len(candles), 30)  # first 30 candles
    spike_candles = candles[:total_spike]

    for c in spike_candles:
        if c.close >= c.open:
            green_count += 1

    green_pct = green_count / total_spike if total_spike > 0 else 0

    # Check if any red candle has significant body
    has_significant_red = False
    for c in spike_candles:
        if c.close < c.open:
            body_pct = abs(c.close - c.open) / max(c.open, 1e-15)
            if body_pct > 0.02:
                has_significant_red = True
                break

    if green_pct >= 0.90 and not has_significant_red:
        fake_score += 2
        flags.append("staircase_pattern")

    # ── FAKE Indicator 2: Uniform Trade Sizes (weight 1) ────────────────
    # (Dominant First Candle removed — structurally normal on pump.fun)
    trade_sizes = launch_detector.trade_sizes
    if len(trade_sizes) >= 5:
        mean_size = sum(trade_sizes) / len(trade_sizes)
        if mean_size > 0:
            variance = sum((s - mean_size) ** 2 for s in trade_sizes) / len(trade_sizes)
            std_dev = math.sqrt(variance)
            if std_dev < mean_size * 0.15:
                fake_score += 1
                flags.append("uniform_trade_sizes")

    # ── FAKE Indicator 3: No Genuine Dip (weight 2) ─────────────────────
    if launch_detector.current_act == 1:
        fake_score += 2
        flags.append("no_dip_occurred")

    # ── REAL Indicator R1: Deep Dip ─────────────────────────────────────
    if launch_detector.dip_depth >= 0.40:
        fake_score -= 1
        flags.append("deep_dip_real")

    # ── REAL Indicator R2: High Variance in Trade Sizes ─────────────────
    if len(trade_sizes) >= 5:
        mean_size = sum(trade_sizes) / len(trade_sizes)
        if mean_size > 0:
            variance = sum((s - mean_size) ** 2 for s in trade_sizes) / len(trade_sizes)
            std_dev = math.sqrt(variance)
            if std_dev > mean_size * 0.5:
                fake_score -= 1
                flags.append("high_size_variance_real")

    # ── REAL Indicator R3: Alternating Green/Red in Spike ───────────────
    if total_spike >= 5:
        alternations = 0
        for i in range(1, total_spike):
            prev_green = spike_candles[i - 1].close >= spike_candles[i - 1].open
            curr_green = spike_candles[i].close >= spike_candles[i].open
            if prev_green != curr_green:
                alternations += 1
        alt_ratio = alternations / (total_spike - 1) if total_spike > 1 else 0
        if alt_ratio > 0.3:
            fake_score -= 1
            flags.append("alternating_candles_real")

    # ── Final Classification ─────────────────────────────────────────────
    # Hard reject threshold raised to 4 because we removed two heavy indicators
    # that were firing legitimately on real pump.fun tokens.
    # fake_score==3 is a soft reject; fake_score==2 is borderline and passes.
    if fake_score >= 4:
        return ChartClassification(is_real=False, confidence=0.9, flags=flags)
    elif fake_score == 3:
        return ChartClassification(is_real=False, confidence=0.7, flags=flags)
    elif fake_score == 2:
        # Borderline — allow through with reduced confidence
        return ChartClassification(is_real=True, confidence=0.5, flags=flags)
    elif fake_score == 1:
        return ChartClassification(is_real=True, confidence=0.75, flags=flags)
    else:
        return ChartClassification(is_real=True, confidence=0.95, flags=flags)
