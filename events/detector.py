import json
import logging

import redis

from collector.market_data import MarketDataCollector
from events.types import EventKind, Market, MarketEvent, WatchConditionType

logger = logging.getLogger(__name__)

_WATCHES_KEY = "ai:watches"


class EventDetector:
    """AI가 설정한 watch 조건만 체크. 하드코딩 임계값 없음."""

    def __init__(self, market_data: MarketDataCollector, redis_client: redis.Redis):
        self._market = market_data
        self._r = redis_client

    def detect(self) -> list[MarketEvent]:
        watches = self._load_watches()
        if not watches:
            return []

        events: list[MarketEvent] = []
        for stock_code, watch in watches.items():
            price_data = self._market.get_price(stock_code)
            if not price_data:
                continue

            current_price = float(price_data.get("current_price", 0))
            current_volume = float(price_data.get("acml_volume", 0))  # 오늘 누적 거래량
            baseline_price = float(watch.get("baseline_price", 0))
            baseline_volume = float(watch.get("baseline_volume", 0))
            triggered_types = watch.get("triggered_types", [])
            fired: list[str] = []

            for cond in watch.get("conditions", []):
                ctype = cond.get("type", "")
                threshold = float(cond.get("threshold", 0))
                if ctype in triggered_types:
                    continue

                hit = False
                detail = {}

                if ctype == WatchConditionType.PRICE_CHANGE and baseline_price > 0:
                    change_pct = (current_price - baseline_price) / baseline_price * 100
                    if abs(change_pct) >= threshold:
                        hit = True
                        detail = {"change_pct": round(change_pct, 2), "baseline_price": baseline_price, "current_price": current_price}

                elif ctype == WatchConditionType.PRICE_ABOVE:
                    if current_price >= threshold:
                        hit = True
                        detail = {"current_price": current_price, "threshold": threshold}

                elif ctype == WatchConditionType.PRICE_BELOW:
                    if current_price <= threshold:
                        hit = True
                        detail = {"current_price": current_price, "threshold": threshold}

                elif ctype == WatchConditionType.VOLUME_SPIKE and baseline_volume > 0:
                    multiple = current_volume / baseline_volume
                    if multiple >= threshold:
                        hit = True
                        detail = {"multiple": round(multiple, 1), "baseline_volume": baseline_volume, "current_volume": current_volume}

                if hit:
                    fired.append(ctype)
                    market = Market(watch.get("market", "domestic"))
                    events.append(MarketEvent(
                        kind=EventKind.WATCH_TRIGGERED,
                        market=market,
                        stock_code=stock_code,
                        stock_name=watch.get("stock_name", stock_code),
                        payload={
                            "condition_type": ctype,
                            "condition_note": cond.get("note", ""),
                            "threshold": threshold,
                            **detail,
                        },
                    ))
                    logger.info("watch 조건 충족: %s %s (threshold=%.2f)", stock_code, ctype, threshold)

            if fired:
                triggered_types.extend(fired)
                watch["triggered_types"] = triggered_types
                self._update_watch(stock_code, watch)

        return events

    def _load_watches(self) -> dict:
        raw = self._r.get(_WATCHES_KEY)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _update_watch(self, stock_code: str, watch: dict) -> None:
        watches = self._load_watches()
        watches[stock_code] = watch
        self._r.set(_WATCHES_KEY, json.dumps(watches, ensure_ascii=False))
