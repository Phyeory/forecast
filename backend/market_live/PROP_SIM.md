# PROP_SIM — the FTMO 2-step forward simulation (operational manual)

**What it is:** the V2M market engine (67 US stocks, 1d bars, frozen `arm14`
params, k=0.75) driven through the **live stack** (`market_live/`) with a paper
broker and the `ftmo_2step` PropGuard, so the firm's rules are enforced on
every bar: 5 % daily loss, static 10 % max loss, no time limit, 50 %
consistency.  Orders fill at the **next bar's open** (the achievable basis the
M12 rerun proved is the honest one).  It runs on one host with one boss:

| host | cadence | state | role |
|---|---|---|---|
| **GitHub Actions** (`Phyeory/prop-sim`, private) | cron 05:00 / 05:30 / 08:00 UTC (+ dashboard Start) | `runtime/` committed to the private repo | the only host — 24/7, free, no card |
| **Dashboard** (`localhost:8000` → Market tab) | on demand | reads/writes the repo via the API | performance · trades · balance · **Start/Stop** |

The Mac resident host was **retired 2026-10-22** (launchd jobs removed) — the
dashboard no longer mirrors a local account, it *controls the remote one*.
One account, one truth: `runtime/account.json` in the repo.

## Dashboard control (the daily interface)

`./start.sh` → open `http://localhost:8000` → **Market** tab.  It talks to the
repo through `market_live/remote.py` (GitHub token from the Keychain via
`git credential fill` — stored nowhere in files):

- **Perf** — equity curve (`runtime/equity.json`), P/L vs the $100k start,
  realized P/L, prop-guard phase progress bar.
- **Trades** — open positions + pending fills from `state.json`,
  closed round trips (entry/exit/px/pnl/days/exit reason) from
  `runtime/trades.json`.
- **Balance** — cash + equity from `runtime/account.json`.
- **Start sim** — removes `runtime/paused` and dispatches a run now.
- **Stop sim** — commits `runtime/paused` (the runner then makes no new
  decisions, reports only) and cancels any in-flight run.

The runner (`deploy/run_daily.sh`) publishes the snapshot after every run via
`market_live/remote_report.py`.

## Restart safety (the part that made a remote host possible)

A restart used to reset the account: the book lived in RAM, the guard state in
the per-session journal dir, and the engine warm consumed the newest bar, so
the crash-time decision was silently swallowed.  Now:

- `state.json` — book (cash/positions/realized/fees), decision cursor
  (`last_ts` per symbol), `desired`, in-flight orders.  Written atomically
  after every poll.
- `guard_state.json` — the PropGuard phase/equity/floors (so a restart does
  not reset a passed phase back to 0).
- `snap.pkl.gz` — the **engine state** per symbol (pickled + gzipped,
  ~2 MB for 67 symbols).  Restoring it skips the ~6-minute warm entirely and
  is **decision-identical** to a fresh warm (verified: same desired position
  and same fills on the next bar).  Fingerprinted by engine-file hash **and**
  frozen-params hash, so a stale snapshot can never be used.
- On resume the engine is warmed only **through the last processed bar**, and
  the new bar is decided live; only (a) orders that were in flight at the
  crash and (b) exits of positions we own are re-armed — the flat-start
  semantics survive a restart.

## Checks (remote-only now)

```bash
open https://github.com/Phyeory/prop-sim/actions        # run history
tail -40 /tmp/mk_dash.log                               # dashboard log
curl -s localhost:8000/api/market/remote | python3 -m json.tool | head -30
```

An Equity/Balance/Lockstep anomaly now means one thing: the remote runner —
check the Actions page for a failed run.  The old Mac poller/monitor logs and
`restart_prop_sim.sh` are retired (the launchd jobs no longer exist).

## Restarting

Nothing to restart on a host — the runner is stateless per run and the state
lives in the repo.  To restart the *sim* use the dashboard **Stop → Start**.
A stop pauses decisions without closing positions; the account resumes from
`state.json` exactly where it stopped (no lost decisions, no account reset).

## The free-host question (verified 2026-10-22)

"Completely free, no card, 24/7" was researched against the actual current
terms, not marketing pages:

| host | card? | verdict |
|---|---|---|
| Oracle Always Free (ARM 2-core/12 GB) | **yes** (identity check) | best specs, but card required |
| GCP e2-micro / AWS / Lightsail / DigitalOcean / Scaleway / Hetzner | **yes** | trial or card |
| PythonAnywhere free ("Beginner") | no | ✗ **no scheduled tasks, no always-on tasks, 100 CPU-s/day, 512 MB** — unusable; the $10/mo Developer plan has tasks |
| Serv00 (FreeBSD shell, cron, 15 procs, 512 MB) | no | ✗ no numpy wheel for FreeBSD → our engine cannot run |
| Hugging Face Spaces (Docker, 2 vCPU/16 GB) | no | viable but sleep management + git auth + a container per task |
| **GitHub Actions (private repo)** | no | ✓ **chosen** — scheduled cron, 4-core runners, 2000 min/mo free for private repos, no card, no sleep; our run costs ~2 min |

The strategy decides once per US close, so a *daily scheduled job* is the
correct shape for a free host — not a resident daemon.  The engine snapshot
turns that daily job into seconds, which is what made even the slowest
candidate viable.

## What the sim is for

The question is not "does the backtest make money" (M12: 84.9 % challenge
pass, +$11.7k EV/attempt at k=0.75 on the honest fill basis) — it is whether
the live stack, on unseen bars, with the firm's rules enforced, reproduces
the edge.  Signals to watch: equity curve vs the backtest's expectation
(+0.0565 %/day), guard phase transitions, realized-vs-model slippage, and
whether the two hosts stay in lockstep.
