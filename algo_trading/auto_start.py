#!/usr/bin/env python3
# ============================================================
#  auto_start.py
#  Scheduled launcher — runs automatically every weekday at 8:55 AM
#  via Windows Task Scheduler (or cron on Linux/Mac)
#
#  FIX: Market hours guard added.
#    If triggered outside the trading window (before 8:50 AM or
#    after 15:30 IST), the script exits immediately WITHOUT
#    launching main.py and WITHOUT attempting Zerodha login.
#    This prevents OTP prompts when accidentally triggered
#    outside market hours (e.g. manual test runs in the evening).
#
#  What it does:
#    1. Checks if today is a trading day (Mon–Fri, not holiday)
#    2. Checks if current IST time is within the trading window
#    3. Sends Telegram: "System starting..."
#    4. Launches main.py --dry-run (or main.py for live)
#    5. Handles crashes and notifies you via Telegram
# ============================================================

import os
import sys
import subprocess
from datetime import datetime, date, time as dt_time

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

IST = ZoneInfo("Asia/Kolkata")

# ----------------------------------------------------------------
# TRADING WINDOW — IST
# ----------------------------------------------------------------
# Script is allowed to launch main.py only within this window.
# Before 8:50 AM: too early — market not open yet, scanner not useful
# After 15:30 PM: market closed — no point logging in at all
TRADING_WINDOW_START = dt_time(8, 50)   # 8:50 AM IST
TRADING_WINDOW_END   = dt_time(15, 30)  # 3:30 PM IST


def is_within_trading_window() -> tuple:
    """
    Returns (bool, str) — (is_within_window, reason_message)
    Uses IST timezone for comparison.
    """
    now_ist  = datetime.now(IST)
    now_time = now_ist.time()

    if now_time < TRADING_WINDOW_START:
        return False, (
            f"Too early — current time {now_time.strftime('%H:%M')} IST "
            f"is before trading window start ({TRADING_WINDOW_START.strftime('%H:%M')} IST)."
        )

    if now_time > TRADING_WINDOW_END:
        return False, (
            f"Market closed — current time {now_time.strftime('%H:%M')} IST "
            f"is after trading window end ({TRADING_WINDOW_END.strftime('%H:%M')} IST).\n"
            f"System will run tomorrow at 8:55 AM."
        )

    return True, "Within trading window ✓"


# ----------------------------------------------------------------
# NSE TRADING HOLIDAYS 2026 — update as needed
# ----------------------------------------------------------------
NSE_HOLIDAYS_2026 = {
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 25),   # Holi
    date(2026, 4, 2),    # Ram Navami
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 8, 15),   # Independence Day
    date(2026, 10, 2),   # Gandhi Jayanti
    date(2026, 10, 20),  # Diwali Laxmi Pujan
    date(2026, 11, 5),   # Diwali Balipratipada
    date(2026, 12, 25),  # Christmas
}


def is_trading_day(today: date = None) -> bool:
    today = today or date.today()
    if today.weekday() >= 5:   # Saturday or Sunday
        return False
    if today in NSE_HOLIDAYS_2026:
        return False
    return True


def send_telegram(message: str):
    """Send Telegram message — never crashes the launcher even if Telegram fails"""
    try:
        from utils.telegram_notifier import get_notifier
        get_notifier().send(message)
    except Exception as e:
        print(f"[Telegram] Could not send: {e}")


