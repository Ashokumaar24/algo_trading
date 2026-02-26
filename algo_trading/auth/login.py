# ============================================================
#  auth/login.py  — PATCHED WITH TELEGRAM OTP FALLBACK
#
#  What changed vs original:
#  1. Added _wait_for_telegram_otp() — polls Telegram for your reply
#  2. In the TOTP retry block: instead of trying another pyotp code,
#     it now asks you via Telegram and waits up to 90 seconds for
#     your 6-digit reply.
#
#  Flow:
#    Attempt 1: pyotp auto-TOTP (works if base32 key is correct)
#    → timeout after 40s → sends Telegram: "Please reply with your
#      Zerodha TOTP"
#    Attempt 2: waits for YOUR reply via Telegram (90 second window)
#    → uses your OTP → login succeeds
#
#  FIND the section marked "# ── TELEGRAM OTP FALLBACK ──" below
#  and replace the corresponding block in YOUR auth/login.py
#
#  The rest of your login.py stays exactly the same.
# ============================================================

import os
import sys
import time
import requests
from pyotp import TOTP
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from utils.logger import get_logger

logger = get_logger("auth")

CRED_FILE         = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api_key.txt")
ACCESS_TOKEN_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "access_token.txt")
KITE_LOGIN_URL    = "https://kite.zerodha.com"


def _load_credentials() -> dict:
    with open(CRED_FILE, 'r') as f:
        lines = [l.strip() for l in f.readlines()]
    return {
        'api_key':      lines[0],
        'api_secret':   lines[1],
        'user_id':      lines[2],
        'password':     lines[3],
        'totp_key':     lines[4] if len(lines) > 4 else '',
        'tg_token':     lines[5] if len(lines) > 5 else '',
        'tg_chat_id':   lines[6] if len(lines) > 6 else '',
    }


# ── TELEGRAM OTP FALLBACK ────────────────────────────────────
def _send_telegram(token: str, chat_id: str, message: str):
    """Send a Telegram message (used before full notifier is initialised)"""
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")


def _wait_for_telegram_otp(token: str, chat_id: str,
                            timeout_seconds: int = 120) -> str | None:
    """
    Polls Telegram for a 6-digit reply from the user.
    Returns the OTP string, or None if timeout exceeded.

    Works by polling getUpdates with a short-poll every 3 seconds.
    Ignores any messages older than the moment this function was called.
    """
    if not token or not chat_id:
        return None

    logger.info("Waiting for OTP via Telegram...")
    start_time  = time.time()
    last_update = 0

    # Get current update offset so we only see NEW messages
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"limit": 1, "timeout": 0},
            timeout=10
        )
        updates = resp.json().get("result", [])
        if updates:
            last_update = updates[-1]["update_id"]
    except Exception:
        pass

    while (time.time() - start_time) < timeout_seconds:
        remaining = int(timeout_seconds - (time.time() - start_time))
        logger.info(f"Waiting for your Telegram OTP reply... ({remaining}s remaining)")

        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"offset": last_update + 1, "timeout": 3},
                timeout=10
            )
            updates = resp.json().get("result", [])

            for update in updates:
                last_update = update["update_id"]
                msg     = update.get("message", {})
                from_id = str(msg.get("chat", {}).get("id", ""))
                text    = msg.get("text", "").strip()

                # Only accept messages from YOUR chat
                if from_id != str(chat_id):
                    continue

                # Accept any 6-digit number
                if text.isdigit() and len(text) == 6:
                    logger.info(f"OTP received via Telegram: {text}")
                    _send_telegram(token, chat_id,
                        f"✅ <b>OTP received: {text}</b>\nLogging into Zerodha now...")
                    return text
                else:
                    _send_telegram(token, chat_id,
                        f"⚠️ That doesn't look like a 6-digit OTP.\nPlease reply with ONLY the 6-digit code from your Zerodha app.")

        except Exception as e:
            logger.debug(f"Telegram poll error: {e}")

        time.sleep(3)

    logger.warning("Telegram OTP timeout — no reply received within window")
    return None
# ── END TELEGRAM OTP FALLBACK ────────────────────────────────


