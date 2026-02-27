# ============================================================
#  strategies/vwap_pullback.py
#  VWAP Pullback Strategy — Core strategy of the AI Hybrid
#
#  FIX 7 (Logic): Replaced no-op reward_risk >= 1.3 filter.
#    - Target is always 2.0 × risk, so reward_risk is always 2.0.
#    - The old check (2.0 >= 1.3) always passed — it filtered nothing.
#    - Replaced with MIN_RISK_PCT = 0.002 (0.2% of entry price).
#    - Setups with risk < 0.2% are now skipped (too tight for slippage).
#
#  FIX (existing): `prev_low` falsy check was `if prev_low else close`
#    - Evaluates to `close` when prev_low is 0 or any falsy number.
#    - Changed to explicit `if prev_low is not None else close`.
# ============================================================

import pandas as pd
from datetime import time, datetime
from typing import Optional

from strategies.base_strategy import BaseStrategy, Signal, Direction
from utils.indicators import ema, vwap, atr
from config.config import (
    VWAP_EMA_PERIOD, VWAP_TOLERANCE_PCT, VWAP_ENTRY_DEADLINE,
    VWAP_TRAIL_AFTER_1R
)
from utils.logger import get_logger

logger = get_logger("vwap_pullback")

# FIX 7: Minimum risk threshold — skip setups where risk is < 0.2% of price.
# This filters out overly tight stops that would be wiped by normal slippage.
MIN_RISK_PCT = 0.002   # 0.2% of entry price


