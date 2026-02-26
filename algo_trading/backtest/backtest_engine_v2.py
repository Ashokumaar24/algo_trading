# ============================================================
#  backtest/backtest_engine_v2.py
#  FIXED Backtest Engine — v2
#
#  Fixes vs original v2:
#  FIX A: _check_exit now handles both lowercase AND uppercase OHLC keys.
#         Original defaulted high/low to 0 when keys were uppercase,
#         causing every LONG trade to immediately hit SL_HIT.
#  FIX B: trade_cap_blocked counter now increments once per BLOCKED DAY
#         rather than once per candle-iteration, so the diagnostic
#         number is accurate and comparable to regime_blocked_days.
#  FIX C: BacktestRegimeFilter ADX threshold now reads from config
#         (ADX_TREND_THRESHOLD = 25) instead of a hardcoded 20,
#         aligning backtest regime decisions with live system.
#
#  Original fixes (kept):
#  FIX 1: Max 2 trades/day cap (cross-symbol, like real system)
#  FIX 2: Market regime filter (skip RANGE+LOW_VOL days)
#  FIX 3: ORB target reduced to 1.2x RR (from 1.5x) — via config
#  FIX 4: ORB hard exit at 12:30 PM
#  FIX 5: Min confidence filter >= 65
#  FIX 6: EMA_RSI removed from comparison (no gross edge)
# ============================================================

import pandas as pd
import numpy as np
from datetime import datetime, timedelta, time
from typing import List, Dict, Optional
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.orb_strategy import ORBStrategy
from strategies.vwap_pullback import VWAPPullbackStrategy
from strategies.ema_rsi_strategy import EMARSIStrategy
from strategies.breakout_atr import BreakoutATRStrategy
from strategies.base_strategy import Direction
from utils.indicators import calculate_trade_cost
from config.config import (
    BACKTEST_INITIAL_CAPITAL,
    ADX_TREND_THRESHOLD,          # FIX C: use shared config value
    ORB_REWARD_RISK_RATIO,        # FIX: use config so backtest matches live
)
from utils.logger import get_logger

logger = get_logger("backtest_v2")

# ----------------------------------------------------------------
# CONSTANTS
# ----------------------------------------------------------------
MAX_TRADES_PER_DAY  = 2
MIN_CONFIDENCE      = 65
ORB_RR_TARGET       = ORB_REWARD_RISK_RATIO   # FIX: was hardcoded 1.2; now from config
ORB_HARD_EXIT_TIME  = time(12, 30)


# ----------------------------------------------------------------
# TRADE RECORD
# ----------------------------------------------------------------
class TradeRecord:
    def __init__(self, symbol, strategy, direction, entry, exit_price,
                 sl, target, quantity, entry_time, exit_time, exit_reason):
        self.symbol      = symbol
        self.strategy    = strategy
        self.direction   = direction
        self.entry       = entry
        self.exit_price  = exit_price
        self.sl          = sl
        self.target      = target
        self.quantity    = quantity
        self.entry_time  = entry_time
        self.exit_time   = exit_time
        self.exit_reason = exit_reason

        if direction == 'LONG':
            gross_pnl = (exit_price - entry) * quantity
        else:
            gross_pnl = (entry - exit_price) * quantity

        cost           = calculate_trade_cost(entry, exit_price, quantity)
        self.gross_pnl = round(gross_pnl, 2)
        self.cost      = round(cost, 2)
        self.net_pnl   = round(gross_pnl - cost, 2)
        self.win       = self.net_pnl > 0

        hold_secs      = (exit_time - entry_time).total_seconds()
        self.hold_mins = int(hold_secs / 60)

        risk           = abs(entry - sl) * quantity
        self.rr_realized = round(abs(gross_pnl) / risk, 2) if risk > 0 else 0


