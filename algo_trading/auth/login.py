# ============================================================
#  auth/login.py
#  Zerodha KiteConnect Auto-Login with TOTP
#  Enhanced from original login.py with session management
# ============================================================

from kiteconnect import KiteConnect
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from pyotp import TOTP
from datetime import datetime, date
import os
import sys
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.config import TOKEN_FILE, ACCESS_TOKEN_FILE, REQUEST_TOKEN_FILE
from utils.logger import get_logger

logger = get_logger("auth")


# ------------------------------------------------------------------
# LOAD CREDENTIALS
# ------------------------------------------------------------------
def load_credentials():
    """Load API credentials from api_key.txt"""
    if not os.path.exists(TOKEN_FILE):
        logger.error(f"Credentials file not found: {TOKEN_FILE}")
        logger.error("Copy api_key.txt.example to api_key.txt and fill in your details.")
        raise FileNotFoundError(f"Missing: {TOKEN_FILE}")

    with open(TOKEN_FILE, 'r') as f:
        keys = f.read().split()

    if len(keys) < 5:
        raise ValueError(
            "api_key.txt must have 5 lines: "
            "API_KEY, API_SECRET, USER_ID, PASSWORD, TOTP_KEY"
        )

    return {
        'api_key':    keys[0],
        'api_secret': keys[1],
        'user_id':    keys[2],
        'password':   keys[3],
        'totp_key':   keys[4],
    }


# ------------------------------------------------------------------
# SESSION VALIDATION
# ------------------------------------------------------------------
def is_session_valid():
    """
    Check if we already have a valid access token for today.
    Avoids unnecessary browser logins during the same trading day.
    """
    if not os.path.exists(ACCESS_TOKEN_FILE):
        return False, None

    # Check if access_token was generated today
    file_mtime = os.path.getmtime(ACCESS_TOKEN_FILE)
    file_date  = date.fromtimestamp(file_mtime)
    today      = date.today()

    if file_date != today:
        logger.info("Access token is from a previous day — will re-login.")
        return False, None

    with open(ACCESS_TOKEN_FILE, 'r') as f:
        token = f.read().strip()

    if not token:
        return False, None

    logger.info("Found today's access token — attempting to reuse.")
    return True, token