class VWAPPullbackStrategy(BaseStrategy):
    """
    VWAP Pullback Trend Continuation Strategy.

    LONG Setup:
    1. Price above VWAP AND VWAP > 20 EMA (uptrend)
    2. 20 EMA sloping upward (EMA[-1] > EMA[-3])
    3. Previous candle pulled back to VWAP (low <= VWAP + tolerance)
    4. Previous candle CLOSED above VWAP (held above)
    5. Current (signal) candle: bullish close above prev high
    6. Risk >= 0.2% of entry price (FIX 7)

    Stop Loss:  Below pullback candle low OR below VWAP (whichever is lower)
    Target 1R:  Move SL to breakeven
    Target 2R:  Full exit (2.0 × risk)

    SHORT Setup: Mirror logic with inverted conditions.
    """

    def __init__(self):
        super().__init__("VWAP_PULLBACK")
        self._pullback_seen: dict = {}
        self._trade_taken: set   = set()

    def reset_daily(self):
        self._pullback_seen.clear()
        self._trade_taken.clear()
        logger.info("VWAP Pullback: Daily reset complete")

    def check_entry(self, symbol: str, candle: dict,
                    candle_history: pd.DataFrame,
                    prev_candle: Optional[dict] = None) -> Optional[Signal]:

        if symbol in self._trade_taken:
            return None

        candle_time = candle.get('timestamp', datetime.now())
        if isinstance(candle_time, datetime):
            if candle_time.time() > VWAP_ENTRY_DEADLINE:
                return None

        if not self._validate_candle_count(candle_history, 25):
            return None

        if prev_candle is None and len(candle_history) >= 2:
            prev_candle = candle_history.iloc[-2].to_dict()

        if prev_candle is None:
            return None

        # --- Compute Indicators ---
        ema20       = ema(candle_history['close'], VWAP_EMA_PERIOD)
        vwap_series = vwap(
            candle_history['high'], candle_history['low'],
            candle_history['close'], candle_history['volume']
        )
        atr_val = atr(
            candle_history['high'], candle_history['low'],
            candle_history['close'], period=14
        ).iloc[-1]

        ema20_curr  = ema20.iloc[-1]
        ema20_prev3 = ema20.iloc[-3] if len(ema20) >= 3 else ema20.iloc[0]
        vwap_curr   = vwap_series.iloc[-1]

        close = candle['close']
        open_ = candle['open']
        high  = candle['high']
        low   = candle['low']

        prev_close = prev_candle.get('close', prev_candle.get('Close'))
        prev_high  = prev_candle.get('high',  prev_candle.get('High'))
        prev_low   = prev_candle.get('low',   prev_candle.get('Low'))

        tol = vwap_curr * VWAP_TOLERANCE_PCT

        # ================================================================
        # LONG SETUP
        # ================================================================
        trend_up = (
            close > vwap_curr and
            vwap_curr > ema20_curr and
            ema20_curr > ema20_prev3
        )

        pullback_to_vwap_long = (
            prev_low is not None and
            prev_low <= vwap_curr + tol and
            prev_close is not None and
            prev_close > vwap_curr
        )

        confirmation_long = (
            close > open_ and
            prev_high is not None and
            close > prev_high and
            low > vwap_curr - tol
        )

        if trend_up and pullback_to_vwap_long and confirmation_long:
            sl     = min(prev_low, vwap_curr - tol) - atr_val * 0.1
            risk   = close - sl

            # FIX 7: Skip if risk is too small — protects against slippage eating the SL
            risk_pct = risk / close if close > 0 else 0
            if risk_pct < MIN_RISK_PCT:
                logger.debug(
                    f"VWAP Long skipped | {symbol} | risk {risk_pct*100:.3f}% "
                    f"< min {MIN_RISK_PCT*100:.2f}% (too tight for slippage)"
                )
                return None

            target = close + 2.0 * risk

            conf = self._confidence(close, vwap_curr, ema20_curr, ema20_prev3,
                                    atr_val, "LONG")

            signal = Signal(
                symbol=symbol, direction=Direction.LONG, strategy=self.name,
                entry=round(close, 2), stop_loss=round(sl, 2),
                target=round(target, 2), target_2=round(target, 2),
                trail_trigger=round(risk, 2), confidence=conf,
                notes=(f"VWAP Pullback Long | VWAP:{vwap_curr:.2f} "
                       f"EMA20:{ema20_curr:.2f} ATR:{atr_val:.2f}")
            )

            if signal.is_valid():
                logger.info(f"SIGNAL: {signal}")
                self._trade_taken.add(symbol)
                return signal

        # ================================================================
        # SHORT SETUP
        # ================================================================
        trend_down = (
            close < vwap_curr and
            vwap_curr < ema20_curr and
            ema20_curr < ema20_prev3
        )

        pullback_to_vwap_short = (
            prev_high is not None and
            prev_high >= vwap_curr - tol and
            prev_close is not None and
            prev_close < vwap_curr
        )

        confirmation_short = (
            close < open_ and
            # FIX (existing): explicit None check — old `if prev_low else close`
            # would evaluate to `close` when prev_low=0 (falsy number)
            close < (prev_low if prev_low is not None else close) and
            high < vwap_curr + tol
        )

        if trend_down and pullback_to_vwap_short and confirmation_short:
            sl   = max(prev_high, vwap_curr + tol) + atr_val * 0.1
            risk = sl - close

            # FIX 7: Skip if risk is too small
            risk_pct = risk / close if close > 0 else 0
            if risk_pct < MIN_RISK_PCT:
                logger.debug(
                    f"VWAP Short skipped | {symbol} | risk {risk_pct*100:.3f}% "
                    f"< min {MIN_RISK_PCT*100:.2f}% (too tight for slippage)"
                )
                return None

            target = close - 2.0 * risk

            conf = self._confidence(close, vwap_curr, ema20_curr, ema20_prev3,
                                    atr_val, "SHORT")

            signal = Signal(
                symbol=symbol, direction=Direction.SHORT, strategy=self.name,
                entry=round(close, 2), stop_loss=round(sl, 2),
                target=round(target, 2), target_2=round(target, 2),
                trail_trigger=round(risk, 2), confidence=conf,
                notes=(f"VWAP Pullback Short | VWAP:{vwap_curr:.2f} "
                       f"EMA20:{ema20_curr:.2f} ATR:{atr_val:.2f}")
            )

            if signal.is_valid():
                logger.info(f"SIGNAL: {signal}")
                self._trade_taken.add(symbol)
                return signal

        return None

    def _confidence(self, close, vwap_val, ema20, ema20_3ago,
                    atr_val, direction) -> float:
        score = 50.0

        ema_slope_pct = (
            abs(ema20 - ema20_3ago) / ema20_3ago * 100
            if ema20_3ago and ema20_3ago != 0 else 0
        )
        if ema_slope_pct > 0.05:
            score += 20
        elif ema_slope_pct > 0.02:
            score += 10

        dist_pct = abs(close - vwap_val) / vwap_val * 100 if vwap_val != 0 else 0
        if 0.1 <= dist_pct <= 0.5:
            score += 20
        elif dist_pct < 0.1:
            score += 5
        elif dist_pct > 0.8:
            score -= 10

        atr_pct = atr_val / close * 100 if close != 0 else 0
        if 0.3 <= atr_pct <= 1.0:
            score += 10

        return min(max(round(score, 1), 0), 100.0)