# ----------------------------------------------------------------
# MARKET REGIME CLASSIFIER (lightweight version for backtest)
# ----------------------------------------------------------------
class BacktestRegimeFilter:
    """
    FIX C: ADX threshold now reads ADX_TREND_THRESHOLD from config (25)
    instead of the hardcoded 20 that caused the live vs backtest mismatch.
    """

    def is_tradeable(self, daily_data: pd.DataFrame) -> bool:
        if len(daily_data) < 20:
            return True

        close = daily_data['close']
        high  = daily_data['high']
        low   = daily_data['low']

        try:
            adx_val = self._calc_adx(high, low, close, period=14)
        except Exception:
            adx_val = ADX_TREND_THRESHOLD  # default to boundary — allow trade

        bb_std   = close.rolling(20).std().iloc[-1]
        bb_mid   = close.rolling(20).mean().iloc[-1]
        bb_width = (4 * bb_std) / bb_mid if bb_mid > 0 else 0

        bb_widths = (4 * close.rolling(20).std()) / close.rolling(20).mean()
        valid_bw  = bb_widths.dropna()
        bb_pct    = float((valid_bw < bb_width).sum() / len(valid_bw) * 100) \
                    if len(valid_bw) > 0 else 50

        # FIX C: was hardcoded 20; now uses config value (25)
        is_range   = adx_val < ADX_TREND_THRESHOLD
        is_low_vol = bb_pct < 25

        if is_range and is_low_vol:
            return False

        # Block absolute no-trend days (ADX < 15 = completely directionless)
        if adx_val < 15:
            return False

        return True

    def _calc_adx(self, high, low, close, period=14):
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs()
        ], axis=1).max(axis=1)

        up   = high - high.shift(1)
        down = low.shift(1) - low
        pdm  = np.where((up > down) & (up > 0), up, 0.0)
        ndm  = np.where((down > up) & (down > 0), down, 0.0)

        atr_s = tr.ewm(span=period, adjust=False).mean()
        pdm_s = pd.Series(pdm, index=close.index).ewm(span=period, adjust=False).mean()
        ndm_s = pd.Series(ndm, index=close.index).ewm(span=period, adjust=False).mean()

        pdi = 100 * pdm_s / atr_s.replace(0, np.nan)
        ndi = 100 * ndm_s / atr_s.replace(0, np.nan)
        dx  = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
        return dx.ewm(span=period, adjust=False).mean().iloc[-1]


