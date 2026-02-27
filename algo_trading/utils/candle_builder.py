# ============================================================
#  utils/candle_builder.py
#  Builds 5-minute OHLCV candles from real-time KiteTicker ticks
#
#  FIX 4 (Critical): Complete daily reset
#    - reset_daily() now clears ALL per-symbol state:
#      open_candles, candle_start, history, cumulative_volume
#    - Previously only cleared cumulative_volume, leaving
#      yesterday's partial candle and stale history in memory.
#      This caused today's indicators to include yesterday's candles.
#
#  FIX 11 (Refactor): History stored as deque(maxlen=500)
#    - Replaced manual list-trim (which created a new list object
#      every 500 candles, wasting memory and CPU)
#    - deque(maxlen=500) auto-trims from the left — O(1) vs O(n)
#    - Also thread-safe for appends
#
#  EXISTING FIX (kept): _get_candle_start formula
#    - Original used interval.seconds * 60 (wrong — mapped everything
#      to the 9:15 candle all morning)
#    - Fixed to use interval_minutes (int)
# ============================================================

import pandas as pd
from datetime import datetime, timedelta
from collections import defaultdict, deque
from typing import Callable, Dict, Optional
from utils.logger import get_logger

logger = get_logger("candle_builder")

# FIX 11: Maximum candles to keep in memory per symbol (≈2 trading days)
HISTORY_MAXLEN = 500


class Candle:
    """Represents a single OHLCV candle"""
    __slots__ = ['symbol', 'timestamp', 'open', 'high', 'low', 'close',
                 'volume', 'oi', 'is_complete']

    def __init__(self, symbol, timestamp, open_, high, low, close, volume, oi=0):
        self.symbol      = symbol
        self.timestamp   = timestamp
        self.open        = open_
        self.high        = high
        self.low         = low
        self.close       = close
        self.volume      = volume
        self.oi          = oi
        self.is_complete = False

    def to_dict(self):
        return {
            'symbol':    self.symbol,
            'timestamp': self.timestamp,
            'open':      self.open,
            'high':      self.high,
            'low':       self.low,
            'close':     self.close,
            'volume':    self.volume,
        }

    def __repr__(self):
        return (f"Candle({self.symbol} {self.timestamp} "
                f"O:{self.open} H:{self.high} L:{self.low} C:{self.close} "
                f"V:{self.volume})")


