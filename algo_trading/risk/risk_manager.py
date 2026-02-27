# ============================================================
#  risk/risk_manager.py
#  Real-time risk management — enforces daily limits
#
#  FIX 10 (Critical): can_trade() returns (bool, str) tuple
#  FIX 15 (Critical): Thread safety on RiskState mutations
#  FIX (startup): Added get_status() method.
#    - telegram_notifier.py calls risk_manager.get_status()
#    - Only get_state_snapshot() existed → AttributeError on every
#      /status Telegram command and on EOD summary.
#    - get_status() is now a public alias for get_state_snapshot()
#      so both call sites work without any further changes.
# ============================================================

import threading
from datetime import datetime, time as dt_time
from dataclasses import dataclass, field
from typing import Optional, Tuple

from config.config import (
    DAILY_LOSS_LIMIT_PCT, MAX_TRADES_PER_DAY,
    NO_NEW_ENTRIES, CAPITAL as INITIAL_CAPITAL
)
from utils.logger import get_logger

logger = get_logger("risk_manager")


# ----------------------------------------------------------------
# RISK STATE
# ----------------------------------------------------------------
@dataclass
class RiskState:
    trades_today:        int   = 0
    open_positions:      int   = 0
    daily_pnl:           float = 0.0
    weekly_pnl:          float = 0.0
    consecutive_losses:  int   = 0
    trade_date:          Optional[datetime] = field(default=None)

    def reset_daily(self):
        self.trades_today       = 0
        self.open_positions     = max(0, self.open_positions)
        self.daily_pnl          = 0.0
        self.consecutive_losses = 0
        self.trade_date         = datetime.now().date()
        logger.info("RiskState: daily reset complete")


