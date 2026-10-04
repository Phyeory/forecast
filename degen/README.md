# DEGEN — AI degen trader (personal use)

An agent that trades like a skilled human memecoin trader: filters new pairs, researches
narrative/community/holders, watches the tape, **buys the momentum after the initial-dip
nuke**, sells by judgment. Not a trading algorithm — no TA indicators, no stop-loss
orders, no sizing math. First trader spec: Setuh's playbook (`RESEARCH.md` §1).

## Layout

```
degen/
  feed.py             new-pair + final-stretch scanner, Setuh filter table
  research.py         trader's research tools (holders/forensics, dev history, socials, sellability)
  vision.py           chart/banner/community screenshot reads (vision model)
  decision.py         THE DECISION MODEL: typed contract + calibration + trader journal
  agent.py            the loop: filter → research → look → decide → execute → manage → review
  traps.py            reflex tier: armed nuke→momentum triggers on the tick tape (no LLM)
  terminal_driver.py  Playwright: Terminal (trade.padre.gg) confirm-mode panel fills
  executor.py         latency-critical local swaps (Jupiter/PumpSwap, code-signed)
  router.py           FastAPI router: /api/degen/*, /ws/degen, serves the page
  web/index.html      STANDALONE page at http://localhost:8000/degen (not a dashboard tab)
  journals/           trader_journal.jsonl + playbook.md (gitignored)
```

## Tiers

- **Reflex (0 ms, pure code):** armed traps only. The decision model decided *whether*
  when it armed; the trigger decides *the millisecond*: nuke ≥X% off high toward floor +
  momentum resume (up-tick streak + buy/sell volume flip + nuke-low reclaim) →
  pre-authorized local swap. Aborts: bundle wallets still distributing, floor lost.
- **Decision model (GLM Flash, ~1 s):** typed contract
  `{action: buy|arm|watch|skip|exit|hold|abstain, p_up, conviction, reasons[], trap_params}`.
  Calibration + abstention first-class; journal feeds our own distilled GBM later.
- **Analyst (GLM flagship + vision, on demand):** research, chart reads, playbook notes.

## Mode

Default **shadow**: everything runs, decisions journal, no orders fire. Confirm/auto
per-lane switches on the page. Seatbelt: per-trade size set once, everything journaled,
traps only on decision-model-qualified coins.

## Run

Integrated into `backend/main.py` (~3 lines: include router + static mount); restart
`main.py`, open http://localhost:8000/degen. Provider is auto-detected from backend/.env:
`OPENROUTER_API_KEY` (current: `z-ai/glm-5.3-flash` on both tiers — a reasoning
model, so `degen/decision.py` caps reasoning tokens and sizes `max_tokens`
accordingly), or `ZAI_API_KEY` / `OPENAI_API_KEY`. Playwright: `pip install playwright && playwright
install chromium` (browser lane only).

Rules honored: no changes to V2 engines / engine-factory / parity pipeline / dashboard
tabs; DB isolation; all decision logic homegrown (vendor scores = cross-checks only).
