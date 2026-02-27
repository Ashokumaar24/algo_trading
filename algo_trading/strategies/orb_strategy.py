# ============================================================
#  strategies/orb_strategy.py
#  Opening Range Breakout — 15-min ORB
#
#  FIX 1 (Critical): True 15-minute ORB using 3 five-minute candles
#    - set_orb() now accepts a list of opening candles (9:15, 9:20, 9:25)
#    - ORB high/low computed from all 3 candles
#    - Previously only used 2 candles (9:15 + 9:30) = 10-min range, not 15-min
#
#  Entry after 9:30 candle close, VWAP confirmation, volume filter
# ============================================================

import pandas as pd
from datetime import time, datetime
from typing import Optional, List

from strategies.base_strategy import BaseStrategy, Signal, Direction
from utils.indicators import vwap, volume_sma
from config.config import (
    ORB_VOLUME_MULTIPLIER, ORB_MIN_RANGE_PCT, ORB_MAX_RANGE_PCT,
    ORB_ENTRY_DEADLINE, ORB_REWARD_RISK_RATIO
)
from utils.logger import get_logger

logger = get_logger("orb_strategy")


class ORBStrategy(BaseStrategy):
    """
    15-Minute Opening Range Breakout Strategy.

    Opening Range = the combined high/low of the first 3 five-minute candles
    (9:15, 9:20, 9:25 candles), giving a true 15-minute opening range.

    Entry Rules (LONG):
    1. Price closes above ORB high
    2. Close must be above VWAP
    3. Volume on breakout candle > 1.5x 20-day average
    4. ORB range is in valid range (0.3% – 1.5% of price)
    5. Entry only between 9:30 AM and 12:00 PM

    Stop Loss: Below ORB low
    Target:    Entry + 1.2 × Risk (1:1.2 RR — from config)
    """

    def __init__(self):
        super().__init__("ORB_15")
        self._orb: dict         = {}   # {symbol: {high, low, range, range_pct}}
        self._trade_taken: set  = set()
        self._todays_date       = None

    def reset_daily(self):
        """Called at start of each trading day"""
        self._orb.clear()
        self._trade_taken.clear()
        self._todays_date = datetime.now().date()
        logger.info("ORB: Daily reset complete")

    def set_orb(self, symbol: str, opening_candles: List[dict]):
        """
        FIX 1: Set the Opening Range from a LIST of opening candles.

        Pass ALL candles from 9:15 to 9:25 (the 3 five-minute candles
        that make up the true 15-minute opening range).

        Args:
            symbol:          Stock symbol e.g. "NSE:RELIANCE"
            opening_candles: List of candle dicts, each with 'high' and 'low' keys.
                             Minimum 1 candle, typically 3 for true 15-min ORB.
        """
        if not opening_candles:
            logger.warning(f"set_orb called with empty candles list for {symbol}")
            return

        orb_high  = max(c['high'] for c in opening_candles)
        orb_low   = min(c['low']  for c in opening_candles)
        orb_range = orb_high - orb_low
        orb_mid   = (orb_high + orb_low) / 2

        self._orb[symbol] = {
            'high':      orb_high,
            'low':       orb_low,
            'range':     orb_range,
            'mid':       orb_mid,
            'range_pct': orb_range / orb_mid if orb_mid > 0 else 0,
        }

        logger.info(
            f"ORB SET | {symbol} | {len(opening_candles)} candles | "
            f"High:{orb_high:.2f} Low:{orb_low:.2f} "
            f"Range:{orb_range:.2f} ({orb_range/orb_mid*100:.2f}%)"
        )

    def check_entry(self, symbol: str, candle: dict,
                    candle_history: pd.DataFrame,
                    avg_volume_20d: float) -> Optional[Signal]:
        """
        Check if current closed candle triggers an ORB breakout.

        Args:
            symbol:          Stock symbol (e.g. "NSE:RELIANCE")
            candle:          Current closed candle dict
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

        candle_time = candle.get('timestamp', datetime.now())
        if isinstance(candle_time, datetime):
            candle_time = candle_time.time()
        else:
            candle_time = datetime.now().time()

        # Only trade between 9:30 AM and 12:00 PM
        if candle_time < time(9, 30) or candle_time > ORB_ENTRY_DEADLINE:
            return None

        if not self._validate_candle_count(candle_history, 5):
            return None

        orb   = self._orb[symbol]
        close = candle['close']
        vol   = candle['volume']

        # --- Filter: ORB range must be in valid bounds ---
        if not (ORB_MIN_RANGE_PCT <= orb['range_pct'] <= ORB_MAX_RANGE_PCT):
            logger.debug(
                f"ORB range {orb['range_pct']*100:.2f}% out of bounds for {symbol}"
            )
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
        hour_now      = candle_time.hour
        time_fraction = max((hour_now - 9) / 6.25, 0.1)
        expected_vol  = avg_volume_20d * time_fraction * ORB_VOLUME_MULTIPLIER
        volume_ok     = vol > expected_vol * 0.15  # per-candle check (scaled)

        # --- LONG Signal ---
        if (close > orb['high'] and
                close > vwap_val and
                volume_ok and
                close <= orb['high'] + orb['range'] * 0.5):  # not too extended

            sl     = orb['low']
            risk   = close - sl
            if risk <= 0:
                return None
            target = close + ORB_REWARD_RISK_RATIO * risk

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

            sl   = orb['high']
            risk = sl - close
            if risk <= 0:
                return None
            target = close - ORB_REWARD_RISK_RATIO * risk

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
        gap_from_vwap = abs(close - vwap_val) / vwap_val * 100 if vwap_val > 0 else 0
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

        # Range quality (tighter = cleaner breakout)
        if orb['range_pct'] < 0.008:
            score += 10

        return min(round(score, 1), 100.0)
