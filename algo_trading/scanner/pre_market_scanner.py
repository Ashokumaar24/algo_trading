# ============================================================
#  scanner/pre_market_scanner.py
#  Pre-Market Alpha Scanner — runs at 9:05 AM IST
#  Scores and ranks Nifty50 stocks for the trading day
# ============================================================

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import List, Optional

from config.config import (
    NIFTY50_SYMBOLS, SCANNER_WEIGHTS,
    GAP_PCT_MIN, GAP_PCT_MAX,
    ATR_PERCENTILE_MIN, ATR_PERCENTILE_MAX
)
from utils.indicators import atr, relative_strength, atr_percentile, volume_sma
from utils.logger import get_logger

logger = get_logger("scanner")


@dataclass
class StockCandidate:
    """Pre-market scan result for a single stock"""
    symbol:        str
    score:         float
    bias:          str       # BULLISH | BEARISH | NEUTRAL
    gap_pct:       float
    rs_vs_nifty:   float
    atr_percentile: float
    vol_ratio:      float
    sentiment:      float
    confidence:     float
    notes:          str = ""
    rank:           int = 0

    def __repr__(self):
        return (f"[{self.rank}] {self.symbol} | {self.bias} | "
                f"Score:{self.score:.0f} | Gap:{self.gap_pct*100:.2f}% | "
                f"RS:{self.rs_vs_nifty:.2f} | Conf:{self.confidence:.0f}")


