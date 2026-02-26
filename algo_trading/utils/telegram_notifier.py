# ============================================================
#  utils/telegram_notifier.py
#  Telegram Bot — Trading system notifications
#
#  Sends you:
#    ✅ Daily startup confirmation
#    📊 Pre-market scanner results
#    🌍 Market regime classification
#    🚨 Every signal that fires (entry/SL/target)
#    🚫 Why trades were blocked (regime, cap, etc.)
#    📓 End-of-day journal summary
#    ❌ Any system errors
#
#  You can send commands back:
#    /status  → current positions + P&L
#    /stop    → emergency halt (no new trades)
#    /resume  → re-enable trading after /stop
#    /journal → get today's journal early
#
#  Setup (one time, 5 minutes, FREE):
#    1. Open Telegram → search @BotFather
#    2. Send /newbot → follow prompts → get BOT_TOKEN
#    3. Search your new bot → click Start
#    4. Visit: https://api.telegram.org/bot{BOT_TOKEN}/getUpdates
#    5. Send any message to your bot, refresh the URL
#    6. Copy the "id" from "chat" section → that's your CHAT_ID
#    7. Add both to api_key.txt (lines 6 and 7)
# ============================================================

import requests
import os
import time
import threading
from datetime import datetime
from typing import Optional
from utils.logger import get_logger

logger = get_logger("telegram")

# ----------------------------------------------------------------
# SINGLETON
# ----------------------------------------------------------------
_notifier_instance = None

def get_notifier() -> 'TelegramNotifier':
    global _notifier_instance
    if _notifier_instance is None:
        _notifier_instance = TelegramNotifier()
    return _notifier_instance


