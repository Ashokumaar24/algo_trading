# ============================================================
#  execution/order_manager.py
#  Order placement and lifecycle management via KiteConnect
#
#  BUG 8 FIX: Paper trade fill noise reduced from ±2% to ±0.05%.
#    The old ±2% was larger than most SL distances (₹5–15 on a ₹1000
#    stock), meaning paper fills routinely appeared to have breached
#    the SL before the trade even started. A real limit order fills
#    at or very close to the limit price — ±0.05% is realistic.
#
#  BUG 9 FIX: force_close_all() now simulates paper exits properly.
#    Previously returned immediately in paper mode with no action,
#    so paper positions were never closed, P&L was always ₹0, and
#    the EOD summary was meaningless. Now fetches live LTP for each
#    open paper position, calculates P&L, and calls mark_closed().
#
#  FIX 6 (Critical, previous session):
#    Entry fill confirmation before placing SL/Target.
#    _wait_for_fill() polls order_history() every 0.5s up to 8s.
#    If entry does not fill, it is cancelled and None returned.
#
#  FIX 9 (Critical, previous session):
#    force_close_all (real trade) uses 'net' positions + net_quantity.
#    'net' shows only open positions; net_quantity is +long/-short/0flat.
# ============================================================

import time
import threading
from datetime import datetime
from typing import Optional, Dict, Tuple
from utils.logger import get_logger

logger = get_logger("order_manager")

