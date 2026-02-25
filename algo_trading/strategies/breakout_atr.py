# ============================================================
#  strategies/breakout_atr.py
#  Intraday Breakout — Previous Day High/Low + ATR + Volume Filter
# ============================================================

import pandas as pd
from datetime import datetime
from typing import Optional

from strategies.base_strategy import BaseStrategy, Signal, Direction
from utils.indicators import atr, volume_sma
from config.config import (
    BREAKOUT_ATR_MIN_PCT, BREAKOUT_ATR_MAX_PCT,
    BREAKOUT_VOL_MULTIPLIER, BREAKOUT_SL_ATR_MULT
)
from utils.logger import get_logger

logger = get_logger("breakout_atr")


class BreakoutATRStrategy(BaseStrategy):
    """
    Previous Day High/Low Breakout with ATR and Volume Confirmation.

    LONG: Price closes above previous day high
         + ATR in valid range (0.8% – 3.0% of price)
         + Cumulative intraday volume > expected

    SL: Previous day high – 0.5×ATR (buffer below breakout)
    Target: 1:1.5 RR
    """

    def __init__(self):
        super().__init__("BREAKOUT_ATR")
        self._prev_day: dict    = {}   # {symbol: {high, low, close}}
        self._trade_taken: set  = set()

    def reset_daily(self):
        self._trade_taken.clear()
        logger.info("Breakout ATR: Daily reset complete")

    def set_prev_day_data(self, symbol: str, prev_high: float,
                          prev_low: float, prev_close: float):
        """Set previous day's OHLC. Call at start of each day."""
        self._prev_day[symbol] = {
            'high': prev_high, 'low': prev_low, 'close': prev_close
        }

    def check_entry(self, symbol: str, candle: dict,
                    candle_history: pd.DataFrame,
                    cum_volume_today: float,
                    avg_daily_volume: float) -> Optional[Signal]:
        """
        Check for breakout above/below previous day range.

        Args:
            symbol:             Stock symbol
            candle:             Current completed candle
            candle_history:     Historical candles DataFrame
            cum_volume_today:   Cumulative volume since 9:15 AM today
            avg_daily_volume:   20-day average DAILY volume
        """
        if symbol in self._trade_taken:
            return None

        if symbol not in self._prev_day:
            logger.debug(f"No prev-day data for {symbol}")
            return None

        if not self._validate_candle_count(candle_history, 15):
            return None

        prev = self._prev_day[symbol]
        close = candle['close']

        atr_val = atr(
            candle_history['high'], candle_history['low'],
            candle_history['close'], period=14
        ).iloc[-1]

        # ATR filter
        atr_pct = atr_val / close
        if not (BREAKOUT_ATR_MIN_PCT <= atr_pct <= BREAKOUT_ATR_MAX_PCT):
            return None

        # Volume filter: proportional to time elapsed
        candle_time = candle.get('timestamp', datetime.now())
        if isinstance(candle_time, datetime):
            minutes_elapsed = (candle_time.hour * 60 + candle_time.minute) - (9 * 60 + 15)
            time_frac = max(minutes_elapsed / 375, 0.05)  # 375 min = full day
        else:
            time_frac = 0.3

        expected_vol = avg_daily_volume * time_frac * BREAKOUT_VOL_MULTIPLIER
        volume_ok    = cum_volume_today > expected_vol

        # ---- LONG: Break above prev day high ----
        if close > prev['high'] * 1.001 and volume_ok:
            sl     = prev['high'] - BREAKOUT_SL_ATR_MULT * atr_val
            risk   = close - sl
            target = close + 1.5 * risk

            signal = Signal(
                symbol=symbol, direction=Direction.LONG,
                strategy=self.name,
                entry=round(close, 2),
                stop_loss=round(sl, 2),
                target=round(target, 2),
                confidence=self._confidence(atr_pct, cum_volume_today,
                                             expected_vol),
                notes=(f"Breakout Long | PrevHigh:{prev['high']:.2f} "
                       f"ATR:{atr_val:.2f} Vol:{cum_volume_today:.0f}")
            )
            if signal.is_valid():
                logger.info(f"SIGNAL: {signal}")
                self._trade_taken.add(symbol)
                return signal

        # ---- SHORT: Break below prev day low ----
        if close < prev['low'] * 0.999 and volume_ok:
            sl     = prev['low'] + BREAKOUT_SL_ATR_MULT * atr_val
            risk   = sl - close
            target = close - 1.5 * risk

            signal = Signal(
                symbol=symbol, direction=Direction.SHORT,
                strategy=self.name,
                entry=round(close, 2),
                stop_loss=round(sl, 2),
                target=round(target, 2),
                confidence=self._confidence(atr_pct, cum_volume_today,
                                             expected_vol),
                notes=(f"Breakout Short | PrevLow:{prev['low']:.2f} "
                       f"ATR:{atr_val:.2f} Vol:{cum_volume_today:.0f}")
            )
            if signal.is_valid():
                logger.info(f"SIGNAL: {signal}")
                self._trade_taken.add(symbol)
                return signal

        return None

    def _confidence(self, atr_pct, cum_vol, expected_vol) -> float:
        score = 50.0
        if 0.01 <= atr_pct <= 0.018:  # sweet spot
            score += 20
        elif BREAKOUT_ATR_MIN_PCT <= atr_pct <= BREAKOUT_ATR_MAX_PCT:
            score += 10

        vol_ratio = cum_vol / expected_vol if expected_vol > 0 else 1
        if vol_ratio > 2.0:
            score += 25
        elif vol_ratio > 1.5:
            score += 15

        return min(max(round(score, 1), 0), 100.0)
