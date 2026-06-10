import asyncio
import json
import logging
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from routers import state

logger = logging.getLogger(__name__)
router = APIRouter()


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
                from datetime import datetime as _dt
                df = domestic.get_minute_ohlcv(stock_code, input_hour=_dt.now().strftime("%H%M%S"))
                if not df.empty:
                    df = domestic._aggregate(df, 5)
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