class CandleBuilder:
    """
    Builds N-minute candles from tick stream.
    Fires on_candle_close(symbol, candle, candle_history) callback
    when a candle period completes.

    Usage:
        builder = CandleBuilder(interval_minutes=5)
        builder.set_callback(my_strategy.on_candle_close)
        # In ticker on_ticks:
        for tick in ticks:
            builder.process_tick(tick)
    """

    def __init__(self, interval_minutes: int = 5):
        self.interval_minutes = interval_minutes
        self.interval         = timedelta(minutes=interval_minutes)

        # Per-symbol state — ALL of these are cleared in reset_daily()
        self.open_candles: Dict[str, Candle]     = {}
        self.candle_start: Dict[str, datetime]   = {}
        # FIX 11: Use deque(maxlen=500) instead of plain list
        self.history: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=HISTORY_MAXLEN)
        )
        self.cumulative_volume: Dict[str, float] = defaultdict(float)

        self._callback: Optional[Callable] = None

    def set_callback(self, callback: Callable):
        """Register the function to call when a candle closes"""
        self._callback = callback

    def _get_candle_start(self, tick_time: datetime) -> datetime:
        """
        Snap tick time back to the start of its candle period.

        FIX (existing): Original formula used interval.seconds * 60 which
        equals (minutes_since_open // 300) * 60 — returning 0 for all
        ticks before 2:15 PM, mapping everything to the 9:15 candle.
        Correct formula: minutes_since_open // interval_minutes
        """
        minutes_since_open = (tick_time.hour * 60 + tick_time.minute) - (9 * 60 + 15)
        period_index  = minutes_since_open // self.interval_minutes
        snap_minutes  = 9 * 60 + 15 + period_index * self.interval_minutes
        return tick_time.replace(
            hour=snap_minutes // 60,
            minute=snap_minutes % 60,
            second=0,
            microsecond=0
        )

    def process_tick(self, tick: dict):
        """
        Process a single tick from KiteTicker on_ticks.

        tick dict expected keys:
            instrument_token, tradingsymbol, last_price, volume,
            oi, timestamp (datetime)
        """
        symbol    = tick.get('tradingsymbol', str(tick.get('instrument_token')))
        price     = tick['last_price']
        volume    = tick.get('volume', 0)
        oi        = tick.get('oi', 0)
        tick_time = tick.get('timestamp', datetime.now())

        if isinstance(tick_time, str):
            tick_time = datetime.fromisoformat(tick_time)

        candle_start = self._get_candle_start(tick_time)

        # --- New candle period: close previous, open new ---
        if symbol in self.candle_start and self.candle_start[symbol] < candle_start:
            self._close_candle(symbol)

        # --- Start new candle if needed ---
        if symbol not in self.open_candles or symbol not in self.candle_start:
            prev_volume = self.cumulative_volume.get(symbol, 0)
            tick_volume = volume - prev_volume if volume > prev_volume else 0

            self.open_candles[symbol]      = Candle(
                symbol, candle_start, price, price, price, price, tick_volume, oi
            )
            self.candle_start[symbol]      = candle_start
            self.cumulative_volume[symbol] = volume

        else:
            # --- Update existing candle ---
            candle   = self.open_candles[symbol]
            prev_vol = self.cumulative_volume[symbol]
            tick_vol = volume - prev_vol if volume > prev_vol else 0

            candle.high    = max(candle.high,  price)
            candle.low     = min(candle.low,   price)
            candle.close   = price
            candle.volume += tick_vol
            candle.oi      = oi

            self.cumulative_volume[symbol] = volume

    def _close_candle(self, symbol: str):
        """Finalise a candle and fire the callback"""
        candle = self.open_candles.pop(symbol, None)
        if candle is None:
            return

        candle.is_complete = True
        # FIX 11: deque auto-trims to maxlen — no manual slice needed
        self.history[symbol].append(candle)

        logger.debug(f"Candle closed: {candle}")

        if self._callback:
            try:
                history_df = self.get_history_df(symbol)
                self._callback(symbol, candle, history_df)
            except Exception as e:
                logger.error(f"Candle callback error for {symbol}: {e}")

    def get_history_df(self, symbol: str) -> pd.DataFrame:
        """Return candle history as a pandas DataFrame"""
        if not self.history[symbol]:
            return pd.DataFrame(
                columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
            )
        # FIX 11: convert deque to list before iterating
        records = [c.to_dict() for c in list(self.history[symbol])]
        df = pd.DataFrame(records)
        df.set_index('timestamp', inplace=True)
        return df

    def force_close_all(self):
        """Force-close all open candles (call at market close)"""
        for symbol in list(self.open_candles.keys()):
            self._close_candle(symbol)
        logger.info("All open candles force-closed.")

    def reset_daily(self):
        """
        FIX 4: Full reset at start of each trading day.

        Clears ALL per-symbol state so yesterday's data cannot bleed
        into today's candles or rolling indicator calculations.

        Previously only cleared cumulative_volume, leaving:
          - open_candles: yesterday's partial last candle
          - candle_start: wrong timestamp for today's first candle
          - history:      yesterday's candles polluting indicator lookback
        """
        self.open_candles.clear()
        self.candle_start.clear()
        self.history.clear()            # FIX 4: was missing
        self.cumulative_volume.clear()
        logger.info("CandleBuilder: full daily reset — all state cleared ✓")
