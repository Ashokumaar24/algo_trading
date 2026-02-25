# ============================================================
#  backtest/backtest_engine.py
#  Zero-lookahead backtest engine with full cost model
#  Run: python backtest/backtest_engine.py
# ============================================================

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.orb_strategy import ORBStrategy
from strategies.vwap_pullback import VWAPPullbackStrategy
from strategies.ema_rsi_strategy import EMARSIStrategy
from strategies.breakout_atr import BreakoutATRStrategy
from strategies.base_strategy import Direction
from utils.indicators import calculate_trade_cost
from config.config import BACKTEST_INITIAL_CAPITAL, BACKTEST_STOCKS
from utils.logger import get_logger

logger = get_logger("backtest")


# ------------------------------------------------------------------
# TRADE RECORD
# ------------------------------------------------------------------
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

        # PnL
        if direction == 'LONG':
            gross_pnl = (exit_price - entry) * quantity
        else:
            gross_pnl = (entry - exit_price) * quantity

        cost           = calculate_trade_cost(entry, exit_price, quantity)
        self.gross_pnl = round(gross_pnl, 2)
        self.cost      = round(cost, 2)
        self.net_pnl   = round(gross_pnl - cost, 2)
        self.win       = self.net_pnl > 0

        hold_secs       = (exit_time - entry_time).total_seconds()
        self.hold_mins  = int(hold_secs / 60)

        risk = abs(entry - sl) * quantity
        self.rr_realized = round(abs(gross_pnl) / risk, 2) if risk > 0 else 0


