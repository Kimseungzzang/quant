"""
FastAPI — AI 트레이더 백엔드 서버 (포트 8000)
- AI 에이전트 루프 (EventEngine + AIAgent) 내장
- 프론트엔드(Electron)에 WebSocket으로 실시간 스트림 제공
- 기존 분석/백테스트/포지션 조회 엔드포인트 유지
"""
import asyncio
import logging
import os
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any

import asyncpg
import redis
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

from kis.auth import KISAuth
from kis.rest import KISRestClient
from kis.domestic import DomesticAPI
from kis.overseas import OverseasAPI
from kis.websocket import (
    KISWebSocket,
    parse_domestic_price,
    parse_domestic_askbid,
    parse_overseas_price,
    parse_domestic_fill_notice,
    parse_overseas_fill_notice,
)
from kis.constants import WebSocketTRID, ExchangeCode, TradingMode


from trading.risk import RiskManager
from trading.order_manager import OrderManager, TradeLogger

from db.pg_writer import PGWriter, PGWriterSync

from collector.market_data import MarketDataCollector
from collector.account import AccountCollector

from events.types import Market, MarketEvent
from events.detector import EventDetector
from events.engine import EventEngine

from ai.memory import AgentMemory
from ai.tools import ToolExecutor
from ai.agent import AIAgent
from ai.provider import create_provider

from utils import CandleAggregator, load_config, setup_logging

logger = logging.getLogger(__name__)

# ── 전역 상태 ─────────────────────────────────────────────────────────

_ws_clients: set[WebSocket] = set()
_components: dict[str, Any] = {}
_agent: AIAgent | None = None
_event_engine: EventEngine | None = None

_components_lock = threading.Lock()
_trading_task: asyncio.Task | None = None


# ── WebSocket 브로드캐스트 ────────────────────────────────────────────

async def _broadcast(msg: dict) -> None:
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


def _on_ai_message(source: str, message: str) -> None:
    if source == "chat":
        return  # CLI가 HTTP 응답으로 직접 처리
    asyncio.get_event_loop().call_soon_threadsafe(
        lambda: asyncio.create_task(
            _broadcast({"type": "ai_message", "source": source, "message": message, "ts": datetime.now().isoformat()})
        )
    )


# ── 컴포넌트 초기화 ───────────────────────────────────────────────────

def _build_sync_components(config: dict) -> dict:
    redis_cfg = config.get("redis", {})
    redis_client = redis.Redis(
        host=redis_cfg.get("host", "localhost"),
        port=redis_cfg.get("port", 6379),
        db=redis_cfg.get("db", 0),
        decode_responses=False,
    )
    auth = KISAuth(config, redis_client=redis_client)
    client = KISRestClient(auth)
    domestic = DomesticAPI(client, config)
    overseas = OverseasAPI(client, config)
    risk = RiskManager(config)
    pg_sync = PGWriterSync()
    order_mgr = OrderManager(domestic, overseas, risk, TradeLogger(), pg=pg_sync, mode=config["mode"])
    market_data = MarketDataCollector(redis_client)
    account = AccountCollector(redis_client)
    return dict(
        config=config, auth=auth,
        domestic=domestic, overseas=overseas,
        risk=risk, order_mgr=order_mgr,
        redis=redis_client,
        market_data=market_data, account=account,
    )


async def _build_async_components(config: dict, sync_comp: dict) -> dict:
    db_cfg = config.get("database", {})
    pg_pool = await asyncpg.create_pool(
        host=db_cfg.get("host", "localhost"),
        port=db_cfg.get("port", 5432),
        database=db_cfg.get("name", "quant_trading"),
        user=db_cfg.get("user") or os.getenv("USER"),
        password=db_cfg.get("password") or None,
        min_size=2, max_size=10,
    )
    memory = AgentMemory(pg_pool)

    tool_executor = ToolExecutor(
        market_data=sync_comp["market_data"],
        account=sync_comp["account"],
        order_manager=sync_comp["order_mgr"],
        memory=memory,
        redis_client=sync_comp["redis"],
        ws=sync_comp.get("ws"),
        domestic_api=sync_comp["domestic"],
        overseas_api=sync_comp["overseas"],
    )
    ai_cfg = config.get("ai", {})
    provider_name = ai_cfg.get("provider", "anthropic")
    _key_env = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
    api_key = os.getenv(_key_env.get(provider_name, "ANTHROPIC_API_KEY"), "")
    provider = create_provider(provider_name, api_key or "")
    agent = AIAgent(provider=provider, tool_executor=tool_executor, memory=memory, on_message=_on_ai_message)
    await agent.initialize()

    detector = EventDetector(sync_comp["market_data"], sync_comp["redis"])
    engine = EventEngine(detector)
    engine.register(agent.handle_event)

    return dict(pg_pool=pg_pool, memory=memory, agent=agent, engine=engine)