class PreMarketScanner:
    """
    Pre-market scanner that runs at 9:05 AM IST.
    Scores each Nifty50 stock on 9 dimensions and returns top 5.

    Usage:
        scanner = PreMarketScanner(kite)
        candidates = scanner.run()   # Returns top 5 StockCandidate objects
    """

    def __init__(self, kite):
        self.kite = kite

    # ------------------------------------------------------------------
    # MAIN SCANNER ENTRY POINT
    # ------------------------------------------------------------------
    def run(self, top_n: int = 5) -> List[StockCandidate]:
        """
        Run the full pre-market scan.
        Returns top_n ranked StockCandidate objects.
        """
        logger.info(f"Pre-market scan started at {datetime.now().strftime('%H:%M:%S')}")

        candidates = []

        for symbol in NIFTY50_SYMBOLS:
            try:
                candidate = self._score_stock(symbol)
                if candidate:
                    candidates.append(candidate)
            except Exception as e:
                logger.warning(f"Scan failed for {symbol}: {e}")

        # Sort by score descending
        candidates.sort(key=lambda x: x.score, reverse=True)

        # Assign ranks
        for i, c in enumerate(candidates[:top_n], 1):
            c.rank = i

        top = candidates[:top_n]

        logger.info(f"Scan complete. Top {top_n} candidates:")
        for c in top:
            logger.info(f"  {c}")

        return top

    # ------------------------------------------------------------------
    # INDIVIDUAL STOCK SCORER
    # ------------------------------------------------------------------
    def _score_stock(self, symbol: str) -> Optional[StockCandidate]:
        """Score a single stock across all 9 dimensions"""

        # --- Fetch historical data ---
        hist = self._get_history(symbol, days=30, interval='day')
        if hist is None or len(hist) < 22:
            return None

        prev_day  = hist.iloc[-1]  # Yesterday's candle
        prev_prev = hist.iloc[-2]

        # --- Pre-market price (use last available tick) ---
        try:
            ltp_data = self.kite.ltp(symbol)
            premarket_price = ltp_data[symbol]['last_price']
        except Exception:
            premarket_price = prev_day['close']  # fallback

        prev_close = prev_day['close']

        # --- Score individual factors ---
        scores = {}

        # 1. Pre-market gap
        gap_pct = (premarket_price - prev_close) / prev_close
        scores['pre_market_gap'] = self._score_gap(gap_pct)

        # 2. Previous day range expansion
        prev_range = prev_day['high'] - prev_day['low']
        avg_range  = (hist['high'] - hist['low']).tail(20).mean()
        range_exp  = prev_range / avg_range if avg_range > 0 else 1.0
        scores['prev_day_range_exp'] = min(range_exp * 60, 100)

        # 3. Sector strength — simplified (use stock's 5-day return vs Nifty)
        scores['sector_strength'] = self._score_sector_strength(symbol, hist)

        # 4. Relative strength vs Nifty
        nifty_hist = self._get_history("NSE:NIFTY 50", days=25, interval='day')
        if nifty_hist is not None and len(nifty_hist) >= 20:
            stock_ret = hist['close'].pct_change()
            nifty_ret = nifty_hist['close'].pct_change()
            rs = relative_strength(stock_ret, nifty_ret, period=20)
        else:
            rs = 1.0
        scores['relative_strength'] = self._score_rs(rs)

        # 5. ATR filter
        atr_series = atr(hist['high'], hist['low'], hist['close'], period=14)
        curr_atr   = atr_series.iloc[-1]
        atr_pct_rank = atr_percentile(curr_atr, atr_series.dropna())
        scores['atr_filter'] = self._score_atr(atr_pct_rank)

        # 6. Volume spike
        vol_sma    = volume_sma(hist['volume'], period=20).iloc[-1]
        vol_ratio  = prev_day['volume'] / vol_sma if vol_sma > 0 else 1.0
        scores['volume_spike'] = min(vol_ratio * 50, 100)

        # 7. News sentiment (stub — returns neutral 50 if no NLP model)
        sentiment_score = self._get_sentiment(symbol)
        scores['news_sentiment'] = (sentiment_score + 1) / 2 * 100  # normalise -1..1 → 0..100

        # 8. FII/DII flow
        scores['fii_dii_flow'] = self._get_fii_dii_score()

        # 9. SGX + Global bias
        scores['sgx_global_bias'] = self._get_global_bias()

        # --- Weighted composite score ---
        total_score = sum(
            scores[factor] * SCANNER_WEIGHTS[factor]
            for factor in SCANNER_WEIGHTS
        )

        # --- Determine bias ---
        bias = "BULLISH" if gap_pct > 0 and rs > 1.0 else \
               "BEARISH" if gap_pct < 0 and rs < 1.0 else "NEUTRAL"

        # --- Confidence ---
        confidence = min(total_score * 1.1, 100)

        return StockCandidate(
            symbol=symbol,
            score=round(total_score, 1),
            bias=bias,
            gap_pct=round(gap_pct, 4),
            rs_vs_nifty=round(rs, 3),
            atr_percentile=round(atr_pct_rank, 1),
            vol_ratio=round(vol_ratio, 2),
            sentiment=round(sentiment_score, 3),
            confidence=round(confidence, 1),
            notes=f"Gap:{gap_pct*100:.2f}% RS:{rs:.2f} ATR%:{atr_pct_rank:.0f}"
        )

    # ------------------------------------------------------------------
    # SCORING HELPERS
    # ------------------------------------------------------------------
    def _score_gap(self, gap_pct: float) -> float:
        """Score pre-market gap. Sweet spot: 0.3% – 1.5%"""
        abs_gap = abs(gap_pct)
        if GAP_PCT_MIN <= abs_gap <= GAP_PCT_MAX:
            return min(abs_gap / GAP_PCT_MAX, 1.0) * 100
        elif abs_gap > GAP_PCT_MAX:
            return max(0, 100 - (abs_gap - GAP_PCT_MAX) * 2000)
        else:
            return abs_gap / GAP_PCT_MIN * 40

    def _score_rs(self, rs: float) -> float:
        """Score relative strength. RS > 1.1 or < 0.9 = actionable"""
        if rs > 1.1:
            return min((rs - 1.0) * 200, 100)
        elif rs < 0.9:
            return min((1.0 - rs) * 200, 100)
        return 20.0  # neutral RS = low score

    def _score_atr(self, atr_pct_rank: float) -> float:
        """ATR sweet spot: 40th–80th percentile"""
        if ATR_PERCENTILE_MIN <= atr_pct_rank <= ATR_PERCENTILE_MAX:
            return 100.0
        elif atr_pct_rank < ATR_PERCENTILE_MIN:
            return atr_pct_rank / ATR_PERCENTILE_MIN * 70
        else:
            return max(0, 100 - (atr_pct_rank - ATR_PERCENTILE_MAX) * 2)

    def _score_sector_strength(self, symbol: str, hist: pd.DataFrame) -> float:
        """Simplified sector score using 5-day momentum"""
        ret_5d = (hist['close'].iloc[-1] / hist['close'].iloc[-5] - 1) * 100
        if ret_5d > 2:   return 100.0
        if ret_5d > 0:   return 60.0
        if ret_5d > -2:  return 40.0
        return 10.0

    # ------------------------------------------------------------------
    # DATA FETCHERS
    # ------------------------------------------------------------------
    def _get_history(self, symbol: str, days: int,
                     interval: str = 'day') -> Optional[pd.DataFrame]:
        """Fetch historical candles from KiteConnect"""
        try:
            instrument = symbol.replace("NSE:", "")
            instruments = self.kite.instruments("NSE")
            inst_df = pd.DataFrame(instruments)
            row = inst_df[inst_df['tradingsymbol'] == instrument]
            if row.empty:
                return None
            token = int(row.iloc[0]['instrument_token'])

            to_date   = datetime.now()
            from_date = to_date - timedelta(days=days + 5)

            data = self.kite.historical_data(
                token, from_date, to_date, interval
            )
            if not data:
                return None

            df = pd.DataFrame(data)
            df.rename(columns={
                'date': 'timestamp', 'open': 'open', 'high': 'high',
                'low': 'low', 'close': 'close', 'volume': 'volume'
            }, inplace=True)
            df.set_index('timestamp', inplace=True)
            return df.tail(days)

        except Exception as e:
            logger.debug(f"History fetch failed for {symbol}: {e}")
            return None

    def _get_sentiment(self, symbol: str) -> float:
        """
        News sentiment score [-1 to +1].
        Stub implementation returns 0.0 (neutral).
        Replace with FinBERT NLP pipeline for production.
        """
        # TODO: Integrate FinBERT or custom sentiment model
        # from sentiment.sentiment_engine import SentimentEngine
        # return SentimentEngine().get_score(symbol)
        return 0.0

    def _get_fii_dii_score(self) -> float:
        """
        FII/DII flow score [0–100].
        Stub: returns 50 (neutral). NSE publishes data by ~8:30 AM.
        """
        # TODO: Scrape NSE FII/DII page or use data vendor
        return 50.0

    def _get_global_bias(self) -> float:
        """
        Global market bias score [0–100] from SGX Nifty + US/Asia.
        Stub: returns 50 (neutral).
        """
        # TODO: Fetch SGX Nifty futures, Dow futures, Nikkei, Hang Seng
        return 50.0

    # ------------------------------------------------------------------
    # PRETTY PRINT
    # ------------------------------------------------------------------
    def print_report(self, candidates: List[StockCandidate]):
        """Print formatted pre-market scan report"""
        print("\n" + "=" * 70)
        print(f"  PRE-MARKET SCANNER REPORT — {datetime.now().strftime('%d %b %Y %H:%M IST')}")
        print("=" * 70)
        print(f"{'Rank':<5}{'Symbol':<15}{'Bias':<10}{'Score':<8}"
              f"{'Gap%':<9}{'RS':<7}{'ATR%':<7}{'VolR':<7}{'Conf':<6}")
        print("-" * 70)
        for c in candidates:
            print(f"{c.rank:<5}{c.symbol.replace('NSE:',''):<15}"
                  f"{c.bias:<10}{c.score:<8.1f}"
                  f"{c.gap_pct*100:+.2f}%   "
                  f"{c.rs_vs_nifty:<7.2f}{c.atr_percentile:<7.0f}"
                  f"{c.vol_ratio:<7.1f}{c.confidence:<6.0f}")
        print("=" * 70)
        if candidates:
            top = candidates[0]
            print(f"  TOP PICK: {top.symbol} | {top.bias} | Confidence: {top.confidence:.0f}/100")
        print()
