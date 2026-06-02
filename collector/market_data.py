import json
import logging
import redis

logger = logging.getLogger(__name__)

_PRICE_TTL = 300
_ORDERBOOK_TTL = 60


class MarketDataCollector:
    def __init__(self, redis_client: redis.Redis):
        self._r = redis_client

    def on_price_tick(self, stock_code: str, data: dict) -> None:
        key = f"price:{stock_code}"
        self._r.setex(key, _PRICE_TTL, json.dumps(data))

    def on_orderbook_tick(self, stock_code: str, data: dict) -> None:
        key = f"orderbook:{stock_code}"
        self._r.setex(key, _ORDERBOOK_TTL, json.dumps(data))

    def get_price(self, stock_code: str) -> dict | None:
        raw = self._r.get(f"price:{stock_code}")
        return json.loads(raw) if raw else None

    def get_orderbook(self, stock_code: str) -> dict | None:
        raw = self._r.get(f"orderbook:{stock_code}")
        return json.loads(raw) if raw else None

    def get_all_prices(self) -> dict[str, dict]:
        keys = self._r.keys("price:*")
        result: dict[str, dict] = {}
        for key in keys:
            raw = self._r.get(key)
            if raw:
                code = key.decode().split(":", 1)[1]
                result[code] = json.loads(raw)
        return result
