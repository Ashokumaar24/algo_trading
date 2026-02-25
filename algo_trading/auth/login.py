# ============================================================
#  auth/login.py
#  Zerodha KiteConnect Auto-Login with TOTP
#  Enhanced from original login.py with session management
# ============================================================

from kiteconnect import KiteConnect
from selenium import webdriver
from selenium.webdriver.common.by import By
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
        otp_input = wait.until(
            EC.presence_of_element_located(
                (By.XPATH, "//input[@type='password' or @type='tel']")
            )
        )
        totp = TOTP(credentials['totp_key']).now()
        otp_input.send_keys(totp)
        logger.info(f"TOTP entered: {totp}")

        # ---- Step 4: Wait for redirect with request_token ----
        logger.info("Waiting for request_token in redirect URL...")
        wait.until(lambda d: "request_token" in d.current_url)
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
