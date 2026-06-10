import asyncio
import json
import logging
import pickle
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from routers import state

logger = logging.getLogger(__name__)
router = APIRouter()

_CHART_CACHE_DIR = Path("data/cache")


async def _fetch_today_minute_candles(domestic, stock_code: str) -> pd.DataFrame:
    """오늘 1분봉을 캐시 + delta 방식으로 반환.

    캐시 없음 → 오늘 전체 페이지네이션 후 저장.
    캐시 있음 → 로드 후 마지막 시각 이후 1회 delta 호출만.
    """
    _CHART_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = _CHART_CACHE_DIR / f"{stock_code}_chart_1min.pkl"

    today = date.today()
    now_str = datetime.now().strftime("%H%M%S")
    cap_hour = "180000" if now_str > "180000" else now_str

    # 오늘 캐시 로드
    cached: pd.DataFrame = pd.DataFrame()
    if cache_file.exists():
        try:
            with cache_file.open("rb") as f:
                loaded: pd.DataFrame = pickle.load(f)
            if not loaded.empty and loaded["datetime"].dt.date.max() == today:
                cached = loaded
        except Exception:
            cache_file.unlink(missing_ok=True)

    if not cached.empty:
        last_hour = cached["datetime"].max().strftime("%H%M%S")
        if last_hour >= cap_hour:
            return cached  # 이미 최신
        # delta: 마지막 캐시 이후 새 1분봉 1회 호출
        try:
            delta = domestic.get_minute_ohlcv(stock_code, input_hour=cap_hour)
            if not delta.empty:
                new_rows = delta[delta["datetime"] > cached["datetime"].max()]
                if not new_rows.empty:
                    cached = (
                        pd.concat([cached, new_rows])
                        .drop_duplicates("datetime")
                        .sort_values("datetime")
                        .reset_index(drop=True)
                    )
                    with cache_file.open("wb") as f:
                        pickle.dump(cached, f)
        except Exception as e:
            logger.warning("분봉 delta 조회 실패(%s): %s", stock_code, e)
        return cached

    # 캐시 없음: 오늘 전체 페이지네이션
    cur_hour = cap_hour
    dfs: list[pd.DataFrame] = []
    for _ in range(20):
        try:
            chunk = domestic.get_minute_ohlcv(stock_code, input_hour=cur_hour)
        except Exception as e:
            logger.warning("분봉 페이지 조회 실패(%s@%s): %s", stock_code, cur_hour, e)
            break
        if chunk.empty:
            break
        today_chunk = chunk[chunk["datetime"].dt.date == today]
        if not today_chunk.empty:
            dfs.append(today_chunk)
        earliest = chunk["datetime"].min()
        if earliest.date() < today or earliest.strftime("%H%M%S") <= "090500":
            break
        cur_hour = (earliest - pd.Timedelta(minutes=1)).strftime("%H%M%S")
        await asyncio.sleep(0.15)

    if not dfs:
        return pd.DataFrame()

    result = (
        pd.concat(dfs)
        .drop_duplicates("datetime")
        .sort_values("datetime")
        .reset_index(drop=True)
    )
    with cache_file.open("wb") as f:
        pickle.dump(result, f)
    return result


class ChatRequest(BaseModel):
    message: str


@router.post("/ai/chat")
async def ai_chat(req: ChatRequest):
    if state.agent is None:
        raise HTTPException(status_code=503, detail="AI 에이전트 초기화 중")
    response = await state.agent.chat(req.message)
    return {"response": response}


@router.get("/ai/plan")
async def ai_plan():
    if not state.components.get("memory"):
        raise HTTPException(status_code=503, detail="메모리 초기화 중")
    plan = await state.components["memory"].get_today_plan()
    return plan or {"message": "오늘 계획 없음"}


@router.get("/ai/decisions")
async def ai_decisions(limit: int = 20):
    if not state.components.get("memory"):
        raise HTTPException(status_code=503, detail="메모리 초기화 중")
    return await state.components["memory"].get_recent_decisions(limit)


@router.get("/ai/memos")
async def ai_memos(limit: int = 10):
    if not state.components.get("memory"):
        raise HTTPException(status_code=503, detail="메모리 초기화 중")
    return await state.components["memory"].get_recent_memos(limit)


@router.post("/ai/brief")
async def trigger_morning_brief():
    if state.agent is None:
        raise HTTPException(status_code=503, detail="AI 에이전트 초기화 중")
    asyncio.create_task(state.agent.morning_brief())
    return {"status": "브리핑 시작됨"}


@router.get("/ai/watches")
async def get_watches():
    r = state.components.get("redis") if state.components else None
    if not r:
        return {"watches": {}}
    raw = r.get("ai:watches")
    return {"watches": json.loads(raw) if raw else {}}


@router.get("/ai/indicators/{stock_code}")
async def get_indicators(stock_code: str):
    r = state.components.get("redis") if state.components else None
    if not r:
        return {"stock_code": stock_code, "indicators": {}}
    raw = r.get(f"ai:indicators:{stock_code}")
    return {"stock_code": stock_code, "indicators": json.loads(raw) if raw else {}}


@router.get("/ai/candles/{stock_code}")
async def get_candles_for_chart(stock_code: str, candle_type: str = "daily", count: int = 30):
    overseas = state.components.get("overseas")
    domestic = state.components.get("domestic")
    if not overseas and not domestic:
        return {"stock_code": stock_code, "candles": []}
    try:
        from kis.constants import ExchangeCode
        is_domestic = stock_code.isdigit()
        if is_domestic and domestic:
            if candle_type == "minute":
                df_1min = await _fetch_today_minute_candles(domestic, stock_code)
                if not df_1min.empty:
                    df = domestic._aggregate(df_1min, 5)
                    df = df[df["volume"] > 0].reset_index(drop=True)
                else:
                    df = pd.DataFrame()
            else:
                end = date.today()
                start = end - timedelta(days=max(count * 2, 60))
                df = domestic.get_daily_ohlcv(stock_code, start, end)
        elif overseas:
            exch = ExchangeCode.NASDAQ
            if candle_type == "minute":
                df = overseas.get_historical_minute_ohlcv(stock_code, exch, lookback_days=2, candle_minutes=5)
            else:
                end = date.today()
                start = end - timedelta(days=max(count * 2, 60))
                df = overseas.get_daily_ohlcv(stock_code, exch, start_date=start, end_date=end)
        else:
            return {"stock_code": stock_code, "candles": []}
        df = df.tail(count)
        candles = [
            {
                "datetime": str(row.get("datetime", idx)),
                "open": float(row.get("open", 0)),
                "high": float(row.get("high", 0)),
                "low": float(row.get("low", 0)),
                "close": float(row.get("close", 0)),
                "volume": float(row.get("volume", 0)),
            }
            for idx, row in df.iterrows()
        ]
        return {"stock_code": stock_code, "candle_type": candle_type, "candles": candles}
    except Exception as e:
        logger.exception("캔들 차트 조회 실패: %s", stock_code)
        return {"stock_code": stock_code, "candles": [], "error": str(e)}
