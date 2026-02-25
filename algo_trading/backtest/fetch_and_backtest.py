# ============================================================
#  backtest/fetch_and_backtest.py
#  Real Historical Data Backtest using KiteConnect
#  Tests all 5 strategies on actual Nifty50 stock data
#
#  Run: python backtest/fetch_and_backtest.py
# ============================================================

import os
import sys
import pandas as pd
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth.login import get_kite_session
from backtest.backtest_engine import run_all_strategy_comparison
from utils.logger import get_logger

logger = get_logger("real_backtest")

# ----------------------------------------------------------------
# SETTINGS — edit these if needed
# ----------------------------------------------------------------
SYMBOLS_TO_TEST = [
    "RELIANCE",
    "HDFCBANK",
    "INFY",
    "TATAMOTORS",
    "AXISBANK",
]
LOOKBACK_DAYS = 180        # 6 months of 5-min data
INTERVAL      = "5minute"


# ----------------------------------------------------------------
# FETCH HISTORICAL DATA
# ----------------------------------------------------------------
def fetch_real_data(kite, symbol: str, days: int) -> pd.DataFrame:
    """Fetch real 5-min OHLCV data from KiteConnect"""

    print(f"  Fetching {symbol}...", end=" ")

    try:
        # Get instrument token
        instruments = kite.instruments("NSE")
        inst_df     = pd.DataFrame(instruments)
        row         = inst_df[inst_df["tradingsymbol"] == symbol]

        if row.empty:
            print(f"NOT FOUND")
            return None

        token     = int(row.iloc[0]["instrument_token"])
        to_date   = datetime.now()
        from_date = to_date - timedelta(days=days)

        # KiteConnect max 60 days per call for 5-min data
        # So we fetch in 60-day chunks
        all_data = []
        chunk_end   = to_date
        chunk_start = max(from_date, chunk_end - timedelta(days=60))

        while chunk_end > from_date:
            data = kite.historical_data(
                token, chunk_start, chunk_end, INTERVAL
            )
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

        # Remove duplicates
        df = df[~df.index.duplicated(keep="first")]

        print(f"{len(df)} candles ({df.index[0].date()} → {df.index[-1].date()})")
        return df

    except Exception as e:
        print(f"ERROR: {e}")
        return None


# ----------------------------------------------------------------
# SAVE RESULTS TO CSV
# ----------------------------------------------------------------
def save_results(all_metrics: list):
    """Save backtest results to CSV for later review"""
    results_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "logs"
    )
    os.makedirs(results_dir, exist_ok=True)

    output_file = os.path.join(
        results_dir,
        f"backtest_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    )

    rows = []
    for m in all_metrics:
        if "error" not in m:
            rows.append(m)

    if rows:
        pd.DataFrame(rows).to_csv(output_file, index=False)
        print(f"\n  Results saved to: {output_file}")


# ----------------------------------------------------------------
# PRINT DETAILED SUMMARY
# ----------------------------------------------------------------
def print_detailed_summary(all_metrics: list, symbols: list):
    print("\n")
    print("=" * 70)
    print("  REAL DATA BACKTEST — DETAILED ANALYSIS")
    print(f"  Period: Last {LOOKBACK_DAYS} days | Symbols: {', '.join(symbols)}")
    print(f"  Run at: {datetime.now().strftime('%d %b %Y %H:%M IST')}")
    print("=" * 70)

    valid = [m for m in all_metrics if "error" not in m]

    if not valid:
        print("  No valid results — check data fetch errors above.")
        return

    for m in valid:
        status = "✅" if m.get("win_rate_pct", 0) >= 55 else "⚠️ "
        print(f"\n  {status} {m['strategy']}")
        print(f"     Win Rate     : {m.get('win_rate_pct', 0):.1f}%"
              f"  {'GOOD ✓' if m.get('win_rate_pct', 0) >= 60 else 'NEEDS IMPROVEMENT'}")
        print(f"     Avg RR       : {m.get('avg_rr', 0):.2f}"
              f"  {'GOOD ✓' if m.get('avg_rr', 0) >= 1.3 else 'LOW'}")
        print(f"     Profit Factor: {m.get('profit_factor', 0):.2f}"
              f"  {'GOOD ✓' if m.get('profit_factor', 0) >= 1.5 else 'BELOW TARGET'}")
        print(f"     Sharpe Ratio : {m.get('sharpe_ratio', 0):.2f}"
              f"  {'GOOD ✓' if m.get('sharpe_ratio', 0) >= 1.0 else 'LOW'}")
        print(f"     Max Drawdown : {m.get('max_drawdown_pct', 0):.1f}%"
              f"  {'SAFE ✓' if m.get('max_drawdown_pct', 0) <= 5 else 'HIGH ⚠️'}")
        print(f"     Total Trades : {m.get('total_trades', 0)}")
        print(f"     Net PnL      : ₹{m.get('total_net_pnl', 0):+,.0f}")
        print(f"     Cost Drag    : ₹{m.get('total_cost_drag', 0):,.0f}")
        print(f"     Annual Ret   : {m.get('annual_return_pct', 0):.1f}%")

    # Best strategy
    best = max(valid, key=lambda x: x.get("sharpe_ratio", 0))
    print("\n" + "=" * 70)
    print(f"  🏆 BEST STRATEGY: {best['strategy']}")
    print(f"     Win Rate: {best.get('win_rate_pct', 0):.1f}% | "
          f"Sharpe: {best.get('sharpe_ratio', 0):.2f} | "
          f"PF: {best.get('profit_factor', 0):.2f}")
    print("=" * 70)

    # Readiness check
    print("\n  📋 LIVE TRADING READINESS:")
    for m in valid:
        wr  = m.get("win_rate_pct", 0)
        pf  = m.get("profit_factor", 0)
        dd  = m.get("max_drawdown_pct", 0)
        sr  = m.get("sharpe_ratio", 0)
        ready = wr >= 55 and pf >= 1.3 and dd <= 5 and sr >= 0.8
        icon  = "✅ READY" if ready else "❌ NOT READY"
        print(f"     {m['strategy']:<20}: {icon}")

    print()


# ----------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------
def main():
    print("=" * 70)
    print("  REAL HISTORICAL DATA BACKTEST")
    print("  Fetching data from Zerodha KiteConnect...")
    print("=" * 70)

    # Login
    print("\n[1/3] Logging in to Zerodha...")
    kite = get_kite_session(headless=True)
    print("      Login successful ✓")

    # Fetch data for all symbols
    print(f"\n[2/3] Fetching {LOOKBACK_DAYS} days of 5-min data...")
    data = {}
    for symbol in SYMBOLS_TO_TEST:
        df = fetch_real_data(kite, symbol, LOOKBACK_DAYS)
        if df is not None and len(df) >= 100:
            data[f"NSE:{symbol}"] = df

    if not data:
        print("\n❌ No data fetched. Check your internet connection and login.")
        return

    print(f"\n  Fetched data for {len(data)} symbols ✓")

    # Run backtest
    print(f"\n[3/3] Running strategy comparison on real data...")
    print("-" * 70)
    all_metrics = run_all_strategy_comparison(data)

    # Detailed summary
    print_detailed_summary(all_metrics, list(data.keys()))

    # Save to CSV
    save_results(all_metrics)


if __name__ == "__main__":
    main()
