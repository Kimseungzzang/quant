"""
1개 종목 단타 트리거 스캐너 — REST 폴링 기반, 결정론적 규칙(AI 미개입).

전략 규칙:
    1. 방향  — 1시간봉 추세 (MA20 + 기울기)
    2. 자리  — 15분봉 지지/저항 (스윙 하이/로우)
    3. 트리거 — 5분봉 반등/돌파 + 거래량 확인
    4. 진입  — 자리에서 트리거 확인 시 알림
    5. 리스크 — 15분봉 지지/저항 바깥
    6. 목표  — 반대편 15분봉 지지/저항, 없으면 1:2 R/R

주문/포지션 관리는 하지 않는다 — 가격 트리거 알림만 콘솔+텔레그램으로 보낸다.
텔레그램 명령:
    /long    — 지금 롱 진입이 적합한지 판정(적합 시 진입/손절/목표가)
    /short   — 지금 숏 진입이 적합한지 판정(적합 시 진입/손절/목표가)
    /change  — 감시 종목 변경 (이후 보내는 메시지를 새 종목코드로 인식)

사용법:
    python scalp_scanner.py <SYMBOL> [--market domestic|overseas] [--interval 15]
"""
import argparse
import asyncio
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import redis
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from utils import load_config
from toss.auth import TossAuth
from toss.rest import TossRestClient
from toss.api import TossBrokerAPI
from toss.domestic import TossDomesticAPI
from toss.overseas import TossOverseasAPI

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("scalp_scanner")

STOP_LOSS_PCT = 0.01
TAKE_PROFIT_PCT = 0.02
COOLDOWN_SEC = 10 * 60
PREPARE_COOLDOWN_SEC = 10 * 60
SR_WINDOW = 3
ZONE_PROXIMITY_PCT = 0.003  # 자리 근접 판정 범위(0.3%)
PREPARE_ZONE_PROXIMITY_PCT = 0.008  # 진입 자리 예고 범위(0.8%)
EXIT_BUFFER_PCT = 0.002  # 지지/저항 바로 앞 노이즈를 피하기 위한 0.2% 버퍼
VOLUME_SPIKE_MULT = 1.2
MIN_ZONE_SCORE = 3
CANDLE_REFRESH_SEC = 60
TICK_DB_PATH = Path("logs/scalp_ticks.sqlite")
MIN_CONFIRM_SCORE = 2
TREND_LOOKBACK_DAYS = 14
ZONE_LOOKBACK_DAYS = 7
TRIGGER_LOOKBACK_DAYS = 7
MOMENTUM_VOLUME_MULT = 1.1
MOMENTUM_MAX_VWAP_DISTANCE_PCT = 0.02
MOMENTUM_MIN_RR = 1.5
MIN_STOP_PCT = 0.005  # 수수료/슬리피지에 쉽게 잠식되지 않도록 최소 손절폭 0.5%
MIN_TARGET_PCT = 0.01  # 최소 익절폭 1.0%; 구조/ATR 목표가가 우선
ATR_TARGET_MULT = 2.0


# ── 판정 로직 (순수 함수) ────────────────────────────────────────────────

def _extract_tick_volume(price_data: dict) -> float | None:
    output = price_data.get("output") or price_data
    for key in (
        "accumulatedVolume",
        "accTradeVolume",
        "accTradeQuantity",
        "tradeVolume",
        "volume",
        "acml_volume",
    ):
        value = output.get(key)
        if value is None:
            continue
        try:
            return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            continue
    return None


def format_price(value: float | None, market: str) -> str:
    if value is None:
        return "-"
    if market == "overseas":
        return f"{float(value):,.2f}"
    return f"{float(value):,.0f}"


