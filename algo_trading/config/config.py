# ============================================================
#  config/config.py
#  Central configuration for the entire trading system
#  Edit these values to customise strategy behaviour
# ============================================================

import os
from datetime import time

# ------------------------------------------------------------------
# BASE PATH — auto-detected from this file's location
# ------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ------------------------------------------------------------------
# CREDENTIALS PATH
# ------------------------------------------------------------------
TOKEN_FILE      = os.path.join(BASE_DIR, "api_key.txt")
ACCESS_TOKEN_FILE   = os.path.join(BASE_DIR, "access_token.txt")
REQUEST_TOKEN_FILE  = os.path.join(BASE_DIR, "request_token.txt")

# ------------------------------------------------------------------
# CAPITAL & RISK SETTINGS
# ------------------------------------------------------------------
CAPITAL = 1_000_000          # Total capital in INR (₹10 lakhs)

RISK_PER_TRADE_NORMAL  = 0.005   # 0.5% of capital
RISK_PER_TRADE_HIGH    = 0.0075  # 0.75% — high confidence trades
RISK_PER_TRADE_APLUS   = 0.010   # 1.0%  — A+ setups only

DAILY_LOSS_LIMIT_PCT   = 0.015   # Stop trading if daily loss > 1.5%
WEEKLY_LOSS_LIMIT_PCT  = 0.035   # Reduce size if weekly loss > 3.5%
MAX_TRADES_PER_DAY     = 2
MIN_CONFIDENCE_SCORE   = 65      # Minimum signal confidence to trade

# ------------------------------------------------------------------
# NIFTY 50 STOCK UNIVERSE
# ------------------------------------------------------------------
NIFTY50_SYMBOLS = [
    "NSE:RELIANCE",  "NSE:TCS",       "NSE:HDFCBANK",  "NSE:INFY",
    "NSE:ICICIBANK", "NSE:HINDUNILVR","NSE:ITC",       "NSE:SBIN",
    "NSE:BHARTIARTL","NSE:KOTAKBANK", "NSE:LT",        "NSE:AXISBANK",
    "NSE:ASIANPAINT","NSE:MARUTI",    "NSE:HCLTECH",   "NSE:WIPRO",
    "NSE:ULTRACEMCO","NSE:BAJFINANCE","NSE:TITAN",     "NSE:NESTLEIND",
    "NSE:TECHM",     "NSE:POWERGRID", "NSE:ONGC",      "NSE:TATAMOTORS",
    "NSE:NTPC",      "NSE:JSWSTEEL",  "NSE:TATASTEEL", "NSE:ADANIENT",
    "NSE:ADANIPORTS","NSE:COALINDIA", "NSE:BAJAJFINSV","NSE:BAJAJ-AUTO",
    "NSE:HEROMOTOCO","NSE:CIPLA",     "NSE:DRREDDY",   "NSE:EICHERMOT",
    "NSE:DIVISLAB",  "NSE:BRITANNIA", "NSE:GRASIM",    "NSE:HINDALCO",
    "NSE:INDUSINDBK","NSE:M&M",       "NSE:SUNPHARMA", "NSE:TATACONSUM",
    "NSE:UPL",       "NSE:VEDL",      "NSE:BPCL",      "NSE:APOLLOHOSP",
    "NSE:HDFCLIFE",  "NSE:SBILIFE",
]

# ------------------------------------------------------------------
# STRATEGY SETTINGS
# ------------------------------------------------------------------

# ORB Strategy
ORB_TIMEFRAME_MINUTES   = 15       # Opening range period
ORB_VOLUME_MULTIPLIER   = 1.5      # Volume must be 1.5x 20-day avg
ORB_MIN_RANGE_PCT       = 0.003    # Minimum ORB range: 0.3% of price
ORB_MAX_RANGE_PCT       = 0.015    # Maximum ORB range: 1.5% of price
ORB_ENTRY_DEADLINE      = time(12, 0)   # No ORB entries after 12 PM
ORB_REWARD_RISK_RATIO   = 1.5

# VWAP Pullback Strategy
VWAP_EMA_PERIOD         = 20
VWAP_TOLERANCE_PCT      = 0.001    # 0.1% tolerance from VWAP
VWAP_ENTRY_DEADLINE     = time(13, 30)
VWAP_TRAIL_AFTER_1R     = True

