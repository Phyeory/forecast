"""
iter74 — Online Markov-switching fleet-regime filter for LIVE sessions.

The realtime half of the iter74 MSM system (analysis/iter74_fleet_regime.py
is the offline/research half).  Computes the SAME fleet observables as the
historical panel — but from LIVE candle streams of the tokens currently
being recorded (plus this session's own candles) — and forward-filters the
pre-trained Gaussian HMM to produce the causal regime posterior
p(state | bins seen so far), updated every bin boundary.

No daily backtests, no qualification floors, no lag: the posterior reacts
to the market as it happens (5-minute bins; within-bin state preview
available on demand at decision time via `state_now()`).

MODEL SOURCE
    fleet_regime_model.json — produced by
    `python analysis/iter74_fleet_regime.py fit`
    (refresh monthly or after any regime break; the filter itself is
    untouched by refreshes — only the emission/transition params change).

ENGINE CONSUMPTION
    strategy_engineV2 (v2_msm_enable=1.0) calls `FleetRegimeFilter.state_now()`
    through its holder — blocked when the argmax state is the worst
    (highest dump-rate) state.  Backtester replays get the exact same
    posterior via `from_panel()` (the historical reconstruction IS the
    ground truth: same candles, same math).

PARITY INVARIANT
    Backtest: ForwardTester/LiveTrader pass each recording's candles bin
    by bin — identical observable sequence to live (live adds OTHER active
    tokens' candles, which the historical panel also had).  The backtest
    replay uses the FLEET panel state at each entry second (prev closed
    bin), precomputed in fleet_filtered_states.json; live uses
    this filter.  Both are the filtered (never smoothed) posterior.
"""
from __future__ import annotations

import json
import math
import os
import statistics
import threading
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))

