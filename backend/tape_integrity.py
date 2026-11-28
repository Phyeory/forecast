"""
Tape-integrity rules — shared by the iter103 corpus audit and the live
autofeed tripwire.

The rules decide, from the recorded 1 s tape ALONE, whether a recording is a
real two-sided market or manufactured activity.  They deliberately do NOT flag
a tape for dying: death is everywhere in the corpus and is not evidence of
manipulation.  Every rule is either a physical impossibility or a mechanical
fingerprint:

  R1 test_fixture          every candle trades the *exact same* SOL size
                           (>=90% modal share) — a generator, not a market.
  R2 activity_padding      the same bot emits hundreds/thousands of *identical*
                           sub-fee prints (size < 10 000 lamports, i.e. below
                           the Solana tx fee, so it cannot be a real swap).
  R3 no_tradable_market    < 2 SOL of volume above the fee floor across the
                           tape — there is no market to trade.
  R4 mechanical_ladder     >=10% of consecutive price steps are identical to
                           1e-5 relative — a programmatic ladder.
  R5 rug_pull              the pool is destroyed while the tape is running:
                           >=65% collapse inside 60 s that never recovers,
                           with pool SOL falling to <=50% of its 60 s high.

Everything else is `clean` (including a token that simply died).
"""

from __future__ import annotations

from collections import Counter

import numpy as np

# ── thresholds ──────────────────────────────────────────────────────────────
FEE_FLOOR_SOL = 1e-5      # 10 000 lamports > tx fee; a smaller "swap" is dust
FIXTURE_SHARE = 0.90      # >=90% of candles at one exact size → generator
PAD_MIN_COUNT = 50        # >=50 identical sub-fee prints on one tape is a bot
PAD_MIN_SHARE = 0.20      # ... and they must be a real share of the candle set
FIXED_MIN_COUNT = 200     # >=200 identical prints at any size → fixed-size bot
FIXED_MIN_SHARE = 0.10
LADDER_EQ = 0.10          # >=10% of steps identical to 1e-5 relative
NO_MARKET_SOL = 2.0       # < 2 SOL traded above the fee floor ⇒ no market
MIN_CANDLES = 100         # thin tapes are judged by the stillborn rule only

# rug event: the pool is destroyed while the recording is live
RUG_DROP_60S = 0.35       # >=65% collapse inside a 60 s window
RUG_RECOVERY = 1.6        # never trades back above 1.6x the collapse low
RUG_POOL_KILL = 0.50      # pool SOL at the collapse <= 50% of its 60 s high
RUG_NOPOOL_DROP = 0.20    # pool unknown ⇒ demand a >=80% collapse instead
RUG_NOPOOL_SPIKE = 10.0   # ... plus a >=10x volume spike at the collapse

# Rules that justify killing a LIVE session (the rest are corpus judgements:
# thin/quiet tapes are not dangerous to a live trader per se).
LIVE_KILL_REASONS = ("rug_pull", "activity_padding", "fixed_size_bot",
                     "mechanical_ladder", "test_fixture")


def _modal(values: np.ndarray) -> tuple[float, int, float]:
    """(modal value, count, share) on 9-dp rounding — exact size repetition."""
    if len(values) == 0:
        return 0.0, 0, 0.0
    cnt = Counter(np.round(values, 9).tolist())
    val, n = cnt.most_common(1)[0]
    return float(val), int(n), n / len(values)


