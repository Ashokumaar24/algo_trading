# ============================================================
#  risk/risk_manager.py
#  Position sizing, daily loss limits, dynamic SL management
# ============================================================

import pandas as pd
from datetime import datetime, time
from typing import Optional
from dataclasses import dataclass

from strategies.base_strategy import Signal, Direction
from config.config import (
    CAPITAL, RISK_PER_TRADE_NORMAL, RISK_PER_TRADE_HIGH, RISK_PER_TRADE_APLUS,
    DAILY_LOSS_LIMIT_PCT, WEEKLY_LOSS_LIMIT_PCT,
    MAX_TRADES_PER_DAY, MIN_CONFIDENCE_SCORE,
    SL_MIN_PCT, SL_MAX_PCT, SL_ATR_MULTIPLIER,
    FORCE_CLOSE_TIME, AGGRESSIVE_EXIT_TIME, NO_NEW_ENTRIES
)
from utils.logger import get_logger

logger = get_logger("risk_manager")


@dataclass
class RiskState:
    """Tracks intraday and weekly risk state"""
    trades_today:       int   = 0
    daily_pnl:          float = 0.0
    weekly_pnl:         float = 0.0
    consecutive_losses: int   = 0
    open_positions:     int   = 0
    capital:            float = CAPITAL