# ── WebSocket 거래 루프 ───────────────────────────────────────────────

async def _trading_loop(config: dict, comp: dict) -> None:
    aggregators: dict[str, CandleAggregator] = {}
    order_mgr: OrderManager = comp["order_mgr"]
    market_data: MarketDataCollector = comp["market_data"]
    engine: EventEngine = comp["engine"]

    def on_domestic_price(fields: list) -> None:
        from kis.websocket import parse_domestic_price as _parse
        parsed = _parse(fields)
        code = parsed.get("stock_code", "")
        if not code:
            return
        tick = {
            "stock_code": code, "current_price": parsed.get("price", 0),
            "volume": parsed.get("vol", 0),
            "acml_volume": parsed.get("acml_vol", 0),
            "time": parsed.get("time", ""),
            "exchange": "KRX", "stock_name": parsed.get("stock_name", code),
        }
        market_data.on_price_tick(code, tick)
        if code in aggregators:
            aggregators[code].update({"price": tick["current_price"], "vol": tick["volume"], "time": tick["time"]})
        order_mgr.record_price(code, tick["current_price"])
        asyncio.get_event_loop().call_soon_threadsafe(
            lambda c=code, p=tick["current_price"]: asyncio.create_task(
                _broadcast({"type": "price", "code": c, "price": p, "ts": datetime.now().isoformat()})
            )
        )

    def on_domestic_askbid(fields: list) -> None:
        from kis.websocket import parse_domestic_askbid as _parse
        parsed = _parse(fields)
        code = parsed.get("stock_code", "")
        if code:
            market_data.on_orderbook_tick(code, parsed)

    def on_overseas_price(fields: list) -> None:
        from kis.websocket import parse_overseas_price as _parse
        parsed = _parse(fields)
        code = parsed.get("stock_code", "")
        if not code:
            return
        tick = {
            "stock_code": code, "current_price": parsed.get("price", 0),
            "volume": parsed.get("vol", 0), "time": parsed.get("time", ""),
            "exchange": "NAS", "stock_name": code,
        }
        market_data.on_price_tick(code, tick)
        order_mgr.record_price(code, tick["current_price"])

    def on_fill(parsed: dict) -> None:
        order_mgr.on_order_notice(parsed)

    callbacks = {
        WebSocketTRID.DOMESTIC_PRICE: on_domestic_price,
        WebSocketTRID.DOMESTIC_ASKBID: on_domestic_askbid,
        WebSocketTRID.OVERSEAS_PRICE: on_overseas_price,
        WebSocketTRID.DOMESTIC_FILL: on_fill,
        WebSocketTRID.OVERSEAS_FILL: on_fill,
    }

    universe = config.get("universe", {})
    domestic_codes = [s if isinstance(s, str) else s["code"]
                      for s in universe.get("domestic", {}).get("stocks", [])]
    overseas_codes = [s["code"] for s in universe.get("overseas", {}).get("stocks", [])]
    for code in domestic_codes:
        aggregators[code] = CandleAggregator(period_minutes=1)

    ws = KISWebSocket(comp["auth"])
    comp["ws"] = ws  # set_watch에서 동적 구독에 사용
    logger.info("WebSocket 연결 시작 — 국내 %d종목, 해외 %d종목", len(domestic_codes), len(overseas_codes))
    await ws.connect_and_subscribe(
        domestic_codes=domestic_codes,
        overseas_codes=overseas_codes,
        callbacks=callbacks,
    )


# ── 아침 브리핑 스케줄러 ─────────────────────────────────────────────

async def _morning_brief_loop(agent: AIAgent, config: dict, stop: asyncio.Event) -> None:
    ai_cfg = config.get("ai", {})
    brief_hour = int(ai_cfg.get("morning_brief_hour", 8))
    brief_min = int(ai_cfg.get("morning_brief_minute", 30))
    while not stop.is_set():
        now = datetime.now()
        if now.hour == brief_hour and now.minute == brief_min:
            try:
                await agent.morning_brief()
            except Exception:
                logger.exception("아침 브리핑 실패")
            await asyncio.sleep(61)
        await asyncio.sleep(30)


# ── 계좌 폴러 ─────────────────────────────────────────────────────────