def tape_features(candles: list[tuple]) -> dict:
    """Tape metrics for one recording.

    `candles` rows: (time, open, high, low, close, volume, buy_volume,
    sell_volume, pool_sol) ordered by time.
    """
    f: dict = {"n": len(candles)}
    if len(candles) < 5:
        return f
    a = np.asarray(candles, dtype=float)
    t, op, hi, lo, cl, v, bv, sv, ps = a.T
    ok = (cl > 0) & (op > 0)
    if ok.sum() < 5:
        return f
    t, op, hi, lo, cl, v, bv, sv, ps = (x[ok] for x in (t, op, hi, lo, cl, v, bv, sv, ps))
    n = len(cl)
    f["n"] = n
    f["dur_s"] = float(t[-1] - t[0])

    logc = np.log(cl)
    lr = np.diff(logc)
    path = float(np.abs(lr).sum()) if len(lr) else 0.0
    f["price_path"] = path
    f["range_ratio"] = float(hi.max() / lo[lo > 0].min()) if (lo > 0).any() else None
    f["final_over_peak"] = float(cl[-1] / hi.max()) if hi.max() > 0 else None

    # ── volume microstructure ──────────────────────────────────────────────
    vpos = v[v > 0]
    f["V"] = float(v.sum())
    f["real_vol_sol"] = float(v[v >= FEE_FLOOR_SOL].sum())
    f["real_candles"] = int((v >= FEE_FLOOR_SOL).sum())
    f["dust_candle_frac"] = float((v < FEE_FLOOR_SOL).mean())
    mval, mcount, mshare = _modal(vpos)
    f["v_modal_sol"] = mval
    f["v_modal_count"] = mcount
    f["v_modal_share"] = mshare
    f["subfee_modal"] = bool(mval < FEE_FLOOR_SOL)

    # ── mechanical ladder ──────────────────────────────────────────────────
    al = np.abs(lr)
    al = al[al > 1e-9]
    if len(al) >= 100:
        f["ladder_eq"] = float((np.abs(np.diff(al)) / al[:-1] < 1e-5).mean())
    else:
        f["ladder_eq"] = 0.0

    # ── AMM physics (only where pool depth was recorded) ───────────────────
    ps_ok = ps > 0
    f["ps_cov"] = float(ps_ok.mean())
    f["ps_med"] = float(np.median(ps[ps_ok])) if ps_ok.any() else 0.0
    if ps_ok.any() and path > 0:
        f["amm_ratio"] = float(v[v >= FEE_FLOOR_SOL].sum() /
                               (np.median(ps[ps_ok]) * path))
    else:
        f["amm_ratio"] = None
    # ── rug event ──────────────────────────────────────────────────────────
    win_s = 60.0
    dd60, dd_i = 1.0, None
    for j in range(n):
        k = int(np.searchsorted(t, t[j] - win_s))
        peak = hi[k:j + 1].max()
        if peak > 0 and cl[j] / peak < dd60:
            dd60, dd_i = float(cl[j] / peak), j
    f["dd60"] = dd60
    f["dd60_pos"] = (float((t[dd_i] - t[0]) / max(t[-1] - t[0], 1.0))
                     if dd_i is not None else None)
    f["dd60_recovery"] = float(cl[dd_i:].max() / cl[dd_i]) if dd_i is not None else 1.0
    vmed = float(np.median(v[v > 0])) if (v > 0).any() else 0.0
    f["dd60_vol"] = float(v[dd_i]) if dd_i is not None else 0.0
    f["dd60_vol_spike"] = (float(v[dd_i] / vmed)
                           if vmed > 0 and dd_i is not None else 0.0)
    if ps_ok.any() and dd_i is not None and ps[dd_i] > 0:
        k = int(np.searchsorted(t, t[dd_i] - win_s))
        win = ps[k:dd_i + 1]
        win = win[win > 0]
        f["dd60_pool_drop"] = float(ps[dd_i] / win.max()) if len(win) else None
    else:
        f["dd60_pool_drop"] = None

    # migration / reserve-basis step: pool AND price collapse together
    if ps_ok.sum() > 20:
        p = ps[ps_ok]
        dlp = np.diff(np.log(p))
        dlc = np.diff(np.log(cl[ps_ok]))
        basis = (dlp < -0.6) & (np.abs(dlc - dlp) < 0.6)
        f["basis_steps"] = int(basis.sum())
    else:
        f["basis_steps"] = 0
    return f


def features_from_dicts(candles: list[dict]) -> dict:
    """Same as `tape_features` but takes candle dicts (live recorder rows)."""
    rows = [(c.get("time", 0), c.get("open", 0), c.get("high", 0),
             c.get("low", 0), c.get("close", 0), c.get("volume", 0) or 0,
             c.get("buy_volume", 0) or 0, c.get("sell_volume", 0) or 0,
             c.get("pool_sol", 0) or 0) for c in candles]
    return tape_features(rows)


def is_rug(f: dict) -> bool:
    """Pool destroyed on tape ⇒ the tape is a rug, not a losing market."""
    dd = f.get("dd60")
    if dd is None or dd >= RUG_DROP_60S:
        return False
    if f.get("dd60_recovery", 1.0) >= RUG_RECOVERY:
        return False
    pdrop = f.get("dd60_pool_drop")
    if f.get("ps_cov", 0.0) > 0.5 and pdrop is not None:
        return pdrop < RUG_POOL_KILL
    return (dd < RUG_NOPOOL_DROP
            and f.get("dd60_vol_spike", 0.0) >= RUG_NOPOOL_SPIKE
            and f.get("dd60_vol", 0.0) >= 5.0)


def classify(f: dict) -> list[str]:
    """Machine-verdict reasons.  Empty list ⇒ the tape is left in the corpus."""
    if f.get("n", 0) == 0:
        return ["empty_tape"]
    if f["n"] < MIN_CANDLES:
        return ["stillborn_lt100"]
    n = f["n"]
    r: list[str] = []
    if f["v_modal_share"] >= FIXTURE_SHARE:
        r.append("test_fixture")
    if f["subfee_modal"] and (
            (f["v_modal_count"] >= PAD_MIN_COUNT and f["v_modal_share"] >= PAD_MIN_SHARE)
            or (f["v_modal_count"] >= FIXED_MIN_COUNT and f["v_modal_share"] >= FIXED_MIN_SHARE)):
        r.append("activity_padding")
    elif f["v_modal_count"] >= FIXED_MIN_COUNT and f["v_modal_share"] >= FIXED_MIN_SHARE:
        r.append("fixed_size_bot")
    if f["real_vol_sol"] < NO_MARKET_SOL:
        r.append("no_tradable_market")
    if f["ladder_eq"] >= LADDER_EQ:
        r.append("mechanical_ladder")
    if is_rug(f):
        r.append("rug_pull")
    return r
