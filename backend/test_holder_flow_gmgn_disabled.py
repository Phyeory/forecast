"""
Holder-flow GMGN poll disablement tests (user directive 2026-11-28).

holder_flow's GMGN smartmoney poll + wallet-registry fetches were the dominant
per-IP GMGN quota consumer; because `rate_limiter` couples all consumers, its
overuse banned the autofeed's discovery poll and holder screen too.  The poll
is now disabled by default (PUMPCHART_HF_GMGN_POLL=1 re-enables) and the
iter66 on-chain watcher remains the primary event source.

Run:  cd backend && python -m pytest test_holder_flow_gmgn_disabled.py -q
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import holder_flow as hf


def test_gmgn_poll_disabled_by_default():
    assert hf.GMGN_POLL_ENABLED is False


def test_start_skips_gmgn_loops_but_runs():
    async def scenario():
        m = hf.HolderFlowMonitor()
        m._api_key = "test-key"
        await m.start()
        try:
            assert m._running is True
            assert m._poll_task is None, "smartmoney poll must not start"
            assert m._registry_task is None, "registry loop must not start"
            assert m._session is not None, "HTTP session still needed"
        finally:
            await m.stop()
        assert m._running is False

    asyncio.run(scenario())


def test_env_flag_reenables_gmgn_loops(monkeypatch):
    monkeypatch.setattr(hf, "GMGN_POLL_ENABLED", True)

    async def scenario():
        m = hf.HolderFlowMonitor()
        m._api_key = "test-key"
        await m.start()
        try:
            assert m._poll_task is not None
            assert m._registry_task is not None
        finally:
            await m.stop()
        assert m._poll_task is None and m._registry_task is None

    asyncio.run(scenario())


def test_watch_token_still_queues_onchain_watch():
    async def scenario():
        m = hf.HolderFlowMonitor()
        m._api_key = "test-key"
        await m.start()          # GMGN loops skipped
        try:
            m.watch_token("MINT" + "x" * 32, recording_id=1)
            assert "MINT" + "x" * 32 in m._watched
        finally:
            await m.stop()

    asyncio.run(scenario())
