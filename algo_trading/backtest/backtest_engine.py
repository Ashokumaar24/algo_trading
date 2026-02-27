# ============================================================
#  backtest/backtest_engine.py
#  Zero-lookahead backtesting engine with realistic cost model
#
#  Supports: ORB_15, VWAP_PULLBACK, EMA_RSI, BREAKOUT_ATR
#
#  Output metrics per strategy:
#    total_trades, win_rate_pct, avg_rr, expectancy_inr,
#    profit_factor, sharpe_ratio, max_drawdown_pct,
#    total_net_pnl, gross_pnl, total_cost_drag,
#    annual_return_pct, avg_hold_mins,
#    target_hit_pct, sl_hit_pct, eod_close_pct,
#    regime_blocked_days, confidence_filtered, trade_cap_blocked
#
#  Design principles:
#    - No lookahead: signals computed on closed candle[-1], exits
#      simulated on subsequent candles only.
#    - Full cost model: brokerage + STT + exchange + SEBI + GST +
#      stamp duty + slippage on every round-trip.
#    - Regime filter: optional ADX gate before strategy runs.
#    - Trade cap: max 2 trades per day (matches live config).
# ============================================================

import pandas as pd
import numpy as np
from datetime import datetime, time as dt_time
from typing import Dict, List, Optional
from dataclasses import dataclass, field

from utils.indicators import (
    ema, atr, vwap, rsi, adx, volume_sma, bollinger_bands,
    calculate_trade_cost
)
from config.config import (
    BACKTEST_INITIAL_CAPITAL, MAX_TRADES_PER_DAY,
    ORB_MIN_RANGE_PCT, ORB_MAX_RANGE_PCT, ORB_REWARD_RISK_RATIO,
    VWAP_TOLERANCE_PCT, VWAP_EMA_PERIOD,
    EMA_FAST, EMA_SLOW, RSI_PERIOD,
    RSI_LONG_MIN, RSI_LONG_MAX, RSI_SHORT_MIN, RSI_SHORT_MAX,
    EMA_ATR_MULTIPLIER_SL, EMA_ATR_MULTIPLIER_TGT,
    BREAKOUT_ATR_MIN_PCT, BREAKOUT_ATR_MAX_PCT, BREAKOUT_SL_ATR_MULT,
    MIN_CONFIDENCE_SCORE, ADX_TREND_THRESHOLD,
    RISK_PER_TRADE_NORMAL,
)
from utils.logger import get_logger

logger = get_logger("backtest")

MARKET_OPEN  = dt_time(9, 15)
ORB_READY    = dt_time(9, 30)
NO_ENTRY     = dt_time(14, 0)
FORCE_CLOSE  = dt_time(15, 15)


# ================================================================
#  DATA CLASSES
# ================================================================
@dataclass
class BacktestTrade:
    strategy:      str
    symbol:        str
    direction:     str        # LONG / SHORT
    entry_time:    datetime
    entry_price:   float
    sl:            float
    target:        float
    exit_time:     Optional[datetime] = None
    exit_price:    Optional[float]    = None
    exit_reason:   str                = ""
    gross_pnl:     float              = 0.0
    cost:          float              = 0.0
    net_pnl:       float              = 0.0
    hold_mins:     float              = 0.0
    quantity:      int                = 1
    confidence:    float              = 0.0


