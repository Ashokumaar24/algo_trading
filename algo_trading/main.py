# ============================================================
#  main.py
#  AI Hybrid Algo Trading Bot — Main Entry Point
#
#  FIX 14a: Deduplicated pre_market_setup() calls
#  FIX 14b: on_candle_close unpacks can_trade() tuple
#
#  FIX (startup crash): KiteLogin import now works.
#    - auth/login.py lacked the KiteLogin class → ImportError on startup.
#    - KiteLogin class added to auth/login.py (see that file for details).
#
#  FIX (CLI flags): --backtest and --scan-only now work.
#    - README documented `python main.py --backtest` and
#      `python main.py --scan-only`, but neither flag was parsed or
#      acted on — the __main__ block always called system.run() regardless.
#    - Both flags are now handled in __main__ before constructing the
#      full AIHybridTradingSystem (which requires Zerodha login).
#    - --backtest runs the demo backtest via BacktestEngine directly.
#    - --scan-only logs in, runs the pre-market scanner, prints results,
#      and exits — no ticker, no orders.
# ============================================================

import sys
import os
import signal
import time
import argparse
from datetime import datetime, time as t
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Parse CLI flags ───────────────────────────────────────────────
_parser = argparse.ArgumentParser(
    description="AI Hybrid Algo Trading System",
    add_help=True,
)
_parser.add_argument('--dry-run',   action='store_true', default=False,
                     help="Paper trade mode — no real orders or Zerodha login")
_parser.add_argument('--backtest',  action='store_true', default=False,
                     help="Run demo backtest on synthetic data and exit")
_parser.add_argument('--scan-only', action='store_true', default=False,
                     help="Run pre-market scanner only, print results, and exit")
_args = _parser.parse_args()

DRY_RUN   = _args.dry_run
BACKTEST  = _args.backtest
SCAN_ONLY = _args.scan_only

if DRY_RUN:
    print("[DRY-RUN] Starting in paper/simulation mode — no real orders or login")

from kiteconnect import KiteTicker
from apscheduler.schedulers.background import BackgroundScheduler
from zoneinfo import ZoneInfo

from auth.login import KiteLogin
from scanner.pre_market_scanner import PreMarketScanner
from strategies.orb_strategy import ORBStrategy
from strategies.vwap_pullback import VWAPPullbackStrategy
from strategies.ema_rsi_strategy import EMARSIStrategy
from strategies.breakout_atr import BreakoutATRStrategy
from utils.candle_builder import CandleBuilder
from execution.order_manager import OrderManager
from risk.risk_manager import RiskManager
from utils.journal import TradeJournal
from config.config import (
    MAX_TRADES_PER_DAY, CAPITAL, NIFTY50_SYMBOLS
)

PAPER_TRADE_MODE = DRY_RUN
INITIAL_CAPITAL  = CAPITAL

from utils.logger import get_logger

logger = get_logger("main")
IST    = ZoneInfo("Asia/Kolkata")


