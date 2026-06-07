import json
import logging
from typing import TYPE_CHECKING

import pandas as pd
import pandas_ta as ta
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
    """pandas_ta로 기술 지표 계산."""
    if not closes:
        return {}

    n = len(closes)
    close_s = pd.Series(closes, dtype=float)
    result: dict = {}

    for period, key in [(5, "ma5"), (10, "ma10"), (20, "ma20"), (60, "ma60")]:
        if n >= period:
            val = close_s.rolling(period).mean().iloc[-1]
            if pd.notna(val):
                result[key] = round(float(val), 4)

    if n >= 14:
        rsi = ta.rsi(close_s, length=14)
        if rsi is not None and not rsi.empty:
            val = rsi.dropna().iloc[-1] if not rsi.dropna().empty else None
            if val is not None and pd.notna(val):
                result["rsi"] = round(float(val), 2)

    if n >= 26:
        macd_df = ta.macd(close_s, fast=12, slow=26, signal=9)
        if macd_df is not None and not macd_df.empty:
            col = next((c for c in macd_df.columns if c.startswith("MACD_")), None)
            if col:
                val = macd_df[col].dropna()
                if not val.empty and pd.notna(val.iloc[-1]):
                    result["macd"] = round(float(val.iloc[-1]), 4)

    if n >= 20:
        bb = ta.bbands(close_s, length=20, std=2.0)
        if bb is not None and not bb.empty:
            lower_col = next((c for c in bb.columns if c.startswith("BBL_")), None)
            upper_col = next((c for c in bb.columns if c.startswith("BBU_")), None)
            pct_col = next((c for c in bb.columns if c.startswith("BBP_")), None)
            if lower_col and upper_col:
                lower = float(bb[lower_col].iloc[-1])
                upper = float(bb[upper_col].iloc[-1])
                if pd.notna(lower) and pd.notna(upper):
                    result["bb_lower"] = round(lower, 4)
                    result["bb_upper"] = round(upper, 4)
            if pct_col:
                val = float(bb[pct_col].iloc[-1])
                if pd.notna(val):
                    result["bb_pct"] = round(val, 4)

    if volumes and n >= 20:
        result["avg_volume"] = sum(volumes[-20:]) / 20

    if highs and lows and len(highs) >= 14 and len(lows) >= 14:
        stoch = ta.stoch(
            pd.Series(highs, dtype=float),
            pd.Series(lows, dtype=float),
            close_s,
            k=14, d=3, smooth_k=3,
        )
        if stoch is not None and not stoch.empty:
            k_col = next((c for c in stoch.columns if c.startswith("STOCHk")), None)
            d_col = next((c for c in stoch.columns if c.startswith("STOCHd")), None)
            if k_col:
                val = stoch[k_col].dropna()
                if not val.empty and pd.notna(val.iloc[-1]):
                    result["stoch_k"] = round(float(val.iloc[-1]), 2)
            if d_col:
                val = stoch[d_col].dropna()
                if not val.empty and pd.notna(val.iloc[-1]):
                    result["stoch_d"] = round(float(val.iloc[-1]), 2)

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
