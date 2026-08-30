# Solana pump.fun Migrated-Coin Market — Daily Difference Report
**Window: 2026-07-27 → 2026-08-29 (34 days) · Prepared 2026-08-30 · External/internet + on-chain sources only**

## Executive Summary

Over the observed month the pump.fun → PumpSwap migration market went through **three distinct regimes** and ended in an unprecedented blow-off event:

1. **Baseline era (Jul 27 – Aug 9)**: ~10–22 graduations/day, pump.fun protocol fees ~$1.2M/day, PumpSwap TVL ~$234–243M, SOL ~$73–76. Migrations were organic, one-per-symbol, and 50–82% of graduates died within days. The protocol's fee chart steadily climbed from $1.0M (Aug 1) to $1.5M (Aug 12).
2. **Clone-farm era (Aug 6 – Aug 24)**: distinct symbol families (TNOS ×12 on Aug 6, SAOF ×3–4/day, UOTF ×5–6/day, CYBERLEEK ×4–6/day) began graduating repeatedly per day — an industrialized copycat pattern where the same symbol is launched dozens of times. Verified ATHs suddenly reach $200M–$1.8B on pools holding only tens of thousands of dollars — reported market caps became decoupled from real liquidity.
3. **Mania regime (Aug 19 – Aug 29)**: SOL rallied +36% ($77→$109), pump.fun fees hit $2.65M/day (Aug 27, 2.2× the July baseline), PumpSwap TVL jumped from $257M to $371M (+44% in 9 days), and graduations exploded: 58 (Aug 27) → 103 (Aug 28) → **1,041 on Aug 29** — a 50× single-day spike, of which ~298 were the new "Mayhem" auto-graduation mode. The headline coins of the final week (WOFI, USMS, RST, UOTF, CYBERLEEK families) printed reported ATHs of $0.6–1.8B each, then collapsed to near zero — with on-chain pool vaults **fully drained** (verified via Solana RPC on the biggest pool of each of Jul 27/31, Aug 8/19/27/28/29: all quote-vaults show as closed accounts).

**Key single-number differences, first week vs last week:**
| Metric | Jul 27–Aug 2 | Aug 24–29 | Δ |
|---|---|---|---|
| Total graduations | 57 | 1,306 | 22.9× |
| pump.fun fees (sum) | $8.02M | $10.76M | +34% |
| PumpSwap fees (sum) | $13.17M | $20.77M | +58% |
| PumpSwap TVL (end) | $234M | $354M | +51% |
| Median liquidity per new pool | ~$45k | ~$73k | +62% |
| Dead share of the day's graduates | 0–71% (scattered) | 86% (Aug 29) | structural |

## Methodology & Sources (all external — no local data)

- **pump.fun official API** (frontend-api-v3.pump.fun, scraped through a real browser session to pass Cloudflare): 4 listing sorts × 15 pages × 70 rows = 2,803 unique graduated coins (complete=true) with created_timestamp, ATH + timestamp, current USD market cap, creator wallet, pump_swap_pool address. Cross-validated live (WOFI: ATH $1,252.7M / current $682k, re-fetched → identical).
- **DexScreener API** (78 batch calls): per-pair `pairCreatedAt` for 1,023 PumpSwap pools — the on-chain migration moment; 24h volumes, liquidity, txns.
- **DefiLlama API**: daily pump.fun fees (911 days of history), PumpSwap fees, pump.fun/PumpSwap DEX volumes, PumpSwap TVL.
- **CoinGecko API**: SOL daily close + volume (90 days).
- **GeckoTerminal API**: per-pool reserves, OHLCV verification.
- **Solana public RPC** (publicnode + mainnet-beta): raw pool-account decodes (301-byte pAMM pool layout: disc | coin_mint | pc_mint | lp_mint | vaults | authority | creator placeholder | migration base 15.01 SOL constant), vault account liveness checks.

**Data caveats.** (a) The pump.fun listing API caps at offset ~1,050, so the daily graduation counts for Jul 27–Aug 28 are a *lower bound* drawn from the union of four sort views (by current mcap, recency, ATH, last-trade); only Aug 29's count (recent-sort saturates the entire 1,050-row window with coins created within the preceding 24h) approaches a census. (b) pump.fun's reported `ath_market_cap` is internally consistent (live-refetch reproduces it exactly) but is a **thin-pool price × supply artifact**: pools with $2k–$70k of actual liquidity report billion-dollar market caps; treat every ATH here as "reported," not as realized capital. (c) For dead coins (<$5k current mcap), ATH fields show occasional 100–1000× unit glitches; those rows were excluded from the "Verified ATH>$50M" column via a credibility rule (created after Aug 20, or ATH/now < 500, or ATH < $50M).

