"""
전략 인터페이스.
모든 전략은 BaseStrategy를 상속하고 check_entry / check_exit 구현.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
import pandas as pd


@dataclass
class EntrySignal:
    should_enter: bool
    reason: str = ""


@dataclass
class ExitSignal:
    should_exit: bool
    reason: str = ""


class BaseStrategy(ABC):

    @property
    @abstractmethod
    def name(self) -> str:
        """전략 이름."""

    @property
    def candle_minutes(self) -> int:
        """전략에 사용할 봉 주기(분). 서브클래스에서 재정의."""
        return 15

    def is_active_at(self, dt: "pd.Timestamp") -> bool:
        """현재 시각이 이 전략의 허용 시간대인지 확인. 기본값: 항상 True."""
        return True

    @abstractmethod
    def check_entry(self, df: pd.DataFrame, tick: dict, context: dict) -> EntrySignal:
        """
        매수 진입 여부 판단.
        df: 현재까지의 15분봉 DataFrame (지표 포함)
        tick: 최신 틱 {'price', 'vol', 'time'}
        context: 종목별 부가 정보 {'resistance', 'prev_close', 'gap_pct', ...}
        """

    @abstractmethod
    def check_exit(self, df: pd.DataFrame, tick: dict, position: dict) -> ExitSignal:
        """
        매도 청산 여부 판단.
        position: {'entry_price', 'entry_time', 'strategy'}
        """
