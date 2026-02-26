# ============================================================
#  backtest/fetch_and_backtest_v3.py
#  Backtest V3 — 3 remaining fixes applied
#
#  FIX A: Real daily OHLCV data passed to regime filter
#  FIX B: VWAP confidence filter removed
#  FIX C: ORB minimum confidence raised to 80 (from 65)
#
#  Run: python backtest/fetch_and_backtest_v3.py
# ============================================================

import os
import sys
import pandas as pd
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth.login import get_kite_session
from backtest.backtest_engine_v2 import BacktestEngineV2, BacktestRegimeFilter
from utils.logger import get_logger

logger = get_logger("backtest_v3")

SYMBOLS_TO_TEST = ["RELIANCE", "HDFCBANK", "INFY", "AXISBANK"]
LOOKBACK_DAYS   = 180
STRATEGIES      = ['ORB_15', 'VWAP_PULLBACK']


# ----------------------------------------------------------------
# FETCH 5-MIN DATA
# ----------------------------------------------------------------
def fetch_5min(kite, symbol: str, days: int):
    print(f"  [5min] {symbol}...", end=" ")
    try:
        inst_df = pd.DataFrame(kite.instruments("NSE"))
        row     = inst_df[inst_df["tradingsymbol"] == symbol]
        if row.empty:
            print("NOT FOUND"); return None

        token     = int(row.iloc[0]["instrument_token"])
        to_date   = datetime.now()
        from_date = to_date - timedelta(days=days)
        all_data  = []

        chunk_end   = to_date
        chunk_start = max(from_date, chunk_end - timedelta(days=60))

        while chunk_end > from_date:
            data = kite.historical_data(token, chunk_start, chunk_end, "5minute")
            if data:
                all_data.extend(data)
            chunk_end   = chunk_start - timedelta(minutes=5)
            chunk_start = max(from_date, chunk_end - timedelta(days=60))
            if chunk_end <= from_date:
                break

        if not all_data:
            print("NO DATA"); return None

        df = pd.DataFrame(all_data)
        df.rename(columns={"date": "timestamp"}, inplace=True)
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True)
        df = df[~df.index.duplicated(keep="first")]
        print(f"{len(df)} candles ✓")
        return df

    except Exception as e:
        print(f"ERROR: {e}"); return None


# ----------------------------------------------------------------
# FIX A: Fetch REAL daily data (separate API call)
# ----------------------------------------------------------------
def fetch_daily(kite, symbol: str, days: int = 300):
    """
    Fetch actual daily OHLCV bars — NOT resampled from 5-min.
    KiteConnect daily data has correct open/high/low/close.
    """
    print(f"  [daily] {symbol}...", end=" ")
    try:
        inst_df = pd.DataFrame(kite.instruments("NSE"))
        row     = inst_df[inst_df["tradingsymbol"] == symbol]
        if row.empty:
            print("NOT FOUND"); return None

        token     = int(row.iloc[0]["instrument_token"])
        to_date   = datetime.now()
        from_date = to_date - timedelta(days=days)

        data = kite.historical_data(token, from_date, to_date, "day")
        if not data:
            print("NO DATA"); return None

        df = pd.DataFrame(data)
        df.rename(columns={"date": "timestamp"}, inplace=True)
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True)
        print(f"{len(df)} daily bars ✓")
        return df

    except Exception as e:
        print(f"ERROR: {e}"); return None


# ----------------------------------------------------------------
# V3 BACKTEST ENGINE (inherits V2, applies 3 fixes)
# ----------------------------------------------------------------
class BacktestEngineV3(BacktestEngineV2):
    """
    V3 engine with targeted per-strategy fixes:

    ORB:  min_confidence = 80 (FIX C)
    VWAP: min_confidence = 0  (FIX B — confidence filter removed)
    Both: real daily data passed to regime filter (FIX A)
    """

    def __init__(self, strategy_name: str, **kwargs):
        # FIX C: ORB gets confidence 80, VWAP gets 0 (no filter)
        if strategy_name == 'ORB_15':
            min_conf = kwargs.pop('min_confidence', 80)
        elif strategy_name == 'VWAP_PULLBACK':
            min_conf = kwargs.pop('min_confidence', 0)   # FIX B: removed
        else:
            min_conf = kwargs.pop('min_confidence', 65)

        super().__init__(strategy_name, min_confidence=min_conf, **kwargs)
        self._real_daily_data = {}   # FIX A: populated before run()

    def set_real_daily_data(self, daily_data: dict):
        """FIX A: Pass real daily OHLCV data for regime filter"""
        self._real_daily_data = daily_data

    def run(self, data: dict, daily_data: dict = None) -> dict:
        """Override: use real daily data if available"""
        # FIX A: prefer real daily data over resampled
        if self._real_daily_data:
            return super().run(data, daily_data=self._real_daily_data)
        return super().run(data, daily_data=daily_data)


