"""
Futures execution model — leveraged-margin accounting layered on the existing
backtest pipeline (option A: paper-engine futures mode).

This module is **strictly additive**: it is only exercised from
``ForwardTester`` when ``market_type="futures"``.  Spot runs never import or
instantiate it, so spot behaviour/byte-identity is preserved by construction.

Design decisions (audit notes)
------------------------------
- The spot pipeline's ``ForwardTester`` keeps a ``balance`` of SOL, buys
  ``size_tokens`` tokens, and computes PnL from proceeds minus committed
  capital.  Futures instead keeps an **isolated-margin account**: a trade
  commits ``margin_sol`` as initial margin, controls
  ``notional = margin * leverage`` worth of token quantity, and its PnL is
  ``qty * (exit - entry)`` (long-only — see below).

- **Long-only is deliberate.**  ``StrategyEngineV2Adapter`` and
  ``ForwardTester`` are long-only today (`direction=+1` gate), and iter33–39
  of the research log demonstrated the down-side posterior (``P_down``) never
  reaches the ≥0.5 threshold that mirror-image short entries would require
  on this cohort ("down-blind" regime).  Engine changes are out of scope; the
  futures layer therefore supports leveraged LONG perps only.  A
  ``short_enable`` extension point is documented but intentionally not
  implemented (would require engine parity work measured in ~600 lines and
  is empirically net-negative per iter33a counterfactuals).

- **Mark price vs last price.**  The engine trades on the *last* price
  (candle close / intra-candle state close).  Liquidation decisions use the
  *mark* price.  We clamp deviations: if the recording carries no
  ``mark_price`` column (whole memecoin spot recording cohort), mark falls
  back to the state close — matching real exchanges' mark≈index behaviour
  when the two feeds coincide.

- **Funding.**  A fixed funding rate per ``funding_interval`` seconds
  (default 8h = 28800s), settled in discrete chunks whenever the event
  stream crosses an interval boundary (timestamp-anchored: the interval
  number is ``time // funding_interval_seconds``).  Positive rate → longs
  pay shorts → long position is *debited* ``rate * notional``; negative →
  the position is credited.  Funding accrues pro-rata when a position is
  open across a boundary, including the forced close at
  ``recording_ended``.

- **Fees.**  The task-specified calibration knobs ``s_0``/``s_1`` only exist
  inside `strategy_engineV2.py`'s Kelly gate as entry-decision costs — they
  are NOT the execution-pipeline fee model (which is
  ``priority_fee + bribe_fee`` flat SOL + ``slippage_pct``).  Futures
  therefore hooks the pipeline fee seam (forward_tester) with
  ``futures_taker_fee_fraction`` / ``futures_maker_fee_fraction`` /
  ``futures_slippage_pct``, defaulting to realistic CEX taker rates
  (0.045% taker ≈ Bybit/Binance, 0.1% slippage) instead of the pump.fun
  defaults.  The engine-internal ``s_0``/``s_1`` Kelly costs are not
  overridden per-run — they live in ``engine_params`` which the caller can
  already set, and futures callers SHOULD pass e.g.
  ``engine_params={"s_0": 0.00045, "fee_fraction": 0.00045}``.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


# ── Config ──────────────────────────────────────────────────────────────────

DEFAULT_TAKER_FEE = 0.00045      # 0.045% — Binance/Bybit USDT-M taker
DEFAULT_MAKER_FEE = 0.0002       # 0.02%
DEFAULT_SLIPPAGE_PCT = 0.1       # 0.1% — liquid perp book, vs spot 1–10%
DEFAULT_MAINT_MARGIN_RATE = 0.005  # 0.5% of notional (perp maintenance margin)
DEFAULT_LIQ_FEE = 0.005          # 0.5% of notional — insurance fund fee
DEFAULT_FUNDING_INTERVAL_S = 8 * 3600  # 8h = 28800 s
DEFAULT_FUNDING_RATE = 0.0001    # 0.01% per 8h — Binance default clamp


@dataclass
class FuturesConfig:
    """Futures-specific run parameters.  All defaults chosen so that a
    ``market_type="futures"`` run on a spot-style recording behaves as
    honestly as the data allows (mark≈close, zero funding if no column)."""
    leverage: float = 1.0
    funding_rate_per_interval: float = DEFAULT_FUNDING_RATE
    funding_interval_seconds: int = DEFAULT_FUNDING_INTERVAL_S
    maintenance_margin_rate: float = DEFAULT_MAINT_MARGIN_RATE
    liquidation_fee_fraction: float = DEFAULT_LIQ_FEE
    taker_fee_fraction: float = DEFAULT_TAKER_FEE
    maker_fee_fraction: float = DEFAULT_MAKER_FEE
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT
    use_mark_price: bool = True     # liquidate on mark (fallback: close)


@dataclass
class FuturesStats:
    total_liquidations: int = 0
    total_funding_paid: float = 0.0    # SOL debited by funding (gross)
    total_funding_received: float = 0.0
    total_fees_paid_futures: float = 0.0
    max_leverage_used: float = 0.0

    def to_dict(self) -> dict:
        return {
            "total_liquidations": self.total_liquidations,
            "total_funding_paid": round(self.total_funding_paid, 8),
            "total_funding_received": round(self.total_funding_received, 8),
            "total_funding_net": round(self.total_funding_paid - self.total_funding_received, 8),
            "total_fees_paid_futures": round(self.total_fees_paid_futures, 8),
            "max_leverage_used": round(self.max_leverage_used, 3),
        }


@dataclass
class FuturesPosition:
    """Open leveraged long.  qty is in *token units* (same units as the
    recording's price series: price is SOL per token)."""
    entry_time: int
    entry_price: float          # execution price (after slippage)
    qty: float                  # tokens controlled
    margin: float               # initial margin committed (SOL)
    leverage: float
    notional: float             # qty * entry_price
    entry_fee: float            # taker fee at open (SOL)
    mark_price: float = 0.0     # latest mark price observed
    last_funding_interval: int = 0  # time // interval at last settlement
    funding_accrued: float = 0.0    # net funding paid (SOL, + = paid)
    liq_price: float = 0.0          # liquidation trigger price (mark space)


class FuturesAccount:
    """
    Isolated-margin paper futures account, ONE open position at a time —
    mirrors the engine's single-long-position constraint.

    Held state is intentionally independent of the engine: a liquidation is
    checked on every intra-candle state using the mark price and fires
    regardless of the Bayesian exit decision (the engine never sees a
    liquidated trade; the pipeline clears its position book and calls
    ``notify_trade_closed`` so the engine's in_position flag stays in sync).
    """

    def __init__(self, cfg: Optional[FuturesConfig] = None,
                 starting_equity: float = 1.0,
                 sol_price_usd: float = 0.0):
        self.cfg = cfg or FuturesConfig()
        self.sol_price_usd = float(sol_price_usd)   # >0 → USDC-native accounting
        self.equity = float(starting_equity)   # free margin (SOL)
        self.position: Optional[FuturesPosition] = None
        self.stats = FuturesStats()

    # ── valuation ──────────────────────────────────────────────────────

    def mark_to_price(self, price: float) -> None:
        """Refresh the mark price on the open position (no-op if flat)."""
        if self.position is not None:
            self.position.mark_price = price

    # ── entry ──────────────────────────────────────────────────────────

    def open_long(self, price: float, time: int,
                  margin: Optional[float] = None) -> Optional[FuturesPosition]:
        """
        Open a leveraged long at *price* (execution price; caller applies
        slippage before calling).

        margin     → SOL committed; defaults to min(0.1×equity, equity) to
                     mirror ``buy_size_sol`` semantics at leverage 1.
        Returns None if insufficient equity (mirror of spot's balance guard).
        """
        if self.position is not None:
            return None
        if price <= 0:
            return None

        lev = max(self.cfg.leverage, 1.0)
        if margin is None:
            margin = 0.1 * self.equity        # mirrors buy_size 10% default
        fee_rate = self.cfg.taker_fee_fraction
        # total debit = margin + entry fee on the *notional*
        notional = margin * lev
        entry_fee = notional * fee_rate
        if margin + entry_fee > self.equity + 1e-15:
            # shrink margin so margin + fee == available equity
            margin = self.equity / (1.0 + fee_rate * lev)
            if margin <= 0:
                return None
            notional = margin * lev
            entry_fee = notional * fee_rate

        qty = notional / price
        # liquidation price (long): equity at liq == maintenance margin
        #   margin + qty*(p_liq - p_entry) = maint * qty * p_liq
        #   → p_liq = (p_entry*qty - margin) / (qty*(1 - maint))
        m = self.cfg.maintenance_margin_rate
        liq_price = (notional - margin) / (qty * (1.0 - m)) if qty > 0 else 0.0
        liq_price = max(liq_price, 0.0)

        self.equity -= (margin + entry_fee)
        self.stats.total_fees_paid_futures += entry_fee
        self.stats.max_leverage_used = max(self.stats.max_leverage_used, lev)

        self.position = FuturesPosition(
            entry_time=int(time),
            entry_price=price,
            qty=qty,
            margin=margin,
            leverage=lev,
            notional=notional,
            entry_fee=entry_fee,
            mark_price=price,
            last_funding_interval=int(time) // max(self.cfg.funding_interval_seconds, 1),
            liq_price=liq_price,
        )
        return self.position

    # ── funding ────────────────────────────────────────────────────────

    def settle_funding(self, time: int, funding_rate: Optional[float] = None,
                       price: Optional[float] = None) -> float:
        """
        Settle all funding intervals crossed since the last settlement.

        Timestamp-anchored on interval number ``time // interval_seconds`` so
        settlement is robust to bar cadence (1s recordings cross 0 intervals
        per bar; sparse recordings may cross many, e.g. overnight gaps).

        rate > 0: longs PAY  → debit  rate × notional(mark)
        rate < 0: longs RECEIVE.
        When the recording carries a ``funding_rate`` column the caller
        passes the current per-bar rate (overrides the run default).
        Returns the net funded amount debited (negative = credited).
        """
        pos = self.position
        if pos is None:
            return 0.0
        iv = max(self.cfg.funding_interval_seconds, 1)
        cur_iv = int(time) // iv
        n = cur_iv - pos.last_funding_interval
        if n <= 0:
            return 0.0
        rate = (self.cfg.funding_rate_per_interval
                if funding_rate is None else float(funding_rate))
        if rate == 0.0:
            pos.last_funding_interval = cur_iv
            return 0.0
        mark = price if price and price > 0 else pos.mark_price or pos.entry_price
        amount = n * rate * pos.qty * mark      # net paid by the long
        pos.last_funding_interval = cur_iv
        pos.funding_accrued += amount
        if amount > 0:
            self.stats.total_funding_paid += amount
        else:
            self.stats.total_funding_received += -amount
        # Funding settles against free equity if any, else against position
        # margin (exchange behaviour: funding drains margin first).
        if amount <= self.equity:
            self.equity -= amount
        else:
            shortfall = amount - self.equity
            self.equity = 0.0
            pos.margin -= shortfall
        return amount

    # ── liquidation ────────────────────────────────────────────────────

    def check_liquidation(self, time: int,
                          mark_price: Optional[float] = None) -> Optional[dict]:
        """
        Returns a liquidation dict if the open position's mark price has
        breached its liquidation price, else None.

        The check is engine-independent and must run on EVERY intra-candle
        state (feeds from the pipeline's 4-state expansion).
        """
        pos = self.position
        if pos is None:
            return None
        mark = mark_price if (mark_price and mark_price > 0 and self.cfg.use_mark_price) \
            else (pos.mark_price or pos.entry_price)
        if mark > pos.liq_price:
            return None
        return {"time": int(time), "mark_price": mark, "liq_price": pos.liq_price}

    def close_long(self, price: float, time: int, reason: str,
                   funding_rate: Optional[float] = None) -> Optional[dict]:
        """
        Close the open long at *price* (execution price; slippage applied by
        caller).  Settles funding up to *time* first, then computes realised
        PnL, exit fee, and (for liquidations) the insurance-fund fee.

        Liquidation close: if the loss after fees/Estar exceeds remaining
        collateral the position is capped at a total loss of its margin
        (cannot go below zero — exchange auto-liquidation floods the
        insurance fund).  Reason "liquidation" additionally applies
        ``liquidation_fee_fraction × notional``.
        """
        if self.position is None:
            return None

        self.settle_funding(time, funding_rate=funding_rate, price=price)
        pos = self.position
        assert pos is not None

        exit_price = price
        gross_pnl = pos.qty * (exit_price - pos.entry_price)
        exit_notional = pos.qty * exit_price
        exit_fee = exit_notional * self.cfg.taker_fee_fraction

        liq_fee = 0.0
        if reason == "liquidation":
            liq_fee = exit_notional * self.cfg.liquidation_fee_fraction

        pnl = gross_pnl - exit_fee - liq_fee
        # funded amounts already debited from equity/margin by settle_funding
        returned = pos.margin + gross_pnl - exit_fee - liq_fee
        credit = max(0.0, returned)
        if returned < 0.0:
            # Isolated margin: loss capped at committed collateral.
            pnl = -pos.margin - pos.entry_fee
            credit = 0.0
        self.equity += credit

        # USDC-native accounting when the caller provides a USD/SOL rate:
        # pipeline carries SOL (spot recordings); users want perp PnL in USDC.
        sol_price_usd = getattr(self, "sol_price_usd", 0.0) or 0.0
        pnl_usd = pnl * sol_price_usd if sol_price_usd > 0 else pnl

        self.stats.total_fees_paid_futures += exit_fee + liq_fee
        result = {
            "entry_time": pos.entry_time, "entry_price": pos.entry_price,
            "exit_time": int(time), "exit_price": exit_price,
            "side": "long", "leverage": pos.leverage,
            "size_sol": pos.margin,                    # margin committed
            "size_tokens": pos.qty,
            "notional_sol": pos.notional,
            "pnl_sol": pnl,
            "pnl_pct": (pnl / pos.margin * 100.0) if pos.margin > 0 else 0.0,
            "funding_paid": pos.funding_accrued if pos.funding_accrued > 0 else 0.0,
            "funding_received": -pos.funding_accrued if pos.funding_accrued < 0 else 0.0,
            "entry_fee": pos.entry_fee, "exit_fee": exit_fee,
            "liquidation_fee": liq_fee,
            "exit_reason": reason,
            "liq_price_at_entry": pos.liq_price,
            "exit_mark_price": pos.mark_price,
            "sol_price_usd": sol_price_usd,            # 0 on memecoin-recordings
            "position_notional_usdc": pos.notional * sol_price_usd if sol_price_usd > 0 else pos.notional,
        }
        self.position = None
        return result

    # ── unrealised view (for reporting) ────────────────────────────────

    def unrealized(self, price: float) -> float:
        if self.position is None or price <= 0:
            return 0.0
        pos = self.position
        gross = pos.qty * (price - pos.entry_price)
        # net of exit fee at this mark
        exit_fee = pos.qty * price * self.cfg.taker_fee_fraction
        return gross - exit_fee
