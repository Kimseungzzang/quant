"""
돌파 매매 전략.

진입: N일 고점 돌파 + 거래량 급증
청산: 손절(돌파가 아래 복귀) / 익절(목표가) / MACD 데드크로스
"""

import logging
import pandas as pd
from .base import BaseStrategy, EntrySignal, ExitSignal

logger = logging.getLogger(__name__)


class BreakoutStrategy(BaseStrategy):

    def __init__(
        self,
        stop_loss_pct: float = 2.0,    # 손절: 돌파가 대비 -2%
        take_profit_pct: float = 4.0,  # 익절: 돌파가 대비 +4% (R:R 1:2)
        vol_threshold: float = 1.5,    # 거래량 1.5배 이상
    ):
        self.stop_loss_pct  = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.vol_threshold  = vol_threshold

    @property
    def name(self) -> str:
        return "breakout"

    @property
    def candle_minutes(self) -> int:
        return 5

    def check_entry(self, df: pd.DataFrame, tick: dict, context: dict) -> EntrySignal:
        """
        context 필수 키:
          resistance: float  — 돌파 기준가 (N일 고점)
        """
        resistance = context.get("resistance")
        if resistance is None or resistance <= 0:
            return EntrySignal(False, "저항선 정보 없음")

        price     = float(tick.get("price", 0) or 0)
        vol_ratio = self._vol_ratio(df)

        if price <= 0:
            return EntrySignal(False, "가격 이상")

        # 돌파 확인: 현재가가 저항선을 0.3% 이상 상회
        broke_out = price > resistance * 1.003
        if not broke_out:
            return EntrySignal(False, f"미돌파 (현재가 {price:,.0f} / 저항선 {resistance:,.0f})")

        # 거래량 확인
        if vol_ratio < self.vol_threshold:
            return EntrySignal(False, f"거래량 부족 (vol_ratio={vol_ratio:.2f})")

        return EntrySignal(True, f"돌파 확인 @ {price:,.0f} (저항선 {resistance:,.0f}, vol={vol_ratio:.2f}x)")

    def check_exit(self, df: pd.DataFrame, tick: dict, position: dict) -> ExitSignal:
        price       = float(tick.get("price", 0) or 0)
        entry_price = float(position.get("entry_price", 0) or 0)

        if price <= 0 or entry_price <= 0:
            return ExitSignal(False)

        pnl_pct = (price - entry_price) / entry_price * 100

        # 손절: 진입가 대비 -stop_loss_pct
        if pnl_pct <= -self.stop_loss_pct:
            return ExitSignal(True, f"손절 {pnl_pct:+.2f}%")

        # 익절: 진입가 대비 +take_profit_pct
        if pnl_pct >= self.take_profit_pct:
            return ExitSignal(True, f"익절 {pnl_pct:+.2f}%")

        # MACD 데드크로스
        if self._macd_dead_cross(df):
            return ExitSignal(True, f"MACD 데드크로스 ({pnl_pct:+.2f}%)")

        return ExitSignal(False)

    @staticmethod
    def _vol_ratio(df: pd.DataFrame) -> float:
        if df.empty or "volume" not in df.columns or len(df) < 2:
            return 0.0
        ma20 = df["volume"].tail(20).mean()
        if ma20 <= 0:
            return 0.0
        return float(df["volume"].iloc[-1]) / float(ma20)

    @staticmethod
    def _macd_dead_cross(df: pd.DataFrame) -> bool:
        if df.empty or "macd_hist" not in df.columns or len(df) < 2:
            return False
        prev = df["macd_hist"].iloc[-2]
        curr = df["macd_hist"].iloc[-1]
        if pd.isna(prev) or pd.isna(curr):
            return False
        return float(prev) > 0 > float(curr)
