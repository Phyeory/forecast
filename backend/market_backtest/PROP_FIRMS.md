# Prop-firm selection & attempt economics (arm14, verified 2026-10-18)

Everything here is measured, not asserted: `mc_firms.py` block-bootstraps the
arm14 **out-of-sample** daily ledger (754 days, 67 US stocks) through each
firm's actual rule set.  Rules were checked against the firms' published pages.

## The strategy's risk fingerprint (what a firm must tolerate)

| metric | value |
|---|---|
| mean | **+0.043 %/day** (≈ +10.9 %/yr) |
| daily σ | 0.92 % |
| Sharpe | 0.75 |
| worst historical day | **−5.17 %** at k=1 |
| median hold | 32 days (p90 144 d) → **swing, not intraday** |
| max concurrent positions | 23 (avg 5.5) |
| full-Kelly leverage | k* ≈ 5.1 (tested k=0.75 ≈ 0.15 Kelly) |

Consequence: the strategy **needs swing accounts** (weekend holding), **static
max-loss** (not trailing), and either **no daily cap or a 5 % daily cap**.

## Rule sensitivity — which rule actually kills you

Same strategy, same k=0.75, only the rule set changes:

| rule set | P1 pass | full pass | funded blow (1 y) | EV / attempt |
|---|---|---|---|---|
| **no daily cap, static 10 %** | 84.6 % | 84.6 % | 32.7 % | **+$10,483** |
| FTMO 2-Step (5 % daily, static 10 %, no TL, consistency 50 %) | 83.5 % | **74.2 %** | 32.7 % | **+$9,124** |
| generic 8 %/5 % 2-step (no TL) | 84.3 % | 75.7 % | 32.7 % | +$9,324 |
| FTMO 1-Step (3 % daily, **trailing** 10 %) | 60.5 % | 60.5 % | 73.5 % | +$5,225 |
| futures prop (2 % daily, trailing 4 %) | 42.1 % | 42.1 % | 100 % | +$831 |
| FTMO 2-Step with a **30-day** phase limit | 0.7 % | 0.1 % | — | **−$527** |

**Ranking of rule impact (destructive → benign):**

1. **Time limit — fatal.** A 30-day cap drops the pass rate from 74 % to 0.1 %.
   The strategy makes ~10 %/yr; it cannot make 10 % in 30 days reliably.
   *Never buy a timed challenge.*
2. **Trailing drawdown — severe.** 10 % trailing behaves like a 5–6 % static
   limit on a compounding equity curve: pass 74 % → 60 %, funded blow 33 % → 74 %.
3. **Daily cap 3 % — severe** (worst day is −5.17 % at k=1; at k=0.75 it is
   −3.9 %, still above a 3 % cap in the tail).
4. **Daily cap 5 % — mild** (−10 pp pass, nothing structural).
5. **Profit target 10 % vs 8 % — mild** (~1.5 pp).
6. **Max loss 10 % vs 8 % — mild** (78 % vs 85 % pass), 6 % hurts (70 %).

## The recommended firm

### Primary: **FTMO 2-Step** (verified rules: 10 % / 5 % targets, 5 % daily,
static 10 % max loss, **no time limit**, min 4 trading days, consistency: best
day ≤ 50 % of total profit, 80 % split, EA allowed)

Measured at **k = 0.75** (7.5 % of equity per position):

| k | P1 pass | full pass | funded blow/yr | E payout | EV/attempt | E payout/mo |
|---|---|---|---|---|---|---|
| 0.50 | 92.5 % | 86.8 % | 10.0 % | $9,204 | +$7,921 | $767 |
| **0.75** | **83.5 %** | **74.2 %** | **32.7 %** | **$12,485** | **+$9,124** | **$1,040** |
| 1.00 | 63.3 % | 48.0 % | 63.0 % | $12,783 | +$5,851 | $1,065 |
| 1.50 | 62.7 % | 44.8 % | 78.6 % | $15,475 | +$6,635 | $1,290 |
| 2.00 | 58.9 % | 37.8 % | 89.0 % | $15,898 | +$5,674 | $1,325 |

*The consistency rule costs almost nothing (75.0 → 74.2 %): the simulator keeps
trading until the best-day share is satisfied, exactly as FTMO requires.*

### Secondary (only if verified): a **no-daily-cap, static-10 %** 1-step firm.
The arithmetic is the best available (84.6 % pass, +$10.5k EV), but most
"1-step" firms actually use **trailing** drawdown, which is worse than FTMO.
Check the rules page before paying: *static* max loss, *no* daily cap, *no*
time limit, weekend holding allowed, EAs allowed.

### Avoid
* Futures prop firms (Topstep/Apex style): 4 % trailing from the high-water
  mark + 2 % daily → pass ≈ 42 %, funded blow ≈ 100 %. The strategy's daily
  gaps cannot survive a 4 % trailing floor.