class RiskManager:
    """
    Enforces all risk management rules:
    - Position sizing (risk-based)
    - Daily / weekly loss limits
    - Max trades per day
    - Dynamic stop loss calculation
    - Time-based exit rules
    """

    def __init__(self, capital: float = CAPITAL):
        self.capital = capital
        self.state   = RiskState(capital=capital)

    # ------------------------------------------------------------------
    # CAN WE TRADE?
    # ------------------------------------------------------------------
    def can_trade(self, current_time: Optional[time] = None) -> bool:
        """
        Returns True if all risk gates are clear for a new entry.
        """
        now = current_time or datetime.now().time()

        # Time gates
        if now >= NO_NEW_ENTRIES:
            logger.debug("No new entries — time gate (2:00 PM)")
            return False

        # Max trades
        if self.state.trades_today >= MAX_TRADES_PER_DAY:
            logger.debug(f"Max trades reached: {self.state.trades_today}")
            return False

        # Daily loss limit
        daily_loss_limit = -self.capital * DAILY_LOSS_LIMIT_PCT
        if self.state.daily_pnl <= daily_loss_limit:
            logger.warning(
                f"Daily loss limit hit: ₹{self.state.daily_pnl:,.0f} "
                f"(limit: ₹{daily_loss_limit:,.0f}) — TRADING STOPPED"
            )
            return False

        # After 3 consecutive losses: 30-minute pause (handled externally)
        if self.state.consecutive_losses >= 5:
            logger.warning("5 consecutive losses — stopping for the day.")
            return False

        return True

    def passes_confidence_gate(self, signal: Signal) -> bool:
        """Check if signal meets minimum confidence threshold"""
        if signal.confidence < MIN_CONFIDENCE_SCORE:
            logger.debug(
                f"Signal rejected: confidence {signal.confidence:.0f} "
                f"< minimum {MIN_CONFIDENCE_SCORE}"
            )
            return False
        return True

    # ------------------------------------------------------------------
    # POSITION SIZING
    # ------------------------------------------------------------------
    def calculate_position_size(self, signal: Signal,
                                  size_multiplier: float = 1.0) -> int:
        """
        Risk-based position sizing.
        Shares = Risk Amount / Risk Per Share

        Args:
            signal:          Trade signal with entry and SL
            size_multiplier: Regime-based multiplier (e.g., 0.5 for high VIX)

        Returns:
            Number of shares to trade (integer)
        """
        # Select risk tier based on confidence
        if signal.confidence >= 85:
            risk_pct = RISK_PER_TRADE_APLUS
        elif signal.confidence >= 75:
            risk_pct = RISK_PER_TRADE_HIGH
        else:
            risk_pct = RISK_PER_TRADE_NORMAL

        # Apply regime multiplier
        risk_pct *= size_multiplier

        # Weekly loss adjustment
        if self.state.weekly_pnl < -self.capital * WEEKLY_LOSS_LIMIT_PCT:
            risk_pct *= 0.5
            logger.info("Weekly loss limit exceeded — halving position size")

        risk_amount   = self.capital * risk_pct
        risk_per_share = abs(signal.entry - signal.stop_loss)

        if risk_per_share <= 0:
            logger.warning(f"Invalid risk per share for {signal.symbol}: {risk_per_share}")
            return 0

        shares = int(risk_amount / risk_per_share)

        # Sanity cap: never deploy more than 30% of capital in one trade
        max_shares = int(self.capital * 0.30 / signal.entry)
        shares     = min(shares, max_shares)

        logger.info(
            f"Position Size | {signal.symbol} | Risk:{risk_pct*100:.2f}% "
            f"= ₹{risk_amount:,.0f} | Risk/share:{risk_per_share:.2f} "
            f"| Shares:{shares} | Value:₹{shares * signal.entry:,.0f}"
        )

        return shares

    # ------------------------------------------------------------------
    # DYNAMIC STOP LOSS
    # ------------------------------------------------------------------
    def calculate_stop_loss(self, signal: Signal, atr_value: float) -> float:
        """
        Multi-layer stop loss: technical SL bounded by ATR and % limits.

        Returns the final validated stop loss price.
        """
        entry = signal.entry
        raw_sl = signal.stop_loss

        # Layer 1: Technical SL (from strategy)
        technical_sl = raw_sl

        # Layer 2: ATR-based maximum SL
        if signal.direction == Direction.LONG:
            atr_sl = entry - SL_ATR_MULTIPLIER * atr_value
            min_sl = entry * (1 - SL_MAX_PCT)    # never wider than 0.8%
            floor_sl = entry * (1 - SL_MIN_PCT)  # never tighter than 0.3%

            # Use tightest valid SL (but not tighter than noise floor)
            final_sl = max(technical_sl, atr_sl, min_sl)
            final_sl = min(final_sl, floor_sl)   # don't go too tight

        else:  # SHORT
            atr_sl   = entry + SL_ATR_MULTIPLIER * atr_value
            max_sl   = entry * (1 + SL_MAX_PCT)
            ceil_sl  = entry * (1 + SL_MIN_PCT)

            final_sl = min(technical_sl, atr_sl, max_sl)
            final_sl = max(final_sl, ceil_sl)

        logger.debug(
            f"SL Calculation | {signal.symbol} | Technical:{technical_sl:.2f} "
            f"ATR-SL:{atr_sl:.2f} | Final:{final_sl:.2f}"
        )

        return round(final_sl, 2)

    def should_trail_stop(self, signal: Signal, current_price: float,
                           current_sl: float) -> Optional[float]:
        """
        Returns new trailing SL price if trailing trigger is hit, else None.
        Moves SL to breakeven after 1R profit.
        """
        if signal.trail_trigger is None:
            return None

        if signal.direction == Direction.LONG:
            profit = current_price - signal.entry
            if profit >= signal.trail_trigger:
                new_sl = max(current_sl, signal.entry)  # minimum: breakeven
                if new_sl > current_sl:
                    logger.info(
                        f"Trailing SL | {signal.symbol} | "
                        f"Old SL:{current_sl:.2f} → New SL:{new_sl:.2f}"
                    )
                    return new_sl
        else:
            profit = signal.entry - current_price
            if profit >= signal.trail_trigger:
                new_sl = min(current_sl, signal.entry)
                if new_sl < current_sl:
                    logger.info(
                        f"Trailing SL | {signal.symbol} | "
                        f"Old SL:{current_sl:.2f} → New SL:{new_sl:.2f}"
                    )
                    return new_sl

        return None

    # ------------------------------------------------------------------
    # TIME-BASED EXIT RULES
    # ------------------------------------------------------------------
    def check_time_exit(self, has_open_position: bool,
                         is_loss: bool = False) -> Optional[str]:
        """
        Returns exit action string if time-based rule triggers.

        Returns:
            'FORCE_CLOSE'  — hard close (3:15 PM)
            'CLOSE_LOSERS' — close losing positions (2:45 PM)
            None           — no action needed
        """
        now = datetime.now().time()

        if now >= FORCE_CLOSE_TIME and has_open_position:
            return 'FORCE_CLOSE'

        if now >= AGGRESSIVE_EXIT_TIME and has_open_position and is_loss:
            return 'CLOSE_LOSERS'

        return None

    # ------------------------------------------------------------------
    # STATE UPDATES
    # ------------------------------------------------------------------
    def record_trade_entry(self):
        self.state.trades_today += 1
        self.state.open_positions += 1

    def record_trade_exit(self, pnl: float):
        self.state.daily_pnl  += pnl
        self.state.weekly_pnl += pnl
        self.state.open_positions = max(0, self.state.open_positions - 1)

        if pnl < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0

        logger.info(
            f"Trade Exit | PnL: ₹{pnl:+,.0f} | "
            f"Daily PnL: ₹{self.state.daily_pnl:+,.0f} | "
            f"Consecutive losses: {self.state.consecutive_losses}"
        )

    def reset_daily(self):
        """Call at start of each trading day"""
        self.state.trades_today       = 0
        self.state.daily_pnl          = 0.0
        self.state.consecutive_losses = 0
        self.state.open_positions     = 0
        logger.info("Risk Manager: Daily state reset")

    def reset_weekly(self):
        """Call at start of each trading week"""
        self.state.weekly_pnl = 0.0
        logger.info("Risk Manager: Weekly state reset")

    def get_status(self) -> dict:
        return {
            'trades_today':       self.state.trades_today,
            'daily_pnl':          round(self.state.daily_pnl, 2),
            'weekly_pnl':         round(self.state.weekly_pnl, 2),
            'consecutive_losses': self.state.consecutive_losses,
            'open_positions':     self.state.open_positions,
            'can_trade':          self.can_trade(),
        }
