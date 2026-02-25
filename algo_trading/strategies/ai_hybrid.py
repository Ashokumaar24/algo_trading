# ============================================================
#  strategies/ai_hybrid.py
#  AI Hybrid Strategy — Regime-aware meta-strategy selector
#  Dynamically picks the best strategy for current conditions
# ============================================================

import pandas as pd
from datetime import datetime
from typing import Optional, List

from strategies.base_strategy import Signal
from strategies.orb_strategy import ORBStrategy
from strategies.vwap_pullback import VWAPPullbackStrategy
from strategies.ema_rsi_strategy import EMARSIStrategy
from strategies.breakout_atr import BreakoutATRStrategy
from regime.market_regime import MarketRegimeClassifier, MarketRegime
from utils.logger import get_logger

logger = get_logger("ai_hybrid")


class AIHybridStrategy:
    """
    AI Hybrid Meta-Strategy.

    Architecture:
    Layer 1: Market Regime Classification (trend + volatility)
    Layer 2: Strategy eligibility filtering by regime
    Layer 3: Sentiment gate (blocks contradictory signals)
    Layer 4: Signal generation from eligible strategies
    Layer 5: Best signal selection by confidence score
    Layer 6: Dynamic position sizing via RiskManager

    This is the ONLY strategy class the main system interacts with.
    It orchestrates all sub-strategies internally.
    """

    # Strategy registry
    STRATEGY_NAMES = {
        'ORB_15':        'orb',
        'VWAP_PULLBACK': 'vwap',
        'EMA_RSI':       'ema_rsi',
        'BREAKOUT_ATR':  'breakout',
    }

    def __init__(self):
        # Initialise sub-strategies
        self.orb      = ORBStrategy()
        self.vwap     = VWAPPullbackStrategy()
        self.ema_rsi  = EMARSIStrategy()
        self.breakout = BreakoutATRStrategy()

        self.regime_classifier = MarketRegimeClassifier()

        self._current_regime: Optional[MarketRegime] = None

    # ------------------------------------------------------------------
    # DAILY SETUP
    # ------------------------------------------------------------------
    def setup_day(self, nifty_daily: pd.DataFrame, india_vix: float = 0.0,
                  prev_day_data: dict = None):
        """
        Call once at start of each trading day.

        Args:
            nifty_daily:   Daily Nifty50 OHLCV DataFrame (200+ rows)
            india_vix:     Today's India VIX reading
            prev_day_data: Dict {symbol: {high, low, close}} for Breakout strategy
        """
        # Classify regime
        self._current_regime = self.regime_classifier.classify(
            nifty_daily, india_vix
        )
        logger.info(f"AI Hybrid Day Setup | {self._current_regime}")

        # Reset all sub-strategies
        self.orb.reset_daily()
        self.vwap.reset_daily()
        self.ema_rsi.reset_daily()
        self.breakout.reset_daily()

        # Set previous day data for breakout strategy
        if prev_day_data:
            for symbol, data in prev_day_data.items():
                self.breakout.set_prev_day_data(
                    symbol, data['high'], data['low'], data['close']
                )

    def set_orb(self, symbol: str, candle_915: dict, candle_930: dict):
        """Set opening range for ORB strategy at 9:30 AM"""
        self.orb.set_orb(symbol, candle_915, candle_930)

    # ------------------------------------------------------------------
    # MAIN SIGNAL GENERATION
    # ------------------------------------------------------------------
    def get_signal(self, symbol: str,
                    candle: dict,
                    candle_history: pd.DataFrame,
                    avg_volume_20d: float,
                    cum_volume_today: float,
                    avg_daily_volume: float,
                    sentiment_score: float = 0.0,
                    nifty_5min: pd.DataFrame = None,
                    india_vix: float = 0.0) -> Optional[Signal]:
        """
        Master signal generation method.

        Args:
            symbol:           Stock symbol
            candle:           Current completed 5-min candle
            candle_history:   Historical 5-min candles DataFrame
            avg_volume_20d:   20-day avg daily volume
            cum_volume_today: Cumulative intraday volume today
            avg_daily_volume: 20-day avg daily volume (for breakout)
            sentiment_score:  NLP sentiment [-1 to +1]
            nifty_5min:       Nifty 5-min data for intraday regime update
            india_vix:        Current VIX reading

        Returns:
            Best Signal object or None
        """
        # --- Get/update regime ---
        regime = self._current_regime
        if regime is None:
            logger.warning("Regime not set — call setup_day() first")
            return None

        # Optionally update intraday regime
        if nifty_5min is not None and len(nifty_5min) >= 20:
            regime = self.regime_classifier.classify_intraday(
                nifty_5min, india_vix
            )

        # --- Hard gate: not tradeable in this regime ---
        if not regime.is_tradeable:
            logger.info(f"AI Hybrid: No trade — regime not tradeable ({regime})")
            return None

        # --- Get eligible strategies for this regime ---
        eligible_strategy_names = self.regime_classifier.get_eligible_strategies(regime)

        if not eligible_strategy_names:
            logger.info(f"AI Hybrid: No eligible strategies for {regime.key}")
            return None

        # --- Sentiment gate ---
        eligible_strategy_names = self._apply_sentiment_gate(
            eligible_strategy_names, sentiment_score, regime
        )

        if not eligible_strategy_names:
            logger.info("AI Hybrid: All strategies blocked by sentiment gate")
            return None

        # --- Collect signals from eligible strategies ---
        signals: List[Signal] = []

        prev_candle = None
        if len(candle_history) >= 2:
            prev_candle = candle_history.iloc[-2].to_dict()

        for strat_name in eligible_strategy_names:
            signal = self._get_strategy_signal(
                strat_name, symbol, candle, candle_history,
                avg_volume_20d, cum_volume_today, avg_daily_volume,
                prev_candle
            )
            if signal:
                signal.regime    = f"{regime.trend}_{regime.volatility}"
                signal.sentiment = sentiment_score
                signals.append(signal)

        if not signals:
            return None

        # --- Pick best signal (highest confidence) ---
        best_signal = max(signals, key=lambda s: s.confidence)

        logger.info(
            f"AI Hybrid Signal | Regime:{regime.key} | "
            f"Candidates:{len(signals)} | Best:{best_signal}"
        )

        return best_signal

    # ------------------------------------------------------------------
    # STRATEGY DISPATCH
    # ------------------------------------------------------------------
    def _get_strategy_signal(self, strategy_name: str, symbol: str,
                               candle: dict, candle_history: pd.DataFrame,
                               avg_volume_20d: float, cum_volume_today: float,
                               avg_daily_volume: float,
                               prev_candle: Optional[dict]) -> Optional[Signal]:
        """Dispatch signal check to the appropriate sub-strategy"""
        try:
            if strategy_name == 'ORB_15':
                return self.orb.check_entry(
                    symbol, candle, candle_history, avg_volume_20d
                )
            elif strategy_name == 'VWAP_PULLBACK':
                return self.vwap.check_entry(
                    symbol, candle, candle_history, prev_candle
                )
            elif strategy_name == 'EMA_RSI':
                return self.ema_rsi.check_entry(
                    symbol, candle, candle_history
                )
            elif strategy_name == 'BREAKOUT_ATR':
                return self.breakout.check_entry(
                    symbol, candle, candle_history,
                    cum_volume_today, avg_daily_volume
                )
        except Exception as e:
            logger.error(f"Strategy error [{strategy_name}] {symbol}: {e}")

        return None

    # ------------------------------------------------------------------
    # SENTIMENT GATE
    # ------------------------------------------------------------------
    def _apply_sentiment_gate(self, strategies: List[str],
                                sentiment: float,
                                regime: MarketRegime) -> List[str]:
        """
        Filter strategies based on sentiment.
        Blocks trend-following longs on strongly negative sentiment
        and trend-following shorts on strongly positive sentiment.
        """
        filtered = []
        for s in strategies:
            # Strong negative sentiment blocks VWAP long setups
            if sentiment < -0.35 and regime.trend == 'BULL' and s == 'VWAP_PULLBACK':
                logger.debug(
                    f"Sentiment gate: blocking {s} (sentiment={sentiment:.2f}, BULL regime)"
                )
                continue
            # Strong positive sentiment blocks VWAP short setups
            if sentiment > 0.35 and regime.trend == 'BEAR' and s == 'VWAP_PULLBACK':
                logger.debug(
                    f"Sentiment gate: blocking {s} (sentiment={sentiment:.2f}, BEAR regime)"
                )
                continue
            filtered.append(s)

        return filtered

    # ------------------------------------------------------------------
    # STATUS
    # ------------------------------------------------------------------
    def get_status(self) -> dict:
        regime = self._current_regime
        return {
            'regime':     str(regime) if regime else "Not set",
            'tradeable':  regime.is_tradeable if regime else False,
            'size_mult':  regime.size_multiplier if regime else 1.0,
        }