def _start_pollers(comp: dict, stop: threading.Event) -> None:
    def _account_poll():
        while not stop.is_set():
            try:
                dom = comp["domestic"].get_balance()
                comp["account"].update_balance("domestic", dom)
                positions = [
                    {
                        "stock_code": p.stock_code, "stock_name": p.name,
                        "market": "domestic" if p.is_domestic() else "overseas",
                        "quantity": p.qty, "avg_price": p.entry_price,
                        "current_price": p.current_price,
                        "unrealized_pct": round((p.current_price - p.entry_price) / p.entry_price * 100, 2)
                        if p.entry_price else 0,
                    }
                    for p in comp["order_mgr"].get_open_positions().values()
                ]
                comp["account"].update_positions(positions)
            except Exception:
                logger.exception("계좌 폴링 실패")
            stop.wait(60)

    threading.Thread(target=_account_poll, daemon=True, name="account-poller").start()


# ── Lifespan ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _components, _agent, _event_engine, _trading_task

    config = load_config()
    setup_logging(config)

    sync_comp = _build_sync_components(config)
    async_comp = await _build_async_components(config, sync_comp)
    _components = {**sync_comp, **async_comp}
    _agent = async_comp["agent"]
    _event_engine = async_comp["engine"]

    stop_event = threading.Event()
    async_stop = asyncio.Event()

    _start_pollers(_components, stop_event)
    await _event_engine.run()
    asyncio.create_task(_morning_brief_loop(_agent, config, async_stop))

    _trading_task = asyncio.create_task(_trading_loop(config, _components))

    logger.info("AI 트레이더 백엔드 준비 완료")
    yield

    async_stop.set()
    stop_event.set()
    if _trading_task:
        _trading_task.cancel()
    await _event_engine.stop()
    await _components["pg_pool"].close()
    logger.info("AI 트레이더 백엔드 종료")


# ── FastAPI 앱 ────────────────────────────────────────────────────────

