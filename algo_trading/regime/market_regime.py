# ============================================================
#  regime/market_regime.py
#  Market Regime Classifier — 5-state classification
#  Uses ADX, EMA50/200, Bollinger Band Width, India VIX
# ============================================================

import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import Tuple

from utils.indicators import adx, ema, bollinger_bands, bb_width_percentile
from config.config import (
    ADX_TREND_THRESHOLD, EMA_SHORT_PERIOD, EMA_LONG_PERIOD,
    INDIA_VIX_HIGH, INDIA_VIX_EXTREME,
    BB_VOL_HIGH_PERCENTILE, BB_VOL_LOW_PERCENTILE
)
from utils.logger import get_logger

logger = get_logger("regime")


@dataclass
class MarketRegime:
    trend:       str    # BULL | BEAR | RANGE
    volatility:  str    # HIGH_VOL | NORMAL_VOL | LOW_VOL
    adx:         float  = 0.0
    india_vix:   float  = 0.0
    bb_width_pct: float = 0.0

    @property
    def key(self) -> Tuple[str, str]:
        return (self.trend, self.volatility)

    @property
    def is_tradeable(self) -> bool:
        """False when regime conditions are unfavourable for our strategies"""
        if self.india_vix > INDIA_VIX_EXTREME:
            return False
        if self.trend == "RANGE" and self.volatility == "LOW_VOL":
            return False
        return True

    @property
    def size_multiplier(self) -> float:
        """Risk sizing multiplier based on regime"""
        if self.india_vix > INDIA_VIX_HIGH:
            return 0.5
        if self.volatility == "HIGH_VOL":
            return 0.7
        if self.trend == "RANGE":
            return 0.6
        return 1.0

    def __str__(self):
        tradeable = "✓ TRADEABLE" if self.is_tradeable else "✗ AVOID"
        return (f"Regime: {self.trend} | {self.volatility} | "
                f"ADX:{self.adx:.1f} VIX:{self.india_vix:.1f} | "
                f"Size:{self.size_multiplier:.1f}x | {tradeable}")


class MarketRegimeClassifier:
    """
    Classifies current market regime using:
    - ADX: trend strength
    - EMA 50 vs EMA 200: trend direction
    - Bollinger Band Width percentile: volatility regime
    - India VIX: fear gauge

    Input: Daily candle DataFrame for Nifty50 (at least 200 bars)
    """

    # Strategy → regime suitability map
    REGIME_STRATEGY_MAP = {
        ('BULL', 'NORMAL_VOL'):  ['VWAP_PULLBACK', 'ORB_15'],
        ('BULL', 'HIGH_VOL'):    ['ORB_15', 'BREAKOUT_ATR'],
        ('BULL', 'LOW_VOL'):     ['VWAP_PULLBACK'],
        ('BEAR', 'NORMAL_VOL'):  ['VWAP_PULLBACK', 'ORB_15'],
        ('BEAR', 'HIGH_VOL'):    ['ORB_15', 'BREAKOUT_ATR'],
        ('BEAR', 'LOW_VOL'):     ['VWAP_PULLBACK'],
        ('RANGE', 'NORMAL_VOL'): ['VWAP_PULLBACK'],
        ('RANGE', 'HIGH_VOL'):   ['ORB_15'],
        ('RANGE', 'LOW_VOL'):    [],  # NO TRADES
    }

    def classify(self, nifty_daily: pd.DataFrame,
                 india_vix: float = 0.0) -> MarketRegime:
        """
        Classify current market regime.

        Args:
            nifty_daily: DataFrame with columns: open, high, low, close, volume
                         Must have at least 200 rows (daily bars)
            india_vix:   Latest India VIX reading (fetched from NSE)

        Returns:
            MarketRegime object
        """
        if len(nifty_daily) < EMA_LONG_PERIOD:
            logger.warning(
                f"Not enough daily bars ({len(nifty_daily)}) for regime "
                f"classification. Need {EMA_LONG_PERIOD}. Defaulting to RANGE/NORMAL."
            )
            return MarketRegime("RANGE", "NORMAL_VOL", india_vix=india_vix)

        close = nifty_daily['close']
        high  = nifty_daily['high']
        low   = nifty_daily['low']

        # --- Trend Regime ---
        adx_val   = adx(high, low, close, period=14).iloc[-1]
        ema50     = ema(close, EMA_SHORT_PERIOD).iloc[-1]
        ema200    = ema(close, EMA_LONG_PERIOD).iloc[-1]
        price     = close.iloc[-1]

        if adx_val > ADX_TREND_THRESHOLD and price > ema50 > ema200:
            trend = "BULL"
        elif adx_val > ADX_TREND_THRESHOLD and price < ema50 < ema200:
            trend = "BEAR"
        else:
            trend = "RANGE"

        # --- Volatility Regime ---
        bb    = bollinger_bands(close, period=20)
        bb_w  = bb['width']
        bb_w_pct = bb_width_percentile(bb_w.iloc[-1], bb_w.dropna())

        if india_vix > INDIA_VIX_HIGH or bb_w_pct > BB_VOL_HIGH_PERCENTILE:
            vol_regime = "HIGH_VOL"
        elif india_vix < 12 or bb_w_pct < BB_VOL_LOW_PERCENTILE:
            vol_regime = "LOW_VOL"
        else:
            vol_regime = "NORMAL_VOL"

        regime = MarketRegime(
            trend=trend,
            volatility=vol_regime,
            adx=round(adx_val, 2),
            india_vix=round(india_vix, 2),
            bb_width_pct=round(bb_w_pct, 1)
        )

        logger.info(f"Market Regime: {regime}")
        return regime

    def get_eligible_strategies(self, regime: MarketRegime) -> list:
        """Returns list of strategy names suited for the current regime"""
        strategies = self.REGIME_STRATEGY_MAP.get(regime.key, [])
        logger.info(f"Eligible strategies for {regime.key}: {strategies}")
        return strategies

    def classify_intraday(self, nifty_5min: pd.DataFrame,
                           india_vix: float = 0.0) -> MarketRegime:
        """
        Lighter intraday regime classification using 5-min Nifty data.
        Used for mid-session regime updates.
        """
        if len(nifty_5min) < 20:
            return MarketRegime("RANGE", "NORMAL_VOL", india_vix=india_vix)

        close = nifty_5min['close']
        high  = nifty_5min['high']
        low   = nifty_5min['low']

        adx_val = adx(high, low, close, period=14).iloc[-1]
        ema20   = ema(close, 20).iloc[-1]
        price   = close.iloc[-1]

        if adx_val > 20 and price > ema20:
            trend = "BULL"
        elif adx_val > 20 and price < ema20:
            trend = "BEAR"
        else:
            trend = "RANGE"

        if india_vix > INDIA_VIX_HIGH:
            vol = "HIGH_VOL"
        else:
            vol = "NORMAL_VOL"

        return MarketRegime(trend, vol, adx_val, india_vix)
