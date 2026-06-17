"""
ForwardTester — Paper-trades the sniper strategy on live data with zero real capital.

Stores results in SQLite for analysis. Run for 500+ simulated trades before
enabling live execution.
"""

from __future__ import annotations
import json
import sqlite3
import time
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from sniper.entry_signal import EntrySignal
from sniper.exit_signal import ExitSignal
from sniper.launch_detector import LaunchDetector

logger = logging.getLogger(__name__)

SNIPER_DB = Path(__file__).parent.parent / "data" / "sniper.db"


def _get_conn() -> sqlite3.Connection:
    SNIPER_DB.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(SNIPER_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_sniper_db():
    """Create the forward test trades table if it doesn't exist."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS forward_test_trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            mint            TEXT NOT NULL,
            token_name      TEXT,
            token_symbol    TEXT,
            detected_at     REAL,
            entry_time      REAL,
            exit_time       REAL,
            entry_mc        REAL,
            exit_mc         REAL,
            spike_high_mc   REAL,
            floor_mc        REAL,
            dip_depth       REAL,
            fees_at_entry   REAL,
            entry_price_sol REAL,
            exit_price_sol  REAL,
            position_size   REAL,
            gross_pnl_sol   REAL,
            net_pnl_sol     REAL,
            pnl_pct         REAL,
            exit_trigger    TEXT,
            conviction      TEXT,
            buy_ratio_at_entry  REAL,
            volume_expansion_at_entry REAL,
            all_conditions  TEXT,
            created_at      REAL DEFAULT (unixepoch())
        );

        CREATE INDEX IF NOT EXISTS idx_ftt_mint ON forward_test_trades(mint);
        CREATE INDEX IF NOT EXISTS idx_ftt_created ON forward_test_trades(created_at DESC);
    """)
    conn.close()


