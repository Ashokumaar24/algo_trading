# ============================================================
#  main.py  — AI Hybrid Algo Trading System
#
#  BUGS FIXED IN THIS VERSION:
#
#  BUG 1–15 (previous sessions): see inline comments
#
#  FIX (this session): Market hours guard added.
#    When main.py is launched outside the trading window
#    (before 8:50 AM or after 15:30 IST), it prints a clear
#    message and exits BEFORE attempting Zerodha login.
#    This prevents OTP prompts, browser launches, and wasted
#    Zerodha API sessions when the script is triggered manually
#    or by Task Scheduler outside market hours.
#    Applies to --dry-run and live mode.
#    Does NOT apply to --backtest or --scan-only (those don't
#    need real-time market access and can run any time).
#
#  BUG 11 FIXED: _get_avg_volume() was always returning 1_000_000.
#  BUG 12 FIXED: Paper trade SL/target never triggered intraday.
#  BUG 13 FIXED: on_exit_callback wired into OrderManager.
#  BUG 14 FIXED: regime not passed to notify_eod_summary.
#  BUG 15 FIXED: ai_hybrid.reset_daily() called properly.
#  BUG 16 FIXED: pre_market_setup() guard — won't re-run if scanner
#    returns empty list (prevents repeated API hammering).
#  BUG 17 FIXED: NO_SIGNAL blocks now logged to DailyJournal.
# ============================================================

import sys
import os
import signal
import time
import argparse
import pandas as pd
from datetime import datetime, time as t, timedelta
from typing import Optional, Dict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_parser = argparse.ArgumentParser(description="AI Hybrid Algo Trading System")
_parser.add_argument('--dry-run',   action='store_true', default=False)
_parser.add_argument('--backtest',  action='store_true', default=False)
_parser.add_argument('--scan-only', action='store_true', default=False)
_args = _parser.parse_args()

DRY_RUN   = _args.dry_run
BACKTEST  = _args.backtest
SCAN_ONLY = _args.scan_only

if DRY_RUN:
    print("[DRY-RUN] Paper trade mode — real Zerodha login, simulated orders")

# ================================================================
#  MARKET HOURS GUARD — check BEFORE any imports that trigger login
# ================================================================
# Backtest and scan-only don't need real-time market access.
# Only paper trade (--dry-run) and live mode need this guard.
if not BACKTEST and not SCAN_ONLY:
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    _IST = ZoneInfo("Asia/Kolkata")
    _now_ist  = datetime.now(_IST)
    _now_time = _now_ist.time()

    _WINDOW_START = t(8, 50)   # 8:50 AM IST — don't login before this
    _WINDOW_END   = t(15, 30)  # 3:30 PM IST — market is closed after this

    if _now_time > _WINDOW_END:
        print(
            f"\n{'='*60}\n"
            f"  MARKET CLOSED — System will not start.\n"
            f"{'='*60}\n"
            f"  Current time : {_now_ist.strftime('%H:%M IST on %d %b %Y')}\n"
            f"  Market hours : {_WINDOW_START.strftime('%H:%M')} – "
            f"{_WINDOW_END.strftime('%H:%M')} IST\n\n"
            f"  No Zerodha login was attempted. No OTP was requested.\n"
            f"  Run again tomorrow during market hours, or use:\n"
            f"    python main.py --backtest    (runs anytime)\n"
            f"    python main.py --scan-only   (requires login, use in hours)\n"
            f"{'='*60}\n"
        )
        # Send Telegram notification if possible (notifier loads without login)
        try:
            from utils.telegram_notifier import get_notifier
            get_notifier().send(
                f"⏰ <b>main.py: Market Closed — Not Starting</b>\n\n"
                f"Triggered at {_now_ist.strftime('%H:%M IST')} — after 15:30 cutoff.\n"
                f"No Zerodha login attempted. No OTP requested.\n"
                f"System will auto-start next trading day at 8:55 AM."
            )
        except Exception:
            pass  # Telegram failure must not block clean exit
        sys.exit(0)

    if _now_time < _WINDOW_START:
        print(
            f"\n{'='*60}\n"
            f"  TOO EARLY — System will not start yet.\n"
            f"{'='*60}\n"
            f"  Current time : {_now_ist.strftime('%H:%M IST on %d %b %Y')}\n"
            f"  Trading starts: {_WINDOW_START.strftime('%H:%M')} IST\n\n"
            f"  No Zerodha login was attempted. No OTP was requested.\n"
            f"{'='*60}\n"
        )
        sys.exit(0)

