# ============================================================
#  utils/daily_journal.py
#  Daily Trade Journal — captures EVERY trading decision with reasoning
#
#  Logs:
#    - WHY a trade was placed (signal, regime, confidence, strategy)
#    - WHY a trade was NOT placed (regime blocked, confidence too low,
#      trade cap hit, time gate, risk gate, no signal conditions met)
#    - Market regime classification for the day
#    - End-of-day P&L summary
#
#  Output: logs/journal_YYYY-MM-DD.md (human readable markdown)
#
#  Usage:
#    from utils.daily_journal import get_journal
#    journal = get_journal()
#    journal.log_regime(regime)
#    journal.log_trade_placed(signal, reason="ORB breakout confirmed")
#    journal.log_trade_blocked("NSE:RELIANCE", "ORB_15", "REGIME", "Market ranging")
# ============================================================

import os
from datetime import datetime
from typing import Optional, List
from dataclasses import dataclass, field


# ----------------------------------------------------------------
# EVENT TYPES
# ----------------------------------------------------------------
@dataclass
class TradeEvent:
    """A trade that was placed (or simulated in dry-run)"""
    time:       str
    symbol:     str
    strategy:   str
    direction:  str
    entry:      float
    sl:         float
    target:     float
    confidence: float
    regime:     str
    rr:         float
    reason:     str      # human-readable why this trade fired
    dry_run:    bool = True


@dataclass
class BlockEvent:
    """A potential trade that was blocked — with full reason"""
    time:       str
    symbol:     str
    strategy:   str
    block_type: str      # REGIME | CONFIDENCE | TRADE_CAP | TIME_GATE | RISK_GATE | NO_SIGNAL | SENTIMENT
    reason:     str      # short label
    detail:     str      # full explanation
    candle_price: Optional[float] = None
    confidence:   Optional[float] = None


@dataclass
class RegimeEvent:
    """Market regime classification logged at start of day"""
    time:        str
    trend:       str
    volatility:  str
    adx:         float
    india_vix:   float
    bb_width_pct: float
    is_tradeable: bool
    eligible_strategies: List[str]
    reason:      str     # why this regime was classified


@dataclass
class ScannerEvent:
    """Pre-market scanner results"""
    time:       str
    top_stocks: List[dict]   # [{symbol, score, bias, gap_pct, confidence}]
    regime_summary: str


# ----------------------------------------------------------------
# SINGLETON JOURNAL
# ----------------------------------------------------------------
_journal_instance = None

def get_journal() -> 'DailyJournal':
    """Get the global daily journal instance (singleton)"""
    global _journal_instance
    if _journal_instance is None:
        _journal_instance = DailyJournal()
    return _journal_instance

def reset_journal():
    """Call at start of each new trading day to get a fresh journal"""
    global _journal_instance
    _journal_instance = DailyJournal()
    return _journal_instance