# ----------------------------------------------------------------
# TELEGRAM NOTIFIER
# ----------------------------------------------------------------
class TelegramNotifier:
    """
    Telegram bot for trading system notifications.
    Uses only the requests library (already in requirements.txt).
    No additional packages needed.
    """

    BASE_URL = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self):
        self.token   = None
        self.chat_id = None
        self.enabled = False

        self._stop_requested  = False
        self._last_update_id  = 0
        self._command_thread: Optional[threading.Thread] = None
        self._trading_system  = None

        self._load_credentials()

    def _load_credentials(self):
        """Load Telegram credentials from api_key.txt (lines 6 and 7)"""
        try:
            cred_file = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "api_key.txt"
            )
            with open(cred_file, 'r') as f:
                lines = [l.strip() for l in f.readlines()]

            if len(lines) >= 7:
                token   = lines[5]
                chat_id = lines[6]
                if token and chat_id and token != 'YOUR_TELEGRAM_BOT_TOKEN':
                    self.token   = token
                    self.chat_id = chat_id
                    self.enabled = True
                    logger.info("Telegram notifier enabled ✓")
                else:
                    logger.info("Telegram credentials not set — notifications disabled")
            else:
                logger.info("api_key.txt has < 7 lines — Telegram disabled")

        except Exception as e:
            logger.warning(f"Telegram credentials load failed: {e}")
            self.enabled = False

    # ------------------------------------------------------------------
    # CORE SEND METHODS
    # ------------------------------------------------------------------
    def send(self, message: str, parse_mode: str = "HTML") -> bool:
        """Send a message to your Telegram chat"""
        if not self.enabled:
            return False
        try:
            url  = self.BASE_URL.format(token=self.token, method="sendMessage")
            resp = requests.post(url, json={
                "chat_id":    self.chat_id,
                "text":       message,
                "parse_mode": parse_mode,
            }, timeout=10)
            return resp.status_code == 200
        except Exception as e:
            logger.warning(f"Telegram send failed: {e}")
            return False

    def send_document(self, file_path: str, caption: str = "") -> bool:
        """Send a file (journal markdown) to your Telegram chat"""
        if not self.enabled or not os.path.exists(file_path):
            return False
        try:
            url = self.BASE_URL.format(token=self.token, method="sendDocument")
            with open(file_path, 'rb') as f:
                resp = requests.post(url, data={
                    "chat_id": self.chat_id,
                    "caption": caption,
                }, files={"document": f}, timeout=30)
            return resp.status_code == 200
        except Exception as e:
            logger.warning(f"Telegram file send failed: {e}")
            return False

    # ------------------------------------------------------------------
    # COMMAND LISTENER (background thread)
    # ------------------------------------------------------------------
    def start_command_listener(self, trading_system=None):
        """Start background thread to listen for /stop /status /resume commands"""
        if not self.enabled:
            return
        self._trading_system = trading_system
        self._command_thread = threading.Thread(
            target=self._poll_commands, daemon=True
        )
        self._command_thread.start()
        logger.info("Telegram command listener started")

    def _poll_commands(self):
        """Poll for new messages every 5 seconds"""
        while True:
            try:
                url  = self.BASE_URL.format(token=self.token, method="getUpdates")
                resp = requests.get(url, params={
                    "offset":  self._last_update_id + 1,
                    "timeout": 5,
                }, timeout=15)

                if resp.status_code == 200:
                    for update in resp.json().get("result", []):
                        self._last_update_id = update["update_id"]
                        self._handle_update(update)

            except Exception as e:
                logger.debug(f"Telegram poll error: {e}")
            time.sleep(5)

    def _handle_update(self, update: dict):
        """Process incoming command"""
        msg  = update.get("message", {})
        text = msg.get("text", "").strip().lower()
        chat = str(msg.get("chat", {}).get("id", ""))

        if chat != str(self.chat_id):
            return

        logger.info(f"Telegram command received: {text}")

        if text == "/stop":
            self._stop_requested = True
            self.send("🛑 <b>EMERGENCY STOP activated.</b>\nNo new trades will be placed.\n"
                      "Existing positions will still be monitored and closed at 3:15 PM.\n"
                      "Send /resume to re-enable.")

        elif text == "/resume":
            self._stop_requested = False
            self.send("✅ <b>Trading resumed.</b> System will take new signals again.")

        elif text == "/status":
            self._send_status()

        elif text == "/journal":
            self._send_current_journal()

        elif text == "/help":
            self.send(
                "📱 <b>Available Commands:</b>\n\n"
                "/status  — Current positions + today's P&amp;L\n"
                "/stop    — Emergency halt (no new trades)\n"
                "/resume  — Re-enable trading after /stop\n"
                "/journal — Get today's journal now\n"
                "/help    — Show this message"
            )

    def _send_status(self):
        try:
            if self._trading_system:
                status = self._trading_system.risk_manager.get_status()
                stop_status = "🛑 STOPPED (use /resume)" if self._stop_requested else "✅ ACTIVE"
                self.send(
                    f"📊 <b>System Status</b>\n\n"
                    f"Mode: {stop_status}\n"
                    f"Trades today: {status['trades_today']}/2\n"
                    f"Open positions: {status['open_positions']}\n"
                    f"Daily P&amp;L: ₹{status['daily_pnl']:+,.0f}\n"
                    f"Consecutive losses: {status['consecutive_losses']}\n"
                    f"Can trade: {'Yes ✅' if status['can_trade'] and not self._stop_requested else 'No 🚫'}"
                )
            else:
                self.send("⚠️ Trading system not yet initialised.")
        except Exception as e:
            self.send(f"⚠️ Status error: {e}")

    def _send_current_journal(self):
        try:
            from utils.daily_journal import get_journal
            journal = get_journal()
            if os.path.exists(journal.report_path):
                self.send_document(journal.report_path, caption="📓 Today's trading journal")
            else:
                path = journal.generate_report()
                self.send_document(path, caption="📓 Journal generated on demand")
        except Exception as e:
            self.send(f"⚠️ Journal error: {e}")

    def is_stop_requested(self) -> bool:
        return self._stop_requested

    # ------------------------------------------------------------------
    # TRADING EVENT NOTIFICATIONS
    # ------------------------------------------------------------------
    def notify_startup(self, dry_run: bool = True):
        mode = "📄 PAPER TRADE (dry-run)" if dry_run else "💰 LIVE TRADING"
        self.send(
            f"🤖 <b>Algo Trading System Started</b>\n\n"
            f"Mode: {mode}\n"
            f"Time: {datetime.now().strftime('%d %b %Y, %H:%M IST')}\n\n"
            f"Login: ✅ Zerodha connected\n"
            f"Next: Pre-market scanner at 9:05 AM\n\n"
            f"Commands: /status /stop /journal /help"
        )

    def notify_login_failed(self, error: str):
        self.send(
            f"❌ <b>LOGIN FAILED</b>\n\n"
            f"Error: {error}\n"
            f"Time: {datetime.now().strftime('%H:%M IST')}\n\n"
            f"⚠️ System cannot trade today. Please check manually."
        )

    def notify_scanner_results(self, candidates: list, regime):
        if not candidates:
            self.send("📊 Scanner complete — no candidates found today.")
            return

        regime_str = f"{regime.trend} + {regime.volatility}" if regime else "Unknown"
        tradeable  = "✅ Tradeable" if (regime and regime.is_tradeable) else "🚫 NOT Tradeable"

        lines = [f"🔍 <b>Pre-Market Scanner Results</b>"]
        lines.append(f"Regime: {regime_str} {tradeable}\n")

        for i, c in enumerate(candidates[:5], 1):
            bias_icon = "🟢" if getattr(c, 'bias', '') == 'BULLISH' else "🔴" if getattr(c, 'bias', '') == 'BEARISH' else "⚪"
            lines.append(
                f"{i}. {bias_icon} <b>{c.symbol.replace('NSE:','')}</b> "
                f"| Score: {c.score:.0f} "
                f"| Gap: {c.gap_pct*100:+.2f}% "
                f"| Conf: {c.confidence:.0f}"
            )

        if regime and not regime.is_tradeable:
            lines.append(f"\n⚠️ <b>No trades today</b> — regime not suitable")

        self.send("\n".join(lines))

    def notify_signal(self, signal, dry_run: bool = True):
        mode = "📄 DRY RUN" if dry_run else "💰 LIVE ORDER"
        direction_icon = "📈" if "LONG" in str(signal.direction) else "📉"
        risk = abs(signal.entry - signal.stop_loss)
        reward = abs(signal.target - signal.entry)

        self.send(
            f"🚨 <b>SIGNAL FIRED — {mode}</b>\n\n"
            f"{direction_icon} <b>{signal.symbol.replace('NSE:','')} {signal.direction.value}</b>\n\n"
            f"Strategy:   {signal.strategy}\n"
            f"Entry:      ₹{signal.entry:.2f}\n"
            f"Stop Loss:  ₹{signal.stop_loss:.2f} (risk: ₹{risk:.2f}/share)\n"
            f"Target:     ₹{signal.target:.2f} (reward: ₹{reward:.2f}/share)\n"
            f"R:R Ratio:  {signal.reward_risk:.2f}x\n"
            f"Confidence: {signal.confidence:.0f}/100\n"
            f"Regime:     {signal.regime}\n\n"
            f"Time: {datetime.now().strftime('%H:%M IST')}"
        )

    def notify_trade_exit(self, symbol: str, pnl: float, exit_reason: str,
                           entry: float, exit_price: float, hold_mins: int):
        outcome = "✅ WIN" if pnl > 0 else "❌ LOSS"
        exit_labels = {
            'TARGET_HIT':     "🎯 Target Hit",
            'SL_HIT':         "🛑 Stop Loss Hit",
            'TIME_EXIT_1230': "⏰ Time Exit (12:30 PM)",
            'EOD_CLOSE':      "🔔 End of Day Close",
            'FORCE_CLOSE':    "⚡ Force Closed",
        }
        exit_label = exit_labels.get(exit_reason, exit_reason)

        self.send(
            f"{outcome} <b>Trade Closed — {symbol.replace('NSE:','')}</b>\n\n"
            f"Exit reason: {exit_label}\n"
            f"Entry:  ₹{entry:.2f}\n"
            f"Exit:   ₹{exit_price:.2f}\n"
            f"P&amp;L:    ₹{pnl:+,.0f}\n"
            f"Held:   {hold_mins} minutes\n"
            f"Time:   {datetime.now().strftime('%H:%M IST')}"
        )

    def notify_regime_blocked(self, regime):
        self.send(
            f"🚫 <b>Market Regime: NO TRADES TODAY</b>\n\n"
            f"Regime: {regime.trend} + {regime.volatility}\n"
            f"ADX: {regime.adx:.1f} | VIX: {regime.india_vix:.1f}\n\n"
            f"Reason: This market condition historically loses money.\n"
            f"System will sit out today. This is correct behaviour. ✅"
        )

    def notify_risk_gate(self, reason: dict):
        self.send(
            f"🛡️ <b>Risk Gate Triggered</b>\n\n"
            f"<b>{reason['short']}</b>\n\n"
            f"{reason['detail']}\n\n"
            f"No new trades for the rest of the day."
        )

    def notify_eod_summary(self, status: dict, journal_path: str = None,
                            dry_run: bool = True):
        mode  = "Paper Trade" if dry_run else "Live Trade"
        wins  = status.get('wins', 0)
        losses = status.get('losses', 0)
        total  = wins + losses
        wr     = f"{wins/total*100:.0f}%" if total > 0 else "N/A"

        self.send(
            f"📓 <b>End of Day Summary — {mode}</b>\n"
            f"<i>{datetime.now().strftime('%d %b %Y')}</i>\n\n"
            f"Trades placed:  {status.get('trades_today', 0)}\n"
            f"Wins:           {wins}\n"
            f"Losses:         {losses}\n"
            f"Win Rate:       {wr}\n"
            f"Daily P&amp;L:     ₹{status.get('daily_pnl', 0):+,.0f}\n"
            f"Weekly P&amp;L:    ₹{status.get('weekly_pnl', 0):+,.0f}\n\n"
            f"Journal saved to logs/ ✅"
        )

        if journal_path and os.path.exists(journal_path):
            time.sleep(1)
            self.send_document(journal_path, caption=f"📓 Full journal — {datetime.now().strftime('%d %b %Y')}")

    def notify_error(self, context: str, error: str):
        self.send(
            f"⚠️ <b>System Error</b>\n\n"
            f"Where: {context}\n"
            f"Error: {error}\n"
            f"Time:  {datetime.now().strftime('%H:%M IST')}\n\n"
            f"System will attempt to continue. Check logs for details."
        )

    def notify_force_close(self):
        self.send(
            f"🔔 <b>3:15 PM — Force Closing All Positions</b>\n"
            f"Market closes in 15 minutes. All open positions being closed."
        )
