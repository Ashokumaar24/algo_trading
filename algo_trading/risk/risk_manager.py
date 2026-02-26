# ============================================================
#  risk/risk_manager.py
#  Position sizing, daily loss limits, dynamic SL management
#
#  ADDED: get_block_reason() — returns structured reason dict
#         so DailyJournal can log exactly why trading was blocked
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
    trades_today:       int   = 0
    daily_pnl:          float = 0.0
    weekly_pnl:         float = 0.0
    consecutive_losses: int   = 0
    open_positions:     int   = 0
    capital:            float = CAPITAL


class RiskManager:

    def __init__(self, capital: float = CAPITAL):
        self.capital = capital
        self.state   = RiskState(capital=capital)

    def can_trade(self, current_time: Optional[time] = None) -> bool:
        now = current_time or datetime.now().time()
        if now >= NO_NEW_ENTRIES:
            return False
        if self.state.trades_today >= MAX_TRADES_PER_DAY:
            return False
        daily_loss_limit = -self.capital * DAILY_LOSS_LIMIT_PCT
        if self.state.daily_pnl <= daily_loss_limit:
            return False
        if self.state.consecutive_losses >= 5:
            return False
        return True

    # ADDED: structured reason for journal logging
    def get_block_reason(self, current_time: Optional[time] = None) -> dict:
        """
        Returns a dict explaining why can_trade() returned False.
        Used by DailyJournal to log the specific reason.
        """
        now = current_time or datetime.now().time()

        if now >= NO_NEW_ENTRIES:
            return {
                'type':   'TIME_GATE',
                'short':  'After 2:00 PM cutoff',
                'detail': (
                    f"Current time {now.strftime('%H:%M')} is past the 2:00 PM no-new-entries rule. "
                    f"Trades entered this late don't have enough time to play out "
                    f"before the 3:15 PM force close."
                )
            }

        if self.state.trades_today >= MAX_TRADES_PER_DAY:
            return {
                'type':   'TRADE_CAP',
                'short':  f'Daily trade cap hit ({MAX_TRADES_PER_DAY} trades)',
                'detail': (
                    f"Already placed {self.state.trades_today} trades today "
                    f"(maximum allowed: {MAX_TRADES_PER_DAY}). "
                    f"The 2-trade cap exists because backtests proved over-trading "
                    f"destroys edge through transaction costs. "
                    f"No new entries for the rest of the day."
                )
            }

        daily_loss_limit = -self.capital * DAILY_LOSS_LIMIT_PCT
        if self.state.daily_pnl <= daily_loss_limit:
            return {
                'type':   'RISK_GATE',
                'short':  f'Daily loss limit hit (₹{abs(daily_loss_limit):,.0f})',
                'detail': (
                    f"Daily P&L is ₹{self.state.daily_pnl:+,.0f}, which has breached "
                    f"the daily loss limit of {DAILY_LOSS_LIMIT_PCT*100:.1f}% of capital "
                    f"(₹{abs(daily_loss_limit):,.0f}). "
                    f"System stops trading for the rest of the day to protect capital."
                )
            }

        if self.state.consecutive_losses >= 5:
            return {
                'type':   'RISK_GATE',
                'short':  f'{self.state.consecutive_losses} consecutive losses',
                'detail': (
                    f"Hit {self.state.consecutive_losses} consecutive losing trades. "
                    f"This triggers an automatic trading halt for the rest of the day. "
                    f"Something may be wrong with market conditions or signal quality."
                )
            }

        return {
            'type':   'RISK_GATE',
            'short':  'Risk gate triggered',
            'detail': 'Risk management blocked the trade (unknown reason).'
        }

    def passes_confidence_gate(self, signal: Signal) -> bool:
        if signal.confidence < MIN_CONFIDENCE_SCORE:
            return False
        return True

    def calculate_position_size(self, signal: Signal,
                                  size_multiplier: float = 1.0) -> int:
        if signal.confidence >= 85:
            risk_pct = RISK_PER_TRADE_APLUS
        elif signal.confidence >= 75:
            risk_pct = RISK_PER_TRADE_HIGH
        else:
            risk_pct = RISK_PER_TRADE_NORMAL

        risk_pct *= size_multiplier

        if self.state.weekly_pnl < -self.capital * WEEKLY_LOSS_LIMIT_PCT:
            risk_pct *= 0.5
            logger.info("Weekly loss limit exceeded — halving position size")

        risk_amount    = self.capital * risk_pct
        risk_per_share = abs(signal.entry - signal.stop_loss)

        if risk_per_share <= 0:
            return 0

        shares     = int(risk_amount / risk_per_share)
        max_shares = int(self.capital * 0.30 / signal.entry)
        shares     = min(shares, max_shares)

        logger.info(
            f"Position Size | {signal.symbol} | Risk:{risk_pct*100:.2f}% "
            f"= ₹{risk_amount:,.0f} | Shares:{shares}"
        )
        return shares

    def calculate_stop_loss(self, signal: Signal, atr_value: float) -> float:
        entry  = signal.entry
        raw_sl = signal.stop_loss

        if signal.direction == Direction.LONG:
            atr_sl   = entry - SL_ATR_MULTIPLIER * atr_value
            min_sl   = entry * (1 - SL_MAX_PCT)
            floor_sl = entry * (1 - SL_MIN_PCT)
            final_sl = max(raw_sl, atr_sl, min_sl)
            final_sl = min(final_sl, floor_sl)
        else:
            atr_sl   = entry + SL_ATR_MULTIPLIER * atr_value
            max_sl   = entry * (1 + SL_MAX_PCT)
            ceil_sl  = entry * (1 + SL_MIN_PCT)
            final_sl = min(raw_sl, atr_sl, max_sl)
            final_sl = max(final_sl, ceil_sl)

        return round(final_sl, 2)

    def should_trail_stop(self, signal: Signal, current_price: float,
                           current_sl: float) -> Optional[float]:
        if signal.trail_trigger is None:
            return None

        if signal.direction == Direction.LONG:
            profit = current_price - signal.entry
            if profit >= signal.trail_trigger:
                new_sl = max(current_sl, signal.entry)
                if new_sl > current_sl:
                    logger.info(f"Trailing SL | {signal.symbol} | {current_sl:.2f} → {new_sl:.2f}")
                    return new_sl
        else:
            profit = signal.entry - current_price
            if profit >= signal.trail_trigger:
                new_sl = min(current_sl, signal.entry)
                if new_sl < current_sl:
                    logger.info(f"Trailing SL | {signal.symbol} | {current_sl:.2f} → {new_sl:.2f}")
                    return new_sl
        return None

    def check_time_exit(self, has_open_position: bool,
                         is_loss: bool = False) -> Optional[str]:
        now = datetime.now().time()
        if now >= FORCE_CLOSE_TIME and has_open_position:
            return 'FORCE_CLOSE'
        if now >= AGGRESSIVE_EXIT_TIME and has_open_position and is_loss:
            return 'CLOSE_LOSERS'
        return None

    def record_trade_entry(self):
        self.state.trades_today   += 1
        self.state.open_positions += 1

    def record_trade_exit(self, pnl: float):
        self.state.daily_pnl   += pnl
        self.state.weekly_pnl  += pnl
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
        self.state.trades_today       = 0
        self.state.daily_pnl          = 0.0
        self.state.consecutive_losses = 0
        self.state.open_positions     = 0
        logger.info("Risk Manager: Daily state reset")

    def reset_weekly(self):
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
