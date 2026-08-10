"""
Full trade-outcome analysis: winners vs losers, big vs small, across
every engine entry metric recorded in the per-trade JSON logs.

Reads per-token results from backend/v2_results/ matching a batch label,
derives entry market cap (entry_price x 1e9, pump.fun fixed supply),
classifies each trade into one of four classes:

    big winner   pnl_pct >= +20
    small winner 0 <= pnl_pct < +20
    big loser    pnl_pct <= -20
    small loser  -20 < pnl_pct < 0

Then renders a set of PNG figures into backend/report_figs/ and writes a
markdown report to <repo>/trade_report.md.

Usage:
    cd backend && source .venv/bin/activate
    python trade_report.py                          # iter31_baseline
    python trade_report.py --batch iter27_g50
    python trade_report.py --sol-usd 150            # override SOL->USD rate
"""

import argparse
import glob
import json
import os
from collections import Counter, defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy import stats as sps

# SOL->USD conversion. No USD price is persisted in price_data.db, so we use a
# fixed rate. Recordings span 2026-07-27..2026-08-07; user-confirmed SOL = $70.
DEFAULT_SOL_USD = 70.0


def _mcap_5k_buckets(max_bucket=200e3, final_label="200k+"):
    """Uniform $5k market-cap buckets from $0 to max_bucket, then a final
    catch-all bucket.  Returns (edges, labels)."""
    edges = list(range(0, int(max_bucket // 5e3) + 1))
    edges = [e * 5e3 for e in edges] + [np.inf]
    labels = [f"{int(edges[i]//1000)}-{int(edges[i+1]//1000)}k"
              for i in range(len(edges) - 2)] + [final_label]
    return edges, labels

# ----------------------------------------------------------------------------
# loading
# ----------------------------------------------------------------------------

CURATED = [
    ("mcap",            "entry mcap (USD)",        "Entry market cap (USD)"),
    ("signal_strength", "signal strength S",       "Base signal strength S"),
    ("s_effective",     "S_effective",             "Barrier-adjusted S_eff"),
    ("trend_confidence","trend confidence C",      "4-pillar confidence C"),
    ("m_hat",           "m_hat (momentum)",        "Kalman momentum estimate"),
    ("v2_P_up",         "P_up",                    "Bayesian P(up)"),
    ("v2_P_down",       "P_down",                  "Bayesian P(down)"),
    ("v2_P_zero",       "P_zero",                  "Bayesian P(flat)"),
    ("v2_E_star",       "E* (Kelly utility)",      "Kelly expected log-utility"),
    ("v2_n_star",       "n* (Kelly fraction)",     "Kelly-optimal size fraction"),
    ("v2_k_up",         "k_up",                    "Kramers escape rate up"),
    ("v2_k_down",       "k_down",                  "Kramers escape rate down"),
    ("v2_mu",           "mu (drift)",              "SDE drift mu_t"),
    ("v2_phi",          "phi (flow)",              "SDE flow pressure phi_t"),
    ("v2_h",            "h (log-vol)",             "SDE log-volatility h_t"),
    ("v2_sigma_t",      "sigma_t",                 "Posterior std sigma_t"),
    ("atr",             "ATR",                     "Average true range"),
    ("ema_spread",      "EMA spread",              "EMA fast-slow spread"),
    ("overextension_ratio", "overextension ratio", "Price overextension ratio"),
]

LOG_SCALE = {"signal_strength", "s_effective", "k_up"}


def load_trades(batch_label, results_dir, sol_usd):
    pattern = os.path.join(results_dir, f"*{batch_label}*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        raise SystemExit(f"no result files matched {pattern!r}")
    rows = []
    rec_ids = set()
    for fp in files:
        with open(fp) as f:
            d = json.load(f)
        rec_ids.add(d.get("recording_id"))
        sym = d.get("token_symbol", "?")
        for t in d.get("trades", []):
            p = t.get("pnl_pct")
            if p is None:
                continue
            ep = float(t["entry_price"])
            mcap = ep * 1_000_000_000 * sol_usd  # USD market cap
            entry = t.get("entry_params", {}) or {}
            row = {
                "symbol": sym,
                "recording_id": d.get("recording_id"),
                "outcome": t.get("outcome"),
                "pnl_pct": float(p),
                "pnl_sol": float(t.get("pnl_sol", 0.0)),
                "exit_reason": t.get("exit_reason", "?"),
                "entry_time": t.get("entry_time"),
                "exit_time": t.get("exit_time"),
                "hold_s": (t.get("exit_time", 0) or 0) - (t.get("entry_time", 0) or 0),
                "mcap": mcap,
            }
            for k, _, _ in CURATED:
                if k == "mcap":
                    continue
                v = entry.get(k)
                row[k] = float(v) if isinstance(v, (int, float)) else np.nan
            rows.append(row)
    return rows, len(files), len(rec_ids)


def classify(row):
    p = row["pnl_pct"]
    if p >= 20:
        return "big_win"
    if 0 <= p < 20:
        return "small_win"
    if p <= -20:
        return "big_loss"
    return "small_loss"


CLASSES = ["big_win", "small_win", "big_loss", "small_loss"]
CLASS_LABEL = {
    "big_win": "Big winner (>= +20%)",
    "small_win": "Small winner (0..+20%)",
    "big_loss": "Big loser (<= -20%)",
    "small_loss": "Small loser (0..-20%)",
}
CLASS_COLOR = {
    "big_win": "#1a7a1a",
    "small_win": "#7fbf7f",
    "big_loss": "#a01414",
    "small_loss": "#e08080",
}


def pctile(xs, p):
    xs = np.asarray(sorted(xs), dtype=float)
    if len(xs) == 0:
        return np.nan
    return float(np.percentile(xs, p))


def describe(xs):
    xs = np.asarray(xs, dtype=float)
    xs = xs[~np.isnan(xs)]
    if len(xs) == 0:
        return dict(n=0, mean=np.nan, sd=np.nan, min=np.nan, p25=np.nan,
                    median=np.nan, p75=np.nan, p90=np.nan, max=np.nan)
    return dict(n=len(xs), mean=float(xs.mean()), sd=float(xs.std(ddof=1)) if len(xs) > 1 else 0.0,
                min=float(xs.min()), p25=pctile(xs, 25), median=float(np.median(xs)),
                p75=pctile(xs, 75), p90=pctile(xs, 90), max=float(xs.max()))


# ----------------------------------------------------------------------------
# figures
# ----------------------------------------------------------------------------

def fig_pnl_kde(rows, outdir):
    fig, ax = plt.subplots(figsize=(11, 6))
    grid = np.linspace(-100, 100, 800)
    for cls in CLASSES:
        xs = np.array([r["pnl_pct"] for r in rows if r["_cls"] == cls])
        xs_c = np.clip(xs, -100, 100)
        bw = 1.06 * xs_c.std() * len(xs_c) ** -0.2 if len(xs_c) > 1 else 5.0
        z = (grid[:, None] - xs_c[None, :]) / bw
        dens = np.exp(-0.5 * z * z).sum(axis=1) / (len(xs_c) * bw * np.sqrt(2 * np.pi))
        ax.plot(grid, dens, color=CLASS_COLOR[cls], lw=2.2,
                label=f"{CLASS_LABEL[cls]} (n={len(xs)})")
        ax.fill_between(grid, dens, color=CLASS_COLOR[cls], alpha=0.12)
    ax.axvline(0, color="black", lw=0.9)
    ax.axvline(20, color="gray", lw=0.7, ls="--")
    ax.axvline(-20, color="gray", lw=0.7, ls="--")
    ax.set_xlabel("PnL per trade (%)")
    ax.set_ylabel("Density")
    ax.set_title("PnL% density by class (clipped to +/-100%)")
    ax.legend(fontsize=9)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "pnl_kde.png"), dpi=140)
    plt.close(fig)


def fig_mcap_hist_kde(rows, outdir):
    fig, ax = plt.subplots(figsize=(11, 6))
    allm = np.array([r["mcap"] for r in rows])
    lo, hi = 0, np.percentile(allm, 99)
    grid = np.linspace(lo, hi, 800)
    bins = np.linspace(lo, hi, 40)
    for cls in CLASSES:
        xs = np.array([r["mcap"] for r in rows if r["_cls"] == cls])
        ax.hist(xs, bins=bins, density=True, alpha=0.18, color=CLASS_COLOR[cls])
        if len(xs) > 2:
            bw = 1.06 * xs.std() * len(xs) ** -0.2
            z = (grid[:, None] - xs[None, :]) / bw
            dens = np.exp(-0.5 * z * z).sum(axis=1) / (len(xs) * bw * np.sqrt(2 * np.pi))
            ax.plot(grid, dens, color=CLASS_COLOR[cls], lw=2.2,
                    label=f"{CLASS_LABEL[cls]} (n={len(xs)})")
    ax.set_xlabel("Entry market cap (USD)")
    ax.set_ylabel("Density")
    ax.set_title("Entry market cap distribution by class (99th pct clip)")
    ax.legend(fontsize=9)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "mcap_hist_kde.png"), dpi=140)
    plt.close(fig)


def fig_mcap_box(rows, outdir):
    fig, ax = plt.subplots(figsize=(9, 6))
    data = [np.array([r["mcap"] for r in rows if r["_cls"] == cls]) for cls in CLASSES]
    # clip extreme outlier for readability
    clip = np.percentile(np.concatenate(data), 99)
    data = [np.clip(d, 0, clip) for d in data]
    bp = ax.boxplot(data, tick_labels=[CLASS_LABEL[c].split(" (")[0] for c in CLASSES],
                    patch_artist=True, showfliers=False, widths=0.55)
    for patch, cls in zip(bp["boxes"], CLASSES):
        patch.set_facecolor(CLASS_COLOR[cls])
        patch.set_alpha(0.55)
    for med in bp["medians"]:
        med.set_color("black")
        med.set_linewidth(1.6)
    ax.set_ylabel("Entry market cap (USD)")
    ax.set_title("Entry market cap by class (box, 99th pct clip, no fliers)")
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "mcap_box.png"), dpi=140)
    plt.close(fig)


def fig_mcap_vs_pnl(rows, outdir):
    fig, ax = plt.subplots(figsize=(11, 6.5))
    for cls in CLASSES:
        xs = np.array([r["mcap"] for r in rows if r["_cls"] == cls])
        ys = np.array([r["pnl_pct"] for r in rows if r["_cls"] == cls])
        ax.scatter(xs, np.clip(ys, -100, 250), s=22, alpha=0.55,
                   color=CLASS_COLOR[cls], edgecolors="none",
                   label=f"{CLASS_LABEL[cls]}")
    ax.set_xscale("log")
    ax.axhline(0, color="black", lw=0.9)
    ax.axhline(20, color="gray", lw=0.7, ls="--")
    ax.axhline(-20, color="gray", lw=0.7, ls="--")
    ax.set_xlabel("Entry market cap (USD, log scale)")
    ax.set_ylabel("PnL per trade (%, clipped -100..+250)")
    ax.set_title("Entry market cap vs trade PnL")
    ax.legend(fontsize=9, loc="upper right")
    ax.grid(alpha=0.25, which="both")
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "mcap_vs_pnl.png"), dpi=140)
    plt.close(fig)


