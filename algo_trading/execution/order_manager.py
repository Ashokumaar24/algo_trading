# ============================================================
#  execution/order_manager.py
#  Order placement and lifecycle management via KiteConnect
#
#  BUG 8 FIX: Paper trade fill noise reduced from ±2% to ±0.05%.
#
#  BUG 9 FIX: force_close_all() simulates paper exits properly.
#
#  NEW: check_paper_exits(symbol, candle) — called from on_candle_close()
#    for paper positions every candle. Without this, paper SL and target
#    are NEVER hit — every paper trade closes at EOD regardless of where
#    price went, making paper trading metrics meaningless.
#    Real edge: system fires only 0-2 trades/day, so the simulation is
#    lightweight. Each candle checks high/low vs sl/target for open positions.
#
#  FIX 6 (Critical, previous session):
#    Entry fill confirmation before placing SL/Target.
#
#  FIX 9 (Critical, previous session):
#    force_close_all (real trade) uses 'net' positions + net_quantity.
# ============================================================

import time
import threading
from datetime import datetime
from typing import Optional, Dict, Callable
from utils.logger import get_logger

logger = get_logger("order_manager")

ORDER_TYPE_LIMIT  = "LIMIT"
ORDER_TYPE_SL     = "SL"
PRODUCT_MIS       = "MIS"
EXCHANGE_NSE      = "NSE"
TRANSACTION_BUY   = "BUY"
TRANSACTION_SELL  = "SELL"
VARIETY_REGULAR   = "regular"