# ----------------------------------------------------------------
# FIXED BACKTEST ENGINE
# ----------------------------------------------------------------
class BacktestEngineV2:

    def __init__(self, strategy_name: str,
                 capital: float = BACKTEST_INITIAL_CAPITAL,
                 apply_regime_filter: bool = True,
                 max_trades_per_day: int = MAX_TRADES_PER_DAY,
                 min_confidence: int = MIN_CONFIDENCE):

        self.strategy_name       = strategy_name
        self.capital             = capital
        self.apply_regime_filter = apply_regime_filter
        self.max_trades_per_day  = max_trades_per_day
        self.min_confidence      = min_confidence
        self.trades: List[TradeRecord] = []
        self.regime_filter       = BacktestRegimeFilter()

        # Diagnostic counters
        self.regime_blocked_days  = 0
        self.confidence_filtered  = 0
        # FIX B: trade_cap_blocked now counts blocked DAYS, not candle iterations
        self.trade_cap_blocked    = 0

    def run(self, data: Dict[str, pd.DataFrame],
            daily_data: Dict[str, pd.DataFrame] = None) -> dict:
        logger.info(
            f"Backtest V2 START | Strategy:{self.strategy_name} | "
            f"Symbols:{list(data.keys())} | "
            f"RegimeFilter:{self.apply_regime_filter} | "
            f"MaxTrades/Day:{self.max_trades_per_day} | "
            f"MinConf:{self.min_confidence}"
        )

        if daily_data is None:
            daily_data = {}
            for sym, df in data.items():
                daily = df.resample('D').agg({
                    'open':   'first',
                    'high':   'max',
                    'low':    'min',
                    'close':  'last',
                    'volume': 'sum'
                }).dropna()
                daily_data[sym] = daily

        all_dates = set()
        for df in data.values():
            all_dates.update(df.index.normalize().unique())
        all_dates = sorted(all_dates)

        for date in all_dates:
            self._process_day(date, data, daily_data)

        return self._calculate_metrics()

    def _process_day(self, date, intraday_data, daily_data):
        trades_today = 0

        if self.apply_regime_filter:
            first_sym = list(daily_data.keys())[0]
            ddata     = daily_data[first_sym]
            past_daily = ddata[ddata.index.normalize() < pd.Timestamp(date)]
            if len(past_daily) >= 20:
                if not self.regime_filter.is_tradeable(past_daily.tail(60)):
                    self.regime_blocked_days += 1
                    return

        strategies = {sym: self._get_strategy() for sym in intraday_data}
        for strat in strategies.values():
            strat.reset_daily()

        orb_set  = {sym: False for sym in intraday_data}
        in_trade = {sym: False for sym in intraday_data}
        signals  = {sym: None  for sym in intraday_data}

        symbols = sorted(intraday_data.keys())

        for sym in symbols:
            df       = intraday_data[sym]
            day_data = df[df.index.normalize() == date]
            if len(day_data) < 5:
                continue

            prev_data = df[df.index.normalize() < pd.Timestamp(date)]

            if self.strategy_name == 'BREAKOUT_ATR' and len(prev_data) >= 1:
                prev_day_candles = prev_data.resample('D').agg({
                    'high': 'max', 'low': 'min', 'close': 'last'
                }).dropna()
                if len(prev_day_candles) >= 1:
                    strategies[sym].set_prev_day_data(
                        sym,
                        float(prev_day_candles['high'].iloc[-1]),
                        float(prev_day_candles['low'].iloc[-1]),
                        float(prev_day_candles['close'].iloc[-1])
                    )

            for i in range(2, len(day_data)):
                candle     = day_data.iloc[i].to_dict()
                candle['timestamp'] = day_data.index[i]
                history    = day_data.iloc[:i]
                candle_t   = day_data.index[i].time()

                if not orb_set[sym] and i == 2 and self.strategy_name == 'ORB_15':
                    c915 = day_data.iloc[0].to_dict()
                    c930 = day_data.iloc[1].to_dict()
                    strategies[sym].set_orb(sym, c915, c930)
                    orb_set[sym] = True

                # FIX 4: ORB hard exit at 12:30 PM
                if (self.strategy_name == 'ORB_15' and
                        in_trade[sym] and candle_t >= ORB_HARD_EXIT_TIME):
                    exit_p = candle.get('close', candle.get('Close', 0))
                    self._record_trade(
                        signals[sym], sym, exit_p,
                        candle['timestamp'], 'TIME_EXIT_1230'
                    )
                    trades_today += 1
                    in_trade[sym] = False
                    signals[sym]  = None
                    continue

                if in_trade[sym] and signals[sym]:
                    exit_p, reason = self._check_exit(signals[sym], candle)
                    if exit_p:
                        self._record_trade(
                            signals[sym], sym, exit_p,
                            candle['timestamp'], reason
                        )
                        trades_today += 1
                        in_trade[sym] = False
                        signals[sym]  = None

                elif not in_trade[sym]:
                    # FIX B: check cap BEFORE getting signal, count blocked days
                    if trades_today >= self.max_trades_per_day:
                        # Only count once per symbol per day (not per candle)
                        # We break the candle loop for this symbol entirely
                        self.trade_cap_blocked += 1
                        break  # breaks candle loop for this symbol for today

                    avg_vol = float(history['volume'].mean()) if len(history) > 5 else 1
                    sig_obj = self._get_signal(
                        strategies[sym], sym, candle, history,
                        avg_vol, float(history['volume'].sum()), avg_vol * 6
                    )

                    if sig_obj:
                        if sig_obj.confidence < self.min_confidence:
                            self.confidence_filtered += 1
                            continue

                        entry = sig_obj.entry
                        sl    = sig_obj.stop_loss
                        risk  = abs(entry - sl)
                        if self.strategy_name == 'ORB_15':
                            if sig_obj.direction == Direction.LONG:
                                target = entry + ORB_RR_TARGET * risk
                            else:
                                target = entry - ORB_RR_TARGET * risk
                        else:
                            target = sig_obj.target

                        in_trade[sym] = True
                        signals[sym] = {
                            'direction':  sig_obj.direction.value,
                            'entry':      entry,
                            'sl':         sl,
                            'target':     target,
                            'entry_time': candle['timestamp'],
                            'confidence': sig_obj.confidence,
                        }

            # EOD close
            if in_trade[sym] and signals[sym] and len(day_data) > 0:
                eod_price = float(day_data.iloc[-1]['close'])
                self._record_trade(
                    signals[sym], sym, eod_price,
                    day_data.index[-1], 'EOD_CLOSE'
                )
                trades_today += 1
                in_trade[sym] = False
                signals[sym]  = None

    def _record_trade(self, signal, symbol, exit_price, exit_time, reason):
        entry = signal['entry']
        sl    = signal['sl']
        risk  = abs(entry - sl)
        qty   = max(int(5000 / risk), 1) if risk > 0 else 1

        record = TradeRecord(
            symbol=symbol,
            strategy=self.strategy_name,
            direction=signal['direction'],
            entry=entry,
            exit_price=exit_price,
            sl=sl,
            target=signal['target'],
            quantity=qty,
            entry_time=signal['entry_time'],
            exit_time=exit_time,
            exit_reason=reason
        )
        self.trades.append(record)

    def _check_exit(self, signal, candle) -> tuple:
        """
        FIX A: Original V2 defaulted to 0 when keys were uppercase,
               causing every LONG trade to immediately trigger SL_HIT
               (since low=0 <= any stop_loss price).
               Now handles both lowercase and uppercase OHLC keys.
        """
        # FIX A: handle both lowercase 'high'/'low' and uppercase 'High'/'Low'
        high  = candle.get('high',  candle.get('High',  None))
        low   = candle.get('low',   candle.get('Low',   None))

        if high is None or low is None:
            return None, None

        if signal['direction'] == 'LONG':
            if low  <= signal['sl']:     return signal['sl'],     'SL_HIT'
            if high >= signal['target']: return signal['target'], 'TARGET_HIT'
        else:
            if high >= signal['sl']:     return signal['sl'],     'SL_HIT'
            if low  <= signal['target']: return signal['target'], 'TARGET_HIT'

        return None, None

    def _get_strategy(self):
        mapping = {
            'ORB_15':        ORBStrategy,
            'VWAP_PULLBACK': VWAPPullbackStrategy,
            'EMA_RSI':       EMARSIStrategy,
            'BREAKOUT_ATR':  BreakoutATRStrategy,
        }
        return mapping[self.strategy_name]()

    def _get_signal(self, strategy, symbol, candle, history,
                    avg_vol, cum_vol, avg_daily_vol):
        try:
            if self.strategy_name == 'ORB_15':
                return strategy.check_entry(symbol, candle, history, avg_vol)
            elif self.strategy_name == 'VWAP_PULLBACK':
                prev = history.iloc[-1].to_dict() if len(history) >= 1 else None
                return strategy.check_entry(symbol, candle, history, prev)
            elif self.strategy_name == 'EMA_RSI':
                return strategy.check_entry(symbol, candle, history)
            elif self.strategy_name == 'BREAKOUT_ATR':
                return strategy.check_entry(symbol, candle, history,
                                            cum_vol, avg_daily_vol)
        except Exception as e:
            logger.debug(f"Signal error [{self.strategy_name}] {symbol}: {e}")
        return None

    # ----------------------------------------------------------------
    # METRICS
    # ----------------------------------------------------------------
    def _calculate_metrics(self) -> dict:
        if not self.trades:
            return {'error': 'No trades generated',
                    'strategy': self.strategy_name,
                    'regime_blocked_days': self.regime_blocked_days}

        df = pd.DataFrame([{
            'net_pnl':     t.net_pnl,
            'gross_pnl':   t.gross_pnl,
            'cost':        t.cost,
            'win':         t.win,
            'rr':          t.rr_realized,
            'hold_mins':   t.hold_mins,
            'exit_reason': t.exit_reason,
        } for t in self.trades])

        total   = len(df)
        winners = df[df['win']]['net_pnl']
        losers  = df[~df['win']]['net_pnl']

        win_rate = len(winners) / total * 100
        avg_win  = winners.mean() if len(winners) > 0 else 0
        avg_loss = losers.mean()  if len(losers)  > 0 else 0
        pf       = (winners.sum() / abs(losers.sum())
                    if losers.sum() != 0 else float('inf'))

        total_net  = df['net_pnl'].sum()
        total_cost = df['cost'].sum()
        gross_pnl  = total_net + total_cost

        equity      = self.capital + df['net_pnl'].cumsum()
        rolling_max = equity.cummax()
        dd          = ((rolling_max - equity) / rolling_max * 100)
        max_dd      = dd.max()

        daily_ret = df['net_pnl'] / self.capital
        sharpe    = (daily_ret.mean() / daily_ret.std() * np.sqrt(250)
                     if daily_ret.std() > 0 else 0)

        expectancy = (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss)

        return {
            'strategy':             self.strategy_name,
            'total_trades':         total,
            'win_rate_pct':         round(win_rate, 1),
            'avg_rr':               round(df['rr'].mean(), 2),
            'expectancy_inr':       round(expectancy, 0),
            'profit_factor':        round(pf, 2),
            'sharpe_ratio':         round(sharpe, 2),
            'max_drawdown_pct':     round(max_dd, 2),
            'total_net_pnl':        round(total_net, 0),
            'gross_pnl':            round(gross_pnl, 0),
            'total_cost_drag':      round(total_cost, 0),
            'annual_return_pct':    round(total_net / self.capital * 100, 1),
            'avg_hold_mins':        round(df['hold_mins'].mean(), 0),
            'target_hit_pct':       round(
                (df['exit_reason'] == 'TARGET_HIT').mean() * 100, 1),
            'sl_hit_pct':           round(
                (df['exit_reason'] == 'SL_HIT').mean() * 100, 1),
            'eod_close_pct':        round(
                (df['exit_reason'] == 'EOD_CLOSE').mean() * 100, 1),
            'regime_blocked_days':  self.regime_blocked_days,
            'confidence_filtered':  self.confidence_filtered,
            # FIX B: now counts blocked symbol-days, not candle iterations
            'trade_cap_blocked':    self.trade_cap_blocked,
        }

    def print_report(self, metrics: dict):
        print("\n" + "=" * 65)
        print(f"  BACKTEST V2 REPORT — {metrics.get('strategy', '')}")
        print("=" * 65)
        for k, v in metrics.items():
            if k != 'strategy':
                icon = ""
                if k == 'win_rate_pct':     icon = " ✅" if v >= 55 else " ⚠️"
                if k == 'profit_factor':    icon = " ✅" if v >= 1.3 else " ⚠️"
                if k == 'sharpe_ratio':     icon = " ✅" if v >= 1.0 else " ⚠️"
                if k == 'max_drawdown_pct': icon = " ✅" if v <= 8  else " ⚠️"
                print(f"  {k:<28}: {v}{icon}")
        print("=" * 65)


