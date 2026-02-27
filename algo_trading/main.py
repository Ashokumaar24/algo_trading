# ============================================================
#  main.py  — AI Hybrid Algo Trading System
#
#  FIXES in this version:
#
#  BUG 1 FIXED: AIHybridStrategy now used instead of individual strategies.
#    - Individual strategies dict replaced with self.ai_hybrid.
#    - Regime filtering, sentiment gate, and strategy selection are all
#      handled inside AIHybridStrategy as designed.
#    - EMA_RSI (retired) can no longer fire.
#
#  BUG 2 FIXED: Real regime classification — MarketRegimeClassifier now
#    called with actual 300-day Nifty50 daily OHLCV data every morning.
#    No more hardcoded RANGE/NORMAL_VOL.
#
#  BUG 3 FIXED: India VIX now fetched live from Zerodha at 9:05 AM.
#    Position sizing and circuit breakers now respond to real fear levels.
#
#  BUG 4 FIXED: set_orb() is now called in on_candle_close() after the
#    9:30 candle closes for each symbol. Extracts 9:15, 9:20, 9:25
#    candles from history and passes them to ai_hybrid.set_orb().
#    ORB strategy can now fire for the first time.
#
#  BUG 5 FIXED: _daily_reset() now clears self.watchlist = [] so
#    pre_market_setup() runs every morning (not just Day 1).
#    Also resets self._orb_set, self._nifty_daily, self._india_vix.
#
#  BUG 6 FIXED: EOD Telegram summary now shows real win/loss count
#    sourced from DailyJournal exit log (not risk manager snapshot).
#
#  BUG 7 FIXED: DailyJournal.generate_report() called in _end_of_day().
#    Journal markdown file path passed to notify_eod_summary() so
#    the full journal is attached to the Telegram EOD message.
#
#  BUG 10 FIXED: DailyJournal wired into main trading flow.
#    log_regime() called after regime classification.
#    log_scanner_results() called after scanner.run().
#    generate_report() called at 3:30 PM.
#
#  TELEGRAM + STARTUP (previous session):
#    Notifier created before login, command listener started,
#    all key events send Telegram messages.
# ============================================================

import sys
import os
import signal
import time
import argparse
import pandas as pd
from datetime import datetime, time as t, timedelta
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Parse CLI flags ───────────────────────────────────────────────
_parser = argparse.ArgumentParser(description="AI Hybrid Algo Trading System")
_parser.add_argument('--dry-run',   action='store_true', default=False,
                     help="Paper trade mode — real login, simulated orders")
_parser.add_argument('--backtest',  action='store_true', default=False,
                     help="Run demo backtest on synthetic data and exit")
_parser.add_argument('--scan-only', action='store_true', default=False,
                     help="Run pre-market scanner only and exit")
_args = _parser.parse_args()

DRY_RUN   = _args.dry_run
BACKTEST  = _args.backtest
SCAN_ONLY = _args.scan_only

if DRY_RUN:
    print("[DRY-RUN] Paper trade mode — real Zerodha login, simulated orders")

from kiteconnect import KiteTicker
from apscheduler.schedulers.background import BackgroundScheduler
from zoneinfo import ZoneInfo

