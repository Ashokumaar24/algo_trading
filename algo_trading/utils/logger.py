# ============================================================
#  utils/logger.py
#  Centralised coloured logging for the trading system
# ============================================================

import logging
import os
import sys
from datetime import datetime

# Try to use colorlog if available
try:
    import colorlog
    HAS_COLOR = True
except ImportError:
    HAS_COLOR = False

_loggers = {}


def get_logger(name: str, level: str = "INFO") -> logging.Logger:
    """
    Get (or create) a named logger.
    All loggers write to console AND to logs/YYYY-MM-DD.log
    """
    if name in _loggers:
        return _loggers[name]

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    if logger.handlers:
        _loggers[name] = logger
        return logger

    fmt = "%(asctime)s | %(name)-16s | %(levelname)-8s | %(message)s"
    date_fmt = "%H:%M:%S"

    # --- Console Handler ---
    if HAS_COLOR:
        color_fmt = (
            "%(log_color)s%(asctime)s%(reset)s | "
            "%(cyan)s%(name)-16s%(reset)s | "
            "%(log_color)s%(levelname)-8s%(reset)s | "
            "%(message)s"
        )
        console_handler = colorlog.StreamHandler(sys.stdout)
        console_handler.setFormatter(colorlog.ColoredFormatter(
            color_fmt,
            datefmt=date_fmt,
            log_colors={
                'DEBUG':    'white',
                'INFO':     'green',
                'WARNING':  'yellow',
                'ERROR':    'red',
                'CRITICAL': 'bold_red',
            }
        ))
    else:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(logging.Formatter(fmt, datefmt=date_fmt))

    logger.addHandler(console_handler)

    # --- File Handler ---
    try:
        log_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "logs"
        )
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f"{datetime.now().strftime('%Y-%m-%d')}.log")

        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setFormatter(logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S"))
        logger.addHandler(file_handler)
    except Exception:
        pass  # File logging is non-critical

    _loggers[name] = logger
    return logger


class TradeLogger:
    """
    Dedicated trade logger — writes structured trade records to
    logs/trades_YYYY-MM-DD.csv for performance analysis.
    """

    def __init__(self):
        self.log_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "logs"
        )
        os.makedirs(self.log_dir, exist_ok=True)
        self.log_file = os.path.join(
            self.log_dir,
            f"trades_{datetime.now().strftime('%Y-%m-%d')}.csv"
        )
        self._init_file()
        self.logger = get_logger("trade_log")

    def _init_file(self):
        if not os.path.exists(self.log_file):
            with open(self.log_file, 'w') as f:
                f.write(
                    "timestamp,symbol,strategy,direction,entry,sl,target,"
                    "exit_price,quantity,pnl,pnl_pct,outcome,regime,"
                    "confidence,hold_minutes,notes\n"
                )

    def log_trade(self, **kwargs):
        """Log a completed trade to CSV"""
        row = ",".join([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            str(kwargs.get('symbol', '')),
            str(kwargs.get('strategy', '')),
            str(kwargs.get('direction', '')),
            str(kwargs.get('entry', '')),
            str(kwargs.get('sl', '')),
            str(kwargs.get('target', '')),
            str(kwargs.get('exit_price', '')),
            str(kwargs.get('quantity', '')),
            str(kwargs.get('pnl', '')),
            str(kwargs.get('pnl_pct', '')),
            str(kwargs.get('outcome', '')),
            str(kwargs.get('regime', '')),
            str(kwargs.get('confidence', '')),
            str(kwargs.get('hold_minutes', '')),
            str(kwargs.get('notes', '')),
        ])
        with open(self.log_file, 'a') as f:
            f.write(row + "\n")

        self.logger.info(
            f"TRADE LOG | {kwargs.get('symbol')} {kwargs.get('direction')} | "
            f"Entry:{kwargs.get('entry')} Exit:{kwargs.get('exit_price')} | "
            f"PnL: ₹{kwargs.get('pnl', 0):,.0f} | {kwargs.get('outcome', '').upper()}"
        )