# ----------------------------------------------------------------
# RISK MANAGER
# ----------------------------------------------------------------
class RiskManager:
    """
    Enforces hard trading rules:
      - Time gate (no new entries after NO_NEW_ENTRIES, default 14:00)
      - Daily trade cap (MAX_TRADES_PER_DAY, default 2)
      - Daily loss limit (DAILY_LOSS_LIMIT_PCT of capital)
      - Consecutive loss circuit breaker (5 losses = halt)

    FIX 10: can_trade() returns Tuple[bool, str] from a single time snapshot.
    FIX 15: All state mutations are wrapped in threading.Lock().
    FIX:    get_status() added as public alias for get_state_snapshot().
    """

    def __init__(self, capital: float = INITIAL_CAPITAL):
        self.capital = capital
        self.state   = RiskState()
        self._lock   = threading.Lock()

        logger.info(
            f"RiskManager init | Capital:₹{capital:,.0f} | "
            f"MaxTrades:{MAX_TRADES_PER_DAY} | "
            f"DailyLossLimit:{DAILY_LOSS_LIMIT_PCT*100:.1f}%"
        )

    # ------------------------------------------------------------------
    # FIX 10: can_trade() — single snapshot, returns (bool, reason_str)
    # ------------------------------------------------------------------
    def can_trade(self, current_time: Optional[dt_time] = None) -> Tuple[bool, str]:
        """
        Check all risk gates in a single call.
        Returns (True, "OK") or (False, "REASON: ...").
        """
        now = current_time or datetime.now().time()
        no_entry_cutoff = NO_NEW_ENTRIES

        if now >= no_entry_cutoff:
            return (
                False,
                f"TIME_GATE: no new entries after "
                f"{no_entry_cutoff.strftime('%H:%M')}"
            )

        if self.state.trades_today >= MAX_TRADES_PER_DAY:
            return (
                False,
                f"TRADE_CAP: {self.state.trades_today}/{MAX_TRADES_PER_DAY} "
                f"trades taken today"
            )

        daily_loss_limit = -abs(self.capital * DAILY_LOSS_LIMIT_PCT)
        if self.state.daily_pnl <= daily_loss_limit:
            return (
                False,
                f"RISK_GATE: daily loss ₹{abs(self.state.daily_pnl):,.0f} "
                f">= limit ₹{abs(daily_loss_limit):,.0f}"
            )

        if self.state.consecutive_losses >= 5:
            return (
                False,
                f"RISK_GATE: {self.state.consecutive_losses} consecutive losses — "
                f"circuit breaker active"
            )

        return True, "OK"

    # ------------------------------------------------------------------
    # BACKWARD-COMPATIBLE WRAPPER
    # ------------------------------------------------------------------
    def get_block_reason(self, current_time: Optional[dt_time] = None) -> dict:
        """Returns block details as a dict. Wraps can_trade()."""
        _, reason_str = self.can_trade(current_time)
        block_type    = reason_str.split(":")[0] if ":" in reason_str else "UNKNOWN"
        return {
            'type':   block_type,
            'short':  reason_str,
            'detail': reason_str,
        }

    # ------------------------------------------------------------------
    # FIX 15: THREAD-SAFE STATE MUTATIONS
    # ------------------------------------------------------------------
    def record_trade_entry(self):
        with self._lock:
            self.state.trades_today   += 1
            self.state.open_positions += 1

        logger.info(
            f"Trade entry recorded | "
            f"Today:{self.state.trades_today}/{MAX_TRADES_PER_DAY} | "
            f"OpenPos:{self.state.open_positions}"
        )

    def record_trade_exit(self, pnl: float):
        with self._lock:
            self.state.daily_pnl          += pnl
            self.state.weekly_pnl         += pnl
            self.state.open_positions      = max(0, self.state.open_positions - 1)

            if pnl < 0:
                self.state.consecutive_losses += 1
            else:
                self.state.consecutive_losses  = 0

        logger.info(
            f"Trade exit recorded | PnL:₹{pnl:+,.0f} | "
            f"DailyPnL:₹{self.state.daily_pnl:+,.0f} | "
            f"ConsecLoss:{self.state.consecutive_losses}"
        )

    def reset_daily(self):
        with self._lock:
            self.state.reset_daily()
        logger.info("RiskManager: daily state reset complete")

    # ------------------------------------------------------------------
    # READ-ONLY HELPERS
    # ------------------------------------------------------------------
    def get_state_snapshot(self) -> dict:
        """Return a copy of current risk state for logging/dashboard."""
        return {
            'trades_today':       self.state.trades_today,
            'open_positions':     self.state.open_positions,
            'daily_pnl':          round(self.state.daily_pnl, 2),
            'weekly_pnl':         round(self.state.weekly_pnl, 2),
            'consecutive_losses': self.state.consecutive_losses,
            'can_trade':          self.can_trade()[0],
            'block_reason':       self.can_trade()[1],
        }

    def get_status(self) -> dict:
        """
        FIX: Public alias for get_state_snapshot().

        telegram_notifier.py calls self._trading_system.risk_manager.get_status()
        but only get_state_snapshot() existed, causing AttributeError on every
        /status Telegram command and on EOD summary generation.

        Both methods return the same dict — callers can use either name.
        """
        return self.get_state_snapshot()

    def is_position_size_ok(self, entry: float, sl: float,
                             quantity: int) -> Tuple[bool, str]:
        """Check that a proposed trade doesn't exceed per-trade risk limits."""
        risk_per_share = abs(entry - sl)
        total_risk     = risk_per_share * quantity
        max_risk       = self.capital * 0.01  # 1%

        if total_risk > max_risk:
            return (
                False,
                f"Position too large: risk ₹{total_risk:,.0f} "
                f"> max ₹{max_risk:,.0f} (1% of ₹{self.capital:,.0f})"
            )
        return True, "OK"

    def calculate_position_size(self, entry: float, sl: float) -> int:
        """Calculate shares to trade based on 0.5% risk per trade."""
        risk_per_share = abs(entry - sl)
        if risk_per_share <= 0:
            return 0
        risk_amount = self.capital * 0.005
        qty         = int(risk_amount / risk_per_share)
        return max(qty, 1)
