"""
전략 라우터.

MarketRegime → 적합한 전략 인스턴스 반환.
실시간 매매 루프에서 틱마다 호출해서 현재 장세에 맞는 전략으로 진입/청산 판단.
"""

import logging
from analysis.market_regime import MarketRegime, MarketSession
from trading.strategies import BreakoutStrategy, PullbackStrategy, GapStrategy
from trading.strategies.base import BaseStrategy, EntrySignal, ExitSignal
from analysis.indicators import calculate_indicators

import pandas as pd

logger = logging.getLogger(__name__)


class StrategyRouter:
    """
    MarketRegime을 보고 적합한 전략을 골라서 진입/청산 신호를 반환.
    여러 전략이 동시에 적용될 수 있음 (예: breakout + pullback 모두 확인).
    """

    def __init__(self, config: dict):
        risk = config.get("trading", {})
        stop  = risk.get("stop_loss_pct",  2.0)
        take  = risk.get("take_profit_pct", 5.0)

        self._strategies: dict[str, BaseStrategy] = {
            "breakout": BreakoutStrategy(stop_loss_pct=stop, take_profit_pct=take * 0.8),
            "pullback": PullbackStrategy(stop_loss_pct=stop, take_profit_pct=take),
            "gap":      GapStrategy(stop_loss_pct=stop, take_profit_pct=take * 0.6),
        }

    def get_active_strategies(self, regime: MarketRegime) -> list[BaseStrategy]:
        """현재 장세에서 활성화할 전략 목록 반환."""
        if not regime.tradeable:
            logger.debug("매매 불가 장세: %s", regime.reason)
            return []
        return [
            self._strategies[name]
            for name in regime.preferred_strategies
            if name in self._strategies
        ]

    def check_entry(
        self,
        regime: MarketRegime,
        dfs: dict[int, pd.DataFrame],
        tick: dict,
        context: dict,
        entry_counts: dict[str, int] | None = None,
        entry_limits: dict[str, int] | None = None,
        now: "pd.Timestamp | None" = None,
    ) -> tuple[bool, str, str]:
        """
        Returns: (should_enter, strategy_name, reason)
        dfs:          {candle_minutes: DataFrame}
        entry_counts: {strategy_name: 오늘 진입 횟수} — 한도 초과 시 스킵
        entry_limits: {strategy_name: 일일 최대 진입 횟수}
        now:          현재 시각 (is_active_at 판단용, None이면 체크 생략)
        """
        _indicator_cache: dict[int, pd.DataFrame] = {}

        for strategy in self.get_active_strategies(regime):
            # 일일 진입 한도 체크
            if entry_counts is not None and entry_limits is not None:
                limit = entry_limits.get(strategy.name, 999)
                if entry_counts.get(strategy.name, 0) >= limit:
                    continue

            # 허용 시간대 체크
            if now is not None and not strategy.is_active_at(now):
                continue

            minutes = strategy.candle_minutes
            if minutes not in _indicator_cache:
                raw = dfs.get(minutes, pd.DataFrame())
                _indicator_cache[minutes] = (
                    calculate_indicators(raw) if not raw.empty and len(raw) >= 5 else raw
                )
            df = _indicator_cache[minutes]
            if df.empty or len(df) < 2:
                continue

            signal: EntrySignal = strategy.check_entry(df, tick, context)
            if signal.should_enter:
                logger.info("[%s] 진입 신호 (%s, %dm봉): %s",
                            tick.get("code", ""), strategy.name, minutes, signal.reason)
                return True, strategy.name, signal.reason
        return False, "", ""

    def check_exit(
        self,
        dfs: dict[int, pd.DataFrame],
        tick: dict,
        position: dict,
        _indicator_cache: dict[int, pd.DataFrame] | None = None,
    ) -> tuple[bool, str]:
        """
        Returns: (should_exit, reason)
        포지션에 기록된 전략으로 청산 판단.
        _indicator_cache: check_entry에서 만든 캐시를 재사용하면 재계산 방지.
        """
        strategy_name = position.get("strategy", "")
        strategy = self._strategies.get(strategy_name)

        if strategy is None:
            return self._default_exit(tick, position)

        minutes = strategy.candle_minutes
        if _indicator_cache is not None and minutes in _indicator_cache:
            df = _indicator_cache[minutes]
        else:
            raw = dfs.get(minutes, pd.DataFrame())
            df = calculate_indicators(raw) if not raw.empty and len(raw) >= 5 else raw

        signal: ExitSignal = strategy.check_exit(df, tick, position)
        if signal.should_exit:
            logger.info("[%s] 청산 신호 (%s, %dm봉): %s",
                        tick.get("code", ""), strategy_name, minutes, signal.reason)
        return signal.should_exit, signal.reason

    @staticmethod
    def _default_exit(tick: dict, position: dict) -> tuple[bool, str]:
        """전략 미지정 시 단순 손절/익절."""
        price       = float(tick.get("price", 0) or 0)
        entry_price = float(position.get("entry_price", 0) or 0)
        if price <= 0 or entry_price <= 0:
            return False, ""
        pnl = (price - entry_price) / entry_price * 100
        if pnl <= -5.0:
            return True, f"기본 손절 {pnl:+.2f}%"
        if pnl >= 5.0:
            return True, f"기본 익절 {pnl:+.2f}%"
        return False, ""
