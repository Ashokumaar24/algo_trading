# ============================================================
#  backtest/fetch_and_backtest_v2.py
#  Fixed Real Data Backtest — V2
#  Applies all 6 fixes from analysis report
#
#  Run: python backtest/fetch_and_backtest_v2.py
# ============================================================

import os
import sys
import pandas as pd
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth.login import get_kite_session
from backtest.backtest_engine_v2 import BacktestEngineV2, compare_v1_vs_v2
from utils.logger import get_logger

logger = get_logger("real_backtest_v2")

SYMBOLS_TO_TEST = ["RELIANCE", "HDFCBANK", "INFY", "AXISBANK"]
LOOKBACK_DAYS   = 180
INTERVAL        = "5minute"
STRATEGIES      = ['ORB_15', 'VWAP_PULLBACK', 'BREAKOUT_ATR']


def fetch_real_data(kite, symbol: str, days: int):
    print(f"  Fetching {symbol}...", end=" ")
    try:
        instruments = kite.instruments("NSE")
        inst_df     = pd.DataFrame(instruments)
        row         = inst_df[inst_df["tradingsymbol"] == symbol]
        if row.empty:
            print("NOT FOUND")
            return None

        token     = int(row.iloc[0]["instrument_token"])
        to_date   = datetime.now()
        from_date = to_date - timedelta(days=days)
        all_data  = []

        chunk_end   = to_date
        chunk_start = max(from_date, chunk_end - timedelta(days=60))

        while chunk_end > from_date:
            data = kite.historical_data(token, chunk_start, chunk_end, INTERVAL)
            if data:
                all_data.extend(data)
            chunk_end   = chunk_start - timedelta(minutes=5)
            chunk_start = max(from_date, chunk_end - timedelta(days=60))
            if chunk_end <= from_date:
                break

        if not all_data:
            print("NO DATA")
            return None

        df = pd.DataFrame(all_data)
        df.rename(columns={"date": "timestamp"}, inplace=True)
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True)
        df = df[~df.index.duplicated(keep="first")]

        print(f"{len(df)} candles ({df.index[0].date()} → {df.index[-1].date()})")
        return df

    except Exception as e:
        print(f"ERROR: {e}")
        return None


def save_results(all_metrics: list, tag: str = "v2"):
    logs_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"
    )
    os.makedirs(logs_dir, exist_ok=True)
    path = os.path.join(
        logs_dir,
        f"backtest_{tag}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    )
    rows = [m for m in all_metrics if "error" not in m]
    if rows:
        pd.DataFrame(rows).to_csv(path, index=False)
        print(f"\n  ✅ Results saved: {path}")


def print_v2_summary(all_metrics: list, symbols: list):
    valid = [m for m in all_metrics if "error" not in m]
    if not valid:
        print("  No valid results.")
        return

    print("\n" + "=" * 70)
    print("  FIXED BACKTEST (V2) — RESULTS SUMMARY")
    print(f"  Symbols: {', '.join(symbols)} | Period: {LOOKBACK_DAYS} days")
    print(f"  Fixes: 2-trade cap | Regime filter | Min confidence 65")
    print(f"         ORB target 1.2x | ORB exit 12:30 PM")
    print("=" * 70)

    print(f"\n  {'Strategy':<18} {'Trades':>7} {'WinRate':>9} {'PF':>7} "
          f"{'Sharpe':>8} {'MaxDD%':>8} {'NetPnL':>12} {'GrossPnL':>12}")
    print("  " + "-" * 66)

    for m in valid:
        wicon = "✅" if m.get('win_rate_pct', 0) >= 52 else "⚠️ "
        print(f"  {wicon}{m['strategy']:<16} "
              f"{m.get('total_trades', 0):>7} "
              f"{m.get('win_rate_pct', 0):>8.1f}% "
              f"{m.get('profit_factor', 0):>7.2f} "
              f"{m.get('sharpe_ratio', 0):>8.2f} "
              f"{m.get('max_drawdown_pct', 0):>7.1f}% "
              f"₹{m.get('total_net_pnl', 0):>10,.0f} "
              f"₹{m.get('gross_pnl', 0):>10,.0f}")

    print()

    # Regime filter impact
    print("  Regime filter impact:")
    for m in valid:
        blocked = m.get('regime_blocked_days', 0)
        conf_f  = m.get('confidence_filtered', 0)
        cap_b   = m.get('trade_cap_blocked', 0)
        print(f"    {m['strategy']:<20}: "
              f"Regime blocked {blocked} days | "
              f"Conf filtered {conf_f} signals | "
              f"Cap blocked {cap_b} signals")

    # Readiness
    print("\n  📋 LIVE READINESS (V2):")
    for m in valid:
        wr  = m.get('win_rate_pct', 0)
        pf  = m.get('profit_factor', 0)
        dd  = m.get('max_drawdown_pct', 0)
        sr  = m.get('sharpe_ratio', 0)
        net = m.get('total_net_pnl', 0)
        ready = wr >= 52 and pf >= 1.1 and dd <= 15 and net > 0
        print(f"    {m['strategy']:<20}: {'✅ CONSIDER PAPER TRADE' if ready else '❌ NEEDS MORE TUNING'}")

    print("=" * 70)


def main():
    print("=" * 70)
    print("  FIXED BACKTEST V2 — REAL HISTORICAL DATA")
    print("=" * 70)

    # Login
    print("\n[1/4] Logging in...")
    kite = get_kite_session(headless=True)
    print("      ✅ Login OK")

    # Fetch data
    print(f"\n[2/4] Fetching {LOOKBACK_DAYS} days of 5-min real data...")
    data = {}
    for symbol in SYMBOLS_TO_TEST:
        df = fetch_real_data(kite, symbol, LOOKBACK_DAYS)
        if df is not None and len(df) >= 100:
            data[f"NSE:{symbol}"] = df

    if not data:
        print("❌ No data fetched.")
        return
    print(f"      ✅ {len(data)} symbols loaded")

    # V1 vs V2 comparison
    print("\n[3/4] Running V1 vs V2 comparison...")
    compare_v1_vs_v2(data)

    # Full V2 backtest
    print("\n[4/4] Running full fixed backtest (V2)...")
    print("-" * 70)
    all_metrics = []

    for strat in STRATEGIES:
        engine  = BacktestEngineV2(strat)
        metrics = engine.run(data)
        engine.print_report(metrics)
        all_metrics.append(metrics)

    # Summary
    print_v2_summary(all_metrics, list(data.keys()))

    # Save
    save_results(all_metrics, tag="v2_fixed")


if __name__ == "__main__":
    main()
