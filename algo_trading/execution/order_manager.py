# ============================================================
#  execution/order_manager.py
#  Order placement, modification, and monitoring via KiteConnect
#
#  ADDED: journal.log_trade_exit() called on every closed position
#         so the daily journal captures outcome + reason for exit
# ============================================================

import time
from datetime import datetime
from typing import Optional, Dict

from kiteconnect import KiteConnect

from strategies.base_strategy import Signal, Direction, SignalStatus
from risk.risk_manager import RiskManager
from regime.market_regime import MarketRegime
from utils.daily_journal import get_journal      # ← ADDED
from utils.logger import get_logger, TradeLogger

logger    = get_logger("order_manager")
trade_log = TradeLogger()


class ActivePosition:
    def __init__(self, signal: Signal, entry_order_id: str,
                 sl_order_id: str, target_order_id: str):
        self.signal           = signal
        self.entry_order_id   = entry_order_id
        self.sl_order_id      = sl_order_id
        self.target_order_id  = target_order_id
        self.current_sl       = signal.stop_loss
        self.entry_time       = datetime.now()


class OrderManager:

    def __init__(self, kite: KiteConnect, risk_manager: RiskManager):
        self.kite         = kite
        self.risk_manager = risk_manager
        self.positions: Dict[str, ActivePosition] = {}

    def place_order(self, signal: Signal, regime: Optional[MarketRegime] = None,
                    dry_run: bool = False) -> Optional[str]:
        if not signal.is_valid():
            logger.error(f"Invalid signal rejected: {signal}")
            return None

        if not self.risk_manager.can_trade():
            logger.warning(f"Risk gate blocked trade for {signal.symbol}")
            return None

        if not self.risk_manager.passes_confidence_gate(signal):
            # ADDED: log confidence block to journal
            get_journal().log_trade_blocked(
                symbol=signal.symbol,
                strategy=signal.strategy,
                block_type='CONFIDENCE',
                reason=f"Confidence {signal.confidence:.0f} < 65",
                detail=(
                    f"Signal generated but confidence score {signal.confidence:.0f}/100 "
                    f"is below the minimum of 65. Low-confidence signals have poor "
                    f"historical win rates and are skipped."
                ),
                candle_price=signal.entry,
                confidence=signal.confidence
            )
            return None

        if signal.position_size <= 0:
            size_mult = regime.size_multiplier if regime else 1.0
            signal.position_size = self.risk_manager.calculate_position_size(
                signal, size_mult
            )

        if signal.position_size <= 0:
            logger.error(f"Zero position size for {signal.symbol} — skipping")
            return None

        qty        = signal.position_size
        tradingsym = signal.symbol.replace("NSE:", "")
        side       = 'BUY'  if signal.direction == Direction.LONG  else 'SELL'
        sl_side    = 'SELL' if signal.direction == Direction.LONG  else 'BUY'

        logger.info(
            f"Placing {'DRY RUN ' if dry_run else ''}Orders | "
            f"{signal.symbol} {side} {qty} @ {signal.entry:.2f} | "
            f"SL:{signal.stop_loss:.2f} | Target:{signal.target:.2f} | "
            f"Strategy:{signal.strategy} | Confidence:{signal.confidence:.0f}"
        )

        if dry_run:
            fake_id = f"DRY_{tradingsym}_{int(time.time())}"
            signal.order_id = fake_id
            signal.status   = SignalStatus.ACTIVE
            pos = ActivePosition(signal, fake_id, f"{fake_id}_SL", f"{fake_id}_TGT")
            self.positions[fake_id] = pos
            self.risk_manager.record_trade_entry()
            logger.info(f"[DRY RUN] Orders simulated: entry={fake_id}")
            return fake_id

        entry_order_id  = None
        sl_order_id     = None
        target_order_id = None

        try:
            entry_order_id = str(self.kite.place_order(
                tradingsymbol=tradingsym, exchange="NSE",
                transaction_type=side, quantity=qty,
                order_type="LIMIT", price=signal.entry,
                product="MIS", variety="regular",
                tag=f"ENTRY_{signal.strategy[:6]}"
            ))
            time.sleep(0.3)

            sl_order_id = str(self.kite.place_order(
                tradingsymbol=tradingsym, exchange="NSE",
                transaction_type=sl_side, quantity=qty,
                order_type="SL-M", trigger_price=round(signal.stop_loss, 2),
                product="MIS", variety="regular",
                tag=f"SL_{signal.strategy[:6]}"
            ))
            time.sleep(0.3)

            target_order_id = str(self.kite.place_order(
                tradingsymbol=tradingsym, exchange="NSE",
                transaction_type=sl_side, quantity=qty,
                order_type="LIMIT", price=signal.target,
                product="MIS", variety="regular",
                tag=f"TGT_{signal.strategy[:6]}"
            ))

        except Exception as e:
            logger.error(f"Order placement failed for {signal.symbol}: {e}")
            self._cancel_partial_orders(entry_order_id, sl_order_id, target_order_id)
            return None

        signal.order_id = entry_order_id
        signal.status   = SignalStatus.ACTIVE

        pos = ActivePosition(signal, entry_order_id, sl_order_id, target_order_id)
        self.positions[entry_order_id] = pos
        self.risk_manager.record_trade_entry()
        return entry_order_id

    def _cancel_partial_orders(self, *order_ids):
        for oid in order_ids:
            if oid is None:
                continue
            try:
                self.kite.cancel_order(variety="regular", order_id=oid)
            except Exception as e:
                logger.warning(f"Could not cancel order {oid}: {e}")

    def monitor_positions(self, symbol_prices: Dict[str, float]):
        for entry_oid, pos in list(self.positions.items()):
            signal = pos.signal
            if signal.status != SignalStatus.ACTIVE:
                continue
            symbol = signal.symbol
            price  = symbol_prices.get(symbol)
            if price is None:
                continue

            self._sync_order_status(entry_oid, pos)
            if signal.status != SignalStatus.ACTIVE:
                continue

            new_sl = self.risk_manager.should_trail_stop(signal, price, pos.current_sl)
            if new_sl:
                self._modify_sl(entry_oid, pos, new_sl)

            is_loss = (
                (signal.direction == Direction.LONG  and price < signal.entry) or
                (signal.direction == Direction.SHORT and price > signal.entry)
            )
            time_action = self.risk_manager.check_time_exit(True, is_loss)
            if time_action in ('FORCE_CLOSE', 'CLOSE_LOSERS'):
                self.close_position(entry_oid, price, reason=time_action)

    def _sync_order_status(self, entry_oid: str, pos: ActivePosition):
        try:
            orders       = self.kite.orders()
            orders_by_id = {str(o['order_id']): o for o in orders}

            sl_order  = orders_by_id.get(pos.sl_order_id)
            tgt_order = orders_by_id.get(pos.target_order_id)

            if sl_order and sl_order['status'] == 'COMPLETE':
                exit_price = sl_order.get('average_price', pos.current_sl)
                self._record_closed_position(entry_oid, pos, exit_price, 'SL_HIT')

            elif tgt_order and tgt_order['status'] == 'COMPLETE':
                exit_price = tgt_order.get('average_price', pos.signal.target)
                self._record_closed_position(entry_oid, pos, exit_price, 'TARGET_HIT')

        except Exception as e:
            logger.debug(f"Order status sync error for {entry_oid}: {e}")

    def _modify_sl(self, entry_oid: str, pos: ActivePosition, new_sl: float):
        signal     = pos.signal
        tradingsym = signal.symbol.replace("NSE:", "")
        sl_side    = 'SELL' if signal.direction == Direction.LONG else 'BUY'
        qty        = signal.position_size

        try:
            self.kite.cancel_order(variety="regular", order_id=pos.sl_order_id)
            time.sleep(0.2)
            new_sl_order_id = str(self.kite.place_order(
                tradingsymbol=tradingsym, exchange="NSE",
                transaction_type=sl_side, quantity=qty,
                order_type="SL-M", trigger_price=round(new_sl, 2),
                product="MIS", variety="regular", tag="TRAIL_SL"
            ))
            old_sl          = pos.current_sl
            pos.sl_order_id = new_sl_order_id
            pos.current_sl  = new_sl
            logger.info(f"SL Trailed | {signal.symbol} | {old_sl:.2f} → {new_sl:.2f}")
        except Exception as e:
            logger.error(f"SL modification failed for {entry_oid}: {e}")

    def close_position(self, entry_oid: str, exit_price: float, reason: str = "manual"):
        pos = self.positions.get(entry_oid)
        if not pos:
            return

        signal     = pos.signal
        tradingsym = signal.symbol.replace("NSE:", "")
        exit_side  = 'SELL' if signal.direction == Direction.LONG else 'BUY'
        qty        = signal.position_size

        for oid in [pos.sl_order_id, pos.target_order_id]:
            if oid:
                try:
                    self.kite.cancel_order(variety="regular", order_id=oid)
                except Exception:
                    pass

        try:
            self.kite.place_order(
                tradingsymbol=tradingsym, exchange="NSE",
                transaction_type=exit_side, quantity=qty,
                order_type="MARKET", product="MIS",
                variety="regular", tag="MANUAL_EXIT"
            )
        except Exception as e:
            logger.error(f"Market exit failed for {signal.symbol}: {e}")

        self._record_closed_position(entry_oid, pos, exit_price, reason)

    def _record_closed_position(self, entry_oid: str, pos: ActivePosition,
                                 exit_price: float, reason: str):
        signal = pos.signal
        if signal.direction == Direction.LONG:
            pnl = (exit_price - signal.entry) * signal.position_size
        else:
            pnl = (signal.entry - exit_price) * signal.position_size

        outcome   = "WIN" if pnl > 0 else "LOSS"
        signal.status = SignalStatus.HIT_TARGET if reason == 'TARGET_HIT' else SignalStatus.HIT_SL

        self.risk_manager.record_trade_exit(pnl)
        hold_mins = int((datetime.now() - pos.entry_time).total_seconds() / 60)

        trade_log.log_trade(
            symbol=signal.symbol, strategy=signal.strategy,
            direction=signal.direction.value, entry=signal.entry,
            sl=signal.stop_loss, target=signal.target,
            exit_price=round(exit_price, 2), quantity=signal.position_size,
            pnl=round(pnl, 2),
            pnl_pct=round(pnl / (signal.entry * signal.position_size) * 100, 3),
            outcome=outcome, regime=signal.regime, confidence=signal.confidence,
            hold_minutes=hold_mins, notes=f"{reason} | {signal.notes}"
        )

        # ADDED: log exit to daily journal
        get_journal().log_trade_exit(
            symbol=signal.symbol,
            strategy=signal.strategy,
            direction=signal.direction.value,
            entry=signal.entry,
            exit_price=round(exit_price, 2),
            exit_reason=reason,
            pnl=round(pnl, 2),
            hold_mins=hold_mins
        )

        self.positions.pop(entry_oid, None)
        logger.info(
            f"Position Closed | {signal.symbol} | PnL: ₹{pnl:+,.0f} | "
            f"{outcome} | Reason:{reason}"
        )

    def force_close_all(self):
        logger.warning("FORCE CLOSING ALL POSITIONS (3:15 PM)")
        for pos in list(self.positions.values()):
            for oid in [pos.sl_order_id, pos.target_order_id]:
                if oid:
                    try:
                        self.kite.cancel_order(variety="regular", order_id=oid)
                    except Exception:
                        pass
        try:
            positions = self.kite.positions().get('day', [])
            for p in positions:
                qty = p.get('quantity', 0)
                if qty == 0:
                    continue
                side = 'SELL' if qty > 0 else 'BUY'
                try:
                    self.kite.place_order(
                        tradingsymbol=p['tradingsymbol'], exchange=p['exchange'],
                        transaction_type=side, quantity=abs(qty),
                        order_type='MARKET', product='MIS',
                        variety='regular', tag='EOD_CLOSE'
                    )
                except Exception as e:
                    logger.error(f"Force close failed for {p['tradingsymbol']}: {e}")
        except Exception as e:
            logger.error(f"force_close_all failed: {e}")
        self.positions.clear()

    def get_active_count(self) -> int:
        return sum(
            1 for pos in self.positions.values()
            if pos.signal.status == SignalStatus.ACTIVE
        )
