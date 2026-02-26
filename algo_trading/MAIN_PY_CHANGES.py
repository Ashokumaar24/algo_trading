# ============================================================
#  TELEGRAM INTEGRATION PATCH for main.py
#  Shows exactly what to add/change in your existing main.py
#
#  Search for each "# TELEGRAM:" comment and add the line below it.
#  There are 10 additions total — takes about 5 minutes.
# ============================================================

# ── CHANGE 1: Add import at top of main.py ──────────────────────
# Add this line next to your other imports:

from utils.telegram_notifier import get_notifier


# ── CHANGE 2: In TradingSystem.__init__() ───────────────────────
# Add after `self.journal = reset_journal()`:

#   self.notifier = get_notifier()
#   self.notifier.start_command_listener(trading_system=self)


# ── CHANGE 3: In pre_market_setup() — after successful login ────
# Add after logger.info("System initialised successfully"):
#   (Put this at the END of __init__, after notifier setup)

#   self.notifier.notify_startup(dry_run=self.dry_run)


# ── CHANGE 4: In pre_market_setup() — after scanner runs ────────
# Add after `self.scanner.print_report(self.selected_stocks)`:

#   if self.strategy._current_regime is not None:
#       self.notifier.notify_scanner_results(
#           self.selected_stocks, self.strategy._current_regime
#       )
#   if self.strategy._current_regime and not self.strategy._current_regime.is_tradeable:
#       self.notifier.notify_regime_blocked(self.strategy._current_regime)


# ── CHANGE 5: In on_candle_close() — after risk gate check ──────
# Add inside the `if not self.risk_manager.can_trade():` block:

#   # Check emergency stop from Telegram /stop command
#   if self.notifier.is_stop_requested():
#       return
#   reason = self.risk_manager.get_block_reason()
#   self.notifier.notify_risk_gate(reason)   # ← add this line


# ── CHANGE 6: In on_candle_close() — after order placed ─────────
# Add after `self.journal.log_trade_placed(signal, dry_run=self.dry_run)`:

#   self.notifier.notify_signal(signal, dry_run=self.dry_run)


# ── CHANGE 7: In force_close_all() ──────────────────────────────
# Add at start of force_close_all():

#   self.notifier.notify_force_close()


# ── CHANGE 8: In end_of_day() ───────────────────────────────────
# Replace `report_path = self.journal.generate_report(...)` block with:

#   report_path = self.journal.generate_report(
#       daily_pnl=status['daily_pnl'],
#       total_trades=status['trades_today']
#   )
#   # Count wins/losses from journal exit log
#   wins   = len([e for e in self.journal.exit_log if e['pnl'] > 0])
#   losses = len([e for e in self.journal.exit_log if e['pnl'] <= 0])
#   self.notifier.notify_eod_summary(
#       status={**status, 'wins': wins, 'losses': losses},
#       journal_path=report_path,
#       dry_run=self.dry_run
#   )


# ── CHANGE 9: In order_manager._record_closed_position() ────────
# Add after `get_journal().log_trade_exit(...)`:

#   from utils.telegram_notifier import get_notifier
#   get_notifier().notify_trade_exit(
#       symbol=signal.symbol,
#       pnl=round(pnl, 2),
#       exit_reason=reason,
#       entry=signal.entry,
#       exit_price=round(exit_price, 2),
#       hold_mins=hold_mins
#   )


# ── CHANGE 10: In main() — wrap system startup in try/except ────
# Wrap the TradingSystem() init:

#   try:
#       system = TradingSystem(dry_run=args.dry_run)
#   except Exception as e:
#       from utils.telegram_notifier import get_notifier
#       get_notifier().notify_login_failed(str(e))
#       raise
