import logging
import pandas as pd
from analysis.indicators import calculate_indicators
from kis.constants import TradeSignal

logger = logging.getLogger(__name__)


class DayTradingStrategy:
    """15분봉 기반 데이트레이딩 전략."""

    def __init__(self, config: dict):
        self.config = config
        self._minute_buffers: dict[str, list[dict]] = {}

    def on_tick(self, stock_code: str, tick: dict) -> TradeSignal | None:
        """
        실시간 틱 수신 시 호출.
        tick: parse_domestic_price() / parse_overseas_price() 반환값
        """
        self._update_buffer(stock_code, tick)
        df = self._build_candle_df(stock_code, interval_minutes=15)
        if df is None or len(df) < 30:
            return None

        df = calculate_indicators(df)
        return self._evaluate(df)

    def _evaluate(self, df: pd.DataFrame) -> TradeSignal | None:
        if df.empty:
            return None
        row  = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else None

        if self._check_buy(row, prev, df):
            return TradeSignal.BUY
        if self._check_sell(row, prev):
            return TradeSignal.SELL
        return None

    def _check_buy(self, row, prev, df) -> bool:
        cols = ["ema5", "ema20", "macd_hist", "rsi", "vol_ratio"]
        if any(col not in df.columns or pd.isna(row.get(col)) for col in cols):
            return False

        ema_up       = row["ema5"] > row["ema20"]
        macd_positive = row["macd_hist"] > 0
        rsi_ok       = 45 <= row["rsi"] <= 70
        vol_ok       = row["vol_ratio"] >= 1.3

        if prev is not None and pd.notna(prev.get("macd_hist")):
            macd_cross = prev["macd_hist"] < 0 < row["macd_hist"]
            return ema_up and macd_cross and rsi_ok and vol_ok

        return ema_up and macd_positive and rsi_ok and vol_ok

    def _check_sell(self, row, prev) -> bool:
        if any(pd.isna(row.get(col)) for col in ["ema5", "ema20", "rsi"]):
            return False

        if prev is not None and pd.notna(prev.get("macd_hist")) and pd.notna(row.get("macd_hist")):
            if prev["macd_hist"] > 0 > row["macd_hist"]:
                return True

        return row["ema5"] < row["ema20"] or row["rsi"] > 75

    def _update_buffer(self, stock_code: str, tick: dict):
        buf = self._minute_buffers.setdefault(stock_code, [])
        buf.append(tick)
        if len(buf) > 2000:
            self._minute_buffers[stock_code] = buf[-2000:]

    def _build_candle_df(self, stock_code: str, interval_minutes: int) -> pd.DataFrame | None:
        ticks = self._minute_buffers.get(stock_code, [])
        if not ticks:
            return None

        records = []
        for t in ticks:
            try:
                records.append({
                    "time":   t.get("time", ""),
                    "price":  float(t.get("price", 0) or 0),
                    "volume": float(t.get("vol", 0) or 0),
                })
            except (ValueError, TypeError):
                continue

        if not records:
            return None

        df = pd.DataFrame(records)
        df = df[df["price"] > 0]
        if df.empty:
            return None

        df["minute_bin"] = (df.index // (interval_minutes * 10)) * interval_minutes
        return df.groupby("minute_bin").agg(
            open=("price", "first"),
            high=("price", "max"),
            low=("price", "min"),
            close=("price", "last"),
            volume=("volume", "sum"),
        ).reset_index(drop=True)
