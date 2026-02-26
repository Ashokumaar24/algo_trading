# ============================================================
#  execution/order_manager.py
#  Order placement, modification, and monitoring via KiteConnect
#
#  FIX: Zerodha disabled Bracket Orders (BO) for equity in 2020.
#       Replaced with:
#         - Regular MIS LIMIT order for entry
#         - SL-M order for stop loss (tracked by order_id)
#         - LIMIT order for target (tracked by order_id)
#       SL modifications now cancel + re-place the SL-M order.
# ============================================================

import time
from datetime import datetime
from typing import Optional, Dict, Tuple

from kiteconnect import KiteConnect

from strategies.base_strategy import Signal, Direction, SignalStatus
from risk.risk_manager import RiskManager
from regime.market_regime import MarketRegime
from utils.logger import get_logger, TradeLogger

logger    = get_logger("order_manager")
trade_log = TradeLogger()


class ActivePosition:
    """Tracks all order IDs for a single open position"""
    def __init__(self, signal: Signal, entry_order_id: str,
                 sl_order_id: str, target_order_id: str):
        self.signal           = signal
        self.entry_order_id   = entry_order_id
        self.sl_order_id      = sl_order_id
        self.target_order_id  = target_order_id
        self.current_sl       = signal.stop_loss
        self.entry_time       = datetime.now()


