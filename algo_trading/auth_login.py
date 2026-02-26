# ============================================================
#  auth/login.py
#  Zerodha auto-login with Telegram OTP fallback
#
#  Flow:
#    1. Try pyotp auto-TOTP (works if TOTP_BASE32_KEY is correct)
#    2. If timeout → ping you on Telegram → wait for your 6-digit reply
#    3. Use your OTP → login succeeds
# ============================================================

import os
import sys
import time
import requests
from pyotp import TOTP
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from utils.logger import get_logger

logger = get_logger("auth")

BASE_DIR          = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRED_FILE         = os.path.join(BASE_DIR, "api_key.txt")
ACCESS_TOKEN_FILE = os.path.join(BASE_DIR, "access_token.txt")
KITE_LOGIN_URL    = "https://kite.zerodha.com"


# ----------------------------------------------------------------
# CREDENTIALS
# ----------------------------------------------------------------
def _load_credentials() -> dict:
    with open(CRED_FILE, 'r') as f:
        lines = [l.strip() for l in f.readlines()]
    return {
        'api_key':    lines[0],
        'api_secret': lines[1],
        'user_id':    lines[2],
        'password':   lines[3],
        'totp_key':   lines[4] if len(lines) > 4 else '',
        'tg_token':   lines[5] if len(lines) > 5 else '',
        'tg_chat_id': lines[6] if len(lines) > 6 else '',
    }


# ----------------------------------------------------------------
# TELEGRAM HELPERS (lightweight — notifier not loaded yet)
# ----------------------------------------------------------------
def _send_telegram(token: str, chat_id: str, message: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")


def _wait_for_telegram_otp(token: str, chat_id: str,
                            timeout_seconds: int = 90) -> str | None:
    """Poll Telegram for a 6-digit reply. Returns OTP string or None."""
    if not token or not chat_id:
        return None

    logger.info("Waiting for OTP via Telegram...")
    start_time  = time.time()
    last_update = 0

    # Get current offset so we only see NEW messages
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"limit": 1, "timeout": 0}, timeout=10
        )
        updates = resp.json().get("result", [])
        if updates:
            last_update = updates[-1]["update_id"]
    except Exception:
        pass

    while (time.time() - start_time) < timeout_seconds:
        remaining = int(timeout_seconds - (time.time() - start_time))
        logger.info(f"Waiting for Telegram OTP... ({remaining}s remaining)")

        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"offset": last_update + 1, "timeout": 3},
                timeout=10
            )
            for update in resp.json().get("result", []):
                last_update = update["update_id"]
                msg     = update.get("message", {})
                from_id = str(msg.get("chat", {}).get("id", ""))
                text    = msg.get("text", "").strip()

                if from_id != str(chat_id):
                    continue

                if text.isdigit() and len(text) == 6:
                    logger.info(f"OTP received via Telegram: {text}")
                    _send_telegram(token, chat_id,
                        f"✅ <b>OTP received: {text}</b>\nLogging into Zerodha now...")
                    return text
                else:
                    _send_telegram(token, chat_id,
                        "⚠️ That doesn't look like a 6-digit OTP.\n"
                        "Please reply with ONLY the 6 digits from your Zerodha app.")
        except Exception as e:
            logger.debug(f"Telegram poll error: {e}")

        time.sleep(3)

    logger.warning("Telegram OTP timeout — no reply received")
    return None


