"""DEGEN decision model — our own Jev-role decision engine.

A typed decision contract over an LLM substrate (GLM Flash by default).  Every
decision point — qualify / arm / re-look / exit — sends a fixed decision vector
and receives a typed verdict:

    {action: buy|arm|watch|skip|exit|hold|abstain,
     p_up: 0-1, conviction: 0-1,
     reasons: [enum…], trap_params?: {…}, rationale?: ≤280 chars}

Zero free text in hot paths; the bounded rationale is for the journal only.
Every call is journaled with its input hash, prompt version, model and latency
so p_up can later be recalibrated against outcomes and distilled into our own
local classifier (RESEARCH.md §2).

Ownership rules: schema/prompts/versioning live here; vendor APIs are a
substrate, never a brain.  The model NEVER supplies position sizes or prices —
sizes come from the user's one-time seatbelt config; prices come from the tape.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("degen.decision")

PROMPT_VERSION = "degen-trader-v1"

ACTIONS = ("buy", "arm", "watch", "skip", "exit", "hold", "abstain")

REASON_VOCAB = [
    # tape / chart
    "nuke_structure_ok", "nuke_too_deep", "momentum_confirmed", "no_momentum",
    "chart_real", "chart_fake", "chart_staircase", "dev_buy_dominant",
    "buying_the_nuke", "buying_the_top", "consolidation_holding", "floor_broken",
    # holders / safety
    "bundle_fingerprint", "holder_over_4pct", "top10_heavy", "mint_or_freeze_active",
    "unsellable", "sell_impact_high", "forensics_clean",
    # narrative / community
    "narrative_strong", "narrative_weak", "community_alive", "community_dead",
    "watcher_ratio_bad", "ticker_rerun", "socials_missing", "socials_ok",
    "tracked_wallets_too_many",
    # dev
    "dev_reputable", "dev_unknown", "dev_serial_launcher", "fake_migration_risk",
    # organic / risk
    "organic_volume", "wash_suspect", "age_ok", "too_old", "mcap_in_band",
    "mcap_out_of_band", "already_moved", "risk_reward_asymmetric",
    "vendor_disagrees", "insufficient_data",
]

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "p_up": {"type": "number"},
        "conviction": {"type": "number"},
        "reasons": {"type": "array", "items": {"type": "string", "enum": REASON_VOCAB},
                    "maxItems": 6},
        "trap_params": {
            "type": "object",
            "properties": {
                "nuke_min_pct": {"type": "number"},
                "max_drawdown_pct": {"type": "number"},
                "up_ticks": {"type": "integer"},
                "reclaim_pct": {"type": "number"},
                "expires_in_s": {"type": "number"},
                "vol_flip": {"type": "boolean"},
            },
        },
        "rationale": {"type": "string"},
    },
    "required": ["action", "p_up", "conviction", "reasons"],
}

SYSTEM_PROMPT = """You are the decision engine of a personal memecoin trader that
replicates how skilled human traders on pump.fun actually make money. You are NOT a
quant system: no indicators, no stop-loss orders, no position math. You judge coins the
way a good degen does — narrative, community, holder forensics, chart personality, and
timing — and you always respect the playbook.

Playbook (Setuh-verified):
- New pairs: never buy the top or the rising staircase. Wait for the run-up to dump;
  buy THE MOMENTUM AFTER THE NUKE toward the floor (50-60% drawdowns), when real buyers
  step back in. Floor entry risks 10-20%; tops risk 80-90%.
- Final stretch (85-97% bonded, your preferred venue): buy the bundle/tracked-wallet
  dump back to the floor, or the 40% dip on 20-35k coins — never a 20% dip (gambling),
  never a 60%+ one (broken).
- Bundles: any non-LP holder >4%, or 3+ top holders with identical remaining-%, or
  identical SOL balances, or one funder = manufactured chart → skip.
- Community coins beat tweet coins; profile coins and news coins are someone else's
  exit. High watchers-to-holders is good; ~30% means multi-wallets. A ticker that
  already bonded = saturated rerun.
- Graduated coins: buy the sideways consolidation above a held 20-50k floor after a
  60-70% nuke from a 100-220k ATH; exit on the breakout. Never buy peaks or shallow dips.
- Exits are judgment: sell into strength, when the narrative dies, when the community
  goes quiet, or when the structure breaks. State the reason.