class OrderManager:
    """
    Handles all order lifecycle operations:
    - Place MIS LIMIT entry + SL-M stop + LIMIT target orders
    - Monitor open positions for trailing SL updates
    - Force-close all positions at 3:15 PM
    - Log completed trades for performance analysis
    """

    def __init__(self, kite: KiteConnect, risk_manager: RiskManager):
        self.kite             = kite
        self.risk_manager     = risk_manager
        self.positions: Dict[str, ActivePosition] = {}   # entry_order_id → position

    # ------------------------------------------------------------------
    # PLACE ORDER
    # ------------------------------------------------------------------
    def place_order(self, signal: Signal, regime: Optional[MarketRegime] = None,
                    dry_run: bool = False) -> Optional[str]:
        """
        Place entry + SL + target orders for the given signal.

        FIX: Was using variety='bo' (Bracket Order) which Zerodha disabled.
             Now uses three separate regular MIS orders:
               1. LIMIT order  → entry
               2. SL-M order   → stop loss  (trigger = signal.stop_loss)
               3. LIMIT order  → target

        Args:
            signal:   Validated Signal object with position_size set
            regime:   Current market regime (for size multiplier)
            dry_run:  If True, log but don't actually place orders

        Returns:
            entry_order_id string or None on failure
        """
        if not signal.is_valid():
            logger.error(f"Invalid signal rejected: {signal}")
            return None

        if not self.risk_manager.can_trade():
            logger.warning(f"Risk gate blocked trade for {signal.symbol}")
            return None

        if not self.risk_manager.passes_confidence_gate(signal):
            return None

        if signal.position_size <= 0:
            size_mult = regime.size_multiplier if regime else 1.0
            signal.position_size = self.risk_manager.calculate_position_size(
                signal, size_mult
            )

        if signal.position_size <= 0:
            logger.error(f"Zero position size for {signal.symbol} — skipping")
            return None

        qty         = signal.position_size
        tradingsym  = signal.symbol.replace("NSE:", "")
        side        = 'BUY' if signal.direction == Direction.LONG else 'SELL'
        sl_side     = 'SELL' if signal.direction == Direction.LONG else 'BUY'

        logger.info(
            f"Placing {'DRY RUN ' if dry_run else ''}Orders | "
            f"{signal.symbol} {side} {qty} @ {signal.entry:.2f} | "
            f"SL:{signal.stop_loss:.2f} | Target:{signal.target:.2f} | "
            f"Strategy:{signal.strategy} | Confidence:{signal.confidence:.0f}"
        )

        # ---- DRY RUN ----
        if dry_run:
            fake_id = f"DRY_{tradingsym}_{int(time.time())}"
            signal.order_id = fake_id
            signal.status   = SignalStatus.ACTIVE
            pos = ActivePosition(signal, fake_id, f"{fake_id}_SL", f"{fake_id}_TGT")
            self.positions[fake_id] = pos
            self.risk_manager.record_trade_entry()
            logger.info(f"[DRY RUN] Orders simulated: entry={fake_id}")
            return fake_id

        # ---- LIVE: Place 3 separate orders ----
        entry_order_id  = None
        sl_order_id     = None
        target_order_id = None

        try:
            # --- 1. Entry order (LIMIT) ---
            entry_order_id = str(self.kite.place_order(
                tradingsymbol=tradingsym,
                exchange="NSE",
                transaction_type=side,
                quantity=qty,
                order_type="LIMIT",
                price=signal.entry,
                product="MIS",
                variety="regular",
                tag=f"ENTRY_{signal.strategy[:6]}"
            ))
            logger.info(f"Entry order placed: {entry_order_id}")

            # Small delay to avoid rapid-fire order rejection
            time.sleep(0.3)

            # --- 2. Stop Loss order (SL-M — Stop Loss Market) ---
            sl_trigger = signal.stop_loss
            sl_order_id = str(self.kite.place_order(
                tradingsymbol=tradingsym,
                exchange="NSE",
                transaction_type=sl_side,
                quantity=qty,
                order_type="SL-M",
                trigger_price=round(sl_trigger, 2),
                product="MIS",
                variety="regular",
                tag=f"SL_{signal.strategy[:6]}"
            ))
            logger.info(f"SL-M order placed: {sl_order_id} @ trigger {sl_trigger:.2f}")

            time.sleep(0.3)

            # --- 3. Target order (LIMIT) ---
            target_order_id = str(self.kite.place_order(
                tradingsymbol=tradingsym,
                exchange="NSE",
                transaction_type=sl_side,
                quantity=qty,
                order_type="LIMIT",
                price=signal.target,
                product="MIS",
                variety="regular",
                tag=f"TGT_{signal.strategy[:6]}"
            ))
            logger.info(f"Target order placed: {target_order_id} @ {signal.target:.2f}")

        except Exception as e:
            logger.error(f"Order placement failed for {signal.symbol}: {e}")
            # Cancel any partial orders placed before the failure
            self._cancel_partial_orders(entry_order_id, sl_order_id, target_order_id)
            return None

        signal.order_id = entry_order_id
        signal.status   = SignalStatus.ACTIVE

        pos = ActivePosition(signal, entry_order_id, sl_order_id, target_order_id)
        self.positions[entry_order_id] = pos
        self.risk_manager.record_trade_entry()

        logger.info(
            f"ORDER SET PLACED ✓ | Entry:{entry_order_id} "
            f"SL:{sl_order_id} Target:{target_order_id} | {signal}"
        )
        return entry_order_id

    # ------------------------------------------------------------------
    # CANCEL PARTIAL ORDERS (called on partial placement failure)
    # ------------------------------------------------------------------
    def _cancel_partial_orders(self, *order_ids):
        """Cancel any orders that were placed before a failure"""
        for oid in order_ids:
            if oid is None:
                continue
            try:
                self.kite.cancel_order(variety="regular", order_id=oid)
                logger.info(f"Cancelled partial order: {oid}")
            except Exception as e:
                logger.warning(f"Could not cancel order {oid}: {e}")

    # ------------------------------------------------------------------
    # MONITOR POSITIONS
    # ------------------------------------------------------------------
    def monitor_positions(self, symbol_prices: Dict[str, float]):
        """
        Check all active positions for:
        - Trailing SL updates
        - SL or Target hit (via order status polling)
        - Time-based exits
        """
        for entry_oid, pos in list(self.positions.items()):
            signal = pos.signal
            if signal.status != SignalStatus.ACTIVE:
                continue

            symbol = signal.symbol
            price  = symbol_prices.get(symbol)
            if price is None:
                continue

            # Check if SL or target order has been filled
            self._sync_order_status(entry_oid, pos)

            if signal.status != SignalStatus.ACTIVE:
                continue

            # Trailing SL
            new_sl = self.risk_manager.should_trail_stop(signal, price, pos.current_sl)
            if new_sl:
                self._modify_sl(entry_oid, pos, new_sl)

            # Time-based exit
            is_loss = (
                (signal.direction == Direction.LONG  and price < signal.entry) or
                (signal.direction == Direction.SHORT and price > signal.entry)
            )
            time_action = self.risk_manager.check_time_exit(
                has_open_position=True, is_loss=is_loss
            )
            if time_action in ('FORCE_CLOSE', 'CLOSE_LOSERS'):
                logger.info(f"Time-based exit [{time_action}] for {symbol}")
                self.close_position(entry_oid, price, reason=time_action)

    # ------------------------------------------------------------------
    # SYNC ORDER STATUS (check if SL or target filled externally)
    # ------------------------------------------------------------------
    def _sync_order_status(self, entry_oid: str, pos: ActivePosition):
        """Poll order status to detect if SL or target was hit by exchange"""
        try:
            orders = self.kite.orders()
            orders_by_id = {str(o['order_id']): o for o in orders}

            sl_order  = orders_by_id.get(pos.sl_order_id)
            tgt_order = orders_by_id.get(pos.target_order_id)

            if sl_order and sl_order['status'] == 'COMPLETE':
                exit_price = sl_order.get('average_price', pos.current_sl)
                self._record_closed_position(
                    entry_oid, pos, exit_price, 'SL_HIT'
                )

            elif tgt_order and tgt_order['status'] == 'COMPLETE':
                exit_price = tgt_order.get('average_price', pos.signal.target)
                self._record_closed_position(
                    entry_oid, pos, exit_price, 'TARGET_HIT'
                )

        except Exception as e:
            logger.debug(f"Order status sync error for {entry_oid}: {e}")

    # ------------------------------------------------------------------
    # MODIFY SL (cancel + re-place SL-M order with new trigger)
    # ------------------------------------------------------------------
    def _modify_sl(self, entry_oid: str, pos: ActivePosition, new_sl: float):
        """
        FIX: BO had a simple modify endpoint.
             With regular orders we must cancel the old SL-M and place a new one.
        """
        signal    = pos.signal
        tradingsym = signal.symbol.replace("NSE:", "")
        sl_side   = 'SELL' if signal.direction == Direction.LONG else 'BUY'
        qty       = signal.position_size

        try:
            # Cancel old SL order
            self.kite.cancel_order(variety="regular", order_id=pos.sl_order_id)
            time.sleep(0.2)

            # Place new SL-M order
            new_sl_order_id = str(self.kite.place_order(
                tradingsymbol=tradingsym,
                exchange="NSE",
                transaction_type=sl_side,
                quantity=qty,
                order_type="SL-M",
                trigger_price=round(new_sl, 2),
                product="MIS",
                variety="regular",
                tag="TRAIL_SL"
            ))

            old_sl            = pos.current_sl
            pos.sl_order_id   = new_sl_order_id
            pos.current_sl    = new_sl

            logger.info(
                f"SL Trailed | {signal.symbol} | "
                f"{old_sl:.2f} → {new_sl:.2f} | New SL order: {new_sl_order_id}"
            )

        except Exception as e:
            logger.error(f"SL modification failed for {entry_oid}: {e}")

    # ------------------------------------------------------------------
    # CLOSE POSITION (manual / time-based exit)
    # ------------------------------------------------------------------
    def close_position(self, entry_oid: str, exit_price: float,
                       reason: str = "manual"):
        """Close a position by cancelling pending SL/target and placing market exit"""
        pos = self.positions.get(entry_oid)
        if not pos:
            return

        signal    = pos.signal
        tradingsym = signal.symbol.replace("NSE:", "")
        exit_side = 'SELL' if signal.direction == Direction.LONG else 'BUY'
        qty       = signal.position_size

        # Cancel open SL and target orders first
        for oid in [pos.sl_order_id, pos.target_order_id]:
            if oid:
                try:
                    self.kite.cancel_order(variety="regular", order_id=oid)
                except Exception:
                    pass

        # Place market exit
        try:
            self.kite.place_order(
                tradingsymbol=tradingsym,
                exchange="NSE",
                transaction_type=exit_side,
                quantity=qty,
                order_type="MARKET",
                product="MIS",
                variety="regular",
                tag="MANUAL_EXIT"
            )
        except Exception as e:
            logger.error(f"Market exit failed for {signal.symbol}: {e}")

        self._record_closed_position(entry_oid, pos, exit_price, reason)

    # ------------------------------------------------------------------
    # RECORD CLOSED POSITION
    # ------------------------------------------------------------------
    def _record_closed_position(self, entry_oid: str, pos: ActivePosition,
                                 exit_price: float, reason: str):
        """Log P&L and clean up position tracking"""
        signal = pos.signal
        if signal.direction == Direction.LONG:
            pnl = (exit_price - signal.entry) * signal.position_size
        else:
            pnl = (signal.entry - exit_price) * signal.position_size

        outcome = "WIN" if pnl > 0 else "LOSS"
        signal.status = SignalStatus.HIT_TARGET if reason == 'TARGET_HIT' else SignalStatus.HIT_SL

        self.risk_manager.record_trade_exit(pnl)

        hold_mins = int((datetime.now() - pos.entry_time).total_seconds() / 60)

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

        self.positions.pop(entry_oid, None)

        logger.info(
            f"Position Closed | {signal.symbol} | PnL: ₹{pnl:+,.0f} | "
            f"{outcome} | Reason:{reason}"
        )

    # ------------------------------------------------------------------
    # FORCE CLOSE ALL (3:15 PM)
    # ------------------------------------------------------------------
    def force_close_all(self):
        """Emergency / EOD close of ALL positions."""
        logger.warning("FORCE CLOSING ALL POSITIONS (3:15 PM)")

        # First cancel all pending SL and target orders
        for pos in list(self.positions.values()):
            for oid in [pos.sl_order_id, pos.target_order_id]:
                if oid:
                    try:
                        self.kite.cancel_order(variety="regular", order_id=oid)
                    except Exception:
                        pass

        # Then close net positions via market orders
        try:
            positions = self.kite.positions().get('day', [])
            for p in positions:
                qty = p.get('quantity', 0)
                if qty == 0:
                    continue
                side = 'SELL' if qty > 0 else 'BUY'
                try:
                    self.kite.place_order(
                        tradingsymbol=p['tradingsymbol'],
                        exchange=p['exchange'],
                        transaction_type=side,
                        quantity=abs(qty),
                        order_type='MARKET',
                        product='MIS',
                        variety='regular',
                        tag='EOD_CLOSE'
                    )
                    logger.info(f"Force closed: {p['tradingsymbol']} qty={abs(qty)}")
                except Exception as e:
                    logger.error(f"Force close failed for {p['tradingsymbol']}: {e}")
        except Exception as e:
            logger.error(f"force_close_all failed: {e}")

        self.positions.clear()

    # ------------------------------------------------------------------
    # STATUS
    # ------------------------------------------------------------------
    def get_active_count(self) -> int:
        return sum(
            1 for pos in self.positions.values()
            if pos.signal.status == SignalStatus.ACTIVE
        )
