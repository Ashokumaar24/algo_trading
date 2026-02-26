# ============================================================
#  main.py
#  AI-Powered Intraday Trading System — Main Orchestrator
#  Zerodha KiteConnect | Nifty50 | Intraday Only
#
#  Run:  python main.py
#  Run (paper trade): python main.py --dry-run
#  Run (backtest):    python main.py --backtest
#
#  ADDED: DailyJournal hooks — logs every decision + reason to
#         logs/journal_YYYY-MM-DD.md at end of day
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
from utils.daily_journal        import get_journal, reset_journal   # ← ADDED
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

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        logger.info(f"Initialising Trading System | {'DRY RUN MODE' if dry_run else 'LIVE MODE'}")

        self.kite   = get_kite_session(headless=True)
        self.ticker = None

        self.scanner           = PreMarketScanner(self.kite)
        self.strategy          = AIHybridStrategy()
        self.regime_classifier = MarketRegimeClassifier()
        self.risk_manager      = RiskManager(capital=CAPITAL)
        self.order_manager     = OrderManager(self.kite, self.risk_manager)
        self.candle_builder    = CandleBuilder(interval_minutes=5)

        # ADDED: fresh journal for today
        self.journal = reset_journal()

        self.selected_stocks:    list = []
        self.instrument_map:     dict = {}
        self.volume_tracker:     dict = {}
        self.avg_volume_20d:     dict = {}
        self.prev_day_data:      dict = {}
        self.nifty_5min_candles: list = []
        self.nifty_daily_df: Optional[object] = None
        self.india_vix:          float = 15.0

        self._candle_915: dict = {}
        self._orb_set:    set  = set()

        self.candle_builder.set_callback(self.on_candle_close)

        logger.info("System initialised successfully")

    # ------------------------------------------------------------------
    # PRE-MARKET SETUP (9:05 AM)
    # ------------------------------------------------------------------
    def pre_market_setup(self):
        logger.info("=" * 60)
        logger.info("PRE-MARKET SETUP STARTING")
        logger.info("=" * 60)

        self.risk_manager.reset_daily()
        self.candle_builder.reset_daily()
        self.volume_tracker.clear()

        # Fetch Nifty daily
        logger.info("Fetching Nifty50 daily data...")
        self.nifty_daily_df = self._fetch_daily_history("NSE:NIFTY 50", days=250)

        # India VIX
        self.india_vix = self._get_india_vix()
        logger.info(f"India VIX: {self.india_vix:.2f}")

        # ADDED: note VIX in journal
        self.journal.add_note(f"India VIX at open: {self.india_vix:.2f}")

        # Run scanner
        logger.info("Running pre-market scanner...")
        self.selected_stocks = self.scanner.run(top_n=5)
        self.scanner.print_report(self.selected_stocks)

        # ADDED: log scanner results to journal
        self.journal.log_scanner_results(self.selected_stocks)

        # Fetch historical data for selected stocks
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

        # Setup AI Hybrid + classify regime
        if self.nifty_daily_df is not None:
            self.strategy.setup_day(
                nifty_daily=self.nifty_daily_df,
                india_vix=self.india_vix,
                prev_day_data=self.prev_day_data
            )

        # ADDED: log regime to journal after setup_day classified it
        if self.strategy._current_regime is not None:
            regime = self.strategy._current_regime
            eligible = self.regime_classifier.get_eligible_strategies(regime)
            self.journal.log_regime(regime, eligible)

            # ADDED: note if market is not tradeable
            if not regime.is_tradeable:
                self.journal.add_note(
                    f"⚠️ Market regime {regime.trend}+{regime.volatility} "
                    f"is NOT tradeable — system will skip all entries today"
                )

        self._build_instrument_map()
        self._start_ticker()

        logger.info("Pre-market setup complete ✓")
        logger.info("=" * 60)

    # ------------------------------------------------------------------
    # TICK HANDLER
    # ------------------------------------------------------------------
    def on_tick(self, ws, ticks: list):
        for tick in ticks:
            try:
                sym = self._token_to_symbol(tick['instrument_token'])
                if sym:
                    tick['tradingsymbol'] = sym
                    self.candle_builder.process_tick(tick)
                    vol = tick.get('volume', 0)
                    if vol > 0:
                        self.volume_tracker[sym] = vol
            except Exception as e:
                logger.debug(f"Tick processing error: {e}")

    # ------------------------------------------------------------------
    # CANDLE CLOSE HANDLER
    # ------------------------------------------------------------------
    def on_candle_close(self, symbol: str, candle, history):
        try:
            now = datetime.now()
            candle_dict = candle.to_dict() if hasattr(candle, 'to_dict') else candle
            candle_price = candle_dict.get('close', 0)

            # Set ORB at 9:30 AM
            if now.time() >= ORB_READY and symbol not in self._orb_set:
                if symbol in self._candle_915 and len(history) >= 2:
                    self.strategy.set_orb(
                        symbol,
                        self._candle_915[symbol],
                        history.iloc[0].to_dict()
                    )
                    self._orb_set.add(symbol)

            if now.time() < ORB_READY and symbol not in self._candle_915:
                self._candle_915[symbol] = candle_dict

            # Monitor existing positions
            prices = {symbol: candle_price}
            self.order_manager.monitor_positions(prices)

            # --- Check risk gate BEFORE getting signal ---
            if not self.risk_manager.can_trade():
                reason = self.risk_manager.get_block_reason()  # see updated risk_manager
                self.journal.log_trade_blocked(
                    symbol=symbol,
                    strategy="ALL",
                    block_type=reason['type'],
                    reason=reason['short'],
                    detail=reason['detail'],
                    candle_price=candle_price
                )
                return

            avg_vol   = self.avg_volume_20d.get(symbol, 1_000_000)
            cum_vol   = self.volume_tracker.get(symbol, 0)

            signal = self.strategy.get_signal(
                symbol=symbol,
                candle=candle_dict,
                candle_history=history,
                avg_volume_20d=avg_vol,
                cum_volume_today=cum_vol,
                avg_daily_volume=avg_vol,
                sentiment_score=0.0,
                nifty_5min=None,
                india_vix=self.india_vix
            )

            if signal:
                logger.info(f"Signal received: {signal}")

                regime = self.strategy._current_regime
                signal.position_size = self.risk_manager.calculate_position_size(
                    signal,
                    size_multiplier=regime.size_multiplier if regime else 1.0
                )

                order_id = self.order_manager.place_order(
                    signal, regime=regime, dry_run=self.dry_run
                )

                if order_id:
                    # ADDED: log successful trade to journal
                    self.journal.log_trade_placed(signal, dry_run=self.dry_run)
                    logger.info(
                        f"{'[DRY RUN] ' if self.dry_run else ''}"
                        f"Order placed: {order_id} | {signal.symbol} "
                        f"{signal.direction.value} | {signal.strategy}"
                    )
            else:
                # ADDED: log that no signal was generated this candle
                # Only log for symbols that are in our watchlist (avoid spam)
                if symbol in self.avg_volume_20d:
                    self.journal.log_trade_blocked(
                        symbol=symbol,
                        strategy=self._get_active_strategy_name(),
                        block_type="NO_SIGNAL",
                        reason="Strategy conditions not met",
                        candle_price=candle_price
                    )

        except Exception as e:
            logger.error(f"on_candle_close error [{symbol}]: {e}", exc_info=True)

    def _get_active_strategy_name(self) -> str:
        """Get current eligible strategy names for logging"""
        if self.strategy._current_regime:
            strategies = self.regime_classifier.get_eligible_strategies(
                self.strategy._current_regime
            )
            return "+".join(strategies) if strategies else "NONE"
        return "UNKNOWN"

    # ------------------------------------------------------------------
    # FORCE CLOSE (3:15 PM)
    # ------------------------------------------------------------------
    def force_close_all(self):
        logger.warning("3:15 PM — FORCE CLOSING ALL POSITIONS")
        # ADDED: note in journal
        self.journal.add_note("3:15 PM — Force close triggered, all positions being closed")
        self.candle_builder.force_close_all()
        if not self.dry_run:
            self.order_manager.force_close_all()
        logger.warning("All positions closed.")

    # ------------------------------------------------------------------
    # END OF DAY (3:30 PM)
    # ------------------------------------------------------------------
    def end_of_day(self):
        status = self.risk_manager.get_status()
        logger.info("=" * 60)
        logger.info("END OF DAY SUMMARY")
        logger.info("=" * 60)
        logger.info(f"  Trades today:  {status['trades_today']}")
        logger.info(f"  Daily P&L:    ₹{status['daily_pnl']:+,.0f}")
        logger.info(f"  Weekly P&L:   ₹{status['weekly_pnl']:+,.0f}")
        logger.info(f"  Consec losses: {status['consecutive_losses']}")
        logger.info("=" * 60)

        # ADDED: add final P&L note and generate journal report
        self.journal.add_note(
            f"EOD Summary — Trades: {status['trades_today']} | "
            f"Daily P&L: ₹{status['daily_pnl']:+,.0f} | "
            f"Consecutive losses: {status['consecutive_losses']}"
        )
        report_path = self.journal.generate_report(
            daily_pnl=status['daily_pnl'],
            total_trades=status['trades_today']
        )
        logger.info(f"📓 Journal saved: {report_path}")

        if self.ticker:
            self.ticker.stop()

    # ------------------------------------------------------------------
    # WEBSOCKET / TICKER SETUP
    # ------------------------------------------------------------------
    def _start_ticker(self):
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
        time.sleep(2)
        logger.info(f"Ticker started | Subscribed: {len(tokens)} instruments")

    def _build_instrument_map(self):
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
        except Exception as e:
            logger.error(f"Instrument map build failed: {e}")

    def _token_to_symbol(self, token: int) -> Optional[str]:
        for sym, tok in self.instrument_map.items():
            if tok == token:
                return sym
        return None

    def _fetch_daily_history(self, symbol: str, days: int):
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
        try:
            ltp = self.kite.ltp("NSE:INDIA VIX")
            return float(ltp.get("NSE:INDIA VIX", {}).get("last_price", 15.0))
        except Exception:
            return 15.0


