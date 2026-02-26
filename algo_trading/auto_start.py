#!/usr/bin/env python3
# ============================================================
#  auto_start.py
#  Scheduled launcher — runs automatically every weekday at 8:55 AM
#  via cron (Linux/Mac) or Task Scheduler (Windows)
#
#  This script:
#    1. Checks if today is a trading day (Mon–Fri, not holiday)
#    2. Sends Telegram: "System starting..."
#    3. Launches main.py --dry-run (or main.py for live)
#    4. Handles crashes and notifies you via Telegram
#
#  Schedule setup at the bottom of this file.
# ============================================================

import os
import sys
import subprocess
from datetime import datetime, date

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

# ----------------------------------------------------------------
# NSE TRADING HOLIDAYS 2026 (add/remove as needed)
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
    date(2026, 10, 20),  # Diwali Laxmi Pujan (check NSE calendar)
    date(2026, 11, 5),   # Diwali Balipratipada (check NSE calendar)
    date(2026, 12, 25),  # Christmas
}


def is_trading_day(today: date = None) -> bool:
    """Returns True if today is a valid NSE trading day"""
    today = today or date.today()
    # Monday=0 ... Friday=4, Saturday=5, Sunday=6
    if today.weekday() >= 5:
        return False
    if today in NSE_HOLIDAYS_2026:
        return False
    return True


def send_telegram(message: str):
    """Send a Telegram message (safe — never crashes the launcher)"""
    try:
        sys.path.insert(0, PROJECT_ROOT)
        from utils.telegram_notifier import get_notifier
        notifier = get_notifier()
        notifier.send(message)
    except Exception as e:
        print(f"[Telegram] Could not send: {e}")


def main():
    today = date.today()
    now   = datetime.now()

    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] auto_start.py triggered")

    # ── Check if trading day ──────────────────────────────────
    if not is_trading_day(today):
        print(f"Today ({today.strftime('%A %d %b')}) is not a trading day. Exiting.")
        send_telegram(
            f"📅 <b>No Trading Today</b>\n"
            f"{today.strftime('%A, %d %b %Y')} is a holiday or weekend.\n"
            f"System will resume next trading day."
        )
        return

    print(f"Today is a trading day ✓")

    # ── Determine mode ────────────────────────────────────────
    # Change LIVE_MODE = True when you're ready to go live
    LIVE_MODE = False
    mode_flag = "" if LIVE_MODE else "--dry-run"
    mode_str  = "LIVE TRADING 💰" if LIVE_MODE else "PAPER TRADE 📄"

    send_telegram(
        f"⏰ <b>Auto-Start Triggered</b>\n\n"
        f"Date: {today.strftime('%d %b %Y (%A)')}\n"
        f"Mode: {mode_str}\n"
        f"Launching system in 10 seconds...\n\n"
        f"Commands available: /stop /status /help"
    )

    # ── Launch main.py ────────────────────────────────────────
    python_exe = sys.executable  # Use same Python that ran this script
    script     = os.path.join(PROJECT_ROOT, "main.py")

    cmd = [python_exe, script]
    if mode_flag:
        cmd.append(mode_flag)

    print(f"Launching: {' '.join(cmd)}")

    try:
        # Run main.py — this blocks until market closes (3:30 PM)
        result = subprocess.run(cmd, cwd=PROJECT_ROOT)

        if result.returncode == 0:
            print("main.py completed successfully ✓")
        else:
            raise RuntimeError(f"main.py exited with code {result.returncode}")

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
#  HOW TO SCHEDULE auto_start.py
# ================================================================
#
# ── LINUX / MAC (cron) ──────────────────────────────────────────
#
#  Open terminal and run:
#    crontab -e
#
#  Add this line (runs at 8:55 AM every weekday):
#    55 8 * * 1-5 /usr/bin/python3 /path/to/algo_trading/auto_start.py >> /path/to/algo_trading/logs/autostart.log 2>&1
#
#  Replace /path/to/algo_trading/ with your actual project path.
#  Find your Python path with: which python3
#
#  Example if project is at ~/algo_trading:
#    55 8 * * 1-5 /usr/bin/python3 ~/algo_trading/auto_start.py >> ~/algo_trading/logs/autostart.log 2>&1
#
#  To verify cron is set:
#    crontab -l
#
# ── WINDOWS (Task Scheduler) ────────────────────────────────────
#
#  Option A — Run the helper script:
#    python auto_start.py --setup-windows-task
#    (see setup_windows_task() below)
#
#  Option B — Manual setup:
#    1. Open Task Scheduler (search in Start Menu)
#    2. Click "Create Basic Task"
#    3. Name: "AlgoTrading"
#    4. Trigger: Daily, 8:55 AM, Recur every 1 day
#    5. Action: Start a program
#       Program: C:\Python311\python.exe  (your Python path)
#       Arguments: C:\algo_trading\auto_start.py
#       Start in: C:\algo_trading\
#    6. Finish
#    7. In Properties → Conditions → uncheck "only if AC power"
#    8. In Properties → Settings → check "Run task as soon as possible
#       after a scheduled start is missed"
#
# ── RUN 24/7 ON A CHEAP CLOUD SERVER ────────────────────────────
#
#  Best option: Oracle Cloud free tier (permanently free)
#    → 1 VM with 1GB RAM running Ubuntu
#    → No electricity cost, no PC needed to be on
#    → Steps:
#       1. Sign up: cloud.oracle.com (free tier, no credit card needed)
#       2. Create Ubuntu 22.04 VM (Always Free tier)
#       3. SSH into it and clone your repo
#       4. pip install -r requirements.txt
#       5. crontab -e → add the cron line above
#       6. Done — runs every weekday automatically forever
#
# ================================================================