# ----------------------------------------------------------------
# PRINT V3 SUMMARY
# ----------------------------------------------------------------
def print_v3_summary(results: list, symbols: list):
    valid = [r for r in results if "error" not in r]
    if not valid:
        print("  No valid results.")
        return

    print("\n" + "=" * 75)
    print("  BACKTEST V3 — FINAL RESULTS")
    print(f"  Symbols: {', '.join([s.replace('NSE:','') for s in symbols])}")
    print(f"  Fixes: Real daily regime data | ORB conf≥80 | VWAP no conf filter")
    print("=" * 75)
    print(f"\n  {'Strategy':<18} {'Trades':>7} {'WinRate':>9} {'PF':>7} "
          f"{'Sharpe':>8} {'MaxDD%':>8} {'NetPnL':>12} {'GrossPnL':>12}")
    print("  " + "-" * 70)

    for m in valid:
        net_icon   = "✅" if m.get('total_net_pnl', 0)   > 0  else "⚠️ "
        gross_icon = "✅" if m.get('gross_pnl', 0)        > 0  else "⚠️ "
        print(f"  {net_icon}{m['strategy']:<16} "
              f"{m.get('total_trades',0):>7} "
              f"{m.get('win_rate_pct',0):>8.1f}% "
              f"{m.get('profit_factor',0):>7.2f} "
              f"{m.get('sharpe_ratio',0):>8.2f} "
              f"{m.get('max_drawdown_pct',0):>7.1f}% "
              f"₹{m.get('total_net_pnl',0):>10,.0f} "
              f"₹{m.get('gross_pnl',0):>10,.0f}")

    print()

    # Regime filter impact
    print("  Regime filter (real daily data):")
    for m in valid:
        blocked = m.get('regime_blocked_days', 0)
        print(f"    {m['strategy']:<20}: {blocked} days blocked "
              f"({'✅ working' if blocked > 0 else '⚠️ still 0 — check daily data fetch'})")

    # Detailed per-strategy analysis
    print()
    for m in valid:
        wr    = m.get('win_rate_pct', 0)
        pf    = m.get('profit_factor', 0)
        dd    = m.get('max_drawdown_pct', 0)
        net   = m.get('total_net_pnl', 0)
        gross = m.get('gross_pnl', 0)
        sl_pct= m.get('sl_hit_pct', 0)
        tgt   = m.get('target_hit_pct', 0)
        trades= m.get('total_trades', 0)
        conf  = m.get('confidence_filtered', 0)
        reg   = m.get('regime_blocked_days', 0)

        print(f"  ── {m['strategy']} ──")
        print(f"     Trades: {trades}  |  Win: {wr:.1f}%  |  "
              f"SL hit: {sl_pct:.1f}%  |  Target: {tgt:.1f}%")
        print(f"     Gross: ₹{gross:+,.0f}  |  Net: ₹{net:+,.0f}  |  "
              f"Regime blocked: {reg} days  |  Conf filtered: {conf}")

        if net > 0:
            monthly = net / 6
            print(f"     📈 NET POSITIVE! Est monthly: ₹{monthly:,.0f}")
        elif gross > 0:
            print(f"     💡 Gross positive but costs still high. "
                  f"Need {int(trades * 0.6)} fewer trades to break even.")
        else:
            print(f"     ❌ No gross edge detected in this period/regime.")
        print()

    # Final readiness
    print("=" * 75)
    print("  📋 PAPER TRADE READINESS:")
    for m in valid:
        net   = m.get('total_net_pnl', 0)
        gross = m.get('gross_pnl', 0)
        wr    = m.get('win_rate_pct', 0)
        dd    = m.get('max_drawdown_pct', 0)

        if net > 0 and wr >= 45 and dd <= 15:
            status = "✅ READY FOR PAPER TRADE"
        elif gross > 0 and wr >= 42:
            status = "🟡 CLOSE — 1 more round of tuning needed"
        else:
            status = "❌ NOT READY — more fixes needed"

        print(f"    {m['strategy']:<20}: {status}")
    print("=" * 75)


