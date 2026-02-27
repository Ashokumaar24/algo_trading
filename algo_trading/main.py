# ============================================================
#  main.py
#  AI Hybrid Algo Trading Bot — Main Entry Point
#
#  FIX 14a: Deduplicated pre_market_setup() calls
#    - Original: pre_market_setup() called in BOTH the 9:00-9:20 branch
#      AND the 9:20-15:20 branch — called twice on startup between 9:20-9:30
#    - Fix: single condition covers the full window (9:00 to 15:20)
#      so it's only ever called once per session
#
#  FIX 14b: on_candle_close unpacks can_trade() tuple
#    - Original: called can_trade() as bool, then get_block_reason() separately
#      — race condition at time boundaries (FIX 10 in risk_manager.py)
#    - Fix: allowed, reason_str = self.risk_manager.can_trade()
#      Both values come from one atomic call
#
#  All other fixes are in the individual modules.
# ============================================================

import sys
import os
import signal
import time
import argparse
from datetime import datetime, time as t
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Parse --dry-run flag ──────────────────────────────────────────
_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument('--dry-run', action='store_true', default=False)
_args, _ = _parser.parse_known_args()
DRY_RUN = _args.dry_run

if DRY_RUN:
    print("[DRY-RUN] Starting in paper/simulation mode — no real orders or login")

from kiteconnect import KiteTicker
from apscheduler.schedulers.background import BackgroundScheduler
from zoneinfo import ZoneInfo   # built-in from Python 3.9 — no install needed

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

# PAPER_TRADE_MODE: True in --dry-run, False in live
PAPER_TRADE_MODE = DRY_RUN
INITIAL_CAPITAL  = CAPITAL   # alias for readability
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

        # Auth & connection
        # In --dry-run mode skip real Zerodha login entirely
        if DRY_RUN:
            self.kite = self._create_mock_kite()
        else:
            kite_login = KiteLogin()
            self.kite  = kite_login.get_kite_instance()

        # Core subsystems
        self.scanner        = PreMarketScanner(self.kite)
        self.candle_builder = CandleBuilder(interval_minutes=5)
        self.order_manager  = OrderManager(self.kite, paper_trade=PAPER_TRADE_MODE)
        self.risk_manager   = RiskManager(capital=INITIAL_CAPITAL)
        self.journal        = TradeJournal()

        # Strategies
        self.strategies = {
            'ORB_15':        ORBStrategy(),
            'VWAP_PULLBACK': VWAPPullbackStrategy(),
            'EMA_RSI':       EMARSIStrategy(),
            'BREAKOUT_ATR':  BreakoutATRStrategy(),
        }

        # Active watchlist (set by scanner at 9:05 AM)
        self.watchlist      = []
        self.ticker         = None
        self.scheduler      = None
        self._running       = False

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
            # FIX 14b: Single atomic call — avoid race between can_trade()
            # and get_block_reason() that existed in the original code
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

        # Daily reset (9:00 AM)
        self.scheduler.add_job(
            self._daily_reset, 'cron',
            hour=9, minute=0, id='daily_reset'
        )

        # Force close all positions (15:15 PM)
        self.scheduler.add_job(
            self._eod_close, 'cron',
            hour=15, minute=15, id='eod_close'
        )

        # Strategy reset for next day (15:30 PM)
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

        FIX 14a: Original called pre_market_setup() in BOTH the
        9:00-9:20 AND 9:20-15:20 branches, triggering it twice during
        the 9:20-9:30 window. Now: single condition covers the full
        pre-market-to-market window.
        """
        logger.info("Starting main loop...")
        self._schedule_jobs()
        self._running = True

        signal.signal(signal.SIGINT,  self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        while self._running:
            try:
                now = datetime.now(IST).time()

                # 9:00 AM – 15:20 PM: active trading session
                # FIX 14a: Single condition — no duplicate call
                if t(9, 0) <= now < t(15, 20):

                    if not self.watchlist:
                        # Only run setup if watchlist is empty (first time)
                        self.pre_market_setup()

                    if now >= t(9, 15) and self.ticker is None:
                        self.start_ticker()

                elif now >= t(15, 20):
                    # Post-market: ensure cleanup ran
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
        """Placeholder — replace with real 20-day average daily volume lookup"""
        return 1_000_000.0

    def _create_mock_kite(self):
        """
        Returns a lightweight mock KiteConnect for --dry-run mode.
        All API calls return safe empty responses so the system can
        start and process candles without touching Zerodha.
        """
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
#  ENTRY POINT
# ================================================================
if __name__ == "__main__":
    system = AIHybridTradingSystem()
    system.run()
