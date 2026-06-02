import json
import logging
import redis

logger = logging.getLogger(__name__)

_TTL = 60


class AccountCollector:
    def __init__(self, redis_client: redis.Redis):
        self._r = redis_client

    def update_balance(self, market: str, data: dict) -> None:
        self._r.setex(f"account:balance:{market}", _TTL, json.dumps(data))

    def update_positions(self, positions: list[dict]) -> None:
        self._r.setex("account:positions", _TTL, json.dumps(positions))

    def get_balance(self, market: str) -> dict | None:
        raw = self._r.get(f"account:balance:{market}")
        return json.loads(raw) if raw else None

    def get_positions(self) -> list[dict]:
        raw = self._r.get("account:positions")
        return json.loads(raw) if raw else []
