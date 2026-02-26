# ============================================================
#  strategies/ai_hybrid.py
#  AI Hybrid Strategy — Regime-aware meta-strategy selector
#
#  FIX: EMA_RSI strategy was instantiated in __init__ but is
#       never present in REGIME_STRATEGY_MAP and was marked
#       as "retired" after backtests showed no gross edge.
#       Removed instantiation to eliminate dead code and
#       unnecessary memory + import overhead.
# ============================================================

import pandas as pd
from datetime import datetime
from typing import Optional, List

from strategies.base_strategy import Signal
from strategies.orb_strategy import ORBStrategy
from strategies.vwap_pullback import VWAPPullbackStrategy
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

    Active strategies: ORB_15, VWAP_PULLBACK, BREAKOUT_ATR
    Retired:           EMA_RSI (no gross edge on 5-min Nifty data)
    """

    def __init__(self):
        # FIX: removed self.ema_rsi = EMARSIStrategy()
        #      EMA_RSI has no gross edge and is not in REGIME_STRATEGY_MAP.
        #      Keeping it instantiated was dead weight.
        self.orb      = ORBStrategy()
        self.vwap     = VWAPPullbackStrategy()
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
        self._current_regime = self.regime_classifier.classify(
            nifty_daily, india_vix
        )
        logger.info(f"AI Hybrid Day Setup | {self._current_regime}")

        self.orb.reset_daily()
        self.vwap.reset_daily()
        self.breakout.reset_daily()

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

        regime = self._current_regime
        if regime is None:
            logger.warning("Regime not set — call setup_day() first")
            return None

        if nifty_5min is not None and len(nifty_5min) >= 20:
            regime = self.regime_classifier.classify_intraday(
                nifty_5min, india_vix
            )

        if not regime.is_tradeable:
            logger.info(f"AI Hybrid: No trade — regime not tradeable ({regime})")
            return None

        eligible_strategy_names = self.regime_classifier.get_eligible_strategies(regime)

        if not eligible_strategy_names:
            logger.info(f"AI Hybrid: No eligible strategies for {regime.key}")
            return None

        eligible_strategy_names = self._apply_sentiment_gate(
            eligible_strategy_names, sentiment_score, regime
        )

        if not eligible_strategy_names:
            logger.info("AI Hybrid: All strategies blocked by sentiment gate")
            return None

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
        try:
            if strategy_name == 'ORB_15':
                return self.orb.check_entry(
                    symbol, candle, candle_history, avg_volume_20d
                )
            elif strategy_name == 'VWAP_PULLBACK':
                return self.vwap.check_entry(
                    symbol, candle, candle_history, prev_candle
                )
            elif strategy_name == 'BREAKOUT_ATR':
                return self.breakout.check_entry(
                    symbol, candle, candle_history,
                    cum_volume_today, avg_daily_volume
                )
            # NOTE: EMA_RSI deliberately omitted — retired strategy
        except Exception as e:
            logger.error(f"Strategy error [{strategy_name}] {symbol}: {e}")

        return None

    # ------------------------------------------------------------------
    # SENTIMENT GATE
    # ------------------------------------------------------------------
    def _apply_sentiment_gate(self, strategies: List[str],
                               sentiment: float,
                               regime: MarketRegime) -> List[str]:
        filtered = []
        for s in strategies:
            if sentiment < -0.35 and regime.trend == 'BULL' and s == 'VWAP_PULLBACK':
                logger.debug(f"Sentiment gate: blocking {s} (sentiment={sentiment:.2f})")
                continue
            if sentiment > 0.35 and regime.trend == 'BEAR' and s == 'VWAP_PULLBACK':
                logger.debug(f"Sentiment gate: blocking {s} (sentiment={sentiment:.2f})")
                continue
            filtered.append(s)
        return filtered

    # ------------------------------------------------------------------
    # STATUS
    # ------------------------------------------------------------------
    def get_status(self) -> dict:
        regime = self._current_regime
        return {
            'regime':    str(regime) if regime else "Not set",
            'tradeable': regime.is_tradeable if regime else False,
            'size_mult': regime.size_multiplier if regime else 1.0,
        }