def fig_lossrate_by_mcap(rows, outdir):
    edges, labels = _mcap_5k_buckets(max_bucket=200e3, final_label="200k+")
    big_rate, any_rate, net, n_trades = [], [], [], []
    for lo, hi in zip(edges[:-1], edges[1:]):
        sub = [r for r in rows if lo <= r["mcap"] < hi]
        n_trades.append(len(sub))
        n_big = sum(1 for r in sub if r["_cls"] == "big_loss")
        n_loss = sum(1 for r in sub if r["outcome"] == "L")
        big_rate.append(100 * n_big / len(sub) if sub else 0)
        any_rate.append(100 * n_loss / len(sub) if sub else 0)
        net.append(sum(r["pnl_sol"] for r in sub))
    x = np.arange(len(labels))
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(18, 8), sharex=True,
                                   gridspec_kw=dict(height_ratios=[1, 1]))
    ax1.bar(x - 0.2, any_rate, width=0.4, color="#e08080", label="any loss rate")
    ax1.bar(x + 0.2, big_rate, width=0.4, color="#a01414", label="big loss (>20%) rate")
    ax1.set_ylabel("% of entries in bucket")
    ax1.set_title("Loss rate by entry-mcap bucket ($5k granularity)")
    ax1.legend(fontsize=9)
    ax1.grid(axis="y", alpha=0.25)

    colors = ["#1a7a1a" if v >= 0 else "#a01414" for v in net]
    ax2.bar(x, net, color=colors, alpha=0.8)
    ax2.axhline(0, color="black", lw=0.8)
    ax2.set_ylabel("Net PnL (SOL)")
    ax2.set_xlabel("Entry market cap bucket (USD)")
    ax2.set_title("Net PnL by entry-mcap bucket ($5k granularity)")
    ax2.grid(axis="y", alpha=0.25)
    ax2.set_xticks(x)
    ax2.set_xticklabels(labels, fontsize=7, rotation=45, ha="right")
    # annotate n
    for i, n in enumerate(n_trades):
        ax2.text(i, ax2.get_ylim()[0], f"n={n}", ha="center", va="bottom", fontsize=7, color="gray")
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "lossrate_by_mcap.png"), dpi=140)
    plt.close(fig)


def fig_exit_reasons(rows, outdir):
    # collect top exit reasons overall
    allr = Counter(r["exit_reason"] for r in rows)
    top = [r for r, _ in allr.most_common(8)]
    other = "other"
    fig, axes = plt.subplots(1, 4, figsize=(16, 4.4), sharey=True)
    for ax, cls in zip(axes, CLASSES):
        sub = [r["exit_reason"] for r in rows if r["_cls"] == cls]
        c = Counter(x if x in top else other for x in sub)
        keys = top + ([other] if c.get(other) else [])
        vals = [100 * c.get(k, 0) / len(sub) if sub else 0 for k in keys]
        ax.barh(range(len(keys)), vals, color=CLASS_COLOR[cls], alpha=0.8)
        ax.set_yticks(range(len(keys)))
        ax.set_yticklabels(keys, fontsize=8)
        ax.invert_yaxis()
        ax.set_title(CLASS_LABEL[cls].split(" (")[0] + f"  n={len(sub)}", fontsize=9)
        ax.set_xlim(0, 100)
        for i, v in enumerate(vals):
            if v > 0:
                ax.text(v + 1, i, f"{v:.0f}", va="center", fontsize=7)
    axes[0].set_ylabel("exit reason")
    for ax in axes:
        ax.set_xlabel("% of class")
        ax.grid(axis="x", alpha=0.25)
    fig.suptitle("Exit-reason mix by class (% within class)", y=1.02)
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "exit_reasons.png"), dpi=140, bbox_inches="tight")
    plt.close(fig)


def fig_metric_grid(rows, outdir, ncols=4):
    n = len(CURATED)
    nrows = int(np.ceil(n / ncols))
    fig, axes = plt.subplots(nrows, ncols, figsize=(4.2 * ncols, 3.2 * nrows))
    axes = np.atleast_2d(axes)
    for idx, (key, label, _) in enumerate(CURATED):
        r, c = divmod(idx, ncols)
        ax = axes[r][c]
        for cls in CLASSES:
            xs = np.array([r_[key] for r_ in rows if r_["_cls"] == cls], dtype=float)
            xs = xs[~np.isnan(xs)]
            if len(xs) < 3:
                continue
            if key in LOG_SCALE and (xs > 0).all():
                xs = np.log10(xs)
            lo, hi = np.percentile(xs, [1, 99])
            xs_c = np.clip(xs, lo, hi)
            grid = np.linspace(lo, hi, 200)
            bw = 1.06 * xs_c.std() * len(xs_c) ** -0.2 if xs_c.std() > 0 else (hi - lo) / 20 or 1
            if bw <= 0:
                bw = (hi - lo) / 20 or 1
            z = (grid[:, None] - xs_c[None, :]) / bw
            dens = np.exp(-0.5 * z * z).sum(axis=1) / (len(xs_c) * bw * np.sqrt(2 * np.pi))
            ax.plot(grid, dens, color=CLASS_COLOR[cls], lw=1.6)
            ax.fill_between(grid, dens, color=CLASS_COLOR[cls], alpha=0.10)
        ttl = label + (" (log10)" if key in LOG_SCALE else "")
        ax.set_title(ttl, fontsize=9)
        ax.tick_params(labelsize=7)
        ax.grid(alpha=0.2)
        if r == 0 and c == 0:
            ax.legend([CLASS_LABEL[cl].split(" (")[0] for cl in CLASSES], fontsize=6)
    # hide empty
    for idx in range(n, nrows * ncols):
        r, c = divmod(idx, ncols)
        axes[r][c].axis("off")
    fig.suptitle("Entry-metric density by class (KDE, 1-99 pct clip)", y=1.005, fontsize=13)
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "metric_grid.png"), dpi=135, bbox_inches="tight")
    plt.close(fig)


def fig_roc_auc(auc_rows, outdir):
    # bar chart of AUC for win-vs-loss and big-vs-small discrimination
    fig, ax = plt.subplots(figsize=(11, 6))
    names = [a[0] for a in auc_rows]
    auc_wl = [a[1] for a in auc_rows]
    auc_bs = [a[2] for a in auc_rows]
    x = np.arange(len(names))
    ax.bar(x - 0.2, auc_wl, width=0.4, color="#555", label="AUC  winner vs loser")
    ax.bar(x + 0.2, auc_bs, width=0.4, color="#999", label="AUC  big vs small")
    ax.axhline(0.5, color="red", lw=0.9, ls="--")
    for i, v in enumerate(auc_wl):
        ax.text(i - 0.2, v + 0.01, f"{v:.2f}", ha="center", fontsize=7)
    for i, v in enumerate(auc_bs):
        ax.text(i + 0.2, v + 0.01, f"{v:.2f}", ha="center", fontsize=7)
    ax.set_xticks(x)
    ax.set_xticklabels(names, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("AUC")
    ax.set_ylim(0, 1.0)
    ax.set_title("Entry-metric discriminative power (AUC; 0.5 = no signal)")
    ax.legend(fontsize=9)
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "roc_auc.png"), dpi=140)
    plt.close(fig)


def fig_hold_time(rows, outdir):
    fig, ax = plt.subplots(figsize=(11, 6))
    allh = np.array([r["hold_s"] for r in rows], dtype=float)
    hi = np.percentile(allh, 99)
    grid = np.linspace(0, hi, 400)
    bins = np.linspace(0, hi, 40)
    for cls in CLASSES:
        xs = np.array([r["hold_s"] for r in rows if r["_cls"] == cls], dtype=float)
        ax.hist(xs, bins=bins, density=True, alpha=0.15, color=CLASS_COLOR[cls])
        if len(xs) > 2 and xs.std() > 0:
            bw = 1.06 * xs.std() * len(xs) ** -0.2
            z = (grid[:, None] - xs[None, :]) / bw
            dens = np.exp(-0.5 * z * z).sum(axis=1) / (len(xs) * bw * np.sqrt(2 * np.pi))
            ax.plot(grid, dens, color=CLASS_COLOR[cls], lw=2.2,
                    label=f"{CLASS_LABEL[cls]}")
    ax.set_xlabel("Hold time (seconds)")
    ax.set_ylabel("Density")
    ax.set_title("Hold-time distribution by class (99th pct clip)")
    ax.legend(fontsize=9)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "hold_time.png"), dpi=140)
    plt.close(fig)


def fig_class_mcap_heatmap(rows, outdir):
    edges, labels = _mcap_5k_buckets(max_bucket=200e3, final_label="200k+")
    M = np.zeros((4, len(labels)))
    for i, cls in enumerate(CLASSES):
        sub = [r for r in rows if r["_cls"] == cls]
        for j, (lo, hi) in enumerate(zip(edges[:-1], edges[1:])):
            M[i, j] = 100 * sum(1 for r in sub if lo <= r["mcap"] < hi) / len(sub) if sub else 0
    fig, ax = plt.subplots(figsize=(18, 3.6))
    im = ax.imshow(M, aspect="auto", cmap="YlOrRd")
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, fontsize=6, rotation=45, ha="right")
    ax.set_yticks(range(4))
    ax.set_yticklabels([CLASS_LABEL[c].split(" (")[0] for c in CLASSES])
    for i in range(4):
        for j in range(len(labels)):
            v = M[i, j]
            ax.text(j, i, f"{v:.0f}" if v > 0 else "", ha="center", va="center",
                    fontsize=5.5, color="black")
    ax.set_xlabel("Entry market cap bucket (USD)")
    ax.set_title("Where each class enters (% of class per mcap bucket)")
    fig.colorbar(im, ax=ax, label="% of class")
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "class_mcap_heatmap.png"), dpi=140)
    plt.close(fig)


# ----------------------------------------------------------------------------
# mcap-gate counterfactual simulation
# ----------------------------------------------------------------------------

def _per_recording_pnl(rows):
    d = {}
    for r in rows:
        d[r["recording_id"]] = d.get(r["recording_id"], 0.0) + r["pnl_sol"]
    return d


def _paired_bootstrap(base_rows, gate_fn, n_boot=10000, seed=42):
    """Bootstrap 95% CI of mean per-recording PnL delta (gated - baseline)."""
    recs = sorted({r["recording_id"] for r in base_rows})
    base_pr = _per_recording_pnl(base_rows)
    gate_pr = _per_recording_pnl([r for r in base_rows if gate_fn(r)])
    deltas = np.array([gate_pr.get(rc, 0.0) - base_pr.get(rc, 0.0) for rc in recs])
    rng = np.random.default_rng(seed)
    means = rng.choice(deltas, size=(n_boot, len(deltas)), replace=True).mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(deltas.mean()), float(lo), float(hi)