class TickStore:
    """스캐너가 직접 관측한 REST 가격 틱을 누적하고 최신 캔들로 합성한다."""

    def __init__(self, path: Path = TICK_DB_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scalp_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL DEFAULT '',
                symbol TEXT NOT NULL,
                market TEXT NOT NULL,
                ts TEXT NOT NULL,
                price REAL NOT NULL,
                volume REAL,
                raw_json TEXT
            )
            """
        )
        cols = [row[1] for row in self._conn.execute("PRAGMA table_info(scalp_ticks)").fetchall()]
        if "chat_id" not in cols:
            self._conn.execute("ALTER TABLE scalp_ticks ADD COLUMN chat_id TEXT NOT NULL DEFAULT ''")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scalp_ticks_user_symbol_ts ON scalp_ticks(chat_id, symbol, market, ts)"
        )
        self._conn.commit()

    def record(self, chat_id: str, symbol: str, market: str, price_data: dict) -> float:
        price = float(price_data.get("current_price") or price_data.get("price") or price_data.get("last") or 0)
        if price <= 0:
            return 0.0
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO scalp_ticks (chat_id, symbol, market, ts, price, volume, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(chat_id),
                    symbol.upper(),
                    market,
                    datetime.now().isoformat(timespec="seconds"),
                    price,
                    _extract_tick_volume(price_data),
                    json.dumps(price_data.get("output") or {}, ensure_ascii=False, default=str),
                ),
            )
            self._conn.commit()
        return price

    def tick_count(self, chat_id: str, symbol: str, market: str, lookback_days: int = 1) -> int:
        since = (datetime.now() - timedelta(days=lookback_days)).isoformat(timespec="seconds")
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*)
                FROM scalp_ticks
                WHERE chat_id = ? AND symbol = ? AND market = ? AND ts >= ?
                """,
                (str(chat_id), symbol.upper(), market, since),
            ).fetchone()
        return int(row[0] if row else 0)

    def merge_with_ticks(self, df: pd.DataFrame, chat_id: str, symbol: str, market: str, candle_minutes: int, lookback_days: int) -> pd.DataFrame:
        ticks = self._load_ticks(chat_id, symbol, market, lookback_days)
        tick_candles = self._ticks_to_candles(ticks, candle_minutes)
        if tick_candles.empty:
            return df
        if df.empty:
            return tick_candles
        combined = pd.concat([df, tick_candles]).drop_duplicates("datetime", keep="last")
        return combined.sort_values("datetime").reset_index(drop=True)

    def _load_ticks(self, chat_id: str, symbol: str, market: str, lookback_days: int) -> pd.DataFrame:
        since = (datetime.now() - timedelta(days=lookback_days)).isoformat(timespec="seconds")
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT ts, price, volume
                FROM scalp_ticks
                WHERE chat_id = ? AND symbol = ? AND market = ? AND ts >= ?
                ORDER BY ts
                """,
                (str(chat_id), symbol.upper(), market, since),
            ).fetchall()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=["datetime", "price", "volume"])
        df["datetime"] = pd.to_datetime(df["datetime"])
        df["price"] = pd.to_numeric(df["price"], errors="coerce")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
        return df.dropna(subset=["price"])

    @staticmethod
    def _ticks_to_candles(ticks: pd.DataFrame, candle_minutes: int) -> pd.DataFrame:
        if ticks.empty:
            return pd.DataFrame()
        ticks = ticks.copy()
        ticks["bucket"] = ticks["datetime"].dt.floor(f"{candle_minutes}min")

        def _volume(series: pd.Series) -> float:
            clean = series.dropna()
            if len(clean) >= 2:
                return max(float(clean.iloc[-1] - clean.iloc[0]), 0.0)
            return 0.0

        candles = ticks.groupby("bucket").agg(
            open=("price", "first"),
            high=("price", "max"),
            low=("price", "min"),
            close=("price", "last"),
            volume=("volume", _volume),
        ).reset_index().rename(columns={"bucket": "datetime"})
        return candles.sort_values("datetime").reset_index(drop=True)

def detect_trend_1h(df: pd.DataFrame) -> str:
    """MA20(종가) + 기울기로 1시간봉 추세 판정. 데이터 부족 시 NEUTRAL."""
    if len(df) < 21:
        return "NEUTRAL"
    ma = df["close"].rolling(20).mean()
    last_close = float(df["close"].iloc[-1])
    ma_now = ma.iloc[-1]
    ma_prev = ma.iloc[-6] if len(ma) > 25 else ma.iloc[-2]
    if pd.isna(ma_now) or pd.isna(ma_prev):
        return "NEUTRAL"
    ma_now, ma_prev = float(ma_now), float(ma_prev)
    if last_close > ma_now and ma_now > ma_prev:
        return "UP"
    if last_close < ma_now and ma_now < ma_prev:
        return "DOWN"
    return "NEUTRAL"


def find_sr_zones(df: pd.DataFrame, current_price: float, window: int = SR_WINDOW) -> tuple[float | None, float | None]:
    """15분봉 스윙 하이/로우 중 현재가에 가장 가까운 (저항, 지지) 반환."""
    if len(df) < window * 2 + 1:
        return None, None
    highs, lows = df["high"], df["low"]
    swing_highs, swing_lows = [], []
    for i in range(window, len(df) - window):
        h_window = highs.iloc[i - window: i + window + 1]
        l_window = lows.iloc[i - window: i + window + 1]
        if highs.iloc[i] == h_window.max():
            swing_highs.append(float(highs.iloc[i]))
        if lows.iloc[i] == l_window.min():
            swing_lows.append(float(lows.iloc[i]))

    resistance_candidates = [h for h in swing_highs if h > current_price]
    support_candidates = [l for l in swing_lows if l < current_price]
    resistance = min(resistance_candidates) if resistance_candidates else None
    support = max(support_candidates) if support_candidates else None
    return resistance, support


def rate_zone_strength(df: pd.DataFrame, zone_price: float | None, zone_type: str) -> dict:
    """터치 횟수/최근성/반응폭/거래량으로 15분봉 자리 강도를 약/보통/강으로 점수화."""
    if zone_price is None or df.empty:
        return {"score": 0, "label": "없음", "touches": 0, "reaction_pct": 0.0, "reason": "자리 없음"}

    tolerance = max(ZONE_PROXIMITY_PCT, (df["high"] - df["low"]).median() / zone_price)
    if zone_type == "support":
        touches = df[(df["low"] >= zone_price * (1 - tolerance)) & (df["low"] <= zone_price * (1 + tolerance))]
    else:
        touches = df[(df["high"] >= zone_price * (1 - tolerance)) & (df["high"] <= zone_price * (1 + tolerance))]

    if touches.empty:
        return {"score": 0, "label": "약", "touches": 0, "reaction_pct": 0.0, "reason": "터치 부족"}

    score = 0
    touch_count = len(touches)
    if touch_count >= 3:
        score += 2
    elif touch_count >= 2:
        score += 1

    last_touch_idx = int(touches.index[-1])
    bars_since_touch = len(df) - 1 - last_touch_idx
    if bars_since_touch <= 16:
        score += 1

    reactions = []
    for idx in touches.index[-5:]:
        pos = df.index.get_loc(idx)
        after = df.iloc[pos + 1: pos + 5]
        if after.empty:
            continue
        if zone_type == "support":
            reaction = (after["high"].max() - zone_price) / zone_price
        else:
            reaction = (zone_price - after["low"].min()) / zone_price
        reactions.append(max(float(reaction), 0.0))

    reaction_pct = max(reactions) * 100 if reactions else 0.0
    if reaction_pct >= 0.7:
        score += 2
    elif reaction_pct >= 0.3:
        score += 1

    avg_volume = df["volume"].mean()
    if avg_volume and touches["volume"].tail(3).mean() > avg_volume:
        score += 1

    label = "강" if score >= 5 else ("보통" if score >= MIN_ZONE_SCORE else "약")
    return {
        "score": score,
        "label": label,
        "touches": touch_count,
        "reaction_pct": round(reaction_pct, 2),
        "reason": f"터치 {touch_count}회, 최대반응 {reaction_pct:.2f}%",
    }


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    result = df.copy()
    typical = (result["high"] + result["low"] + result["close"]) / 3
    volume = result["volume"].fillna(0)
    cumulative_volume = volume.cumsum()
    result["vwap"] = (typical * volume).cumsum() / cumulative_volume.replace(0, pd.NA)

    delta = result["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, pd.NA)
    result["rsi"] = 100 - (100 / (1 + rs))

    prev_close = result["close"].shift(1)
    tr = pd.concat(
        [
            result["high"] - result["low"],
            (result["high"] - prev_close).abs(),
            (result["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    result["atr"] = tr.rolling(14).mean()
    return result


def score_confirmations(direction: str, entry: float, exits: dict, df_5m: pd.DataFrame) -> dict:
    if df_5m.empty:
        return {"score": 0, "items": ["5M 지표 데이터 없음"], "rsi": None, "vwap": None, "atr": None}

    ind = add_indicators(df_5m)
    last = ind.iloc[-1]
    score = 0
    items: list[str] = []

    vwap = last.get("vwap")
    if pd.notna(vwap):
        if direction == "UP" and entry >= float(vwap):
            score += 1
            items.append("VWAP 위")
        elif direction == "DOWN" and entry <= float(vwap):
            score += 1
            items.append("VWAP 아래")
        else:
            items.append("VWAP 역방향")

    rsi = last.get("rsi")
    if pd.notna(rsi):
        rsi_value = float(rsi)
        if direction == "UP" and rsi_value < 80:
            score += 1
            items.append(f"RSI 과열 아님({rsi_value:.1f})")
        elif direction == "DOWN" and rsi_value > 20:
            score += 1
            items.append(f"RSI 과매도 아님({rsi_value:.1f})")
        else:
            items.append(f"RSI 위험({rsi_value:.1f})")

    atr = last.get("atr")
    if pd.notna(atr) and atr > 0:
        risk = abs(entry - exits["sl"])
        atr_ratio = risk / float(atr)
        if 0.5 <= atr_ratio <= 2.5:
            score += 1
            items.append(f"손절폭 ATR 적정({atr_ratio:.1f}x)")
        else:
            items.append(f"손절폭 ATR 이탈({atr_ratio:.1f}x)")

    return {
        "score": score,
        "items": items or ["보조 확인 없음"],
        "rsi": None if pd.isna(rsi) else round(float(rsi), 2),
        "vwap": None if pd.isna(vwap) else float(vwap),
        "atr": None if pd.isna(atr) else float(atr),
    }


def check_5m_momentum_continuation(df: pd.DataFrame, direction: str, price: float) -> dict:
    """15M 자리 근처가 아니어도 강한 5M 추세 지속이면 제한적으로 추격 후보를 허용."""
    if len(df) < 30 or price <= 0:
        return {"ok": False, "reason": "5M 추세 지속 데이터 부족"}

    ind = add_indicators(df)
    ind["ema9"] = ind["close"].ewm(span=9, adjust=False).mean()
    ind["ema20"] = ind["close"].ewm(span=20, adjust=False).mean()
    last = ind.iloc[-1]
    prev = ind.iloc[-2]
    recent = ind.iloc[-7:-1]

    vwap = last.get("vwap")
    rsi = last.get("rsi")
    atr = last.get("atr")
    ema9 = last.get("ema9")
    ema20 = last.get("ema20")
    avg_volume = ind["volume"].iloc[-21:-1].mean()
    if pd.isna(rsi):
        recent_delta = ind["close"].diff().tail(15)
        gain = float(recent_delta.clip(lower=0).mean())
        loss = float((-recent_delta.clip(upper=0)).mean())
        if loss == 0 and gain > 0:
            rsi = 100.0
        elif gain == 0 and loss > 0:
            rsi = 0.0
    if any(pd.isna(v) for v in (vwap, rsi, atr, ema9, ema20)) or pd.isna(avg_volume) or avg_volume <= 0:
        return {"ok": False, "reason": "5M 추세 지속 지표 부족"}

    close = float(last["close"])
    open_ = float(last["open"])
    prev_close = float(prev["close"])
    vwap_value = float(vwap)
    rsi_value = float(rsi)
    atr_value = float(atr)
    ema9_value = float(ema9)
    ema20_value = float(ema20)
    volume_ok = float(last["volume"]) >= float(avg_volume) * MOMENTUM_VOLUME_MULT
    vwap_distance = abs(price - vwap_value) / price
    not_chasing = vwap_distance <= MOMENTUM_MAX_VWAP_DISTANCE_PCT

    if direction == "UP":
        ma_ok = ema9_value > ema20_value and close > ema9_value and price > vwap_value
        rsi_ok = 45 <= rsi_value <= 85
        breakout = close >= float(recent["high"].max()) and close > open_ and volume_ok
        pullback_hold = float(prev["low"]) <= ema9_value and close > prev_close and close > open_
        ok = bool(ma_ok and rsi_ok and not_chasing and (breakout or pullback_hold))
        trigger = "5M 고점 돌파" if breakout else "5M EMA9 눌림 유지"
        return {
            "ok": ok,
            "reason": trigger if ok else "5M 추세 지속 조건 미충족",
            "ema9": ema9_value,
            "ema20": ema20_value,
            "vwap": vwap_value,
            "rsi": rsi_value,
            "atr": atr_value,
            "items": [trigger, f"EMA9>EMA20", f"RSI {rsi_value:.1f}", f"VWAP 이격 {vwap_distance * 100:.2f}%"],
        }

    ma_ok = ema9_value < ema20_value and close < ema9_value and price < vwap_value
    rsi_ok = 15 <= rsi_value <= 55
    breakdown = close <= float(recent["low"].min()) and close < open_ and volume_ok
    pullback_hold = float(prev["high"]) >= ema9_value and close < prev_close and close < open_
    ok = bool(ma_ok and rsi_ok and not_chasing and (breakdown or pullback_hold))
    trigger = "5M 저점 이탈" if breakdown else "5M EMA9 되돌림 유지"
    return {
        "ok": ok,
        "reason": trigger if ok else "5M 추세 지속 조건 미충족",
        "ema9": ema9_value,
        "ema20": ema20_value,
        "vwap": vwap_value,
        "rsi": rsi_value,
        "atr": atr_value,
        "items": [trigger, f"EMA9<EMA20", f"RSI {rsi_value:.1f}", f"VWAP 이격 {vwap_distance * 100:.2f}%"],
    }


def check_5m_trigger(df: pd.DataFrame, zone_price: float, direction: str) -> bool:
    """최근 5분봉이 zone에서 반등(UP)/이탈-거부(DOWN)했는지 + 거래량 급증 확인."""
    if len(df) < 21:
        return False
    last = df.iloc[-1]
    prev = df.iloc[-2]
    avg_volume = df["volume"].iloc[-21:-1].mean()
    if pd.isna(avg_volume) or avg_volume <= 0:
        return False
    volume_ok = float(last["volume"]) > avg_volume * VOLUME_SPIKE_MULT

    if direction == "UP":
        bounced = last["low"] <= zone_price and last["close"] > zone_price and last["close"] > last["open"]
        broke_out = last["close"] > zone_price and prev["close"] <= zone_price
        return bool(volume_ok and (bounced or broke_out))
    else:
        rejected = last["high"] >= zone_price and last["close"] < zone_price and last["close"] < last["open"]
        broke_down = last["close"] < zone_price and prev["close"] >= zone_price
        return bool(volume_ok and (rejected or broke_down))


def calculate_exit_prices(direction: str, entry: float, support: float | None, resistance: float | None, zone: float) -> dict:
    """15분봉 지지/저항을 우선 사용하고, 반대편 자리가 없으면 1:2 R/R로 보수적 대체."""
    if direction == "UP":
        sl = (support or zone) * (1 - EXIT_BUFFER_PCT)
        sl = min(sl, entry * (1 - MIN_STOP_PCT))
        risk = max(entry - sl, entry * 0.001)
        buffered_resistance = resistance * (1 - EXIT_BUFFER_PCT) if resistance else None
        if buffered_resistance and buffered_resistance > entry + risk * MOMENTUM_MIN_RR:
            tp = buffered_resistance
            tp_basis = "15M 저항"
        else:
            tp = entry + risk * 2
            tp_basis = "15M 저항 없음 → 1:2 R/R"
        return {"sl": sl, "tp": tp, "sl_basis": "15M 지지 하단", "tp_basis": tp_basis}

    sl = (resistance or zone) * (1 + EXIT_BUFFER_PCT)
    sl = max(sl, entry * (1 + MIN_STOP_PCT))
    risk = max(sl - entry, entry * 0.001)
    buffered_support = support * (1 + EXIT_BUFFER_PCT) if support else None
    if buffered_support and buffered_support < entry - risk * MOMENTUM_MIN_RR:
        tp = buffered_support
        tp_basis = "15M 지지"
    else:
        tp = entry - risk * 2
        tp_basis = "15M 지지 없음 → 1:2 R/R"
    return {"sl": sl, "tp": tp, "sl_basis": "15M 저항 상단", "tp_basis": tp_basis}


def calculate_momentum_exit_prices(direction: str, entry: float, data: dict, momentum: dict) -> dict:
    """원웨이 추세 지속 진입은 5M ATR/EMA9와 남은 15M 반대 자리로 손익비를 잡는다."""
    atr = float(momentum.get("atr") or entry * 0.003)
    ema9 = float(momentum.get("ema9") or entry)
    support = data.get("support")
    resistance = data.get("resistance")

    if direction == "UP":
        atr_stop = entry - atr * 1.2
        ema_stop = ema9 * (1 - EXIT_BUFFER_PCT)
        sl = min(entry * (1 - MIN_STOP_PCT), max(atr_stop, ema_stop))
        risk = max(entry - sl, entry * 0.001)
        buffered_resistance = resistance * (1 - EXIT_BUFFER_PCT) if resistance else None
        if buffered_resistance and buffered_resistance > entry + risk * MOMENTUM_MIN_RR:
            tp = buffered_resistance
            tp_basis = "15M 저항"
        else:
            tp = entry + max(risk * MOMENTUM_MIN_RR, atr * ATR_TARGET_MULT, entry * MIN_TARGET_PCT)
            tp_basis = "ATR/최소 손익비 fallback"
        return {"sl": sl, "tp": tp, "sl_basis": "5M EMA9/ATR 이탈", "tp_basis": tp_basis}

    atr_stop = entry + atr * 1.2
    ema_stop = ema9 * (1 + EXIT_BUFFER_PCT)
    sl = max(entry * (1 + MIN_STOP_PCT), min(atr_stop, ema_stop))
    risk = max(sl - entry, entry * 0.001)
    buffered_support = support * (1 + EXIT_BUFFER_PCT) if support else None
    if buffered_support and buffered_support < entry - risk * MOMENTUM_MIN_RR:
        tp = min(buffered_support, entry - risk * MOMENTUM_MIN_RR)
        tp_basis = "15M 지지"
    else:
        tp = entry - max(risk * MOMENTUM_MIN_RR, atr * ATR_TARGET_MULT, entry * MIN_TARGET_PCT)
        tp_basis = "ATR/최소 손익비 fallback"
    return {"sl": sl, "tp": tp, "sl_basis": "5M EMA9/ATR 이탈", "tp_basis": tp_basis}


def scan_once(
    api,
    symbol: str,
    market: str = "domestic",
    tick_store: TickStore | None = None,
    chat_id: str = "",
) -> dict:
    """현재가 + 1H/15M/5M 데이터를 모아 판정에 필요한 재료를 반환."""
    price_data = api.get_price(symbol)
    if tick_store:
        price = tick_store.record(chat_id, symbol, market, price_data)
    else:
        price = float(price_data.get("current_price") or 0)
    df_1h = api.get_historical_minute_ohlcv(symbol, lookback_days=TREND_LOOKBACK_DAYS, candle_minutes=60)
    if tick_store:
        df_1h = tick_store.merge_with_ticks(
            df_1h, chat_id, symbol, market, candle_minutes=60, lookback_days=TREND_LOOKBACK_DAYS
        )
    trend = detect_trend_1h(df_1h)

    resistance = support = None
    resistance_strength = {"score": 0, "label": "없음", "touches": 0, "reaction_pct": 0.0, "reason": "자리 없음"}
    support_strength = {"score": 0, "label": "없음", "touches": 0, "reaction_pct": 0.0, "reason": "자리 없음"}
    df_5m = pd.DataFrame()
    if price > 0:
        df_15m = api.get_historical_minute_ohlcv(symbol, lookback_days=ZONE_LOOKBACK_DAYS, candle_minutes=15)
        if tick_store:
            df_15m = tick_store.merge_with_ticks(
                df_15m, chat_id, symbol, market, candle_minutes=15, lookback_days=ZONE_LOOKBACK_DAYS
            )
        resistance, support = find_sr_zones(df_15m, price)
        resistance_strength = rate_zone_strength(df_15m, resistance, "resistance")
        support_strength = rate_zone_strength(df_15m, support, "support")
        df_5m = api.get_historical_minute_ohlcv(symbol, lookback_days=TRIGGER_LOOKBACK_DAYS, candle_minutes=5)
        if tick_store:
            df_5m = tick_store.merge_with_ticks(
                df_5m, chat_id, symbol, market, candle_minutes=5, lookback_days=TRIGGER_LOOKBACK_DAYS
            )

    return {
        "price": price,
        "trend": trend,
        "resistance": resistance,
        "support": support,
        "resistance_strength": resistance_strength,
        "support_strength": support_strength,
        "df_5m": df_5m,
    }


def refresh_price(
    api,
    symbol: str,
    data: dict,
    market: str = "domestic",
    tick_store: TickStore | None = None,
    chat_id: str = "",
) -> dict:
    """캔들/자리 계산은 재사용하고 현재가만 가볍게 갱신."""
    price_data = api.get_price(symbol)
    refreshed = dict(data)
    refreshed["price"] = (
        tick_store.record(chat_id, symbol, market, price_data)
        if tick_store
        else float(price_data.get("current_price") or 0)
    )
    return refreshed


def judge_direction(direction: str, data: dict) -> dict:
    """direction("UP"=롱 요청/"DOWN"=숏 요청)이 지금 적합한지 판정.
    반환: {"fit": bool, "reason": str|None, "price": float, "zone": float|None,
           "entry": float, "sl": float, "tp": float}"""
    price = data["price"]
    if price <= 0:
        return {"fit": False, "reason": "현재가 조회 실패", "price": price, "zone": None}

    trend = data["trend"]
    if trend != direction:
        return {"fit": False, "reason": f"1H 추세 불일치 (현재 추세: {trend})", "price": price, "zone": None}

    def _momentum_result(fallback_reason: str, zone_value: float | None = None, zone_strength_value: dict | None = None) -> dict:
        momentum = check_5m_momentum_continuation(data["df_5m"], direction, price)
        if not momentum["ok"]:
            result = {
                "fit": False,
                "reason": f"{fallback_reason}; 추세 지속도 미충족 ({momentum['reason']})",
                "price": price,
                "zone": zone_value,
            }
            if zone_strength_value:
                result["zone_strength"] = zone_strength_value
            return result

        entry = price
        exits = calculate_momentum_exit_prices(direction, entry, data, momentum)
        confirmations = {
            "score": MIN_CONFIRM_SCORE,
            "items": momentum["items"],
            "rsi": round(float(momentum["rsi"]), 2),
            "vwap": float(momentum["vwap"]),
            "atr": float(momentum["atr"]),
        }
        return {
            "fit": True,
            "reason": None,
            "setup_type": "momentum",
            "setup_label": "추세 지속",
            "price": price,
            "zone": float(momentum["ema9"]),
            "zone_strength": {
                "score": MIN_ZONE_SCORE,
                "label": "추세",
                "touches": 0,
                "reaction_pct": 0.0,
                "reason": momentum["reason"],
            },
            "entry": entry,
            "confirmations": confirmations,
            **exits,
        }

    zone = data["support"] if direction == "UP" else data["resistance"]
    zone_strength = data["support_strength"] if direction == "UP" else data["resistance_strength"]
    if zone is None:
        return _momentum_result("15M 지지/저항 자리 없음")
    if zone_strength["score"] < MIN_ZONE_SCORE:
        return _momentum_result(f"15M 자리 강도 약함 ({zone_strength['reason']})", zone, zone_strength)

    near_zone = abs(price - zone) / zone <= ZONE_PROXIMITY_PCT
    if not near_zone:
        distance_pct = abs(price - zone) / zone * 100
        return _momentum_result(f"자리 근처 아님 (거리 {distance_pct:.2f}%)", zone, zone_strength)

    if not check_5m_trigger(data["df_5m"], zone, direction):
        return {"fit": False, "reason": "자리 근처지만 5분봉 반등/거래량 트리거 미확인", "price": price, "zone": zone, "zone_strength": zone_strength}

    entry = price
    exits = calculate_exit_prices(direction, entry, data.get("support"), data.get("resistance"), zone)
    confirmations = score_confirmations(direction, entry, exits, data["df_5m"])
    if confirmations["score"] < MIN_CONFIRM_SCORE:
        return {
            "fit": False,
            "reason": f"보조 확인 부족 ({confirmations['score']}/{MIN_CONFIRM_SCORE}: {', '.join(confirmations['items'])})",
            "price": price,
            "zone": zone,
            "zone_strength": zone_strength,
            "confirmations": confirmations,
        }
    return {
        "fit": True,
        "reason": None,
        "setup_type": "zone",
        "setup_label": "15M 자리",
        "price": price,
        "zone": zone,
        "zone_strength": zone_strength,
        "entry": entry,
        "confirmations": confirmations,
        **exits,
    }


def format_trigger_message(symbol: str, direction: str, result: dict, market: str = "domestic") -> str:
    side = "롱" if direction == "UP" else "숏"
    setup_label = result.get("setup_label", "15M 자리")
    return (
        f"[단타 트리거] {symbol} {side} ({setup_label})\n"
        f"기준가: {format_price(result['zone'], market)}\n"
        f"자리강도: {result['zone_strength']['label']} ({result['zone_strength']['reason']})\n"
        f"보조확인: {result['confirmations']['score']}점 ({', '.join(result['confirmations']['items'])})\n"
        f"진입: {format_price(result['entry'], market)}\n"
        f"손절: {format_price(result['sl'], market)} ({result['sl_basis']})\n"
        f"익절: {format_price(result['tp'], market)} ({result['tp_basis']})"
    )


def format_prepare_message(symbol: str, direction: str, data: dict, zone: float, market: str) -> str:
    side = "롱" if direction == "UP" else "숏"
    distance_pct = abs(float(data["price"]) - zone) / zone * 100
    zone_label = "지지" if direction == "UP" else "저항"
    strength = data["support_strength"] if direction == "UP" else data["resistance_strength"]
    return (
        f"[단타 준비 알림] {symbol} {side}\n"
        f"1H 추세: {data['trend']}\n"
        f"현재가: {format_price(data['price'], market)}\n"
        f"접근 중인 {zone_label}: {format_price(zone, market)}\n"
        f"거리: {distance_pct:.2f}% ({strength['label']})\n"
        "아직 5분봉 트리거 전입니다. 반등/돌파 확인 후 진입하세요."
    )


def format_judgement_reply(symbol: str, direction: str, result: dict, market: str = "domestic") -> str:
    side = "롱" if direction == "UP" else "숏"
    if result["fit"]:
        setup_label = result.get("setup_label", "15M 자리")
        return (
            f"[{side} 판정] {symbol}\n"
            f"적합 ({setup_label})\n"
            f"기준가: {format_price(result['zone'], market)}\n"
            f"자리강도: {result['zone_strength']['label']} ({result['zone_strength']['reason']})\n"
            f"보조확인: {result['confirmations']['score']}점 ({', '.join(result['confirmations']['items'])})\n"
            f"진입: {format_price(result['entry'], market)}\n"
            f"손절: {format_price(result['sl'], market)} ({result['sl_basis']})\n"
            f"익절: {format_price(result['tp'], market)} ({result['tp_basis']})"
        )
    price_part = f"\n현재가: {format_price(result['price'], market)}" if result.get("price") else ""
    zone_part = f"\n자리: {format_price(result['zone'], market)}" if result.get("zone") else ""
    return f"[{side} 판정] {symbol}\n부적합\n사유: {result['reason']}{price_part}{zone_part}"


def build_market_api(config: dict, market: str, redis_client: redis.Redis):
    auth = TossAuth(config, redis_client=redis_client)
    client = TossRestClient(auth)
    broker = TossBrokerAPI(client)
    if market == "domestic":
        return TossDomesticAPI(broker, config, redis_client=redis_client)
    return TossOverseasAPI(broker, config, redis_client=redis_client)


class ScannerState:
    def __init__(self, symbol: str, market: str):
        self.symbol = symbol.upper()
        self.market = market
        self.awaiting_symbol = False
        self.scan_data: dict | None = None
        self.last_candle_scan_at = 0.0


def _parse_allowed_chat_ids(telegram_cfg: dict) -> set[str]:
    raw_ids = telegram_cfg.get("allowed_chat_ids") or []
    if isinstance(raw_ids, (str, int)):
        raw_ids = [raw_ids]
    allowed = {str(chat_id).strip() for chat_id in raw_ids if str(chat_id).strip()}
    legacy_chat_id = str(telegram_cfg.get("chat_id") or "").strip()
    if legacy_chat_id:
        allowed.add(legacy_chat_id)
    return allowed


def _parse_symbol_args(args: list[str], current_market: str) -> tuple[str, str] | None:
    if not args:
        return None
    if len(args) >= 2 and args[0].lower() in ("domestic", "overseas"):
        return args[1].upper(), args[0].lower()
    return args[0].upper(), current_market


def _trigger_zone_key(zone: float | None, market: str) -> float:
    if zone is None:
        return 0.0
    return round(float(zone), 2 if market == "overseas" else 0)


def _help_text() -> str:
    return (
        "[단타 스캐너]\n"
        "/status - 현재 감시 상태\n"
        "/long - 롱 적합 판정\n"
        "/short - 숏 적합 판정\n"
        "/change 005930 - 국내 종목 변경\n"
        "/change overseas GOOGL - 해외 종목 변경"
    )


def _status_text(symbol: str, market: str, data: dict, tick_count: int) -> str:
    support = data.get("support")
    resistance = data.get("resistance")
    support_strength = data.get("support_strength") or {}
    resistance_strength = data.get("resistance_strength") or {}
    return (
        "[단타 상태]\n"
        f"감시: {market} {symbol}\n"
        f"현재가: {format_price(data.get('price'), market)}\n"
        f"1H 추세: {data.get('trend', 'UNKNOWN')}\n"
        f"15M 지지: {format_price(support, market)} ({support_strength.get('label', '-')}, {support_strength.get('reason', '-')})\n"
        f"15M 저항: {format_price(resistance, market)} ({resistance_strength.get('label', '-')}, {resistance_strength.get('reason', '-')})\n"
        f"최근 1일 틱: {tick_count}개"
    )


async def run_async(symbol: str, market: str, interval: int) -> None:
    config = load_config()
    redis_cfg = config.get("redis", {})
    redis_client = redis.Redis(
        host=redis_cfg.get("host", "localhost"),
        port=redis_cfg.get("port", 6379),
        db=redis_cfg.get("db", 0),
        decode_responses=False,
    )
    api_by_market = {
        "domestic": build_market_api(config, "domestic", redis_client),
        "overseas": build_market_api(config, "overseas", redis_client),
    }
    tick_store = TickStore()
    telegram_cfg = config.get("telegram", {})
    token = telegram_cfg.get("bot_token")
    allowed_chat_ids = _parse_allowed_chat_ids(telegram_cfg)

    loop = asyncio.get_event_loop()
    states: dict[str, ScannerState] = {
        chat_id: ScannerState(symbol, market) for chat_id in allowed_chat_ids
    }
    locks: dict[str, asyncio.Lock] = {}
    last_trigger_at: dict[tuple[str, str, str, str, float], float] = {}
    last_prepare_at: dict[tuple[str, str, str, str, float], float] = {}

    def _state_for(chat_id: str) -> ScannerState:
        states.setdefault(chat_id, ScannerState(symbol, market))
        locks.setdefault(chat_id, asyncio.Lock())
        return states[chat_id]

    def _lock_for(chat_id: str) -> asyncio.Lock:
        locks.setdefault(chat_id, asyncio.Lock())
        return locks[chat_id]

    def _is_allowed(update: Update) -> tuple[bool, str]:
        chat = update.effective_chat
        chat_id = str(chat.id) if chat else ""
        return bool(chat_id and chat_id in allowed_chat_ids), chat_id

    async def _full_scan(chat_id: str, state: ScannerState) -> dict:
        api = api_by_market[state.market]
        data = await loop.run_in_executor(
            None,
            scan_once,
            api,
            state.symbol,
            state.market,
            tick_store,
            chat_id,
        )
        state.scan_data = data
        state.last_candle_scan_at = time.time()
        return data

    async def _confirm_symbol_changed(chat_id: str, state: ScannerState, args: list[str]) -> str:
        parsed = _parse_symbol_args(args, state.market)
        if not parsed:
            state.awaiting_symbol = True
            return "바꿀 종목코드를 보내주세요. 예: 005930 또는 overseas AAPL"

        new_symbol, new_market = parsed
        old = f"{state.market} {state.symbol}"
        state.symbol = new_symbol
        state.market = new_market
        state.awaiting_symbol = False
        state.scan_data = None
        state.last_candle_scan_at = 0.0
        for key in list(last_trigger_at):
            if key[0] == chat_id:
                last_trigger_at.pop(key, None)
        for key in list(last_prepare_at):
            if key[0] == chat_id:
                last_prepare_at.pop(key, None)

        try:
            data = await _full_scan(chat_id, state)
            return (
                "[단타 감시 변경]\n"
                f"{old} -> {state.market} {state.symbol}\n"
                f"현재가: {format_price(data.get('price'), state.market)}\n"
                f"1H 추세: {data.get('trend', 'UNKNOWN')}"
            )
        except Exception:
            logger.exception("종목 변경 확인 스캔 실패: chat_id=%s symbol=%s", chat_id, state.symbol)
            return (
                "[단타 감시 변경]\n"
                f"{old} -> {state.market} {state.symbol}\n"
                "변경은 완료됐지만 현재가/추세 확인에 실패했습니다."
            )

    async def _on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        allowed, chat_id = _is_allowed(update)
        if not allowed or not update.message:
            logger.warning("허용되지 않은 텔레그램 접근: chat_id=%s", chat_id or "-")
            if update.message and chat_id:
                await update.message.reply_text(f"등록되지 않은 사용자입니다. 관리자에게 chat_id를 보내주세요: {chat_id}")
            return

        state = _state_for(chat_id)
        cmd = (update.message.text or "").split()[0].lower().split("@")[0]
        async with _lock_for(chat_id):
            if cmd in ("/start", "/help"):
                state.awaiting_symbol = False
                await update.message.reply_text(_help_text())
                return

            if cmd == "/change":
                reply = await _confirm_symbol_changed(chat_id, state, list(context.args or []))
                await update.message.reply_text(reply)
                return

            if cmd == "/status":
                data = await _full_scan(chat_id, state)
                count = tick_store.tick_count(chat_id, state.symbol, state.market)
                await update.message.reply_text(_status_text(state.symbol, state.market, data, count))
                return

            direction = "UP" if cmd == "/long" else "DOWN"
            data = await _full_scan(chat_id, state)
            result = judge_direction(direction, data)
            await update.message.reply_text(format_judgement_reply(state.symbol, direction, result, state.market))

    async def _on_text(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        allowed, chat_id = _is_allowed(update)
        if not allowed or not update.message:
            logger.warning("허용되지 않은 텔레그램 접근: chat_id=%s", chat_id or "-")
            if update.message and chat_id:
                await update.message.reply_text(f"등록되지 않은 사용자입니다. 관리자에게 chat_id를 보내주세요: {chat_id}")
            return

        text = (update.message.text or "").strip()
        state = _state_for(chat_id)
        async with _lock_for(chat_id):
            if state.awaiting_symbol and text:
                reply = await _confirm_symbol_changed(chat_id, state, text.split())
            else:
                reply = _help_text()
            await update.message.reply_text(reply)

    app = None
    if token and allowed_chat_ids:
        app = ApplicationBuilder().token(token).build()
        app.add_handler(CommandHandler(["start", "help", "long", "short", "change", "status"], _on_command))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))
        await app.initialize()
        await app.start()
        if app.updater:
            await app.updater.start_polling(drop_pending_updates=True)
        logger.info("텔레그램 단타 스캐너 활성화: allowed_chat_ids=%d", len(allowed_chat_ids))
    else:
        logger.info("텔레그램 설정 없음(config.yaml의 telegram.bot_token/chat_id 또는 allowed_chat_ids) — 콘솔 스캐너만 실행")

    if not states:
        states["console"] = ScannerState(symbol, market)

    logger.info("스캐너 시작: 기본 %s(%s), interval=%ds", symbol.upper(), market, interval)
    try:
        while True:
            for chat_id, state in list(states.items()):
                try:
                    async with _lock_for(chat_id):
                        now = time.time()
                        api = api_by_market[state.market]
                        if state.scan_data is None or now - state.last_candle_scan_at >= CANDLE_REFRESH_SEC:
                            state.scan_data = await loop.run_in_executor(
                                None,
                                scan_once,
                                api,
                                state.symbol,
                                state.market,
                                tick_store,
                                chat_id,
                            )
                            state.last_candle_scan_at = now
                            scan_kind = "full"
                        else:
                            state.scan_data = await loop.run_in_executor(
                                None,
                                refresh_price,
                                api,
                                state.symbol,
                                state.scan_data,
                                state.market,
                                tick_store,
                                chat_id,
                            )
                            scan_kind = "price"

                        trend = state.scan_data["trend"]
                        logger.info(
                            "%s %s(%s) price=%s trend=%s scan=%s",
                            chat_id,
                            state.symbol,
                            state.market,
                            format_price(state.scan_data["price"], state.market),
                            trend,
                            scan_kind,
                        )

                        for direction in ("UP", "DOWN"):
                            result = judge_direction(direction, state.scan_data)
                            if not result["fit"]:
                                zone = state.scan_data.get("support" if direction == "UP" else "resistance")
                                strength = state.scan_data.get(
                                    "support_strength" if direction == "UP" else "resistance_strength"
                                ) or {}
                                price = float(state.scan_data.get("price") or 0)
                                if (
                                    state.scan_data.get("trend") == direction
                                    and zone
                                    and strength.get("score", 0) >= MIN_ZONE_SCORE
                                    and price > 0
                                ):
                                    distance = abs(price - float(zone)) / float(zone)
                                    if ZONE_PROXIMITY_PCT < distance <= PREPARE_ZONE_PROXIMITY_PCT:
                                        prepare_key = (
                                            chat_id,
                                            state.market,
                                            state.symbol,
                                            direction,
                                            _trigger_zone_key(zone, state.market),
                                        )
                                        last_at = last_prepare_at.get(prepare_key, 0.0)
                                        if time.time() - last_at >= PREPARE_COOLDOWN_SEC:
                                            message = format_prepare_message(
                                                state.symbol, direction, state.scan_data, float(zone), state.market
                                            )
                                            logger.info("준비 알림 발생: chat_id=%s\n%s", chat_id, message)
                                            if app and chat_id != "console":
                                                await app.bot.send_message(chat_id=chat_id, text=message)
                                            last_prepare_at[prepare_key] = time.time()
                                continue
                            cooldown_key = (
                                chat_id,
                                state.market,
                                state.symbol,
                                direction,
                                _trigger_zone_key(result.get("zone"), state.market),
                            )
                            last_at = last_trigger_at.get(cooldown_key, 0.0)
                            if time.time() - last_at < COOLDOWN_SEC:
                                continue

                            message = format_trigger_message(state.symbol, direction, result, state.market)
                            logger.info("트리거 발생: chat_id=%s\n%s", chat_id, message)
                            if app and chat_id != "console":
                                await app.bot.send_message(chat_id=chat_id, text=message)
                            last_trigger_at[cooldown_key] = time.time()
                except Exception:
                    logger.exception("스캐너 사이클 실패: chat_id=%s — 다음 주기에 재시도", chat_id)

            await asyncio.sleep(interval)
    finally:
        if app:
            if app.updater:
                await app.updater.stop()
            await app.stop()
            await app.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description="1개 종목 단타 트리거 스캐너 (REST 폴링, 결정론적 규칙)")
    parser.add_argument("symbol", help="종목코드 (예: 005930)")
    parser.add_argument("--market", choices=["domestic", "overseas"], default="domestic")
    parser.add_argument("--interval", type=int, default=15, help="폴링 주기(초, 기본 15)")
    args = parser.parse_args()

    try:
        asyncio.run(run_async(args.symbol, args.market, args.interval))
    except KeyboardInterrupt:
        logger.info("사용자 중단 — 스캐너 종료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
