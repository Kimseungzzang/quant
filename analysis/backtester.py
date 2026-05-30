import logging
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Callable
from .indicators import calculate_indicators, get_signal_score
from trading.strategies import BreakoutStrategy, PullbackStrategy, GapStrategy
from trading.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    strategy: str
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    pnl_pct: float
    exit_reason: str


@dataclass
class BacktestResult:
    stock_code: str
    total_return_pct: float = 0.0
    win_rate_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    avg_profit_pct: float = 0.0
    avg_loss_pct: float = 0.0
    signal_score: float = 0.0
    trades: list = field(default_factory=list)  # List[TradeRecord] — DB 저장 안 함


def resample_candles(df: pd.DataFrame, candle_minutes: int) -> pd.DataFrame:
    """1분봉 OHLCV를 N분봉 OHLCV로 집계."""
    if df.empty:
        return df.copy()
    if candle_minutes <= 1:
        result = df.copy()
        result["datetime"] = pd.to_datetime(result["datetime"])
        return result.sort_values("datetime").reset_index(drop=True)

    required = {"datetime", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"분봉 데이터 컬럼 누락: {', '.join(sorted(missing))}")

    work = df.copy()
    work["datetime"] = pd.to_datetime(work["datetime"])
    work = work.sort_values("datetime")
    work["bucket"] = work["datetime"].dt.floor(f"{candle_minutes}min")
    result = work.groupby("bucket", as_index=False).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).rename(columns={"bucket": "datetime"})
    return result.sort_values("datetime").reset_index(drop=True)