from auth.login import KiteLogin
from scanner.pre_market_scanner import PreMarketScanner
# BUG 1 FIX: Use AIHybridStrategy — not individual strategies
from strategies.ai_hybrid import AIHybridStrategy
from regime.market_regime import MarketRegimeClassifier, MarketRegime
from utils.candle_builder import CandleBuilder
from execution.order_manager import OrderManager
from risk.risk_manager import RiskManager
from utils.journal import TradeJournal
# BUG 10 FIX: Wire DailyJournal
from utils.daily_journal import get_journal as get_daily_journal, reset_journal
from utils.telegram_notifier import get_notifier
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

    def __init__(self):
        logger.info("=" * 60)
        logger.info("  AI HYBRID TRADING SYSTEM — Starting up")
        logger.info("=" * 60)

        # Create notifier first — so login failures can be reported
        self.notifier = get_notifier()

        # Always do a real Zerodha login — even in dry-run.
        # DRY_RUN only controls ORDER PLACEMENT, not authentication.
        # Real login = real tick data, real prices, real scanner.
        try:
            logger.info(
                f"Connecting to Zerodha... "
                f"({'PAPER TRADE — real login, simulated orders' if DRY_RUN else 'LIVE TRADING'})"
            )
            kite_login = KiteLogin()
            self.kite  = kite_login.get_kite_instance()
            self.notifier.notify_startup(dry_run=DRY_RUN)
            logger.info("✅ Zerodha login confirmed — Telegram notified")
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Login failed: {error_msg}")
            self.notifier.notify_login_failed(error_msg)
            raise

        self.scanner        = PreMarketScanner(self.kite)
        self.candle_builder = CandleBuilder(interval_minutes=5)
        self.order_manager  = OrderManager(self.kite, paper_trade=PAPER_TRADE_MODE)
        self.risk_manager   = RiskManager(capital=INITIAL_CAPITAL)
        self.journal        = TradeJournal()

        # BUG 1 FIX: Single AIHybridStrategy instead of 4 individual strategies.
        # AIHybrid handles: regime selection, sentiment gate, EMA_RSI exclusion.
        self.ai_hybrid = AIHybridStrategy()

        self.watchlist        = []
        self.ticker           = None
        self.scheduler        = None
        self._running         = False

        # BUG 4 FIX: Track which symbols have had set_orb() called today
        self._orb_set: set    = set()

        # BUG 2+3 FIX: Cache Nifty daily data and VIX for intraday use
        self._nifty_daily: Optional[pd.DataFrame] = None
        self._india_vix: float                    = 15.0  # safe default
        self._current_regime: Optional[MarketRegime] = None

        # BUG 10 FIX: DailyJournal instance (reset each morning)
        self.daily_journal = get_daily_journal()

        # Wire Telegram command listener
        self.notifier._trading_system = self
        self.notifier.start_command_listener(trading_system=self)
        logger.info("Telegram command listener active — /stop /status /journal /help")

    # ------------------------------------------------------------------
    # PRE-MARKET SETUP (9:05 AM)
    # BUG 2+3+10 FIX: Fetch real VIX + Nifty daily, classify regime properly
    # ------------------------------------------------------------------
    def pre_market_setup(self):
        logger.info("Running pre-market setup...")

        try:
            # ── BUG 3 FIX: Fetch live India VIX ─────────────────────
            try:
                vix_data       = self.kite.ltp(["NSE:INDIA VIX"])
                self._india_vix = float(vix_data["NSE:INDIA VIX"]["last_price"])
                logger.info(f"India VIX: {self._india_vix:.2f}")
            except Exception as e:
                logger.warning(f"VIX fetch failed ({e}) — using default {self._india_vix:.1f}")

            # ── BUG 2 FIX: Fetch Nifty50 daily OHLCV (300 days) ─────
            self._nifty_daily = self._fetch_nifty_daily()

            # ── BUG 2 FIX: Classify real market regime ───────────────
            classifier = MarketRegimeClassifier()
            if self._nifty_daily is not None and len(self._nifty_daily) >= 200:
                self._current_regime = classifier.classify(
                    self._nifty_daily, self._india_vix
                )
                eligible = classifier.get_eligible_strategies(self._current_regime)
            else:
                logger.warning(
                    "Insufficient Nifty daily data — defaulting to RANGE/NORMAL_VOL"
                )
                self._current_regime = MarketRegime(
                    "RANGE", "NORMAL_VOL",
                    adx=0.0, india_vix=self._india_vix
                )
                eligible = ["VWAP_PULLBACK"]

            logger.info(f"Regime: {self._current_regime}")

            # ── Run pre-market scanner ────────────────────────────────
            candidates     = self.scanner.run(top_n=5)
            self.watchlist = [c.symbol for c in candidates]
            logger.info(f"Watchlist: {self.watchlist}")

            # ── BUG 1 FIX: Setup AIHybridStrategy with today's regime ─
            # Pass previous day data for Breakout ATR strategy
            prev_day_data = self._build_prev_day_data()
            self.ai_hybrid.setup_day(
                nifty_daily=self._nifty_daily if self._nifty_daily is not None
                            else pd.DataFrame(),
                india_vix=self._india_vix,
                prev_day_data=prev_day_data
            )

            # ── BUG 10 FIX: Log to DailyJournal ─────────────────────
            self.daily_journal.log_regime(self._current_regime, eligible)
            self.daily_journal.log_scanner_results(candidates)
            self.daily_journal.add_note(
                f"India VIX at open: {self._india_vix:.2f}"
            )

            # ── Notify Telegram ───────────────────────────────────────
            self.notifier.notify_scanner_results(candidates, self._current_regime)

            # ── Reset per-day state ───────────────────────────────────
            self.candle_builder.reset_daily()
            self.risk_manager.reset_daily()
            self.candle_builder.set_callback(self.on_candle_close)
            self._orb_set.clear()

            # ── Notify if regime blocks trading ──────────────────────
            if not self._current_regime.is_tradeable:
                self.notifier.notify_regime_blocked(self._current_regime)

        except Exception as e:
            logger.error(f"pre_market_setup failed: {e}")
            self.notifier.notify_error("pre_market_setup", str(e))

    # ------------------------------------------------------------------
    # BUG 2 FIX: Fetch Nifty50 daily OHLCV from Zerodha
    # ------------------------------------------------------------------
    def _fetch_nifty_daily(self) -> Optional[pd.DataFrame]:
        """Fetch 300 days of Nifty50 daily OHLCV for regime classification."""
        try:
            instruments = self.kite.instruments("NSE")
            inst_df     = pd.DataFrame(instruments)

            # Nifty 50 index
            row = inst_df[inst_df['tradingsymbol'] == 'NIFTY 50']
            if row.empty:
                logger.warning("NIFTY 50 instrument not found in NSE master")
                return None

            token     = int(row.iloc[0]['instrument_token'])
            to_date   = datetime.now()
            from_date = to_date - timedelta(days=300)

            data = self.kite.historical_data(token, from_date, to_date, 'day')
            if not data:
                return None

            df = pd.DataFrame(data)
            df.rename(columns={'date': 'timestamp'}, inplace=True)
            df.set_index('timestamp', inplace=True)
            df.sort_index(inplace=True)

            logger.info(f"Nifty daily data: {len(df)} bars loaded ✓")
            return df

        except Exception as e:
            logger.warning(f"Nifty daily fetch failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Helper: build prev_day_data for BreakoutATR strategy
    # ------------------------------------------------------------------
    def _build_prev_day_data(self) -> dict:
        """Fetch previous day OHLC for each watchlist stock."""
        prev_data = {}
        for symbol in self.watchlist:
            try:
                instruments = self.kite.instruments("NSE")
                inst_df     = pd.DataFrame(instruments)
                clean       = symbol.replace("NSE:", "")
                row         = inst_df[inst_df['tradingsymbol'] == clean]
                if row.empty:
                    continue
                token  = int(row.iloc[0]['instrument_token'])
                to_dt  = datetime.now()
                from_dt = to_dt - timedelta(days=5)
                hist   = self.kite.historical_data(token, from_dt, to_dt, 'day')
                if hist and len(hist) >= 2:
                    prev = hist[-2]  # yesterday
                    prev_data[symbol] = {
                        'high':  prev['high'],
                        'low':   prev['low'],
                        'close': prev['close'],
                    }
            except Exception as e:
                logger.debug(f"prev_day_data failed for {symbol}: {e}")
        return prev_data

    # ------------------------------------------------------------------
    # CANDLE CLOSE CALLBACK
    # BUG 1+4 FIX: Use AIHybridStrategy and call set_orb() when ready
    # ------------------------------------------------------------------
    def on_candle_close(self, symbol: str, candle, history):
        try:
            # ── BUG 4 FIX: Set ORB after first 9:30 candle ───────────
            candle_t = (candle.timestamp.time()
                        if hasattr(candle.timestamp, 'time')
                        else datetime.now().time())

            if candle_t >= t(9, 30) and symbol not in self._orb_set:
                self._try_set_orb(symbol, history)

            # ── Risk gate check ───────────────────────────────────────
            allowed, reason_str = self.risk_manager.can_trade()
            if self.notifier.is_stop_requested():
                allowed    = False
                reason_str = "TELEGRAM_STOP: /stop command active"

            if not allowed:
                self.journal.log_trade_blocked(
                    symbol=symbol,
                    candle_time=candle.timestamp,
                    reason=reason_str,
                )
                return

            # ── BUG 1 FIX: Get signal from AIHybridStrategy ───────────
            candle_dict = candle.to_dict()
            cum_vol     = float(history['volume'].sum()) if 'volume' in history.columns else 0.0

            signal = self.ai_hybrid.get_signal(
                symbol=symbol,
                candle=candle_dict,
                candle_history=history,
                avg_volume_20d=self._get_avg_volume(symbol),
                cum_volume_today=cum_vol,
                avg_daily_volume=self._get_avg_volume(symbol),
                india_vix=self._india_vix,
            )

            if signal:
                logger.info(f"Signal received: {signal}")
                self.notifier.notify_signal(signal, dry_run=PAPER_TRADE_MODE)
                self.daily_journal.log_trade_placed(signal, dry_run=PAPER_TRADE_MODE)
                self._execute_signal(signal)

        except Exception as e:
            logger.error(f"on_candle_close error for {symbol}: {e}")

    # ------------------------------------------------------------------
    # BUG 4 FIX: Extract opening candles and call set_orb()
    # ------------------------------------------------------------------
    def _try_set_orb(self, symbol: str, history: pd.DataFrame):
        """
        Extract 9:15, 9:20, 9:25 candles from history and set the ORB.
        Called once per symbol when the 9:30 candle closes.
        """
        try:
            opening_candles = []
            for ts, row in history.iterrows():
                row_time = ts.time() if hasattr(ts, 'time') else None
                if row_time and t(9, 15) <= row_time < t(9, 30):
                    opening_candles.append({
                        'high':  float(row['high']),
                        'low':   float(row['low']),
                        'open':  float(row['open']),
                        'close': float(row['close']),
                    })

            if opening_candles:
                self.ai_hybrid.set_orb(symbol, opening_candles)
                self._orb_set.add(symbol)
                logger.info(
                    f"ORB set for {symbol} using {len(opening_candles)} "
                    f"opening candles ✓"
                )
            else:
                logger.debug(
                    f"No 9:15–9:25 candles in history yet for {symbol} "
                    f"(history has {len(history)} rows)"
                )

        except Exception as e:
            logger.debug(f"_try_set_orb failed for {symbol}: {e}")

    # ------------------------------------------------------------------
    # SIGNAL EXECUTION
    # ------------------------------------------------------------------
    def _execute_signal(self, signal):
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
            logger.warning(f"Order not placed for {symbol}")

    # ------------------------------------------------------------------
    # KITE TICKER
    # ------------------------------------------------------------------
    def start_ticker(self):
        try:
            access_token = self.kite.access_token
            api_key      = self.kite.api_key
            self.ticker  = KiteTicker(api_key, access_token)

            self.ticker.on_ticks   = self._on_ticks
            self.ticker.on_connect = self._on_connect
            self.ticker.on_close   = self._on_close
            self.ticker.on_error   = self._on_error

            self.ticker.connect(threaded=True)
            logger.info("KiteTicker connected (threaded)")

            self.notifier.send(
                "📡 <b>Market Open — Live Tick Feed Connected</b>\n"
                f"Watching: {', '.join(s.replace('NSE:','') for s in self.watchlist[:5])}\n"
                f"Regime: {self._current_regime.trend if self._current_regime else 'UNKNOWN'} | "
                f"VIX: {self._india_vix:.1f}\n"
                f"Time: {datetime.now().strftime('%H:%M IST')}"
            )

        except Exception as e:
            logger.error(f"KiteTicker start failed: {e}")
            self.notifier.notify_error("KiteTicker", str(e))

    def _on_connect(self, ws, response):
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
            self._daily_reset, 'cron', hour=9, minute=0, id='daily_reset'
        )
        self.scheduler.add_job(
            self._eod_close, 'cron', hour=15, minute=15, id='eod_close'
        )
        self.scheduler.add_job(
            self._end_of_day, 'cron', hour=15, minute=30, id='end_of_day'
        )
        self.scheduler.start()
        logger.info("Scheduler started")

    def _daily_reset(self):
        """
        BUG 5 FIX: Clear self.watchlist so pre_market_setup() runs every day.
        Also resets all per-day cached state.
        """
        logger.info("Scheduler: daily reset triggered")

        # BUG 5 FIX: Must clear watchlist — otherwise pre_market_setup() never
        # runs on day 2+ because run() checks `if not self.watchlist`.
        self.watchlist = []
        self._orb_set.clear()
        self._nifty_daily  = None
        self._india_vix    = 15.0
        self._current_regime = None

        # BUG 10 FIX: Get a fresh DailyJournal for the new day
        self.daily_journal = reset_journal()

        # Reset all sub-system daily state
        self.candle_builder.reset_daily()
        self.risk_manager.reset_daily()

        # BUG 1 FIX: reset ai_hybrid per-day state (clears ORB, trade_taken, etc.)
        # setup_day() will be called properly in pre_market_setup() with real data
        # For now just reset strategy internal state without regime data
        self.ai_hybrid.orb.reset_daily()
        self.ai_hybrid.vwap.reset_daily()
        self.ai_hybrid.breakout.reset_daily()

        logger.info("Daily reset complete — watchlist cleared, awaiting pre_market_setup")

    def _eod_close(self):
        logger.info("Scheduler: EOD force close triggered")
        self.notifier.notify_force_close()
        self.candle_builder.force_close_all()
        self.order_manager.force_close_all(reason="EOD_15:15")

    def _end_of_day(self):
        """
        BUG 6+7+10 FIX:
        - Generate DailyJournal markdown report
        - Read real wins/losses from exit log
        - Pass journal_path to notify_eod_summary so file is attached
        """
        logger.info("Scheduler: end-of-day wrap-up")
        state = self.risk_manager.get_state_snapshot()
        self.journal.log_daily_summary(state)

        # BUG 7+10 FIX: Generate the daily journal markdown and get its path
        try:
            journal_path = self.daily_journal.generate_report(
                daily_pnl=state.get('daily_pnl', 0),
                total_trades=state.get('trades_today', 0)
            )
        except Exception as e:
            logger.error(f"Journal generate_report failed: {e}")
            journal_path = None

        # BUG 6 FIX: Build wins/losses from exit log, not risk manager snapshot
        exit_log    = getattr(self.daily_journal, 'exit_log', [])
        wins        = sum(1 for e in exit_log if e.get('pnl', 0) > 0)
        losses      = sum(1 for e in exit_log if e.get('pnl', 0) <= 0)
        state['wins']   = wins
        state['losses'] = losses

        self.notifier.notify_eod_summary(
            status=state,
            journal_path=journal_path,
            dry_run=PAPER_TRADE_MODE
        )

    # ------------------------------------------------------------------
    # FIX 14a RETAINED: MAIN RUN LOOP
    # ------------------------------------------------------------------
    def run(self):
        logger.info("Starting main loop...")
        self._schedule_jobs()
        self._running = True

        signal.signal(signal.SIGINT,  self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        while self._running:
            try:
                now = datetime.now(IST).time()

                if t(9, 0) <= now < t(15, 20):
                    # BUG 5 FIX: watchlist is cleared in _daily_reset() every morning
                    # so this correctly re-runs pre_market_setup() each day
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
                self.notifier.notify_error("main loop", str(e))
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

        self.notifier.send("⚡ <b>System shut down.</b> All positions force-closed.")
        logger.info("Shutdown complete")

    def _get_avg_volume(self, symbol: str) -> float:
        """
        TODO: Cache real 20-day average volumes per symbol during pre_market_setup.
        Currently returns a safe default. This affects ORB and Breakout ATR
        volume filters — they'll use proportional thresholds but won't be
        stock-specific until this is wired to real data.
        """
        return 1_000_000.0


# ================================================================
#  SCAN-ONLY MODE
# ================================================================
def run_scan_only():
    logger.info("=== SCAN-ONLY MODE ===")
    notifier = get_notifier()

    try:
        kite_login = KiteLogin()
        kite       = kite_login.get_kite_instance()
        notifier.send(
            "🔍 <b>Scan-Only Mode</b>\n"
            "✅ Zerodha login successful.\n"
            "Running pre-market scanner..."
        )
    except Exception as e:
        notifier.notify_login_failed(str(e))
        raise

    scanner    = PreMarketScanner(kite)
    candidates = scanner.run(top_n=5)
    scanner.print_report(candidates)

    regime = MarketRegime("RANGE", "NORMAL_VOL")
    notifier.notify_scanner_results(candidates, regime)
    logger.info("Scan complete. Exiting.")


# ================================================================
#  ENTRY POINT
# ================================================================
if __name__ == "__main__":

    if BACKTEST:
        logger.info("=== BACKTEST MODE ===")
        try:
            from backtest.backtest_engine import BacktestEngine, run_all_strategy_comparison
            import numpy as np

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

            demo_df   = pd.DataFrame({
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

    if SCAN_ONLY:
        run_scan_only()
        sys.exit(0)

    system = AIHybridTradingSystem()
    system.run()
