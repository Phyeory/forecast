# Coin Screening & Filtering System — Agent Prompt

## Goal

Build an internal coin-screening and filtering system for our automated memecoin trading pipeline. Its purpose is to identify and reject **bundled, insider-manipulated, or "dead" coins** before we trade them, so we stop eating slow-bleed losses on coins that were set up to be dumped from day one.

Two things motivate this:

1. **The third-party risk metrics we currently rely on are not trustworthy.** Their bundle/dev/insider labels are sparse, backfilled inconsistently, and too often wrong. We can no longer base entry decisions on a single vendor's aggregate score.
2. **Our trading logs show a persistent tail of ~forced-exit losers** (positions we hold in live and are forced to sell when a session ends) — coins that bleed out slowly because they were never organic to begin with. We believe many are manipulated/bundled launches the vendor labeler failed to flag.

The system must produce its **own** evidence from first principles, rather than trusting any pre-computed risk score — and it must be **resistant to operators who actively try to hide**. The measure of success is catching clusters that make themselves look clean to naive tools.

---

## Features

**1. Detect coordinated buying at launch (the "bundle")**
- Identify when a group of wallets acquires meaningful supply in the same instant, before the public could reasonably react.
- Distinguish genuine early demand from coordinated buying using cluster size and supply taken as the core signals, not raw transaction count.
- Filter false positives from traders or sniping bots that happen to land together — only count wallets that *consistently* act as a coordinated group over time, not one-off co-blockers.
- Exclude system holders (liquidity pools, the bonding curve) when measuring supply so the numbers aren't inflated by design.

**2. Fingerprint wallets by behavior, not just by visible links (the core differentiator)**

A naive "wallet graph" — drawing a line between wallets that share one obvious funder — is exactly what sophisticated operators already evade by laundering their trail through exchanges and mixers. Do **not** rely on that. Instead, build a multi-dimensional behavioral fingerprint per wallet and look for wallets that behave like they're one person:

- **Timing-linked funding:** wallets that all became funded at the same moment, even if from different-looking sources, are a cohort preparing for the same event.
- **Birth cohort alignment:** wallets created within the same short window immediately before a launch.
- **Structural similarity:** wallets that share suspiciously matching traits — similar funding amounts, similar starting balances, similar operational patterns — rather than relying on a single shared address.
- **Lifecycle coincidence:** wallets with the same spread of other coins they've bought and sold, in the same order and timing — they've marched through the same plays together before.
- **Behavioral orphaning:** wallets that are active only around this token and its siblings, then go silent.

The goal is to cluster wallets by *"are these acting as a single coordinated entity"* across **many independent dimensions simultaneously**, so that no single countermeasure (a fresh funder, a mixer, a new address) is enough to break the fingerprint. A cluster should only be strongly flagged when several of these dimensions agree — each one alone is weak, the convergence is what matters.

**3. Flag fresh-wallet cohorts**
- Detect when many wallets were *created* shortly before the token launched, bought early, then fell silent — prepared insider wallets rather than real people discovering a coin.

**4. Measure effective supply concentration**
- Report holder concentration *excluding* the dev and already-detected cluster wallets, so we see what real outside buyers actually control.
- Report the difference between how much supply was bundled at launch vs how much those wallets *still hold now* — a bundle that farmed and re-bought is a live dump risk; one that already exited is not.

**5. Assess the developer's past behavior**
- Track the deployer across launches: how many, how many died, average survival time, and how quickly they fire off new projects.
- Factor in where and how the dev's wallet was funded.
- A repeated pattern of dead launches from the same dev is a strong negative even if this coin looks clean.

**6. Measure the realness of trading interest, not just volume**
- Estimate how much volume comes from genuine independent wallets vs bots/wash trading/churning market makers.
- Look for mechanical wash signatures: a few wallets driving most volume, per-wallet buy≈sell near-equality, thin volume relative to liquidity.

**7. Combine everything into one screening decision**
- Turn all of the above into a single risk picture per candidate (or a small number of clear sub-signals) the pipeline can gate on.
- Keep each component independently interpretable so a reviewer can see *why* a coin was rejected and which evidence drove it.
- Allow sub-signals to be weighted or selectively enabled — no signal is proof alone, the *stack* is what matters.

---

## Design Goals

- **Self-owned and auditable:** every rejection must be explainable from raw on-chain evidence we can show, not an opaque vendor number.
- **Deterministic and consistent across all execution modes:** backtesting, paper trading, and live trading must reach the same verdict on the same data, so we can validate the filter offline against recorded history before trusting it live.
- **Recordable:** persist the full set of signals and verdict for every screened candidate so we can replay decisions, correlate with outcomes, and prove the filter improves results rather than betting on intuition.
- **Evasion-resistant first:** assume the opponent already knows the standard detection tricks and actively tries to look clean. Structural/behavioral clustering that holds up under obfuscation is the priority, not a graph that a single mixer hop defeats.
- **Aim at our specific failure:** the primary target is the forced-exit slow-bleed tail; filtering bundled dumpers in general is a bonus — but don't sacrifice genuinely organic trading activity to stamp out bundles.

## Acceptance

The screening signals must be built from data we can produce ourselves, and the whole thing must be testable by replaying historical sessions to confirm rejected coins were in fact the losers and accepted coins were not, before it gates any live trades.