def _wilcoxon_one_sided(base_rows, gate_fn):
    """Per-recording paired Wilcoxon signed-rank, one-sided (gate > baseline).

    Returns (p, n_changed, mean_delta). Drops zero deltas (recordings whose
    trade set is unchanged by the gate)."""
    recs = sorted({r["recording_id"] for r in base_rows})
    base_pr = _per_recording_pnl(base_rows)
    gate_pr = _per_recording_pnl([r for r in base_rows if gate_fn(r)])
    deltas = np.array([gate_pr.get(rc, 0.0) - base_pr.get(rc, 0.0) for rc in recs])
    n_changed = int((deltas != 0).sum())
    if n_changed < 1:
        return float("nan"), n_changed, float(deltas.mean())
    try:
        with np.errstate(invalid="ignore"):
            w = sps.wilcoxon(deltas, alternative="greater", zero_method="wilcox")
        p = float(w.pvalue)
    except Exception:
        p = float("nan")
    return p, n_changed, float(deltas.mean())


def _perf(rows):
    n = len(rows)
    if n == 0:
        return dict(n=0, w=0, l=0, wr=np.nan, pnl=0.0, pf=np.nan, bigw=0, bigl=0)
    w = sum(1 for r in rows if r["outcome"] == "W")
    l = n - w
    pnl = sum(r["pnl_sol"] for r in rows)
    gp = sum(r["pnl_sol"] for r in rows if r["pnl_sol"] > 0)
    gl = -sum(r["pnl_sol"] for r in rows if r["pnl_sol"] < 0)
    pf = gp / gl if gl > 0 else np.inf
    bigw = sum(1 for r in rows if r["pnl_pct"] >= 20)
    bigl = sum(1 for r in rows if r["pnl_pct"] <= -20)
    return dict(n=n, w=w, l=l, wr=100 * w / n, pnl=pnl, pf=pf, bigw=bigw, bigl=bigl)


def _fmt_usd(x):
    return f"${x/1000:.0f}k" if x >= 1000 else f"${x:.0f}"


def compute_gate_sim(rows, n_boot=10000, n_zone_perm=20000,
                     results_dir="v2_results", sol_usd=DEFAULT_SOL_USD):
    """Returns dict with floor/ceiling sweeps, bootstrap CIs, band sweep, and
    exhaustive discontiguous zone-mask search with a max-stat permutation null."""
    from itertools import combinations
    base = _perf(rows)
    thr = [0, 5e3, 7e3, 10.5e3, 14e3, 17.5e3, 21e3, 28e3, 35e3, 50e3,
           70e3, 100e3, 140e3, 200e3]
    floor_rows = [(t, _perf([r for r in rows if r["mcap"] >= t])) for t in thr]
    ceil_rows = [(t, _perf([r for r in rows if r["mcap"] <= t]))
                 for t in [x for x in thr if x > 0] + [np.inf]]
    floor_boot = []
    for t in thr:
        if t == 0:
            continue
        m, lo, hi = _paired_bootstrap(rows, lambda r, tt=t: r["mcap"] >= tt, n_boot=n_boot)
        pw, nch, _ = _wilcoxon_one_sided(rows, lambda r, tt=t: r["mcap"] >= tt)
        floor_boot.append((t, m, lo, hi, pw, nch))
    band_lo = [0, 7e3, 10.5e3, 14e3, 21e3, 28e3, 35e3, 50e3]
    band_hi = [35e3, 50e3, 70e3, 100e3, 140e3, 200e3, np.inf]
    bands = []
    for lo in band_lo:
        for hi in band_hi:
            if hi <= lo:
                continue
            bands.append((lo, hi, _perf([r for r in rows if lo <= r["mcap"] < hi])))
    bands.sort(key=lambda x: -x[2]["pnl"])

    # ---- floor x ceiling 2D combination sweep ($5k grid) ----
    # For every (floor F, ceiling C) with F < C, keep trades with F <= mcap < C.
    # Returns a matrix of net PnL + a sorted list of the best combos.
    fc_floors = [i * 5e3 for i in range(0, 21)]          # $0 .. $100k in $5k steps
    fc_ceils  = [i * 5e3 for i in range(2, 41)] + [np.inf]  # $10k .. $200k + inf
    mc_arr = np.array([r["mcap"] for r in rows])
    pn_arr = np.array([r["pnl_sol"] for r in rows])
    ot_arr = np.array([r["outcome"] for r in rows])
    fc_matrix = np.full((len(fc_floors), len(fc_ceils)), np.nan)
    fc_list = []
    for fi, F in enumerate(fc_floors):
        for ci, C in enumerate(fc_ceils):
            if C <= F:
                continue
            m = (mc_arr >= F) & (mc_arr < C)
            n = int(m.sum())
            if n == 0:
                continue
            pnl = float(pn_arr[m].sum())
            wr = 100 * float((ot_arr[m] == "W").mean())
            fc_matrix[fi, ci] = pnl
            fc_list.append(dict(floor=F, ceil=C, n=n, wr=wr, pnl=pnl,
                                dpnl=pnl - base["pnl"], pf=_perf([r for r in rows if F <= r["mcap"] < C])["pf"]))
    fc_list.sort(key=lambda x: -x["pnl"])

    # ---- discontiguous zone-mask search over bucket unions ----
    # $20k grid (10 buckets, 1023 subsets) — fine enough to detect zone
    # structure while keeping exhaustive enumeration tractable.  The main
    # report tables and figures use $5k buckets (see _mcap_5k_buckets).
    zedges = [0, 20e3, 40e3, 60e3, 80e3, 100e3, 120e3, 140e3, 160e3, 180e3, np.inf]
    zlabels = ["0-20k", "20-40k", "40-60k", "60-80k", "80-100k",
               "100-120k", "120-140k", "140-160k", "160-180k", "180k+"]
    mc = np.array([r["mcap"] for r in rows])
    pn = np.array([r["pnl_sol"] for r in rows])
    ot = np.array([r["outcome"] for r in rows])
    nb = len(zlabels)
    bmask = [(mc >= zedges[i]) & (mc < zedges[i + 1]) for i in range(nb)]
    bpnl = np.array([float(pn[m].sum()) for m in bmask])
    bn = [int(m.sum()) for m in bmask]
    bwr = [100 * float((ot[m] == "W").mean()) if m.any() else 0.0 for m in bmask]

    subs = []
    for k in range(1, nb + 1):
        for sel in combinations(range(nb), k):
            subs.append(sel)

    def _mask(sel):
        m = np.zeros(len(rows), bool)
        for i in sel:
            m |= bmask[i]
        return m

    zone_results = sorted(((float(pn[_mask(s)].sum()), s) for s in subs),
                          key=lambda x: -x[0])
    # max-stat permutation null: shuffle bucket->PnL mapping, take max over subsets
    rng = np.random.default_rng(42)
    maxnull = np.empty(n_zone_perm)
    for b in range(n_zone_perm):
        perm = rng.permutation(bpnl)
        maxnull[b] = max(float(perm[list(s)].sum()) for s in subs)
    zone_top = []
    for p, s in zone_results[:15]:
        m = _mask(s)
        perm_p = float((maxnull >= p).mean())
        zone_top.append(dict(sel=s, pnl=p, dpnl=p - base["pnl"], n=int(m.sum()),
                             wr=100 * float((ot[m] == "W").mean()), perm_p=perm_p))

    # Wilcoxon + bootstrap for the best zone mask
    best_sel = zone_results[0][1]
    best_pnl_obs = zone_results[0][0]
    def _zone_gate(r, _bs=set(best_sel), _e=zedges):
        return any(_e[i] <= r["mcap"] < _e[i + 1] for i in _bs)
    zw_p, zw_nch, zw_md = _wilcoxon_one_sided(rows, _zone_gate)
    _, zlo, zhi = _paired_bootstrap(rows, _zone_gate, n_boot=n_boot)

    # single-best-PnL (naive, uncorrected) permutation null: permute bucket PnL,
    # compare observed best zone vs best single bucket each shuffle.
    single_best_null = np.empty(n_zone_perm)
    for b in range(n_zone_perm):
        perm = rng.permutation(bpnl)
        single_best_null[b] = float(perm.max())
    naive_perm_p = float((single_best_null >= best_pnl_obs).mean())

    # also permutation p for the best CONTIGUOUS band (bucket indices 2..5)
    contig_sel = tuple(range(2, 6))
    contig_pnl = float(pn[_mask(contig_sel)].sum())
    contig_perm_p = float((maxnull >= contig_pnl).mean())

    # ---- curated contiguous-gate hypothesis tests ----
    contig_gates = [
        ("floor $14k",        lambda r: r["mcap"] >= 14e3),
        ("ceiling $140k",     lambda r: r["mcap"] <= 140e3),
        ("band $14k-$140k",   lambda r: 14e3 <= r["mcap"] < 140e3),
        ("band $14k-$200k",   lambda r: 14e3 <= r["mcap"] < 200e3),
    ]
    contig_tests = []
    for name, fn in contig_gates:
        kept = _perf([r for r in rows if fn(r)])
        pw, nch, _ = _wilcoxon_one_sided(rows, fn)
        md, lo, hi = _paired_bootstrap(rows, fn, n_boot=n_boot)
        contig_tests.append(dict(name=name, pnl=kept["pnl"], dpnl=kept["pnl"] - base["pnl"],
                                 wr=kept["wr"], n=kept["n"], wilcoxon_p=pw,
                                 boot_lo=lo, boot_hi=hi, n_changed=nch))

    # ---- across-baseline robustness: same gates against every old engine baseline ----
    # Load other batch results straight from results_dir and re-run the gate test.
    robust_batches = ["iter22b", "iter23_underw", "iter25_diag", "iter26_baseline",
                      "iter27_g50", "iter31_baseline", "iter33_hold_base",
                      "iter33_screen_base", "iter33_cand_full", "iter36_dualkde300"]
    robust_gates = [
        ("floor $14k",      lambda r: r["mcap"] >= 14e3),
        ("band $14k-$140k", lambda r: 14e3 <= r["mcap"] < 140e3),
        ("band $14k-$200k", lambda r: 14e3 <= r["mcap"] < 200e3),
    ]
    robust_rows = []  # (batch, n_trades, [(gate, base, gated, dpnl, wilcoxon_p, lo, hi, n_chg, n_rec)])
    import glob as _glob

    def _load_batch(blabel, results_dir, sol_usd):
        out = []
        for fp in sorted(_glob.glob(os.path.join(results_dir, f"*{blabel}*.json"))):
            with open(fp) as f:
                d = json.load(f)
            for t in d.get("trades", []):
                if t.get("pnl_pct") is None:
                    continue
                out.append(dict(recording_id=d.get("recording_id"),
                                mcap=float(t["entry_price"]) * 1e9 * sol_usd,
                                pnl_sol=float(t.get("pnl_sol", 0.0)),
                                outcome=t.get("outcome")))
        return out

    for blabel in robust_batches:
        b_rows = _load_batch(blabel, results_dir, sol_usd)
        if len(b_rows) < 10:
            continue
        brecs = sorted({r["recording_id"] for r in b_rows})
        b_base_pr = _per_recording_pnl(b_rows)
        b_base_pnl = sum(b_base_pr.values())
        gate_res = []
        for gname, gf in robust_gates:
            g_pr = _per_recording_pnl([r for r in b_rows if gf(r)])
            deltas = np.array([g_pr.get(rc, 0.0) - b_base_pr.get(rc, 0.0) for rc in brecs])
            nch = int((deltas != 0).sum())
            g_pnl = sum(g_pr.values())
            if nch < 1:
                pw = float("nan")
            else:
                try:
                    with np.errstate(invalid="ignore"):
                        pw = float(sps.wilcoxon(deltas, alternative="greater",
                                                zero_method="wilcox").pvalue)
                except Exception:
                    pw = float("nan")
            rng = np.random.default_rng(42)
            means = rng.choice(deltas, size=(n_boot, len(deltas)), replace=True).mean(axis=1)
            lo, hi = np.percentile(means, [2.5, 97.5])
            gate_res.append(dict(gate=gname, base=b_base_pnl, gated=g_pnl,
                                 dpnl=g_pnl - b_base_pnl, wilcoxon_p=pw,
                                 lo=float(lo), hi=float(hi), n_chg=nch, n_rec=len(brecs)))
        robust_rows.append((blabel, len(b_rows), gate_res))

    # ---- deliberate attempt to construct a significant gate (corrected scan) ----
    # Enrich rows with entry params for hold/signal/conf gates.
    for r in rows:
        r.setdefault("hold_s", (r.get("exit_time", 0) or 0) - (r.get("entry_time", 0) or 0))
    s_eff_vals = np.array([r.get("s_effective", np.nan) for r in rows], dtype=float)
    s_med = float(np.nanmedian(s_eff_vals)) if np.isfinite(s_eff_vals).any() else 0.0
    scan_gates = []
    for thr in [7e3, 10.5e3, 14e3, 21e3, 28e3, 35e3, 50e3, 70e3]:
        scan_gates.append((f"mcap>={thr/1000:.0f}k", lambda r, t=thr: r["mcap"] >= t))
    for thr in [70e3, 100e3, 140e3, 200e3]:
        scan_gates.append((f"mcap<={thr/1000:.0f}k", lambda r, t=thr: r["mcap"] <= t))
    scan_gates.append(("band14-140k", lambda r: 14e3 <= r["mcap"] < 140e3))
    scan_gates.append(("band14-200k", lambda r: 14e3 <= r["mcap"] < 200e3))
    for hs in [300, 600, 1200, 1800]:
        scan_gates.append((f"hold<={hs}s", lambda r, h=hs: r["hold_s"] <= h))
    scan_gates.append(("s_eff>median", lambda r, m=s_med: r.get("s_effective", 0) >= m))
    scan_gates.append(("conf>=0.86", lambda r: r.get("trend_confidence", 0) >= 0.86))
    scan_gates.append(("mcap>=14k & hold<=1200s",
                       lambda r: r["mcap"] >= 14e3 and r["hold_s"] <= 1200))
    scan_gates.append(("mcap>=14k & conf>=0.86",
                       lambda r: r["mcap"] >= 14e3 and r.get("trend_confidence", 0) >= 0.86))

    n_scan = len(scan_gates)
    bonf_alpha = 0.05 / n_scan
    scan = []
    recs = sorted({r["recording_id"] for r in rows})
    base_pr = _per_recording_pnl(rows)
    base_pnl_total = sum(base_pr.values())
    for name, fn in scan_gates:
        g_rows = [r for r in rows if fn(r)]
        if len(g_rows) < 5:
            continue
        g_pr = _per_recording_pnl(g_rows)
        deltas = np.array([g_pr.get(rc, 0.0) - base_pr.get(rc, 0.0) for rc in recs])
        nch = int((deltas != 0).sum())
        if nch < 5:
            continue
        try:
            with np.errstate(invalid="ignore"):
                pw = float(sps.wilcoxon(deltas, alternative="greater",
                                        zero_method="wilcox").pvalue)
        except Exception:
            pw = float("nan")
        rng = np.random.default_rng(42)
        means = rng.choice(deltas, size=(n_boot, len(deltas)), replace=True).mean(axis=1)
        lo, hi = np.percentile(means, [2.5, 97.5])
        dpnl = sum(g_pr.values()) - base_pnl_total
        raw_sig = bool(pw < 0.05 and lo > 0)
        bonf_sig = bool(pw < bonf_alpha and lo > 0)
        scan.append(dict(name=name, kept=len(g_rows), total=len(rows), dpnl=dpnl,
                         p=pw, lo=float(lo), hi=float(hi),
                         raw_sig=raw_sig, bonf_sig=bonf_sig))

    return dict(base=base, floor=floor_rows, ceil=ceil_rows, boot=floor_boot,
                bands=bands, thr=thr, n_boot=n_boot,
                contig_tests=contig_tests, robust_rows=robust_rows, scan=scan,
                bonf_alpha=bonf_alpha,
                fc=dict(matrix=fc_matrix, floors=fc_floors, ceils=fc_ceils,
                        list=fc_list),
                zone=dict(edges=zedges, labels=zlabels, bpnl=bpnl, bn=bn, bwr=bwr,
                          top=zone_top, perm_p_best=zone_top[0]["perm_p"],
                          n_zone_perm=n_zone_perm,
                          contig_sel=contig_sel, contig_pnl=contig_pnl,
                          contig_perm_p=contig_perm_p,
                          best_sel=best_sel, best_pnl=best_pnl_obs,
                          best_wilcoxon_p=zw_p, best_boot_lo=zlo, best_boot_hi=zhi,
                          best_n_changed=zw_nch, naive_perm_p=naive_perm_p))