# ----------------------------------------------------------------
# MAIN LOGIN
# ----------------------------------------------------------------
def login(headless: bool = True) -> str:
    """
    Log into Zerodha Kite and return the access token.
    Tries pyotp first, falls back to Telegram OTP if that fails.
    """
    creds = _load_credentials()

    # Setup Chrome
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
        # Step 1: Open login page
        logger.info("Opening Kite login page...")
        driver.get(KITE_LOGIN_URL)
        time.sleep(2)

        # Step 2: Enter user ID + password
        logger.info("Entering credentials...")
        wait.until(EC.presence_of_element_located((By.ID, "userid"))).send_keys(creds['user_id'])
        driver.find_element(By.ID, "password").send_keys(creds['password'])
        driver.find_element(By.XPATH, "//button[@type='submit']").click()
        logger.info("Submitted login credentials")

        # Step 3: Find TOTP field
        logger.info("Waiting for TOTP field...")
        totp_selector = "//input[@id='userid']/../../..//input[@type='number']"
        wait.until(EC.presence_of_element_located((By.XPATH, totp_selector)))
        logger.info("TOTP field found")

        # Attempt 1: auto-generate via pyotp
        if creds['totp_key'] and creds['totp_key'] not in ('', 'YOUR_TOTP_BASE32_KEY'):
            totp_code = TOTP(creds['totp_key']).now()
            logger.info(f"TOTP entered: {totp_code}")
            totp_field = driver.find_element(By.XPATH, totp_selector)
            totp_field.clear()
            totp_field.send_keys(totp_code)
            totp_field.send_keys(Keys.RETURN)
            logger.info("TOTP submitted")
            try:
                driver.find_element(By.XPATH, "//button[@type='submit']").click()
            except Exception:
                pass

        # Wait for redirect with request_token (40s window)
        logger.info("Waiting for request_token in redirect URL...")
        request_token = None
        deadline = time.time() + 40

        while time.time() < deadline:
            if "request_token=" in driver.current_url:
                request_token = driver.current_url.split("request_token=")[1].split("&")[0]
                logger.info(f"request_token obtained: {request_token[:8]}...")
                break
            time.sleep(1)

        # Attempt 2: Telegram OTP fallback
        if not request_token:
            logger.warning("Auto-TOTP timed out — switching to Telegram OTP fallback")
            tg_token   = creds.get('tg_token', '')
            tg_chat_id = creds.get('tg_chat_id', '')

            if not tg_token or tg_token == 'YOUR_TELEGRAM_BOT_TOKEN':
                raise TimeoutError(
                    "Login failed: TOTP timed out and Telegram is not configured. "
                    "Add your BOT_TOKEN and CHAT_ID to api_key.txt (lines 6 and 7)."
                )

            _send_telegram(tg_token, tg_chat_id,
                "🔐 <b>Zerodha Login — OTP Required</b>\n\n"
                "Auto-login failed (TOTP key mismatch).\n\n"
                "👉 Open your <b>Zerodha / Google Authenticator app</b>\n"
                "👉 Find the 6-digit code for Zerodha\n"
                "👉 Reply here with JUST the 6 digits\n\n"
                "⏳ You have <b>90 seconds</b> to reply."
            )

            # Clear TOTP field
            try:
                driver.find_element(By.XPATH, totp_selector).clear()
            except Exception:
                pass

            user_otp = _wait_for_telegram_otp(tg_token, tg_chat_id, timeout_seconds=90)

            if not user_otp:
                raise TimeoutError(
                    "Login failed: No OTP received via Telegram within 90 seconds."
                )

            # Enter user's OTP
            try:
                totp_field = driver.find_element(By.XPATH, totp_selector)
            except Exception:
                wait.until(EC.presence_of_element_located((By.XPATH, totp_selector)))
                totp_field = driver.find_element(By.XPATH, totp_selector)

            totp_field.clear()
            totp_field.send_keys(user_otp)
            totp_field.send_keys(Keys.RETURN)
            logger.info(f"User OTP entered: {user_otp}")

            try:
                driver.find_element(By.XPATH, "//button[@type='submit']").click()
            except Exception:
                pass

            # Wait for redirect again
            deadline2 = time.time() + 40
            while time.time() < deadline2:
                if "request_token=" in driver.current_url:
                    request_token = driver.current_url.split("request_token=")[1].split("&")[0]
                    logger.info(f"request_token obtained after Telegram OTP: {request_token[:8]}...")
                    break
                time.sleep(1)

        if not request_token:
            raise RuntimeError("Login failed: Could not obtain request_token.")

        # Step 4: Exchange for access_token
        from kiteconnect import KiteConnect
        kite         = KiteConnect(api_key=creds['api_key'])
        session_data = kite.generate_session(request_token, api_secret=creds['api_secret'])
        access_token = session_data["access_token"]

        with open(ACCESS_TOKEN_FILE, 'w') as f:
            f.write(access_token)

        logger.info("✅ Login successful — access token saved")
        return access_token

    finally:
        driver.quit()


# ----------------------------------------------------------------
# PUBLIC API — used by main.py, backtests, scanner
# ----------------------------------------------------------------
def get_kite_session(headless: bool = True):
    """Login and return an authenticated KiteConnect instance."""
    from kiteconnect import KiteConnect
    access_token = login(headless=headless)
    creds = _load_credentials()
    kite  = KiteConnect(api_key=creds['api_key'])
    kite.set_access_token(access_token)
    return kite


def load_credentials() -> dict:
    """Public wrapper for _load_credentials()"""
    return _load_credentials()


# ----------------------------------------------------------------
# QUICK TEST: python auth/login.py
# ----------------------------------------------------------------
if __name__ == "__main__":
    token = login(headless=False)
    print(f"Access token: {token[:16]}...")

    from kiteconnect import KiteConnect
    creds = _load_credentials()
    kite  = KiteConnect(api_key=creds['api_key'])
    kite.set_access_token(token)

    profile = kite.profile()
    print(f"Logged in as: {profile['user_name']} ({profile['email']})")

    ltp = kite.ltp(["NSE:NIFTY 50"])
    print(f"Nifty LTP: {ltp['NSE:NIFTY 50']['last_price']}")
