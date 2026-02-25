# ============================================================
#  strategies/orb_strategy.py
#  Opening Range Breakout — 15-min ORB
#  Entry after 9:30 candle close, VWAP confirmation, volume filter
# ============================================================

import pandas as pd
from datetime import time, datetime
from typing import Optional

from strategies.base_strategy import BaseStrategy, Signal, Direction
from utils.indicators import vwap, volume_sma, calculate_orb
from config.config import (
    ORB_VOLUME_MULTIPLIER, ORB_MIN_RANGE_PCT, ORB_MAX_RANGE_PCT,
    ORB_ENTRY_DEADLINE, ORB_REWARD_RISK_RATIO
)
from utils.logger import get_logger

logger = get_logger("orb_strategy")


class ORBStrategy(BaseStrategy):
    """
    15-Minute Opening Range Breakout Strategy.

    Entry Rules (LONG):
    1. Price closes above ORB high (9:15 + 9:30 combined range)
    2. Close must be above VWAP
    3. Volume on breakout candle > 1.5x 20-day average
    4. ORB range is in valid range (0.3% – 1.5% of price)
    5. Entry only between 9:30 AM and 12:00 PM

    Stop Loss: Below ORB low
    Target:    Entry + 1.5 × Risk (1:1.5 RR)
    """

    def __init__(self):
        super().__init__("ORB_15")
        self._orb: dict             = {}   # {symbol: {high, low, range, range_pct}}
        self._trade_taken: set      = set()
        self._todays_date           = None

    def reset_daily(self):
        """Called at start of each trading day"""
        self._orb.clear()
        self._trade_taken.clear()
        self._todays_date = datetime.now().date()
        logger.info("ORB: Daily reset complete")

    def set_orb(self, symbol: str, candle_915: dict, candle_930: dict):
        """
        Set the Opening Range after 9:30 candle closes.
        Call this once at 9:30 AM for each tracked symbol.
        """
        orb = calculate_orb(candle_915, candle_930)
        self._orb[symbol] = orb
        logger.info(
            f"ORB SET | {symbol} | High:{orb['high']:.2f} "
            f"Low:{orb['low']:.2f} Range:{orb['range_pct']*100:.2f}%"
        )

    def check_entry(self, symbol: str, candle: dict,
                    candle_history: pd.DataFrame,
                    avg_volume_20d: float) -> Optional[Signal]:
        """
        Check if current closed candle triggers an ORB breakout.

        Args:
            symbol:          Stock symbol (e.g. "NSE:RELIANCE")
            candle:          Current closed candle dict {open,high,low,close,volume,timestamp}
            candle_history:  DataFrame of past 5-min candles (indexed by timestamp)
            avg_volume_20d:  20-day average daily volume

        Returns:
            Signal or None
        """
        # --- Guard checks ---
        if symbol in self._trade_taken:
            return None

        if symbol not in self._orb:
            logger.debug(f"ORB not set for {symbol} yet")
            return None

        candle_time = candle.get('timestamp', datetime.now()).time() \
                      if isinstance(candle.get('timestamp'), datetime) \
                      else datetime.now().time()

        if candle_time > ORB_ENTRY_DEADLINE:
            return None  # Too late in the day

        if not self._validate_candle_count(candle_history, 5):
            return None

        orb  = self._orb[symbol]
        close = candle['close']
        high  = candle['high']
        low   = candle['low']
        vol   = candle['volume']

        # --- Filter: ORB range must be in valid bounds ---
        if not (ORB_MIN_RANGE_PCT <= orb['range_pct'] <= ORB_MAX_RANGE_PCT):
            logger.debug(f"ORB range {orb['range_pct']*100:.2f}% out of bounds for {symbol}")
            return None

        # --- Compute VWAP ---
        vwap_val = vwap(
            candle_history['high'],
            candle_history['low'],
            candle_history['close'],
            candle_history['volume']
        ).iloc[-1]

        # --- Volume confirmation ---
        # Scale avg daily volume to intraday proportion
        current_hour = candle_time.hour
        time_fraction = max((current_hour - 9) / 6.25, 0.1)  # fraction of 6.25hr day
        expected_vol  = avg_volume_20d * time_fraction * ORB_VOLUME_MULTIPLIER
        volume_ok     = vol > expected_vol * 0.15  # per-candle check (scaled)

        # --- LONG Signal ---
        if (close > orb['high'] and
                close > vwap_val and
                volume_ok and
                close <= orb['high'] + orb['range'] * 0.5):  # not too extended

            sl      = orb['low']
            risk    = close - sl
            target  = close + ORB_REWARD_RISK_RATIO * risk

            conf = self._confidence(close, vwap_val, vol, avg_volume_20d,
                                    orb, direction="LONG")

            signal = Signal(
                symbol=symbol, direction=Direction.LONG, strategy=self.name,
                entry=round(close, 2), stop_loss=round(sl, 2),
                target=round(target, 2), confidence=conf,
                notes=f"ORB Long | VWAP:{vwap_val:.2f} Vol:{vol:.0f}"
            )

            if signal.is_valid():
                logger.info(f"SIGNAL: {signal}")
                self._trade_taken.add(symbol)
                return signal

        # --- SHORT Signal ---
        if (close < orb['low'] and
                close < vwap_val and
                volume_ok and
                close >= orb['low'] - orb['range'] * 0.5):

            sl      = orb['high']
            risk    = sl - close
            target  = close - ORB_REWARD_RISK_RATIO * risk

            conf = self._confidence(close, vwap_val, vol, avg_volume_20d,
                                    orb, direction="SHORT")

            signal = Signal(
                symbol=symbol, direction=Direction.SHORT, strategy=self.name,
                entry=round(close, 2), stop_loss=round(sl, 2),
                target=round(target, 2), confidence=conf,
                notes=f"ORB Short | VWAP:{vwap_val:.2f} Vol:{vol:.0f}"
            )

            if signal.is_valid():
                logger.info(f"SIGNAL: {signal}")
                self._trade_taken.add(symbol)
                return signal

        return None

    def _confidence(self, close, vwap_val, vol, avg_vol_20d,
                    orb, direction) -> float:
        score = 50.0

        # VWAP proximity (closer to VWAP when crossing ORB = stronger)
        gap_from_vwap = abs(close - vwap_val) / vwap_val * 100
        if gap_from_vwap < 0.3:
            score += 20
        elif gap_from_vwap < 0.6:
            score += 10

        # Volume strength
        vol_ratio = vol / (avg_vol_20d * 0.15 + 1)
        if vol_ratio > 2.0:
            score += 20
        elif vol_ratio > 1.5:
            score += 10

        # Range quality (tighter = cleaner)
        if orb['range_pct'] < 0.008:
            score += 10

        return min(round(score, 1), 100.0)
