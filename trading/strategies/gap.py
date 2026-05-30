"""
갭 상승 매매 전략 (개장 구간 전용).

진입: 갭 상승 후 첫 눌림 → 거래량 감소 → 반등 확인 시 매수
청산: 갭 시작가 아래 복귀(손절) / 목표가(익절) / 거래량 소멸
"""

import logging
import pandas as pd
from .base import BaseStrategy, EntrySignal, ExitSignal

logger = logging.getLogger(__name__)


class GapStrategy(BaseStrategy):

    def __init__(
        self,
        min_gap_pct: float = 2.0,      # 최소 갭 크기 (전일 종가 대비 %)
        stop_loss_pct: float = 2.0,    # 손절: 갭 시작가 아래
        take_profit_pct: float = 3.0,  # 익절 (갭 크기의 절반 정도)
        vol_threshold: float = 1.5,
    ):
        self.min_gap_pct    = min_gap_pct
        self.stop_loss_pct  = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.vol_threshold  = vol_threshold

    @property
    def name(self) -> str:
        return "gap"

    @property
    def candle_minutes(self) -> int:
        return 1

    def is_active_at(self, dt: "pd.Timestamp") -> bool:
        """갭 전략은 개장 구간(09:00~09:30)에만 진입."""
        h, m = dt.hour, dt.minute
        return (9, 0) <= (h, m) <= (9, 30)

    def check_entry(self, df: pd.DataFrame, tick: dict, context: dict) -> EntrySignal:
        """
        context 필수 키:
          prev_close: float  — 전일 종가
          gap_open:   float  — 갭 시작가 (당일 시가)
        """
        prev_close = context.get("prev_close")
        gap_open   = context.get("gap_open")

        if not prev_close or not gap_open or prev_close <= 0:
            return EntrySignal(False, "갭 정보 없음")

        gap_pct = (gap_open - prev_close) / prev_close * 100

        # 갭 크기 최소 조건
        if gap_pct < self.min_gap_pct:
            return EntrySignal(False, f"갭 크기 부족 ({gap_pct:.2f}% < {self.min_gap_pct}%)")

        price = float(tick.get("price", 0) or 0)
        if price <= 0:
            return EntrySignal(False)

        # 가격이 갭 시작가보다 아래로 내려왔다가 회복 중 (첫 눌림 후 반등)
        pulled_back = price < gap_open * 0.995  # 갭 시작가 -0.5% 이하로 눌렸어야
        if df.empty or len(df) < 3:
            return EntrySignal(False, "캔들 부족")

        # 최근 저점이 갭 시작가보다 낮은지 확인
        recent_low = float(df["low"].tail(3).min())
        if recent_low >= gap_open:
            return EntrySignal(False, "아직 눌림 미발생")

        # 현재 가격이 반등 중 (직전 캔들보다 높음)
        if len(df) >= 2:
            prev_close_candle = float(df["close"].iloc[-2])
            if price <= prev_close_candle:
                return EntrySignal(False, "반등 미확인")

        # 거래량 확인
        vol_ratio = self._vol_ratio(df)
        if vol_ratio < self.vol_threshold:
            return EntrySignal(False, f"거래량 부족 ({vol_ratio:.2f}x)")

        # 매수세 확인: 체결강도 + 거래량 가속 (2개 중 1개 이상)
        buying_score = self._buying_pressure_score(df)
        if buying_score < 1:
            return EntrySignal(False, f"매수세 부족 (score={buying_score}/2)")

        return EntrySignal(
            True,
            f"갭 상승 후 눌림 반등 @ {price:,.0f} (갭={gap_pct:.1f}%, vol={vol_ratio:.2f}x, 매수세={buying_score}/2)"
        )

    def check_exit(self, df: pd.DataFrame, tick: dict, position: dict) -> ExitSignal:
        price       = float(tick.get("price", 0) or 0)
        entry_price = float(position.get("entry_price", 0) or 0)

        if price <= 0 or entry_price <= 0:
            return ExitSignal(False)

        pnl_pct = (price - entry_price) / entry_price * 100

        if pnl_pct <= -self.stop_loss_pct:
            return ExitSignal(True, f"손절 {pnl_pct:+.2f}%")

        if pnl_pct >= self.take_profit_pct:
            return ExitSignal(True, f"익절 {pnl_pct:+.2f}%")

        # 거래량 소멸 — 상승 동력 상실
        vol_ratio = self._vol_ratio(df)
        if vol_ratio < 0.7 and pnl_pct > 0:
            return ExitSignal(True, f"거래량 소멸 — 수익 보호 ({pnl_pct:+.2f}%)")

        return ExitSignal(False)

    @staticmethod
    def _buying_pressure_score(df: pd.DataFrame) -> int:
        """
        갭 전략용 매수세 점수 (0~2). 1점 이상이면 진입.

        ① 체결강도: 현재 봉에서 가격이 봉 상단 60% 이상에 위치
        ② 거래량 가속: 직전 봉보다 현재 봉 거래량 증가
        """
        if df.empty or len(df) < 2:
            return 0
        score = 0
        cur = df.iloc[-1]

        high  = cur.get("high")
        low   = cur.get("low")
        close = cur.get("close")
        if pd.notna(high) and pd.notna(low) and pd.notna(close):
            rng = float(high) - float(low)
            if rng > 0 and (float(close) - float(low)) / rng >= 0.6:
                score += 1

        if "volume" in df.columns:
            v_cur  = float(df["volume"].iloc[-1] or 0)
            v_prev = float(df["volume"].iloc[-2] or 0)
            if v_prev > 0 and v_cur > v_prev:
                score += 1

        return score

    @staticmethod
    def _vol_ratio(df: pd.DataFrame) -> float:
        if df.empty or "volume" not in df.columns or len(df) < 2:
            return 0.0
        ma = df["volume"].mean()
        if ma <= 0:
            return 0.0
        return float(df["volume"].iloc[-1]) / float(ma)