def main():
    today = date.today()
    now   = datetime.now(IST)

    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')} IST] auto_start.py triggered")

    # ── Check 1: Is today a trading day? ────────────────────────────
    if not is_trading_day(today):
        msg = (
            f"Today ({today.strftime('%A %d %b')}) is not a trading day. Exiting."
        )
        print(msg)
        send_telegram(
            f"📅 <b>No Trading Today</b>\n"
            f"{today.strftime('%A, %d %b %Y')} is a holiday or weekend.\n"
            f"System will resume next trading day."
        )
        return

    print(f"Today is a trading day ✓")

    # ── Check 2: Are we within the trading window? ───────────────────
    # THIS IS THE KEY FIX — prevents OTP prompt and login attempts
    # when the script is triggered outside market hours.
    in_window, window_msg = is_within_trading_window()

    if not in_window:
        print(f"\n⏰ OUTSIDE TRADING WINDOW: {window_msg}")
        print("Exiting without logging in to Zerodha. No OTP will be requested.\n")

        send_telegram(
            f"⏰ <b>Auto-Start: Outside Trading Window</b>\n\n"
            f"{window_msg}\n\n"
            f"No Zerodha login was attempted.\n"
            f"System will auto-start at 8:55 AM on the next trading day."
        )
        return

    print(f"Time check: {window_msg}")

    # ── Determine mode ───────────────────────────────────────────────
    # Change LIVE_MODE = True when you're ready to go live
    LIVE_MODE = False
    mode_flag = "" if LIVE_MODE else "--dry-run"
    mode_str  = "LIVE TRADING 💰" if LIVE_MODE else "PAPER TRADE 📄"

    send_telegram(
        f"⏰ <b>Auto-Start Triggered</b>\n\n"
        f"Date: {today.strftime('%d %b %Y (%A)')}\n"
        f"Time: {now.strftime('%H:%M IST')}\n"
        f"Mode: {mode_str}\n"
        f"Launching system now...\n\n"
        f"Commands: /stop /status /help"
    )

    # ── Launch main.py ───────────────────────────────────────────────
    python_exe = sys.executable
    script     = os.path.join(PROJECT_ROOT, "main.py")

    cmd = [python_exe, script]
    if mode_flag:
        cmd.append(mode_flag)

    print(f"Launching: {' '.join(cmd)}")

    try:
        result = subprocess.run(cmd, cwd=PROJECT_ROOT)

        if result.returncode == 0:
            print("main.py completed successfully ✓")
        else:
            print(f"ERROR: main.py exited with code {result.returncode}")
            send_telegram(
                f"❌ <b>System Exited Unexpectedly</b>\n\n"
                f"Exit code: {result.returncode}\n"
                f"Time: {datetime.now(IST).strftime('%H:%M IST')}\n\n"
                f"⚠️ Check logs for details."
            )

    except KeyboardInterrupt:
        print("Manual shutdown requested")
        send_telegram("⚡ System manually shut down (Ctrl+C)")

    except Exception as e:
        print(f"ERROR: {e}")
        send_telegram(
            f"❌ <b>System Crashed</b>\n\n"
            f"Error: {str(e)}\n"
            f"Time: {datetime.now(IST).strftime('%H:%M IST')}\n\n"
            f"⚠️ Check your computer. Positions may be open!\n"
            f"Log in to Zerodha app immediately to check."
        )


if __name__ == "__main__":
    main()


# ================================================================
#  WINDOWS TASK SCHEDULER SETUP
# ================================================================
#
#  Option A — run the helper script (easiest):
#    python setup_windows_task.py
#
#  Option B — manual setup:
#    1. Open Task Scheduler (search in Start Menu)
#    2. Click "Create Basic Task"
#    3. Name: "AlgoTrading"
#    4. Trigger: Daily, 8:55 AM, Recur every 1 day
#    5. Action: Start a program
#       Program:   C:\Python314\python.exe   ← your Python path
#       Arguments: C:\Users\ashok\Downloads\algo_trading\algo_trading\auto_start.py
#       Start in:  C:\Users\ashok\Downloads\algo_trading\algo_trading\
#    6. Finish
#    7. Properties → Conditions → uncheck "only if AC power"
#    8. Properties → Settings → check "Run task as soon as possible
#       after a scheduled start is missed"
#
#  IMPORTANT: Set Windows Power & Sleep → "Never" when plugged in
#  so PC doesn't sleep at 8:55 AM.
#
# ================================================================
