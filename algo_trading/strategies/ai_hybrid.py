# ============================================================
#  strategies/ai_hybrid.py
#  AI Hybrid Strategy — Regime-aware meta-strategy selector
#
#  ADDED: journal logging when regime or sentiment blocks strategies
# ============================================================

import pandas as pd
from datetime import datetime
from typing import Optional, List

from strategies.base_strategy import Signal
from strategies.orb_strategy import ORBStrategy
from strategies.vwap_pullback import VWAPPullbackStrategy
from strategies.breakout_atr import BreakoutATRStrategy
from regime.market_regime import MarketRegimeClassifier, MarketRegime
from utils.daily_journal import get_journal          # ← ADDED
from utils.logger import get_logger

logger = get_logger("ai_hybrid")


class AIHybridStrategy:
    """
    AI Hybrid Meta-Strategy.
    Active strategies: ORB_15, VWAP_PULLBACK, BREAKOUT_ATR
    Retired:           EMA_RSI (no gross edge on 5-min Nifty data)
    """

    def __init__(self):
        self.orb      = ORBStrategy()
        self.vwap     = VWAPPullbackStrategy()
        self.breakout = BreakoutATRStrategy()

        self.regime_classifier  = MarketRegimeClassifier()
        self._current_regime: Optional[MarketRegime] = None
        self._regime_blocked_logged_today: set = set()   # avoid duplicate logs

    def setup_day(self, nifty_daily: pd.DataFrame, india_vix: float = 0.0,
                  prev_day_data: dict = None):
        self._current_regime = self.regime_classifier.classify(nifty_daily, india_vix)
        logger.info(f"AI Hybrid Day Setup | {self._current_regime}")

        self.orb.reset_daily()
        self.vwap.reset_daily()
        self.breakout.reset_daily()
        self._regime_blocked_logged_today.clear()

        if prev_day_data:
            for symbol, data in prev_day_data.items():
                self.breakout.set_prev_day_data(
                    symbol, data['high'], data['low'], data['close']
                )

    def set_orb(self, symbol: str, candle_915: dict, candle_930: dict):
        self.orb.set_orb(symbol, candle_915, candle_930)

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
            regime = self.regime_classifier.classify_intraday(nifty_5min, india_vix)

        candle_price = candle.get('close', 0)

        # ADDED: log regime-blocked once per day (not every candle)
        if not regime.is_tradeable:
            if symbol not in self._regime_blocked_logged_today:
                get_journal().log_trade_blocked(
                    symbol=symbol,
                    strategy="ALL",
                    block_type="REGIME",
                    reason=f"Regime {regime.trend}+{regime.volatility} not tradeable",
                    detail=(
                        f"Market regime is {regime.trend} + {regime.volatility}. "
                        f"ADX: {regime.adx:.1f}, India VIX: {regime.india_vix:.1f}. "
                        f"This combination historically loses money — "
                        f"{'VIX too high (extreme fear)' if regime.india_vix > 28 else 'ranging + low volatility market'}. "
                        f"All entries blocked for today."
                    ),
                    candle_price=candle_price
                )
                self._regime_blocked_logged_today.add(symbol)
            return None

        eligible_strategy_names = self.regime_classifier.get_eligible_strategies(regime)

        if not eligible_strategy_names:
            # ADDED: log no eligible strategies
            if f"no_strat_{symbol}" not in self._regime_blocked_logged_today:
                get_journal().log_trade_blocked(
                    symbol=symbol,
                    strategy="ALL",
                    block_type="REGIME",
                    reason=f"No strategies eligible for {regime.trend}+{regime.volatility}",
                    detail=(
                        f"The regime map has no strategies assigned for "
                        f"{regime.trend}+{regime.volatility}. "
                        f"This occurs for RANGE+LOW_VOL — the system correctly sits out."
                    ),
                    candle_price=candle_price
                )
                self._regime_blocked_logged_today.add(f"no_strat_{symbol}")
            return None

        filtered_strategies = self._apply_sentiment_gate(
            eligible_strategy_names, sentiment_score, regime
        )

        # ADDED: log sentiment-blocked strategies
        blocked_by_sentiment = set(eligible_strategy_names) - set(filtered_strategies)
        for strat in blocked_by_sentiment:
            key = f"sentiment_{symbol}_{strat}"
            if key not in self._regime_blocked_logged_today:
                direction = "bearish" if sentiment_score < -0.35 else "bullish"
                get_journal().log_trade_blocked(
                    symbol=symbol,
                    strategy=strat,
                    block_type="SENTIMENT",
                    reason=f"Sentiment gate ({sentiment_score:.2f}) blocked {strat}",
                    detail=(
                        f"Sentiment score is {sentiment_score:.2f} (strongly {direction}). "
                        f"Taking a {strat} signal in the opposite direction of sentiment "
                        f"historically reduces win rate. Strategy blocked for this candle."
                    ),
                    candle_price=candle_price
                )
                self._regime_blocked_logged_today.add(key)

        if not filtered_strategies:
            return None

        signals: List[Signal] = []
        prev_candle = None
        if len(candle_history) >= 2:
            prev_candle = candle_history.iloc[-2].to_dict()

        for strat_name in filtered_strategies:
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

    def _get_strategy_signal(self, strategy_name: str, symbol: str,
                              candle: dict, candle_history: pd.DataFrame,
                              avg_volume_20d: float, cum_volume_today: float,
                              avg_daily_volume: float,
                              prev_candle: Optional[dict]) -> Optional[Signal]:
        try:
            if strategy_name == 'ORB_15':
                return self.orb.check_entry(symbol, candle, candle_history, avg_volume_20d)
            elif strategy_name == 'VWAP_PULLBACK':
                return self.vwap.check_entry(symbol, candle, candle_history, prev_candle)
            elif strategy_name == 'BREAKOUT_ATR':
                return self.breakout.check_entry(
                    symbol, candle, candle_history, cum_volume_today, avg_daily_volume
                )
        except Exception as e:
            logger.error(f"Strategy error [{strategy_name}] {symbol}: {e}")
        return None

    def _apply_sentiment_gate(self, strategies: List[str],
                               sentiment: float,
                               regime: MarketRegime) -> List[str]:
        filtered = []
        for s in strategies:
            if sentiment < -0.35 and regime.trend == 'BULL' and s == 'VWAP_PULLBACK':
                continue
            if sentiment > 0.35 and regime.trend == 'BEAR' and s == 'VWAP_PULLBACK':
                continue
            filtered.append(s)
        return filtered

    def get_status(self) -> dict:
        regime = self._current_regime
        return {
            'regime':    str(regime) if regime else "Not set",
            'tradeable': regime.is_tradeable if regime else False,
            'size_mult': regime.size_multiplier if regime else 1.0,
        }
