# ============================================================
#  auth/login.py
#  Zerodha KiteConnect login with Telegram OTP fallback
#
#  Critical: must use kite.zerodha.com/connect/login?api_key=...
#  NOT kite.zerodha.com — the /connect/login URL is what
#  generates the request_token after TOTP.
# ============================================================

import os
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
# TELEGRAM HELPERS
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


def _get_latest_update_id(token: str) -> int:
    """Get the current highest update_id so we ONLY accept messages sent AFTER this moment."""
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"limit": 100, "timeout": 0},
            timeout=10
        )
        updates = resp.json().get("result", [])
        if updates:
            return updates[-1]["update_id"]
    except Exception:
        pass
    return 0


def _wait_for_telegram_otp(token: str, chat_id: str,
                             start_update_id: int,
                             timeout_seconds: int = 120) -> str | None:
    """
    Poll Telegram for a NEW 6-digit reply.
    Only processes messages with update_id > start_update_id
    (messages that arrived AFTER we sent the OTP request).
    """
    if not token or not chat_id:
        return None

    logger.info("Waiting for OTP via Telegram...")
    start_time  = time.time()
    last_update = start_update_id  # only look at messages AFTER this

    while (time.time() - start_time) < timeout_seconds:
        remaining = int(timeout_seconds - (time.time() - start_time))
        logger.info(f"Waiting for Telegram OTP... ({remaining}s left)")

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
                        f"✅ <b>OTP {text} received!</b>\n"
                        f"Logging into Zerodha now...")
                    return text
                elif text.startswith("/"):
                    pass  # ignore commands during login
                else:
                    _send_telegram(token, chat_id,
                        "⚠️ Please send ONLY the 6-digit code.\n"
                        "Example: <code>482917</code>")

        except Exception as e:
            logger.debug(f"Telegram poll error: {e}")

        time.sleep(2)

    logger.warning("Telegram OTP timeout — no reply received")
    return None


def _find_totp_field(driver, timeout=30):
    """Try multiple selectors to find the TOTP input field."""
    selectors = [
        (By.XPATH,        "//input[@type='number']"),
        (By.CSS_SELECTOR, "input[type='number']"),
        (By.XPATH,        "//input[@autocomplete='one-time-code']"),
        (By.CSS_SELECTOR, "input[placeholder*='TOTP']"),
        (By.CSS_SELECTOR, "input[placeholder*='OTP']"),
        (By.CSS_SELECTOR, "input[placeholder*='code']"),
    ]

    deadline = time.time() + timeout
    while time.time() < deadline:
        for by, selector in selectors:
            try:
                for el in driver.find_elements(by, selector):
                    if el.is_displayed() and el.is_enabled():
                        logger.info(f"TOTP field found via: {by} = {selector}")
                        return el
            except Exception:
                pass
        time.sleep(0.5)

    raise TimeoutError(
        "Could not find OTP input field on Zerodha login page.\n"
        "Possible cause: wrong password, account locked, or Zerodha page changed."
    )