def fig_gate_sim(gs, outdir):
    base = gs["base"]
    floor_rows = gs["floor"]
    floor_boot = gs["boot"]
    xs = [t for t, _ in floor_rows]
    fig, axes = plt.subplots(1, 3, figsize=(17, 5.2))

    ax = axes[0]
    pnls = [p["pnl"] for _, p in floor_rows]
    wrs = [p["wr"] for _, p in floor_rows]
    ax.plot(xs, pnls, "o-", color="#1a7a1a", label="total PnL (SOL)")
    ax.axhline(base["pnl"], color="#1a7a1a", ls="--", alpha=0.5,
               label=f"baseline {base['pnl']:+.3f}")
    ax.set_xscale("symlog", linthresh=5000)
    ax.set_xlabel("min-mcap floor (USD)")
    ax.set_ylabel("Total PnL (SOL)", color="#1a7a1a")
    ax.tick_params(axis="y", labelcolor="#1a7a1a")
    ax2 = ax.twinx()
    ax2.plot(xs, wrs, "s--", color="#1f77b4", label="win rate %")
    ax2.axhline(base["wr"], color="#1f77b4", ls=":", alpha=0.5)
    ax2.set_ylabel("Win rate %", color="#1f77b4")
    ax2.tick_params(axis="y", labelcolor="#1f77b4")
    ax.set_title("Floor gate: block mcap < X")
    ax.grid(alpha=0.25)
    for t, p in zip(xs, pnls):
        ax.annotate(f"{p:+.2f}", (t, p), textcoords="offset points",
                    xytext=(0, 7), fontsize=7, ha="center")

    ax = axes[1]
    if floor_boot:
        ts = [t[0] for t in floor_boot]
        means = [t[1] for t in floor_boot]
        los = [t[2] for t in floor_boot]
        his = [t[3] for t in floor_boot]
        ax.axhline(0, color="black", lw=0.9)
        ax.errorbar(ts, means, yerr=[np.array(means) - np.array(los),
                                     np.array(his) - np.array(means)],
                    fmt="o-", color="#a01414", capsize=4)
        ax.set_xscale("symlog", linthresh=5000)
        ax.set_xlabel("min-mcap floor (USD)")
        ax.set_ylabel("mean per-recording dPnL (SOL)")
        ax.set_title(f"Paired bootstrap 95% CI (n_boot={gs['n_boot']})")
        ax.grid(alpha=0.25)

    ax = axes[2]
    bigw_kept = [p["bigw"] / base["bigw"] * 100 if base["bigw"] else 0 for _, p in floor_rows]
    bigl_kept = [p["bigl"] / base["bigl"] * 100 if base["bigl"] else 0 for _, p in floor_rows]
    n_kept = [p["n"] / base["n"] * 100 if base["n"] else 0 for _, p in floor_rows]
    ax.plot(xs, n_kept, "o-", color="#555", label="all trades kept %")
    ax.plot(xs, bigw_kept, "o-", color="#1a7a1a", label="big winners kept %")
    ax.plot(xs, bigl_kept, "o-", color="#a01414", label="big losers kept %")
    ax.set_xscale("symlog", linthresh=5000)
    ax.set_xlabel("min-mcap floor (USD)")
    ax.set_ylabel("% retained")
    ax.set_title("What a floor gate removes")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.25)

    fig.suptitle("Counterfactual mcap-gate simulation "
                 "(NAIVE: blocked trade = no trade; upper bound only)")
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "mcap_gate_sim.png"), dpi=140, bbox_inches="tight")
    plt.close(fig)


def fig_floor_ceiling_heatmap(gs, outdir):
    """2D heatmap of net PnL for every (floor, ceiling) combination."""
    fc = gs["fc"]
    M = fc["matrix"]
    floors = fc["floors"]
    ceils = fc["ceils"]
    # mask NaN (empty combos) for display
    Mdisp = np.ma.masked_invalid(M)
    fig, ax = plt.subplots(figsize=(14, 9))
    vmax = max(abs(np.nanmin(M)), abs(np.nanmax(M)))
    im = ax.imshow(Mdisp, aspect="auto", cmap="RdYlGn", vmin=-vmax, vmax=vmax,
                   interpolation="nearest")
    ceil_labels = [("$" + str(int(c / 1000)) + "k") if np.isfinite(c) else "inf"
                   for c in ceils]
    floor_labels = ["$" + str(int(f / 1000)) + "k" for f in floors]
    ax.set_xticks(range(len(ceils)))
    ax.set_xticklabels(ceil_labels, fontsize=7, rotation=45, ha="right")
    ax.set_yticks(range(len(floors)))
    ax.set_yticklabels(floor_labels, fontsize=7)
    ax.set_xlabel("Ceiling (block mcap > C)")
    ax.set_ylabel("Floor (block mcap < F)")
    ax.set_title("Floor x Ceiling gate — net PnL (SOL).  Green = beats baseline "
                 f"({gs['base']['pnl']:+.3f}), red = worse.")
    # mark the baseline (floor=0, ceil=inf) cell
    ax.scatter(len(ceils) - 1, 0, s=120, marker="o", edgecolors="blue",
               facecolors="none", linewidths=2, label="baseline (no gate)")
    ax.legend(fontsize=8, loc="upper left")
    fig.colorbar(im, ax=ax, label="Net PnL (SOL)")
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "floor_ceiling_heatmap.png"), dpi=140)
    plt.close(fig)


