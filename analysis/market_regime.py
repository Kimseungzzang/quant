"""
시장 상황 분류 모듈.

MarketRegimeDetector.detect() → MarketRegime
  - 장세 방향 (상승/하락/횡보)
  - 변동성 수준
  - 시간대 (개장/본장/오후장/마감)
  - 추천 전략 목록
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta, time as dtime
from enum import Enum

import pandas as pd

logger = logging.getLogger(__name__)

# ── Enum 정의 ──────────────────────────────────────────────────────────

class MarketTrend(str, Enum):
    UP       = "up"        # 상승 추세
    DOWN     = "down"      # 하락 추세
    SIDEWAYS = "sideways"  # 횡보


class MarketSession(str, Enum):
    PRE_MARKET = "pre"        # ~09:00          장 전
    OPENING    = "opening"    # 09:00~09:30     개장 (갭 전략)
    MORNING    = "morning"    # 09:30~11:30     오전 본장 (돌파/눌림목)
    MIDDAY     = "midday"     # 11:30~14:00     점심 (신호 약화)
    AFTERNOON  = "afternoon"  # 14:00~15:20     오후장 (반등 전략)
    CLOSING    = "closing"    # 15:20~          마감 (청산만)


class MarketVolatility(str, Enum):
    LOW    = "low"
    NORMAL = "normal"
    HIGH   = "high"


# ── MarketRegime 데이터클래스 ──────────────────────────────────────────

@dataclass
class MarketRegime:
    trend:               MarketTrend
    trend_strength:      float           # 0~100 (강할수록 추세 명확)
    volatility:          MarketVolatility
    session:             MarketSession
    index_change_pct:    float           # KOSPI 당일 등락률
    preferred_strategies: list[str] = field(default_factory=list)
    tradeable:           bool = True
    reason:              str = ""

    def __str__(self) -> str:
        strategies = ", ".join(self.preferred_strategies) or "없음"
        return (
            f"[장세] {self.trend.value} | 강도={self.trend_strength:.0f} | "
            f"변동성={self.volatility.value} | 시간대={self.session.value} | "
            f"KOSPI={self.index_change_pct:+.2f}% | "
            f"전략={strategies} | 매매={'가능' if self.tradeable else '불가'}"
        )


# ── 공통 헬퍼 ─────────────────────────────────────────────────────────

def _parse_hhmm(value: str, fallback: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    try:
        h, m = value.split(":", 1)
        return int(h), int(m)
    except Exception:
        return fallback


def _is_between_cross_midnight(
    now: datetime, start: tuple[int, int], end: tuple[int, int]
) -> bool:
    cur       = now.hour * 60 + now.minute
    start_min = start[0] * 60 + start[1]
    end_min   = end[0]   * 60 + end[1]
    if start_min <= end_min:
        return start_min <= cur < end_min
    return cur >= start_min or cur < end_min


def _classify_trend(df: pd.DataFrame) -> tuple["MarketTrend", float]:
    if df.empty or len(df) < 5:
        return MarketTrend.SIDEWAYS, 0.0
    closes = df["close"].astype(float)
    ema5   = closes.ewm(span=5,  adjust=False).mean()
    ema20  = closes.ewm(span=20, adjust=False).mean()
    last5, last20 = float(ema5.iloc[-1]), float(ema20.iloc[-1])
    if last5 > last20 * 1.003:
        trend = MarketTrend.UP
    elif last5 < last20 * 0.997:
        trend = MarketTrend.DOWN
    else:
        trend = MarketTrend.SIDEWAYS
    pct_5d   = abs((float(closes.iloc[-1]) - float(closes.iloc[-5])) / float(closes.iloc[-5]) * 100) \
               if len(closes) >= 5 else 0.0
    strength = round(min(pct_5d * 10, 100.0), 1)
    return trend, strength


def _classify_volatility(df: pd.DataFrame) -> "MarketVolatility":
    if df.empty or len(df) < 5:
        return MarketVolatility.NORMAL
    closes  = df["close"].astype(float)
    returns = closes.pct_change().dropna().tail(5)
    std     = float(returns.std()) * 100
    if std < 0.5:
        return MarketVolatility.LOW
    if std > 1.5:
        return MarketVolatility.HIGH
    return MarketVolatility.NORMAL


def _today_change(df: pd.DataFrame) -> float:
    if df.empty or len(df) < 2:
        return 0.0
    try:
        last = float(df["close"].iloc[-1])
        prev = float(df["close"].iloc[-2])
        return round((last - prev) / prev * 100, 2)
    except Exception:
        return 0.0


# ── 탐지기 ────────────────────────────────────────────────────────────

class MarketRegimeDetector:
    """
    KOSPI 일봉 + 현재 시각으로 MarketRegime을 판단.
    domestic_api: DomesticAPI 인스턴스
    """

    # KOSPI 프록시: KODEX 200 ETF (KIS paper API가 지수 일봉 미지원)
    KOSPI_CODE = "069500"

    def __init__(self, domestic_api):
        self.domestic = domestic_api

    def detect(self) -> MarketRegime:
        now     = datetime.now()
        session = self._classify_session(now)
        kospi   = self._fetch_kospi()

        trend, strength     = _classify_trend(kospi)
        volatility          = _classify_volatility(kospi)
        index_change        = _today_change(kospi)
        tradeable, reason   = self._is_tradeable(session, volatility, trend)
        strategies          = self._preferred_strategies(session, trend, volatility)

        regime = MarketRegime(
            trend=trend,
            trend_strength=strength,
            volatility=volatility,
            session=session,
            index_change_pct=index_change,
            preferred_strategies=strategies,
            tradeable=tradeable,
            reason=reason,
        )
        logger.info("장세 분석: %s", regime)
        return regime

    # ── KOSPI 데이터 ───────────────────────────────────────────────────

    def _fetch_kospi(self) -> pd.DataFrame:
        """최근 30일 KOSPI 일봉 조회."""
        end   = date.today()
        start = end - timedelta(days=45)  # 거래일 약 30일 확보
        try:
            df = self.domestic.get_daily_ohlcv(self.KOSPI_CODE, start, end)
            return df
        except Exception as e:
            logger.warning("KOSPI 조회 실패: %s → 기본값 사용", e)
            return pd.DataFrame()

    # ── 시간대 분류 ───────────────────────────────────────────────────

    @staticmethod
    def _classify_session(now: datetime) -> MarketSession:
        t = now.time()
        if t < dtime(9, 0):
            return MarketSession.PRE_MARKET
        elif t < dtime(9, 30):
            return MarketSession.OPENING
        elif t < dtime(11, 30):
            return MarketSession.MORNING
        elif t < dtime(14, 0):
            return MarketSession.MIDDAY
        elif t < dtime(15, 20):
            return MarketSession.AFTERNOON
        else:
            return MarketSession.CLOSING

    # ── 매매 가능 여부 ────────────────────────────────────────────────

    @staticmethod
    def _is_tradeable(
        session: MarketSession,
        volatility: MarketVolatility,
        trend: MarketTrend,
    ) -> tuple[bool, str]:
        if session == MarketSession.PRE_MARKET:
            return False, "장 전"
        if session == MarketSession.CLOSING:
            return False, "마감 구간 — 청산만"
        if volatility == MarketVolatility.HIGH and trend == MarketTrend.DOWN:
            return False, "하락 고변동성 — 매매 위험"
        return True, ""

    # ── 추천 전략 ─────────────────────────────────────────────────────

    @staticmethod
    def _preferred_strategies(
        session: MarketSession,
        trend: MarketTrend,
        volatility: MarketVolatility,
    ) -> list[str]:
        strategies: list[str] = []

        if session == MarketSession.OPENING:
            strategies.append("gap")          # 갭 매매는 개장 구간에만

        if session in (MarketSession.MORNING, MarketSession.MIDDAY, MarketSession.AFTERNOON):
            if trend == MarketTrend.UP:
                strategies.append("breakout")  # 상승 추세 → 돌파
                strategies.append("pullback")  # 상승 추세 → 눌림목
            elif trend == MarketTrend.SIDEWAYS:
                strategies.append("pullback")  # 횡보 → 눌림목만 (방향성 없어서 돌파 위험)

        if session == MarketSession.AFTERNOON and trend != MarketTrend.DOWN:
            strategies.append("afternoon")    # 오후 반등

        # 횡보 + 변동성 낮음 → 매매 안 함
        if trend == MarketTrend.SIDEWAYS and volatility == MarketVolatility.LOW:
            strategies.clear()

        return strategies


# ── 미국 장세 탐지기 ──────────────────────────────────────────────────

class OverseasRegimeDetector:
    """
    KODEX S&P500 ETF(360750) 일봉 + 현재 시각으로 미국 MarketRegime 판단.
    국내 시장이 열려 있을 때 360750 ETF 일봉을 통해 S&P500 추세를 근사한다.
    """

    SP500_CODE = "360750"   # KODEX S&P500 ETF

    def __init__(self, domestic_api, config: dict):
        self.domestic = domestic_api
        self.config   = config

    def detect(self) -> MarketRegime:
        now = datetime.now()
        session, tradeable, reason = self._classify_session(now)
        df = self._fetch_sp500()

        trend, strength = _classify_trend(df)
        volatility      = _classify_volatility(df)
        index_change    = _today_change(df)

        # 하락 고변동성 시 매매 불가
        if tradeable and volatility == MarketVolatility.HIGH and trend == MarketTrend.DOWN:
            tradeable = False
            reason    = "하락 고변동성 — 매매 위험"

        strategies = self._preferred_strategies(session, trend, volatility) if tradeable else []

        regime = MarketRegime(
            trend=trend,
            trend_strength=strength,
            volatility=volatility,
            session=session,
            index_change_pct=index_change,
            preferred_strategies=strategies,
            tradeable=tradeable,
            reason=reason,
        )
        logger.info("미국 장세 분석: %s", regime)
        return regime

    def _fetch_sp500(self) -> pd.DataFrame:
        """KODEX S&P500 ETF 최근 45일 일봉."""
        end   = date.today()
        start = end - timedelta(days=45)
        try:
            return self.domestic.get_daily_ohlcv(self.SP500_CODE, start, end)
        except Exception as e:
            logger.warning("S&P500(360750) 조회 실패: %s → 기본값 사용", e)
            return pd.DataFrame()

    def _classify_session(self, now: datetime) -> tuple[MarketSession, bool, str]:
        sched    = self.config.get("schedule", {})
        us_start = _parse_hhmm(sched.get("us_analysis_time",    "22:30"))
        us_end   = _parse_hhmm(sched.get("us_trading_end_time", "05:00"))

        in_us       = _is_between_cross_midnight(now, us_start, us_end)
        in_daytime  = (10, 0) <= (now.hour, now.minute) < (22, 0)

        if not (in_us or in_daytime):
            return MarketSession.CLOSING, False, "해외주식 거래 시간 외 (05:00~10:00)"

        if in_us:
            us_start_min      = us_start[0] * 60 + us_start[1]
            minutes_from_open = (now.hour * 60 + now.minute - us_start_min) % (24 * 60)
            session = MarketSession.OPENING if minutes_from_open < 30 else MarketSession.MORNING
            return session, True, "미국 정규장"

        return MarketSession.MORNING, True, "주간거래"

    @staticmethod
    def _preferred_strategies(
        session: MarketSession, trend: MarketTrend, volatility: MarketVolatility
    ) -> list[str]:
        strategies: list[str] = []
        if session == MarketSession.OPENING:
            strategies.append("gap")
        if session in (MarketSession.OPENING, MarketSession.MORNING):
            if trend == MarketTrend.UP:
                strategies.extend(["breakout", "pullback"])
            elif trend == MarketTrend.SIDEWAYS:
                strategies.append("pullback")
        if trend == MarketTrend.SIDEWAYS and volatility == MarketVolatility.LOW:
            strategies.clear()
        return strategies
