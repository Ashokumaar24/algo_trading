#!/usr/bin/env python3
# ============================================================
#  auto_start.py
#  Scheduled launcher — runs automatically every weekday at 8:55 AM
#  via Windows Task Scheduler (or cron on Linux/Mac)
#
#  What it does:
#    1. Checks if today is a trading day (Mon–Fri, not holiday)
#    2. Sends Telegram: "System starting..."
#    3. Launches main.py --dry-run (or main.py for live)
#    4. Handles crashes and notifies you via Telegram
# ============================================================

import os
import sys
import subprocess
from datetime import datetime, date

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

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
    now   = datetime.now()

    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] auto_start.py triggered")

    # ── Check if trading day ─────────────────────────────────────────
    if not is_trading_day(today):
        print(f"Today ({today.strftime('%A %d %b')}) is not a trading day. Exiting.")
        send_telegram(
            f"📅 <b>No Trading Today</b>\n"
            f"{today.strftime('%A, %d %b %Y')} is a holiday or weekend.\n"
            f"System will resume next trading day."
        )
        return

    print(f"Today is a trading day ✓")

    # ── Determine mode ───────────────────────────────────────────────
    # Change LIVE_MODE = True when you're ready to go live
    LIVE_MODE = False
    mode_flag = "" if LIVE_MODE else "--dry-run"
    mode_str  = "LIVE TRADING 💰" if LIVE_MODE else "PAPER TRADE 📄"

    send_telegram(
        f"⏰ <b>Auto-Start Triggered</b>\n\n"
        f"Date: {today.strftime('%d %b %Y (%A)')}\n"
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
                f"Time: {datetime.now().strftime('%H:%M IST')}\n\n"
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
            f"Time: {datetime.now().strftime('%H:%M IST')}\n\n"
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