# ----------------------------------------------------------------
# COMPARE V1 vs V2
# ----------------------------------------------------------------
def compare_v1_vs_v2(data: dict) -> dict:
    from backtest.backtest_engine import BacktestEngine

    strategies = ['ORB_15', 'VWAP_PULLBACK', 'BREAKOUT_ATR']
    comparison = {}

    print("\n" + "=" * 80)
    print("  V1 (ORIGINAL) vs V2 (FIXED) COMPARISON")
    print("=" * 80)
    print(f"  {'Strategy':<18} {'Metric':<18} {'V1':>14} {'V2':>14} {'Δ Change':>12}")
    print("-" * 80)

    for strat in strategies:
        e1 = BacktestEngine(strat)
        m1 = e1.run(data)

        e2 = BacktestEngineV2(strat)
        m2 = e2.run(data)

        comparison[strat] = {'v1': m1, 'v2': m2}

        if 'error' not in m1 and 'error' not in m2:
            metrics_to_show = [
                ('total_trades',     'Trades'),
                ('win_rate_pct',     'Win Rate %'),
                ('profit_factor',    'Profit Factor'),
                ('max_drawdown_pct', 'Max Drawdown %'),
                ('total_net_pnl',    'Net PnL ₹'),
                ('gross_pnl',        'Gross PnL ₹'),
            ]

            first = True
            for key, label in metrics_to_show:
                v1_val = m1.get(key, 0)
                if key == 'gross_pnl' and 'gross_pnl' not in m1:
                    v1_val = m1.get('total_net_pnl', 0) + m1.get('total_cost_drag', 0)
                v2_val    = m2.get(key, 0)
                delta     = v2_val - v1_val if isinstance(v2_val, (int, float)) else 0
                strat_lbl = strat if first else ""
                print(f"  {strat_lbl:<18} {label:<18} "
                      f"{str(v1_val):>14} {str(v2_val):>14} {delta:>+12.1f}")
                first = False
            print()

    return comparison