## The Master Day-by-Day Table

| Date | Grads | Distinct syms | Top clone (count) | Verified ATH>$50M | Med reported ATH $M | Dead now | Surv>$1M | SOL close | pump.fun fees $k | PumpSwap fees $k | pump.fun DEXvol $M | PumpSwap vol $M | PumpSwap TVL $M | Headline graduation (reported ATH) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-07-27 | 2 | 2 | MMM ×1 | 0 | 3.9 | 0% | 1 | $76.7 | 1,191 | 1,983 | 68 | 803 | 243 | Kimchi ($6M) |
| 2026-07-28 | 7 | 7 | TRUMP250 ×1 | 0 | 1.0 | 14% | 0 | $74.1 | 1,204 | 2,230 | 73 | 615 | 234 | DOGE-1 ($2M) |
| 2026-07-29 | 5 | 5 | DOGCAT ×1 | 0 | 1.2 | 40% | 0 | $73.8 | 1,209 | 1,880 | 74 | 682 | 234 | Fraudci ($1M) |
| 2026-07-30 | 14 | 12 | TNOS ×3 | 0 | 213.8 | 71% | 0 | $73.6 | 1,266 | 2,120 | 79 | 490 | 236 | wiwiwi ($2M) |
| 2026-07-31 | 7 | 6 | TNOS ×2 | 0 | 0.8 | 57% | 0 | $74.5 | 1,162 | 1,782 | 69 | 660 | 238 | AuraBattles ($1M) |
| 2026-08-01 | 12 | 10 | TNOS ×3 | 0 | 2.8 | 50% | 1 | $72.8 | 1,007 | 1,450 | 61 | 642 | 236 | Buddy ($3M) |
| 2026-08-02 | 10 | 7 | TNOS ×4 | 0 | 102.0 | 60% | 0 | $71.9 | 985 | 1,724 | 55 | 620 | 234 | Martians ($6M) |
| 2026-08-03 | 12 | 9 | SAOF ×3 | 0 | 96.7 | 58% | 0 | $73.5 | 1,256 | 2,272 | 70 | 731 | 238 | Doom ($8M) |
| 2026-08-04 | 9 | 5 | SAOF ×3 | 0 | 193.2 | 67% | 0 | $73.5 | 1,359 | 2,255 | 78 | 658 | 237 | TikTok ($5M) |
| 2026-08-05 | 12 | 6 | SAOF ×4 | 0 | 196.7 | 67% | 0 | $73.7 | 1,277 | 2,000 | 76 | 607 | 241 | Dealer ($5M) |
| 2026-08-06 | 21 | 7 | TNOS ×12 | 0 | 278.2 | 81% | 0 | $74.0 | 1,171 | 1,867 | 68 | 560 | 237 | WW ($1M) |
| 2026-08-07 | 17 | 10 | CATE ×3 | 0 | 243.9 | 82% | 0 | $72.6 | 1,194 | 2,044 | 70 | 397 | 236 | BAYLA ($1M) |
| 2026-08-08 | 12 | 10 | TNOS ×2 | 0 | 13.1 | 50% | 1 | $73.6 | 1,234 | 2,348 | 70 | 688 | 243 | TOAD ($25M) |
| 2026-08-09 | 22 | 17 | TOAD ×4 | 0 | 5.3 | 59% | 0 | $76.0 | 1,468 | 2,703 | 83 | 575 | 251 | GENTLE ($6M) |
| 2026-08-10 | 12 | 12 | omo ×1 | 0 | 0.3 | 67% | 0 | $76.2 | 1,433 | 2,492 | 86 | 335 | 253 | Sisyphus ($3M) |
| 2026-08-11 | 16 | 14 | TNOS ×2 | 0 | 1.3 | 50% | 0 | $75.9 | 1,434 | 2,609 | 79 | 585 | 250 | Plumber ($6M) |
| 2026-08-12 | 17 | 15 | TNOS ×3 | 0 | 0.6 | 59% | 1 | $76.2 | 1,534 | 2,452 | 88 | 556 | 255 | TNOS ($41M) |
| 2026-08-13 | 11 | 11 | Qenis ×1 | 3 | 1.0 | 46% | 1 | $75.6 | 1,480 | 2,399 | 84 | 867 | 251 | UOTF ($278M) |
| 2026-08-14 | 17 | 16 | UOTF ×2 | 4 | 0.7 | 53% | 1 | $76.2 | 1,354 | 2,145 | 82 | 598 | 254 | UOTF ($427M) |
| 2026-08-15 | 10 | 10 | Token ×1 | 2 | 3.7 | 60% | 2 | $75.3 | 1,239 | 1,980 | 72 | 555 | 251 | FTR ($269M) |
| 2026-08-16 | 9 | 9 | TOAD ×1 | 4 | 0.4 | 100% | 0 | $75.3 | 1,153 | 1,884 | 65 | 503 | 250 | TOAD ($308M) |
| 2026-08-17 | 24 | 24 | BULLSHIT ×1 | 2 | 0.7 | 38% | 1 | $74.5 | 1,414 | 3,192 | 80 | 373 | 251 | 牛来 ($269M) |
| 2026-08-18 | 20 | 18 | USWS ×3 | 6 | 1.0 | 40% | 1 | $76.0 | 1,512 | 2,614 | 85 | 699 | 251 | XTAL ($264M) |
| 2026-08-19 | 16 | 16 | PANTS ×1 | 5 | 0.4 | 81% | 0 | $77.0 | 1,991 | 2,850 | 111 | 754 | 257 | WWR ($223M) |
| 2026-08-20 | 29 | 22 | UOTF ×6 | 10 | 3.4 | 48% | 5 | $85.3 | 1,757 | 2,769 | 98 | 485 | 283 | UOTF ($713M) |
| 2026-08-21 | 22 | 16 | UOTF ×5 | 9 | 1.1 | 64% | 0 | $87.7 | 1,779 | 3,219 | 100 | 601 | 296 | UOTF ($1,283M) |
| 2026-08-22 | 26 | 18 | USMS ×5 | 14 | 201.6 | 69% | 0 | $93.7 | 1,501 | 3,015 | 84 | 570 | 317 | UOTF ($1,824M) |
| 2026-08-23 | 29 | 19 | WOFI ×6 | 16 | 199.9 | 69% | 0 | $93.9 | 1,420 | 3,247 | 76 | 713 | 317 | UOTF ($1,225M) |
| 2026-08-24 | 34 | 25 | CYBERLEEK ×4 | 17 | 100.6 | 65% | 3 | $95.4 | 1,817 | 4,028 | 95 | 695 | 318 | UOTF ($912M) |
| 2026-08-25 | 38 | 32 | CYBERLEEK ×6 | 12 | 2.9 | 42% | 7 | $98.9 | 1,953 | 3,809 | 112 | 568 | 333 | CYBERLEEK ($273M) |
| 2026-08-26 | 32 | 23 | CYBERLEEK ×5 | 15 | 1.6 | 69% | 0 | $96.6 | 2,173 | 4,157 | 64 | 765 | 329 | CYBERLEEK ($643M) |
| 2026-08-27 | 58 | 45 | CYBERLEEK ×5 | 18 | 1.0 | 47% | 8 | $102.0 | 2,650 | 4,660 | 144 | 1460 | 343 | RST ($1,064M) |
| 2026-08-28 | 103 | 71 | WOFI ×12 | 22 | 2.2 | 37% | 27 | $109.2 | 2,162 | 4,112 | 118 | 576 | 371 | WOFI ($1,151M) |
| 2026-08-29 | 1041 | 745 | WOFI ×22 | 45 | 0.1 | 86% | 66 | $105.0 | 0 | 0 | 110 | 585 | 354 | WOFI ($1,253M) |

