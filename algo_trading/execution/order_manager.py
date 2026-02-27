# ============================================================
#  execution/order_manager.py
#  Order placement and lifecycle management via KiteConnect
#
#  FIX 6 (Critical): Entry fill confirmation before placing SL/Target
#    - Original code placed SL and target orders after just time.sleep(0.3)
#    - If the entry order was still pending, SL/target would be orphaned
#      (attached to no position), creating open risk with no protection
#    - Now: _wait_for_fill() polls order_history() every 0.5s up to 8s
#    - If entry does not fill within 8 seconds, the entry order is
#      cancelled and None is returned — no SL or target is ever placed
#      without a confirmed fill
#
#  FIX 9 (Critical): force_close_all uses 'net' positions + net_quantity
#    - Original read positions().get('day', []) which includes all
#      intraday trades including already-exited positions
#    - qty = p.get('quantity') returned GROSS quantity (entries + exits)
#    - Fix: use positions().get('net', []) and p.get('net_quantity')
#    - 'net' shows only open positions; net_quantity is + for long, - for short
# ============================================================

import time
import threading
from datetime import datetime
from typing import Optional, Dict, Tuple
from utils.logger import get_logger

logger = get_logger("order_manager")

# Kite order types
ORDER_TYPE_LIMIT  = "LIMIT"
ORDER_TYPE_SL     = "SL"
PRODUCT_MIS       = "MIS"
EXCHANGE_NSE      = "NSE"
TRANSACTION_BUY   = "BUY"
TRANSACTION_SELL  = "SELL"
VARIETY_REGULAR   = "regular"