# ================================================================
#  AI HYBRID TRADING SYSTEM
# ================================================================
class AIHybridTradingSystem:
    """
    Coordinates all subsystems:
      Scanner → Strategies → Risk → Execution → Journal
    """

    def __init__(self):
        logger.info("=" * 60)
        logger.info("  AI HYBRID TRADING SYSTEM — Starting up")
        logger.info("=" * 60)

        if DRY_RUN:
            self.kite = self._create_mock_kite()
        else:
            kite_login = KiteLogin()
            self.kite  = kite_login.get_kite_instance()

        self.scanner        = PreMarketScanner(self.kite)
        self.candle_builder = CandleBuilder(interval_minutes=5)
        self.order_manager  = OrderManager(self.kite, paper_trade=PAPER_TRADE_MODE)
        self.risk_manager   = RiskManager(capital=INITIAL_CAPITAL)
        self.journal        = TradeJournal()

        self.strategies = {
            'ORB_15':        ORBStrategy(),
            'VWAP_PULLBACK': VWAPPullbackStrategy(),
            'EMA_RSI':       EMARSIStrategy(),
            'BREAKOUT_ATR':  BreakoutATRStrategy(),
        }

        self.watchlist  = []
        self.ticker     = None
        self.scheduler  = None
        self._running   = False

    # ------------------------------------------------------------------
    # PRE-MARKET SETUP (9:05 AM)
    # ------------------------------------------------------------------
    def pre_market_setup(self):
        """
        Run scanner, set watchlist, initialise strategies.
        Called ONCE per session (see FIX 14a in run()).
        """
        logger.info("Running pre-market setup...")

        try:
            candidates     = self.scanner.run(top_n=5)
            self.watchlist = [c.symbol for c in candidates]
            logger.info(f"Watchlist set: {self.watchlist}")

            for strat in self.strategies.values():
                strat.reset_daily()

            self.candle_builder.reset_daily()
            self.risk_manager.reset_daily()
            self.candle_builder.set_callback(self.on_candle_close)

        except Exception as e:
            logger.error(f"pre_market_setup failed: {e}")

    # ------------------------------------------------------------------
    # CANDLE CLOSE CALLBACK
    # ------------------------------------------------------------------
    def on_candle_close(self, symbol: str, candle, history):
        """
        Called by CandleBuilder every time a 5-minute candle closes.

        FIX 14b: Unpack can_trade() tuple in one call — no race condition.
        """
        try:
            allowed, reason_str = self.risk_manager.can_trade()

            if not allowed:
                self.journal.log_trade_blocked(
                    symbol=symbol,
                    candle_time=candle.timestamp,
                    reason=reason_str,
                )
                return

            candle_dict = candle.to_dict()

            for name, strategy in self.strategies.items():
                if symbol not in self.watchlist:
                    continue

                signal = None

                try:
                    if name == 'ORB_15':
                        signal = strategy.check_entry(
                            symbol, candle_dict, history,
                            avg_volume_20d=self._get_avg_volume(symbol)
                        )
                    elif name == 'VWAP_PULLBACK':
                        prev = history.iloc[-2].to_dict() if len(history) >= 2 else None
                        signal = strategy.check_entry(symbol, candle_dict, history, prev)
                    elif name == 'EMA_RSI':
                        signal = strategy.check_entry(symbol, candle_dict, history)
                    elif name == 'BREAKOUT_ATR':
                        signal = strategy.check_entry(
                            symbol, candle_dict, history,
                            float(history['volume'].sum()),
                            self._get_avg_volume(symbol) * 6
                        )
                except Exception as e:
                    logger.debug(f"Strategy error [{name}] {symbol}: {e}")
                    continue

                if signal:
                    logger.info(f"Signal received: {signal}")
                    self._execute_signal(signal)

        except Exception as e:
            logger.error(f"on_candle_close error for {symbol}: {e}")

    # ------------------------------------------------------------------
    # SIGNAL EXECUTION
    # ------------------------------------------------------------------
    def _execute_signal(self, signal):
        """Execute a trading signal through order manager + risk manager"""
        symbol    = signal.symbol
        entry     = signal.entry
        sl        = signal.stop_loss
        target    = signal.target
        direction = signal.direction.value

        qty = self.risk_manager.calculate_position_size(entry, sl)
        if qty <= 0:
            logger.warning(f"Position size 0 for {symbol} — skipping")
            return

        size_ok, size_msg = self.risk_manager.is_position_size_ok(entry, sl, qty)
        if not size_ok:
            logger.warning(f"Position size check failed: {size_msg}")
            return

        order_info = self.order_manager.place_order(
            symbol=symbol.replace("NSE:", ""),
            direction=direction,
            entry=entry, sl=sl, target=target, quantity=qty
        )

        if order_info:
            self.risk_manager.record_trade_entry()
            self.journal.log_entry(
                symbol=symbol, direction=direction,
                entry=entry, sl=sl, target=target, qty=qty,
                fill_price=order_info.get('fill_price', entry),
                strategy=signal.strategy, confidence=signal.confidence,
                notes=signal.notes
            )
        else:
            logger.warning(f"Order not placed for {symbol} (fill confirmation failed)")

    # ------------------------------------------------------------------
    # KITE TICKER
    # ------------------------------------------------------------------
    def start_ticker(self):
        """Start real-time KiteTicker websocket"""
        try:
            access_token   = self.kite.access_token
            api_key        = self.kite.api_key
            self.ticker    = KiteTicker(api_key, access_token)

            self.ticker.on_ticks   = self._on_ticks
            self.ticker.on_connect = self._on_connect
            self.ticker.on_close   = self._on_close
            self.ticker.on_error   = self._on_error

            self.ticker.connect(threaded=True)
            logger.info("KiteTicker connected (threaded)")

        except Exception as e:
            logger.error(f"KiteTicker start failed: {e}")

    def _on_connect(self, ws, response):
        """Subscribe to instrument tokens for watchlist"""
        try:
            instruments = self.kite.instruments("NSE")
            token_map   = {i['tradingsymbol']: i['instrument_token']
                           for i in instruments}

            tokens = []
            for sym in self.watchlist:
                clean = sym.replace("NSE:", "")
                if clean in token_map:
                    tokens.append(token_map[clean])

            if tokens:
                ws.subscribe(tokens)
                ws.set_mode(ws.MODE_FULL, tokens)
                logger.info(f"Subscribed to {len(tokens)} instruments")

        except Exception as e:
            logger.error(f"Subscription error: {e}")

    def _on_ticks(self, ws, ticks):
        for tick in ticks:
            try:
                self.candle_builder.process_tick(tick)
            except Exception as e:
                logger.debug(f"Tick error: {e}")

    def _on_close(self, ws, code, reason):
        logger.warning(f"Ticker closed: code={code} reason={reason}")

    def _on_error(self, ws, code, reason):
        logger.error(f"Ticker error: code={code} reason={reason}")

    # ------------------------------------------------------------------
    # SCHEDULER JOBS
    # ------------------------------------------------------------------
    def _schedule_jobs(self):
        self.scheduler = BackgroundScheduler(timezone=IST)

        self.scheduler.add_job(
            self._daily_reset, 'cron',
            hour=9, minute=0, id='daily_reset'
        )
        self.scheduler.add_job(
            self._eod_close, 'cron',
            hour=15, minute=15, id='eod_close'
        )
        self.scheduler.add_job(
            self._end_of_day, 'cron',
            hour=15, minute=30, id='end_of_day'
        )

        self.scheduler.start()
        logger.info("Scheduler started")

    def _daily_reset(self):
        logger.info("Scheduler: daily reset triggered")
        for strat in self.strategies.values():
            strat.reset_daily()
        self.candle_builder.reset_daily()
        self.risk_manager.reset_daily()

    def _eod_close(self):
        logger.info("Scheduler: EOD force close triggered")
        self.candle_builder.force_close_all()
        self.order_manager.force_close_all(reason="EOD_15:15")

    def _end_of_day(self):
        logger.info("Scheduler: end-of-day wrap-up")
        self.journal.log_daily_summary(
            self.risk_manager.get_state_snapshot()
        )

    # ------------------------------------------------------------------
    # FIX 14a: MAIN RUN LOOP — deduplicated pre_market_setup()
    # ------------------------------------------------------------------
    def run(self):
        """
        Main run loop. Checks market time and calls appropriate actions.
        FIX 14a: pre_market_setup() called only once (when watchlist is empty).
        """
        logger.info("Starting main loop...")
        self._schedule_jobs()
        self._running = True

        signal.signal(signal.SIGINT,  self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        while self._running:
            try:
                now = datetime.now(IST).time()

                if t(9, 0) <= now < t(15, 20):
                    if not self.watchlist:
                        self.pre_market_setup()

                    if now >= t(9, 15) and self.ticker is None:
                        self.start_ticker()

                elif now >= t(15, 20):
                    if self._running and self.ticker:
                        logger.info("Post-market: stopping ticker")
                        try:
                            self.ticker.close()
                        except Exception:
                            pass
                        self.ticker = None

                time.sleep(10)

            except Exception as e:
                logger.error(f"Main loop error: {e}")
                time.sleep(30)

    def _shutdown(self, signum=None, frame=None):
        logger.info("Shutdown signal received — cleaning up...")
        self._running = False

        if self.ticker:
            try:
                self.order_manager.force_close_all(reason="SHUTDOWN")
                self.ticker.close()
            except Exception as e:
                logger.error(f"Shutdown error: {e}")

        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)

        logger.info("Shutdown complete")

    def _get_avg_volume(self, symbol: str) -> float:
        return 1_000_000.0

    def _create_mock_kite(self):
        """Returns a lightweight mock KiteConnect for --dry-run mode."""
        class MockKite:
            api_key      = "dry_run"
            access_token = "dry_run"

            def profile(self):           return {"user_id": "DRY_RUN"}
            def instruments(self, *a):   return []
            def ltp(self, *a):           return {}
            def positions(self):         return {"net": [], "day": []}
            def orders(self):            return []
            def place_order(self, **kw): return f"MOCK_{int(time.time())}"
            def cancel_order(self, *a):  return True
            def order_history(self, oid):
                return [{"status": "COMPLETE", "average_price": 100.0}]
            def historical_data(self, *a, **kw): return []

        logger.info("DRY-RUN: using MockKite — no real API calls will be made")
        return MockKite()


