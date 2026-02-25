# ============================================================
#  strategies/ema_rsi_strategy.py
#  EMA 9/21 Crossover + RSI Filter
# ============================================================

import pandas as pd
from datetime import datetime
from typing import Optional

from strategies.base_strategy import BaseStrategy, Signal, Direction
from utils.indicators import ema, rsi, atr
from config.config import (
    EMA_FAST, EMA_SLOW, RSI_PERIOD,
    RSI_LONG_MIN, RSI_LONG_MAX, RSI_SHORT_MIN, RSI_SHORT_MAX,
    EMA_ATR_MULTIPLIER_SL, EMA_ATR_MULTIPLIER_TGT
)
from utils.logger import get_logger

logger = get_logger("ema_rsi")


class EMARSIStrategy(BaseStrategy):
    """
    EMA 9/21 Crossover with RSI Confirmation.

    LONG Entry:
    - EMA9 crosses above EMA21 (on candle close)
    - RSI between 55 and 75 (trending up, not overbought)
    - Price above both EMAs (trend alignment)

    SL: 1.5 × ATR below entry
    Target: 2.0 × ATR above entry
    """

    def __init__(self):
        super().__init__("EMA_RSI")
        self._trade_taken: set = set()

    def reset_daily(self):
        self._trade_taken.clear()
        logger.info("EMA-RSI: Daily reset complete")

    def check_entry(self, symbol: str, candle: dict,
                    candle_history: pd.DataFrame) -> Optional[Signal]:

        if symbol in self._trade_taken:
            return None

        if not self._validate_candle_count(candle_history, 30):
            return None

        close_series = candle_history['close']

        ema9  = ema(close_series, EMA_FAST)
        ema21 = ema(close_series, EMA_SLOW)
        rsi_s = rsi(close_series, RSI_PERIOD)
        atr_v = atr(
            candle_history['high'], candle_history['low'],
            close_series, period=14
        )

        ema9_curr   = ema9.iloc[-1]
        ema9_prev   = ema9.iloc[-2]
        ema21_curr  = ema21.iloc[-1]
        ema21_prev  = ema21.iloc[-2]
        rsi_curr    = rsi_s.iloc[-1]
        atr_curr    = atr_v.iloc[-1]
        close       = candle['close']

        # Bullish crossover
        bullish_cross = (ema9_prev <= ema21_prev) and (ema9_curr > ema21_curr)
        bearish_cross = (ema9_prev >= ema21_prev) and (ema9_curr < ema21_curr)

        if bullish_cross:
            rsi_ok        = RSI_LONG_MIN <= rsi_curr <= RSI_LONG_MAX
            price_aligned = close > ema9_curr > ema21_curr

            if rsi_ok and price_aligned:
                sl     = close - EMA_ATR_MULTIPLIER_SL * atr_curr
                target = close + EMA_ATR_MULTIPLIER_TGT * atr_curr

                signal = Signal(
                    symbol=symbol, direction=Direction.LONG,
                    strategy=self.name,
                    entry=round(close, 2),
                    stop_loss=round(sl, 2),
                    target=round(target, 2),
                    confidence=self._confidence(rsi_curr, ema9_curr, ema21_curr,
                                                close, atr_curr),
                    notes=f"EMA Cross Long | RSI:{rsi_curr:.1f} EMA9:{ema9_curr:.2f}"
                )

                if signal.is_valid():
                    logger.info(f"SIGNAL: {signal}")
                    self._trade_taken.add(symbol)
                    return signal

        elif bearish_cross:
            rsi_ok        = RSI_SHORT_MIN <= rsi_curr <= RSI_SHORT_MAX
            price_aligned = close < ema9_curr < ema21_curr

            if rsi_ok and price_aligned:
                sl     = close + EMA_ATR_MULTIPLIER_SL * atr_curr
                target = close - EMA_ATR_MULTIPLIER_TGT * atr_curr

                signal = Signal(
                    symbol=symbol, direction=Direction.SHORT,
                    strategy=self.name,
                    entry=round(close, 2),
                    stop_loss=round(sl, 2),
                    target=round(target, 2),
                    confidence=self._confidence(rsi_curr, ema9_curr, ema21_curr,
                                                close, atr_curr),
                    notes=f"EMA Cross Short | RSI:{rsi_curr:.1f} EMA9:{ema9_curr:.2f}"
                )

                if signal.is_valid():
                    logger.info(f"SIGNAL: {signal}")
                    self._trade_taken.add(symbol)
                    return signal

        return None

    def _confidence(self, rsi_val, ema9, ema21, close, atr_val) -> float:
        score = 50.0

        # RSI in ideal zone
        if 58 <= rsi_val <= 68:
            score += 20
        elif 55 <= rsi_val <= 72:
            score += 10

        # EMA separation
        sep_pct = abs(ema9 - ema21) / ema21 * 100
        if sep_pct > 0.2:
            score += 15
        elif sep_pct > 0.1:
            score += 8

        # ATR context
        atr_pct = atr_val / close * 100
        if 0.4 <= atr_pct <= 1.2:
            score += 15

        return min(max(round(score, 1), 0), 100.0)
