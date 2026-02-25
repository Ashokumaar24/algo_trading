# ============================================================
#  main.py
#  AI-Powered Intraday Trading System — Main Orchestrator
#  Zerodha KiteConnect | Nifty50 | Intraday Only
#
#  Run:  python main.py
#  Run (paper trade): python main.py --dry-run
#  Run (backtest):    python main.py --backtest
# ============================================================

import sys
import os
import time
import argparse
import schedule
import threading
from datetime import datetime, date, timedelta
from typing import Dict, Optional, List

from kiteconnect import KiteTicker

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auth.login       import get_kite_session
from scanner.pre_market_scanner import PreMarketScanner
from strategies.ai_hybrid       import AIHybridStrategy
from regime.market_regime       import MarketRegimeClassifier
from risk.risk_manager          import RiskManager
from execution.order_manager    import OrderManager
from utils.candle_builder       import CandleBuilder
from utils.logger               import get_logger
from config.config              import (
    CAPITAL, NIFTY50_SYMBOLS,
    FORCE_CLOSE_TIME, MARKET_OPEN, ORB_READY
)

logger = get_logger("main")


# ================================================================
#  TRADING SYSTEM
# ================================================================
class TradingSystem:
    """
    Main trading system orchestrator.

    Lifecycle:
    1. pre_market_setup()   — 9:05 AM: scan, regime, subscribe ticks
    2. on_tick()            — Real-time tick processing
    3. on_candle_close()    — 5-min candle close: signal check
    4. force_close_all()    — 3:15 PM: close all positions
    5. end_of_day()         — 3:30 PM: logging, reset
    """

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        logger.info(f"Initialising Trading System | {'DRY RUN MODE' if dry_run else 'LIVE MODE'}")

        # --- KiteConnect session ---
        self.kite   = get_kite_session(headless=True)
        self.ticker = None  # Initialised after pre-market setup

        # --- Core modules ---
        self.scanner          = PreMarketScanner(self.kite)
        self.strategy         = AIHybridStrategy()
        self.regime_classifier = MarketRegimeClassifier()
        self.risk_manager     = RiskManager(capital=CAPITAL)
        self.order_manager    = OrderManager(self.kite, self.risk_manager)
        self.candle_builder   = CandleBuilder(interval_minutes=5)

        # --- State ---
        self.selected_stocks: list = []       # Top 5 from pre-market scan
        self.instrument_map:  dict = {}       # symbol → instrument_token
        self.volume_tracker:  dict = {}       # symbol → cumulative volume today
        self.avg_volume_20d:  dict = {}       # symbol → 20-day avg volume
        self.prev_day_data:   dict = {}       # symbol → {high, low, close}
        self.nifty_5min_candles: list = []    # For intraday regime updates
        self.nifty_daily_df: Optional[object] = None
        self.india_vix:       float = 15.0

        # --- ORB state ---
        self._candle_915:     dict = {}       # symbol → 9:15 candle
        self._orb_set:        set  = set()

        # --- Callbacks ---
        self.candle_builder.set_callback(self.on_candle_close)

        logger.info("System initialised successfully")

    # ------------------------------------------------------------------
    # PRE-MARKET SETUP (9:05 AM)
    # ------------------------------------------------------------------
    def pre_market_setup(self):
        """
        Full pre-market preparation:
        - Run stock scanner
        - Fetch historical data
        - Classify market regime
        - Setup AI Hybrid strategy
        - Subscribe to ticks
        """
        logger.info("=" * 60)
        logger.info("PRE-MARKET SETUP STARTING")
        logger.info("=" * 60)

        # --- 1. Reset daily state ---
        self.risk_manager.reset_daily()
        self.candle_builder.reset_daily()
        self.volume_tracker.clear()

        # --- 2. Fetch Nifty daily data for regime classification ---
        logger.info("Fetching Nifty50 daily data...")
        self.nifty_daily_df = self._fetch_daily_history("NSE:NIFTY 50", days=250)

        # --- 3. Get India VIX ---
        self.india_vix = self._get_india_vix()
        logger.info(f"India VIX: {self.india_vix:.2f}")

        # --- 4. Run pre-market scanner ---
        logger.info("Running pre-market scanner...")
        self.selected_stocks = self.scanner.run(top_n=5)
        self.scanner.print_report(self.selected_stocks)

        # --- 5. Fetch historical data for selected stocks ---
        logger.info("Fetching historical data for selected stocks...")
        for candidate in self.selected_stocks:
            sym = candidate.symbol
            hist = self._fetch_daily_history(sym, days=25)
            if hist is not None and len(hist) >= 2:
                self.avg_volume_20d[sym] = float(hist['volume'].tail(20).mean())
                prev = hist.iloc[-1]
                self.prev_day_data[sym] = {
                    'high':  float(prev['high']),
                    'low':   float(prev['low']),
                    'close': float(prev['close']),
                }
                logger.info(
                    f"  {sym} | Avg Vol:{self.avg_volume_20d[sym]:,.0f} | "
                    f"PrevHigh:{self.prev_day_data[sym]['high']:.2f}"
                )

        # --- 6. Setup AI Hybrid with regime and prev-day data ---
        if self.nifty_daily_df is not None:
            self.strategy.setup_day(
                nifty_daily=self.nifty_daily_df,
                india_vix=self.india_vix,
                prev_day_data=self.prev_day_data
            )
        else:
            logger.warning("Nifty daily data not available — regime defaulting to RANGE/NORMAL")

        # --- 7. Build instrument token map ---
        self._build_instrument_map()

        # --- 8. Start WebSocket ticker ---
        self._start_ticker()

        logger.info("Pre-market setup complete ✓")
        logger.info("=" * 60)

    # ------------------------------------------------------------------
    # TICK HANDLER
    # ------------------------------------------------------------------
    def on_tick(self, ws, ticks: list):
        """
        KiteTicker on_ticks callback.
        Forwards each tick to candle builder for aggregation.
        """
        for tick in ticks:
            try:
                sym = self._token_to_symbol(tick['instrument_token'])
                if sym:
                    tick['tradingsymbol'] = sym
                    self.candle_builder.process_tick(tick)

                    # Track cumulative volume
                    vol = tick.get('volume', 0)
                    if vol > 0:
                        self.volume_tracker[sym] = vol

            except Exception as e:
                logger.debug(f"Tick processing error: {e}")

    # ------------------------------------------------------------------
    # CANDLE CLOSE HANDLER (main decision point)
    # ------------------------------------------------------------------
    def on_candle_close(self, symbol: str, candle, history):
        """
        Called by CandleBuilder on every completed 5-min candle.
        This is where all trading decisions are made.
        """
        try:
            now = datetime.now()
            candle_dict = candle.to_dict() if hasattr(candle, 'to_dict') else candle

            # --- Set ORB at 9:30 AM ---
            if now.time() >= ORB_READY and symbol not in self._orb_set:
                if symbol in self._candle_915 and len(history) >= 2:
                    self.strategy.set_orb(
                        symbol,
                        self._candle_915[symbol],
                        history.iloc[0].to_dict()
                    )
                    self._orb_set.add(symbol)

            # Store 9:15 candle
            if now.time() < ORB_READY and symbol not in self._candle_915:
                self._candle_915[symbol] = candle_dict

            # --- Guard: max trades, time, daily loss ---
            if not self.risk_manager.can_trade():
                return

            # --- Monitor existing positions ---
            prices = {symbol: candle_dict.get('close', 0)}
            self.order_manager.monitor_positions(prices)

            # --- Get signal from AI Hybrid ---
            avg_vol      = self.avg_volume_20d.get(symbol, 1_000_000)
            cum_vol      = self.volume_tracker.get(symbol, 0)
            avg_daily    = avg_vol

            signal = self.strategy.get_signal(
                symbol=symbol,
                candle=candle_dict,
                candle_history=history,
                avg_volume_20d=avg_vol,
                cum_volume_today=cum_vol,
                avg_daily_volume=avg_daily,
                sentiment_score=0.0,    # TODO: hook up sentiment engine
                nifty_5min=None,        # TODO: pass Nifty 5-min data
                india_vix=self.india_vix
            )

            if signal:
                logger.info(f"Signal received: {signal}")

                # Get regime for position sizing
                regime = self.strategy._current_regime
                signal.position_size = self.risk_manager.calculate_position_size(
                    signal,
                    size_multiplier=regime.size_multiplier if regime else 1.0
                )

                # Place order
                order_id = self.order_manager.place_order(
                    signal, regime=regime, dry_run=self.dry_run
                )

                if order_id:
                    logger.info(
                        f"{'[DRY RUN] ' if self.dry_run else ''}"
                        f"Order placed: {order_id} | {signal.symbol} "
                        f"{signal.direction.value} | {signal.strategy}"
                    )

        except Exception as e:
            logger.error(f"on_candle_close error [{symbol}]: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # FORCE CLOSE (3:15 PM)
    # ------------------------------------------------------------------
    def force_close_all(self):
        """Hard close all positions. Must be called at 3:15 PM."""
        logger.warning("3:15 PM — FORCE CLOSING ALL POSITIONS")
        self.candle_builder.force_close_all()
        if not self.dry_run:
            self.order_manager.force_close_all()
        logger.warning("All positions closed.")

    # ------------------------------------------------------------------
    # END OF DAY (3:30 PM)
    # ------------------------------------------------------------------
    def end_of_day(self):
        """Daily wrap-up: log summary, print P&L"""
        status = self.risk_manager.get_status()
        logger.info("=" * 60)
        logger.info("END OF DAY SUMMARY")
        logger.info("=" * 60)
        logger.info(f"  Trades today:  {status['trades_today']}")
        logger.info(f"  Daily P&L:    ₹{status['daily_pnl']:+,.0f}")
        logger.info(f"  Weekly P&L:   ₹{status['weekly_pnl']:+,.0f}")
        logger.info(f"  Consec losses: {status['consecutive_losses']}")
        logger.info("=" * 60)

        # Stop ticker
        if self.ticker:
            self.ticker.stop()

    # ------------------------------------------------------------------
    # WEBSOCKET / TICKER SETUP
    # ------------------------------------------------------------------
    def _start_ticker(self):
        """Start KiteTicker WebSocket for real-time data"""
        from config.config import TOKEN_FILE
        with open(TOKEN_FILE, 'r') as f:
            keys = f.read().split()
        api_key = keys[0]

        from auth.login import ACCESS_TOKEN_FILE
        with open(ACCESS_TOKEN_FILE, 'r') as f:
            access_token = f.read().strip()

        self.ticker = KiteTicker(api_key, access_token)

        tokens = list(self.instrument_map.values())

        def on_connect(ws, response):
            logger.info("WebSocket connected — subscribing to ticks...")
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)

        def on_close(ws, code, reason):
            logger.warning(f"WebSocket closed: {code} — {reason}")

        def on_error(ws, code, reason):
            logger.error(f"WebSocket error: {code} — {reason}")

        self.ticker.on_ticks   = self.on_tick
        self.ticker.on_connect = on_connect
        self.ticker.on_close   = on_close
        self.ticker.on_error   = on_error

        ticker_thread = threading.Thread(
            target=self.ticker.connect, kwargs={'threaded': True}
        )
        ticker_thread.daemon = True
        ticker_thread.start()
        time.sleep(2)   # Allow connection to establish
        logger.info(f"Ticker started | Subscribed: {len(tokens)} instruments")

    def _build_instrument_map(self):
        """Build {symbol: instrument_token} map for selected stocks"""
        try:
            instruments = self.kite.instruments("NSE")
            import pandas as pd
            inst_df = pd.DataFrame(instruments)

            for candidate in self.selected_stocks:
                ticker_sym = candidate.symbol.replace("NSE:", "")
                row = inst_df[inst_df['tradingsymbol'] == ticker_sym]
                if not row.empty:
                    token = int(row.iloc[0]['instrument_token'])
                    self.instrument_map[candidate.symbol] = token
                    logger.info(f"  Token mapped: {candidate.symbol} → {token}")

        except Exception as e:
            logger.error(f"Instrument map build failed: {e}")

    def _token_to_symbol(self, token: int) -> Optional[str]:
        """Reverse lookup: instrument token → symbol"""
        for sym, tok in self.instrument_map.items():
            if tok == token:
                return sym
        return None

    def _fetch_daily_history(self, symbol: str, days: int):
        """Fetch daily OHLCV from KiteConnect"""
        try:
            import pandas as pd
            ticker = symbol.replace("NSE:", "")
            instruments = self.kite.instruments("NSE")
            inst_df = pd.DataFrame(instruments)
            row = inst_df[inst_df['tradingsymbol'] == ticker]
            if row.empty:
                return None
            token = int(row.iloc[0]['instrument_token'])

            to_date   = datetime.now()
            from_date = to_date - timedelta(days=days + 10)

            data = self.kite.historical_data(token, from_date, to_date, 'day')
            if not data:
                return None

            df = pd.DataFrame(data)
            df.rename(columns={'date': 'timestamp'}, inplace=True)
            df.set_index('timestamp', inplace=True)
            return df.tail(days)

        except Exception as e:
            logger.error(f"History fetch error for {symbol}: {e}")
            return None

    def _get_india_vix(self) -> float:
        """Fetch India VIX from KiteConnect"""
        try:
            ltp = self.kite.ltp("NSE:INDIA VIX")
            return float(ltp.get("NSE:INDIA VIX", {}).get("last_price", 15.0))
        except Exception:
            return 15.0  # default neutral