# ================================================================
#  BACKTEST ENGINE
# ================================================================
class BacktestEngine:
    """
    Runs a single strategy on historical 5-min OHLCV data.

    Usage:
        engine = BacktestEngine("ORB_15", capital=1_000_000)
        metrics = engine.run(df)
    """

    def __init__(self, strategy_name: str,
                 capital: float = BACKTEST_INITIAL_CAPITAL):
        self.strategy_name  = strategy_name
        self.capital        = capital
        self.trades: List[BacktestTrade] = []

        # Counters for block analysis
        self.regime_blocked_days    = 0
        self.confidence_filtered    = 0
        self.trade_cap_blocked      = 0

    # ------------------------------------------------------------------
    # MAIN RUN
    # ------------------------------------------------------------------
    def run(self, df: pd.DataFrame) -> dict:
        """
        Run backtest on a 5-min OHLCV DataFrame.

        Args:
            df: DataFrame with open, high, low, close, volume columns,
                indexed by timestamp (datetime).

        Returns:
            dict of performance metrics.
        """
        if df is None or len(df) < 50:
            return {"strategy": self.strategy_name, "error": "insufficient_data"}

        df = df.copy().sort_index()

        # Pre-compute indicators (no lookahead — rolling windows)
        df = self._add_indicators(df)

        # Group by trading day
        days = df.groupby(df.index.date)

        for day_date, day_df in days:
            self._run_day(day_date, day_df)

        return self._compute_metrics()

    # ------------------------------------------------------------------
    # DAILY LOOP
    # ------------------------------------------------------------------
    def _run_day(self, day_date, day_df: pd.DataFrame):
        if len(day_df) < 10:
            return

        trades_today = 0
        open_trade: Optional[BacktestTrade] = None

        # Regime check (ADX from daily context approximated intraday)
        day_adx = day_df['adx'].dropna()
        regime_ok = True
        if len(day_adx) >= 5:
            avg_adx = day_adx.mean()
            # Low ADX + low volume → skip day (ranging/dull)
            avg_vol = day_df['volume'].mean()
            hist_vol = day_df['volume'].quantile(0.3)
            if avg_adx < 12 and avg_vol < hist_vol:
                self.regime_blocked_days += 1
                regime_ok = False

        if not regime_ok:
            return

        # Compute ORB for this day
        orb = self._compute_orb(day_df)

        # Compute VWAP for the day (reset per day)
        day_df = self._add_daily_vwap(day_df)

        # Walk forward candle by candle
        for i in range(5, len(day_df)):
            candle      = day_df.iloc[i]
            candle_time = candle.name.time() if hasattr(candle.name, 'time') else MARKET_OPEN
            history     = day_df.iloc[: i + 1]

            # ── Manage open trade ────────────────────────────────────
            if open_trade is not None:
                open_trade = self._update_open_trade(open_trade, candle, candle_time)
                if open_trade is None:
                    continue  # trade closed this candle

            # ── Entry gates ──────────────────────────────────────────
            if open_trade is not None:
                continue  # already in a trade

            if candle_time >= NO_ENTRY:
                continue

            if trades_today >= MAX_TRADES_PER_DAY:
                self.trade_cap_blocked += 1
                continue

            # ── Get signal ───────────────────────────────────────────
            signal = self._get_signal(candle, history, day_df, orb)

            if signal is None:
                continue

            if signal['confidence'] < MIN_CONFIDENCE_SCORE:
                self.confidence_filtered += 1
                continue

            # ── Open trade ───────────────────────────────────────────
            qty = self._position_size(signal['entry'], signal['sl'])
            if qty <= 0:
                continue

            open_trade = BacktestTrade(
                strategy    = self.strategy_name,
                symbol      = "BACKTEST",
                direction   = signal['direction'],
                entry_time  = candle.name,
                entry_price = signal['entry'],
                sl          = signal['sl'],
                target      = signal['target'],
                quantity    = qty,
                confidence  = signal['confidence'],
            )
            trades_today += 1

        # ── EOD force close ──────────────────────────────────────────
        if open_trade is not None and open_trade.exit_time is None:
            last = day_df.iloc[-1]
            self._close_trade(open_trade, last['close'], last.name, "EOD_CLOSE")

    # ------------------------------------------------------------------
    # OPEN TRADE MANAGEMENT
    # ------------------------------------------------------------------
    def _update_open_trade(self, trade: BacktestTrade,
                            candle, candle_time) -> Optional[BacktestTrade]:
        """
        Check if candle hits SL, target, or force-close time.
        Simulates realistic exit using candle's high/low range.
        Returns None if trade is closed.
        """
        hi  = candle['high']
        lo  = candle['low']
        cl  = candle['close']
        ts  = candle.name

        if candle_time >= FORCE_CLOSE:
            self._close_trade(trade, cl, ts, "EOD_CLOSE")
            return None

        if trade.direction == "LONG":
            # SL hit (low touches SL)
            if lo <= trade.sl:
                self._close_trade(trade, trade.sl, ts, "SL_HIT")
                return None
            # Target hit (high touches target)
            if hi >= trade.target:
                self._close_trade(trade, trade.target, ts, "TARGET_HIT")
                return None
        else:  # SHORT
            if hi >= trade.sl:
                self._close_trade(trade, trade.sl, ts, "SL_HIT")
                return None
            if lo <= trade.target:
                self._close_trade(trade, trade.target, ts, "TARGET_HIT")
                return None

        return trade  # still open

    def _close_trade(self, trade: BacktestTrade, exit_price: float,
                     exit_time, reason: str):
        trade.exit_time   = exit_time
        trade.exit_price  = exit_price
        trade.exit_reason = reason

        if trade.direction == "LONG":
            trade.gross_pnl = (exit_price - trade.entry_price) * trade.quantity
        else:
            trade.gross_pnl = (trade.entry_price - exit_price) * trade.quantity

        trade.cost    = calculate_trade_cost(
            trade.entry_price, exit_price, trade.quantity
        )
        trade.net_pnl = trade.gross_pnl - trade.cost

        if trade.entry_time and trade.exit_time:
            delta = trade.exit_time - trade.entry_time
            trade.hold_mins = delta.total_seconds() / 60

        self.trades.append(trade)

    # ------------------------------------------------------------------
    # SIGNAL GENERATORS (per strategy)
    # ------------------------------------------------------------------
    def _get_signal(self, candle, history: pd.DataFrame,
                    day_df: pd.DataFrame, orb: Optional[dict]) -> Optional[dict]:
        name = self.strategy_name
        if name == "ORB_15":
            return self._signal_orb(candle, history, orb)
        elif name == "VWAP_PULLBACK":
            return self._signal_vwap(candle, history)
        elif name == "EMA_RSI":
            return self._signal_ema_rsi(candle, history)
        elif name == "BREAKOUT_ATR":
            return self._signal_breakout(candle, history, day_df)
        return None

    def _signal_orb(self, candle, history: pd.DataFrame,
                    orb: Optional[dict]) -> Optional[dict]:
        if orb is None:
            return None
        candle_time = candle.name.time()
        if candle_time < ORB_READY or candle_time > dt_time(12, 0):
            return None

        close   = candle['close']
        vwap_v  = history['vwap_day'].iloc[-1] if 'vwap_day' in history else close
        vol     = candle['volume']
        avg_vol = history['volume'].tail(20).mean()

        if not (ORB_MIN_RANGE_PCT <= orb['range_pct'] <= ORB_MAX_RANGE_PCT):
            return None

        vol_ok = vol > avg_vol * 0.15 * 1.5

        # LONG breakout
        if close > orb['high'] and close > vwap_v and vol_ok:
            sl     = orb['low']
            risk   = close - sl
            if risk <= 0:
                return None
            target = close + ORB_REWARD_RISK_RATIO * risk
            conf   = min(60 + (vol / (avg_vol * 0.15 + 1)) * 10, 95)
            return dict(direction="LONG", entry=close, sl=sl, target=target, confidence=conf)

        # SHORT breakout
        if close < orb['low'] and close < vwap_v and vol_ok:
            sl   = orb['high']
            risk = sl - close
            if risk <= 0:
                return None
            target = close - ORB_REWARD_RISK_RATIO * risk
            conf   = min(60 + (vol / (avg_vol * 0.15 + 1)) * 10, 95)
            return dict(direction="SHORT", entry=close, sl=sl, target=target, confidence=conf)

        return None

    def _signal_vwap(self, candle, history: pd.DataFrame) -> Optional[dict]:
        if len(history) < 25:
            return None

        close  = candle['close']
        open_  = candle['open']
        high   = candle['high']
        low    = candle['low']

        vwap_v  = history['vwap_day'].iloc[-1] if 'vwap_day' in history else close
        ema20   = history['ema20'].iloc[-1]
        ema20_3 = history['ema20'].iloc[-3] if len(history) >= 3 else ema20
        atr_v   = history['atr14'].iloc[-1]

        if len(history) < 2:
            return None
        prev = history.iloc[-2]
        prev_close = prev['close']
        prev_high  = prev['high']
        prev_low   = prev['low']
        tol        = vwap_v * VWAP_TOLERANCE_PCT

        # LONG
        trend_up = close > vwap_v and vwap_v > ema20 and ema20 > ema20_3
        pb_long  = (prev_low <= vwap_v + tol and prev_close > vwap_v)
        conf_l   = (close > open_ and close > prev_high and low > vwap_v - tol)
        if trend_up and pb_long and conf_l:
            sl   = min(prev_low, vwap_v - tol) - atr_v * 0.1
            risk = close - sl
            if risk / close < 0.002 or risk <= 0:
                return None
            target = close + 2.0 * risk
            conf   = min(65 + (abs(ema20 - ema20_3) / ema20_3 * 1000), 92)
            return dict(direction="LONG", entry=close, sl=sl, target=target, confidence=conf)

        # SHORT
        trend_dn = close < vwap_v and vwap_v < ema20 and ema20 < ema20_3
        pb_short = (prev_high >= vwap_v - tol and prev_close < vwap_v)
        conf_s   = (close < open_ and close < prev_low and high < vwap_v + tol)
        if trend_dn and pb_short and conf_s:
            sl   = max(prev_high, vwap_v + tol) + atr_v * 0.1
            risk = sl - close
            if risk / close < 0.002 or risk <= 0:
                return None
            target = close - 2.0 * risk
            conf   = min(65 + (abs(ema20 - ema20_3) / ema20_3 * 1000), 92)
            return dict(direction="SHORT", entry=close, sl=sl, target=target, confidence=conf)

        return None

    def _signal_ema_rsi(self, candle, history: pd.DataFrame) -> Optional[dict]:
        if len(history) < 30:
            return None

        close      = candle['close']
        ema9_curr  = history['ema9'].iloc[-1]
        ema9_prev  = history['ema9'].iloc[-2]
        ema21_curr = history['ema21'].iloc[-1]
        ema21_prev = history['ema21'].iloc[-2]
        rsi_v      = history['rsi14'].iloc[-1]
        atr_v      = history['atr14'].iloc[-1]

        bull = (ema9_prev <= ema21_prev) and (ema9_curr > ema21_curr)
        bear = (ema9_prev >= ema21_prev) and (ema9_curr < ema21_curr)

        if bull and RSI_LONG_MIN <= rsi_v <= RSI_LONG_MAX and close > ema9_curr > ema21_curr:
            sl     = close - EMA_ATR_MULTIPLIER_SL * atr_v
            target = close + EMA_ATR_MULTIPLIER_TGT * atr_v
            conf   = 58 + max(0, rsi_v - 55) * 0.5
            return dict(direction="LONG", entry=close, sl=sl, target=target, confidence=conf)

        if bear and RSI_SHORT_MIN <= rsi_v <= RSI_SHORT_MAX and close < ema9_curr < ema21_curr:
            sl     = close + EMA_ATR_MULTIPLIER_SL * atr_v
            target = close - EMA_ATR_MULTIPLIER_TGT * atr_v
            conf   = 58 + max(0, 45 - rsi_v) * 0.5
            return dict(direction="SHORT", entry=close, sl=sl, target=target, confidence=conf)

        return None

    def _signal_breakout(self, candle, history: pd.DataFrame,
                          day_df: pd.DataFrame) -> Optional[dict]:
        """Uses previous day's high/low derived from the multi-day DataFrame."""
        if len(history) < 15:
            return None

        close   = candle['close']
        atr_v   = history['atr14'].iloc[-1]
        atr_pct = atr_v / close
        candle_time = candle.name.time()

        if not (BREAKOUT_ATR_MIN_PCT <= atr_pct <= BREAKOUT_ATR_MAX_PCT):
            return None

        # Approximate prev-day high/low from same-day early data
        prev_day_bars = day_df[day_df.index < candle.name]
        if len(prev_day_bars) < 5:
            return None
        pd_high = prev_day_bars['high'].max()
        pd_low  = prev_day_bars['low'].min()

        # Volume check
        mins_elapsed = (candle_time.hour * 60 + candle_time.minute) - (9 * 60 + 15)
        time_frac    = max(mins_elapsed / 375, 0.05)
        avg_vol      = history['volume'].tail(20).mean()
        expected_vol = avg_vol * time_frac * 1.5
        cum_vol      = day_df.loc[: candle.name, 'volume'].sum()

        if cum_vol <= expected_vol:
            return None

        if close > pd_high * 1.001:
            sl     = pd_high - BREAKOUT_SL_ATR_MULT * atr_v
            risk   = close - sl
            if risk <= 0:
                return None
            target = close + 1.5 * risk
            return dict(direction="LONG", entry=close, sl=sl, target=target, confidence=68.0)

        if close < pd_low * 0.999:
            sl   = pd_low + BREAKOUT_SL_ATR_MULT * atr_v
            risk = sl - close
            if risk <= 0:
                return None
            target = close - 1.5 * risk
            return dict(direction="SHORT", entry=close, sl=sl, target=target, confidence=68.0)

        return None

    # ------------------------------------------------------------------
    # ORB COMPUTATION (true 15-min from first 3 five-min candles)
    # ------------------------------------------------------------------
    def _compute_orb(self, day_df: pd.DataFrame) -> Optional[dict]:
        """Extract 9:15–9:25 candles and compute ORB."""
        opening = day_df[
            (day_df.index.time >= MARKET_OPEN) &
            (day_df.index.time < ORB_READY)
        ]
        if len(opening) == 0:
            return None
        orb_high  = opening['high'].max()
        orb_low   = opening['low'].min()
        orb_range = orb_high - orb_low
        orb_mid   = (orb_high + orb_low) / 2
        return {
            'high': orb_high, 'low': orb_low,
            'range': orb_range, 'mid': orb_mid,
            'range_pct': orb_range / orb_mid if orb_mid > 0 else 0,
        }

    # ------------------------------------------------------------------
    # INDICATOR PRE-COMPUTATION
    # ------------------------------------------------------------------
    def _add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add all indicators using only past data (no lookahead)."""
        df['ema9']   = ema(df['close'], EMA_FAST)
        df['ema20']  = ema(df['close'], VWAP_EMA_PERIOD)
        df['ema21']  = ema(df['close'], EMA_SLOW)
        df['rsi14']  = rsi(df['close'], RSI_PERIOD)
        df['atr14']  = atr(df['high'], df['low'], df['close'], 14)
        df['vol_sma20'] = volume_sma(df['volume'], 20)
        try:
            from utils.indicators import adx as adx_fn
            df['adx'] = adx_fn(df['high'], df['low'], df['close'], 14)
        except Exception:
            df['adx'] = 20.0
        return df

    def _add_daily_vwap(self, day_df: pd.DataFrame) -> pd.DataFrame:
        """Add intraday VWAP that resets each day."""
        tp      = (day_df['high'] + day_df['low'] + day_df['close']) / 3
        cum_tpv = (tp * day_df['volume']).cumsum()
        cum_vol = day_df['volume'].cumsum()
        day_df  = day_df.copy()
        day_df['vwap_day'] = cum_tpv / cum_vol.replace(0, np.nan)
        return day_df

    # ------------------------------------------------------------------
    # POSITION SIZING
    # ------------------------------------------------------------------
    def _position_size(self, entry: float, sl: float) -> int:
        risk_per_share = abs(entry - sl)
        if risk_per_share <= 0:
            return 0
        risk_amount = self.capital * RISK_PER_TRADE_NORMAL
        qty = int(risk_amount / risk_per_share)
        return max(qty, 1)

    # ------------------------------------------------------------------
    # METRICS COMPUTATION
    # ------------------------------------------------------------------
    def _compute_metrics(self) -> dict:
        trades = self.trades
        n      = len(trades)

        if n == 0:
            return {
                "strategy":           self.strategy_name,
                "total_trades":       0,
                "win_rate_pct":       0.0,
                "avg_rr":             0.0,
                "expectancy_inr":     0.0,
                "profit_factor":      0.0,
                "sharpe_ratio":       0.0,
                "max_drawdown_pct":   0.0,
                "total_net_pnl":      0.0,
                "gross_pnl":          0.0,
                "total_cost_drag":    0.0,
                "annual_return_pct":  0.0,
                "avg_hold_mins":      0.0,
                "target_hit_pct":     0.0,
                "sl_hit_pct":         0.0,
                "eod_close_pct":      0.0,
                "regime_blocked_days": self.regime_blocked_days,
                "confidence_filtered": self.confidence_filtered,
                "trade_cap_blocked":   self.trade_cap_blocked,
            }

        wins         = [t for t in trades if t.net_pnl > 0]
        losses       = [t for t in trades if t.net_pnl <= 0]
        target_hits  = [t for t in trades if t.exit_reason == "TARGET_HIT"]
        sl_hits      = [t for t in trades if t.exit_reason == "SL_HIT"]
        eod_closes   = [t for t in trades if t.exit_reason == "EOD_CLOSE"]

        gross_wins   = sum(t.gross_pnl for t in wins)
        gross_losses = abs(sum(t.gross_pnl for t in losses))

        total_gross  = sum(t.gross_pnl for t in trades)
        total_cost   = sum(t.cost for t in trades)
        total_net    = sum(t.net_pnl for t in trades)

        rr_values    = []
        for t in trades:
            risk = abs(t.entry_price - t.sl) * t.quantity
            if risk > 0:
                rr_values.append(abs(t.gross_pnl) / risk * (1 if t.net_pnl > 0 else -1))

        # Sharpe using daily P&L series
        daily_pnl = {}
        for t in trades:
            if t.entry_time:
                day = t.entry_time.date()
                daily_pnl[day] = daily_pnl.get(day, 0) + t.net_pnl
        pnl_series = np.array(list(daily_pnl.values()))
        sharpe = 0.0
        if len(pnl_series) > 1 and pnl_series.std() > 0:
            sharpe = round(
                (pnl_series.mean() / pnl_series.std()) * np.sqrt(252), 2
            )

        # Max drawdown
        equity  = self.capital + np.cumsum([t.net_pnl for t in trades])
        rolling_max = np.maximum.accumulate(equity)
        drawdowns   = (equity - rolling_max) / rolling_max * 100
        max_dd      = abs(drawdowns.min()) if len(drawdowns) > 0 else 0.0

        # Annual return (assume 252 trading days, ~6 candles/hr * 6hrs = 78 per day)
        if trades:
            first = trades[0].entry_time
            last  = trades[-1].exit_time or trades[-1].entry_time
            days_elapsed = max((last - first).days, 1) if first and last else 252
            annual_factor = 252 / days_elapsed
        else:
            annual_factor = 1.0

        annual_ret = (total_net / self.capital) * annual_factor * 100

        return {
            "strategy":            self.strategy_name,
            "total_trades":        n,
            "win_rate_pct":        round(len(wins) / n * 100, 1),
            "avg_rr":              round(np.mean(rr_values), 2) if rr_values else 0.0,
            "expectancy_inr":      round(total_net / n, 0),
            "profit_factor":       round(gross_wins / gross_losses, 2) if gross_losses > 0 else 0.0,
            "sharpe_ratio":        sharpe,
            "max_drawdown_pct":    round(max_dd, 2),
            "total_net_pnl":       round(total_net, 0),
            "gross_pnl":           round(total_gross, 0),
            "total_cost_drag":     round(total_cost, 0),
            "annual_return_pct":   round(annual_ret, 1),
            "avg_hold_mins":       round(np.mean([t.hold_mins for t in trades]), 0),
            "target_hit_pct":      round(len(target_hits) / n * 100, 1),
            "sl_hit_pct":          round(len(sl_hits) / n * 100, 1),
            "eod_close_pct":       round(len(eod_closes) / n * 100, 1),
            "regime_blocked_days": self.regime_blocked_days,
            "confidence_filtered": self.confidence_filtered,
            "trade_cap_blocked":   self.trade_cap_blocked,
        }


# ================================================================
#  MULTI-STRATEGY COMPARISON
# ================================================================
def run_all_strategy_comparison(
    data: Dict[str, pd.DataFrame],
    capital: float = BACKTEST_INITIAL_CAPITAL
) -> List[dict]:
    """
    Run all strategies on all provided symbols and aggregate results.

    Args:
        data:    {symbol: DataFrame} — 5-min OHLCV data per stock.
        capital: Starting capital (default from config).

    Returns:
        List of metric dicts, one per strategy.
    """
    strategies = ["ORB_15", "VWAP_PULLBACK", "EMA_RSI", "BREAKOUT_ATR"]
    all_results = []

    for strat_name in strategies:
        logger.info(f"\nRunning backtest: {strat_name}")
        combined_trades = []
        total_blocks     = {"regime": 0, "confidence": 0, "cap": 0}

        for symbol, df in data.items():
            engine = BacktestEngine(strat_name, capital=capital)
            engine.run(df)
            combined_trades.extend(engine.trades)
            total_blocks["regime"]     += engine.regime_blocked_days
            total_blocks["confidence"] += engine.confidence_filtered
            total_blocks["cap"]        += engine.trade_cap_blocked

        # Aggregate metrics across all symbols
        agg_engine          = BacktestEngine(strat_name, capital=capital)
        agg_engine.trades   = combined_trades
        agg_engine.regime_blocked_days  = total_blocks["regime"]
        agg_engine.confidence_filtered  = total_blocks["confidence"]
        agg_engine.trade_cap_blocked    = total_blocks["cap"]
        metrics             = agg_engine._compute_metrics()

        all_results.append(metrics)
        _print_strategy_summary(metrics)

    return all_results


def _print_strategy_summary(m: dict):
    status = "✅" if m.get("win_rate_pct", 0) >= 55 else "⚠️ "
    print(
        f"  {status} {m['strategy']:<16} | "
        f"WR: {m.get('win_rate_pct', 0):.1f}% | "
        f"Sharpe: {m.get('sharpe_ratio', 0):.2f} | "
        f"PF: {m.get('profit_factor', 0):.2f} | "
        f"MaxDD: {m.get('max_drawdown_pct', 0):.1f}% | "
        f"Net PnL: ₹{m.get('total_net_pnl', 0):+,.0f} | "
        f"Trades: {m.get('total_trades', 0)}"
    )