def _resolve_model_path(path: str | None = None) -> str:
    if path and os.path.exists(path):
        return path
    candidates = [
        os.path.join(HERE, "fleet_regime_model.json"),
        os.path.join(HERE, "data", "fleet_regime_model.json"),
        os.path.join(HERE, "analysis", "fleet_regime_model.json"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]

MODEL_PATH = _resolve_model_path()

BIN_SECONDS = 300
OCCUPANCY_WINDOW = 288          # 24 h of 5-min bins (iter74b)
RET_CLIP = 5.0
MIN_TOKENS_PER_BIN = 2


class FleetRegimeFilter:
    """Forward-filtered HMM posterior over live fleet observables.

    feed_bin(bin_id, obs) is called by main.py at every bin boundary with the
    CLOSED bin's observables (causal).  state_now() returns the current
    argmax state + posterior for entry gating.
    """

    def __init__(self, model_path: str | None = None):
        target_path = _resolve_model_path(model_path)
        with open(target_path) as f:
            m = json.load(f)
        self.n_states = m["n_states"]
        self.bin_seconds = m.get("bin_seconds", BIN_SECONDS)
        self.transmat = m["transmat"]
        self.state_means = m["state_means"]
        self.state_covars = m["state_covars"]
        self.feat_mu = m["feature_means"]
        self.feat_sd = m["feature_stds"]
        # worst state = highest dump-rate mean (determined by the model,
        # not by the researcher — pre-registered in iter74 PREREGISTRATION)
        dump_idx = 5  # FEATURES order: med_ret,p25_ret,buy_share-0.5,flow,pump,dump,log1p(n)
        self.worst_state = max(range(self.n_states),
                               key=lambda i: self.state_means[i][dump_idx])
        self.worst = self.worst_state
        self._alpha = None           # current filtered posterior (list of K)
        self._last_bin = None
        self._state_hist: list[int] = []   # argmax per CLOSED bin (occupancy)
        self._lock = threading.Lock()

    # ── core math (shared with the offline research path) ────────────────

    def _standardise(self, obs: list[float]) -> list[float]:
        return [(obs[j] - self.feat_mu[j]) / (self.feat_sd[j] or 1.0)
                for j in range(len(obs))]

    def _emission_logpdf(self, z: list[float], state: int) -> float:
        """Multivariate normal logpdf (no scipy dependency at runtime)."""
        mu = self.state_means[state]
        cov = self.state_covars[state]
        k = len(z)
        # solve cov^{-1} (z - mu) via Gaussian elimination
        diff = [z[j] - mu[j] for j in range(k)]
        A = [row[:] + [diff[j]] for j, row in enumerate(cov)]  # augmented
        # Cholesky for stability
        L = [[0.0] * k for _ in range(k)]
        for i in range(k):
            for j in range(i + 1):
                s = cov[i][j] - sum(L[i][p] * L[j][p] for p in range(j))
                if i == j:
                    if s <= 0:
                        s = 1e-12
                    L[i][j] = math.sqrt(s)
                else:
                    L[i][j] = s / L[j][j]
        # forward/back substitution with y = L^-1 diff
        y = [0.0] * k
        for i in range(k):
            y[i] = (diff[i] - sum(L[i][p] * y[p] for p in range(i))) / L[i][i]
        quad = sum(v * v for v in y)
        logdet = 2.0 * sum(math.log(L[i][i]) for i in range(k))
        return -0.5 * (k * math.log(2 * math.pi) + logdet + quad)

    def _step(self, alpha, z):
        T = self.transmat
        pred = [sum(alpha[i] * T[i][j] for i in range(self.n_states))
                for j in range(self.n_states)]
        ll = [self._emission_logpdf(z, j) for j in range(self.n_states)]
        mx = max(ll)
        lik = [math.exp(l - mx) for l in ll]
        post = [pred[j] * lik[j] for j in range(self.n_states)]
        s = sum(post) or 1.0
        return [p / s for p in post]

    # ── public API ───────────────────────────────────────────────────────

    def feed_bin(self, bin_id: int, obs: dict) -> None:
        """Feed one CLOSED bin's observables (causal). Thread-safe."""
        z = self._standardise([
            obs["med_ret"], obs["p25_ret"], obs["buy_share"] - 0.5,
            obs["flow"], obs["pump_rate"], obs["dump_rate"],
            math.log1p(obs["n_tok"]),
        ])
        with self._lock:
            if self._alpha is None:
                sp = self._model_startprob()
                ll = [self._emission_logpdf(z, j) for j in range(self.n_states)]
                mx = max(ll)
                lik = [math.exp(l - mx) for l in ll]
                post = [sp[j] * lik[j] for j in range(self.n_states)]
                s = sum(post) or 1.0
                self._alpha = [p / s for p in post]
            else:
                self._alpha = self._step(self._alpha, z)
            self._state_hist.append(max(range(self.n_states),
                                        key=lambda i: self._alpha[i]))
            self._last_bin = bin_id

    def _model_startprob(self):
        # stationary distribution of the transition matrix (the historical
        # panel start is arbitrary for live use)
        T = self.transmat
        K = self.n_states
        v = [1.0 / K] * K
        for _ in range(200):
            nv = [sum(v[i] * T[i][j] for i in range(K)) for j in range(K)]
            s = sum(nv) or 1.0
            nv = [x / s for x in nv]
            if max(abs(nv[i] - v[i]) for i in range(K)) < 1e-12:
                v = nv
                break
            v = nv
        return v

    def state_now(self):
        """(argmax_state, posterior, worst_state) — None before first bin."""
        with self._lock:
            if self._alpha is None:
                return None
            a = list(self._alpha)
        arg = max(range(self.n_states), key=lambda i: a[i])
        return {"state": arg, "posterior": a, "worst": self.worst_state,
                "blocked": arg == self.worst_state}

    # ── engine-side interface (same surface as _PanelStateSource) ─────────

    def state_at(self, ts: float, occupancy_floor: float = 0.0, **kwargs):
        """Gate-facing shim: the engine calls state_at(current_time); the
        live filter answers with the CURRENT filtered posterior (state of
        the last closed bin — the same prev-bin rule as the backtest).
        With occupancy_floor > 0 the worst state only blocks when the
        trailing OCCUPANCY_WINDOW bins were worst-state dominated above
        the floor (iter74b refinement).  Insufficient history → no block."""
        sn = self.state_now()
        if sn is None:
            return None
        blocked = sn["blocked"]
        if blocked and occupancy_floor > 0.0:
            occ = self.trailing_occupancy()
            blocked = occ is not None and occ > occupancy_floor
        return {"state": sn["state"], "worst": self.worst_state,
                "blocked": blocked}

    def trailing_occupancy(self, window: int = OCCUPANCY_WINDOW):
        """Fraction of the last `window` CLOSED bins in the worst state,
        or None with insufficient history (< window // 4 bins seen)."""
        with self._lock:
            hist = self._state_hist
        if len(hist) < max(1, window // 4):
            return None
        h = hist[-window:]
        return sum(1 for s in h if s == self.worst_state) / len(h)

    # ── backtest parity ───────────────────────────────────────────────────

    @classmethod
    def from_panel(cls, filtered_states_path: str, model_path: str | None = None):
        """Backtest replay source: precomputed causal filtered states
        per bin (fleet_filtered_states.json).  The file embeds its own
        OOS model + worst_state (state numbering is fit-specific — do
        NOT derive worst from another artifact).  Returns a lookup
        helper, not a live filter."""
        with open(filtered_states_path) as f:
            d = json.load(f)
        fmap = dict(zip(d["bins"], d["filt_states"]))
        worst = int(d.get("worst_state", 0))
        bin_s = int(d.get("bin_seconds", BIN_SECONDS))
        return _PanelStateSource(fmap, worst, bin_s)


class _PanelStateSource:
    """Lookup adapter the engines use in backtest mode."""

    def __init__(self, fmap: dict, worst_state: int, bin_seconds: int = BIN_SECONDS):
        self.fmap = fmap
        self.worst = worst_state
        self.bin_seconds = bin_seconds
        self._bins_sorted = sorted(fmap)

    def state_at(self, ts: float, occupancy_floor: float = 0.0, **kwargs):
        """Filtered fleet state for the LAST CLOSED bin before ts.
        With occupancy_floor > 0, the worst state only reports blocked
        when the trailing OCCUPANCY_WINDOW bins had worst-state occupancy
        above the floor (iter74b).  Insufficient history → no block."""
        b = int(ts) // self.bin_seconds - 1
        s = self.fmap.get(b)
        if s is None:
            return None
        blocked = s == self.worst
        if blocked and occupancy_floor > 0.0:
            # trailing 24 h of CALENDAR bins (b-288 .. b-1), counting only
            # bins present in the panel — the exact formula the iter74b
            # research ledger validated (calendar window, not index window)
            _w = 86400 // self.bin_seconds   # 24h of bins at this size
            window = [self.fmap.get(w)
                      for w in range(b - _w, b)]
            window = [x for x in window if x is not None]
            if len(window) < _w // 4:
                return {"state": s, "worst": self.worst, "blocked": False}
            occ = sum(1 for x in window if x == self.worst) / len(window)
            blocked = occ > occupancy_floor
        return {"state": s, "worst": self.worst, "blocked": blocked}


# ── live observable computation (runs in main.py per bin) ────────────────

def compute_bin_observables(candle_closes: dict[int, list[tuple[int, float]]],
                            bin_id: int, flows: dict[int, tuple[float, float]],
                            ) -> dict | None:
    """Observables for one CLOSED bin from active tokens' candles.

    candle_closes: {token_key: [(time, close), ...]} — only the bin's rows.
    flows: {token_key: (buy_volume, sell_volume)} accumulated for the bin.
    Returns the obs dict for feed_bin, or None when the bin is empty.
    """
    rets = []
    tot_buy = tot_sell = 0.0
    n = 0
    for key, rows in candle_closes.items():
        if not rows:
            continue
        first = last = None
        for t, c in rows:
            if first is None:
                first = c
            last = c
        if first and last is not None and first > 0:
            r = (last / first) - 1.0
            if r == r:
                rets.append(max(-RET_CLIP, min(RET_CLIP, r)))
            n += 1
        b, s = flows.get(key, (0.0, 0.0))
        tot_buy += b
        tot_sell += s
    if n == 0:
        return None
    vol = tot_buy + tot_sell
    rets.sort()
    return {
        "bin": bin_id,
        "n_tok": n,
        "med_ret": statistics.median(rets) if rets else 0.0,
        "p25_ret": rets[max(0, len(rets) // 4 - 1)] if rets else 0.0,
        "buy_share": (tot_buy / vol) if vol > 0 else 0.5,
        "flow": math.log1p(vol),
        "pump_rate": (sum(1 for r in rets if r >= 0.10) / len(rets)) if rets else 0.0,
        "dump_rate": (sum(1 for r in rets if r <= -0.20) / len(rets)) if rets else 0.0,
    }
