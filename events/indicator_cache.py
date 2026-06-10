"""
기술 지표 캐시.
- 5분봉: watch 등록 시 3일치 전체 로드, 이후 5분마다 증분 업데이트
- 일봉:  watch 등록 시 180일치 로드, 이후 30분마다 갱신
- EventDetector는 Redis에서 읽기만 함 (REST 직접 호출 없음)
"""
import asyncio
import json
import logging
import time

import pandas as pd
import redis

from events.detector import _compute_indicators

logger = logging.getLogger(__name__)

_CACHE_KEY_PREFIX = "ai:indicators:"
_WATCHES_KEY = "ai:watches"
_REFRESH_INTERVAL_SEC = 300    # 5분봉 갱신 주기
_DAILY_REFRESH_INTERVAL_SEC = 1800  # 일봉 갱신 주기 (30분)
_MAX_CANDLES = 500             # ~3일치 5분봉
_DAILY_LOOKBACK_DAYS = 180     # MA60 계산에 충분한 일봉 수


class IndicatorCache:
    def __init__(self, redis_client: redis.Redis, domestic=None, overseas=None):
        self._r = redis_client
        self._domestic = domestic
        self._overseas = overseas
        self._candles: dict[str, pd.DataFrame] = {}
        self._daily_candles: dict[str, pd.DataFrame] = {}
        self._daily_last_refresh: dict[str, float] = {}

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
                    logger.debug("지표 갱신: %s", stock_code)
            except Exception:
                logger.warning("지표 계산 실패: %s", stock_code, exc_info=True)

    def _update(self, stock_code: str, market: str, exchange: str | None) -> dict:
        # ── 5분봉 지표 ──────────────────────────────────────────────
        if stock_code not in self._candles:
            df = self._fetch_full(stock_code, market, exchange)
            if df is None or df.empty:
                return {}
            self._candles[stock_code] = df.tail(_MAX_CANDLES).reset_index(drop=True)
            logger.info("5분봉 최초 로드: %s %d개", stock_code, len(self._candles[stock_code]))
        else:
            new_df = self._fetch_recent(stock_code, market, exchange)
            if new_df is not None and not new_df.empty:
                existing = self._candles[stock_code]
                last_dt = existing["datetime"].iloc[-1]
                added = new_df[new_df["datetime"] > last_dt]
                if not added.empty:
                    self._candles[stock_code] = pd.concat(
                        [existing, added], ignore_index=True
                    ).tail(_MAX_CANDLES).reset_index(drop=True)
                    logger.debug("5분봉 append: %s +%d개", stock_code, len(added))

        df5 = self._candles[stock_code]
        indicators_5min = _compute_indicators(
            df5["close"].tolist(),
            df5["volume"].tolist() if "volume" in df5.columns else [],
            highs=df5["high"].tolist() if "high" in df5.columns else None,
            lows=df5["low"].tolist() if "low" in df5.columns else None,
        )

        # ── 일봉 지표 ──────────────────────────────────────────────
        indicators_daily = self._update_daily(stock_code, market, exchange)

        return {**indicators_5min, **indicators_daily}

    def _update_daily(self, stock_code: str, market: str, exchange: str | None) -> dict:
        now = time.monotonic()
        last = self._daily_last_refresh.get(stock_code, 0)
        stale = (now - last) >= _DAILY_REFRESH_INTERVAL_SEC

        if stale or stock_code not in self._daily_candles:
            df = self._fetch_daily(stock_code, market, exchange)
            if df is None or df.empty:
                return {}
            self._daily_candles[stock_code] = df
            self._daily_last_refresh[stock_code] = now
            logger.info("일봉 로드: %s %d개", stock_code, len(df))

        df_d = self._daily_candles[stock_code]
        ind = _compute_indicators(
            df_d["close"].tolist(),
            df_d["volume"].tolist() if "volume" in df_d.columns else [],
            highs=df_d["high"].tolist() if "high" in df_d.columns else None,
            lows=df_d["low"].tolist() if "low" in df_d.columns else None,
        )
        return {f"{k}_daily": v for k, v in ind.items()}

    def _fetch_daily(self, stock_code: str, market: str, exchange: str | None) -> pd.DataFrame | None:
        from datetime import date, timedelta
        end = date.today()
        start = end - timedelta(days=_DAILY_LOOKBACK_DAYS)
        if market == "domestic" and self._domestic:
            return self._domestic.get_daily_ohlcv(stock_code, start, end)
        if market == "overseas" and self._overseas:
            return self._overseas.get_daily_ohlcv(
                stock_code, self._exch(exchange), start_date=start, end_date=end
            )
        return None

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
