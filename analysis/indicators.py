import pandas as pd
import pandas_ta as ta


def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """OHLCV DataFrame에 기술적 지표를 추가하여 반환."""
    if df.empty:
        return df

    df = df.copy()

    close = df["close"].astype(float)

    # 이동평균은 실시간 차트/초반 전략 판단에서도 보여야 하므로 첫 봉부터 계산한다.
    df["ema5"] = close.ewm(span=5, adjust=False).mean()
    df["ema20"] = close.ewm(span=20, adjust=False).mean()
    df["ema60"] = close.ewm(span=60, adjust=False).mean()

    # MACD (12, 26, 9)
    if len(df) >= 26:
        macd = ta.macd(close, fast=12, slow=26, signal=9)
        if macd is not None:
            df["macd"] = macd.get("MACD_12_26_9")
            df["macd_signal"] = macd.get("MACDs_12_26_9")
            df["macd_hist"] = macd.get("MACDh_12_26_9")

    # RSI (14)
    if len(df) >= 14:
        df["rsi"] = ta.rsi(close, length=14)

    # Bollinger Band (20, 2)
    if len(df) >= 20:
        bb = ta.bbands(close, length=20, std=2)
        if bb is not None:
            df["bb_upper"] = bb.get("BBU_20_2.0")
            df["bb_mid"] = bb.get("BBM_20_2.0")
            df["bb_lower"] = bb.get("BBL_20_2.0")
            df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]

    # ATR (14)
    if len(df) >= 14:
        df["atr"] = ta.atr(df["high"], df["low"], close, length=14)

    # OBV
    df["obv"] = ta.obv(close, df["volume"])

    # 거래량 이동평균 및 비율
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma20"]

    # 전일 대비 등락률
    df["pct_change"] = df["close"].pct_change() * 100

    return df


def get_signal_score(df: pd.DataFrame) -> float:
    """마지막 행 기준으로 매수 신호 점수 계산 (0~100점)."""
    if df.empty:
        return 0.0

    row = df.iloc[-1]
    score = 0.0

    def _val(key):
        """None/NaN 안전하게 float로 변환."""
        v = row.get(key)
        if v is None or pd.isna(v):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # EMA 정배열 (5 > 20 > 60)
    e5, e20, e60 = _val("ema5"), _val("ema20"), _val("ema60")
    if e5 is not None and e20 is not None and e60 is not None:
        if e5 > e20 > e60:
            score += 25

    # MACD 골든크로스 (히스토그램 양전환)
    if "macd_hist" in df.columns and len(df) >= 2:
        prev_hist = df["macd_hist"].iloc[-2]
        curr_hist = _val("macd_hist")
        if curr_hist is not None and pd.notna(prev_hist):
            ph = float(prev_hist)
            if ph < 0 < curr_hist:
                score += 25
            elif curr_hist > 0:
                score += 10

    # RSI (50~70 상승세)
    rsi = _val("rsi")
    if rsi is not None:
        if 50 <= rsi <= 70:
            score += 20
        elif 40 <= rsi < 50:
            score += 10

    # 거래량 폭등
    vol = _val("vol_ratio")
    if vol is not None:
        if vol >= 2.0:
            score += 20
        elif vol >= 1.5:
            score += 10

    # 볼린저밴드 위치 (중심선 위)
    bb_mid = _val("bb_mid")
    close  = _val("close")
    if bb_mid is not None and close is not None:
        if close > bb_mid:
            score += 10

    return min(score, 100.0)
