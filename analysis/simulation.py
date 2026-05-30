"""
실전 루프와 동일한 코드 경로로 과거 데이터를 재현하는 시뮬레이션.

backtester.py 와의 차이:
  backtester.py  → 자체 루프, 별도 진입/청산 로직, 실전 코드와 별개
  simulation.py  → CandleAggregator / StrategyRouter / is_active_at /
                   일일 진입 한도 / 마감 강제 청산 / context 동적 갱신 전부 실전과 동일
"""

import logging
import numpy as np
import pandas as pd
from dataclasses import dataclass
from datetime import datetime, date

from main import CandleAggregator, _LIVE_ENTRY_LIMITS, _CLOSE_HOUR, _CLOSE_MIN
from trading.strategy_router import StrategyRouter
from analysis.backtester import BacktestResult, TradeRecord, _calc_mdd, _calc_sharpe
from analysis.market_regime import (
    MarketRegimeDetector, MarketRegime,
    MarketTrend, MarketVolatility,
)

logger = logging.getLogger(__name__)


# ── 시뮬레이션 주문 관리자 ────────────────────────────────────────────

@dataclass
class SimPosition:
    stock_code: str
    name: str
    exchange: str
    entry_price: float
    entry_time: str
    entry_date: "date | None" = None   # 보유일 계산용
    strategy: str = ""


class SimOrderManager:
    """실제 주문 없이 포지션·손익을 메모리에서 추적."""

    def __init__(self, max_positions: int = 5):
        self._positions: dict[str, SimPosition] = {}
        self.trades: list[TradeRecord] = []
        self.max_positions = max_positions
        self._now_str: str = ""
        self._now_date: "date | None" = None

    def set_time(self, s: str, d: "date | None" = None):
        self._now_str  = s
        self._now_date = d

    def get_open_positions(self) -> dict:
        return dict(self._positions)

    def open_position(self, stock_code: str, name: str, exchange: str,
                      price: float, strategy: str = "") -> bool:
        if stock_code in self._positions or len(self._positions) >= self.max_positions:
            return False
        self._positions[stock_code] = SimPosition(
            stock_code=stock_code, name=name, exchange=exchange,
            entry_price=price, entry_time=self._now_str,
            entry_date=self._now_date, strategy=strategy,
        )
        logger.debug("[SIM 매수] %s @ %,.0f (%s)", stock_code, price, strategy)
        return True

    def close_position(self, stock_code: str, price: float, reason: str = ""):
        pos = self._positions.pop(stock_code, None)
        if pos is None:
            return
        pnl = (price - pos.entry_price) / pos.entry_price * 100
        self.trades.append(TradeRecord(
            strategy=pos.strategy,
            entry_time=pos.entry_time,
            entry_price=round(pos.entry_price, 2),
            exit_time=self._now_str,
            exit_price=round(price, 2),
            pnl_pct=round(pnl, 2),
            exit_reason=reason,
        ))
        logger.debug("[SIM 매도] %s @ %,.0f | %+.2f%% | %s", stock_code, price, pnl, reason)

    def on_price_update(self, *args):
        pass  # fallback 호환용


# ── 과거 KOSPI 기반 장세 감지기 ──────────────────────────────────────

class HistoricalRegimeDetector(MarketRegimeDetector):
    """
    KOSPI 과거 일봉으로 특정 날짜의 MarketRegime을 계산.
    MarketRegimeDetector를 상속해 분류 로직을 그대로 재사용.
    """

    def __init__(self, kospi_df: pd.DataFrame):
        # domestic_api 없이 초기화 — _fetch_kospi는 오버라이드
        self.domestic = None
        df = kospi_df.copy()
        df["date"] = pd.to_datetime(df["date"])
        self._kospi_hist = df

    def detect_for(self, target_date: date, current_time: datetime) -> MarketRegime:
        """target_date까지의 최근 30거래일 KOSPI로 MarketRegime 계산."""
        cutoff = pd.Timestamp(target_date)
        df = self._kospi_hist[self._kospi_hist["date"] <= cutoff].tail(30)

        if len(df) < 2:
            return self._fallback(current_time)

        trend,    strength   = self._classify_trend(df)
        volatility           = self._classify_volatility(df)
        index_change         = self._today_change(df)
        session              = self._classify_session(current_time)
        tradeable, reason    = self._is_tradeable(session, volatility, trend)
        strategies           = self._preferred_strategies(session, trend, volatility)

        return MarketRegime(
            trend=trend, trend_strength=strength,
            volatility=volatility, session=session,
            index_change_pct=index_change,
            preferred_strategies=strategies,
            tradeable=tradeable, reason=reason,
        )

    def _fallback(self, current_time: datetime) -> MarketRegime:
        session = self._classify_session(current_time)
        tradeable, reason = self._is_tradeable(
            session, MarketVolatility.NORMAL, MarketTrend.SIDEWAYS
        )
        return MarketRegime(
            trend=MarketTrend.SIDEWAYS, trend_strength=0.0,
            volatility=MarketVolatility.NORMAL, session=session,
            index_change_pct=0.0, preferred_strategies=["breakout", "pullback"],
            tradeable=tradeable, reason=reason,
        )