def run_strategy_backtest(
    stock_code: str,
    df_1m: pd.DataFrame,
    context: dict | None = None,
    stop_loss_pct: float = 5.0,
    take_profit_pct: float = 5.0,
    strategies: list[BaseStrategy] | None = None,
) -> BacktestResult:
    """
    새 전략 클래스 기반 분봉 백테스트.
    1분봉 원본에서 전략별 candle_minutes(1/5/15 등)를 리샘플링해 같은 원본 데이터로 검증한다.
    """
    if df_1m.empty:
        return BacktestResult(stock_code=stock_code)

    context = context or {}
    if strategies is None:
        strategies = [
            GapStrategy(stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct * 0.6),
            BreakoutStrategy(stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct * 0.8),
            PullbackStrategy(stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct),
        ]

    timeframes = sorted({strategy.candle_minutes for strategy in strategies})
    raw_dfs = {minutes: resample_candles(df_1m, minutes) for minutes in timeframes}
    dfs = {
        minutes: calculate_indicators(df) if not df.empty and len(df) >= 5 else df
        for minutes, df in raw_dfs.items()
    }

    signal_base = dfs.get(15)
    if signal_base is None or signal_base.empty:
        signal_base = next((df for df in dfs.values() if not df.empty), pd.DataFrame())
    signal_score = get_signal_score(signal_base) if not signal_base.empty else 0.0

    if len(df_1m) < 20:
        return BacktestResult(stock_code=stock_code, signal_score=round(signal_score, 1))

    # 날짜별 context 동적 계산을 위해 1분봉 → 일봉 집계
    _ordered = df_1m.sort_values("datetime").copy()
    _ordered["_date"] = _ordered["datetime"].dt.date
    _daily_agg = (
        _ordered.groupby("_date", sort=True)
        .agg(open=("open", "first"), high=("high", "max"),
             low=("low", "min"), close=("close", "last"))
        .reset_index()
    )
    _daily_dates = list(_daily_agg["_date"])

    def _build_daily_context(cur_date) -> dict:
        """해당 날짜의 gap_open, prev_close, resistance를 1분봉 일봉 집계에서 계산."""
        idx = _daily_dates.index(cur_date) if cur_date in _daily_dates else -1
        if idx < 1:
            return dict(context)  # 첫날이면 기존 context 유지
        today_row = _daily_agg.iloc[idx]
        prev_row  = _daily_agg.iloc[idx - 1]
        recent    = _daily_agg.iloc[max(0, idx - 19): idx + 1]
        return {
            "gap_open":   float(today_row["open"]),
            "prev_close": float(prev_row["close"]),
            "resistance": float(recent["high"].max()),
            "regime_fit": True,
        }

    # 전략별 일일 진입 한도 (과매매 방지)
    _ENTRY_LIMITS = {"gap": 1, "breakout": 2, "pullback": 3}

    capital = 100.0
    position: dict | None = None
    trade_returns: list[float] = []
    equity_curve = [capital]
    collected_trades: list[TradeRecord] = []
    _current_date  = None
    _day_context   = dict(context)
    _daily_entries: dict[str, int] = {}   # 날짜 리셋되는 전략별 진입 횟수

    def _fmt_dt(dt) -> str:
        try:
            return pd.Timestamp(dt).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return str(dt)

    ordered_1m = _ordered.reset_index(drop=True)
    for _, row in ordered_1m.iterrows():
        current_dt = pd.Timestamp(row["datetime"])
        price = _f(row, "close") or 0.0
        if price <= 0:
            continue

        # 날짜가 바뀌면 context + 진입 카운터 리셋
        row_date = current_dt.date()
        if row_date != _current_date:
            _current_date  = row_date
            _day_context   = _build_daily_context(row_date)
            _daily_entries = {}

        tick = {
            "code": stock_code,
            "price": price,
            "vol": _f(row, "volume") or 0.0,
            "time": current_dt,
        }

        if position is None:
            for strategy in strategies:
                # 허용 시간대 체크
                if not strategy.is_active_at(current_dt):
                    continue
                # 일일 진입 한도 체크
                if _daily_entries.get(strategy.name, 0) >= _ENTRY_LIMITS.get(strategy.name, 999):
                    continue
                df = _slice_until(dfs[strategy.candle_minutes], current_dt)
                if df.empty or len(df) < 2:
                    continue
                signal = strategy.check_entry(df, tick, _day_context)
                if signal.should_enter:
                    position = {
                        "entry_price": price,
                        "entry_time": current_dt,
                        "strategy": strategy.name,
                    }
                    _daily_entries[strategy.name] = _daily_entries.get(strategy.name, 0) + 1
                    break
        else:
            strategy = next((s for s in strategies if s.name == position.get("strategy")), None)
            entry = float(position.get("entry_price", 0) or 0)
            if strategy is None or entry <= 0:
                position = None
                continue

            df = _slice_until(dfs[strategy.candle_minutes], current_dt)
            signal = strategy.check_exit(df, tick, position)
            pnl = (price - entry) / entry * 100
            if signal.should_exit:
                trade_returns.append(pnl)
                capital *= 1 + pnl / 100
                equity_curve.append(capital)
                collected_trades.append(TradeRecord(
                    strategy=position["strategy"],
                    entry_time=_fmt_dt(position["entry_time"]),
                    entry_price=round(entry, 2),
                    exit_time=_fmt_dt(current_dt),
                    exit_price=round(price, 2),
                    pnl_pct=round(pnl, 2),
                    exit_reason=signal.reason,
                ))
                # Breakout 청산 후 저항선을 현재가로 갱신 → 즉시 재진입 방지
                if position["strategy"] == "breakout":
                    _day_context["resistance"] = max(
                        _day_context.get("resistance", 0), price
                    )
                position = None

    if position is not None:
        last = _f(ordered_1m.iloc[-1], "close") or position["entry_price"]
        last_dt = pd.Timestamp(ordered_1m.iloc[-1]["datetime"])
        pnl = (last - position["entry_price"]) / position["entry_price"] * 100
        trade_returns.append(pnl)
        capital *= 1 + pnl / 100
        equity_curve.append(capital)
        collected_trades.append(TradeRecord(
            strategy=position["strategy"],
            entry_time=_fmt_dt(position["entry_time"]),
            entry_price=round(position["entry_price"], 2),
            exit_time=_fmt_dt(last_dt),
            exit_price=round(last, 2),
            pnl_pct=round(pnl, 2),
            exit_reason="기간 종료 (강제 청산)",
        ))

    if not trade_returns:
        return BacktestResult(stock_code=stock_code, signal_score=round(signal_score, 1))

    wins = [r for r in trade_returns if r > 0]
    losses = [r for r in trade_returns if r <= 0]

    return BacktestResult(
        stock_code=stock_code,
        total_return_pct=round(capital - 100.0, 2),
        win_rate_pct=round(len(wins) / len(trade_returns) * 100, 1),
        max_drawdown_pct=round(_calc_mdd(equity_curve), 2),
        sharpe_ratio=round(_calc_sharpe(trade_returns), 2),
        total_trades=len(trade_returns),
        winning_trades=len(wins),
        avg_profit_pct=round(np.mean(wins), 2) if wins else 0.0,
        avg_loss_pct=round(np.mean(losses), 2) if losses else 0.0,
        signal_score=round(signal_score, 1),
        trades=collected_trades,
    )