# EMA-RSI Strategy
EMA_FAST                = 9
EMA_SLOW                = 21
RSI_PERIOD              = 14
RSI_LONG_MIN            = 55
RSI_LONG_MAX            = 75
RSI_SHORT_MIN           = 25
RSI_SHORT_MAX           = 45
EMA_ATR_MULTIPLIER_SL   = 1.5
EMA_ATR_MULTIPLIER_TGT  = 2.0

# Breakout ATR Strategy
BREAKOUT_ATR_MIN_PCT    = 0.008    # ATR must be > 0.8% of price
BREAKOUT_ATR_MAX_PCT    = 0.030    # ATR must be < 3.0% of price
BREAKOUT_VOL_MULTIPLIER = 1.5
BREAKOUT_SL_ATR_MULT    = 0.5

# Dynamic SL Settings
SL_MIN_PCT              = 0.003    # Never tighter than 0.3%
SL_MAX_PCT              = 0.008    # Never wider than 0.8%
SL_ATR_MULTIPLIER       = 1.5

# ------------------------------------------------------------------
# MARKET REGIME THRESHOLDS
# ------------------------------------------------------------------
ADX_TREND_THRESHOLD     = 25
EMA_SHORT_PERIOD        = 50
EMA_LONG_PERIOD         = 200
INDIA_VIX_HIGH          = 22       # Reduce size above this
INDIA_VIX_EXTREME       = 28       # Stop trading above this
BB_VOL_HIGH_PERCENTILE  = 75
BB_VOL_LOW_PERCENTILE   = 25

# ------------------------------------------------------------------
# TIME GATES (IST)
# ------------------------------------------------------------------
MARKET_OPEN             = time(9, 15)
ORB_READY               = time(9, 30)   # First valid entry time
NO_NEW_ENTRIES          = time(14, 0)   # No new positions after 2 PM
AGGRESSIVE_EXIT_TIME    = time(14, 45)  # Start closing losing positions
FORCE_CLOSE_TIME        = time(15, 15)  # Hard close ALL positions

# ------------------------------------------------------------------
# SCANNER WEIGHTS (must sum to 1.0)
# ------------------------------------------------------------------
SCANNER_WEIGHTS = {
    'pre_market_gap':       0.15,
    'prev_day_range_exp':   0.10,
    'sector_strength':      0.10,
    'relative_strength':    0.15,
    'atr_filter':           0.10,
    'volume_spike':         0.15,
    'news_sentiment':       0.10,
    'fii_dii_flow':         0.05,
    'sgx_global_bias':      0.10,
}

# ATR sweet spot percentiles for scanner
ATR_PERCENTILE_MIN = 40
ATR_PERCENTILE_MAX = 80

# Gap sweet spot
GAP_PCT_MIN = 0.003   # 0.3%
GAP_PCT_MAX = 0.015   # 1.5%

# ------------------------------------------------------------------
# TRANSACTION COST MODEL (Zerodha Intraday MIS)
# ------------------------------------------------------------------
BROKERAGE_PER_ORDER     = 20        # Flat ₹20 per order
STT_SELL_PCT            = 0.00025   # 0.025% on sell-side only
EXCHANGE_TXN_PCT        = 0.0000345
SEBI_CHARGE_PCT         = 0.000001
GST_ON_BROKERAGE        = 0.18
STAMP_DUTY_PCT          = 0.00003   # Buy side only
SLIPPAGE_PCT            = 0.0005    # 0.05% realistic slippage

# ------------------------------------------------------------------
# BACKTEST SETTINGS
# ------------------------------------------------------------------
BACKTEST_INITIAL_CAPITAL = 1_000_000
BACKTEST_TIMEFRAME       = '5minute'
BACKTEST_STOCKS = [
    "NSE:RELIANCE", "NSE:HDFCBANK",
    "NSE:INFY", "NSE:TATAMOTORS", "NSE:AXISBANK"
]

# ------------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------------
LOG_DIR     = os.path.join(BASE_DIR, "logs")
LOG_LEVEL   = "INFO"
LOG_TO_FILE = True
