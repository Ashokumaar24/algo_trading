# ============================================================
#  execution/order_manager.py
#  Order placement, modification, and monitoring via KiteConnect
# ============================================================

import time
from datetime import datetime
from typing import Optional, Dict

from kiteconnect import KiteConnect

from strategies.base_strategy import Signal, Direction, SignalStatus
from risk.risk_manager import RiskManager
from regime.market_regime import MarketRegime
from utils.logger import get_logger, TradeLogger

logger      = get_logger("order_manager")
trade_log   = TradeLogger()


class OrderManager:
    """
    Handles all order lifecycle operations:
    - Place bracket orders (BO) via KiteConnect
    - Monitor open positions for trailing SL updates
    - Force-close all positions at 3:15 PM
    - Log completed trades for performance analysis
    """

    def __init__(self, kite: KiteConnect, risk_manager: RiskManager):
        self.kite          = kite
        self.risk_manager  = risk_manager
        self.active_signals: Dict[str, Signal]  = {}   # order_id → Signal
        self.active_sl:     Dict[str, float]    = {}   # order_id → current SL

    # ------------------------------------------------------------------
    # PLACE ORDER
    # ------------------------------------------------------------------
    def place_order(self, signal: Signal, regime: Optional[MarketRegime] = None,
                     dry_run: bool = False) -> Optional[str]:
        """
        Place a bracket order for the given signal.

        Args:
            signal:   Validated Signal object with position_size set
            regime:   Current market regime (for size multiplier)
            dry_run:  If True, log the order but don't actually place it

        Returns:
            order_id string or None on failure
        """
        # --- Validate ---
        if not signal.is_valid():
            logger.error(f"Invalid signal rejected: {signal}")
            return None

        if not self.risk_manager.can_trade():
            logger.warning(f"Risk gate blocked trade for {signal.symbol}")
            return None

        if not self.risk_manager.passes_confidence_gate(signal):
            return None

        # --- Calculate position size if not already set ---
        if signal.position_size <= 0:
            size_mult = regime.size_multiplier if regime else 1.0
            signal.position_size = self.risk_manager.calculate_position_size(
                signal, size_mult
            )

        if signal.position_size <= 0:
            logger.error(f"Zero position size for {signal.symbol} — skipping")
            return None

        qty  = signal.position_size
        side = 'BUY' if signal.direction == Direction.LONG else 'SELL'

        sq_off = abs(signal.target - signal.entry)
        sl_pts = abs(signal.entry - signal.stop_loss)

        logger.info(
            f"Placing {'DRY RUN ' if dry_run else ''}Order | "
            f"{signal.symbol} {side} {qty} @ {signal.entry:.2f} | "
            f"SL:{sl_pts:.2f} pts | Target:{sq_off:.2f} pts | "
            f"Strategy:{signal.strategy} | Confidence:{signal.confidence:.0f}"
        )

        if dry_run:
            fake_order_id = f"DRY_{signal.symbol}_{int(time.time())}"
            signal.order_id = fake_order_id
            signal.status   = SignalStatus.ACTIVE
            self.active_signals[fake_order_id] = signal
            self.active_sl[fake_order_id]      = signal.stop_loss
            self.risk_manager.record_trade_entry()
            logger.info(f"[DRY RUN] Order simulated: {fake_order_id}")
            return fake_order_id

        # --- Place real Bracket Order ---
        try:
            order_params = dict(
                tradingsymbol=signal.symbol.replace("NSE:", ""),
                exchange="NSE",
                transaction_type=side,
                quantity=qty,
                order_type="LIMIT",
                price=signal.entry,
                product="MIS",           # Intraday
                variety="bo",            # Bracket Order
                stoploss=round(sl_pts, 2),
                squareoff=round(sq_off, 2),
                trailing_stoploss=0,     # We trail manually
            )

            order_id = self.kite.place_order(**order_params)

            signal.order_id = str(order_id)
            signal.status   = SignalStatus.ACTIVE
            self.active_signals[signal.order_id] = signal
            self.active_sl[signal.order_id]      = signal.stop_loss

            self.risk_manager.record_trade_entry()

            logger.info(f"ORDER PLACED ✓ | ID:{order_id} | {signal}")
            return signal.order_id

        except Exception as e:
            logger.error(f"Order placement failed for {signal.symbol}: {e}")
            return None

    # ------------------------------------------------------------------
    # MONITOR POSITIONS (call on every tick or candle close)
    # ------------------------------------------------------------------
    def monitor_positions(self, symbol_prices: Dict[str, float]):
        """
        Check all active positions for:
        - Trailing SL updates
        - Target hit (informational — BO handles this)
        - Time-based exits

        Args:
            symbol_prices: Dict {symbol: current_price}
        """
        for order_id, signal in list(self.active_signals.items()):
            if signal.status != SignalStatus.ACTIVE:
                continue

            symbol  = signal.symbol
            price   = symbol_prices.get(symbol)
            if price is None:
                continue

            current_sl = self.active_sl.get(order_id, signal.stop_loss)

            # Check trailing SL
            new_sl = self.risk_manager.should_trail_stop(signal, price, current_sl)
            if new_sl:
                self._modify_sl(order_id, signal, new_sl)

            # Time-based exit check
            is_loss = (
                (signal.direction == Direction.LONG  and price < signal.entry) or
                (signal.direction == Direction.SHORT and price > signal.entry)
            )
            time_action = self.risk_manager.check_time_exit(
                has_open_position=True, is_loss=is_loss
            )
            if time_action in ('FORCE_CLOSE', 'CLOSE_LOSERS'):
                logger.info(f"Time-based exit triggered [{time_action}] for {symbol}")
                self.close_position(order_id, price, reason=time_action)

    # ------------------------------------------------------------------
    # MODIFY SL
    # ------------------------------------------------------------------
    def _modify_sl(self, order_id: str, signal: Signal, new_sl: float):
        """Modify bracket order SL via KiteConnect"""
        try:
            sl_points = abs(signal.entry - new_sl)
            self.kite.modify_order(
                variety="bo",
                order_id=order_id,
                stoploss=round(sl_points, 2)
            )
            self.active_sl[order_id] = new_sl
            logger.info(
                f"SL Modified | {signal.symbol} | New SL: {new_sl:.2f}"
            )
        except Exception as e:
            logger.error(f"SL modification failed for {order_id}: {e}")

    # ------------------------------------------------------------------
    # CLOSE POSITION
    # ------------------------------------------------------------------
    def close_position(self, order_id: str, exit_price: float,
                        reason: str = "manual"):
        """Close an open bracket order position"""
        signal = self.active_signals.get(order_id)
        if not signal:
            return

        try:
            self.kite.exit_order(variety="bo", order_id=order_id)
        except Exception as e:
            logger.error(f"Exit order failed for {order_id}: {e}")

        # Calculate PnL
        if signal.direction == Direction.LONG:
            pnl = (exit_price - signal.entry) * signal.position_size
        else:
            pnl = (signal.entry - exit_price) * signal.position_size

        outcome = "WIN" if pnl > 0 else "LOSS"
        signal.status = SignalStatus.HIT_TARGET if pnl > 0 else SignalStatus.HIT_SL

        self.risk_manager.record_trade_exit(pnl)

        # Log trade
        entry_time = signal.timestamp
        hold_mins  = int((datetime.now() - entry_time).total_seconds() / 60)

        trade_log.log_trade(
            symbol=signal.symbol,
            strategy=signal.strategy,
            direction=signal.direction.value,
            entry=signal.entry,
            sl=signal.stop_loss,
            target=signal.target,
            exit_price=round(exit_price, 2),
            quantity=signal.position_size,
            pnl=round(pnl, 2),
            pnl_pct=round(pnl / (signal.entry * signal.position_size) * 100, 3),
            outcome=outcome,
            regime=signal.regime,
            confidence=signal.confidence,
            hold_minutes=hold_mins,
            notes=f"{reason} | {signal.notes}"
        )

        del self.active_signals[order_id]
        self.active_sl.pop(order_id, None)

        logger.info(
            f"Position Closed | {signal.symbol} | PnL: ₹{pnl:+,.0f} | "
            f"{outcome} | Reason:{reason}"
        )

    # ------------------------------------------------------------------
    # FORCE CLOSE ALL
    # ------------------------------------------------------------------
    def force_close_all(self):
        """Emergency / EOD close of ALL positions. Call at 3:15 PM."""
        logger.warning("FORCE CLOSING ALL POSITIONS (3:15 PM)")

        try:
            positions = self.kite.positions().get('day', [])
            for pos in positions:
                qty = pos.get('quantity', 0)
                if qty == 0:
                    continue

                side = 'SELL' if qty > 0 else 'BUY'
                try:
                    self.kite.place_order(
                        tradingsymbol=pos['tradingsymbol'],
                        exchange=pos['exchange'],
                        transaction_type=side,
                        quantity=abs(qty),
                        order_type='MARKET',
                        product='MIS',
                        variety='regular'
                    )
                    logger.info(
                        f"Force closed: {pos['tradingsymbol']} {abs(qty)} shares"
                    )
                except Exception as e:
                    logger.error(
                        f"Force close failed for {pos['tradingsymbol']}: {e}"
                    )

        except Exception as e:
            logger.error(f"force_close_all failed: {e}")

    # ------------------------------------------------------------------
    # STATUS
    # ------------------------------------------------------------------
    def get_active_count(self) -> int:
        return len([s for s in self.active_signals.values()
                    if s.status == SignalStatus.ACTIVE])
