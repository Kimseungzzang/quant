from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class EventKind(str, Enum):
    WATCH_TRIGGERED = "watch_triggered"
    MORNING_BRIEF = "morning_brief"
    CHAT = "chat"


class WatchConditionType(str, Enum):
    PRICE_CHANGE = "price_change"    # 설정 시점 대비 ±X% 변동
    PRICE_ABOVE = "price_above"      # 가격이 X 이상
    PRICE_BELOW = "price_below"      # 가격이 X 이하
    VOLUME_SPIKE = "volume_spike"    # 거래량이 평균의 X배
    EXPR = "expr"                    # 자유 수식 (price, volume, rsi, macd, ma5/10/20/60, change_pct, volume_ratio)


class Market(str, Enum):
    DOMESTIC = "domestic"
    OVERSEAS = "overseas"


@dataclass
class WatchCondition:
    type: WatchConditionType
    threshold: float = 0.0
    formula: str = ""   # expr 타입일 때 사용
    note: str = ""


@dataclass
class WatchEntry:
    stock_code: str
    stock_name: str
    market: Market
    conditions: list[WatchCondition]
    baseline_price: float = 0.0
    baseline_volume: float = 0.0
    set_at: str = field(default_factory=lambda: datetime.now().isoformat())
    triggered_types: list[str] = field(default_factory=list)


@dataclass
class MarketEvent:
    kind: EventKind
    market: Market
    stock_code: str = ""
    stock_name: str = ""
    payload: dict = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=datetime.now)

    def __str__(self) -> str:
        return f"[{self.kind}] {self.stock_code or self.market} @ {self.occurred_at.strftime('%H:%M:%S')}"