# ----------------------------------------------------------------
# MAIN LOGIN
# ----------------------------------------------------------------
def login(headless: bool = True) -> str:
    """
    Log into Zerodha via KiteConnect API flow and return the access token.

    IMPORTANT: Must use /connect/login?api_key= URL, NOT kite.zerodha.com directly.
    Only the /connect/login flow generates the request_token in the redirect URL.
    """
    creds = _load_credentials()

    api_key    = creds['api_key']
    tg_token   = creds.get('tg_token', '')
    tg_chat_id = creds.get('tg_chat_id', '')
    tg_enabled = bool(tg_token and tg_token not in ('', 'YOUR_TELEGRAM_BOT_TOKEN'))
    totp_key   = creds.get('totp_key', '')
    totp_ok    = bool(totp_key and totp_key not in ('', 'YOUR_TOTP_BASE32_KEY'))

    # ── Get Telegram offset BEFORE sending OTP request ───────────────
    # This ensures we only read messages sent AFTER our prompt —
    # ignoring any old messages already in the inbox.
    tg_offset = 0
    if tg_enabled:
        tg_offset = _get_latest_update_id(tg_token)
        logger.info(f"Telegram offset snapshot: {tg_offset}")

    # ── If TOTP key missing → ask Telegram upfront ───────────────────
    if not totp_ok:
        if not tg_enabled:
            raise RuntimeError(
                "No TOTP key and Telegram not configured.\n"
                "Set TOTP_BASE32_KEY in api_key.txt line 5, OR\n"
                "Set Telegram credentials in lines 6 and 7."
            )
        logger.info("No TOTP key — asking Telegram for OTP before browser opens")
        _send_telegram(tg_token, tg_chat_id,
            "🔐 <b>Zerodha Login — OTP Needed</b>\n\n"
            "👉 Open your <b>Zerodha app</b>\n"
            "👉 Tap Profile → Security → and get the 6-digit code\n"
            "👉 <b>Reply here with JUST the 6 digits</b>\n\n"
            "⏳ You have <b>120 seconds</b>\n"
            "Example: <code>482917</code>"
        )

    # ── Setup Chrome ─────────────────────────────────────────────────
    options = Options()
    if headless:
        options.add_argument("--headless=new")
        logger.info("Running Chrome in headless mode")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,900")
    options.add_argument("--log-level=3")

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )
    wait = WebDriverWait(driver, 30)

    try:
        # ── Step 1: Open KiteConnect API login URL ────────────────────
        # THIS IS THE KEY FIX:
        # Must use /connect/login?api_key=... to get request_token in redirect
        login_url = f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"
        logger.info(f"Opening KiteConnect login URL...")
        driver.get(login_url)
        time.sleep(2)

        # ── Step 2: Enter user ID + password ─────────────────────────
        logger.info("Entering credentials...")
        wait.until(EC.presence_of_element_located((By.ID, "userid"))).send_keys(creds['user_id'])
        driver.find_element(By.ID, "password").send_keys(creds['password'])
        driver.find_element(By.XPATH, "//button[@type='submit']").click()
        logger.info("Password submitted")
        time.sleep(2)

        # ── Step 3: Find OTP input field ──────────────────────────────
        logger.info("Looking for OTP input field...")
        totp_field = _find_totp_field(driver, timeout=30)

        # ── Step 4: Get OTP ───────────────────────────────────────────
        if totp_ok:
            otp = TOTP(totp_key).now()
            logger.info(f"Using pyotp auto-TOTP: {otp}")
        else:
            logger.info("Waiting for your Telegram reply...")
            otp = _wait_for_telegram_otp(tg_token, tg_chat_id, tg_offset, timeout_seconds=120)
            if not otp:
                raise TimeoutError("Login failed: No OTP received from Telegram within 120 seconds.")

        # ── Step 5: Enter OTP ─────────────────────────────────────────
        logger.info(f"Entering OTP: {otp}")
        totp_field.clear()
        totp_field.send_keys(otp)
        time.sleep(0.3)
        totp_field.send_keys(Keys.RETURN)
        try:
            driver.find_element(By.XPATH, "//button[@type='submit']").click()
        except Exception:
            pass

        # ── Step 6: Wait for redirect with request_token ─────────────
        logger.info("Waiting for KiteConnect redirect with request_token...")
        request_token = None
        deadline = time.time() + 40

        while time.time() < deadline:
            if "request_token=" in driver.current_url:
                request_token = driver.current_url.split("request_token=")[1].split("&")[0]
                logger.info(f"request_token obtained: {request_token[:8]}...")
                break
            time.sleep(1)

        # ── pyotp failed → Telegram fallback ─────────────────────────
        if not request_token and totp_ok:
            logger.warning("pyotp TOTP rejected — falling back to Telegram OTP")

            if not tg_enabled:
                raise RuntimeError(
                    "Auto-TOTP failed and Telegram is not configured.\n"
                    "Fix TOTP_BASE32_KEY in api_key.txt line 5, OR\n"
                    "Add Telegram credentials to lines 6 and 7."
                )

            # Snapshot offset again before sending the prompt
            tg_offset = _get_latest_update_id(tg_token)

            _send_telegram(tg_token, tg_chat_id,
                "🔐 <b>OTP Required</b>\n\n"
                "Auto-login failed (TOTP key incorrect).\n\n"
                "👉 Open your <b>Zerodha app</b>\n"
                "👉 Reply with the fresh 6-digit code\n\n"
                "⏳ You have <b>90 seconds</b>."
            )

            try:
                totp_field = _find_totp_field(driver, timeout=5)
                totp_field.clear()
            except Exception:
                pass

            user_otp = _wait_for_telegram_otp(tg_token, tg_chat_id, tg_offset, timeout_seconds=90)
            if not user_otp:
                raise TimeoutError("Login failed: No OTP received via Telegram within 90 seconds.")

            totp_field = _find_totp_field(driver, timeout=15)
            totp_field.clear()
            totp_field.send_keys(user_otp)
            totp_field.send_keys(Keys.RETURN)
            logger.info(f"Telegram OTP entered: {user_otp}")

            try:
                driver.find_element(By.XPATH, "//button[@type='submit']").click()
            except Exception:
                pass

            deadline2 = time.time() + 40
            while time.time() < deadline2:
                if "request_token=" in driver.current_url:
                    request_token = driver.current_url.split("request_token=")[1].split("&")[0]
                    logger.info(f"request_token obtained after Telegram OTP: {request_token[:8]}...")
                    break
                time.sleep(1)

        if not request_token:
            raise RuntimeError(
                "Login failed: No request_token in redirect URL after OTP.\n"
                "Check that your KiteConnect app redirect URL is correctly set in\n"
                "Zerodha Developer Console: https://developers.kite.trade/apps"
            )

        # ── Step 7: Exchange request_token for access_token ──────────
        from kiteconnect import KiteConnect
        kite         = KiteConnect(api_key=api_key)
        session_data = kite.generate_session(request_token, api_secret=creds['api_secret'])
        access_token = session_data["access_token"]

        with open(ACCESS_TOKEN_FILE, 'w') as f:
            f.write(access_token)

        logger.info("✅ Login successful — access token saved")

        if tg_enabled:
            _send_telegram(tg_token, tg_chat_id,
                "✅ <b>Zerodha Login Successful!</b>\n"
                "System is now running. Pre-market scan starting...\n\n"
                "Commands: /status /stop /journal /help"
            )

        return access_token

    finally:
        driver.quit()


# ----------------------------------------------------------------
# PUBLIC API
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
    return _load_credentials()


# ----------------------------------------------------------------
# QUICK TEST: python auth/login.py
# ----------------------------------------------------------------
if __name__ == "__main__":
    token = login(headless=False)
    print(f"\nAccess token: {token[:16]}...")

    from kiteconnect import KiteConnect
    creds = _load_credentials()
    kite  = KiteConnect(api_key=creds['api_key'])
    kite.set_access_token(token)
    profile = kite.profile()
    print(f"Logged in as: {profile['user_name']} ({profile['email']})")
    ltp = kite.ltp(["NSE:NIFTY 50"])
    print(f"Nifty LTP: ₹{ltp['NSE:NIFTY 50']['last_price']}")