app = FastAPI(title="AI Trader", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 요청 모델 ─────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str

class ModeRequest(BaseModel):
    mode: str


# ── 헬스 ──────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    config = _components.get("config") or {}
    return {
        "status": "ok",
        "mode": config.get("mode"),
        "trading_active": _trading_task is not None and not _trading_task.done(),
    }


# ── AI 엔드포인트 ─────────────────────────────────────────────────────

@app.post("/ai/chat")
async def ai_chat(req: ChatRequest):
    if _agent is None:
        raise HTTPException(status_code=503, detail="AI 에이전트 초기화 중")
    response = await _agent.chat(req.message)
    return {"response": response}


@app.get("/ai/plan")
async def ai_plan():
    if not _components.get("memory"):
        raise HTTPException(status_code=503, detail="메모리 초기화 중")
    plan = await _components["memory"].get_today_plan()
    return plan or {"message": "오늘 계획 없음"}


@app.get("/ai/decisions")
async def ai_decisions(limit: int = 20):
    if not _components.get("memory"):
        raise HTTPException(status_code=503, detail="메모리 초기화 중")
    return await _components["memory"].get_recent_decisions(limit)


@app.get("/ai/memos")
async def ai_memos(limit: int = 10):
    if not _components.get("memory"):
        raise HTTPException(status_code=503, detail="메모리 초기화 중")
    return await _components["memory"].get_recent_memos(limit)


@app.post("/ai/brief")
async def trigger_morning_brief():
    if _agent is None:
        raise HTTPException(status_code=503, detail="AI 에이전트 초기화 중")
    asyncio.create_task(_agent.morning_brief())
    return {"status": "브리핑 시작됨"}


# ── WebSocket 스트림 ──────────────────────────────────────────────────

@app.websocket("/ws/stream")
async def ws_stream(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        _ws_clients.discard(ws)


# ── 계좌 / 시장 ───────────────────────────────────────────────────────

_balance_cache: dict = {}
_BALANCE_TTL = 30

@app.get("/account/balance")
def account_balance(market: str = "domestic", mode: str | None = None):
    config = _components.get("config") or {}
    engine_mode = config.get("mode")
    if mode and engine_mode and mode != engine_mode:
        raise HTTPException(status_code=400, detail=f"현재 엔진 모드: {engine_mode}")
    cache_key = f"{engine_mode}:{market}"
    cached = _balance_cache.get(cache_key)
    if cached and (datetime.now().timestamp() - cached["ts"]) < _BALANCE_TTL:
        return cached["data"]
    try:
        if market == "overseas":
            configured = config.get("universe", {}).get("overseas", {}).get("exchanges", ["NAS", "NYS"])
            positions, summaries, seen = [], [], set()
            for exch in configured:
                try:
                    balance = _components["overseas"].get_balance(ExchangeCode(exch))
                except Exception:
                    continue
                summaries.append(balance.get("summary") or {})
                for pos in balance.get("positions") or []:
                    key = (pos.get("ovrs_excg_cd"), pos.get("ovrs_pdno"))
                    if key not in seen:
                        seen.add(key)
                        positions.append(pos)
            stock_value = sum(_to_float(p.get("ovrs_stck_evlu_amt")) for p in positions)
            purchase_amt = sum(_to_float(s.get("frcr_pchs_amt1")) for s in summaries)
            evlu_pfls = sum(_to_float(s.get("ovrs_tot_pfls")) for s in summaries)
            raw_cash = _components["overseas"].get_foreign_margin_usd()
            cash = max(raw_cash - purchase_amt, 0) if purchase_amt > 0 else raw_cash
            total = (stock_value if stock_value > 0 else purchase_amt + evlu_pfls) + cash
            result = {
                "market": "overseas", "mode": engine_mode, "currency": "USD",
                "cash": cash, "totalAssets": total, "positionValue": stock_value,
                "positionCount": len(positions), "totalPnl": evlu_pfls,
                "totalPnlPct": round(evlu_pfls / purchase_amt * 100, 4) if purchase_amt > 0 else 0,
                "updatedAt": datetime.now().isoformat(),
            }
        else:
            balance = _components["domestic"].get_balance()
            summary = balance.get("summary") or {}
            result = {
                "market": "domestic", "mode": engine_mode, "currency": "KRW",
                "cash": _to_float(summary.get("dnca_tot_amt")),
                "totalAssets": _to_float(summary.get("tot_evlu_amt") or summary.get("nass_amt")),
                "positionValue": _to_float(summary.get("evlu_amt_smtl_amt")),
                "positionCount": len(balance.get("positions") or []),
                "updatedAt": datetime.now().isoformat(),
            }
        _balance_cache[cache_key] = {"data": result, "ts": datetime.now().timestamp()}
        return result
    except Exception as e:
        logger.exception("계좌 잔고 조회 실패")
        raise HTTPException(status_code=502, detail=str(e))


# ── 포지션 / 주문 ─────────────────────────────────────────────────────

@app.get("/trade/positions")
async def get_positions():
    return await PGWriter().get_positions()


@app.get("/trade/positions/live")
async def get_live_positions(mode: str | None = None):
    order_mgr = _components.get("order_mgr")
    if order_mgr is None:
        raise HTTPException(status_code=503, detail="초기화 중")
    return order_mgr.get_live_positions()


@app.get("/trade/orders/pending")
def get_pending_orders():
    order_mgr = _components.get("order_mgr")
    if order_mgr is None:
        raise HTTPException(status_code=503, detail="초기화 중")
    return order_mgr.get_pending_order_rows()


@app.get("/trade/orders/fills")
def get_order_fills(market: str = "overseas"):
    try:
        if market == "overseas":
            return _components["overseas"].get_daily_orders()
        return _components["domestic"].get_daily_orders()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/mode")
def set_mode(req: ModeRequest):
    if req.mode not in {"paper", "live"}:
        raise HTTPException(status_code=400, detail="mode는 paper/live 중 하나")
    cfg = _components.get("config") or {}
    cfg["mode"] = req.mode
    order_mgr = _components.get("order_mgr")
    if order_mgr:
        order_mgr.mode = req.mode
    return {"status": "ok", "mode": req.mode}


# ── 거래 내역 조회 ───────────────────────────────────────────────────

@app.get("/trades")
async def get_trades(mode: str = "paper", page: int = 0, size: int = 20, period: str = "all", stockCode: str = ""):
    return await PGWriter().get_trades(mode=mode, page=page, size=size, period=period, stock_code=stockCode)


@app.get("/trades/pnl/summary")
async def get_pnl_summary(mode: str = "paper"):
    return await PGWriter().get_pnl_summary(mode=mode)


@app.get("/trades/pnl/chart")
async def get_pnl_chart(mode: str = "paper", days: int = 30):
    return await PGWriter().get_pnl_chart(mode=mode, days=days)


@app.get("/trades/performance/stocks")
async def get_stock_performance(mode: str = "paper", period: str = "month"):
    return await PGWriter().get_stock_performance(mode=mode, period=period)


@app.get("/trades/reports/daily")
async def get_daily_reports(mode: str = "paper", period: str = "month"):
    return await PGWriter().get_daily_reports(mode=mode, period=period)



# ── 유틸 ──────────────────────────────────────────────────────────────

def _to_float(value) -> float:
    try:
        return float(str(value or "0").replace(",", ""))
    except (TypeError, ValueError):
        return 0.0