def run_backtest(
    stock_code: str,
    df: pd.DataFrame,
    stop_loss_pct: float = 5.0,
    take_profit_pct: float = 5.0,
    entry_fn: Callable | None = None,
    start_from=None,   # date: 이 날짜부터만 백테스트 루프 실행 (이전 데이터는 EMA 워밍업)
) -> BacktestResult:
    """
    단순 백테스트 엔진.
    entry_fn(df, i) -> bool: i번째 행에서 진입할지 여부.
    기본 진입 조건: EMA 정배열 + MACD 히스토그램 양전환
    start_from: 지정 시 해당 날짜 이후 행부터만 루프 실행 (지표는 전체로 계산)
    """
    df = calculate_indicators(df)
    if len(df) < 30:
        return BacktestResult(stock_code=stock_code)

    if entry_fn is None:
        entry_fn = _default_entry

    # 루프 시작 인덱스: EMA60 워밍업(60행) vs start_from 날짜 중 큰 값
    loop_start = 60
    if start_from is not None and "date" in df.columns:
        ts = pd.Timestamp(start_from)
        mask = df["date"] >= ts
        if mask.any():
            loop_start = max(loop_start, int(mask.idxmax()))

    capital = 100.0
    position = None
    trade_returns = []
    equity_curve = [capital]

    for i in range(loop_start, len(df)):
        row = df.iloc[i]
        price = row["close"]

        if position is None:
            if entry_fn(df, i):
                position = {"entry_price": price, "entry_idx": i}
        else:
            entry_price = position["entry_price"]
            pnl_pct = (price - entry_price) / entry_price * 100

            exit_signal = (
                pnl_pct <= -stop_loss_pct
                or pnl_pct >= take_profit_pct
                or _default_exit(df, i)
            )
            if exit_signal:
                trade_returns.append(pnl_pct)
                capital *= 1 + pnl_pct / 100
                equity_curve.append(capital)
                position = None

    # 기간 종료 시 열린 포지션 강제 청산 (mark-to-market)
    if position is not None:
        last_price = df.iloc[-1]["close"]
        pnl_pct = (last_price - position["entry_price"]) / position["entry_price"] * 100
        trade_returns.append(pnl_pct)
        capital *= 1 + pnl_pct / 100
        equity_curve.append(capital)

    signal_score = get_signal_score(df)

    if not trade_returns:
        return BacktestResult(stock_code=stock_code, signal_score=round(signal_score, 1))

    wins = [r for r in trade_returns if r > 0]
    losses = [r for r in trade_returns if r <= 0]

    mdd = _calc_mdd(equity_curve)
    sharpe = _calc_sharpe(trade_returns)

    return BacktestResult(
        stock_code=stock_code,
        total_return_pct=round(capital - 100.0, 2),
        win_rate_pct=round(len(wins) / len(trade_returns) * 100, 1),
        max_drawdown_pct=round(mdd, 2),
        sharpe_ratio=round(sharpe, 2),
        total_trades=len(trade_returns),
        winning_trades=len(wins),
        avg_profit_pct=round(np.mean(wins), 2) if wins else 0.0,
        avg_loss_pct=round(np.mean(losses), 2) if losses else 0.0,
        signal_score=round(signal_score, 1),
    )