def login(headless: bool = True) -> str:
    """
    Logs into Zerodha Kite via Selenium and returns the access token.
    Automatically handles TOTP — falls back to Telegram if pyotp fails.
    """
    creds = _load_credentials()

    # ── Setup Chrome ─────────────────────────────────────────
    options = Options()
    if headless:
        options.add_argument("--headless=new")
        logger.info("Running Chrome in headless mode")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,900")

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )
    wait = WebDriverWait(driver, 30)

    try:
        # ── Step 1: Open login page ───────────────────────────
        logger.info("Opening Kite login page...")
        driver.get(KITE_LOGIN_URL)
        time.sleep(2)

        # ── Step 2: Enter user ID + password ─────────────────
        logger.info("Entering credentials...")
        wait.until(EC.presence_of_element_located((By.ID, "userid"))).send_keys(creds['user_id'])
        driver.find_element(By.ID, "password").send_keys(creds['password'])
        driver.find_element(By.XPATH, "//button[@type='submit']").click()
        logger.info("Submitted login credentials")

        # ── Step 3: Enter TOTP ────────────────────────────────
        logger.info("Waiting for TOTP field...")

        # Find TOTP input field
        totp_selector = "//input[@id='userid']/../../..//input[@type='number']"
        wait.until(EC.presence_of_element_located((By.XPATH, totp_selector)))
        logger.info(f"TOTP field found with selector: {totp_selector}")

        # Attempt 1: auto-generate via pyotp
        totp_code = None
        if creds['totp_key'] and creds['totp_key'] not in ('', 'YOUR_TOTP_BASE32_KEY'):
            totp_code = TOTP(creds['totp_key']).now()
            logger.info(f"TOTP entered: {totp_code}")
            totp_field = driver.find_element(By.XPATH, totp_selector)
            totp_field.clear()
            totp_field.send_keys(totp_code)
            from selenium.webdriver.common.keys import Keys
            totp_field.send_keys(Keys.RETURN)
            logger.info("TOTP submitted via ENTER key")

            # Also click submit button if present
            try:
                driver.find_element(By.XPATH, "//button[@type='submit']").click()
                logger.info("TOTP submit button clicked")
            except Exception:
                pass

        # ── Wait for redirect with request_token ─────────────
        logger.info("Waiting for request_token in redirect URL...")
        request_token = None
        deadline = time.time() + 40   # 40 second window for auto-TOTP

        while time.time() < deadline:
            current_url = driver.current_url
            if "request_token=" in current_url:
                request_token = current_url.split("request_token=")[1].split("&")[0]
                logger.info(f"request_token obtained: {request_token[:8]}...")
                break
            time.sleep(1)

        # ── Fallback: ask user via Telegram ──────────────────
        if not request_token:
            logger.warning("Redirect timeout — switching to Telegram OTP fallback...")

            tg_token   = creds.get('tg_token', '')
            tg_chat_id = creds.get('tg_chat_id', '')

            if tg_token and tg_token not in ('', 'YOUR_TELEGRAM_BOT_TOKEN'):
                # Notify user on Telegram
                _send_telegram(tg_token, tg_chat_id,
                    "🔐 <b>Zerodha Login — OTP Required</b>\n\n"
                    "Auto-login failed (TOTP key mismatch).\n\n"
                    "👉 Open your <b>Zerodha / Google Authenticator app</b>\n"
                    "👉 Find the 6-digit code for Zerodha\n"
                    "👉 Reply here with JUST the 6 digits\n\n"
                    "⏳ You have <b>90 seconds</b> to reply."
                )

                # Clear the TOTP field and wait for user's reply
                try:
                    totp_field = driver.find_element(By.XPATH, totp_selector)
                    totp_field.clear()
                except Exception:
                    pass

                # Wait for reply
                user_otp = _wait_for_telegram_otp(tg_token, tg_chat_id, timeout_seconds=90)

                if user_otp:
                    # Enter the user-provided OTP
                    try:
                        totp_field = driver.find_element(By.XPATH, totp_selector)
                        totp_field.clear()
                        totp_field.send_keys(user_otp)
                        from selenium.webdriver.common.keys import Keys
                        totp_field.send_keys(Keys.RETURN)
                        logger.info(f"User OTP entered: {user_otp}")
                    except Exception:
                        # Page may have refreshed — re-find field
                        wait.until(EC.presence_of_element_located((By.XPATH, totp_selector)))
                        totp_field = driver.find_element(By.XPATH, totp_selector)
                        totp_field.send_keys(user_otp)
                        from selenium.webdriver.common.keys import Keys
                        totp_field.send_keys(Keys.RETURN)

                    try:
                        driver.find_element(By.XPATH, "//button[@type='submit']").click()
                    except Exception:
                        pass

                    # Wait for redirect again
                    deadline2 = time.time() + 40
                    while time.time() < deadline2:
                        current_url = driver.current_url
                        if "request_token=" in current_url:
                            request_token = current_url.split("request_token=")[1].split("&")[0]
                            logger.info(f"request_token obtained after Telegram OTP: {request_token[:8]}...")
                            break
                        time.sleep(1)

                else:
                    raise TimeoutError(
                        "Login failed: No OTP received via Telegram within 90 seconds. "
                        "Please check your Telegram app."
                    )
            else:
                raise TimeoutError(
                    "Login failed: TOTP redirect timed out and Telegram is not configured. "
                    "Please set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in api_key.txt (lines 6 and 7)."
                )

        if not request_token:
            raise RuntimeError("Login failed: Could not obtain request_token even after OTP submission.")

        # ── Step 4: Exchange request_token for access_token ───
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=creds['api_key'])
        session_data = kite.generate_session(request_token, api_secret=creds['api_secret'])
        access_token = session_data["access_token"]

        # Save for reuse
        with open(ACCESS_TOKEN_FILE, 'w') as f:
            f.write(access_token)

        logger.info("✅ Login successful — access token saved")
        return access_token

    finally:
        driver.quit()


if __name__ == "__main__":
    """Quick test: python auth/login.py"""
    token = login(headless=False)   # headless=False shows the browser window
    print(f"Access token: {token[:16]}...")

    from kiteconnect import KiteConnect
    creds = _load_credentials()
    kite  = KiteConnect(api_key=creds['api_key'])
    kite.set_access_token(token)

    profile = kite.profile()
    print(f"Logged in as: {profile['user_name']} ({profile['email']})")

    ltp = kite.ltp(["NSE:NIFTY 50"])
    print(f"Nifty LTP: {ltp['NSE:NIFTY 50']['last_price']}")
