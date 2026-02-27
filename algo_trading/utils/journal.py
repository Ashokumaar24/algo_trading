# ============================================================
#  utils/journal.py
#  Trade Journal — logs every trade entry, exit, and daily summary
#
#  Writes to:
#    logs/trades_YYYY-MM-DD.csv   — one row per trade entry
#    logs/blocked_YYYY-MM-DD.csv  — blocked signal log
#    logs/daily_summary.csv       — one row per trading day
#
#  Used by main.py:
#    journal.log_entry(...)
#    journal.log_trade_blocked(...)
#    journal.log_daily_summary(...)
# ============================================================

import os
import csv
import json
from datetime import datetime
from typing import Optional

from config.config import BASE_DIR, LOG_DIR
from utils.logger import get_logger

logger = get_logger("journal")

# ------------------------------------------------------------------
# PATHS
# ------------------------------------------------------------------
JOURNAL_DIR = os.path.join(LOG_DIR, "journal")
os.makedirs(JOURNAL_DIR, exist_ok=True)


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _csv_path(name: str) -> str:
    return os.path.join(JOURNAL_DIR, f"{name}_{_today_str()}.csv")


def _append_csv(filepath: str, row: dict):
    """Append a dict as a CSV row, writing header if file is new."""
    file_exists = os.path.isfile(filepath)
    try:
        with open(filepath, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=row.keys())
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
    except Exception as e:
        logger.error(f"Journal write failed ({filepath}): {e}")


# ------------------------------------------------------------------
# TRADE JOURNAL
# ------------------------------------------------------------------
class TradeJournal:
    """
    Logs trade entries, blocked signals, and daily summaries to CSV files.
    All files are stored in logs/journal/ with today's date in the filename.
    """

    def __init__(self):
        logger.info(f"TradeJournal initialised — logs at: {JOURNAL_DIR}")

    # ------------------------------------------------------------------
    # LOG TRADE ENTRY
    # ------------------------------------------------------------------
    def log_entry(self, symbol: str, direction: str,
                  entry: float, sl: float, target: float,
                  qty: int, fill_price: float,
                  strategy: str, confidence: float,
                  notes: str = ""):
        """Called when an order is placed and confirmed filled."""
        risk        = abs(entry - sl) * qty
        reward      = abs(target - entry) * qty
        rr          = round(reward / risk, 2) if risk > 0 else 0

        row = {
            'timestamp':   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'symbol':      symbol,
            'direction':   direction,
            'strategy':    strategy,
            'entry_price': entry,
            'fill_price':  fill_price,
            'stop_loss':   sl,
            'target':      target,
            'quantity':    qty,
            'risk_inr':    round(risk, 0),
            'reward_inr':  round(reward, 0),
            'rr_ratio':    rr,
            'confidence':  confidence,
            'notes':       notes,
        }

        _append_csv(_csv_path("trades"), row)
        logger.info(
            f"JOURNAL ENTRY | {symbol} {direction} | "
            f"Fill:{fill_price} SL:{sl} Tgt:{target} | "
            f"RR:{rr} Conf:{confidence}"
        )

    # ------------------------------------------------------------------
    # LOG BLOCKED SIGNAL
    # ------------------------------------------------------------------
    def log_trade_blocked(self, symbol: str,
                          candle_time,
                          reason: str):
        """Called when a signal is generated but risk manager blocks it."""
        row = {
            'timestamp':   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'candle_time': str(candle_time),
            'symbol':      symbol,
            'reason':      reason,
        }
        _append_csv(_csv_path("blocked"), row)
        logger.debug(f"BLOCKED | {symbol} @ {candle_time} | {reason}")

    # ------------------------------------------------------------------
    # LOG DAILY SUMMARY
    # ------------------------------------------------------------------
    def log_daily_summary(self, state_snapshot: dict):
        """Called at end of day (15:30) with risk manager snapshot."""
        row = {
            'date':               _today_str(),
            'logged_at':          datetime.now().strftime("%H:%M:%S"),
            'trades_today':       state_snapshot.get('trades_today', 0),
            'open_positions':     state_snapshot.get('open_positions', 0),
            'daily_pnl':          state_snapshot.get('daily_pnl', 0),
            'weekly_pnl':         state_snapshot.get('weekly_pnl', 0),
            'consecutive_losses': state_snapshot.get('consecutive_losses', 0),
            'can_trade':          state_snapshot.get('can_trade', False),
            'block_reason':       state_snapshot.get('block_reason', ''),
        }
        summary_path = os.path.join(JOURNAL_DIR, "daily_summary.csv")
        _append_csv(summary_path, row)

        logger.info(
            f"DAILY SUMMARY | Trades:{row['trades_today']} | "
            f"PnL:₹{row['daily_pnl']:+,.0f} | "
            f"ConsecLoss:{row['consecutive_losses']}"
        )
