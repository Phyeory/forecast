# DEGEN — Research Foundations

Personal-use AI degen trader. Everything decision-side is homegrown: our own filters,
forensics, decision model, prompts, journals, execution. No third-party AI trading
products, no hosted-key execution, no copy-trading; vendor risk scores are cross-check
signals only, never verdicts.

Sources: Setuh "Memecoin Blueprint" Day 2 (Bundles, Jan 2026), Day 3 (Scanning, Mar 2026),
Day 4 (Scalping, Apr 2026) — all three transcripts captured and verified 2026-10-03
(watch-skill captions, vtt in temp; numbers below are verbatim-verified).
Execution-venue + decision-model + safety-tooling research: 2026-10-03 (3 parallel
research passes; URLs inlined where load-bearing).

---

## 1. Setuh playbook (verified against captions)

### 1.1 Filters (Terminal pages; SOL-anchored — he retunes as SOL moves)

| Page | Filter | Day 2 value | Day 4 value |
|---|---|---|---|
| New pairs | market cap min | 6,000 | **3,500** (launch-price anchored: "minimum launch price" + delta) |
| New pairs | token age max | 6 min | 6 min |
| Soon (final stretch) | token age max | 30 min | 30 min |
| Soon | pro traders min | 20 | 20 |
| Soon | market cap min | 8,500 | **6,000** ("the equivalent of 3,500 for our new pairs") |
| Migrated | pro traders min | 100 | 100 |
| Migrated | market cap min | 30,000 | 30,000 |
| New pairs | holders min | — | **1** (kills spam-deploy floods) |

Fees (Terminal P1 preset): priority 0.00001, tip 0.0001 ("one zero fewer than pryo"),
MEV protection OFF ("you will never get MEV'd on a low port"), buy slippage 20%,
sell slippage 50%.

### 1.2 Safety — bundle forensics (Day 2)

A bundle = one person controlling many wallets to accumulate 10-20%+ supply invisibly,
then clipping out on buyers. Dev can only buy ~3-4% directly with his own wallet.

Checks (in his order):
1. **Remaining column** (holder distribution): any single non-LP holder > **4%** → stay
   away. Identical remaining-% across top holders (e.g. top-5 all holding exactly 2.5%)
   = bundle fingerprint.
2. **Funded-by tab**: top holders funded from the same source at the same time
   (Coinbase/Kraken/FixedFloat, same day) = bundle. Varied funders/dates = clean.
3. **Terminal badges**: if all top-10 holders use the same trading platform = red flag.
   Fresh-wallet ("leaf") badges in the top-10 = bundle cohort.
4. **Top-10 trade-history overlap**: if top holders all bought the same prior coins =
   one person multi-walleting.
5. **Bubblemaps**: 4-5% clusters tolerable; multiple 6%+ clusters = bad. Explicitly
   "hit-or-miss" — mixers evade it; he treats it as a secondary check.

### 1.3 Scanning — narrative/community (Day 3)

- Copy trading: never (swears against it). Wallet tracking instead: import 100-200
  leaderboard wallets; **>2-3 tracked wallets inside a coin = hard reject** (they
  multi-wallet, hold 20%+, dump on the crowd that watches them).
- Coin types: **community coins** (best), **tweets with a community attached** (only
  tweet plays to trade), news coins (vamped hundreds of times — avoid), **profile
  coins** (worst — promo/shill vehicles, avoid).
- Community quality checks: watchers-to-holders ratio (his example: 90 watchers /
  300 holders = 30% = multi-wallet infested; want the ratio high); **OG check** —
  search the ticker for prior graduations (already-bonded same ticker = saturated
  rerun = reject; never-bonded = good); ignore Meteora-prefixed junk; community page:
  CA in description, pinned thesis post, active moderation; DEX-paid banner resolution
  (stretched/wrong-ratio banner = third-party launch tool = bundler); community
  liveliness (posts 5+ min old need 100+ views / 10+ likes).

### 1.4 Trading — scalping (Day 4, the money video)

Fake vs real charts (three fake patterns):
1. Big green candle + tiny sell + big green candle, repeating = bundle tool at work.
2. **Dev buy is the biggest candle on the chart** = scam (real devs buy 3-4%, bundle
   the rest — dev holding 20-40% via bundles).
3. Staircase only-up grind = classic scam chart.
Real charts: real dips from sniper/dev profit-taking, heterogeneous candles,
"personality" — real humans are trading.

**New pairs:** never quick-buy / never buy tops. Wait for the run-up (dev+bundle exit
zone ≈ 6-10k), then the dump back to the floor (~3-4k), then buy **the 50-60% dips**.
Asymmetry: floor entry max loss 10-20% vs buying the top. Take profit at **40-60% PnL**
(quick flips, low ports). **Mental stop -20%: "do not set a physical stop loss … if
it's down 20%, I'm going to full-stack out."**