You output ONLY the typed verdict object. p_up is your calibrated probability the
position is higher in 15 minutes after entry; conviction is how much you'd stake your
own streak on this call. Abstain when data is thin — overconfidence is your known
failure mode. When arming a nuke trap, set trap_params to what the tape must do
(a nuke of nuke_min_pct then an up-tick streak with buys overtaking sells) — never a
position size."""


# ── journal ──────────────────────────────────────────────────────────────────

class TraderJournal:
    """Append-only JSONL journal + the playbook note the model re-reads."""

    def __init__(self, dir_path: Path):
        self.dir = Path(dir_path)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.journal = self.dir / "trader_journal.jsonl"
        self.playbook = self.dir / "playbook.md"
        if not self.playbook.exists():
            self.playbook.write_text(
                "# Trader playbook\n\n"
                "This file is the agent's own notebook. The review tier appends "
                "post-trade lessons; the decision model re-reads it each call.\n",
                encoding="utf-8",
            )

    def append(self, kind: str, payload: dict) -> None:
        rec = {"ts": time.time(), "kind": kind, **payload}
        try:
            with self.journal.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except Exception:
            logger.exception("[DegenDecision] journal write failed")

    def read_playbook(self, limit_chars: int = 4_000) -> str:
        try:
            text = self.playbook.read_text(encoding="utf-8")
            return text[-limit_chars:]
        except Exception:
            return ""

    def append_playbook(self, text: str) -> None:
        with self.playbook.open("a", encoding="utf-8") as f:
            f.write(f"\n## {time.strftime('%Y-%m-%d %H:%M', time.localtime())}\n{text}\n")


# ── calibration store ────────────────────────────────────────────────────────

class CalibrationStore:
    """p_up predictions → outcomes.  Reliability bins + Brier; v1 reporting only
    (the gating thresholds stay hand-set until there is data to recalibrate)."""

    def __init__(self, journal: TraderJournal):
        self.journal = journal

    def record_outcome(self, decision_id: str, went_up: bool) -> None:
        self.journal.append("p_up_outcome", {"decision_id": decision_id, "went_up": bool(went_up)})

    def reliability(self) -> dict:
        preds, outs = {}, {}
        try:
            for line in self.journal.journal.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("kind") == "decision" and rec.get("id"):
                    preds[rec["id"]] = rec.get("decision", {}).get("p_up")
                elif rec.get("kind") == "p_up_outcome":
                    outs[rec.get("decision_id")] = rec.get("went_up")
        except FileNotFoundError:
            pass
        bins = {b: [0, 0] for b in range(5)}   # (count, up_count) per 0.2 bin
        briers = []
        for did, p in preds.items():
            if did not in outs or p is None:
                continue
            b = min(4, int(p * 5))
            bins[b][0] += 1
            bins[b][1] += 1 if outs[did] else 0
            briers.append((p - (1.0 if outs[did] else 0.0)) ** 2)
        return {
            "n_scored": len(briers),
            "brier": round(sum(briers) / len(briers), 4) if briers else None,
            "bins": {f"{b * 0.2:.1f}-{(b + 1) * 0.2:.1f}":
                     {"n": v[0], "realized_up": round(v[1] / v[0], 3) if v[0] else None}
                     for b, v in bins.items()},
        }


# ── the decision ─────────────────────────────────────────────────────────────

@dataclass
class Decision:
    decision_id: str
    point: str
    mint: str
    action: str
    p_up: float
    conviction: float
    reasons: list[str]
    trap_params: Optional[dict]
    rationale: str
    model: str
    latency_s: float
    prompt_version: str
    ts: float
    raw: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _vector_hash(vector: dict) -> str:
    return hashlib.sha1(json.dumps(vector, sort_keys=True, default=str).encode()).hexdigest()[:12]


def _extract_json(text: str) -> Optional[dict]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def _clamp(v: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return lo


class DecisionEngine:
    """Typed decision calls against an LLM substrate.

    Provider detection (in order): DEGEN_BASE_URL forces an endpoint; else
    OPENROUTER_API_KEY → openrouter.ai; else ZAI_API_KEY → Z.ai; else
    OPENAI_API_KEY → OpenAI.  DEGEN_MODEL / DEGEN_ANALYST_MODEL name the models
    (provider-qualified, e.g. `z-ai/glm-5.3-flash` on OpenRouter); both default
    to the GLM-5.3-Flash class for the ~1s typed-decision cadence.
    """

    DEFAULT_MODELS = {
        "openrouter": "z-ai/glm-5.3-flash",
        "zai": "glm-5.3-flash",
        "openai": "gpt-4o-mini",
    }

    def __init__(self, journal: TraderJournal, model: Optional[str] = None,
                 analyst_model: Optional[str] = None, timeout_s: float = 20.0):
        self.journal = journal
        self.calibration = CalibrationStore(journal)
        self.timeout_s = timeout_s
        self.prompt_version = PROMPT_VERSION

        if os.environ.get("OPENROUTER_API_KEY"):
            self.provider = "openrouter"
        elif os.environ.get("ZAI_API_KEY"):
            self.provider = "zai"
        elif os.environ.get("OPENAI_API_KEY"):
            self.provider = "openai"
        else:
            self.provider = "openrouter"   # no key — health() will show has_key=False
        self.api_key = {
            "openrouter": os.environ.get("OPENROUTER_API_KEY", ""),
            "zai": os.environ.get("ZAI_API_KEY", ""),
            "openai": os.environ.get("OPENAI_API_KEY", ""),
        }[self.provider]
        self.base_url = os.environ.get("DEGEN_BASE_URL") or {
            "openrouter": "https://openrouter.ai/api/v1",
            "zai": "https://api.z.ai/api/paas/v4",
            "openai": "https://api.openai.com/v1",
        }[self.provider]
        self.model = model or os.environ.get("DEGEN_MODEL") or self.DEFAULT_MODELS[self.provider]
        self.analyst_model = analyst_model or os.environ.get("DEGEN_ANALYST_MODEL") or self.model
        self._session: Optional[aiohttp.ClientSession] = None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_session(self) -> "aiohttp.ClientSession":
        import aiohttp
        if self._session is None or self._session.closed:
            headers = {"Authorization": f"Bearer {self.api_key}",
                       "Content-Type": "application/json"}
            if self.provider == "openrouter":
                # OpenRouter attribution headers (optional but recommended)
                headers["X-Title"] = "pump-chart-degen"
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_s),
                headers=headers,
            )
        return self._session

    async def _chat(self, model: str, user_prompt: str, temperature: float = 0.0) -> tuple[str, float]:
        sess = await self._get_session()
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            # glm-5.3-flash is a reasoning model: completion tokens INCLUDE the
            # reasoning trace.  Cap the thinking so the typed answer always fits
            # (OpenRouter reasoning control — ignored harmlessly if unsupported).
            "reasoning": {"max_tokens": 800},
            "max_tokens": 3000,
        }
        t0 = time.time()
        async with sess.post(f"{self.base_url}/chat/completions", json=body) as resp:
            if resp.status != 200:
                raise RuntimeError(f"LLM HTTP {resp.status}: {(await resp.text())[:200]}")
            data = await resp.json()
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        return content, time.time() - t0

    # ── the typed call ───────────────────────────────────────────────────

    async def decide(self, point: str, mint: str, vector: dict,
                     playbook: bool = True) -> Decision:
        """point: qualify | arm_check | manage | review_context — everything is
        the same typed contract, only the vector differs."""
        user = {
            "decision_point": point,
            "decision_vector": vector,
            "playbook_tail": self.journal.read_playbook() if playbook else "",
            "output_contract": OUTPUT_SCHEMA,
            "instructions": (
                "Return ONLY a JSON object matching output_contract. "
                "reasons must come from the enum. No position sizes anywhere."
            ),
        }
        raw, latency = "", 0.0
        parsed: Optional[dict] = None
        err = ""
        try:
            raw, latency = await self._chat(self.model, json.dumps(user, ensure_ascii=False, default=str))
            parsed = _extract_json(raw)
            if parsed is None:
                err = "unparseable output"
        except Exception as e:
            err = str(e)

        if parsed is None:
            # fail-safe: abstain, never guess
            dec = Decision(
                decision_id=f"dec-{uuid4hex()}", point=point, mint=mint,
                action="abstain", p_up=0.0, conviction=0.0,
                reasons=["insufficient_data"], trap_params=None,
                rationale=f"engine error: {err[:180]}", model=self.model,
                latency_s=round(latency, 3), prompt_version=self.prompt_version,
                ts=time.time(),
            )
        else:
            reasons = [r for r in (parsed.get("reasons") or []) if r in REASON_VOCAB][:6] \
                      or ["insufficient_data"]
            tp = parsed.get("trap_params")
            dec = Decision(
                decision_id=f"dec-{uuid4hex()}", point=point, mint=mint,
                action=parsed.get("action") if parsed.get("action") in ACTIONS else "abstain",
                p_up=_clamp(parsed.get("p_up", 0), 0.0, 1.0),
                conviction=_clamp(parsed.get("conviction", 0), 0.0, 1.0),
                reasons=reasons,
                trap_params=tp if isinstance(tp, dict) else None,
                rationale=str(parsed.get("rationale") or "")[:280],
                model=self.model, latency_s=round(latency, 3),
                prompt_version=self.prompt_version, ts=time.time(), raw=raw[:400],
            )
        self.journal.append("decision", {
            "id": dec.decision_id, "point": dec.point, "mint": mint,
            "vector_hash": _vector_hash(vector),
            "prompt_version": dec.prompt_version, "model": dec.model,
            "latency_s": dec.latency_s, "decision": dec.to_dict(),
        })
        return dec

    # ── analyst tier (research narrative / review) ───────────────────────

    async def analyst(self, prompt: str, max_tokens: int = 900) -> tuple[str, float]:
        sess = await self._get_session()
        body = {
            "model": self.analyst_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens + 400,
        }
        t0 = time.time()
        async with sess.post(f"{self.base_url}/chat/completions", json=body) as resp:
            if resp.status != 200:
                raise RuntimeError(f"LLM HTTP {resp.status}: {(await resp.text())[:200]}")
            data = await resp.json()
        return (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "",
                time.time() - t0)

    def health(self) -> dict:
        return {
            "model": self.model,
            "analyst_model": self.analyst_model,
            "base_url": self.base_url,
            "has_key": bool(self.api_key),
            "prompt_version": self.prompt_version,
        }


def uuid4hex() -> str:
    import uuid
    return uuid.uuid4().hex[:10]