def run_minute_backtest(
    stock_code: str,
    df: pd.DataFrame,
    stop_loss_pct: float = 5.0,
    take_profit_pct: float = 5.0,
) -> BacktestResult:
    """
    분봉 기반 백테스트. strategy.py의 진입/청산 조건과 완전히 동일.

    진입: EMA5>EMA20 + MACD hist 음→양 전환(or 양수) + RSI 45~70 + vol_ratio 1.3배
    청산: 손절(-stop%) / 익절(+take%) / MACD 사망크로스 / EMA5<EMA20 / RSI>75
    """
    df = calculate_indicators(df)
    signal_score = get_signal_score(df)

    if len(df) < 60:
        return BacktestResult(stock_code=stock_code, signal_score=round(signal_score, 1))

    capital = 100.0
    position = None
    trade_returns: list[float] = []
    equity_curve = [capital]

    for i in range(60, len(df)):
        row  = df.iloc[i]
        prev = df.iloc[i - 1]
        price = _f(row, "close") or 0

        if position is None:
            if _minute_entry(row, prev):
                position = {"entry": price, "idx": i}
        else:
            entry = position["entry"]
            if entry <= 0:
                position = None
                continue
            pnl = (price - entry) / entry * 100

            exit_hit = (
                pnl <= -stop_loss_pct
                or pnl >= take_profit_pct
                or _minute_exit(row, prev)
            )
            if exit_hit:
                trade_returns.append(pnl)
                capital *= 1 + pnl / 100
                equity_curve.append(capital)
                position = None

    # 기간 종료 시 열린 포지션 mark-to-market
    if position is not None:
        last = _f(df.iloc[-1], "close") or position["entry"]
        pnl = (last - position["entry"]) / position["entry"] * 100
        trade_returns.append(pnl)
        capital *= 1 + pnl / 100
        equity_curve.append(capital)

    if not trade_returns:
        return BacktestResult(stock_code=stock_code, signal_score=round(signal_score, 1))

    wins   = [r for r in trade_returns if r > 0]
    losses = [r for r in trade_returns if r <= 0]

    return BacktestResult(
        stock_code=stock_code,
        total_return_pct=round(capital - 100.0, 2),
        win_rate_pct=round(len(wins) / len(trade_returns) * 100, 1),
        max_drawdown_pct=round(_calc_mdd(equity_curve), 2),
        sharpe_ratio=round(_calc_sharpe(trade_returns), 2),
        total_trades=len(trade_returns),
        winning_trades=len(wins),
        avg_profit_pct=round(np.mean(wins), 2)   if wins   else 0.0,
        avg_loss_pct=round(np.mean(losses), 2)   if losses else 0.0,
        signal_score=round(signal_score, 1),
    )


def _f(row, key) -> float | None:
    """None/NaN 안전 float 변환."""
    v = row.get(key) if hasattr(row, "get") else row[key]
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _slice_until(df: pd.DataFrame, current_dt: pd.Timestamp) -> pd.DataFrame:
    if df.empty or "datetime" not in df.columns:
        return df
    idx = df["datetime"].searchsorted(current_dt, side="right")
    return df.iloc[:idx]


def _minute_entry(row, prev) -> bool:
    """strategy.py _check_buy 와 동일한 진입 조건."""
    e5  = _f(row,  "ema5")
    e20 = _f(row,  "ema20")
    mh  = _f(row,  "macd_hist")
    pmh = _f(prev, "macd_hist")
    rsi = _f(row,  "rsi")
    vol = _f(row,  "vol_ratio") or 0.0

    if any(v is None for v in [e5, e20, mh, rsi]):
        return False

    ema_up  = e5 > e20
    rsi_ok  = 45 <= rsi <= 70
    vol_ok  = vol >= 1.3

    if pmh is not None:
        macd_cross = pmh < 0 < mh
        return ema_up and macd_cross and rsi_ok and vol_ok

    return ema_up and mh > 0 and rsi_ok and vol_ok