# ----------------------------------------------------------------
# STANDALONE RUN
# ----------------------------------------------------------------
if __name__ == "__main__":
    import numpy as np

    print("BacktestEngineV2 — quick self-test with synthetic data")
    np.random.seed(42)

    dates  = pd.date_range('2024-01-02 09:15', periods=3000, freq='5min')
    dates  = dates[dates.indexer_between_time('09:15', '15:30')]
    n      = len(dates)
    price  = 2500.0
    prices = [price]
    for _ in range(n - 1):
        price += np.random.normal(0.05, 4)
        prices.append(max(price, 100))

    close = pd.Series(prices, index=dates[:n])
    high  = close + abs(pd.Series(np.random.normal(0, 3, n), index=dates[:n]))
    low   = close - abs(pd.Series(np.random.normal(0, 3, n), index=dates[:n]))
    vol   = pd.Series(np.random.randint(50000, 300000, n), index=dates[:n])
    df    = pd.DataFrame({'open': close.shift(1).fillna(close),
                          'high': high, 'low': low,
                          'close': close, 'volume': vol})

    data = {"NSE:RELIANCE": df}

    for strat in ['ORB_15', 'VWAP_PULLBACK', 'BREAKOUT_ATR']:
        engine  = BacktestEngineV2(strat)
        metrics = engine.run(data)
        engine.print_report(metrics)