# ------------------------------------------------------------------
# BACKTEST ENGINE
# ------------------------------------------------------------------
class BacktestEngine:
    """
    Event-driven backtest engine.
    Processes candles one at a time (no lookahead).
    """

    def __init__(self, strategy_name: str, capital: float = BACKTEST_INITIAL_CAPITAL):
        self.strategy_name = strategy_name
        self.capital       = capital
        self.equity        = capital
        self.trades: List[TradeRecord] = []

    def run(self, data: Dict[str, pd.DataFrame]) -> dict:
        """
        Run backtest over provided OHLCV data.

        Args:
            data: {symbol: DataFrame with columns open,high,low,close,volume
                            indexed by datetime}
        Returns:
            metrics dict
        """
        logger.info(f"Backtest START | Strategy:{self.strategy_name} | "
                    f"Symbols:{list(data.keys())}")

        for symbol, df in data.items():
            self._backtest_symbol(symbol, df)

        return self._calculate_metrics()

    def _backtest_symbol(self, symbol: str, df: pd.DataFrame):
        """Run the strategy bar-by-bar on a single symbol"""

        # Initialise strategy fresh for each symbol
        strategy = self._get_strategy()
        strategy.reset_daily()

        # Sort chronologically
        df = df.sort_index()
        dates = df.index.normalize().unique()

        for date in dates:
            day_data = df[df.index.normalize() == date]
            if len(day_data) < 10:
                continue

            strategy.reset_daily()
            prev_day = df[df.index.normalize() < date].tail(1)

            if self.strategy_name == 'BREAKOUT_ATR' and len(prev_day) >= 1:
                strategy.set_prev_day_data(
                    symbol,
                    float(prev_day['high'].iloc[-1]),
                    float(prev_day['low'].iloc[-1]),
                    float(prev_day['close'].iloc[-1])
                )

            orb_set = False
            in_trade = False
            signal   = None
            entry_candle_idx = None

            for i in range(2, len(day_data)):
                candle = day_data.iloc[i].to_dict()
                candle['timestamp'] = day_data.index[i]
                history = day_data.iloc[:i]

                # ORB setup at i=1 (9:30 candle close)
                if not orb_set and i == 2 and self.strategy_name == 'ORB_15':
                    c915 = day_data.iloc[0].to_dict()
                    c930 = day_data.iloc[1].to_dict()
                    strategy.set_orb(symbol, c915, c930)
                    orb_set = True

                if in_trade and signal:
                    # Check exit on this candle
                    exit_price, reason = self._check_exit(signal, candle)
                    if exit_price:
                        qty = max(int(5000 / abs(signal['entry'] - signal['sl'])), 1)

                        record = TradeRecord(
                            symbol=symbol,
                            strategy=self.strategy_name,
                            direction=signal['direction'],
                            entry=signal['entry'],
                            exit_price=exit_price,
                            sl=signal['sl'],
                            target=signal['target'],
                            quantity=qty,
                            entry_time=signal['entry_time'],
                            exit_time=candle['timestamp'],
                            exit_reason=reason
                        )
                        self.trades.append(record)
                        in_trade = False
                        signal   = None

                elif not in_trade:
                    avg_vol = float(history['volume'].mean()) if len(history) > 5 else 1
                    sig_obj = self._get_signal(
                        strategy, symbol, candle, history, avg_vol,
                        float(history['volume'].sum()), avg_vol * 6
                    )
                    if sig_obj:
                        in_trade = True
                        signal = {
                            'direction':  sig_obj.direction.value,
                            'entry':      sig_obj.entry,
                            'sl':         sig_obj.stop_loss,
                            'target':     sig_obj.target,
                            'entry_time': candle['timestamp'],
                        }

            # Force close at EOD if still in trade
            if in_trade and signal and len(day_data) > 0:
                eod_price = float(day_data.iloc[-1]['close'])
                qty = max(int(5000 / abs(signal['entry'] - signal['sl'])), 1)
                record = TradeRecord(
                    symbol=symbol,
                    strategy=self.strategy_name,
                    direction=signal['direction'],
                    entry=signal['entry'],
                    exit_price=eod_price,
                    sl=signal['sl'],
                    target=signal['target'],
                    quantity=qty,
                    entry_time=signal['entry_time'],
                    exit_time=day_data.index[-1],
                    exit_reason='EOD_CLOSE'
                )
                self.trades.append(record)

    def _check_exit(self, signal, candle) -> tuple:
        """Return (exit_price, reason) or (None, None)"""
        high  = candle.get('high', candle.get('High'))
        low   = candle.get('low',  candle.get('Low'))
        close = candle.get('close', candle.get('Close'))

        if signal['direction'] == 'LONG':
            if low <= signal['sl']:
                return signal['sl'], 'SL_HIT'
            if high >= signal['target']:
                return signal['target'], 'TARGET_HIT'
        else:
            if high >= signal['sl']:
                return signal['sl'], 'SL_HIT'
            if low <= signal['target']:
                return signal['target'], 'TARGET_HIT'

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
        if self.strategy_name == 'ORB_15':
            return strategy.check_entry(symbol, candle, history, avg_vol)
        elif self.strategy_name == 'VWAP_PULLBACK':
            prev = history.iloc[-1].to_dict() if len(history) >= 1 else None
            return strategy.check_entry(symbol, candle, history, prev)
        elif self.strategy_name == 'EMA_RSI':
            return strategy.check_entry(symbol, candle, history)
        elif self.strategy_name == 'BREAKOUT_ATR':
            return strategy.check_entry(symbol, candle, history, cum_vol, avg_daily_vol)
        return None

    # ------------------------------------------------------------------
    # METRICS
    # ------------------------------------------------------------------
    def _calculate_metrics(self) -> dict:
        if not self.trades:
            return {'error': 'No trades generated'}

        df = pd.DataFrame([{
            'net_pnl':     t.net_pnl,
            'gross_pnl':   t.gross_pnl,
            'cost':        t.cost,
            'win':         t.win,
            'rr':          t.rr_realized,
            'hold_mins':   t.hold_mins,
            'exit_reason': t.exit_reason,
            'strategy':    t.strategy,
        } for t in self.trades])

        total_trades  = len(df)
        winners       = df[df['win']]['net_pnl']
        losers        = df[~df['win']]['net_pnl']

        win_rate      = len(winners) / total_trades * 100
        avg_win       = winners.mean() if len(winners) > 0 else 0
        avg_loss      = losers.mean()  if len(losers)  > 0 else 0
        profit_factor = (winners.sum() / abs(losers.sum())
                         if losers.sum() != 0 else float('inf'))

        total_net     = df['net_pnl'].sum()
        total_cost    = df['cost'].sum()

        # Equity curve for drawdown
        equity_curve  = self.capital + df['net_pnl'].cumsum()
        rolling_max   = equity_curve.cummax()
        drawdown      = ((rolling_max - equity_curve) / rolling_max * 100)
        max_drawdown  = drawdown.max()

        # Sharpe ratio (annualised, assume 250 trading days)
        daily_returns = df['net_pnl'] / self.capital
        sharpe = (daily_returns.mean() / daily_returns.std() * np.sqrt(250)
                  if daily_returns.std() > 0 else 0)

        expectancy = (win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss)

        metrics = {
            'strategy':       self.strategy_name,
            'total_trades':   total_trades,
            'win_rate_pct':   round(win_rate, 1),
            'avg_rr':         round(df['rr'].mean(), 2),
            'expectancy_inr': round(expectancy, 0),
            'profit_factor':  round(profit_factor, 2),
            'sharpe_ratio':   round(sharpe, 2),
            'max_drawdown_pct': round(max_drawdown, 2),
            'total_net_pnl':  round(total_net, 0),
            'total_cost_drag': round(total_cost, 0),
            'annual_return_pct': round(total_net / self.capital * 100, 1),
            'avg_hold_mins':  round(df['hold_mins'].mean(), 0),
            'target_hit_pct': round((df['exit_reason'] == 'TARGET_HIT').mean() * 100, 1),
        }

        return metrics

    def print_report(self, metrics: dict):
        print("\n" + "=" * 60)
        print(f"  BACKTEST REPORT — {metrics.get('strategy', 'Unknown')}")
        print("=" * 60)
        for k, v in metrics.items():
            if k != 'strategy':
                print(f"  {k:<25}: {v}")
        print("=" * 60)