# ================================================================
#  SCHEDULER SETUP
# ================================================================
def setup_schedule(system: TradingSystem):
    """Schedule all daily tasks"""
    schedule.every().day.at("09:05").do(system.pre_market_setup)
    schedule.every().day.at("15:15").do(system.force_close_all)
    schedule.every().day.at("15:30").do(system.end_of_day)

    logger.info("Scheduled tasks:")
    logger.info("  09:05 — Pre-market setup")
    logger.info("  15:15 — Force close all positions")
    logger.info("  15:30 — End of day summary")


# ================================================================
#  MAIN
# ================================================================
def main():
    parser = argparse.ArgumentParser(
        description="AI-Powered Intraday Trading System | Nifty50"
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Paper trading mode — no real orders placed'
    )
    parser.add_argument(
        '--backtest', action='store_true',
        help='Run backtest comparison and exit'
    )
    parser.add_argument(
        '--scan-only', action='store_true',
        help='Run pre-market scan and exit (for testing)'
    )
    args = parser.parse_args()

    # ---- Backtest mode ----
    if args.backtest:
        logger.info("Running backtest comparison...")
        from backtest.backtest_engine import run_all_strategy_comparison
        import numpy as np, pandas as pd

        # Generate demo data (replace with real data for actual backtest)
        np.random.seed(42)
        dates  = pd.date_range('2023-01-02 09:15', periods=3000, freq='5min')
        dates  = dates[dates.indexer_between_time('09:15', '15:30')]
        n      = len(dates)
        price  = 2500.0
        prices = [price]
        for _ in range(n - 1):
            price += np.random.normal(0, 4)
            prices.append(max(price, 100))

        close = pd.Series(prices, index=dates[:n])
        high  = close + abs(pd.Series(np.random.normal(0, 3, n), index=dates[:n]))
        low   = close - abs(pd.Series(np.random.normal(0, 3, n), index=dates[:n]))
        vol   = pd.Series(np.random.randint(50000, 300000, n), index=dates[:n])
        df    = pd.DataFrame({'open': close.shift(1).fillna(close),
                               'high': high, 'low': low, 'close': close, 'volume': vol})
        run_all_strategy_comparison({"NSE:RELIANCE": df})
        return

    # ---- Trading system ----
    mode = "DRY RUN (Paper Trading)" if args.dry_run else "LIVE TRADING"
    logger.info("=" * 60)
    logger.info(f"  ALGO TRADING SYSTEM — {mode}")
    logger.info(f"  Started: {datetime.now().strftime('%d %b %Y %H:%M:%S IST')}")
    logger.info("=" * 60)

    system = TradingSystem(dry_run=args.dry_run)

    # ---- Scan-only mode ----
    if args.scan_only:
        logger.info("SCAN ONLY MODE")
        system.pre_market_setup()
        return

    # ---- Schedule and run ----
    setup_schedule(system)

    # If started during market hours, run setup immediately
    now = datetime.now().time()
    from datetime import time as t
    if t(9, 0) <= now <= t(9, 20):
        logger.info("Market opening soon — running pre-market setup immediately...")
        system.pre_market_setup()
    elif t(9, 20) < now < t(15, 20):
        logger.info("Market is open — running pre-market setup immediately...")
        system.pre_market_setup()

    logger.info("Scheduler running... Press Ctrl+C to stop.")
    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        logger.info("Shutdown requested by user.")
        system.force_close_all()
        system.end_of_day()


if __name__ == "__main__":
    main()