# ================================================================
#  SCHEDULER SETUP
# ================================================================
def setup_schedule(system: TradingSystem):
    schedule.every().day.at("09:05").do(system.pre_market_setup)
    schedule.every().day.at("15:15").do(system.force_close_all)
    schedule.every().day.at("15:30").do(system.end_of_day)

    logger.info("Scheduled tasks:")
    logger.info("  09:05 — Pre-market setup + scanner + regime")
    logger.info("  15:15 — Force close all positions")
    logger.info("  15:30 — End of day summary + journal saved")


# ================================================================
#  MAIN
# ================================================================
def main():
    parser = argparse.ArgumentParser(
        description="AI-Powered Intraday Trading System | Nifty50"
    )
    parser.add_argument('--dry-run',   action='store_true')
    parser.add_argument('--backtest',  action='store_true')
    parser.add_argument('--scan-only', action='store_true')
    args = parser.parse_args()

    if args.backtest:
        logger.info("Running backtest comparison...")
        from backtest.backtest_engine import run_all_strategy_comparison
        import numpy as np, pandas as pd
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

    mode = "DRY RUN (Paper Trading)" if args.dry_run else "LIVE TRADING"
    logger.info("=" * 60)
    logger.info(f"  ALGO TRADING SYSTEM — {mode}")
    logger.info(f"  Started: {datetime.now().strftime('%d %b %Y %H:%M:%S IST')}")
    logger.info("=" * 60)

    system = TradingSystem(dry_run=args.dry_run)

    if args.scan_only:
        logger.info("SCAN ONLY MODE")
        system.pre_market_setup()
        system.journal.generate_report()
        return

    setup_schedule(system)

    now = datetime.now().time()
    from datetime import time as t
    if t(9, 0) <= now <= t(9, 20):
        system.pre_market_setup()
    elif t(9, 20) < now < t(15, 20):
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