class OrderManager:
    """
    Manages the full order lifecycle.
    """

    def __init__(self, kite, paper_trade: bool = False,
                 on_exit_callback: Optional[Callable] = None):
        self.kite             = kite
        self.paper_trade      = paper_trade
        self._lock            = threading.Lock()
        self.open_orders: Dict[str, dict] = {}

        # Callback fired when a paper position is exited intraday.
        # Signature: on_exit_callback(symbol, pnl, exit_reason, exit_price)
        self.on_exit_callback = on_exit_callback

    # ------------------------------------------------------------------
    # PLACE TRADE
    # ------------------------------------------------------------------
    def place_order(self, symbol: str, direction: str,
                    entry: float, sl: float, target: float,
                    quantity: int) -> Optional[dict]:

        if self.paper_trade:
            return self._paper_place_order(
                symbol, direction, entry, sl, target, quantity
            )

        transaction    = TRANSACTION_BUY  if direction == "LONG"  else TRANSACTION_SELL
        sl_transaction = TRANSACTION_SELL if direction == "LONG"  else TRANSACTION_BUY

        try:
            logger.info(
                f"Placing ENTRY | {symbol} {direction} | "
                f"Qty:{quantity} Entry:{entry} SL:{sl} Target:{target}"
            )

            entry_order_id = self.kite.place_order(
                variety=VARIETY_REGULAR,
                exchange=EXCHANGE_NSE,
                tradingsymbol=symbol,
                transaction_type=transaction,
                quantity=quantity,
                product=PRODUCT_MIS,
                order_type=ORDER_TYPE_LIMIT,
                price=entry,
                tag="BOT_ENTRY"
            )

            logger.info(f"Entry order placed: {entry_order_id}")
            fill_price = self._wait_for_fill(entry_order_id, timeout=8.0)

            if fill_price is None:
                logger.warning(f"Entry {entry_order_id} did not fill — cancelling")
                self._safe_cancel_order(entry_order_id)
                return None

            logger.info(f"Entry CONFIRMED | {symbol} | Fill:{fill_price:.2f}")

            sl_trigger = (round(sl * 1.001, 2) if direction == "LONG"
                          else round(sl * 0.999, 2))
            sl_price   = (round(sl * 0.999, 2) if direction == "LONG"
                          else round(sl * 1.001, 2))

            sl_order_id = self.kite.place_order(
                variety=VARIETY_REGULAR,
                exchange=EXCHANGE_NSE,
                tradingsymbol=symbol,
                transaction_type=sl_transaction,
                quantity=quantity,
                product=PRODUCT_MIS,
                order_type=ORDER_TYPE_SL,
                price=sl_price,
                trigger_price=sl_trigger,
                tag="BOT_SL"
            )

            target_order_id = self.kite.place_order(
                variety=VARIETY_REGULAR,
                exchange=EXCHANGE_NSE,
                tradingsymbol=symbol,
                transaction_type=sl_transaction,
                quantity=quantity,
                product=PRODUCT_MIS,
                order_type=ORDER_TYPE_LIMIT,
                price=target,
                tag="BOT_TARGET"
            )

            order_info = {
                'symbol':           symbol,
                'direction':        direction,
                'entry_order_id':   entry_order_id,
                'sl_order_id':      sl_order_id,
                'target_order_id':  target_order_id,
                'entry_price':      entry,
                'fill_price':       fill_price,
                'sl':               sl,
                'target':           target,
                'quantity':         quantity,
                'placed_at':        datetime.now(),
                'status':           'OPEN',
            }

            with self._lock:
                self.open_orders[symbol] = order_info

            return order_info

        except Exception as e:
            logger.error(f"place_order failed for {symbol}: {e}")
            return None

    # ------------------------------------------------------------------
    # FIX 6: FILL CONFIRMATION POLLING
    # ------------------------------------------------------------------
    def _wait_for_fill(self, order_id: str,
                        timeout: float = 8.0) -> Optional[float]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                history = self.kite.order_history(order_id)
                if history:
                    latest = history[-1]
                    status = latest.get('status', '')
                    if status == 'COMPLETE':
                        fill_price = float(latest.get('average_price', 0))
                        return fill_price if fill_price > 0 else None
                    if status in ('CANCELLED', 'REJECTED'):
                        return None
            except Exception as e:
                logger.debug(f"Polling {order_id}: {e}")
            time.sleep(0.5)
        return None

    def _safe_cancel_order(self, order_id: str):
        try:
            self.kite.cancel_order(VARIETY_REGULAR, order_id)
        except Exception as e:
            logger.debug(f"Cancel {order_id}: {e}")

    # ------------------------------------------------------------------
    # NEW: PAPER TRADE INTRADAY EXIT SIMULATION
    # ------------------------------------------------------------------
    def check_paper_exits(self, symbol: str, candle: dict) -> Optional[dict]:
        """
        Check if the current candle's high/low has hit the SL or target
        for an open paper position on this symbol.

        CRITICAL: Without this method, paper trades NEVER hit SL or target
        intraday — they ALL close at EOD, making win/loss metrics meaningless.

        Call this from main.py on_candle_close() BEFORE checking for new signals.

        Returns:
            dict with exit info if position was closed, else None.
        """
        if not self.paper_trade:
            return None  # real trades managed by KiteConnect bracket orders

        with self._lock:
            order_info = self.open_orders.get(symbol)

        if order_info is None or order_info.get('status') != 'OPEN':
            return None

        direction   = order_info['direction']
        sl          = order_info['sl']
        target      = order_info['target']
        fill_price  = order_info.get('fill_price', order_info['entry_price'])
        quantity    = order_info['quantity']
        candle_high = candle.get('high', candle.get('close', fill_price))
        candle_low  = candle.get('low',  candle.get('close', fill_price))

        exit_price  = None
        exit_reason = None

        if direction == 'LONG':
            # Check SL first (worst case within candle)
            if candle_low <= sl:
                exit_price  = sl
                exit_reason = 'SL_HIT'
            elif candle_high >= target:
                exit_price  = target
                exit_reason = 'TARGET_HIT'
        else:  # SHORT
            if candle_high >= sl:
                exit_price  = sl
                exit_reason = 'SL_HIT'
            elif candle_low <= target:
                exit_price  = target
                exit_reason = 'TARGET_HIT'

        if exit_price is None:
            return None  # position still open

        # Calculate P&L
        if direction == 'LONG':
            pnl = (exit_price - fill_price) * quantity
        else:
            pnl = (fill_price - exit_price) * quantity

        # Deduct realistic costs
        from utils.indicators import calculate_trade_cost
        cost = calculate_trade_cost(fill_price, exit_price, quantity)
        net_pnl = pnl - cost

        hold_mins = 0
        placed_at = order_info.get('placed_at')
        if placed_at:
            delta = datetime.now() - placed_at
            hold_mins = int(delta.total_seconds() / 60)

        logger.info(
            f"PAPER EXIT | {symbol} {direction} | "
            f"Fill:₹{fill_price:.2f} → Exit:₹{exit_price:.2f} | "
            f"Reason:{exit_reason} | PnL:₹{net_pnl:+,.0f}"
        )

        self.mark_closed(symbol, exit_price, exit_reason)

        result = {
            'symbol':      symbol,
            'direction':   direction,
            'fill_price':  fill_price,
            'exit_price':  exit_price,
            'exit_reason': exit_reason,
            'gross_pnl':   pnl,
            'cost':        cost,
            'net_pnl':     net_pnl,
            'hold_mins':   hold_mins,
            'quantity':    quantity,
        }

        # Fire callback so risk_manager and daily_journal can update
        if self.on_exit_callback:
            try:
                self.on_exit_callback(
                    symbol=symbol,
                    pnl=net_pnl,
                    exit_reason=exit_reason,
                    exit_price=exit_price,
                    hold_mins=hold_mins,
                    order_info=order_info,
                )
            except Exception as e:
                logger.error(f"on_exit_callback error: {e}")

        return result

    # ------------------------------------------------------------------
    # CANCEL PROTECTIVE ORDERS
    # ------------------------------------------------------------------
    def cancel_symbol_orders(self, symbol: str):
        with self._lock:
            order_info = self.open_orders.get(symbol)
        if not order_info:
            return
        for oid in [order_info.get('sl_order_id'),
                    order_info.get('target_order_id')]:
            if oid:
                self._safe_cancel_order(oid)

    # ------------------------------------------------------------------
    # FORCE CLOSE ALL (EOD)
    # ------------------------------------------------------------------
    def force_close_all(self, reason: str = "EOD_CLOSE"):
        logger.info(f"force_close_all triggered | reason: {reason}")

        if self.paper_trade:
            logger.info("Paper trade — simulating EOD close")

            with self._lock:
                open_copy = dict(self.open_orders)

            if not open_copy:
                logger.info("No open paper positions to close.")
                return

            for symbol, order_info in open_copy.items():
                direction  = order_info.get('direction', 'LONG')
                fill_price = order_info.get('fill_price', order_info.get('entry_price', 0))
                quantity   = order_info.get('quantity', 1)

                exit_price = fill_price
                try:
                    full_sym  = (f"NSE:{symbol}" if not symbol.startswith("NSE:") else symbol)
                    ltp_data  = self.kite.ltp([full_sym])
                    exit_price = float(ltp_data[full_sym]['last_price'])
                except Exception as e:
                    logger.debug(f"LTP fetch failed for {symbol} ({e}) — using midpoint")
                    tgt = order_info.get('target', fill_price)
                    sl  = order_info.get('sl',     fill_price)
                    exit_price = round((tgt + sl) / 2, 2)

                if direction == 'LONG':
                    pnl = (exit_price - fill_price) * quantity
                else:
                    pnl = (fill_price - exit_price) * quantity

                from utils.indicators import calculate_trade_cost
                cost    = calculate_trade_cost(fill_price, exit_price, quantity)
                net_pnl = pnl - cost

                logger.info(
                    f"PAPER EOD CLOSE | {symbol} {direction} | "
                    f"Fill:₹{fill_price:.2f} → Exit:₹{exit_price:.2f} | "
                    f"Net PnL: ₹{net_pnl:+,.0f}"
                )

                self.mark_closed(symbol, exit_price, reason)

                if self.on_exit_callback:
                    try:
                        hold_mins = 0
                        if order_info.get('placed_at'):
                            delta = datetime.now() - order_info['placed_at']
                            hold_mins = int(delta.total_seconds() / 60)
                        self.on_exit_callback(
                            symbol=symbol, pnl=net_pnl,
                            exit_reason=reason, exit_price=exit_price,
                            hold_mins=hold_mins, order_info=order_info,
                        )
                    except Exception as e:
                        logger.error(f"EOD exit callback error: {e}")

            with self._lock:
                self.open_orders.clear()

            logger.info("Paper force_close_all complete")
            return

        # ── REAL TRADE PATH ──────────────────────────────────────────
        try:
            positions = self.kite.positions().get('net', [])
        except Exception as e:
            logger.error(f"Could not fetch positions: {e}")
            return

        closed_count = 0
        for p in positions:
            symbol = p.get('tradingsymbol', '')
            qty    = p.get('net_quantity', 0)
            if qty == 0:
                continue

            self.cancel_symbol_orders(symbol)
            transaction = TRANSACTION_SELL if qty > 0 else TRANSACTION_BUY
            close_qty   = abs(qty)

            try:
                self.kite.place_order(
                    variety=VARIETY_REGULAR,
                    exchange=EXCHANGE_NSE,
                    tradingsymbol=symbol,
                    transaction_type=transaction,
                    quantity=close_qty,
                    product=PRODUCT_MIS,
                    order_type="MARKET",
                    tag=f"FORCE_CLOSE_{reason}"
                )
                closed_count += 1
            except Exception as e:
                logger.error(f"Force close failed for {symbol}: {e}")

        with self._lock:
            self.open_orders.clear()

        logger.info(f"Real force_close_all complete: {closed_count} positions closed")

    # ------------------------------------------------------------------
    # PAPER TRADE SIMULATION
    # ------------------------------------------------------------------
    def _paper_place_order(self, symbol: str, direction: str,
                            entry: float, sl: float, target: float,
                            quantity: int) -> dict:
        import random
        fake_fill = round(entry * (1 + random.uniform(-0.0005, 0.0005)), 2)

        order_info = {
            'symbol':          symbol,
            'direction':       direction,
            'entry_order_id':  f"PAPER_{symbol}_{int(time.time())}",
            'sl_order_id':     f"PAPER_SL_{int(time.time())}",
            'target_order_id': f"PAPER_TGT_{int(time.time())}",
            'entry_price':     entry,
            'fill_price':      fake_fill,
            'sl':              sl,
            'target':          target,
            'quantity':        quantity,
            'placed_at':       datetime.now(),
            'status':          'OPEN',
        }

        with self._lock:
            self.open_orders[symbol] = order_info

        logger.info(
            f"PAPER TRADE | {symbol} {direction} | "
            f"Entry:₹{entry:.2f} Fill:₹{fake_fill:.2f} "
            f"SL:₹{sl:.2f} Target:₹{target:.2f} Qty:{quantity}"
        )
        return order_info

    # ------------------------------------------------------------------
    # STATUS HELPERS
    # ------------------------------------------------------------------
    def get_open_symbols(self) -> list:
        with self._lock:
            return list(self.open_orders.keys())

    def get_order_info(self, symbol: str) -> Optional[dict]:
        with self._lock:
            return self.open_orders.get(symbol)

    def mark_closed(self, symbol: str, exit_price: float, reason: str):
        with self._lock:
            if symbol in self.open_orders:
                info = self.open_orders[symbol]
                info['status']      = 'CLOSED'
                info['exit_price']  = exit_price
                info['exit_reason'] = reason
                info['exit_time']   = datetime.now()
                del self.open_orders[symbol]