def _minute_exit(row, prev) -> bool:
    """strategy.py _check_sell 와 동일한 청산 조건."""
    e5  = _f(row,  "ema5")
    e20 = _f(row,  "ema20")
    rsi = _f(row,  "rsi")
    mh  = _f(row,  "macd_hist")
    pmh = _f(prev, "macd_hist")

    if e5 is None or e20 is None:
        return False

    if pmh is not None and mh is not None and pmh > 0 > mh:
        return True  # MACD 사망크로스

    return e5 < e20 or (rsi is not None and rsi > 75)


def _default_entry(df: pd.DataFrame, i: int) -> bool:
    row  = df.iloc[i]
    prev = df.iloc[i - 1]

    def _f(r, key):
        v = r.get(key) if hasattr(r, "get") else r[key]
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    e5, e20, e60 = _f(row, "ema5"), _f(row, "ema20"), _f(row, "ema60")
    mh_cur  = _f(row,  "macd_hist")
    mh_prev = _f(prev, "macd_hist")
    vol     = _f(row,  "vol_ratio") or 0.0

    if any(v is None for v in [e5, e20, e60, mh_cur, mh_prev]):
        return False

    ema_aligned   = e5 > e20 > e60
    macd_cross_up = mh_prev < 0 < mh_cur
    vol_surge     = vol >= 1.5

    return ema_aligned and macd_cross_up and vol_surge


def swing_entry(df: pd.DataFrame, i: int) -> bool:
    """스윙용 일봉 진입 조건: 추세 눌림 재상승 또는 20일 고점 돌파."""
    if i < 20:
        return False

    row = df.iloc[i]
    prev = df.iloc[i - 1]

    e5 = _f(row, "ema5")
    e20 = _f(row, "ema20")
    e60 = _f(row, "ema60")
    close = _f(row, "close")
    prev_close = _f(prev, "close")
    mh_cur = _f(row, "macd_hist")
    mh_prev = _f(prev, "macd_hist")
    rsi = _f(row, "rsi")
    vol = _f(row, "vol_ratio") or 0.0
    bb_mid = _f(row, "bb_mid")

    if any(v is None for v in [e5, e20, e60, close, prev_close, mh_cur, mh_prev, rsi]):
        return False

    trend_ok = e5 > e20 and close > e20 and e20 >= e60 * 0.98
    rsi_ok = 42 <= rsi <= 78
    macd_improving = mh_cur > mh_prev or mh_cur > 0
    volume_ok = vol >= 0.8

    pullback_rebound = (
        trend_ok
        and rsi_ok
        and macd_improving
        and volume_ok
        and (bb_mid is None or close >= bb_mid)
        and close > prev_close
    )

    recent_high = 0.0
    if "high" in df.columns:
        recent_high = float(df["high"].iloc[i - 20:i].max())
    breakout = (
        trend_ok
        and rsi <= 85
        and vol >= 1.1
        and close > recent_high
        and mh_cur >= mh_prev
    )

    return pullback_rebound or breakout


def _default_exit(df: pd.DataFrame, i: int) -> bool:
    row = df.iloc[i]
    if "ema5" not in df.columns or "ema20" not in df.columns:
        return False
    if pd.isna(row["ema5"]) or pd.isna(row["ema20"]):
        return False
    return row["ema5"] < row["ema20"]


def _calc_mdd(equity: list[float]) -> float:
    peak = equity[0]
    mdd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > mdd:
            mdd = dd
    return mdd


def _calc_sharpe(returns: list[float], risk_free: float = 0.0) -> float:
    if len(returns) < 2:
        return 0.0
    arr = np.array(returns)
    excess = arr - risk_free
    std = np.std(excess, ddof=1)
    if std == 0:
        return 0.0
    return float(np.mean(excess) / std * np.sqrt(252))
