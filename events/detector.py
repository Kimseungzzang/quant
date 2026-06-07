import json
import logging
from typing import TYPE_CHECKING

import redis
from simpleeval import EvalWithCompoundTypes, FeatureNotAvailable, InvalidExpression

from collector.market_data import MarketDataCollector
from events.types import EventKind, Market, MarketEvent, WatchConditionType

if TYPE_CHECKING:
    from events.indicator_cache import IndicatorCache

logger = logging.getLogger(__name__)

_WATCHES_KEY = "ai:watches"


def _compute_indicators(
    closes: list[float],
    volumes: list[float],
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> dict:
    """캔들 데이터로 지표 계산. 데이터 부족 시 None."""
    result: dict = {}
    n = len(closes)
    if n == 0:
        return result

    def ema(prices: list[float], period: int) -> float | None:
        if len(prices) < period:
            return None
        k = 2 / (period + 1)
        val = sum(prices[:period]) / period
        for p in prices[period:]:
            val = p * k + val * (1 - k)
        return val

    for period, key in [(5, "ma5"), (10, "ma10"), (20, "ma20"), (60, "ma60")]:
        if n >= period:
            result[key] = sum(closes[-period:]) / period

    if n >= 14:
        gains, losses = [], []
        for i in range(1, n):
            diff = closes[i] - closes[i - 1]
            gains.append(max(diff, 0))
            losses.append(max(-diff, 0))
        avg_gain = sum(gains[-14:]) / 14
        avg_loss = sum(losses[-14:]) / 14
        if avg_loss == 0:
            result["rsi"] = 100.0
        else:
            rs = avg_gain / avg_loss
            result["rsi"] = round(100 - 100 / (1 + rs), 2)

    fast = ema(closes, 12)
    slow = ema(closes, 26)
    if fast is not None and slow is not None:
        result["macd"] = round(fast - slow, 4)

    if volumes and n >= 20:
        result["avg_volume"] = sum(volumes[-20:]) / 20

    # 볼린저 밴드 %B (20일, 2σ)
    if n >= 20:
        import math
        ma20 = sum(closes[-20:]) / 20
        std20 = math.sqrt(sum((c - ma20) ** 2 for c in closes[-20:]) / 20)
        if std20 > 0:
            bb_upper = ma20 + 2 * std20
            bb_lower = ma20 - 2 * std20
            result["bb_upper"] = round(bb_upper, 4)
            result["bb_lower"] = round(bb_lower, 4)
            result["bb_pct"] = round((closes[-1] - bb_lower) / (bb_upper - bb_lower), 4)

    # 스토캐스틱 %K, %D (14일)
    if highs and lows and len(highs) >= 14 and len(lows) >= 14:
        period = 14
        high14 = max(highs[-period:])
        low14 = min(lows[-period:])
        if high14 != low14:
            stoch_k = (closes[-1] - low14) / (high14 - low14) * 100
            result["stoch_k"] = round(stoch_k, 2)
            # %D = 3일 %K SMA (슬라이딩)
            k_values = []
            for i in range(3):
                idx = n - 1 - i
                if idx < period - 1:
                    break
                h = max(highs[idx - period + 1: idx + 1])
                l = min(lows[idx - period + 1: idx + 1])
                if h != l:
                    k_values.append((closes[idx] - l) / (h - l) * 100)
            if len(k_values) == 3:
                result["stoch_d"] = round(sum(k_values) / 3, 2)

    return result


class EventDetector:
    """AI가 설정한 watch 조건만 체크. 하드코딩 임계값 없음."""

    def __init__(
        self,
        market_data: MarketDataCollector,
        redis_client: redis.Redis,
        indicator_cache: "IndicatorCache | None" = None,
    ):
        self._market = market_data
        self._r = redis_client
        self._indicator_cache = indicator_cache

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
            current_volume = float(price_data.get("acml_volume", 0))
            baseline_price = float(watch.get("baseline_price", 0))
            baseline_volume = float(watch.get("baseline_volume", 0))
            triggered_types = watch.get("triggered_types", [])

            has_expr = any(c.get("type") == WatchConditionType.EXPR for c in watch.get("conditions", []))
            indicators: dict = {}
            if has_expr and self._indicator_cache:
                indicators = self._indicator_cache.get(stock_code)

            fired: list[str] = []

            for cond in watch.get("conditions", []):
                ctype = cond.get("type", "")
                threshold = float(cond.get("threshold", 0))
                cond_key = f"{ctype}:{cond.get('formula', '')}:{threshold}"
                if cond_key in triggered_types:
                    continue

                hit = False
                detail: dict = {}

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

                elif ctype == WatchConditionType.EXPR:
                    formula = cond.get("formula", "")
                    hit, detail = self._eval_expr(
                        formula=formula,
                        price=current_price,
                        volume=current_volume,
                        baseline_price=baseline_price,
                        baseline_volume=baseline_volume,
                        indicators=indicators,
                    )

                if hit:
                    fired.append(cond_key)
                    market = Market(watch.get("market", "domestic"))
                    events.append(MarketEvent(
                        kind=EventKind.WATCH_TRIGGERED,
                        market=market,
                        stock_code=stock_code,
                        stock_name=watch.get("stock_name", stock_code),
                        payload={
                            "condition_type": ctype,
                            "condition_note": cond.get("note", ""),
                            "formula": cond.get("formula", ""),
                            "threshold": threshold,
                            **detail,
                        },
                    ))
                    logger.info("watch 조건 충족: %s %s", stock_code, cond.get("formula") or ctype)

            if fired:
                triggered_types.extend(fired)
                watch["triggered_types"] = triggered_types
                self._update_watch(stock_code, watch)

        return events

    def _eval_expr(
        self,
        formula: str,
        price: float,
        volume: float,
        baseline_price: float,
        baseline_volume: float,
        indicators: dict,
    ) -> tuple[bool, dict]:
        if not formula:
            return False, {}

        change_pct = ((price - baseline_price) / baseline_price * 100) if baseline_price > 0 else 0.0
        volume_ratio = (volume / baseline_volume) if baseline_volume > 0 else 0.0

        names = {
            "price": price,
            "volume": volume,
            "baseline_price": baseline_price,
            "baseline_volume": baseline_volume,
            "change_pct": round(change_pct, 4),
            "volume_ratio": round(volume_ratio, 4),
            **{k: v for k, v in indicators.items() if v is not None},
        }

        try:
            evaluator = EvalWithCompoundTypes(names=names)
            result = evaluator.eval(formula)
            hit = bool(result)
            detail = {"formula": formula, "variables": {k: round(v, 4) if isinstance(v, float) else v for k, v in names.items()}}
            return hit, detail
        except (FeatureNotAvailable, InvalidExpression, Exception) as e:
            logger.warning("expr 평가 실패 '%s': %s", formula, e)
            return False, {}

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