# ----------------------------------------------------------------
# SAVE RESULTS
# ----------------------------------------------------------------
def save_results(results: list, tag="v3"):
    logs_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"
    )
    os.makedirs(logs_dir, exist_ok=True)
    path = os.path.join(
        logs_dir,
        f"backtest_{tag}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    )
    rows = [r for r in results if "error" not in r]
    if rows:
        pd.DataFrame(rows).to_csv(path, index=False)
        print(f"\n  ✅ Results saved: {path}")
    return path


# ----------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------
def main():
    print("=" * 75)
    print("  BACKTEST V3 — REAL DATA WITH ALL FIXES APPLIED")
    print("=" * 75)

    # Login
    print("\n[1/5] Logging in...")
    kite = get_kite_session(headless=True)
    print("      ✅ Login OK")

    # Fetch 5-min intraday data
    print(f"\n[2/5] Fetching {LOOKBACK_DAYS} days of 5-min data...")
    intraday_data = {}
    for symbol in SYMBOLS_TO_TEST:
        df = fetch_5min(kite, symbol, LOOKBACK_DAYS)
        if df is not None and len(df) >= 100:
            intraday_data[f"NSE:{symbol}"] = df

    if not intraday_data:
        print("❌ No intraday data."); return
    print(f"      ✅ {len(intraday_data)} symbols loaded")

    # FIX A: Fetch REAL daily data (300 days for regime filter)
    print(f"\n[3/5] Fetching real DAILY data for regime filter (300 days)...")
    daily_data = {}
    for symbol in SYMBOLS_TO_TEST:
        df = fetch_daily(kite, symbol, days=300)
        if df is not None and len(df) >= 50:
            daily_data[f"NSE:{symbol}"] = df

    print(f"      ✅ {len(daily_data)} symbols with daily bars")

    # V1 baseline (for comparison)
    print(f"\n[4/5] Running V1 baseline for comparison...")
    from backtest.backtest_engine import BacktestEngine
    v1_results = {}
    for strat in STRATEGIES:
        e = BacktestEngine(strat)
        m = e.run(intraday_data)
        v1_results[strat] = m

    # V3 fixed backtest
    print(f"\n[5/5] Running V3 fixed backtest...")
    print("-" * 75)
    v3_results = []

    for strat in STRATEGIES:
        engine = BacktestEngineV3(strat)
        engine.set_real_daily_data(daily_data)    # FIX A
        metrics = engine.run(intraday_data)
        engine.print_report(metrics)
        v3_results.append(metrics)

    # Print comparison V1 vs V3
    print("\n" + "=" * 75)
    print("  V1 → V3 IMPROVEMENT SUMMARY")
    print("=" * 75)
    print(f"  {'Strategy':<18} {'Metric':<20} {'V1':>14} {'V3':>14} {'Δ':>10}")
    print("  " + "-" * 70)

    for strat in STRATEGIES:
        m1 = v1_results.get(strat, {})
        m3 = next((r for r in v3_results if r.get('strategy') == strat), {})
        if 'error' in m1 or 'error' in m3:
            continue

        compare_keys = [
            ('total_trades',     'Trades'),
            ('win_rate_pct',     'Win Rate %'),
            ('gross_pnl',        'Gross PnL ₹'),
            ('total_net_pnl',    'Net PnL ₹'),
            ('max_drawdown_pct', 'Max Drawdown %'),
            ('regime_blocked_days', 'Regime Blocked Days'),
        ]

        first = True
        for key, label in compare_keys:
            v1_val = m1.get(key, 0)
            # V1 doesn't have gross_pnl — calculate it
            if key == 'gross_pnl' and 'gross_pnl' not in m1:
                v1_val = m1.get('total_net_pnl', 0) + m1.get('total_cost_drag', 0)
            v3_val = m3.get(key, 0)
            delta  = v3_val - v1_val if isinstance(v3_val, (int, float)) else 0

            icon   = ""
            if key == 'total_trades':      icon = "✅" if delta < 0 else "⚠️"
            if key == 'win_rate_pct':      icon = "✅" if delta > 0 else "⚠️"
            if key == 'gross_pnl':         icon = "✅" if v3_val > 0 else "⚠️"
            if key == 'total_net_pnl':     icon = "✅" if v3_val > 0 else "⚠️"
            if key == 'max_drawdown_pct':  icon = "✅" if delta < 0 else "⚠️"
            if key == 'regime_blocked_days': icon = "✅" if v3_val > 0 else "⚠️"

            strat_lbl = strat if first else ""
            print(f"  {strat_lbl:<18} {label:<20} "
                  f"{str(round(v1_val,1)):>14} "
                  f"{str(round(v3_val,1)):>14} "
                  f"{'+' if delta > 0 else ''}{round(delta,1):>8} {icon}")
            first = False
        print()

    # Summary and readiness
    print_v3_summary(v3_results, list(intraday_data.keys()))
    save_results(v3_results, tag="v3_final")


if __name__ == "__main__":
    main()