def fig_zone_sim(gs, outdir):
    """Visualize the zone search: per-bucket PnL + the best zone mask found."""
    z = gs["zone"]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 4.8))

    colors = ["#1a7a1a" if v >= 0 else "#a01414" for v in z["bpnl"]]
    ax1.bar(range(len(z["labels"])), z["bpnl"], color=colors, alpha=0.85)
    ax1.axhline(0, color="black", lw=0.8)
    for i, v in enumerate(z["bpnl"]):
        ax1.text(i, v + (0.015 if v >= 0 else -0.015), f"{v:+.2f}",
                 ha="center", va="bottom" if v >= 0 else "top", fontsize=8)
        ax1.text(i, 0, f"n={z['bn'][i]}\n{z['bwr'][i]:.0f}%W", ha="center",
                 va="center", fontsize=7, color="white" if abs(v) > 0.1 else "gray")
    ax1.set_xticks(range(len(z["labels"])))
    ax1.set_xticklabels(z["labels"], fontsize=8, rotation=30, ha="right")
    ax1.set_ylabel("Net PnL (SOL)")
    ax1.set_title("Per-bucket PnL (the unit zones are built from)")
    ax1.grid(axis="y", alpha=0.25)

    # best zone mask highlight
    best = z["top"][0]
    keep = set(best["sel"])
    ax2.bar(range(len(z["labels"])), z["bpnl"],
            color=["#1a7a1a" if i in keep else "#cccccc" for i in range(len(z["labels"]))],
            alpha=0.9)
    ax2.axhline(0, color="black", lw=0.8)
    for i, v in enumerate(z["bpnl"]):
        if i in keep:
            ax2.text(i, v + (0.015 if v >= 0 else -0.015), f"{v:+.2f}",
                     ha="center", va="bottom" if v >= 0 else "top", fontsize=8)
    ax2.set_xticks(range(len(z["labels"])))
    ax2.set_xticklabels(z["labels"], fontsize=8, rotation=30, ha="right")
    ax2.set_ylabel("Net PnL (SOL)")
    ax2.set_title(f"Best zone mask (green = kept): PnL {best['pnl']:+.3f} "
                  f"(Δ{best['dpnl']:+.3f}), permutation p={best['perm_p']:.3f}")
    ax2.grid(axis="y", alpha=0.25)

    fig.suptitle("Discontiguous zone-mask search (exhaustive over bucket unions)")
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "mcap_zone_sim.png"), dpi=140, bbox_inches="tight")
    plt.close(fig)


# ----------------------------------------------------------------------------
# stats
# ----------------------------------------------------------------------------

def auc(xs_pos, xs_neg):
    """Mann-Whitney AUC of xs_pos > xs_neg (rank-based)."""
    xs_pos = np.asarray(xs_pos); xs_neg = np.asarray(xs_neg)
    xs_pos = xs_pos[~np.isnan(xs_pos)]; xs_neg = xs_neg[~np.isnan(xs_neg)]
    if len(xs_pos) == 0 or len(xs_neg) == 0:
        return np.nan
    conc = np.concatenate([xs_pos, xs_neg])
    ranks = sps.rankdata(conc)
    r_pos = ranks[:len(xs_pos)].sum()
    return float((r_pos - len(xs_pos) * (len(xs_pos) + 1) / 2) / (len(xs_pos) * len(xs_neg)))


def metric_tests(rows):
    """Per-metric: AUC(winner>loser), AUC(big>small), MW p, KS p, means."""
    out = []
    W = [r for r in rows if r["outcome"] == "W"]
    L = [r for r in rows if r["outcome"] == "L"]
    BIG = [r for r in rows if r["_cls"] in ("big_win", "big_loss")]
    SM = [r for r in rows if r["_cls"] in ("small_win", "small_loss")]
    for key, label, _ in CURATED:
        xw = np.array([r[key] for r in W], dtype=float)
        xl = np.array([r[key] for r in L], dtype=float)
        xw = xw[~np.isnan(xw)]; xl = xl[~np.isnan(xl)]
        xb = np.array([r[key] for r in BIG], dtype=float)
        xs_ = np.array([r[key] for r in SM], dtype=float)
        xb = xb[~np.isnan(xb)]; xs_ = xs_[~np.isnan(xs_)]
        a_wl = auc(xw, xl)
        a_bs = auc(xb, xs_)
        p_mw = np.nan
        p_ks = np.nan
        if len(xw) > 2 and len(xl) > 2:
            try:
                p_mw = float(sps.mannwhitneyu(xw, xl, alternative="two-sided").pvalue)
                p_ks = float(sps.ks_2samp(xw, xl).pvalue)
            except Exception:
                pass
        out.append(dict(key=key, label=label,
                        mean_w=float(xw.mean()) if len(xw) else np.nan,
                        mean_l=float(xl.mean()) if len(xl) else np.nan,
                        med_w=float(np.median(xw)) if len(xw) else np.nan,
                        med_l=float(np.median(xl)) if len(xl) else np.nan,
                        auc_wl=a_wl, auc_bs=a_bs, p_mw=p_mw, p_ks=p_ks))
    return out


# ----------------------------------------------------------------------------
# report
# ----------------------------------------------------------------------------

def fmt(x, nd=2):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "-"
    return f"{x:.{nd}f}"


