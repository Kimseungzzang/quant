"""
기술 지표 캐시.
- 최초 로드: 3일치 5분봉 전체
- 이후 5분마다: 신규 캔들만 append → 지표 증분 업데이트
- EventDetector는 Redis에서 읽기만 함 (REST 직접 호출 없음)
"""
import asyncio
import json
import logging

import pandas as pd
import redis

from events.detector import _compute_indicators

logger = logging.getLogger(__name__)

_CACHE_KEY_PREFIX = "ai:indicators:"
_WATCHES_KEY = "ai:watches"
_REFRESH_INTERVAL_SEC = 300  # 5분
_MAX_CANDLES = 500           # ~3일치 5분봉


class IndicatorCache:
    def __init__(self, redis_client: redis.Redis, domestic=None, overseas=None):
        self._r = redis_client
        self._domestic = domestic
        self._overseas = overseas
        self._candles: dict[str, pd.DataFrame] = {}  # 메모리 내 캔들 시계열

    def get(self, stock_code: str) -> dict:
        raw = self._r.get(f"{_CACHE_KEY_PREFIX}{stock_code}")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return {}

    async def refresh_loop(self) -> None:
        while True:
            try:
                await self._refresh_all()
            except Exception:
                logger.exception("지표 캐시 갱신 오류")
            await asyncio.sleep(_REFRESH_INTERVAL_SEC)

    async def _refresh_all(self) -> None:
        watches = self._load_watches()
        if not watches:
            return
        loop = asyncio.get_event_loop()
        for stock_code, watch in watches.items():
            market = watch.get("market", "domestic")
            exchange = watch.get("exchange")
            try:
                indicators = await loop.run_in_executor(
                    None, self._update, stock_code, market, exchange
                )
                if indicators:
                    self._r.set(
                        f"{_CACHE_KEY_PREFIX}{stock_code}",
                        json.dumps(indicators, ensure_ascii=False),
                        ex=_REFRESH_INTERVAL_SEC * 3,
                    )
                    logger.debug("지표 갱신: %s rows=%d", stock_code, len(self._candles.get(stock_code, [])))
            except Exception:
                logger.warning("지표 계산 실패: %s", stock_code, exc_info=True)

    def _update(self, stock_code: str, market: str, exchange: str | None) -> dict:
        if stock_code not in self._candles:
            # 최초: 3일치 전체 로드
            df = self._fetch_full(stock_code, market, exchange)
            if df is None or df.empty:
                return {}
            self._candles[stock_code] = df.tail(_MAX_CANDLES).reset_index(drop=True)
            logger.info("최초 로드: %s %d개 캔들", stock_code, len(self._candles[stock_code]))
        else:
            # 이후: 최신 캔들만 가져와서 신규분 append
            new_df = self._fetch_recent(stock_code, market, exchange)
            if new_df is not None and not new_df.empty:
                existing = self._candles[stock_code]
                last_dt = existing["datetime"].iloc[-1]
                added = new_df[new_df["datetime"] > last_dt]
                if not added.empty:
                    self._candles[stock_code] = pd.concat(
                        [existing, added], ignore_index=True
                    ).tail(_MAX_CANDLES).reset_index(drop=True)
                    logger.debug("캔들 append: %s +%d개", stock_code, len(added))

        df = self._candles[stock_code]
        return _compute_indicators(
            df["close"].tolist(),
            df["volume"].tolist() if "volume" in df.columns else [],
            highs=df["high"].tolist() if "high" in df.columns else None,
            lows=df["low"].tolist() if "low" in df.columns else None,
        )

    def _fetch_full(self, stock_code: str, market: str, exchange: str | None) -> pd.DataFrame | None:
        if market == "domestic" and self._domestic:
            return self._domestic.get_historical_minute_ohlcv(
                stock_code, lookback_days=3, candle_minutes=5
            )
        if market == "overseas" and self._overseas:
            return self._overseas.get_historical_minute_ohlcv(
                stock_code, exchange=self._exch(exchange), lookback_days=3, candle_minutes=5
            )
        return None

    def _fetch_recent(self, stock_code: str, market: str, exchange: str | None) -> pd.DataFrame | None:
        """최근 캔들만 가져옴 (lookback_days=1)."""
        if market == "domestic" and self._domestic:
            return self._domestic.get_historical_minute_ohlcv(
                stock_code, lookback_days=1, candle_minutes=5
            )
        if market == "overseas" and self._overseas:
            return self._overseas.get_historical_minute_ohlcv(
                stock_code, exchange=self._exch(exchange), lookback_days=1, candle_minutes=5
            )
        return None

    def _exch(self, exchange: str | None):
        from kis.constants import ExchangeCode
        return {
            "NAS": ExchangeCode.NASDAQ, "NASD": ExchangeCode.NASDAQ,
            "NYS": ExchangeCode.NYSE,   "NYSE": ExchangeCode.NYSE,
            "AMS": ExchangeCode.AMEX,   "AMEX": ExchangeCode.AMEX,
        }.get(exchange or "", ExchangeCode.NASDAQ)

    def _load_watches(self) -> dict:
        raw = self._r.get(_WATCHES_KEY)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return {}
