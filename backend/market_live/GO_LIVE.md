# GO LIVE — the exact sequence

Everything is built. After this you buy the account and the system runs.

## Decision (already made)

**FTMO Challenge: 2-Step, $100,000, MT5.** k = 0.75 (7.5% of equity per
position), 67 US stock CFDs, signal at the close, order at the next open.

Why this firm and not another:

| requirement | why it matters | FTMO 2-Step |
|---|---|---|
| no time limit | a 30-day cap drops the pass rate 74% → 0.1% | ✅ none |
| static max loss | trailing 10% ≈ a 5–6% static floor | ✅ static |
| 5% daily cap | worst day is −5.17% at k=1, −3.9% at k=0.75 | ✅ 5% |
| swing holding | median hold 32 days | ✅ |
| EA allowed | the whole point | ✅ |
| 80–90% split | payout economics | ✅ 80% |

Measured: **74.2% full-challenge pass** at k=0.75, **+$9.1k EV per attempt**,
funded blow-up 32.7%/yr, expected ~$1,040/month per funded account.

## What you need to buy / create (30 minutes)

1. **FTMO account** — register, buy *FTMO Challenge: 2-Step, $100k* (~$540).
   Choose **MT5** as the platform.
2. **A Windows VPS** (~$10–20/mo) — MT5 must run on Windows. Providers:
   Contabo, Vultr, Hetzner, ForexVPS. 2 vCPU / 4 GB is plenty.
   *Why not macOS:* MT5's Python API is Windows-only; the bridge is designed
   for a small Windows box and talks HTTP to this repo.
3. **Optional, free:** an **Alpaca paper account** (alpaca.markets) for an
   unlimited-duration forward test on live market data while the challenge
   runs. No deposit, no KYC for paper.

## Install on the VPS (10 minutes)

```powershell
# 1. Python 3.11+ and MetaTrader 5 installed, MT5 logged into the FTMO account
pip install MetaTrader5

# 2. copy backend/market_live/mt5_bridge_server.py to the VPS, then:
python mt5_bridge_server.py --port 8765 --magic 77001
#    -> "[bridge] connected: login=... server=... balance=100000.00"
#    -> in MT5: Tools > Options > Expert Advisors > Allow algorithmic trading
```

Expose port 8765 only to your machine (SSH tunnel is enough):

```bash
ssh -L 8765:127.0.0.1:8765 user@vps
```

## Wire this repo to the account (2 minutes)

```bash
cd backend
# point at the tunnel (default already 127.0.0.1:8765)
./.venv/bin/python -m market_live.setup_prop \
    --config market_live/config/prop_ftmo_100k.json
```

Expected:

```
[preflight] sizing: 7.50% of equity per position ($7,500 on $100,000)
  ✓ data: 67 symbols have tapes + frozen params
  ✓ broker: equity $100,000.00 ...
  ✓ naming: 67 symbols resolved
  ✓ sizing: $7,500/position clears minimums
  ✓ guard: ftmo_2step floor $90,000 daily floor $95,000 target 10.0%
[preflight] GO — buy the account, then run the daily loop
```

If naming fails (`AAPL` vs `AAPL.US`), run
`curl "localhost:8765/symbols?q=AAPL"` and add the real names to
`broker_symbols` in the config. If sizing fails, raise `k`.

Optionally prove order routing on the **demo** first:

```bash
./.venv/bin/python -m market_live.setup_prop \
    --config market_live/config/prop_ftmo_100k.json --place-test-order
```

## The daily loop (one command, after the US close)

```bash
cd backend
./.venv/bin/python -m market_live.run_live live \
    --config market_live/config/prop_ftmo_100k.json --once
```

Run it at **16:30 ET, Mon–Fri** (cron on the Mac/VPS):

```cron
30 16 * * 1-5 cd /path/to/pump-chart-market/backend && ./.venv/bin/python -m market_live.run_live live --config market_live/config/prop_ftmo_100k.json --once >> logs/live_$(date +\%F).log 2>&1
```