# ------------------------------------------------------------------
# AUTO-LOGIN (Selenium)
# ------------------------------------------------------------------
def autologin(headless=True, credentials=None):
    """
    Automated Zerodha Kite login using Selenium + TOTP.
    Returns KiteConnect instance with valid access token.
    """
    if credentials is None:
        credentials = load_credentials()

    logger.info("Starting Zerodha auto-login...")
    kite = KiteConnect(api_key=credentials['api_key'])

    # --- Browser options ---
    options = webdriver.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
        logger.info("Running Chrome in headless mode")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--log-level=3")
    options.add_argument("--window-size=1920,1080")

    driver = webdriver.Chrome(options=options)
    wait   = WebDriverWait(driver, 30)

    request_token = None

    try:
        # ---- Step 1: Load login page ----
        logger.info("Opening Kite login page...")
        driver.get(kite.login_url())

        # ---- Step 2: Enter User ID ----
        logger.info("Entering credentials...")
        wait.until(EC.presence_of_element_located((By.ID, "userid"))).send_keys(
            credentials['user_id']
        )
        driver.find_element(By.ID, "password").send_keys(credentials['password'])
        driver.find_element(By.XPATH, "//button[@type='submit']").click()
        logger.info("Submitted login credentials")

        # ---- Step 3: TOTP ----
        logger.info("Waiting for TOTP field...")

        # Try multiple selectors — Zerodha sometimes changes field type
        otp_input = None
        totp_selectors = [
            (By.XPATH, "//input[@id='userid']/../../..//input[@type='number']"),
            (By.XPATH, "//input[@type='number']"),
            (By.XPATH, "//input[@label='External TOTP']"),
            (By.XPATH, "//input[@autocomplete='one-time-code']"),
            (By.XPATH, "//input[contains(@placeholder,'TOTP') or contains(@placeholder,'PIN') or contains(@placeholder,'OTP')]"),
            (By.XPATH, "//input[@type='tel']"),
            (By.XPATH, "//input[@type='password' and not(@id='password')]"),
        ]

        for selector in totp_selectors:
            try:
                otp_input = WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located(selector)
                )
                logger.info(f"TOTP field found with selector: {selector[1]}")
                break
            except Exception:
                continue

        if otp_input is None:
            # Last resort: any input that appeared after login submit
            time.sleep(2)
            inputs = driver.find_elements(By.TAG_NAME, "input")
            visible = [el for el in inputs if el.is_displayed() and el.get_attribute("id") != "userid"]
            if visible:
                otp_input = visible[-1]
                logger.info(f"TOTP field found via fallback (visible input): id={otp_input.get_attribute('id')}")
            else:
                logger.error("Could not find TOTP input field")
                driver.save_screenshot(os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "logs", f"totp_not_found_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                ))
                raise RuntimeError("TOTP input field not found on page")

        # ---- Wait for TOTP code with enough validity remaining ----
        # TOTP resets every 30s — if <5s left, wait for fresh window
        remaining = 30 - (int(time.time()) % 30)
        if remaining <= 5:
            logger.info(f"TOTP almost expired ({remaining}s left) — waiting {remaining + 2}s...")
            time.sleep(remaining + 2)

        totp = TOTP(credentials['totp_key']).now()
        otp_input.click()
        time.sleep(0.2)
        otp_input.clear()
        otp_input.send_keys(totp)
        logger.info(f"TOTP entered: {totp}")
        time.sleep(0.3)

        # ---- Step 4: Submit TOTP ----
        # PRIMARY: Press ENTER — most reliable across all Zerodha UI versions
        otp_input.send_keys(Keys.RETURN)
        logger.info("TOTP submitted via ENTER key")
        time.sleep(1)

        # SECONDARY: Also click submit button if still on TOTP page
        if "request_token" not in driver.current_url:
            for btn_xpath in [
                "//button[@type='submit']",
                "//button[contains(text(),'Continue')]",
                "//button[contains(text(),'Login')]",
                "//button[contains(text(),'Verify')]",
            ]:
                try:
                    btn = driver.find_element(By.XPATH, btn_xpath)
                    if btn.is_displayed() and btn.is_enabled():
                        btn.click()
                        logger.info(f"TOTP submit button clicked")
                        time.sleep(1)
                        break
                except Exception:
                    continue

        # ---- Step 5: Wait for redirect with request_token ----
        logger.info("Waiting for request_token in redirect URL...")
        try:
            WebDriverWait(driver, 40).until(
                lambda d: "request_token" in d.current_url
            )
        except Exception:
            # RETRY: TOTP may have expired — try once more with fresh code
            logger.warning("Redirect timeout — retrying with fresh TOTP code...")
            totp_fields = driver.find_elements(By.XPATH, "//input[@type='number']")
            if not totp_fields:
                totp_fields = driver.find_elements(By.XPATH, "//input[@type='tel']")

            if totp_fields and any(f.is_displayed() for f in totp_fields):
                # Wait for next TOTP window to be sure
                time.sleep(5)
                fresh_totp = TOTP(credentials['totp_key']).now()
                for f in totp_fields:
                    if f.is_displayed():
                        f.clear()
                        f.send_keys(fresh_totp)
                        time.sleep(0.3)
                        f.send_keys(Keys.RETURN)
                        logger.info(f"Retry TOTP entered: {fresh_totp}")
                        time.sleep(1)
                        break

                # Final wait
                WebDriverWait(driver, 30).until(
                    lambda d: "request_token" in d.current_url
                )
            else:
                current_url = driver.current_url
                raise RuntimeError(
                    f"Login failed — TOTP page not found for retry. URL: {current_url}"
                )

        if "request_token" not in driver.current_url:
            raise RuntimeError(
                f"Login did not redirect to request_token. URL: {driver.current_url}"
            )

        time.sleep(1)  # brief pause for URL to stabilise

        # ---- Step 5: Extract request token ----
        request_token = driver.current_url.split("request_token=")[1].split("&")[0]

        with open(REQUEST_TOKEN_FILE, 'w') as f:
            f.write(request_token)
        logger.info(f"Request token saved: {request_token}")

    except Exception as e:
        logger.error(f"Login failed: {e}")
        # Save screenshot for debugging
        try:
            screenshot_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "logs", f"login_error_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            )
            os.makedirs(os.path.dirname(screenshot_path), exist_ok=True)
            driver.save_screenshot(screenshot_path)
            logger.error(f"Screenshot saved: {screenshot_path}")
        except Exception:
            pass
        raise

    finally:
        driver.quit()
        logger.info("Browser closed")

    return request_token


# ------------------------------------------------------------------
# GENERATE ACCESS TOKEN
# ------------------------------------------------------------------
def generate_access_token(request_token, credentials):
    """Exchange request_token for access_token via Zerodha API"""
    kite = KiteConnect(api_key=credentials['api_key'])

    logger.info("Generating access token...")
    data = kite.generate_session(
        request_token,
        api_secret=credentials['api_secret']
    )
    access_token = data["access_token"]

    with open(ACCESS_TOKEN_FILE, 'w') as f:
        f.write(access_token)

    logger.info(f"Access token saved: {access_token[:10]}...")
    return access_token


# ------------------------------------------------------------------
# VERIFY SESSION
# ------------------------------------------------------------------
def verify_session(kite):
    """Verify session by fetching profile and a test LTP"""
    try:
        profile = kite.profile()
        logger.info(f"Logged in as: {profile['user_name']} ({profile['user_id']})")

        ltp = kite.ltp("NSE:NIFTY 50")
        nifty_price = ltp["NSE:NIFTY 50"]["last_price"]
        logger.info(f"Nifty50 LTP: ₹{nifty_price:,.2f}")

        return True
    except Exception as e:
        logger.error(f"Session verification failed: {e}")
        return False


# ------------------------------------------------------------------
# MAIN ENTRY POINT — get_kite_session()
# ------------------------------------------------------------------
def get_kite_session(headless=True, force_relogin=False):
    """
    Master function to get a valid KiteConnect session.
    
    Usage:
        from auth.login import get_kite_session
        kite = get_kite_session()
    
    - Reuses today's token if available (skips browser)
    - Auto-logins if token is stale or missing
    - Verifies session before returning

    Returns:
        kite (KiteConnect): Authenticated KiteConnect instance
    """
    credentials = load_credentials()
    kite = KiteConnect(api_key=credentials['api_key'])

    # --- Try to reuse existing session ---
    if not force_relogin:
        valid, existing_token = is_session_valid()
        if valid:
            kite.set_access_token(existing_token)
            if verify_session(kite):
                logger.info("Reusing existing session — no browser needed.")
                return kite
            logger.warning("Existing token invalid — performing fresh login.")

    # --- Perform fresh login ---
    try:
        request_token = autologin(headless=headless, credentials=credentials)
        access_token  = generate_access_token(request_token, credentials)
        kite.set_access_token(access_token)

        if verify_session(kite):
            logger.info("Fresh login successful.")
            return kite
        else:
            raise RuntimeError("Session verification failed after fresh login.")

    except Exception as e:
        logger.critical(f"Cannot establish Kite session: {e}")
        raise


# ------------------------------------------------------------------
# STANDALONE EXECUTION
# ------------------------------------------------------------------
if __name__ == "__main__":
    """Run directly to test login: python auth/login.py"""
    print("=" * 60)
    print("  ZERODHA KITE — AUTO LOGIN TEST")
    print("=" * 60)

    try:
        kite = get_kite_session(headless=False)  # headless=False to see browser
        print("\n[SUCCESS] Login verified! System ready to trade.")

        # Quick test: fetch a few LTPs
        symbols = ["NSE:RELIANCE", "NSE:HDFCBANK", "NSE:INFY"]
        ltps = kite.ltp(symbols)
        print("\nSample LTPs:")
        for sym, data in ltps.items():
            print(f"  {sym}: ₹{data['last_price']:,.2f}")

    except Exception as e:
        print(f"\n[FAILED] Login error: {e}")
        sys.exit(1)