# ── 전략별 규칙 ──────────────────────────────────────────────────────

# 오버나잇 최대 횟수 (0 = 당일 15:20 강제 청산)
_MAX_HOLD_DAYS = {"gap": 0, "breakout": 1, "pullback": 2}

# 15:20 강제 청산 대상 전략 (gap만)
_INTRADAY_ONLY = {"gap"}


# ── 메인 시뮬레이션 ───────────────────────────────────────────────────

def run_simulation(
    stock_code: str,
    df_1m: pd.DataFrame,
    config: dict,
    context: dict,
    kospi_df: pd.DataFrame | None = None,
    name: str = "",
    exchange: str = "KRX",
) -> BacktestResult:
    """
    실전 루프와 동일한 코드 경로로 과거 1분봉을 재현.

    df_1m:    datetime / open / high / low / close / volume
    context:  장전 분석에서 계산된 초기 context
    kospi_df: KOSPI 일봉 (없으면 기본 MarketRegime 사용)
    """
    if df_1m.empty or len(df_1m) < 10:
        return BacktestResult(stock_code=stock_code)

    router    = StrategyRouter(config)
    order_mgr = SimOrderManager(
        max_positions=config.get("trading", {}).get("max_positions", 5)
    )
    hist_det  = HistoricalRegimeDetector(kospi_df) if kospi_df is not None else None

    aggs = {1: CandleAggregator(1), 5: CandleAggregator(5), 15: CandleAggregator(15)}

    _REGIME_REFRESH_SEC = 30 * 60  # 실전과 동일하게 30분마다 재계산

    mutable_ctx      = dict(context)
    prev_p           = [0.0]
    last_day         = [None]
    regime_cache     = [None]
    regime_updated   = [None]  # 마지막 regime 계산 시각
    daily_entries    = [{}]    # {strategy_name: count}

    ordered = df_1m.sort_values("datetime").reset_index(drop=True)

    for _, row in ordered.iterrows():
        dt    = pd.Timestamp(row["datetime"])
        now   = dt.to_pydatetime()
        price = float(row.get("close") or 0)
        if price <= 0:
            continue

        row_date = dt.date()
        order_mgr.set_time(dt.strftime("%Y-%m-%d %H:%M"), row_date)

        # ── 날짜 변경 처리 ────────────────────────────────────────
        if row_date != last_day[0]:
            # 최대 보유일 초과 포지션 청산 (entry_date 기준)
            for code, pos in list(order_mgr.get_open_positions().items()):
                if pos.entry_date is not None:
                    days_held = (row_date - pos.entry_date).days
                    max_days  = _MAX_HOLD_DAYS.get(pos.strategy, 1)
                    if days_held > max_days:
                        order_mgr.close_position(
                            code, prev_p[0] or pos.entry_price,
                            reason=f"최대 보유일({max_days}일) 초과 청산",
                        )

            last_day[0]    = row_date
            daily_entries[0] = {}

            # gap_open / prev_close 동적 갱신
            if prev_p[0] > 0:
                mutable_ctx["prev_close"] = prev_p[0]
                mutable_ctx["gap_open"]   = price

            # 날짜 변경 시 regime 강제 초기화
            regime_cache[0]   = None
            regime_updated[0] = None

        prev_p[0] = price

        # ── MarketRegime 30분마다 재계산 (실전과 동일) ────────────
        upd = regime_updated[0]
        if upd is None or (now - upd).total_seconds() >= _REGIME_REFRESH_SEC:
            if hist_det:
                try:
                    regime_cache[0] = hist_det.detect_for(row_date, now)
                except Exception as e:
                    logger.warning("장세 계산 실패: %s", e)
                    regime_cache[0] = _default_regime(now)
            else:
                regime_cache[0] = _default_regime(now)
            regime_updated[0] = now

        regime = regime_cache[0]

        # ── 마감 강제 청산 (15:20 이후, Gap만 당일 청산) ─────────────
        is_closing = (now.hour > _CLOSE_HOUR or
                      (now.hour == _CLOSE_HOUR and now.minute >= _CLOSE_MIN))
        if is_closing:
            for code, pos in list(order_mgr.get_open_positions().items()):
                if pos.strategy in _INTRADAY_ONLY:
                    order_mgr.close_position(code, price, reason="장마감 강제 청산 (갭전략)")
            continue  # 마감 후 신규 진입 없음

        # ── 봉 집계 ───────────────────────────────────────────────
        tick = {
            "code":  stock_code,
            "time":  dt.strftime("%H%M%S"),
            "price": price,
            "vol":   float(row.get("volume") or 0),
            "open":  float(row.get("open")   or price),
            "high":  float(row.get("high")   or price),
            "low":   float(row.get("low")    or price),
        }
        for agg in aggs.values():
            agg.update(tick)
        dfs = {m: agg.get_df() for m, agg in aggs.items()}

        positions = order_mgr.get_open_positions()

        # ── 진입 판단 ─────────────────────────────────────────────
        if stock_code not in positions:
            if regime and regime.tradeable:
                should_enter, strat_name, _ = router.check_entry(
                    regime, dfs, tick, mutable_ctx,
                    entry_counts=daily_entries[0],
                    entry_limits=_LIVE_ENTRY_LIMITS,
                    now=dt,
                )
                if should_enter:
                    order_mgr.open_position(
                        stock_code, name, exchange, price, strategy=strat_name
                    )
                    daily_entries[0][strat_name] = daily_entries[0].get(strat_name, 0) + 1

        # ── 청산 판단 ─────────────────────────────────────────────
        else:
            pos = positions[stock_code]
            position_dict = {"entry_price": pos.entry_price, "strategy": pos.strategy}
            should_exit, reason = router.check_exit(dfs, tick, position_dict)
            if should_exit:
                order_mgr.close_position(stock_code, price, reason=reason)
                if pos.strategy == "breakout":
                    mutable_ctx["resistance"] = max(mutable_ctx.get("resistance", 0), price)

    # 기간 종료 — 열린 포지션 강제 청산
    last_price = float(ordered.iloc[-1]["close"]) if len(ordered) > 0 else 0.0
    for code in list(order_mgr.get_open_positions()):
        order_mgr.close_position(code, last_price, reason="기간 종료 강제 청산")

    trades = order_mgr.trades
    if not trades:
        return BacktestResult(stock_code=stock_code)

    returns    = [t.pnl_pct for t in trades]
    wins       = [r for r in returns if r > 0]
    losses     = [r for r in returns if r <= 0]
    capital    = 100.0
    equity     = [capital]
    for r in returns:
        capital *= 1 + r / 100
        equity.append(capital)

    return BacktestResult(
        stock_code=stock_code,
        total_return_pct=round(capital - 100.0, 2),
        win_rate_pct=round(len(wins) / len(trades) * 100, 1),
        max_drawdown_pct=round(_calc_mdd(equity), 2),
        sharpe_ratio=round(_calc_sharpe(returns), 2),
        total_trades=len(trades),
        winning_trades=len(wins),
        avg_profit_pct=round(np.mean(wins), 2) if wins else 0.0,
        avg_loss_pct=round(np.mean(losses), 2) if losses else 0.0,
        signal_score=0.0,
        trades=trades,
    )


def _default_regime(now: datetime) -> MarketRegime:
    """KOSPI 데이터 없을 때 사용하는 기본 MarketRegime.
    세션별로 적합한 전략만 포함 (gap은 개장 구간에만)."""
    session = MarketRegimeDetector._classify_session(now)
    tradeable, reason = MarketRegimeDetector._is_tradeable(
        session, MarketVolatility.NORMAL, MarketTrend.UP
    )
    # UP 트렌드 기준으로 전략 결정 (데이터 없으므로 보수적 가정)
    strategies = MarketRegimeDetector._preferred_strategies(
        session, MarketTrend.UP, MarketVolatility.NORMAL
    )
    return MarketRegime(
        trend=MarketTrend.UP, trend_strength=50.0,
        volatility=MarketVolatility.NORMAL, session=session,
        index_change_pct=0.0, preferred_strategies=strategies,
        tradeable=tradeable, reason="KOSPI 데이터 없음 — 기본 전략 적용",
    )