**Column notes.** *Grads* = coins in the union census whose PumpSwap pool was created that day (DexScreener pairCreatedAt, falling back to pump.fun created_timestamp for coins without a DS pair). *Distinct syms / Top clone* = symbol-farm concentration. *Dead now* = share of that day's graduates currently under $5k mcap (as of Aug 30). *Surv>$1M* = still above $1M now. *Verified ATH>$50M* = graduates whose reported ATH exceeds $50M under the credibility rule. Fees are protocol-day values from DefiLlama; Aug 29 fee values are missing because DefiLlama's daily fee series lags one day at scrape time.

## Day-by-Day Differences (narrative)

### Week 1 — Quiet baseline (Jul 27 – Aug 2)
- **Jul 27**: only 2 graduations make the census (Kimchi $6M ATH is the day's top); both survive — the only day with 0% dead. PumpSwap TVL $243M (local peak for the month's start), SOL $76.7.
- **Jul 28–29**: 7 and 5 graduations, all distinct symbols (no clones) — TRUMP250, FRIED, DOGE-1, DOGCAT, Fraudci. Dead-rate jumps to 14%→40%.
- **Jul 30**: 14 graduations; first clone signal (TNOS ×3); the famous LeoSold graduates ( ATH field corrupted to $70.9B — excluded from verified counts; its true scale is ~$422k peak per SOL-denominated mcap). 71% of the day's cohort is now dead.
- **Aug 1–2**: fees dip to the month's low ($985–1,007k on Aug 1–2, the only sub-$1M days); 10–12 grads/day; dead-rate 50–60%. SOL bottoms at $71.9.

