# ============================================================
#  utils/indicators.py
#  Pure-function technical indicators — NO lookahead bias
#  All functions take a numpy/pandas series and return values
#  computed only from data available at that point in time.
# ============================================================

import numpy as np
import pandas as pd
from typing import Optional


# ------------------------------------------------------------------
# MOVING AVERAGES
# ------------------------------------------------------------------

def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average"""
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average"""
    return series.rolling(window=period).mean()


def ema_slope(ema_series: pd.Series, lookback: int = 3) -> pd.Series:
    """
    Returns True where EMA is sloping upward over last `lookback` bars.
    Used to confirm trend direction.
    """
    return ema_series > ema_series.shift(lookback)


# ------------------------------------------------------------------
# ATR — Average True Range
# ------------------------------------------------------------------

def atr(high: pd.Series, low: pd.Series, close: pd.Series,
        period: int = 14) -> pd.Series:
    """
    Average True Range.
    True Range = max(high-low, |high-prev_close|, |low-prev_close|)
    """
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def atr_percentile(current_atr: float, atr_history: pd.Series) -> float:
    """Returns percentile rank of current ATR vs historical ATR values"""
    return float((atr_history < current_atr).sum() / len(atr_history) * 100)


# ------------------------------------------------------------------
# VWAP — Volume Weighted Average Price
# ------------------------------------------------------------------

def vwap(high: pd.Series, low: pd.Series, close: pd.Series,
         volume: pd.Series) -> pd.Series:
    """
    Intraday VWAP — resets at 9:15 AM each day.
    Groups by date so VWAP resets daily.
    """
    typical_price = (high + low + close) / 3
    tp_vol = typical_price * volume

    # Group by date and compute cumulative VWAP
    date_group = close.index.date if hasattr(close.index, 'date') else None

    if date_group is not None:
        cum_tp_vol = tp_vol.groupby(date_group).cumsum()
        cum_vol    = volume.groupby(date_group).cumsum()
    else:
        cum_tp_vol = tp_vol.cumsum()
        cum_vol    = volume.cumsum()

    return cum_tp_vol / cum_vol


# ------------------------------------------------------------------
# RSI — Relative Strength Index
# ------------------------------------------------------------------

def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """
    RSI using Wilder's smoothing (standard).
    Returns values 0–100.
    """
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)

    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()

    rs  = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


# ------------------------------------------------------------------
# ADX — Average Directional Index
# ------------------------------------------------------------------

def adx(high: pd.Series, low: pd.Series, close: pd.Series,
        period: int = 14) -> pd.Series:
    """
    ADX for trend strength.
    ADX > 25 = trending market
    ADX < 20 = ranging/weak trend
    """
    tr_val = atr(high, low, close, period=1)  # raw TR before smoothing

    up_move   = high - high.shift(1)
    down_move = low.shift(1) - low

    pos_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
    neg_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)

    pos_dm_s = pd.Series(pos_dm, index=close.index).ewm(span=period, adjust=False).mean()
    neg_dm_s = pd.Series(neg_dm, index=close.index).ewm(span=period, adjust=False).mean()
    atr_s    = tr_val.ewm(span=period, adjust=False).mean()

    pos_di = 100 * pos_dm_s / atr_s.replace(0, np.nan)
    neg_di = 100 * neg_dm_s / atr_s.replace(0, np.nan)

    dx = 100 * (pos_di - neg_di).abs() / (pos_di + neg_di).replace(0, np.nan)
    return dx.ewm(span=period, adjust=False).mean()


# ------------------------------------------------------------------
# BOLLINGER BANDS
# ------------------------------------------------------------------

def bollinger_bands(close: pd.Series, period: int = 20,
                    std_dev: float = 2.0) -> dict:
    """Returns dict with 'upper', 'middle', 'lower', 'width'"""
    middle = sma(close, period)
    std    = close.rolling(window=period).std()
    upper  = middle + std_dev * std
    lower  = middle - std_dev * std
    width  = (upper - lower) / middle

    return {'upper': upper, 'middle': middle, 'lower': lower, 'width': width}


def bb_width_percentile(current_width: float,
                         width_history: pd.Series) -> float:
    """Percentile of current Bollinger Band width vs 1-year history"""
    return float((width_history < current_width).sum() / len(width_history) * 100)


# ------------------------------------------------------------------
# VOLUME INDICATORS
# ------------------------------------------------------------------

def volume_sma(volume: pd.Series, period: int = 20) -> pd.Series:
    """20-day average volume"""
    return volume.rolling(window=period).mean()


def relative_volume(volume: pd.Series, avg_volume: pd.Series) -> pd.Series:
    """Current volume / average volume — values > 1.5 = volume spike"""
    return volume / avg_volume.replace(0, np.nan)


# ------------------------------------------------------------------
# OPENING RANGE
# ------------------------------------------------------------------

def calculate_orb(candles_915: pd.Series, candles_930: pd.Series) -> dict:
    """
    Calculate Opening Range (9:15 + 9:30 candles combined).
    Returns high, low, range, mid.
    No lookahead — only uses the two completed opening candles.
    """
    orb_high  = max(candles_915['high'], candles_930['high'])
    orb_low   = min(candles_915['low'],  candles_930['low'])
    orb_range = orb_high - orb_low
    orb_mid   = (orb_high + orb_low) / 2

    return {
        'high':  orb_high,
        'low':   orb_low,
        'range': orb_range,
        'mid':   orb_mid,
        'range_pct': orb_range / orb_mid,
    }


# ------------------------------------------------------------------
# RELATIVE STRENGTH vs INDEX
# ------------------------------------------------------------------

def relative_strength(stock_returns: pd.Series,
                       index_returns: pd.Series,
                       period: int = 20) -> float:
    """
    Stock RS vs index over the last `period` bars.
    RS > 1.1 = outperforming (bullish)
    RS < 0.9 = underperforming (bearish)
    """
    stock_perf = (1 + stock_returns.tail(period)).prod() - 1
    index_perf = (1 + index_returns.tail(period)).prod() - 1

    if index_perf == 0:
        return 1.0
    return (1 + stock_perf) / (1 + index_perf)


# ------------------------------------------------------------------
# TRANSACTION COST CALCULATOR
# ------------------------------------------------------------------

def calculate_trade_cost(entry_price: float, exit_price: float,
                          quantity: int) -> float:
    """
    Full Zerodha intraday cost model.
    Returns total cost in INR for a round-trip trade.
    """
    from config.config import (
        BROKERAGE_PER_ORDER, STT_SELL_PCT, EXCHANGE_TXN_PCT,
        SEBI_CHARGE_PCT, GST_ON_BROKERAGE, STAMP_DUTY_PCT, SLIPPAGE_PCT
    )

    buy_value  = entry_price * quantity
    sell_value = exit_price  * quantity
    total_val  = buy_value + sell_value

    brokerage  = 2 * BROKERAGE_PER_ORDER
    stt        = sell_value * STT_SELL_PCT
    exchange   = total_val  * EXCHANGE_TXN_PCT
    sebi       = total_val  * SEBI_CHARGE_PCT
    gst        = brokerage  * GST_ON_BROKERAGE
    stamp      = buy_value  * STAMP_DUTY_PCT
    slippage   = total_val  * SLIPPAGE_PCT

    return brokerage + stt + exchange + sebi + gst + stamp + slippage