# ================================================================
#  SCAN-ONLY MODE — login, run scanner, print results, exit
# ================================================================
def run_scan_only():
    """
    FIX: --scan-only now actually runs.
    Login → pre-market scan → print top 5 → exit.
    No ticker, no orders, no scheduler.
    """
    logger.info("=== SCAN-ONLY MODE ===")
    kite_login = KiteLogin()
    kite       = kite_login.get_kite_instance()

    scanner    = PreMarketScanner(kite)
    candidates = scanner.run(top_n=5)
    scanner.print_report(candidates)
    logger.info("Scan complete. Exiting.")


# ================================================================
#  ENTRY POINT
# ================================================================
if __name__ == "__main__":

    # ── --backtest: run demo backtest and exit ────────────────────
    if BACKTEST:
        logger.info("=== BACKTEST MODE ===")
        try:
            from backtest.backtest_engine import BacktestEngine, run_all_strategy_comparison
            import pandas as pd
            import numpy as np

            # Generate synthetic 5-min data for the demo
            logger.info("Generating synthetic demo data...")
            dates = pd.date_range("2025-01-01 09:15", periods=2000, freq="5min")
            dates = dates[dates.time() >= __import__('datetime').time(9, 15)]
            dates = dates[dates.time() <= __import__('datetime').time(15, 15)]

            np.random.seed(42)
            close  = 500 + np.cumsum(np.random.randn(len(dates)) * 2)
            open_  = close + np.random.randn(len(dates))
            high   = np.maximum(open_, close) + abs(np.random.randn(len(dates)))
            low    = np.minimum(open_, close) - abs(np.random.randn(len(dates)))
            volume = np.random.randint(100_000, 500_000, len(dates)).astype(float)

            demo_df = pd.DataFrame({
                'open': open_, 'high': high, 'low': low,
                'close': close, 'volume': volume
            }, index=dates)

            demo_data = {"NSE:RELIANCE": demo_df}
            metrics   = run_all_strategy_comparison(demo_data)

            print("\n" + "=" * 60)
            print("  BACKTEST RESULTS (demo / synthetic data)")
            print("=" * 60)
            for m in metrics:
                if "error" not in m:
                    print(f"  {m['strategy']:<20} | "
                          f"Win Rate: {m.get('win_rate_pct', 0):.1f}% | "
                          f"Sharpe: {m.get('sharpe_ratio', 0):.2f} | "
                          f"PF: {m.get('profit_factor', 0):.2f} | "
                          f"Net PnL: ₹{m.get('total_net_pnl', 0):+,.0f}")
            print()
            print("  For real data backtest: python backtest/fetch_and_backtest.py")
            print()
        except ImportError as e:
            logger.error(f"Backtest engine not available: {e}")
            sys.exit(1)
        sys.exit(0)

    # ── --scan-only: login, scan, print, exit ────────────────────
    if SCAN_ONLY:
        run_scan_only()
        sys.exit(0)

    # ── Normal / dry-run: full trading system ────────────────────
    system = AIHybridTradingSystem()
    system.run()