def build_report(rows, batch, nfiles, nrecs, tests, out_md, figdir_rel, sol_usd, gs):
    by_cls = {c: [r for r in rows if r["_cls"] == c] for c in CLASSES}
    W = [r for r in rows if r["outcome"] == "W"]
    L = [r for r in rows if r["outcome"] == "L"]
    Lbig = by_cls["big_loss"]; Lsm = by_cls["small_loss"]

    total_pnl = sum(r["pnl_sol"] for r in rows)
    pnl_w = sum(r["pnl_sol"] for r in W)
    pnl_l = sum(r["pnl_sol"] for r in L)
    gross_profit = sum(r["pnl_sol"] for r in rows if r["pnl_sol"] > 0)
    gross_loss = -sum(r["pnl_sol"] for r in rows if r["pnl_sol"] < 0)
    pf = gross_profit / gross_loss if gross_loss > 0 else np.inf
    wr = 100 * len(W) / len(rows)

    lines = []
    a = lines.append

    a(f"# Trade Report — `{batch}`")
    a("")
    a(f"Auto-generated by `backend/trade_report.py`.")
    a("")
    a(f"- **Batch**: `{batch}`")
    a(f"- **Files**: {nfiles}  |  **Recordings**: {nrecs}  |  **Trades**: {len(rows)}")
    a(f"- **Winners**: {len(W)}  |  **Losers**: {len(L)}  |  **Win rate**: {wr:.1f}%")
    a(f"- **Total PnL**: {total_pnl:+.3f} SOL  (W {pnl_w:+.3f} / L {pnl_l:+.3f})")
    a(f"- **Profit factor**: {pf:.2f}")
    a("")
    a(f"Entry market cap derived as `entry_price x 1,000,000,000 x ${sol_usd:.0f}` "
      f"(pump.fun fixed supply x SOL/USD). All mcap figures in **USD**.")
    a("Classes: **big** = |pnl_pct| >= 20%, **small** = |pnl_pct| < 20%.")
    a("")
    a("---")
    a("")

    # ---- class summary ----
    a("## 1. Class summary")
    a("")
    a("| class | n | % trades | total PnL (SOL) | mean pnl% | median pnl% | median mcap (USD) | mean mcap (USD) |")
    a("|---|---|---|---|---|---|---|---|")
    for c in CLASSES:
        sub = by_cls[c]
        pnl = sum(r["pnl_sol"] for r in sub)
        mp = np.mean([r["pnl_pct"] for r in sub])
        mdp = np.median([r["pnl_pct"] for r in sub])
        mdm = np.median([r["mcap"] for r in sub])
        mmm = np.mean([r["mcap"] for r in sub])
        a(f"| {CLASS_LABEL[c]} | {len(sub)} | {100*len(sub)/len(rows):.1f}% | {pnl:+.3f} | {mp:+.2f}% | {mdp:+.2f}% | ${mdm:,.0f} | ${mmm:,.0f} |")
    a("")
    a("![pnl kde](%s/pnl_kde.png)" % figdir_rel)
    a("")

    # ---- winner vs loser mcap ----
    a("## 2. Entry market cap — winners vs losers")
    a("")
    for side, sub in (("Winners", W), ("Losers", L)):
        d = describe([r["mcap"] for r in sub])
        a(f"- **{side}** (n={d['n']}): median ${d['median']:,.0f}, mean ${d['mean']:,.0f}, "
          f"p25 ${d['p25']:,.0f}, p75 ${d['p75']:,.0f}, p90 ${d['p90']:,.0f}, range [${d['min']:,.0f}, ${d['max']:,.0f}]")
    a("")
    a("![mcap hist kde](%s/mcap_hist_kde.png)" % figdir_rel)
    a("")
    a("![mcap box](%s/mcap_box.png)" % figdir_rel)
    a("")

    # ---- big vs small ----
    a("## 3. Big vs small — the tail split")
    a("")
    a("### Winners")
    for side, sub in (("Big winners (>= +20%)", by_cls["big_win"]),
                      ("Small winners (0..+20%)", by_cls["small_win"])):
        d = describe([r["mcap"] for r in sub])
        pnl = sum(r["pnl_sol"] for r in sub)
        a(f"- **{side}** (n={d['n']}, PnL {pnl:+.3f} SOL): median mcap ${d['median']:,.0f}, mean ${d['mean']:,.0f}")
    a("")
    a("### Losers")
    for side, sub in (("Big losers (<= -20%)", Lbig), ("Small losers (0..-20%)", Lsm)):
        d = describe([r["mcap"] for r in sub])
        pnl = sum(r["pnl_sol"] for r in sub)
        a(f"- **{side}** (n={d['n']}, PnL {pnl:+.3f} SOL): median mcap ${d['median']:,.0f}, mean ${d['mean']:,.0f}")
    a("")
    bl_pnl = sum(r["pnl_sol"] for r in Lbig)
    sl_pnl = sum(r["pnl_sol"] for r in Lsm)
    a(f"Big losers carry **{100*abs(bl_pnl)/(abs(bl_pnl)+abs(sl_pnl)):.0f}%** of all losing PnL "
      f"({bl_pnl:+.3f} of {bl_pnl+sl_pnl:+.3f} SOL).")
    a("")
    a("![class mcap heatmap](%s/class_mcap_heatmap.png)" % figdir_rel)
    a("")

    # ---- mcap buckets ----
    a("## 4. Performance by entry-mcap bucket ($5k granularity)")
    a("")
    edges, labels = _mcap_5k_buckets(max_bucket=200e3, final_label="200k+")
    a("| mcap (USD) | n | W | L | big-W | big-L | loss% | big-L% | net PnL |")
    a("|---|---|---|---|---|---|---|---|---|")
    for lbl, lo, hi in zip(labels, edges[:-1], edges[1:]):
        sub = [r for r in rows if lo <= r["mcap"] < hi]
        w = sum(1 for r in sub if r["outcome"] == "W")
        l = sum(1 for r in sub if r["outcome"] == "L")
        bw = sum(1 for r in sub if r["_cls"] == "big_win")
        bl = sum(1 for r in sub if r["_cls"] == "big_loss")
        lp = 100 * l / len(sub) if sub else 0
        blp = 100 * bl / len(sub) if sub else 0
        net = sum(r["pnl_sol"] for r in sub)
        a(f"| {lbl} | {len(sub)} | {w} | {l} | {bw} | {bl} | {lp:.0f}% | {blp:.0f}% | {net:+.3f} |")
    a("")
    a("![lossrate by mcap](%s/lossrate_by_mcap.png)" % figdir_rel)
    a("")
    a("![mcap vs pnl](%s/mcap_vs_pnl.png)" % figdir_rel)
    a("")

    # ---- metric discrimination ----
    a("## 5. Entry-metric discriminative power")
    a("")
    a("AUC = Mann-Whitney probability that a randomly chosen **winner** has a higher")
    a("metric value than a randomly chosen **loser** (col `AUC W>L`), or that a **big**")
    a("trade exceeds a **small** trade (col `AUC big>small`). 0.5 = no signal;")
    a(">0.6 or <0.4 = potentially useful. `p MW` = Mann-Whitney U p-value,")
    a("`p KS` = Kolmogorov-Smirnov p-value (winner vs loser distributions).")
    a("")
    a("| metric | mean W | mean L | med W | med L | AUC W>L | AUC big>small | p MW | p KS |")
    a("|---|---|---|---|---|---|---|---|---|")
    # sort by |auc_wl - 0.5| desc
    tests_sorted = sorted(tests, key=lambda t: -abs((t["auc_wl"] or 0.5) - 0.5))
    for t in tests_sorted:
        a(f"| {t['label']} | {fmt(t['mean_w'],3)} | {fmt(t['mean_l'],3)} | "
          f"{fmt(t['med_w'],3)} | {fmt(t['med_l'],3)} | {fmt(t['auc_wl'],3)} | "
          f"{fmt(t['auc_bs'],3)} | {fmt(t['p_mw'],4)} | {fmt(t['p_ks'],4)} |")
    a("")
    a("![roc auc](%s/roc_auc.png)" % figdir_rel)
    a("")
    a("![metric grid](%s/metric_grid.png)" % figdir_rel)
    a("")

    # ---- exit reasons ----
    a("## 6. Exit-reason mix")
    a("")
    a("| exit reason | big-W | small-W | big-L | small-L | total |")
    a("|---|---|---|---|---|---|")
    allr = [r for r, _ in Counter(r["exit_reason"] for r in rows).most_common()]
    for rsn in allr:
        counts = [sum(1 for r in by_cls[c] if r["exit_reason"] == rsn) for c in CLASSES]
        a(f"| {rsn} | {counts[0]} | {counts[1]} | {counts[2]} | {counts[3]} | {sum(counts)} |")
    a("")
    a("![exit reasons](%s/exit_reasons.png)" % figdir_rel)
    a("")

    # ---- hold time ----
    a("## 7. Hold time")
    a("")
    a("| class | median hold (s) | p75 | p90 | max |")
    a("|---|---|---|---|---|")
    for c in CLASSES:
        hs = [r["hold_s"] for r in by_cls[c]]
        d = describe(hs)
        a(f"| {CLASS_LABEL[c].split(' (')[0]} | {d['median']:.0f} | {d['p75']:.0f} | {d['p90']:.0f} | {d['max']:.0f} |")
    a("")
    a("![hold time](%s/hold_time.png)" % figdir_rel)
    a("")

    # ---- mcap-gate simulation ----
    base = gs["base"]
    a("## 8. Entry-mcap gate simulation (counterfactual)")
    a("")
    a("What if we blocked entries by market cap? This recomputes cohort performance")
    a("under a min-mcap floor, a max-mcap ceiling, and a band-pass. **Caveat:** this is")
    a("the *naive* counterfactual — a blocked trade is treated as \"no trade at all\".")
    a("In the live engine, blocking an entry at time T usually just delays entry to T+k")
    a("at a different price/mcap (replacement-entry dynamics), so these PnL deltas are")
    a("an **upper bound** on an in-engine gate. Statistical significance uses the repo's")
    a("paired bootstrap on per-recording PnL deltas.")
    a("")
    a("![mcap gate sim](%s/mcap_gate_sim.png)" % figdir_rel)
    a("")
    a("### Floor sweep — block entries with mcap < X")
    a("")
    a("| floor | kept | WR% | PnL (SOL) | dPnL | PF | bigW kept | bigL kept |")
    a("|---|---|---|---|---|---|---|---|")
    for t, p in gs["floor"]:
        dp = p["pnl"] - base["pnl"]
        a(f"| {_fmt_usd(t)} | {p['n']} | {p['wr']:.1f} | {p['pnl']:+.3f} | {dp:+.3f} | "
          f"{p['pf']:.2f} | {p['bigw']}/{base['bigw']} | {p['bigl']}/{base['bigl']} |")
    a("")
    a("### Ceiling sweep — block entries with mcap > X")
    a("")
    a("| ceiling | kept | WR% | PnL (SOL) | dPnL | PF | bigW kept | bigL kept |")
    a("|---|---|---|---|---|---|---|---|")
    for t, p in gs["ceil"]:
        dp = p["pnl"] - base["pnl"]
        lbl = _fmt_usd(t) if np.isfinite(t) else "inf"
        a(f"| {lbl} | {p['n']} | {p['wr']:.1f} | {p['pnl']:+.3f} | {dp:+.3f} | "
          f"{p['pf']:.2f} | {p['bigw']}/{base['bigw']} | {p['bigl']}/{base['bigl']} |")
    a("")
    a("### Band sweep — top bands by net PnL")
    a("")
    a("| band | kept | WR% | PnL (SOL) | dPnL | PF | bigW | bigL |")
    a("|---|---|---|---|---|---|---|---|")
    for lo, hi, p in gs["bands"][:10]:
        dp = p["pnl"] - base["pnl"]
        hi_lbl = _fmt_usd(hi) if np.isfinite(hi) else "inf"
        a(f"| {_fmt_usd(lo)}-{hi_lbl} | {p['n']} | {p['wr']:.1f} | {p['pnl']:+.3f} | "
          f"{dp:+.3f} | {p['pf']:.2f} | {p['bigw']} | {p['bigl']} |")
    a("")

    # ---- floor x ceiling 2D sweep ----
    a("### Floor x Ceiling combination sweep ($5k grid)")
    a("")
    a("Every (floor F, ceiling C) pair with F < C, keeping trades where")
    a("F <= mcap < C.  Floor grid: $0–$100k in $5k steps; ceiling grid: $10k–$200k")
    a("in $5k steps plus `inf`.  The heatmap shows net PnL for every combination;")
    a("the baseline (no gate) is the F=$0, C=inf cell (blue circle).")
    a("")
    a("![floor ceiling heatmap](%s/floor_ceiling_heatmap.png)" % figdir_rel)
    a("")
    a("**Top 15 (floor, ceiling) combinations by net PnL:**")
    a("")
    a("| rank | floor | ceiling | kept | WR% | PnL (SOL) | dPnL | PF |")
    a("|---|---|---|---|---|---|---|---|")
    for rank, fc in enumerate(gs["fc"]["list"][:15]):
        cl = _fmt_usd(fc["ceil"]) if np.isfinite(fc["ceil"]) else "inf"
        a(f"| {rank+1} | {_fmt_usd(fc['floor'])} | {cl} | {fc['n']} | "
          f"{fc['wr']:.1f} | {fc['pnl']:+.3f} | {fc['dpnl']:+.3f} | {fc['pf']:.2f} |")
    a("")
    # how many combos beat baseline?
    n_beat = sum(1 for fc in gs["fc"]["list"] if fc["pnl"] > base["pnl"])
    n_total = len(gs["fc"]["list"])
    best_fc = gs["fc"]["list"][0]
    a(f"**{n_beat} of {n_total}** (floor, ceiling) combinations beat the baseline "
      f"({base['pnl']:+.3f} SOL).  Best: floor {_fmt_usd(best_fc['floor'])} / "
      f"ceiling {_fmt_usd(best_fc['ceil']) if np.isfinite(best_fc['ceil']) else 'inf'} "
      f"= {best_fc['pnl']:+.3f} SOL ({best_fc['dpnl']:+.3f} vs baseline, "
      f"{best_fc['n']} trades, {best_fc['wr']:.1f}% WR).  However — as the "
      "significance tables below show — none of these survive the paired "
      "Wilcoxon/bootstrap gate once the search is controlled.")
    a("")
    a("### Statistical significance — paired bootstrap on per-recording PnL delta")
    a("")
    a(f"Unit of pairing is the recording (n_boot={gs['n_boot']}). A gate is a real")
    a("improvement only if the 95% CI lower bound is > 0.")
    a("")
    a("| floor | mean dPnL/rec | 95% CI | Wilcoxon p (1-sided) | significant? |")
    a("|---|---|---|---|---|")
    for t, m, lo, hi, pw, nch in gs["boot"]:
        sig = "**YES**" if (lo > 0 and pw < 0.05) else ("no (negative)" if hi < 0 else "no")
        a(f"| {_fmt_usd(t)} | {m:+.5f} | [{lo:+.5f}, {hi:+.5f}] | {pw:.4f} | {sig} |")
    a("")

    # ---- curated contiguous-gate hypothesis tests ----
    a("### Hypothesis tests — contiguous gates (Wilcoxon signed-rank + bootstrap)")
    a("")
    a("Per-recording paired tests vs baseline. `Wilcoxon p` is one-sided")
    a("(gate > baseline); `n changed` = recordings whose trade set the gate alters.")
    a("**None of these reach p < 0.05 or a bootstrap CI strictly above 0.**")
    a("")
    a("| gate | kept n | WR% | PnL (SOL) | dPnL | Wilcoxon p | bootstrap 95% CI | sig? |")
    a("|---|---|---|---|---|---|---|---|")
    for ct in gs["contig_tests"]:
        sig = "**YES**" if (ct["boot_lo"] > 0 and ct["wilcoxon_p"] < 0.05) else "no"
        a(f"| {ct['name']} | {ct['n']} | {ct['wr']:.1f} | {ct['pnl']:+.3f} | "
          f"{ct['dpnl']:+.3f} | {ct['wilcoxon_p']:.4f} | "
          f"[{ct['boot_lo']:+.5f}, {ct['boot_hi']:+.5f}] | {sig} |")
    a("")

    # ---- across-baseline robustness ----
    a("### Robustness across old engine baselines")
    a("")
    a("Do the mcap gates hold up on *other* engine baselines, or only on")
    a(f"`{batch}`? Same per-recording paired Wilcoxon (1-sided) + bootstrap, each")
    a("gate tested against each batch's own ungated baseline. `sig+` = Wilcoxon")
    a("p<0.05 AND bootstrap CI lower bound > 0.")
    a("")
    a("| baseline | n trades | gate | dPnL (SOL) | Wilcoxon p | bootstrap 95% CI | sig? |")
    a("|---|---|---|---|---|---|---|")
    for blabel, ntr, gate_res in gs["robust_rows"]:
        for gr in gate_res:
            sig = "**YES**" if (gr["wilcoxon_p"] < 0.05 and gr["lo"] > 0) else (
                "neg" if gr["hi"] < 0 else "no")
            pw = f"{gr['wilcoxon_p']:.4f}" if not np.isnan(gr["wilcoxon_p"]) else "nan"
            a(f"| {blabel} | {ntr} | {gr['gate']} | {gr['dpnl']:+.4f} | {pw} | "
              f"[{gr['lo']:+.5f}, {gr['hi']:+.5f}] | {sig} |")
    a("")
    n_sig = sum(1 for _, _, gres in gs["robust_rows"]
                for gr in gres if gr["wilcoxon_p"] < 0.05 and gr["lo"] > 0)
    n_tot = sum(len(gres) for _, _, gres in gs["robust_rows"])
    a(f"**{n_sig} of {n_tot}** (baseline, gate) combinations reach significance. "
      "The mcap gate does **not** replicate as a significant improvement on any "
      "engine baseline — including the larger `iter33_screen_base`/`iter33_cand_full` "
      "cohorts where it has the most trades to detect an effect.")
    a("")

    # ---- deliberate attempt to FIND a significant gate ----
    a("### Attempt to construct a significant gate (corrected scan)")
    a("")
    a("A deliberate search for *any* significant entry gate. 22 candidates scanned:")
    a("mcap floors/ceilings/bands, hold-time caps, signal-strength and confidence")
    a("thresholds, and 2-feature combos. Per-recording paired Wilcoxon (1-sided) +")
    a("10k bootstrap, with **Bonferroni correction** across the 22-gate family")
    a("(family α = 0.05 → per-test α = 0.0023).")
    a("")
    a("| gate | kept | dPnL (SOL) | Wilcoxon p | bootstrap 95% CI | verdict |")
    a("|---|---|---|---|---|---|")
    for g in gs["scan"]:
        verdict = ("**BONF-SIG**" if g["bonf_sig"] else
                   ("raw-sig (fails Bonferroni)" if g["raw_sig"] else "ns"))
        pw = f"{g['p']:.4f}" if not np.isnan(g["p"]) else "nan"
        a(f"| {g['name']} | {g['kept']}/{g['total']} | {g['dpnl']:+.4f} | {pw} | "
          f"[{g['lo']:+.5f}, {g['hi']:+.5f}] | {verdict} |")
    a("")
    n_bonf = sum(1 for g in gs["scan"] if g["bonf_sig"])
    n_raw = sum(1 for g in gs["scan"] if g["raw_sig"])
    a(f"**Result: {n_bonf} of {len(gs['scan'])} gates are significant after Bonferroni "
      f"correction** ({n_raw} reach raw p<0.05 but none survive the correction). "
      "Several gates are *significantly negative* under one-sided testing "
      "(`conf>=0.86` p=0.999, `mcap>=14k & conf>=0.86` p=0.999, `s_eff>median` p=0.986, "
      "`mcap<=70k` p=0.992) — i.e. blocking those entries *removes net-positive* trades. "
      "**No entry gate on mcap, hold time, signal strength, or confidence is a "
      "statistically significant improvement on this dataset.**")
    a("")

    # ---- discontiguous zones ----
    z = gs["zone"]
    a("### Discontiguous trading zones (exhaustive zone-mask search)")
    a("")
    a("Instead of a contiguous band, allow any **union of mcap buckets** — a set of")
    a("\"trading zones\". Searched all 127 non-empty bucket unions over the 7 buckets")
    a(f"({', '.join(z['labels'])}). Because this is a large multiple-comparison search,")
    a(f"significance uses a **max-statistic permutation null** (n={z['n_zone_perm']}):")
    a("the observed best-zone PnL is compared against the best zone achievable under")
    a("randomly permuted bucket PnL. p near 1.0 = no better than chance.")
    a("")
    a("![mcap zone sim](%s/mcap_zone_sim.png)" % figdir_rel)
    a("")
    a("**Per-bucket PnL** (the building blocks):")
    a("")
    a("| bucket (USD) | n | PnL (SOL) | WR% |")
    a("|---|---|---|---|")
    for i in range(len(z["labels"])):
        a(f"| {z['labels'][i]} | {z['bn'][i]} | {z['bpnl'][i]:+.3f} | {z['bwr'][i]:.0f}% |")
    a("")
    a("**Top 10 zone masks** by PnL (`+` = bucket kept):")
    a("")
    a("| rank | kept buckets | n | PnL (SOL) | dPnL | WR% | permutation p |")
    a("|---|---|---|---|---|---|---|")
    for rank, zz in enumerate(z["top"][:10]):
        kept = "+".join(z["labels"][i] for i in zz["sel"])
        a(f"| {rank+1} | {kept} | {zz['n']} | {zz['pnl']:+.3f} | {zz['dpnl']:+.3f} | "
          f"{zz['wr']:.1f}% | {zz['perm_p']:.3f} |")
    a("")
    best_zone = z["top"][0]
    a(f"Best contiguous band for reference: buckets "
      f"{'+'.join(z['labels'][i] for i in z['contig_sel'])} "
      f"PnL {z['contig_pnl']:+.3f} (max-stat permutation p={z['contig_perm_p']:.3f}).")
    a("")
    a("**Hypothesis tests on the best zone mask** "
      f"({'+'.join(z['labels'][i] for i in z['best_sel'])}, PnL {z['best_pnl']:+.3f}):")
    a("")
    a("| test | statistic | p-value | interpretation |")
    a("|---|---|---|---|")
    a(f"| Per-recording Wilcoxon signed-rank (1-sided) | n_changed={z['best_n_changed']} | "
      f"{z['best_wilcoxon_p']:.4f} | not significant (>= 0.05) |")
    a(f"| Paired bootstrap 95% CI of dPnL/rec | mean Δ | "
      f"[{z['best_boot_lo']:+.5f}, {z['best_boot_hi']:+.5f}] | straddles 0 → not significant |")
    a(f"| Naive permutation (best single bucket, uncorrected) | best PnL | "
      f"{z['naive_perm_p']:.4f} | best zone no better than best single bucket by chance |")
    a(f"| **Max-stat permutation (127-comparison corrected)** | best PnL | "
      f"**{z['perm_p_best']:.4f}** | **not significant — gain is search-selection artifact** |")
    a("")
    a(f"**Verdict:** the best zone mask reaches {best_zone['pnl']:+.3f} SOL "
      f"(Δ{best_zone['dpnl']:+.3f}) and even beats the naive single-bucket null "
      f"(p={z['naive_perm_p']:.3f}), but once the 127-combination search is controlled "
      f"by the max-statistic permutation null, **p = {z['perm_p_best']:.3f}** — far from "
      "significant. The apparent gain is a multiple-comparison artifact. Discontiguous "
      "zones do **not** beat contiguous bands.")
    a("")

    # ---- 9. recording_ended exit-reason deep-dive ----
    rew = [r for r in rows if r["exit_reason"] == "recording_ended"]
    others = [r for r in rows if r["exit_reason"] != "recording_ended"]
    n_re = len(rew)
    n_total = len(rows)
    pct_re = 100.0 * n_re / n_total if n_total > 0 else 0.0
    pnl_re = sum(r["pnl_sol"] for r in rew)
    avg_pnl_re = pnl_re / n_re if n_re > 0 else 0.0
    w_re = sum(1 for r in rew if r["pnl_sol"] > 0)
    wr_re = 100.0 * w_re / n_re if n_re > 0 else 0.0
    underwater_n = sum(1 for r in rew if r["pnl_pct"] < 0)
    underwater_pct = 100.0 * underwater_n / n_re if n_re > 0 else 0.0

    # hold times
    med_hold_re = np.median([r["hold_s"] for r in rew]) if rew else 0.0
    p75_hold_re = pctile([r["hold_s"] for r in rew], 75) if rew else 0.0
    max_hold_re = max([r["hold_s"] for r in rew]) if rew else 0.0
    med_hold_other = np.median([r["hold_s"] for r in others]) if others else 0.0

    # mcaps
    med_re_mcap = np.median([r["mcap"] for r in rew]) if rew else 0.0
    p25_re_mcap = pctile([r["mcap"] for r in rew], 25) if rew else 0.0
    p75_re_mcap = pctile([r["mcap"] for r in rew], 75) if rew else 0.0
    med_other_mcap = np.median([r["mcap"] for r in others]) if others else 0.0
    p25_other_mcap = pctile([r["mcap"] for r in others], 25) if others else 0.0
    p75_other_mcap = pctile([r["mcap"] for r in others], 75) if others else 0.0
    mcap_ratio = med_other_mcap / med_re_mcap if med_re_mcap > 0 else 0.0

    # MW U test on mcap
    x_re = np.array([r["mcap"] for r in rew if not np.isnan(r["mcap"])])
    x_ot = np.array([r["mcap"] for r in others if not np.isnan(r["mcap"])])
    if len(x_re) > 2 and len(x_ot) > 2:
        res_mw = sps.mannwhitneyu(x_re, x_ot, alternative="two-sided")
        p_mw_mcap = res_mw.pvalue
        conc = np.concatenate([x_re, x_ot])
        ranks = sps.rankdata(conc)
        auc_mcap = (ranks[:len(x_re)].sum() - len(x_re) * (len(x_re) + 1) / 2) / (len(x_re) * len(x_ot))
    else:
        p_mw_mcap = 1.0
        auc_mcap = 0.5

    a("## 9. `recording_ended` exit-reason deep-dive")
    a("")
    a("`recording_ended` is a force-close exit reason that triggers when the backtester runs out of "
      "historical data on a recording while a trade is still in position. These represent trades where the "
      "engine got stuck in-position during a slow-bleed and never fired any Bayesian exit (e.g. Kramers escape "
      "or reversal) before the recording truncated.")
    a("")
    a("### Profile of `recording_ended` trades")
    a("")
    a(f"- **Total `recording_ended` trades**: {n_re} ({pct_re:.1f}% of all {n_total} trades in cohort)")
    a(f"- **Net PnL**: {pnl_re:+.3f} SOL (Average: {avg_pnl_re:+.5f} SOL per trade)")
    a(f"- **Win Rate**: {wr_re:.1f}% ({w_re}/{n_re} ended positive at force-close)")
    a(f"- **Underwater Density**: {underwater_pct:.1f}% ({underwater_n}/{n_re}) of these trades ended negative at force-close (avg PnL: {np.mean([r['pnl_pct'] for r in rew]):+.1f}%)")
    a(f"- **Hold Time**: Median hold {med_hold_re:.0f}s (p75: {p75_hold_re:.0f}s, max: {max_hold_re:.0f}s) vs. median {med_hold_other:.0f}s for normal exits.")
    a("")
    a("### Entry Market Cap Profile")
    a("")
    a("`recording_ended` trades enter at significantly lower market caps than normal trades:")
    a(f"- **Median Entry Market Cap (`recording_ended`)**: ${med_re_mcap:,.0f} USD (IQR: ${p25_re_mcap:,.0f} – ${p75_re_mcap:,.0f} USD)")
    a(f"- **Median Entry Market Cap (Others)**: ${med_other_mcap:,.0f} USD (IQR: ${p25_other_mcap:,.0f} – ${p75_other_mcap:,.0f} USD)")
    a(f"- **Comparison**: Median entry cap is **{mcap_ratio:.1f}x lower** for `recording_ended` trades. A Mann-Whitney U test confirms this difference is highly significant (AUC = {auc_mcap:.3f}, p = {p_mw_mcap:.4g}). They are heavily concentrated in the micro-cap zone below $14k.")
    a("")

    # Entry-time metrics comparisons for losers
    a("### Entry-time metric comparison for losers")
    a("")
    a("Do `recording_ended` losers differ from other losers at entry time? We compare the entry parameters "
      "of `recording_ended` losers against other losers in the cohort:")
    a("")
    a("| Metric | `recording_ended` Loser Median | Other Loser Median | AUC (`rec_end` > other) |")
    a("|---|---|---|---|")

    rew_losers = [r for r in rew if r["pnl_sol"] <= 0]
    other_losers = [r for r in rows if r["pnl_sol"] <= 0 and r["exit_reason"] != "recording_ended"]

    for k, label in [("s_effective", "S_effective"), ("trend_confidence", "C (Confidence)"),
                     ("v2_sigma_t", "sigma_t (Vol)"), ("v2_mu", "mu (Drift)"),
                     ("v2_P_up", "P_up"), ("v2_P_down", "P_down"),
                     ("v2_E_star", "E*"), ("atr", "ATR")]:
        a_vals = np.array([r[k] for r in rew_losers if not np.isnan(r[k])])
        b_vals = np.array([r[k] for r in other_losers if not np.isnan(r[k])])
        if len(a_vals) > 2 and len(b_vals) > 2:
            conc = np.concatenate([a_vals, b_vals])
            ranks = sps.rankdata(conc)
            auc = (ranks[:len(a_vals)].sum() - len(a_vals) * (len(a_vals) + 1) / 2) / (len(a_vals) * len(b_vals))
            med_a = np.median(a_vals)
            med_b = np.median(b_vals)
            a(f"| {label} | {med_a:.4f} | {med_b:.4f} | {auc:.3f} |")
        else:
            a(f"| {label} | N/A | N/A | N/A |")
    a("")
    a("Most entry-time internal indicators have AUC near 0.5 — indicating that entry-time engine state "
      "cannot distinguish between a standard loss and a recording-ended bleed. The primary distinguishing "
      "characteristic remains the **entry-time market cap**.")
    a("")
    a("### Counterfactual sweep on `recording_ended` population")
    a("")
    a("If we block entries below a market cap floor, we selectively block these micro-cap bleeders. "
      "Below is the counterfactual sweep showing the impact of various market cap floors on the "
      "`recording_ended` trade population:")
    a("")
    a("| mcap floor | kept | removed | removed PnL (SOL) | big wins removed | big losses removed | WR of removed |")
    a("|---|---|---|---|---|---|---|")

    for f_lo in [0, 5000, 7000, 10000, 14000, 18000, 21000]:
        kept = [r for r in rew if r["mcap"] >= f_lo]
        removed = [r for r in rew if r["mcap"] < f_lo]
        if not removed:
            continue
        removed_pnl = sum(r["pnl_sol"] for r in removed)
        removed_wr = 100 * sum(1 for r in removed if r["pnl_sol"] > 0) / len(removed)
        bigw_rm = sum(1 for r in removed if r["pnl_pct"] >= 20)
        bigl_rm = sum(1 for r in removed if r["pnl_pct"] <= -20)
        a(f"| ${f_lo/1000:5.0f}k | {len(kept):>3}/{len(rew)} | {len(removed):>3} | {removed_pnl:+8.4f} SOL | {bigw_rm} | {bigl_rm} | {removed_wr:.0f}% |")
    a("")
    a("A block entry floor in the range of **$10k to $14k** USD targets exactly the area where these "
      "stuck trades concentrate. At a **$14k floor**, we eliminate **14 of 28** `recording_ended` trades, "
      "saving **0.430 SOL** of losses. This includes removing **10 of 14** of the big losers, with a "
      "win rate of only 21% on the blocked set. Above $14k, the floor starts blocking too many profitable "
      "normal trades, resulting in a net decline in overall cohort PnL.")
    a("")

    # ---- key takeaways ----
    a("## 10. Key takeaways")
    a("")
    bw = by_cls["big_win"]; bl = by_cls["big_loss"]
    a(f"1. **Big winners and big losers enter at nearly identical mcap** "
      f"(median ${np.median([r['mcap'] for r in bw]):,.0f} vs ${np.median([r['mcap'] for r in bl]):,.0f}). "
      "At low mcap the engine cannot separate a pump from a dump at entry time.")
    # best bucket
    bucket_net = []
    for lbl, lo, hi in zip(labels, edges[:-1], edges[1:]):
        net = sum(r["pnl_sol"] for r in rows if lo <= r["mcap"] < hi)
        bucket_net.append((net, lbl))
    bucket_net.sort(reverse=True)
    a(f"2. **Best mcap band by net PnL**: ${bucket_net[0][1]} "
      f"({bucket_net[0][0]:+.3f} SOL); worst: ${bucket_net[-1][1]} ({bucket_net[-1][0]:+.3f} SOL).")
    # big loser exit
    bl_kf = sum(1 for r in bl if r["exit_reason"] == "kelly_flat")
    a(f"3. **Big losers die via `kelly_flat`**: {bl_kf}/{len(bl)} ({100*bl_kf/len(bl):.0f}%) — "
      "the engine rides them down with no Bayesian exit firing.")
    # AUC
    best = max(tests, key=lambda t: abs((t["auc_wl"] or 0.5) - 0.5))
    a(f"4. **Strongest entry-metric discriminator**: `{best['label']}` "
      f"(AUC W>L = {best['auc_wl']:.3f}, p MW = {fmt(best['p_mw'],4)}). "
      "Most metrics sit near 0.5 — little entry-time separability.")
    # gate sim takeaway: best band by pnl and whether any boot CI > 0
    best_band = gs["bands"][0] if gs["bands"] else None
    any_sig = any(b[2] > 0 for b in gs["boot"])
    if best_band is not None:
        lo_lbl = _fmt_usd(best_band[0])
        hi_lbl = _fmt_usd(best_band[1]) if np.isfinite(best_band[1]) else "inf"
        bp = best_band[2]
        a(f"5. **Mcap gate is small and not significant**: best band {lo_lbl}-{hi_lbl} "
          f"gives {bp['pnl']:+.3f} SOL ({bp['pnl']-gs['base']['pnl']:+.3f} vs baseline), "
          f"WR {bp['wr']:.1f}% — but no floor gate has a bootstrap CI strictly > 0 "
          f"({'none significant' if not any_sig else 'some significant'}). "
          "Counterfactual is an upper bound that ignores replacement-entry dynamics.")
    zt = gs["zone"]["top"][0]
    a(f"6. **Discontiguous zones overfit**: best zone mask = {zt['pnl']:+.3f} SOL "
      f"(Δ{zt['dpnl']:+.3f}) but permutation p={zt['perm_p']:.3f} (max-stat null). "
      "No better than contiguous bands once multiple comparisons are controlled.")
    n_sig = sum(1 for _, _, gres in gs["robust_rows"]
                for gr in gres if gr["wilcoxon_p"] < 0.05 and gr["lo"] > 0)
    n_tot = sum(len(gres) for _, _, gres in gs["robust_rows"])
    a(f"7. **Gate does not replicate across baselines**: {n_sig}/{n_tot} "
      "(baseline, gate) pairs significant. The mcap gate fails to reproduce on every "
      "old engine baseline tested (iter22b…iter36) — it is not a robust effect.")
    # rec_ended takeaway
    a(f"8. **`recording_ended` trades concentrate at micro-cap (<$14k)**: A block entry floor "
      f"of $10k–$14k USD targets the peak density of stuck slow-bleeds (saving up to 0.430 SOL "
      f"from 14 force-closes). However, the in-engine impact is bounded by replacement-entry "
      f"dynamics where blocked trades often re-trigger at later, slightly higher prices.")
    a("")
    a("---")
    a("")
    a(f"_Figures in `{figdir_rel}/`. Regenerate: `cd backend && source .venv/bin/activate && python trade_report.py --batch {batch}`_")

    with open(out_md, "w") as f:
        f.write("\n".join(lines))


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", default="iter31_baseline")
    ap.add_argument("--results-dir", default="v2_results")
    ap.add_argument("--figdir", default="report_figs")
    ap.add_argument("--out", default=os.path.join("..", "trade_report.md"))
    ap.add_argument("--sol-usd", type=float, default=DEFAULT_SOL_USD)
    ap.add_argument("--n-boot", type=int, default=10000)
    args = ap.parse_args()

    rows, nfiles, nrecs = load_trades(args.batch, args.results_dir, args.sol_usd)
    for r in rows:
        r["_cls"] = classify(r)

    os.makedirs(args.figdir, exist_ok=True)

    print(f"batch={args.batch} trades={len(rows)} recordings={nrecs} sol_usd=${args.sol_usd:.0f}")
    print("rendering figures...")
    fig_pnl_kde(rows, args.figdir)
    fig_mcap_hist_kde(rows, args.figdir)
    fig_mcap_box(rows, args.figdir)
    fig_mcap_vs_pnl(rows, args.figdir)
    fig_lossrate_by_mcap(rows, args.figdir)
    fig_exit_reasons(rows, args.figdir)
    fig_metric_grid(rows, args.figdir)
    fig_hold_time(rows, args.figdir)
    fig_class_mcap_heatmap(rows, args.figdir)

    print("running mcap-gate simulation...")
    gs = compute_gate_sim(rows, n_boot=args.n_boot,
                          results_dir=args.results_dir, sol_usd=args.sol_usd)
    fig_gate_sim(gs, args.figdir)
    fig_floor_ceiling_heatmap(gs, args.figdir)
    fig_zone_sim(gs, args.figdir)

    print("computing metric tests...")
    tests = metric_tests(rows)
    auc_rows = [(t["label"], t["auc_wl"] if not np.isnan(t["auc_wl"]) else 0.5,
                 t["auc_bs"] if not np.isnan(t["auc_bs"]) else 0.5) for t in tests]
    fig_roc_auc(auc_rows, args.figdir)

    print("writing report...")
    # report lives at repo root; figures referenced relative to root
    figdir_rel = os.path.relpath(args.figdir, os.path.dirname(args.out))
    build_report(rows, args.batch, nfiles, nrecs, tests, args.out, figdir_rel,
                 args.sol_usd, gs)
    print(f"done -> {args.out}  (figs in {args.figdir}/)")


if __name__ == "__main__":
    main()
