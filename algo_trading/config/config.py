# ============================================================
#  config/config.py
#  Central configuration for the entire trading system
#
#  FIX: ORB_REWARD_RISK_RATIO changed from 1.5 → 1.2.
#       The live system was using 1.5 while all backtests used 1.2.
#       All backtest results (V2/V3/V4) were produced at 1.2,
#       so the live system would have behaved differently from
#       what was tested. Both now use 1.2 consistently.
#       If you deliberately want to test 1.5, change it here and
#       re-run backtests — never change only one side.
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
TOKEN_FILE          = os.path.join(BASE_DIR, "api_key.txt")
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
ORB_TIMEFRAME_MINUTES   = 15
ORB_VOLUME_MULTIPLIER   = 1.5
ORB_MIN_RANGE_PCT       = 0.003    # 0.3% of price
ORB_MAX_RANGE_PCT       = 0.015    # 1.5% of price
ORB_ENTRY_DEADLINE      = time(12, 0)

# FIX: was 1.5 in live but all backtests used 1.2.
#      Changed to 1.2 so live matches what was tested.
#      Do NOT change this without re-running backtests.
ORB_REWARD_RISK_RATIO   = 1.2

# VWAP Pullback Strategy
VWAP_EMA_PERIOD         = 20
VWAP_TOLERANCE_PCT      = 0.001
VWAP_ENTRY_DEADLINE     = time(13, 30)
VWAP_TRAIL_AFTER_1R     = True

# EMA-RSI Strategy (retired — no gross edge on 5-min Nifty stocks)
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
BREAKOUT_ATR_MIN_PCT    = 0.008
BREAKOUT_ATR_MAX_PCT    = 0.030
BREAKOUT_VOL_MULTIPLIER = 1.5
BREAKOUT_SL_ATR_MULT    = 0.5

# Dynamic SL Settings
SL_MIN_PCT              = 0.003
SL_MAX_PCT              = 0.008
SL_ATR_MULTIPLIER       = 1.5

# ------------------------------------------------------------------
# MARKET REGIME THRESHOLDS
# ------------------------------------------------------------------
ADX_TREND_THRESHOLD     = 25       # shared by live classifier AND backtest filter
EMA_SHORT_PERIOD        = 50
EMA_LONG_PERIOD         = 200
INDIA_VIX_HIGH          = 22
INDIA_VIX_EXTREME       = 28
BB_VOL_HIGH_PERCENTILE  = 75
BB_VOL_LOW_PERCENTILE   = 25

# ------------------------------------------------------------------
# TIME GATES (IST)
# ------------------------------------------------------------------
MARKET_OPEN             = time(9, 15)
ORB_READY               = time(9, 30)
NO_NEW_ENTRIES          = time(14, 0)
AGGRESSIVE_EXIT_TIME    = time(14, 45)
FORCE_CLOSE_TIME        = time(15, 15)

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

ATR_PERCENTILE_MIN = 40
ATR_PERCENTILE_MAX = 80
GAP_PCT_MIN        = 0.003
GAP_PCT_MAX        = 0.015

# ------------------------------------------------------------------
# TRANSACTION COST MODEL (Zerodha Intraday MIS)
# ------------------------------------------------------------------
BROKERAGE_PER_ORDER     = 20
STT_SELL_PCT            = 0.00025
EXCHANGE_TXN_PCT        = 0.0000345
SEBI_CHARGE_PCT         = 0.000001
GST_ON_BROKERAGE        = 0.18
STAMP_DUTY_PCT          = 0.00003
SLIPPAGE_PCT            = 0.0005

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