* Any challenge with a 30/60-day deadline.
* Firms with 3 % daily caps (our tail lives at −3.9 % at k=0.75).
* Firms that ban weekend holding (median hold is 32 days).

## Deterministic proof (the real path, not a bootstrap)

The actual arm14 out-of-sample ledger, scaled and run day-by-day through the
FTMO 2-Step guard:

| k | outcome | day | date | best-day share |
|---|---|---|---|---|
| 0.50 | pass P1 + P2 | 538 | 2025-12-05 | 30% |
| **0.75** | **pass P1 + P2** | **442** | **2025-07-22** | **46%** |
| 1.00 | pass P1 + P2 | 337 | 2025-02-19 | 11% |

At k=0.75 the account took 442 trading days (~21 months) to clear both phases
— legal only because FTMO has no deadline.  The consistency rule was satisfied
(46% < 50%) at the moment of passing.

## Attempt economics — "how do I make thousands a month with no capital?"

A $100k FTMO 2-Step attempt costs **$540**. At k=0.75 each attempt is worth
**+$9.1k in expectation** (fee refunded on funding + payouts before blow-up).

Running N attempts in parallel:

| attempts | fee outlay | P(≥1 funded) | E funded accounts | E payouts (12 mo) | E net |
|---|---|---|---|---|---|
| 1 | $540 | 74 % | 0.74 | $9.2k | +$8.6k |
| 2 | $1,080 | 93 % | 1.48 | $18.5k | +$17.4k |
| 3 | $1,620 | 98 % | 2.23 | $27.8k | +$26.2k |
| 5 | $2,700 | 99.9 % | 3.71 | $46.3k | +$43.6k |

So **2–3 parallel attempts** is the practical launch configuration: ~$1.1–1.6k
of fees, 93–98 % chance of at least one funded account, and roughly
**$1.5k–3.1k/month expected** across the resulting accounts.

**Withdraw monthly.** The EV comes from payouts taken *before* the account
eventually blows (33 %/yr at k=0.75). Money not withdrawn is money at risk.

## Real payout mechanics (verified 2026-10-19, ftmo.com/faq)

* Funded "FTMO Account" = simulated capital, **real money rewards**. Only
  at-risk capital ever is the entry fee.
* **Reward claim from day 14** after the first funded trade (Account
  MetriX), then on demand; all positions must be closed to claim; review
  1–2 business days, sent 1–2 business days after invoice approval ⇒
  cash can land ~**every 14 days**, faster than the monthly cadence the
  MC assumed (slightly better economics — money leaves the risk pool
  sooner).
* Split: **80%** on 2-Step (90% once Scaling/Premium conditions met;
  1-Step/Instant are 90%). Entry fee **refunded with the first payout**.
* Methods: bank wire / Visa Direct / Mastercard Send (≤$20k), Skrill
  (≤$3k), crypto (min $50 closed profit; wire/Visa min $20).
* **Reward Rollover**: a 2-Step funded account can roll its reward into
  the *next* FTMO Account (paid out there at **100%** — no double ratio
  cut). This is the cheap way to grow from 1 → N accounts without paying
  full fees for each.
* Withdrawal does not reset the Maximum Loss Limit (Instant-account
  warning; static floor stays anchored to the initial balance either way).
* **FTMO Identity** (KYC) is required before the first Reward is processed
  — not to start trading.
* Taxes are the trader's responsibility.

## Honest limits

* The MC is a bootstrap of **one 3-year out-of-sample window** (plus 5.5 y
  in-sample). It cannot price regime change. Live results will be worse than
  the model if the arming edge decays.
* Selection: arm14 came out of a ~146-cell search. It was chosen on train and
  confirmed on test, but the search itself inflates the expected live edge.
* Model assumptions: $540 fee, 80 % split, monthly payouts, static max-loss
  floor on the initial balance, no swap/commission drag. CFD swap on
  multi-week stock positions is ≈ −1 to −1.5 %/yr at the measured ~55 %
  average exposure — small but real.
* Slippage: the backtest charges 3 bp per side. Live next-open execution
  measured −0.47 pp over 2.8 years vs close-fill, i.e. the fill convention is
  not where the edge dies.
* The MC assumes the *engine* trades the whole universe simultaneously.
  A live account must run all 67 symbols to reproduce the ledger; running 5
  names reproduces the shape but not the breadth.

## Exact settings

```
engine:  StrategyEngineV2M (clone of V2, ζ=0)
config:  gain_retrace_arm_pct = 14.0     (arm-family winner)
sizing:  k = 0.75  → 7.5 % of equity per position
universe: 67 US single-name equities (daily bars) — the only market with
          edge AND breadth; intl/sector/comm books add +1.1 pp CAGR but fatten
          the daily tail, so stocks-only for the prop account
firm:    FTMO 2-Step (10/5, 5 % daily, static 10 %, no time limit)
exec:    signal at bar close, fill at next open (measured cost −0.47 pp/2.8 y)
```