# ----------------------------------------------------------------
# DAILY JOURNAL
# ----------------------------------------------------------------
class DailyJournal:
    """
    Captures every trading decision made by the system throughout the day.
    At end of day, generates a detailed markdown report explaining
    exactly what happened, what didn't, and why.
    """

    def __init__(self):
        self.date       = datetime.now().strftime("%Y-%m-%d")
        self.start_time = datetime.now().strftime("%H:%M:%S")

        self.trades_placed:   List[TradeEvent]   = []
        self.trades_blocked:  List[BlockEvent]   = []
        self.regime_log:      List[RegimeEvent]  = []
        self.scanner_results: Optional[ScannerEvent] = None
        self.exit_log:        List[dict]         = []   # trade exits with P&L
        self.daily_notes:     List[str]          = []   # free-form notes

        # Summary counters (filled at report time)
        self._block_counts = {}

        # Setup log directory
        self.log_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "logs"
        )
        os.makedirs(self.log_dir, exist_ok=True)

        self.report_path = os.path.join(
            self.log_dir, f"journal_{self.date}.md"
        )

    # ------------------------------------------------------------------
    # LOG METHODS — called throughout the day
    # ------------------------------------------------------------------

    def log_regime(self, regime, eligible_strategies: List[str] = None):
        """
        Log the market regime classification at start of day.
        Call from main.py after pre_market_setup().
        """
        if eligible_strategies is None:
            eligible_strategies = []

        # Build human-readable reason
        reasons = []
        if hasattr(regime, 'adx'):
            if regime.adx >= 25:
                reasons.append(f"ADX {regime.adx:.1f} ≥ 25 → trending market")
            else:
                reasons.append(f"ADX {regime.adx:.1f} < 25 → weak/no trend")

        if hasattr(regime, 'india_vix'):
            if regime.india_vix > 28:
                reasons.append(f"India VIX {regime.india_vix:.1f} > 28 → extreme fear, no trades")
            elif regime.india_vix > 22:
                reasons.append(f"India VIX {regime.india_vix:.1f} > 22 → elevated fear, size halved")
            else:
                reasons.append(f"India VIX {regime.india_vix:.1f} → normal fear levels")

        if hasattr(regime, 'bb_width_pct'):
            if regime.bb_width_pct > 75:
                reasons.append(f"BB Width {regime.bb_width_pct:.0f}th percentile → high volatility")
            elif regime.bb_width_pct < 25:
                reasons.append(f"BB Width {regime.bb_width_pct:.0f}th percentile → low volatility/squeeze")

        event = RegimeEvent(
            time=datetime.now().strftime("%H:%M:%S"),
            trend=getattr(regime, 'trend', 'UNKNOWN'),
            volatility=getattr(regime, 'volatility', 'UNKNOWN'),
            adx=getattr(regime, 'adx', 0.0),
            india_vix=getattr(regime, 'india_vix', 0.0),
            bb_width_pct=getattr(regime, 'bb_width_pct', 0.0),
            is_tradeable=getattr(regime, 'is_tradeable', True),
            eligible_strategies=eligible_strategies,
            reason=" | ".join(reasons) if reasons else "Classification based on ADX + EMA + BB Width"
        )
        self.regime_log.append(event)

    def log_scanner_results(self, candidates: list):
        """
        Log pre-market scanner top picks.
        Call from main.py after scanner.run().
        """
        top_stocks = []
        for c in candidates:
            top_stocks.append({
                'symbol':     getattr(c, 'symbol', ''),
                'score':      getattr(c, 'score', 0),
                'bias':       getattr(c, 'bias', ''),
                'gap_pct':    getattr(c, 'gap_pct', 0),
                'confidence': getattr(c, 'confidence', 0),
                'notes':      getattr(c, 'notes', ''),
            })

        regime_summary = "No regime data"
        if self.regime_log:
            r = self.regime_log[-1]
            regime_summary = f"{r.trend} + {r.volatility}"

        self.scanner_results = ScannerEvent(
            time=datetime.now().strftime("%H:%M:%S"),
            top_stocks=top_stocks,
            regime_summary=regime_summary
        )

    def log_trade_placed(self, signal, dry_run: bool = True, extra_reason: str = ""):
        """
        Log a trade that was placed (or simulated).
        Call from order_manager.place_order().
        """
        # Build human-readable reason
        reasons = []
        strategy = getattr(signal, 'strategy', '')
        direction = getattr(signal, 'direction', '')
        direction_str = direction.value if hasattr(direction, 'value') else str(direction)
        conf = getattr(signal, 'confidence', 0)
        notes = getattr(signal, 'notes', '')
        regime = getattr(signal, 'regime', 'UNKNOWN')

        if strategy == 'ORB_15':
            reasons.append("ORB breakout: price closed above/below the 9:15–9:30 opening range")
        elif strategy == 'VWAP_PULLBACK':
            reasons.append("VWAP Pullback: price pulled back to VWAP then resumed trend direction")
        elif strategy == 'BREAKOUT_ATR':
            reasons.append("Breakout: price closed above/below previous day high/low with volume")

        reasons.append(f"Confidence score: {conf:.0f}/100 (minimum required: 65)")
        reasons.append(f"Market regime: {regime}")

        if notes:
            reasons.append(f"Signal details: {notes}")

        if extra_reason:
            reasons.append(extra_reason)

        event = TradeEvent(
            time=datetime.now().strftime("%H:%M:%S"),
            symbol=getattr(signal, 'symbol', ''),
            strategy=strategy,
            direction=direction_str,
            entry=getattr(signal, 'entry', 0),
            sl=getattr(signal, 'stop_loss', 0),
            target=getattr(signal, 'target', 0),
            confidence=conf,
            regime=regime,
            rr=getattr(signal, 'reward_risk', 0),
            reason="\n  - ".join(reasons),
            dry_run=dry_run
        )
        self.trades_placed.append(event)

    def log_trade_exit(self, symbol: str, strategy: str, direction: str,
                       entry: float, exit_price: float, exit_reason: str,
                       pnl: float, hold_mins: int):
        """
        Log a trade exit with outcome.
        Call from order_manager._record_closed_position().
        """
        outcome = "WIN ✅" if pnl > 0 else "LOSS ❌"

        exit_reasons = {
            'TARGET_HIT':     "Price reached the target level",
            'SL_HIT':         "Price hit the stop loss level",
            'TIME_EXIT_1230': "Hard time exit at 12:30 PM (ORB rule)",
            'EOD_CLOSE':      "End of day force close at 3:15 PM",
            'FORCE_CLOSE':    "Manual force close triggered",
            'CLOSE_LOSERS':   "Time-based exit: losing trade after 2:45 PM",
        }

        self.exit_log.append({
            'time':        datetime.now().strftime("%H:%M:%S"),
            'symbol':      symbol,
            'strategy':    strategy,
            'direction':   direction,
            'entry':       entry,
            'exit_price':  exit_price,
            'exit_reason': exit_reason,
            'exit_detail': exit_reasons.get(exit_reason, exit_reason),
            'pnl':         pnl,
            'hold_mins':   hold_mins,
            'outcome':     outcome,
        })

    def log_trade_blocked(self, symbol: str, strategy: str,
                           block_type: str, reason: str,
                           detail: str = "",
                           candle_price: float = None,
                           confidence: float = None):
        """
        Log a potential trade that was blocked with full reason.

        block_type options:
          REGIME       — market regime not suitable
          CONFIDENCE   — signal confidence below minimum
          TRADE_CAP    — max 2 trades per day already hit
          TIME_GATE    — after 2:00 PM cutoff
          RISK_GATE    — daily loss limit or consecutive losses
          SENTIMENT    — sentiment gate blocked strategy
          NO_SIGNAL    — strategy conditions not met (not a block, just no setup)
        """
        # Build detailed explanation
        block_details = {
            'REGIME': (
                f"Market regime is not suitable for {strategy}. "
                f"The regime classifier identified conditions where this strategy "
                f"historically loses money (ranging/low-volatility market). Skipped."
            ),
            'CONFIDENCE': (
                f"Signal confidence {confidence:.0f}/100 is below the minimum threshold of 65. "
                f"Low-confidence signals have a poor historical win rate. Skipped."
            ) if confidence else (
                f"Signal confidence is below the minimum threshold of 65. Skipped."
            ),
            'TRADE_CAP': (
                f"Already placed 2 trades today (daily maximum). "
                f"The 2-trade cap exists because backtests showed over-trading "
                f"destroys edge through transaction costs. No more entries today."
            ),
            'TIME_GATE': (
                f"Current time is past 2:00 PM cutoff for new entries. "
                f"Trades entered after 2 PM don't have enough time to play out "
                f"before the 3:15 PM force close."
            ),
            'RISK_GATE': (
                f"Risk management gate triggered. Either daily loss limit hit (>1.5% of capital) "
                f"or 5 consecutive losses reached. System protects capital by stopping for the day."
            ),
            'SENTIMENT': (
                f"Sentiment gate: signal direction conflicts with market sentiment. "
                f"e.g. VWAP Pullback Long blocked when sentiment is strongly negative."
            ),
            'NO_SIGNAL': (
                f"Strategy conditions were not fully met on this candle. "
                f"This is normal — the system only trades high-quality setups."
            ),
        }

        full_detail = detail if detail else block_details.get(block_type, reason)

        event = BlockEvent(
            time=datetime.now().strftime("%H:%M:%S"),
            symbol=symbol,
            strategy=strategy,
            block_type=block_type,
            reason=reason,
            detail=full_detail,
            candle_price=candle_price,
            confidence=confidence
        )
        self.trades_blocked.append(event)

    def add_note(self, note: str):
        """Add a free-form note to the journal (e.g. market observations)"""
        self.daily_notes.append(
            f"[{datetime.now().strftime('%H:%M:%S')}] {note}"
        )

    # ------------------------------------------------------------------
    # GENERATE END-OF-DAY REPORT
    # ------------------------------------------------------------------
    def generate_report(self, daily_pnl: float = 0.0,
                         total_trades: int = 0) -> str:
        """
        Generate the full markdown report.
        Call from main.py in end_of_day().
        Saves to logs/journal_YYYY-MM-DD.md
        """
        lines = []
        now = datetime.now().strftime("%H:%M:%S")

        # ---- HEADER ----
        lines.append(f"# 📓 Daily Trading Journal — {self.date}")
        lines.append(f"**Generated:** {now} IST  |  **Mode:** {'DRY RUN (Paper Trade)' if self._is_dry_run() else 'LIVE'}\n")
        lines.append("---\n")

        # ---- MARKET REGIME ----
        lines.append("## 🌍 Market Regime (Today's Classification)\n")
        if self.regime_log:
            r = self.regime_log[-1]
            tradeable_icon = "✅ TRADEABLE" if r.is_tradeable else "🚫 NOT TRADEABLE"
            lines.append(f"| Field | Value |")
            lines.append(f"|-------|-------|")
            lines.append(f"| Trend | **{r.trend}** |")
            lines.append(f"| Volatility | **{r.volatility}** |")
            lines.append(f"| ADX | {r.adx:.1f} (>25 = trending, <20 = ranging) |")
            lines.append(f"| India VIX | {r.india_vix:.1f} |")
            lines.append(f"| BB Width Percentile | {r.bb_width_pct:.0f}th |")
            lines.append(f"| Status | {tradeable_icon} |")
            lines.append(f"| Eligible Strategies | {', '.join(r.eligible_strategies) if r.eligible_strategies else 'NONE'} |")
            lines.append(f"\n**Why this regime?**  \n{r.reason}\n")
        else:
            lines.append("_Regime data not captured_\n")

        # ---- PRE-MARKET SCANNER ----
        lines.append("---\n")
        lines.append("## 🔍 Pre-Market Scanner Results\n")
        if self.scanner_results:
            s = self.scanner_results
            lines.append(f"**Scan time:** {s.time} IST  |  **Regime:** {s.regime_summary}\n")
            lines.append(f"| Rank | Symbol | Bias | Score | Gap% | Confidence |")
            lines.append(f"|------|--------|------|-------|------|------------|")
            for i, stock in enumerate(s.top_stocks, 1):
                gap_str = f"{stock['gap_pct']*100:+.2f}%"
                lines.append(
                    f"| {i} | {stock['symbol'].replace('NSE:','')} "
                    f"| {stock['bias']} | {stock['score']:.0f} "
                    f"| {gap_str} | {stock['confidence']:.0f} |"
                )
            lines.append("")
        else:
            lines.append("_Scanner results not captured_\n")

        # ---- TRADES PLACED ----
        lines.append("---\n")
        lines.append(f"## ✅ Trades Placed Today ({len(self.trades_placed)})\n")

        if self.trades_placed:
            for i, t in enumerate(self.trades_placed, 1):
                mode = "📄 DRY RUN" if t.dry_run else "💰 LIVE"
                lines.append(f"### Trade {i}: {t.symbol} {t.direction} — {t.strategy}  {mode}")
                lines.append(f"| Field | Value |")
                lines.append(f"|-------|-------|")
                lines.append(f"| Time | {t.time} IST |")
                lines.append(f"| Symbol | {t.symbol} |")
                lines.append(f"| Direction | **{t.direction}** |")
                lines.append(f"| Strategy | {t.strategy} |")
                lines.append(f"| Entry | ₹{t.entry:.2f} |")
                lines.append(f"| Stop Loss | ₹{t.sl:.2f} (risk: ₹{abs(t.entry - t.sl):.2f}/share) |")
                lines.append(f"| Target | ₹{t.target:.2f} |")
                lines.append(f"| R:R Ratio | {t.rr:.2f}x |")
                lines.append(f"| Confidence | {t.confidence:.0f}/100 |")
                lines.append(f"| Regime | {t.regime} |")
                lines.append(f"\n**Why this trade was taken:**")
                lines.append(f"  - {t.reason}\n")

                # Find matching exit
                matching_exit = next(
                    (e for e in self.exit_log if e['symbol'] == t.symbol
                     and e['strategy'] == t.strategy), None
                )
                if matching_exit:
                    pnl_str = f"₹{matching_exit['pnl']:+,.0f}"
                    lines.append(f"**Outcome:** {matching_exit['outcome']}  |  PnL: {pnl_str}")
                    lines.append(f"**Exit:** {matching_exit['exit_price']:.2f} at {matching_exit['time']} "
                                 f"({matching_exit['exit_detail']})  "
                                 f"Hold: {matching_exit['hold_mins']} mins\n")
                else:
                    lines.append(f"**Outcome:** Position still open at report time\n")
        else:
            lines.append("_No trades were placed today._\n")

        # ---- TRADES BLOCKED ----
        lines.append("---\n")
        lines.append(f"## 🚫 Trades That Were Blocked Today ({len(self.trades_blocked)})\n")

        if self.trades_blocked:
            # Group by block type for summary
            block_summary = {}
            for b in self.trades_blocked:
                block_summary[b.block_type] = block_summary.get(b.block_type, 0) + 1

            lines.append("**Block Summary:**\n")
            block_labels = {
                'REGIME':     '🌍 Regime not suitable',
                'CONFIDENCE': '📊 Confidence too low',
                'TRADE_CAP':  '🔢 Daily trade cap hit',
                'TIME_GATE':  '⏰ After 2:00 PM cutoff',
                'RISK_GATE':  '🛡️ Risk gate triggered',
                'SENTIMENT':  '💬 Sentiment gate',
                'NO_SIGNAL':  '📉 Conditions not met',
            }
            for btype, count in sorted(block_summary.items(), key=lambda x: -x[1]):
                label = block_labels.get(btype, btype)
                lines.append(f"- {label}: **{count} times**")
            lines.append("")

            # Detail each unique block type (don't list every single no-signal)
            # Show first 3 examples of each type
            shown = {}
            for b in self.trades_blocked:
                if b.block_type == 'NO_SIGNAL':
                    continue  # too verbose — just show the count
                if shown.get(b.block_type, 0) >= 3:
                    continue

                lines.append(f"### 🚫 {b.time} — {b.symbol} ({b.strategy}) — {b.reason}")
                lines.append(f"**Block type:** {b.block_type}")
                if b.candle_price:
                    lines.append(f"**Price at block:** ₹{b.candle_price:.2f}")
                if b.confidence:
                    lines.append(f"**Signal confidence:** {b.confidence:.0f}/100 (minimum: 65)")
                lines.append(f"\n**Why it was blocked:**  \n{b.detail}\n")
                shown[b.block_type] = shown.get(b.block_type, 0) + 1

            # Explain NO_SIGNAL separately
            no_signal_count = block_summary.get('NO_SIGNAL', 0)
            if no_signal_count > 0:
                lines.append(f"### 📉 No Signal Conditions Met: {no_signal_count} candles")
                lines.append(
                    "The strategy scanned every 5-min candle but the entry conditions "
                    "(VWAP pullback confirmation, ORB volume, etc.) were not all met. "
                    "This is **normal and expected** — the system is designed to be selective. "
                    "A well-tuned strategy should only fire on 1-3 high-quality setups per day.\n"
                )
        else:
            lines.append("_No blocked trades logged today._\n")

        # ---- DAILY NOTES ----
        if self.daily_notes:
            lines.append("---\n")
            lines.append("## 📝 Notes\n")
            for note in self.daily_notes:
                lines.append(f"- {note}")
            lines.append("")

        # ---- EOD SUMMARY ----
        lines.append("---\n")
        lines.append("## 📊 End of Day Summary\n")

        wins  = [e for e in self.exit_log if e['pnl'] > 0]
        losses = [e for e in self.exit_log if e['pnl'] <= 0]
        total_pnl = sum(e['pnl'] for e in self.exit_log)

        lines.append(f"| Metric | Value |")
        lines.append(f"|--------|-------|")
        lines.append(f"| Trades Placed | {len(self.trades_placed)} |")
        lines.append(f"| Trades Exited | {len(self.exit_log)} |")
        lines.append(f"| Wins | {len(wins)} |")
        lines.append(f"| Losses | {len(losses)} |")
        win_rate = (len(wins)/len(self.exit_log)*100) if self.exit_log else 0
        lines.append(f"| Win Rate | {win_rate:.0f}% |")
        lines.append(f"| Total P&L | ₹{total_pnl:+,.0f} {'✅' if total_pnl > 0 else '❌'} |")
        lines.append(f"| Signals Blocked | {len(self.trades_blocked)} |")
        if self.regime_log:
            r = self.regime_log[-1]
            lines.append(f"| Regime | {r.trend} + {r.volatility} |")
        lines.append("")

        # Decision guidance
        lines.append("### 📋 What This Tells You\n")
        if not self.trades_placed:
            if self.regime_log and not self.regime_log[-1].is_tradeable:
                lines.append(
                    "⚠️ **No trades today because the market regime was not tradeable.** "
                    "The system correctly sat out a ranging/low-volatility day. "
                    "This is the regime filter working as intended. "
                    "These days would have been losses in backtests."
                )
            else:
                lines.append(
                    "ℹ️ **No trades today despite tradeable regime.** "
                    "Either all signals were blocked by confidence/cap/time gates, "
                    "or no strategy conditions were fully met. "
                    "This can happen — not every day has a clean setup."
                )
        elif len(self.trades_placed) >= 2:
            lines.append(
                "✅ **Full trading day — 2 trades placed (daily maximum).** "
                "Trade cap working correctly."
            )
        else:
            lines.append(
                "ℹ️ **1 trade placed today.** "
                "System was selective — only took the highest quality setup."
            )

        lines.append("\n---")
        lines.append(f"*Journal auto-generated at {now} IST by DailyJournal*")

        # Write to file
        report = "\n".join(lines)
        with open(self.report_path, 'w', encoding='utf-8') as f:
            f.write(report)

        print(f"\n📓 Daily journal saved: {self.report_path}")
        return self.report_path

    def _is_dry_run(self) -> bool:
        return any(t.dry_run for t in self.trades_placed) if self.trades_placed else True