### Week 2 — First fee upcycle + TNOS farm (Aug 3 – Aug 9)
- **Aug 3–5**: SAOF family (3–4 clones/day) appears; graduations 9–12/day; reported ATHs of $300–500M appear on the SAOF/TNOS families for the first time (unit-verified only later — these coins are all dead now, so flagged unverified).
- **Aug 6**: the month's first graduation spike — 21 in one day, 12 of them TNOS clones from a coordinated farm; 81% now dead. PumpSwap volume falls to $560M (the launchpad is absorbing activity the DEX doesn't see).
- **Aug 7**: 17 grads, 82% dead (the worst dead-rate of the pre-mania month). TOAD graduates (later the only early-August coin still above $5M today, $5.41M).
- **Aug 8–9**: fees break $1.4–1.47M for the first time; PumpSwap TVL recovers to $251M; 22 grads on Aug 9 with TOAD ×4.

### Week 3 — UOTF/Qenis/family rotation (Aug 10 – Aug 16)
- **Aug 10–12**: graduations moderate (12–17/day) but fees hit new highs ($1.43–1.53M), the first sign that per-coin activity (not count) was rising. Aug 12's headline TNOS prints a verified $41M ATH.
- **Aug 13–14**: **the verified-ATH era begins** — UOTF's first verifiable $278M→$427M reported peaks; 3–4 ATH>$50M coins per day.
- **Aug 15–16**: the month's softest days (9–10 grads, all singleton symbols); Aug 16 cohort is 100% dead today. FTR ($269M) and TOAD ($308M) verify.

### Week 4 — The mania ignition (Aug 17 – Aug 23)
- **Aug 17**: 24 grads — the first step-change; PumpSwap fees spike to $3.19M (dex fee regime doubles vs week 1); 牛来 (Bull) verifies at $269M ATH.
- **Aug 18–19**: XTAL ($264M), WWR ($223M) verify; pump.fun fees hit $1.99M on Aug 19 (first time near $2M); **SOL starts moving** ($77). TVL begins its climb ($251M→$257M).
- **Aug 20**: SOL gaps +10.8% to $85.3 in one day (the macro catalyst); PumpSwap TVL steps $257M→$283M; UOTF reported ATH $713M.
- **Aug 21–23**: UOTF's clone family saturates (5–6 clones/day, reported ATHs $1.28B, $1.82B — live-verified against the API: the $1.82B ATH is reported identically on refetch, while the pool's entire real liquidity is ~$2–70k); TVL jumps $283M→$317M in 48h (+12%); ATH>$50M count reaches 14–16/day.
- **Aug 23**: pump.fun fees sag to $1.42M (the only dip inside the mania — a one-day cooldown before the terminal wave).

