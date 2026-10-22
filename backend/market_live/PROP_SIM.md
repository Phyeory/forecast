# PROP_SIM — the FTMO 2-step forward simulation (operational manual)

**What it is:** the V2M market engine (67 US stocks, 1d bars, frozen `arm14`
params, k=0.75) driven through the **live stack** (`market_live/`) with a paper
broker and the `ftmo_2step` PropGuard, so the firm's rules are enforced on
every bar: 5 % daily loss, static 10 % max loss, no time limit, 50 %
consistency.  Orders fill at the **next bar's open** (the achievable basis the
M12 rerun proved is the honest one).  Two hosts run it, independently, as a
replication pair:

| host | cadence | state | role |
|---|---|---|---|
| **Mac** (`com.jaime.prop-sim` launchd, under `caffeinate`) | resident, polls every 60 s → processes each close within ~1 min | `backend/data/live_market/prop_sim/` | fast feedback + the dashboard |
| **GitHub Actions** (`Phyeory/prop-sim`, private) | cron 22:15 + 22:45 UTC (= 23:15 / 23:45 BST), after the US close in EDT *and* EST | `runtime/` committed to the private repo | 24/7 coverage when the Mac is off |

Both accounts start **flat** (`warm_sync=false`: the engine's inherited
positions are adopted as desired state but never traded — only fresh signals
open positions) and both use the **frozen arm14 params** (the same engine the
backtest and the prop MC ran on).  Their equity curves must match — that
equality is the replication check on the whole stack.

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

## Daily checks

```bash
cd /Users/jaime/pump-chart-market/backend
tail -3 data/live_market/prop_sim_poller.out.log      # the Mac's last polls
tail -2 data/live_market/prop_sim_monitor.log          # 30-min anomaly monitor
cat data/live_market/prop_sim/state.json | head -20   # book + cursor
```

The monitor appends one line per 30 min and raises a macOS notification on:
poller gone · guard halted · equity at/below the firm floors · stale
heartbeat.

```bash
# the remote runner
open https://github.com/Phyeory/prop-sim/actions        # run history
```

## Restarting

```bash
./market_live/restart_prop_sim.sh     # reinstall + load poller and monitor
```

A restart resumes exactly where it stopped (no lost decisions, no account
reset) — the snapshot makes it fast.

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