class OrderManager:
    """
    Manages the full order lifecycle:
      1. Place entry order
      2. Poll for fill confirmation (FIX 6)
      3. Place SL and target bracket only after confirmed fill
      4. Track open orders
      5. Force-close all positions at EOD (FIX 9)
    """

    def __init__(self, kite, paper_trade: bool = False):
        self.kite        = kite
        self.paper_trade = paper_trade
        self._lock       = threading.Lock()
        self.open_orders: Dict[str, dict] = {}  # {symbol: order_info}

    # ------------------------------------------------------------------
    # PLACE TRADE (entry → wait for fill → SL + target)
    # ------------------------------------------------------------------
    def place_order(self, symbol: str, direction: str,
                    entry: float, sl: float, target: float,
                    quantity: int) -> Optional[dict]:
        """
        Place a full bracket trade: entry → confirmed fill → SL + target.

        FIX 6: SL and target are ONLY placed after the entry order is
        confirmed COMPLETE via order_history polling (up to 8 seconds).

        Args:
            symbol:    e.g. "RELIANCE"
            direction: "LONG" or "SHORT"
            entry:     limit price
            sl:        stop-loss price
            target:    target price
            quantity:  number of shares

        Returns:
            dict with order IDs and fill details, or None if entry failed
        """
        if self.paper_trade:
            return self._paper_place_order(symbol, direction, entry, sl, target, quantity)

        transaction   = TRANSACTION_BUY if direction == "LONG" else TRANSACTION_SELL
        sl_transaction = TRANSACTION_SELL if direction == "LONG" else TRANSACTION_BUY

        try:
            # --- Step 1: Place entry order ---
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

            # --- Step 2: FIX 6 — Wait for confirmed fill (up to 8 seconds) ---
            fill_price = self._wait_for_fill(entry_order_id, timeout=8.0)

            if fill_price is None:
                logger.warning(
                    f"Entry order {entry_order_id} did NOT fill within 8s — "
                    f"cancelling. No SL or target will be placed."
                )
                self._safe_cancel_order(entry_order_id)
                return None

            logger.info(
                f"Entry CONFIRMED | {symbol} | OrderID:{entry_order_id} | "
                f"FillPrice:{fill_price:.2f}"
            )

            # --- Step 3: Place SL order (now safe — entry confirmed) ---
            sl_trigger = round(sl * 1.001, 2) if direction == "LONG" else round(sl * 0.999, 2)
            sl_price   = round(sl * 0.999, 2) if direction == "LONG" else round(sl * 1.001, 2)

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

            logger.info(f"SL order placed: {sl_order_id} @ {sl_price}")

            # --- Step 4: Place target order ---
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

            logger.info(f"Target order placed: {target_order_id} @ {target}")

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
    def _wait_for_fill(self, order_id: str, timeout: float = 8.0) -> Optional[float]:
        """
        Poll order history every 0.5 seconds until the order is COMPLETE.

        Returns:
            average_price (float) if filled, or None if cancelled/rejected/timeout
        """
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
                        logger.warning(
                            f"Order {order_id} is {status}: "
                            f"{latest.get('status_message', '')}"
                        )
                        return None

                    logger.debug(f"Order {order_id} status: {status} — waiting...")

            except Exception as e:
                logger.debug(f"Polling {order_id}: {e}")

            time.sleep(0.5)

        logger.warning(f"Order {order_id} fill timeout after {timeout}s")
        return None

    def _safe_cancel_order(self, order_id: str):
        """Cancel an order, ignoring errors if it's already done"""
        try:
            self.kite.cancel_order(VARIETY_REGULAR, order_id)
            logger.info(f"Order {order_id} cancelled")
        except Exception as e:
            logger.debug(f"Cancel order {order_id}: {e} (may already be terminal)")

    # ------------------------------------------------------------------
    # CANCEL PROTECTIVE ORDERS
    # ------------------------------------------------------------------
    def cancel_symbol_orders(self, symbol: str):
        """Cancel all open SL and target orders for a symbol"""
        with self._lock:
            order_info = self.open_orders.get(symbol)
        if not order_info:
            return

        for oid in [order_info.get('sl_order_id'), order_info.get('target_order_id')]:
            if oid:
                self._safe_cancel_order(oid)

    # ------------------------------------------------------------------
    # FIX 9: FORCE CLOSE ALL — uses 'net' positions and net_quantity
    # ------------------------------------------------------------------
    def force_close_all(self, reason: str = "EOD_CLOSE"):
        """
        Close all open net positions at market.

        FIX 9:
        - Uses positions().get('net', []) — only OPEN positions
          (not 'day' which includes already-exited trades)
        - Reads net_quantity (+ = long, - = short, 0 = flat/already closed)
        - Previously used 'quantity' which was gross lot size and
          could attempt to close positions that no longer existed
        """
        logger.info(f"force_close_all triggered | reason: {reason}")

        if self.paper_trade:
            logger.info("Paper trade — skipping force close")
            return

        try:
            positions = self.kite.positions().get('net', [])   # FIX 9
        except Exception as e:
            logger.error(f"Could not fetch positions for force close: {e}")
            return

        closed_count = 0

        for p in positions:
            symbol = p.get('tradingsymbol', '')
            # FIX 9: Use net_quantity — positive = long, negative = short
            qty    = p.get('net_quantity', 0)

            if qty == 0:
                continue  # already flat

            # Cancel protective orders first to avoid double-fill
            self.cancel_symbol_orders(symbol)

            transaction = TRANSACTION_SELL if qty > 0 else TRANSACTION_BUY
            close_qty   = abs(qty)

            try:
                logger.info(
                    f"Force closing {symbol} | {transaction} "
                    f"{close_qty} @ MARKET | Reason:{reason}"
                )
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

        logger.info(f"force_close_all complete: {closed_count} positions closed")

    # ------------------------------------------------------------------
    # PAPER TRADE SIMULATION
    # ------------------------------------------------------------------
    def _paper_place_order(self, symbol, direction, entry, sl, target, quantity):
        """Simulate order fill immediately for paper trading"""
        import random
        fake_fill = round(entry + random.uniform(-0.02, 0.02) * entry, 2)

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

        logger.info(f"PAPER TRADE | {symbol} {direction} | Fill:{fake_fill}")
        return order_info

    # ------------------------------------------------------------------
    # STATUS HELPERS
    # ------------------------------------------------------------------
    def get_open_symbols(self):
        with self._lock:
            return list(self.open_orders.keys())

    def get_order_info(self, symbol: str) -> Optional[dict]:
        with self._lock:
            return self.open_orders.get(symbol)

    def mark_closed(self, symbol: str, exit_price: float, reason: str):
        with self._lock:
            if symbol in self.open_orders:
                self.open_orders[symbol]['status']     = 'CLOSED'
                self.open_orders[symbol]['exit_price'] = exit_price
                self.open_orders[symbol]['exit_reason'] = reason
                self.open_orders[symbol]['exit_time']  = datetime.now()
                del self.open_orders[symbol]