> **SUPERSEDED (2026-10-19): the `--once` cron model is structurally broken.**
> `warm()` replays ALL history — including the just-closed bar — through the
> engine (synthetic fills, engine-state only), so an evening `--once` run
> always reports `poll: 0 bars` and never submits: a daily cron can never
> place the opg order from the fresh close signal.
> The correct deployment is the **persistent poller**: warm ONCE at launch,
> then `poll_once()` every 60 s — each closed bar is fetched minutes after its
> close and processed exactly once (`since_ts` prevents doubles); the signal
> submits an opg order that fills at the next open. Sleep/reboot catch-up:
> `closed_bars(since_ts)` replays missed bars in sequence with identical
> semantics; an opg whose targeted open already passed falls back to a day
> order (logged `late_fill` wedge). The Alpaca implementation runs under
> **launchd** (`com.jaime.market-eval`,
> `market_live/market_eval_poller.sh`, logs `data/live_market/poller.out.log`).

Each run: reads newly closed bars → engine decides (same four-state replay as
the backtester) → reconciles positions through the bridge → updates the guard →
writes the journal → persists `guard_state.json` → exits.

## Weekly check (5 minutes)

```bash
./.venv/bin/python -m market_live.forward_monitor --latest
```

```
mean/day  +0.0432%   (backtest +0.0432%)
σ/day      0.919%    (backtest 0.919% — ratio 1.00x)
maxDD      13.3%     worst day -3.9%
verdict    ON TRACK
```

Verdicts: `ON TRACK` / `DEGRADING` (mean < half expected or t<0) / `FAILING`
(mean ≤ 0 over 20+ days) / `TOO EARLY`.

## Operating rules

1. **Never raise k to pass faster.** EV peaks at k=0.5–0.75; above k=1 the
   blow-up rate outruns the pass rate.
2. **Withdraw every payout.** The funded account blows ~33%/yr; withdrawn
   profit is the only profit that survives.
3. **Run 2–3 attempts in parallel** if you can fund the fees — 93–98% chance
   at least one passes.
4. **Do not restart the trader mid-day** — if you must, `guard_state.json`
   restores the firm's floors; verify `session_open.json`'s `code_commit`
   matches HEAD.
5. **Stop trading** if `forward_monitor` says FAILING for two consecutive weeks.

## Free trials — what they are and what they are not

**Card?** No — verified 2026-10-19 from ftmo.com/en/ftmo-free-trial/: the
sign-up is "Register now" only (email), zero payment/card references on the
page, a breach "costs you nothing", and identity checks (FTMO Identity) are
only required before rewards, not for trials. Also true on that page: the
trial **ends if you don't trade in the first 7 days**, and it is 14 days
long.

**Why the trial still isn't the answer to "can it pass prop evals live?":**
FTMO Free Trial mirrors the *first phase* only, halved target, **14-day
deadline** (2-Step trial: 5% target, 10% static, 5% daily, 2 min days, no
consistency; MT5/cTrader; unlimited retries, $0). Measured pass probability
for this strategy: **3.8% at k=0.75** (96% time out) — it is a 14-day sprint
and our edge is a multi-month grind. The real 2-Step has no deadline, which
is why it passes at 74%. **Use the free trial for what it is good for:**
proving that orders land, the symbol map is right, sizing is accepted, and
the guard tracks equity on FTMO's own platform — record this before the
purchase, expect the 14-day end (not a failure signal).

**Alpaca paper is the real free forward validator** — unlimited duration,
live real-time market data, simulated fills, no card, no KYC. And on top of
it the prop evaluation is answered continuously:

- the **live PropGuard** enforces the FTMO 2-Step rules on the realized
  equity (floors restart-safe in `guard_state.json`, 0.5% early trigger so
  the real 5%/10% walls are never crossed by accident);
- `challenge_status.py` replays the session's realized path through the
  FTMO-exact rule cascade and answers **"if this were a real evaluation,
  would it be passing today?"** — phase, target progress, trading days,
  best-day share, distances to the daily/max floors, PASS/BREACH verdict.

```bash
export APCA_API_KEY_ID=... APCA_API_SECRET_KEY=...
./.venv/bin/python -m market_live.setup_prop \
    --config market_live/config/alpaca_paper.json
./.venv/bin/python -m market_live.run_live live \
    --config market_live/config/alpaca_paper.json --once
./.venv/bin/python -m market_live.challenge_status --latest   # would it pass?
./.venv/bin/python -m market_live.forward_monitor --latest    # edge healthy?
```

An `opg` (market-on-open) order submitted after the close fills at the next
open — the same next-open semantics as the FTMO/MT5 deployment path
(measured wedge −0.47 pp / 2.8 y), so the forward test measures the
production fill convention.