**Final stretch ("soon" — his preferred venue):** floors are higher (≈7-8k).
- Method 1 — bundle/tracked-wallet dump: run to ~20k → 60% dump back to the 8k floor →
  buy the dump ("we want to be buying the nuke … then the coin can pretty much not
  nuke anymore"). Never buy 10-20% dips ("praying it doesn't nuke").
- Method 2 — the **40% dip method**: on coins at 20-35k, enter the 40% dip — not 20%
  (gambling), not 60% (already broken). Ride the recovery to ~2x.
- Method 3 — tracked dev wallet: known-good dev launches → snipers run it to ~10k →
  dev + snipers sell into **sideways consolidation** → coin rips 2-3x once sniper
  supply exhausts. Requires verifying the dev's prior tokens (no fake migrations).
- IMPORTANT (user correction, matches the tape): you do not buy the falling knife —
  you buy **the momentum after the nuke**: the dump completes, real buyers step in,
  the turn confirms, THEN you're in. Our reflex trigger is therefore
  nuke-complete → momentum-resume, never "buy while falling".

**Graduated/migrated scalping:** 1-3h old coins, ATH 100-220k, nuked 60-70% to a
20-50k floor, sideways consolidation that never breaks the floor → buy inside the
consolidation, exit on breakout of the range (often through the old ATH). Never buy
peaks or shallow dips (nothing stopping it going lower).

---

## 2. Decision model (our own, Jev-role)

- **Typed decision contract** at every decision point (qualify/arm/re-look/exit):
  fixed input schema (filters + forensics + tape + narrative + vision) → typed output
  `{action: buy|arm|watch|skip|exit|hold|abstain, p_up, conviction, reasons[], trap_params}`.
  Bounded rationale; zero free text in hot paths. "Jev" (TypeSafe AI, Sep 2026) is the
  pattern source only — zero-token typed decisions, ms latency; we build our own schema,
  prompts, calibration. Substrate: GLM Flash (Z.ai) for live decisions (~1s, <$0.001),
  GLM flagship + vision for research; env-pluggable override.
- **Calibration + abstention**: p_up journaled vs realized outcome; reliability/Brier
  recalibration; abstain is first-class (documented Jev-class failure = overconfidence
  on contested cases).
- **Distillation endpoint**: train our own GBM on journal features→outcomes (sub-ms,
  deterministic, backtestable). LLM engine stays as analyst + fallback.
- Literature anchors: TradingAgents (multi-agent debate, arXiv:2412.20138), FinMem
  (layered decaying memory), LLM-gated FinRL (LLM veto/scale, never create; journal
  every gate decision), FINSABER (arXiv:2505.07078: LLM strategies lose edge once
  look-ahead/memorization controlled → our journal-first, live-only overlay posture).
- Injection hardening: token names/descriptions/socials are untrusted input — schema-
  extracted, instruction-stripped before the model sees them (Bankr incident: 14 wallets
  drained via prompt injection through agent-facing content).

## 3. Execution

- **Latency-critical (nuke snipes)**: local programmatic executor (repo's Jupiter path),
  code-signed, broadcast <1s. Traps pre-authorized by the decision model; the reflex
  trigger is pure code on the 4-state candle tape.
- **Slow plays**: Terminal (trade.padre.gg) browser lane, confirm mode — agent fills
  the panel, human signs. Playwright profile, human logs in once.
- **URGENT repo-wide**: Jupiter `lite-api.jup.ag/swap/v1` is being retired (new portal
  2026-04-06; grace ended 2026-06-30; keyless survives at 0.5 RPS on api.jup.ag).
  Migrate to `api.jup.ag` + `/swap/v2` (order/execute); optional `x-api-key`
  ($25/mo Developer = 10 RPS). Axiom: no official API (unofficial SDK = Cloudflare
  Turnstile war, swap execution undocumented). Padre/Terminal: no API at all (pump.fun
  acquired Oct 2025). GMGN Agent API (beta 2026-03) exists but = hosted keys → excluded
  by our ownership rule. pump.fun official docs (github.com/pump-fun/pump-public-docs):
  PumpSwap AMM fee 0.25%; `buy_v2/sell_v2`; **Sep-2026: `virtual_quote_reserves` can be
  negative** → our pool_virtual_reserves resolver must handle it.

## 4. Safety forensics (all computed by our code; vendor scores cross-check only)

| Check | Source | Note |
|---|---|---|
| Mint/freeze authority revoked | RPC account read | hard reject |
| Token-2022 transfer-fee/hook, mutable metadata | RPC | hard reject on transfer-hook |
| Top-10 share + >4% single non-LP holder | Helius getTokenLargestAccounts | Setuh rule |
| Identical remaining-% across top holders | same | bundle fingerprint |
| Identical SOL balances across top holders | RPC | bundle fingerprint |
| Fresh-wallet cohorts, funder fan-out, same-slot buys | Bitquery / RPC sigs | bundle forensics |
| Sellability (sell quote + price impact) | Jupiter quote API | honeypot check |
| Wash heuristics: buyers-vs-txns, vol/liq, fees-vs-volume | DexScreener + pump.fun | organic check |
| Dev prior tokens / fake migrations | pump.fun profile / Bitquery | tracked-dev gate |
| Watchers/holders, ticker OG-check | Terminal/scrape, GMGN | narrative checks |
| RugCheck report/insiders, GoPlus sol | free APIs | cross-check signals only |

Free-stack latency ≈ 2-4s per candidate, concurrent.

## 5. Data plumbing budget (~$0 to start)

PumpPortal WS (free) + Solana RPC/Helius free tier + DexScreener (60 req/min) +
RugCheck/GoPlus free = the whole v1. Optional later: Bitquery $49/mo (bundle/deep
forensics), twitterapi.io ~$0.15/1k tweets (Twitter channel). Nitter dead (2026-08);
X API = pay-per-use $0.005/post read.
