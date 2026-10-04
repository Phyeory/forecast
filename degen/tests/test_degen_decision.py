"""Decision model: typed contract validation, fail-safe abstention, journal."""

import json
import time

import pytest

from degen.decision import (ACTIONS, REASON_VOCAB, CalibrationStore, DecisionEngine,
                            TraderJournal, _extract_json)


@pytest.fixture
def journal(tmp_path):
    return TraderJournal(tmp_path / "journals")


def test_extract_json_tolerant():
    assert _extract_json('{"action": "skip"}') == {"action": "skip"}
    assert _extract_json('```json\n{"action": "buy", "p_up": 0.7}\n```')["action"] == "buy"
    assert _extract_json('blah blah {"action": "watch"} trailing') == {"action": "watch"}
    assert _extract_json("no json here") is None
    assert _extract_json("") is None


def test_engine_fails_safe_on_bad_output(journal, monkeypatch):
    """Unparseable / wrong-enum output must become abstain, never a guess."""
    engine = DecisionEngine(journal, model="test-model")

    async def fake_chat(model, prompt, temperature=0.0):
        return "I think you should ape in!!", 0.1

    monkeypatch.setattr(engine, "_chat", fake_chat)
    dec = engine.__class__.__dict__  # placeholder to keep flake quiet
    import asyncio
    decision = asyncio.run(
        engine.decide("qualify", "Mint11111111111111111111111111111111", {"event": "soon"}))
    assert decision.action == "abstain"
    assert decision.reasons == ["insufficient_data"]


def test_engine_validates_and_clamps(journal, monkeypatch):
    import asyncio
    engine = DecisionEngine(journal, model="test-model")

    async def fake_chat(model, prompt, temperature=0.0):
        payload = {
            "action": "NUKE IT",                 # invalid enum → abstain? no: → abstain
            "p_up": 7.0,                         # clamped
            "conviction": -1,                    # clamped
            "reasons": ["narrative_strong", "made_up_reason"],
            "rationale": "x" * 500,
        }
        return json.dumps(payload), 0.05

    monkeypatch.setattr(engine, "_chat", fake_chat)
    decision = asyncio.run(
        engine.decide("qualify", "Mint22222222222222222222222222222222", {}))
    # invalid action → abstain (fail-safe)
    assert decision.action == "abstain"
    # but a valid-action payload clamps and filters
    async def fake_chat2(model, prompt, temperature=0.0):
        return json.dumps({"action": "arm", "p_up": 7.0, "conviction": -1,
                           "reasons": ["narrative_strong", "made_up_reason"],
                           "trap_params": {"nuke_min_pct": 99}}), 0.05
    monkeypatch.setattr(engine, "_chat", fake_chat2)
    decision = asyncio.run(
        engine.decide("qualify", "Mint22222222222222222222222222222222", {}))
    assert decision.action == "arm"
    assert decision.p_up == 1.0 and decision.conviction == 0.0
    assert decision.reasons == ["narrative_strong"]        # vocab-filtered
    assert len(decision.rationale) <= 280


def test_journal_records_decisions(journal, monkeypatch):
    import asyncio
    engine = DecisionEngine(journal, model="test-model")

    async def fake_chat(model, prompt, temperature=0.0):
        return json.dumps({"action": "watch", "p_up": 0.6, "conviction": 0.5,
                           "reasons": ["mcap_in_band"]}), 0.02

    monkeypatch.setattr(engine, "_chat", fake_chat)
    asyncio.run(
        engine.decide("qualify", "Mint33333333333333333333333333333333", {}))
    lines = journal.journal.read_text().splitlines()
    rec = json.loads(lines[-1])
    assert rec["kind"] == "decision"
    assert rec["decision"]["action"] == "watch"
    assert rec["prompt_version"] == engine.prompt_version


def test_calibration_reliability(journal):
    store = CalibrationStore(journal)
    # two decisions + outcomes
    for i, (p, up) in enumerate([(0.9, True), (0.9, False), (0.2, False)]):
        did = f"dec-{i}"
        journal.append("decision", {"id": did, "point": "qualify", "mint": "M",
                                    "decision": {"p_up": p}})
        store.record_outcome(did, up)
    rel = store.reliability()
    assert rel["n_scored"] == 3
    # brier for (0.9,T)=0.01, (0.9,F)=0.81, (0.2,F)=0.04 → mean ≈ 0.2867
    assert abs(rel["brier"] - 0.2867) < 0.001
    assert rel["bins"]["0.8-1.0"]["n"] == 2


def test_no_position_size_field_reaches_model_contract():
    """The output schema must never offer the model a position-size knob."""
    schema_str = json.dumps(_schema_ref())
    assert "size" not in schema_str
    assert "amount" not in schema_str


def _schema_ref():
    from degen.decision import OUTPUT_SCHEMA
    return OUTPUT_SCHEMA