# ------------------------------------------------------------------
# COMPARE ALL STRATEGIES
# ------------------------------------------------------------------
def run_all_strategy_comparison(data: Dict[str, pd.DataFrame]):
    """Compare all 4 strategies on the same dataset"""
    strategies = ['ORB_15', 'VWAP_PULLBACK', 'EMA_RSI', 'BREAKOUT_ATR']
    all_metrics = []

    for name in strategies:
        engine  = BacktestEngine(name)
        metrics = engine.run(data)
        all_metrics.append(metrics)
        engine.print_report(metrics)

    # Summary table
    print("\n" + "=" * 90)
    print("  STRATEGY COMPARISON SUMMARY")
    print("=" * 90)
    headers = ['Strategy', 'WinRate%', 'Avg RR', 'Expectancy', 'MaxDD%', 'Sharpe', 'PF', 'Trades']
    print(f"{'Strategy':<20}{'WinRate':>9}{'AvgRR':>8}{'Expect':>12}"
          f"{'MaxDD%':>8}{'Sharpe':>8}{'PF':>8}{'Trades':>8}")
    print("-" * 90)
    for m in all_metrics:
        print(f"{m.get('strategy',''):<20}"
              f"{m.get('win_rate_pct',0):>8.1f}%"
              f"{m.get('avg_rr',0):>8.2f}"
              f"{m.get('expectancy_inr',0):>12.0f}"
              f"{m.get('max_drawdown_pct',0):>7.1f}%"
              f"{m.get('sharpe_ratio',0):>8.2f}"
              f"{m.get('profit_factor',0):>8.2f}"
              f"{m.get('total_trades',0):>8}")
    print("=" * 90)

    return all_metrics


# ------------------------------------------------------------------
# STANDALONE RUN
# ------------------------------------------------------------------
if __name__ == "__main__":
    """
    Quick demo backtest using synthetic data.
    Replace with real historical data fetched via KiteConnect.
    """
    print("Generating synthetic OHLCV data for demo backtest...")

    np.random.seed(42)
    n = 5000  # bars
    dates = pd.date_range('2023-01-01 09:15', periods=n, freq='5min')
    # Filter to market hours only
    dates = dates[dates.indexer_between_time('09:15', '15:30')][:n]

    price = 2500.0
    prices = [price]
    for _ in range(len(dates) - 1):
        price += np.random.normal(0, 5)
        price  = max(price, 100)
        prices.append(price)

    close  = pd.Series(prices, index=dates)
    high   = close + abs(pd.Series(np.random.normal(0, 3, len(dates)), index=dates))
    low    = close - abs(pd.Series(np.random.normal(0, 3, len(dates)), index=dates))
    open_  = close.shift(1).fillna(close)
    vol    = pd.Series(np.random.randint(100000, 500000, len(dates)), index=dates)

    demo_df = pd.DataFrame({'open': open_, 'high': high, 'low': low,
                             'close': close, 'volume': vol})

    data = {"NSE:RELIANCE": demo_df}

    run_all_strategy_comparison(data)
