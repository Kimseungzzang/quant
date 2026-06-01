"""
눌림목 매매 전략.

진입: 상승 추세 확인 후 MA 근처 눌림 → 매수세 3종 동시 확인 후 진입
  - 체결강도: 봉 내 매수 우위 (close가 봉 상단에 위치)
  - 거래량 가속: 눌림 봉보다 현재 봉 거래량 증가
  - OBV 상승: 순매수 누적량 증가 추세
청산: 손절(눌림목 저점 아래) / 익절(이전 고점 근처) / 추세 훼손
"""

import logging
import pandas as pd
from .base import BaseStrategy, EntrySignal, ExitSignal

logger = logging.getLogger(__name__)


class PullbackStrategy(BaseStrategy):

    def __init__(
        self,
        stop_loss_pct: float = 2.0,
        take_profit_pct: float = 5.0,
        ma_proximity_pct: float = 2.0,  # MA 근처 허용 범위 ±2%
        vol_threshold: float = 1.3,
    ):
        self.stop_loss_pct   = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.ma_proximity    = ma_proximity_pct / 100
        self.vol_threshold   = vol_threshold

    @property
    def name(self) -> str:
        return "pullback"

    def check_entry(self, df: pd.DataFrame, tick: dict, context: dict) -> EntrySignal:
        if df.empty or len(df) < 10:
            return EntrySignal(False, "데이터 부족")

        price = float(tick.get("price", 0) or 0)
        if price <= 0:
            return EntrySignal(False)

        row = df.iloc[-1]
        ema5  = row.get("ema5")
        ema20 = row.get("ema20")

        if pd.isna(ema5) or pd.isna(ema20):
            return EntrySignal(False, "EMA 미계산")

        ema5, ema20 = float(ema5), float(ema20)

        # ① 상승 추세 확인 (EMA5 > EMA20)
        if ema5 <= ema20:
            return EntrySignal(False, "상승 추세 아님")

        # ② 가격이 EMA20 근처에서 눌림 (EMA20 ± ma_proximity)
        near_ma20 = abs(price - ema20) / ema20 <= self.ma_proximity
        near_ema5 = abs(price - ema5) / ema5 <= self.ma_proximity

        if not (near_ma20 or near_ema5):
            return EntrySignal(False, f"MA 근처 아님 (가격={price:,.0f}, EMA20={ema20:,.0f})")

        # ③ RSI 과매도 아님 (눌림이지 추세 전환 아님)
        rsi = row.get("rsi")
        if pd.notna(rsi) and float(rsi) < 35:
            return EntrySignal(False, f"RSI 과매도 {rsi:.1f} → 추세 전환 가능성")

        # ④ 거래량 평균 대비 회복 (데이터 부족 시 스킵)
        vol_ratio = self._vol_ratio(df)
        if vol_ratio > 0 and vol_ratio < self.vol_threshold:
            return EntrySignal(False, f"거래량 미회복 (vol_ratio={vol_ratio:.2f})")

        # ⑤ 가격이 직전 캔들보다 높아야 (반등 확인)
        if len(df) >= 2:
            prev_close = float(df["close"].iloc[-2])
            if price <= prev_close:
                return EntrySignal(False, "반등 미확인 (이전 봉보다 낮음)")

        # ⑥ 매수세 3종 확인 (하락 추세 속 기술적 반등 필터)
        buying_score = self._buying_pressure_score(df)
        if buying_score < 2:
            return EntrySignal(False, f"매수세 부족 (score={buying_score}/3)")

        # ⑦ 호가 불균형: 실시간 bid 잔량 우위 확인 (데이터 있을 때만)
        askbid = context.get("askbid")
        imbalance = askbid["imbalance"] if askbid else None
        if imbalance is not None and imbalance < 0.55:
            return EntrySignal(False, f"호가 매도우위 (imbalance={imbalance:.2f})")

        imb_str = f", 호가={imbalance:.2f}" if imbalance is not None else ""
        return EntrySignal(
            True,
            f"눌림목 반등 @ {price:,.0f} (EMA20={ema20:,.0f}, vol={vol_ratio:.2f}x, 매수세={buying_score}/3{imb_str})"
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

        # EMA5 < EMA20 → 추세 훼손
        if not df.empty and len(df) >= 1:
            row = df.iloc[-1]
            e5  = row.get("ema5")
            e20 = row.get("ema20")
            if pd.notna(e5) and pd.notna(e20) and float(e5) < float(e20):
                return ExitSignal(True, f"추세 훼손 EMA5 < EMA20 ({pnl_pct:+.2f}%)")

        return ExitSignal(False)

    @staticmethod
    def _buying_pressure_score(df: pd.DataFrame) -> int:
        """
        매수세 강도 점수 (0~3). 2점 이상이면 실제 매수세 유입으로 판단.

        ① 체결강도: 현재 봉에서 가격이 봉 상단에 마감 (매수 우위)
           (close - low) / (high - low) >= 0.6
        ② 거래량 가속: 직전 봉보다 현재 봉 거래량 증가 (매수 유입)
           volume[-1] > volume[-2]
        ③ OBV 상승: 3봉 전보다 OBV 증가 (순매수 누적 증가)
           obv[-1] > obv[-3]
        """
        if df.empty or len(df) < 4:
            return 0

        score = 0
        cur = df.iloc[-1]

        # ① 체결강도 (봉 내 가격 위치)
        high  = cur.get("high")
        low   = cur.get("low")
        close = cur.get("close")
        if pd.notna(high) and pd.notna(low) and pd.notna(close):
            rng = float(high) - float(low)
            if rng > 0 and (float(close) - float(low)) / rng >= 0.6:
                score += 1

        # ② 거래량 가속
        if "volume" in df.columns:
            v_cur  = float(df["volume"].iloc[-1] or 0)
            v_prev = float(df["volume"].iloc[-2] or 0)
            if v_prev > 0 and v_cur > v_prev:
                score += 1

        # ③ OBV 상승 추세
        if "obv" in df.columns:
            obv_cur  = df["obv"].iloc[-1]
            obv_old  = df["obv"].iloc[-4]   # 3봉 전
            if pd.notna(obv_cur) and pd.notna(obv_old) and float(obv_cur) > float(obv_old):
                score += 1

        return score

    @staticmethod
    def _vol_ratio(df: pd.DataFrame) -> float:
        if df.empty or "volume" not in df.columns or len(df) < 2:
            return 0.0
        ma20 = df["volume"].tail(20).mean()
        if ma20 <= 0:
            return 0.0
        return float(df["volume"].iloc[-1]) / float(ma20)