# Kite order types / constants
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
      5. Force-close all positions at EOD (FIX 9 / BUG 9)
    """

    def __init__(self, kite, paper_trade: bool = False):
        self.kite        = kite
        self.paper_trade = paper_trade
        self._lock       = threading.Lock()
        self.open_orders: Dict[str, dict] = {}   # {symbol: order_info}

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
            symbol:    e.g. "RELIANCE"  (no NSE: prefix)
            direction: "LONG" or "SHORT"
            entry:     limit price
            sl:        stop-loss price
            target:    target price
            quantity:  number of shares

        Returns:
            dict with order IDs and fill details, or None if entry failed
        """
        if self.paper_trade:
            return self._paper_place_order(
                symbol, direction, entry, sl, target, quantity
            )

        transaction    = TRANSACTION_BUY  if direction == "LONG"  else TRANSACTION_SELL
        sl_transaction = TRANSACTION_SELL if direction == "LONG"  else TRANSACTION_BUY

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
                f"Entry CONFIRMED | {symbol} | "
                f"OrderID:{entry_order_id} | FillPrice:{fill_price:.2f}"
            )

            # --- Step 3: Place SL order (safe — entry confirmed) ---
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
    def _wait_for_fill(self, order_id: str,
                        timeout: float = 8.0) -> Optional[float]:
        """
        Poll order history every 0.5 seconds until the order is COMPLETE.

        Returns:
            average_price (float) if filled, None if cancelled/rejected/timeout
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
        """Cancel an order, ignoring errors if it's already in terminal state."""
        try:
            self.kite.cancel_order(VARIETY_REGULAR, order_id)
            logger.info(f"Order {order_id} cancelled")
        except Exception as e:
            logger.debug(
                f"Cancel order {order_id}: {e} (may already be terminal)"
            )

    # ------------------------------------------------------------------
    # CANCEL PROTECTIVE ORDERS
    # ------------------------------------------------------------------
    def cancel_symbol_orders(self, symbol: str):
        """Cancel all open SL and target orders for a symbol."""
        with self._lock:
            order_info = self.open_orders.get(symbol)
        if not order_info:
            return
        for oid in [order_info.get('sl_order_id'),
                    order_info.get('target_order_id')]:
            if oid:
                self._safe_cancel_order(oid)

    # ------------------------------------------------------------------
    # FORCE CLOSE ALL
    # FIX 9 (prev session): Real trade uses 'net' positions + net_quantity
    # BUG 9 FIX: Paper trade now simulates exits properly
    # ------------------------------------------------------------------
    def force_close_all(self, reason: str = "EOD_CLOSE"):
        """
        Close all open positions at market.

        Paper trade (BUG 9 FIX):
          - Previously returned immediately → positions never closed, P&L = ₹0.
          - Now fetches live LTP for each open paper position.
          - Calculates P&L and calls mark_closed() so risk_manager tracks it.

        Real trade (FIX 9 from previous session):
          - Uses positions().get('net', []) — only OPEN positions.
          - Reads net_quantity (+ = long, − = short, 0 = flat/closed already).
        """
        logger.info(f"force_close_all triggered | reason: {reason}")

        # ── PAPER TRADE PATH ─────────────────────────────────────────
        if self.paper_trade:
            logger.info(
                "Paper trade — simulating EOD close for all open paper positions"
            )

            with self._lock:
                open_copy = dict(self.open_orders)

            if not open_copy:
                logger.info("No open paper positions to close.")
                return

            closed_count = 0
            for symbol, order_info in open_copy.items():
                direction  = order_info.get('direction', 'LONG')
                fill_price = order_info.get(
                    'fill_price', order_info.get('entry_price', 0)
                )
                quantity   = order_info.get('quantity', 1)

                # Try to fetch real current price for a realistic exit
                exit_price = fill_price  # fallback
                try:
                    full_sym   = (f"NSE:{symbol}"
                                  if not symbol.startswith("NSE:") else symbol)
                    ltp_data   = self.kite.ltp([full_sym])
                    exit_price = float(ltp_data[full_sym]['last_price'])
                    logger.debug(
                        f"Paper exit LTP for {symbol}: ₹{exit_price:.2f}"
                    )
                except Exception as e:
                    logger.debug(
                        f"LTP fetch failed for {symbol} ({e}) — "
                        f"using midpoint of target/SL as exit"
                    )
                    tgt = order_info.get('target', fill_price)
                    sl  = order_info.get('sl',     fill_price)
                    exit_price = round((tgt + sl) / 2, 2)

                # Calculate P&L
                if direction == 'LONG':
                    pnl = (exit_price - fill_price) * quantity
                else:
                    pnl = (fill_price - exit_price) * quantity

                logger.info(
                    f"PAPER EOD CLOSE | {symbol} {direction} | "
                    f"Fill:₹{fill_price:.2f} → Exit:₹{exit_price:.2f} | "
                    f"PnL: ₹{pnl:+,.0f}"
                )

                self.mark_closed(symbol, exit_price, reason)
                closed_count += 1

            with self._lock:
                self.open_orders.clear()

            logger.info(
                f"Paper force_close_all complete: {closed_count} positions closed"
            )
            return

        # ── REAL TRADE PATH ──────────────────────────────────────────
        try:
            positions = self.kite.positions().get('net', [])
        except Exception as e:
            logger.error(f"Could not fetch positions for force close: {e}")
            return

        closed_count = 0
        for p in positions:
            symbol = p.get('tradingsymbol', '')
            # net_quantity: + = long open, - = short open, 0 = flat
            qty    = p.get('net_quantity', 0)

            if qty == 0:
                continue

            # Cancel SL / target orders first to avoid double-fill
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

        logger.info(
            f"Real force_close_all complete: {closed_count} positions closed"
        )

    # ------------------------------------------------------------------
    # PAPER TRADE SIMULATION
    # BUG 8 FIX: Fill noise reduced from ±2% to ±0.05%
    # ------------------------------------------------------------------
    def _paper_place_order(self, symbol: str, direction: str,
                            entry: float, sl: float, target: float,
                            quantity: int) -> dict:
        """
        Simulate a limit order fill for paper trading.

        BUG 8 FIX: Old noise was ±2% of entry price.
          On a ₹1000 stock that's ±₹20 — larger than most SL distances.
          A real limit order fills at or very close to the limit price.
          Changed to ±0.05% (realistic intraday slippage).
        """
        import random
        # ±0.05% noise — realistic for a limit order vs the old ±2%
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
        """Record a position as closed. Called after target/SL hit or EOD close."""
        with self._lock:
            if symbol in self.open_orders:
                info = self.open_orders[symbol]
                info['status']      = 'CLOSED'
                info['exit_price']  = exit_price
                info['exit_reason'] = reason
                info['exit_time']   = datetime.now()
                del self.open_orders[symbol]