# ================================================================
#  REST OF IMPORTS — only reached if within trading window
# ================================================================
from kiteconnect import KiteTicker
from apscheduler.schedulers.background import BackgroundScheduler
from zoneinfo import ZoneInfo

from auth.login import KiteLogin
from scanner.pre_market_scanner import PreMarketScanner
from strategies.ai_hybrid import AIHybridStrategy
from regime.market_regime import MarketRegimeClassifier, MarketRegime
from utils.candle_builder import CandleBuilder
from execution.order_manager import OrderManager
from risk.risk_manager import RiskManager
from utils.journal import TradeJournal
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


class AIHybridTradingSystem:

    def __init__(self):
        logger.info("=" * 60)
        logger.info("  AI HYBRID TRADING SYSTEM — Starting up")
        logger.info("=" * 60)

        self.notifier = get_notifier()

        try:
            logger.info(
                f"Connecting to Zerodha... "
                f"({'PAPER TRADE' if DRY_RUN else 'LIVE TRADING'})"
            )
            kite_login = KiteLogin()
            self.kite  = kite_login.get_kite_instance()
            self.notifier.notify_startup(dry_run=DRY_RUN)
        except Exception as e:
            self.notifier.notify_login_failed(str(e))
            raise

        self.scanner        = PreMarketScanner(self.kite)
        self.candle_builder = CandleBuilder(interval_minutes=5)
        self.risk_manager   = RiskManager(capital=INITIAL_CAPITAL)
        self.journal        = TradeJournal()

        # BUG 13 FIX: Wire on_exit_callback so paper exits update risk+journal
        self.order_manager  = OrderManager(
            self.kite,
            paper_trade=PAPER_TRADE_MODE,
            on_exit_callback=self._on_trade_exit
        )

        self.ai_hybrid = AIHybridStrategy()

        self.watchlist        = []
        self.ticker           = None
        self.scheduler        = None
        self._running         = False
        self._orb_set: set    = set()

        self._nifty_daily: Optional[pd.DataFrame] = None
        self._india_vix: float                    = 15.0
        self._current_regime: Optional[MarketRegime] = None

        # BUG 11 FIX: Cache real avg volumes per symbol
        self._avg_volumes: Dict[str, float] = {}

        # BUG 16 FIX: Guard against repeated pre_market_setup() calls
        # when scanner returns empty list (prevents API hammering)
        self._pre_market_done: bool = False

        # Token map cache — reused on websocket reconnects (BUG 3 FIX)
        self._token_map: Dict[str, int] = {}

        self.daily_journal = get_daily_journal()

        self.notifier._trading_system = self
        self.notifier.start_command_listener(trading_system=self)

    # ------------------------------------------------------------------
    # BUG 13 FIX: Exit callback — updates risk_manager + daily_journal
    # ------------------------------------------------------------------
    def _on_trade_exit(self, symbol: str, pnl: float, exit_reason: str,
                       exit_price: float, hold_mins: int, order_info: dict):
        self.risk_manager.record_trade_exit(pnl)

        direction  = order_info.get('direction', '')
        entry      = order_info.get('entry_price', 0)
        strategy   = order_info.get('strategy', 'UNKNOWN')

        self.daily_journal.log_trade_exit(
            symbol=symbol, strategy=strategy, direction=direction,
            entry=entry, exit_price=exit_price,
            exit_reason=exit_reason, pnl=pnl, hold_mins=hold_mins,
        )

        self.notifier.notify_trade_exit(
            symbol=symbol, pnl=pnl, exit_reason=exit_reason,
            entry=entry, exit_price=exit_price, hold_mins=hold_mins,
        )

        logger.info(
            f"Trade exit processed | {symbol} {direction} | "
            f"PnL:₹{pnl:+,.0f} | Reason:{exit_reason}"
        )

    # ------------------------------------------------------------------
    # PRE-MARKET SETUP (9:05 AM)
    # ------------------------------------------------------------------
    def pre_market_setup(self):
        logger.info("Running pre-market setup...")

        try:
            try:
                vix_data        = self.kite.ltp(["NSE:INDIA VIX"])
                self._india_vix = float(vix_data["NSE:INDIA VIX"]["last_price"])
                logger.info(f"India VIX: {self._india_vix:.2f}")
            except Exception as e:
                logger.warning(f"VIX fetch failed ({e}) — using {self._india_vix:.1f}")

            self._nifty_daily = self._fetch_nifty_daily()

            classifier = MarketRegimeClassifier()
            if self._nifty_daily is not None and len(self._nifty_daily) >= 200:
                self._current_regime = classifier.classify(
                    self._nifty_daily, self._india_vix
                )
                eligible = classifier.get_eligible_strategies(self._current_regime)
            else:
                logger.warning("Insufficient Nifty daily data — defaulting RANGE/NORMAL_VOL")
                self._current_regime = MarketRegime(
                    "RANGE", "NORMAL_VOL",
                    adx=0.0, india_vix=self._india_vix
                )
                eligible = ["VWAP_PULLBACK"]

            logger.info(f"Regime: {self._current_regime}")

            candidates     = self.scanner.run(top_n=5)
            self.watchlist = [c.symbol for c in candidates]
            logger.info(f"Watchlist: {self.watchlist}")

            # BUG 11 FIX: Cache real avg volumes using already-loaded instrument master
            self._cache_avg_volumes()

            # BUG 3 FIX: Pre-build token map from cached instruments (no re-fetch on reconnect)
            if self.scanner._instruments_df is not None:
                self._token_map = {
                    row['tradingsymbol']: int(row['instrument_token'])
                    for _, row in self.scanner._instruments_df.iterrows()
                }
                logger.info(f"Token map cached: {len(self._token_map)} instruments ✓")

            prev_day_data = self._build_prev_day_data()
            self.ai_hybrid.setup_day(
                nifty_daily=self._nifty_daily if self._nifty_daily is not None
                            else pd.DataFrame(),
                india_vix=self._india_vix,
                prev_day_data=prev_day_data
            )

            self.daily_journal.log_regime(self._current_regime, eligible)
            self.daily_journal.log_scanner_results(candidates)
            self.daily_journal.add_note(f"India VIX at open: {self._india_vix:.2f}")

            self.notifier.notify_scanner_results(candidates, self._current_regime)

            self.candle_builder.reset_daily()
            self.risk_manager.reset_daily()
            self.candle_builder.set_callback(self.on_candle_close)
            self._orb_set.clear()

            if not self._current_regime.is_tradeable:
                self.notifier.notify_regime_blocked(self._current_regime)

            # BUG 16 FIX: Mark pre-market as done so the main loop
            # doesn't retry if scanner returns an empty watchlist.
            self._pre_market_done = True

        except Exception as e:
            logger.error(f"pre_market_setup failed: {e}")
            self.notifier.notify_error("pre_market_setup", str(e))
            # Do NOT set _pre_market_done = True on failure — allow one retry
            # on the next loop tick, but only for genuine exceptions (not empty results).
            # For persistent failures this will retry every 10s; acceptable.

    # ------------------------------------------------------------------
    # BUG 11 FIX: Cache real 20-day average volumes per symbol
    # Uses scanner's already-loaded instrument master — no extra API call
    # ------------------------------------------------------------------
    def _cache_avg_volumes(self):
        logger.info("Caching 20-day avg volumes for watchlist...")
        self._avg_volumes.clear()

        # BUG 1 FIX (audit): Reuse scanner's cached instrument master
        # instead of calling kite.instruments("NSE") per symbol in a loop.
        inst_df = getattr(self.scanner, '_instruments_df', None)
        if inst_df is None:
            logger.warning("Instrument master not cached — skipping avg volume cache")
            return

        for symbol in self.watchlist:
            try:
                clean = symbol.replace("NSE:", "")
                row   = inst_df[inst_df['tradingsymbol'] == clean]
                if row.empty:
                    continue

                token   = int(row.iloc[0]['instrument_token'])
                to_dt   = datetime.now()
                from_dt = to_dt - timedelta(days=30)
                hist    = self.kite.historical_data(token, from_dt, to_dt, 'day')

                if hist and len(hist) >= 5:
                    df      = pd.DataFrame(hist)
                    avg_vol = float(df['volume'].tail(20).mean())
                    self._avg_volumes[symbol] = avg_vol
                    logger.info(f"  {clean}: avg_vol = {avg_vol:,.0f}")

            except Exception as e:
                logger.debug(f"avg_volume fetch failed for {symbol}: {e}")

        logger.info(f"Avg volumes cached for {len(self._avg_volumes)} symbols ✓")

    def _get_avg_volume(self, symbol: str) -> float:
        return self._avg_volumes.get(symbol, 1_000_000.0)

    # ------------------------------------------------------------------
    # NIFTY DAILY DATA FETCH
    # Reuses scanner instrument master — no duplicate kite.instruments() call
    # ------------------------------------------------------------------
    def _fetch_nifty_daily(self) -> Optional[pd.DataFrame]:
        try:
            inst_df = getattr(self.scanner, '_instruments_df', None)
            if inst_df is not None:
                row = inst_df[inst_df['tradingsymbol'] == 'NIFTY 50']
            else:
                # Fallback if scanner hasn't run yet
                instruments = self.kite.instruments("NSE")
                inst_df     = pd.DataFrame(instruments)
                row         = inst_df[inst_df['tradingsymbol'] == 'NIFTY 50']

            if row.empty:
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

    def _build_prev_day_data(self) -> dict:
        prev_data = {}
        # BUG 1 FIX (audit): Reuse scanner's cached instrument master
        inst_df = getattr(self.scanner, '_instruments_df', None)
        if inst_df is None:
            return prev_data

        for symbol in self.watchlist:
            try:
                clean = symbol.replace("NSE:", "")
                row   = inst_df[inst_df['tradingsymbol'] == clean]
                if row.empty:
                    continue
                token   = int(row.iloc[0]['instrument_token'])
                to_dt   = datetime.now()
                from_dt = to_dt - timedelta(days=5)
                hist    = self.kite.historical_data(token, from_dt, to_dt, 'day')
                if hist and len(hist) >= 2:
                    prev = hist[-2]
                    prev_data[symbol] = {
                        'high': prev['high'], 'low': prev['low'], 'close': prev['close'],
                    }
            except Exception as e:
                logger.debug(f"prev_day_data failed for {symbol}: {e}")
        return prev_data

    # ------------------------------------------------------------------
    # CANDLE CLOSE CALLBACK
    # ------------------------------------------------------------------
    def on_candle_close(self, symbol: str, candle, history):
        try:
            candle_dict = candle.to_dict()

            candle_t = (candle.timestamp.time()
                        if hasattr(candle.timestamp, 'time')
                        else datetime.now().time())

            # BUG 12 FIX: Check paper exits FIRST, before looking for new signals.
            if PAPER_TRADE_MODE and candle_t >= t(9, 15):
                exit_result = self.order_manager.check_paper_exits(symbol, candle_dict)
                if exit_result:
                    logger.info(
                        f"Paper intraday exit | {symbol} | "
                        f"{exit_result['exit_reason']} | PnL:₹{exit_result['net_pnl']:+,.0f}"
                    )

            # Set ORB after 9:30 candle closes
            if candle_t >= t(9, 30) and symbol not in self._orb_set:
                self._try_set_orb(symbol, history)

            # Risk gate
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

            cum_vol  = float(history['volume'].sum()) if 'volume' in history.columns else 0.0
            signal   = self.ai_hybrid.get_signal(
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
            else:
                # BUG 17 FIX: Log NO_SIGNAL to DailyJournal so journal shows
                # accurate "conditions not met" counts (not always 0).
                # Guard: only log during active trading hours to avoid flooding.
                if t(9, 15) <= candle_t <= t(14, 0):
                    self.daily_journal.log_trade_blocked(
                        symbol=symbol,
                        strategy="ALL",
                        block_type="NO_SIGNAL",
                        reason="No signal conditions met",
                        detail=(
                            "Strategy scanned this 5-min candle but entry conditions "
                            "(VWAP pullback confirmation, ORB breakout, volume) "
                            "were not all met. This is normal and expected."
                        ),
                    )

        except Exception as e:
            logger.error(f"on_candle_close error for {symbol}: {e}")

    # ------------------------------------------------------------------
    # ORB SETUP
    # ------------------------------------------------------------------
    def _try_set_orb(self, symbol: str, history: pd.DataFrame):
        try:
            opening_candles = []
            for ts, row in history.iterrows():
                row_time = ts.time() if hasattr(ts, 'time') else None
                if row_time and t(9, 15) <= row_time < t(9, 30):
                    opening_candles.append({
                        'high': float(row['high']), 'low': float(row['low']),
                        'open': float(row['open']), 'close': float(row['close']),
                    })

            if opening_candles:
                self.ai_hybrid.set_orb(symbol, opening_candles)
                self._orb_set.add(symbol)
                logger.info(f"ORB set for {symbol} using {len(opening_candles)} candles ✓")

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

        clean_symbol = symbol.replace("NSE:", "")
        order_info = self.order_manager.place_order(
            symbol=clean_symbol,
            direction=direction,
            entry=entry, sl=sl, target=target, quantity=qty
        )

        if order_info:
            # BUG 8 FIX (audit): Store strategy before any potential exit callback
            order_info['strategy'] = signal.strategy

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
                f"VIX: {self._india_vix:.1f}"
            )

        except Exception as e:
            logger.error(f"KiteTicker start failed: {e}")
            self.notifier.notify_error("KiteTicker", str(e))

    def _on_connect(self, ws, response):
        """
        BUG 3 FIX (audit): Use pre-cached token map — no kite.instruments()
        call on every reconnect. Token map built once in pre_market_setup().
        """
        try:
            tokens = []
            for sym in self.watchlist:
                clean = sym.replace("NSE:", "")
                if clean in self._token_map:
                    tokens.append(self._token_map[clean])
                else:
                    logger.warning(f"Token not found in cache for {clean}")

            if tokens:
                ws.subscribe(tokens)
                ws.set_mode(ws.MODE_FULL, tokens)
                logger.info(f"Subscribed to {len(tokens)} instruments ✓")
            else:
                logger.warning("No tokens to subscribe — token map may be empty")
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
        self.scheduler.add_job(self._daily_reset,  'cron', hour=9,  minute=0,  id='daily_reset')
        self.scheduler.add_job(self._eod_close,    'cron', hour=15, minute=15, id='eod_close')
        self.scheduler.add_job(self._end_of_day,   'cron', hour=15, minute=30, id='end_of_day')
        self.scheduler.start()
        logger.info("Scheduler started")

    def _daily_reset(self):
        logger.info("Scheduler: daily reset triggered")

        self.watchlist = []
        self._orb_set.clear()
        self._nifty_daily    = None
        self._india_vix      = 15.0
        self._current_regime = None
        self._avg_volumes.clear()
        self._token_map.clear()

        # BUG 16 FIX: Reset pre_market_done flag so setup runs again today
        self._pre_market_done = False

        self.daily_journal = reset_journal()

        self.candle_builder.reset_daily()
        self.risk_manager.reset_daily()

        # BUG 15 FIX: Full reset including _regime_blocked_logged_today
        self.ai_hybrid.reset_daily()

        logger.info("Daily reset complete")

    def _eod_close(self):
        logger.info("Scheduler: EOD force close triggered")
        self.notifier.notify_force_close()
        self.candle_builder.force_close_all()
        self.order_manager.force_close_all(reason="EOD_15:15")

    def _end_of_day(self):
        logger.info("Scheduler: end-of-day wrap-up")
        state = self.risk_manager.get_state_snapshot()
        self.journal.log_daily_summary(state)

        # BUG 14 FIX: Include regime in EOD status for notify_eod_summary
        if self._current_regime:
            state['regime'] = (
                f"{self._current_regime.trend}+{self._current_regime.volatility} "
                f"(VIX {self._current_regime.india_vix:.1f})"
            )

        try:
            journal_path = self.daily_journal.generate_report(
                daily_pnl=state.get('daily_pnl', 0),
                total_trades=state.get('trades_today', 0)
            )
        except Exception as e:
            logger.error(f"Journal generate_report failed: {e}")
            journal_path = None

        exit_log = getattr(self.daily_journal, 'exit_log', [])
        wins     = sum(1 for e in exit_log if e.get('pnl', 0) > 0)
        losses   = sum(1 for e in exit_log if e.get('pnl', 0) <= 0)
        state['wins']   = wins
        state['losses'] = losses

        self.notifier.notify_eod_summary(
            status=state,
            journal_path=journal_path,
            dry_run=PAPER_TRADE_MODE
        )

    # ------------------------------------------------------------------
    # MAIN RUN LOOP
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
                    # BUG 16 FIX: Only run pre_market_setup once per day.
                    # Old code checked `if not self.watchlist` — if scanner returned
                    # 0 candidates, setup was retried every 10 seconds all morning,
                    # hammering the Kite API with repeated instrument downloads.
                    if not self._pre_market_done:
                        self.pre_market_setup()

                    if now >= t(9, 15) and self.ticker is None and self.watchlist:
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


# ================================================================
#  SCAN-ONLY MODE
# ================================================================
def run_scan_only():
    logger.info("=== SCAN-ONLY MODE ===")
    notifier = get_notifier()
    try:
        kite_login = KiteLogin()
        kite       = kite_login.get_kite_instance()
        notifier.send("🔍 <b>Scan-Only Mode</b>\n✅ Login OK. Running scanner...")
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
            print("  For real data: python backtest/fetch_and_backtest.py")
        except ImportError as e:
            logger.error(f"Backtest engine not available: {e}")
            sys.exit(1)
        sys.exit(0)

    if SCAN_ONLY:
        run_scan_only()
        sys.exit(0)

    system = AIHybridTradingSystem()
    system.run()
