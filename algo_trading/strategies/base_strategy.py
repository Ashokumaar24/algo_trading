# ============================================================
#  strategies/base_strategy.py
#  Signal dataclass and abstract base for all strategies
# ============================================================

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime
from enum import Enum


class Direction(str, Enum):
    LONG  = "LONG"
    SHORT = "SHORT"


class SignalStatus(str, Enum):
    PENDING  = "PENDING"
    ACTIVE   = "ACTIVE"
    HIT_TARGET = "HIT_TARGET"
    HIT_SL     = "HIT_SL"
    EXPIRED    = "EXPIRED"
    CANCELLED  = "CANCELLED"


@dataclass
class Signal:
    """
    Represents a trade signal from any strategy.
    Contains all information needed by the order manager.
    """
    # Core fields
    symbol:        str
    direction:     Direction
    strategy:      str
    entry:         float
    stop_loss:     float
    target:        float

    # Optional / computed fields
    confidence:    float    = 0.0
    position_size: int      = 0
    risk_amount:   float    = 0.0
    reward_risk:   float    = 0.0
    regime:        str      = "UNKNOWN"
    sentiment:     float    = 0.0

    # Trailing / partial exits
    target_2:      Optional[float] = None    # 2R target
    trail_trigger: Optional[float] = None    # profit to trigger trailing

    # Metadata
    timestamp:     datetime = field(default_factory=datetime.now)
    status:        SignalStatus = SignalStatus.PENDING
    order_id:      Optional[str] = None
    notes:         str = ""

    def __post_init__(self):
        if self.entry > 0 and self.stop_loss > 0 and self.target > 0:
            risk   = abs(self.entry - self.stop_loss)
            reward = abs(self.target - self.entry)
            self.reward_risk = round(reward / risk, 2) if risk > 0 else 0.0

    def is_valid(self) -> bool:
        """Basic signal sanity checks"""
        if self.entry <= 0 or self.stop_loss <= 0 or self.target <= 0:
            return False
        if self.direction == Direction.LONG:
            return self.stop_loss < self.entry < self.target
        else:
            return self.target < self.entry < self.stop_loss

    def risk_per_share(self) -> float:
        return abs(self.entry - self.stop_loss)

    def reward_per_share(self) -> float:
        return abs(self.target - self.entry)

    def __repr__(self):
        return (
            f"Signal({self.symbol} {self.direction.value} | "
            f"E:{self.entry:.2f} SL:{self.stop_loss:.2f} T:{self.target:.2f} | "
            f"RR:{self.reward_risk:.2f} Conf:{self.confidence:.0f} | "
            f"{self.strategy})"
        )


class BaseStrategy:
    """
    Abstract base class for all trading strategies.
    All strategies must implement check_entry().
    """

    def __init__(self, name: str):
        self.name = name

    def check_entry(self, *args, **kwargs) -> Optional[Signal]:
        """
        Returns a Signal if entry conditions are met, else None.
        Must be implemented by subclasses.
        """
        raise NotImplementedError

    def _validate_candle_count(self, df, min_candles: int) -> bool:
        """Ensure we have enough history before calculating indicators"""
        return df is not None and len(df) >= min_candles

    def __repr__(self):
        return f"Strategy({self.name})"