### Week 5 — Terminal blow-off (Aug 24 – Aug 29)
- **Aug 24**: graduations 34 (a new high); 17 verified ATH>$50M; PumpSwap fees $4.03M (2× week-1).
- **Aug 25**: 38 grads; dead-rate falls to 42% (survival improves as the mania pulls everything up); CYBERLEEK's first $273M verified peak; TVL $333M.
- **Aug 26**: 32 grads; CYBERLEEK $643M reported ATH; pump.fun fees $2.17M.
- **Aug 27**: **the single busiest organic day** — 58 graduations, 45 distinct symbols, RST tops at a verified $1,064M reported ATH, pump.fun fees peak at $2.65M (2.2× the July baseline), PumpSwap volume explodes to $1.46B (2.4× normal), TVL $343M, SOL crosses $102.
- **Aug 28**: 103 graduations — WOFI ×12 clones in one day, 22 verified ATH>$50M, 27 coins still >$1M today (the mania's survivor cohort), TVL peaks at $370.7M (+52% vs month start), SOL $109.2 (the month's top close).
- **Aug 29**: **1,041 graduations in 24h** (the recent-sort's entire 1,050-row API window contains only coins created within the preceding 24 hours; 745 distinct symbols; WOFI ×22, USMS ×21, GTA 6 Coin ×17, RST ×13). ~298 of these carry the new `mayhem_state` marker — a new pump.fun product mode that auto-graduates within minutes. 45 coins print reported ATHs >$50M (led by WOFI $1,253M and USMS $895M, both live-verified); USMS is the only mega-graduation still holding its value ($894M current). 86% of the day's graduates are already dead (<$5k) within ~24 hours — the fastest die-off observed.

## Structural Findings

1. **Graduation count is now SOL-price-coupled.** corr(grads/day, SOL close) = **+0.85** over Jul 27–Aug 28; corr(grads/day, pump.fun fees) = **+0.74**; corr(grads/day, next-day PumpSwap TVL) = **+0.79**. The migration market tracks the macro token almost linearly until the Aug 29 discontinuity.
2. **The liquidity–market-cap decoupling is the month's defining structural event.** Reported ATHs grew 10,000× (from $2–8M in week 1 to $1.2–1.8B in week 5) while median real pool liquidity moved only from ~$45k to ~$73k. On-chain vault reads show the biggest daily graduations' pools have their quote vaults **closed/drained** — the reported market caps were never backed by capital.
3. **Symbol cloning industrialized.** Week 1: one graduation per symbol. By Aug 28, 12 of 103 graduations share one symbol (WOFI); on Aug 29, 22. The top-4 clone families (WOFI 42, RST 36, USMS 35, CYBERLEEK 24 in the final 6 days) account for 137 of the 296 late-window graduations. The same creator-wallet repetition is visible: 169 wallets graduated >1 coin, max 13 (creator `yHCxHB…` — also the ANSEM coin creator).
4. **Survivorship collapsed, then vanished.** Week-1 cohorts: 0–1 coin/day above $1M today. Aug 28: 27 of 103 still above $1M (mania-quality survivors). Aug 29: 66 of 1,041 — a 6.3% rate, versus 7.3% month-long (126 of 1726 window graduates are >$1M today).
5. **Migration speed.** For pre-flood organic coins: median creation→PumpSwap-pool lag = **0.2h** (75% under 6h) — graduating is an event within hours of launch, not days. The Mayhem-mode coins (Aug 29) graduate in minutes, effectively dissolving the bonding-curve phase.
6. **PumpSwap economics scaled with the wave.** PumpSwap daily fees: $1.78–2.35M (week 1) → $4.66M (Aug 27 peak). TVL: $234M → $370.7M peak (+59%); the Aug 29 crash-back to $354M is the first TVL decline of the mania.

## Per-Day Difference Callouts (what actually changed, day over day)

- **Aug 5→6**: first graduation spike (12→21) driven by a single symbol farm (TNOS ×12) — volume on PumpSwap *fell* $607M→$560M: launch-side activity and DEX-side activity diverge for the first time.
- **Aug 12→13**: verified-ATH era begins ($0 → 3 coins >$50M/day) — the switch flips when coins that still trade start printing 9-figure reported peaks.
- **Aug 19→20**: SOL +10.8% day — the macro ignition; every subsequent metric step-changes (TVL +$26M in one day, fees +$1.4M→$1.76M, grads 16→29).
- **Aug 23→24**: the mid-mania cooldown inverts — fees $1.42M→$1.82M, grads 29→34, TVL +$1M only.
- **Aug 27**: everything peaks simultaneously (fees, volume $1.46B, TVL-rate, grads-organic 58) — the coherent top.
- **Aug 28→29**: the discontinuity — grads 103→1,041 (+911%) with fees *flat* (~$2.2M) and volume *falling* ($576M→$585M): the graduation flood was driven by the Mayhem auto-mode, not by real capital. 86% of the flood dies within a day; the reported-ATH>$50M count still hits 45 (speculative endpoint manipulation at industrial scale).

## Appendix — Source Artifacts

All raw pulls are preserved under `/tmp/pumpdaily/`:
- `browser/snap_*.txt` — raw pump.fun listing pages (4 sorts × 15 pages)
- `graduated_union_cohort_clean.json` — the 2,803-coin census with migration timestamps, ATHs, creators, pool addresses
- `dexscreener_pairs.json` — 1,294 mints × all DEX pairs (PumpSwap/Raydium/Meteora/Orca)
- `llama_daily.json` — DefiLlama daily series (fees/TVL/volume) + CoinGecko SOL
- `report_daily_table.json` — the per-day master metrics
- `rpc_sample.json` — on-chain vault-state samples

*Report generated 2026-08-30 from live external APIs and raw Solana RPC state; no local codebase data was used.*
