import json
import logging
import redis

logger = logging.getLogger(__name__)

_TTL = 600


class RankingCollector:
    def __init__(self, redis_client: redis.Redis):
        self._r = redis_client

    def update_volume_rank(self, market: str, items: list[dict]) -> None:
        self._r.setex(f"rank:volume:{market}", _TTL, json.dumps(items))

    def update_trading_value_rank(self, market: str, items: list[dict]) -> None:
        self._r.setex(f"rank:value:{market}", _TTL, json.dumps(items))

    def get_volume_rank(self, market: str) -> list[dict]:
        raw = self._r.get(f"rank:volume:{market}")
        return json.loads(raw) if raw else []

    def get_trading_value_rank(self, market: str) -> list[dict]:
        raw = self._r.get(f"rank:value:{market}")
        return json.loads(raw) if raw else []