class SniperForwardTester:
    """Paper trading module for the sniper dip-recovery strategy."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or str(SNIPER_DB)
        init_sniper_db()

    async def record_entry(
        self,
        entry_signal: EntrySignal,
        launch_detector: LaunchDetector,
    ) -> int:
        """Record a paper entry. Returns the trade ID."""
        conn = _get_conn()
        cur = conn.execute(
            """INSERT INTO forward_test_trades
               (mint, token_name, token_symbol, detected_at, entry_time,
                entry_mc, spike_high_mc, floor_mc, dip_depth, fees_at_entry,
                entry_price_sol, position_size, conviction,
                buy_ratio_at_entry, volume_expansion_at_entry, all_conditions)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry_signal.mint,
                launch_detector.token_name,
                launch_detector.token_symbol,
                launch_detector.genesis_time,
                entry_signal.timestamp,
                entry_signal.current_mc,
                launch_detector.spike_high_mc,
                launch_detector.lowest_mc_since_spike,
                launch_detector.dip_depth,
                launch_detector.fees_paid_sol,
                entry_signal.entry_price_sol,
                entry_signal.conviction_to_size(),
                entry_signal.conviction,
                entry_signal.pressure.buy_ratio_1s if entry_signal.pressure else 0,
                entry_signal.pressure.volume_expansion if entry_signal.pressure else 0,
                json.dumps(entry_signal.conditions_met),
            ),
        )
        trade_id = cur.lastrowid
        conn.commit()
        conn.close()
        logger.info(
            f"[SniperFT] Paper ENTRY #{trade_id} {entry_signal.mint[:8]} "
            f"@ {entry_signal.entry_price_sol:.8f} SOL, "
            f"conviction={entry_signal.conviction}"
        )
        return trade_id

    async def record_exit(
        self,
        trade_id: int,
        exit_signal: ExitSignal,
        entry_price_sol: float,
        position_size_sol: float,
    ) -> None:
        """Complete the trade record with exit data and P&L."""
        exit_price = exit_signal.exit_price_sol

        # Gross P&L
        if entry_price_sol > 0:
            price_change_ratio = exit_price / entry_price_sol
        else:
            price_change_ratio = 1.0

        gross_pnl = position_size_sol * (price_change_ratio - 1.0)

        # Costs simulation
        buy_fee = position_size_sol * 0.01                      # 1% pump.fun buy fee
        sell_fee = position_size_sol * price_change_ratio * 0.01  # 1% sell fee
        slippage_in = position_size_sol * 0.0025                  # 0.25% buy slippage
        slippage_out = position_size_sol * price_change_ratio * 0.0025  # 0.25% sell slippage
        tx_cost = 0.001  # fixed SOL

        total_costs = buy_fee + sell_fee + slippage_in + slippage_out + tx_cost
        net_pnl = gross_pnl - total_costs

        pnl_pct = ((exit_price - entry_price_sol) / entry_price_sol * 100) if entry_price_sol > 0 else 0

        conn = _get_conn()
        conn.execute(
            """UPDATE forward_test_trades
               SET exit_time = ?, exit_mc = ?, exit_price_sol = ?,
                   gross_pnl_sol = ?, net_pnl_sol = ?, pnl_pct = ?,
                   exit_trigger = ?
               WHERE id = ?""",
            (
                exit_signal.timestamp,
                exit_signal.current_mc,
                exit_price,
                gross_pnl,
                net_pnl,
                pnl_pct,
                exit_signal.trigger_name,
                trade_id,
            ),
        )
        conn.commit()
        conn.close()
        logger.info(
            f"[SniperFT] Paper EXIT #{trade_id} trigger={exit_signal.trigger_name} "
            f"net_pnl={net_pnl:+.4f} SOL ({pnl_pct:+.1f}%)"
        )

    def get_stats(self) -> dict:
        """Return aggregate forward test statistics."""
        conn = _get_conn()

        # Only count completed trades (have exit_time)
        rows = conn.execute(
            "SELECT * FROM forward_test_trades WHERE exit_time IS NOT NULL ORDER BY created_at DESC"
        ).fetchall()

        if not rows:
            conn.close()
            return {
                "total_trades": 0,
                "win_rate": 0.0,
                "avg_net_pnl_sol": 0.0,
                "total_net_pnl_sol": 0.0,
                "avg_hold_time_seconds": 0.0,
                "exit_trigger_breakdown": {},
                "conviction_breakdown": {},
                "avg_dip_depth_winners": 0.0,
                "avg_dip_depth_losers": 0.0,
                "best_trade": None,
                "worst_trade": None,
            }

        trades = [dict(r) for r in rows]
        total = len(trades)
        winners = [t for t in trades if (t.get("net_pnl_sol") or 0) > 0]
        losers = [t for t in trades if (t.get("net_pnl_sol") or 0) <= 0]

        win_rate = len(winners) / total * 100 if total > 0 else 0
        total_pnl = sum(t.get("net_pnl_sol", 0) or 0 for t in trades)
        avg_pnl = total_pnl / total if total > 0 else 0

        # Hold times
        hold_times = []
        for t in trades:
            if t.get("entry_time") and t.get("exit_time"):
                hold_times.append(t["exit_time"] - t["entry_time"])
        avg_hold = sum(hold_times) / len(hold_times) if hold_times else 0

        # Exit trigger breakdown
        triggers = {}
        for t in trades:
            trig = t.get("exit_trigger", "unknown")
            triggers[trig] = triggers.get(trig, 0) + 1

        # Conviction breakdown (win rate per level)
        convictions = {}
        for level in ["high", "medium", "low"]:
            level_trades = [t for t in trades if t.get("conviction") == level]
            if level_trades:
                level_wins = sum(1 for t in level_trades if (t.get("net_pnl_sol") or 0) > 0)
                convictions[level] = {
                    "count": len(level_trades),
                    "win_rate": level_wins / len(level_trades) * 100,
                }

        # Dip depth averages
        winner_depths = [t.get("dip_depth", 0) or 0 for t in winners]
        loser_depths = [t.get("dip_depth", 0) or 0 for t in losers]

        # Best/worst trades
        sorted_by_pnl = sorted(trades, key=lambda t: t.get("net_pnl_sol", 0) or 0)
        best = sorted_by_pnl[-1] if sorted_by_pnl else None
        worst = sorted_by_pnl[0] if sorted_by_pnl else None

        conn.close()

        return {
            "total_trades": total,
            "win_rate": round(win_rate, 1),
            "avg_net_pnl_sol": round(avg_pnl, 6),
            "total_net_pnl_sol": round(total_pnl, 6),
            "avg_hold_time_seconds": round(avg_hold, 1),
            "exit_trigger_breakdown": triggers,
            "conviction_breakdown": convictions,
            "avg_dip_depth_winners": round(sum(winner_depths) / max(len(winner_depths), 1), 3),
            "avg_dip_depth_losers": round(sum(loser_depths) / max(len(loser_depths), 1), 3),
            "best_trade": best,
            "worst_trade": worst,
        }

    def get_recent_trades(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """Return paginated forward test trade records."""
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM forward_test_trades ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_total_trade_count(self) -> int:
        """Return total number of completed forward test trades."""
        conn = _get_conn()
        count = conn.execute(
            "SELECT COUNT(*) FROM forward_test_trades WHERE exit_time IS NOT NULL"
        ).fetchone()[0]
        conn.close()
        return count


# Initialize on import
init_sniper_db()
