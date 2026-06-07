"""
FastAPI — AI 트레이더 백엔드 서버 (포트 8000)
- AI 에이전트 루프 (EventEngine + AIAgent) 내장
- 프론트엔드(Electron)에 WebSocket으로 실시간 스트림 제공
- 기존 분석/백테스트/포지션 조회 엔드포인트 유지
"""
import asyncio
import json
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

# ── 로그 버퍼 (최근 500줄 메모리 보관) ──────────────────────────────────
import collections as _collections
_log_buffer: _collections.deque = _collections.deque(maxlen=500)

class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        import datetime as _dt
        _log_buffer.append({
            "ts": _dt.datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
            "level": record.levelname,
            "name": record.name.split(".")[-1],
            "msg": record.getMessage(),
        })

_buf_handler = _BufferHandler()
_buf_handler.setLevel(logging.INFO)

# ── 전역 상태 ─────────────────────────────────────────────────────────

_ws_clients: set[WebSocket] = set()
_components: dict[str, Any] = {}
_agent: AIAgent | None = None
_event_engine: EventEngine | None = None

_components_lock = threading.Lock()
_trading_task: asyncio.Task | None = None
_trading_last_error: str | None = None


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
        config=config,
    )
    ai_cfg = config.get("ai", {})
    provider_name = ai_cfg.get("provider", "anthropic")
    _key_env = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
    api_key = os.getenv(_key_env.get(provider_name, "ANTHROPIC_API_KEY"), "")
    provider = create_provider(provider_name, api_key or "")
    agent = AIAgent(provider=provider, tool_executor=tool_executor, memory=memory, on_message=_on_ai_message)
    await agent.initialize()

    from events.indicator_cache import IndicatorCache
    indicator_cache = IndicatorCache(
        redis_client=sync_comp["redis"],
        domestic=sync_comp.get("domestic"),
        overseas=sync_comp.get("overseas"),
    )
    detector = EventDetector(sync_comp["market_data"], sync_comp["redis"], indicator_cache=indicator_cache)
    engine = EventEngine(detector, indicator_cache=indicator_cache)
    engine.register(agent.handle_event)

    return dict(pg_pool=pg_pool, memory=memory, agent=agent, engine=engine)


# ── WebSocket 거래 루프 ───────────────────────────────────────────────

async def _trading_loop(config: dict, comp: dict) -> None:
    aggregators: dict[str, CandleAggregator] = {}
    order_mgr: OrderManager = comp["order_mgr"]
    market_data: MarketDataCollector = comp["market_data"]
    engine: EventEngine = comp["engine"]

    def on_domestic_price(tr_id, fields: list) -> None:
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
            "received_at": datetime.now().isoformat(),
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

    def on_domestic_askbid(tr_id, fields: list) -> None:
        from kis.websocket import parse_domestic_askbid as _parse
        parsed = _parse(fields)
        code = parsed.get("stock_code", "")
        if code:
            market_data.on_orderbook_tick(code, parsed)

    def on_overseas_price(tr_id, fields: list) -> None:
        from kis.websocket import parse_overseas_price as _parse
        parsed = _parse(fields)
        code = parsed.get("stock_code", "")
        if not code:
            return
        subscription_key = parsed.get("subscription_key", "")
        exchange = subscription_key[1:4] if len(subscription_key) >= 4 else "NAS"
        tick = {
            "stock_code": code, "current_price": parsed.get("price", 0),
            "volume": parsed.get("vol", 0),
            "acml_volume": parsed.get("acml_vol", 0),
            "time": parsed.get("time", ""),
            "exchange": exchange, "stock_name": code,
            "received_at": datetime.now().isoformat(),
        }
        market_data.on_price_tick(code, tick)
        order_mgr.record_price(code, tick["current_price"])

    def on_fill(tr_id, fields: list) -> None:
        parser = parse_domestic_fill_notice if tr_id in (
            WebSocketTRID.DOMESTIC_FILL_PAPER,
            WebSocketTRID.DOMESTIC_FILL_LIVE,
        ) else parse_overseas_fill_notice
        order_mgr.on_order_notice(parser(fields))

    domestic_fill_trid = WebSocketTRID.DOMESTIC_FILL_PAPER if comp["auth"].is_paper else WebSocketTRID.DOMESTIC_FILL_LIVE
    overseas_fill_trid = WebSocketTRID.OVERSEAS_FILL_PAPER if comp["auth"].is_paper else WebSocketTRID.OVERSEAS_FILL_LIVE

    callbacks = {
        WebSocketTRID.DOMESTIC_PRICE: on_domestic_price,
        WebSocketTRID.DOMESTIC_ASKBID: on_domestic_askbid,
        WebSocketTRID.OVERSEAS_PRICE: on_overseas_price,
        domestic_fill_trid: on_fill,
        overseas_fill_trid: on_fill,
    }

    universe = config.get("universe", {})
    domestic_codes = [s if isinstance(s, str) else s["code"]
                      for s in universe.get("domestic", {}).get("stocks", [])]
    overseas_items = universe.get("overseas", {}).get("stocks", [])
    overseas_codes = [s["code"] for s in overseas_items]
    overseas_exchanges = {
        str(s["code"]).upper(): str(s.get("exchange", "NAS")).upper()
        for s in overseas_items
        if isinstance(s, dict) and s.get("code")
    }
    overseas_exchanges.setdefault("HPE", "NYS")
    try:
        raw_watches = comp["redis"].get("ai:watches")
        watches = json.loads(raw_watches) if raw_watches else {}
        watched_codes = list(watches.keys()) if isinstance(watches, dict) else []
    except Exception:
        watches = {}
        watched_codes = []
    domestic_set = set(domestic_codes)
    overseas_set = set(overseas_codes)
    watched_domestic = [
        c for c in watched_codes
        if c in domestic_set or str(watches.get(c, {}).get("market", "")).lower() == "domestic"
    ]
    watched_overseas = [
        c for c in watched_codes
        if c in overseas_set or str(watches.get(c, {}).get("market", "")).lower() == "overseas"
    ]
    for code in watched_overseas:
        watch_exchange = watches.get(code, {}).get("exchange")
        if watch_exchange:
            overseas_exchanges[str(code).upper()] = str(watch_exchange).upper()
    domestic_codes = watched_domestic + [c for c in domestic_codes if c not in watched_codes]
    overseas_codes = watched_overseas + [c for c in overseas_codes if c not in watched_codes]
    max_ws_subscriptions = int(config.get("kis", {}).get("max_ws_subscriptions", 3))
    if watched_codes:
        domestic_codes = [c for c in domestic_codes if c in watched_codes]
        overseas_codes = [c for c in overseas_codes if c in watched_codes]

    if max_ws_subscriptions > 0:
        selected: list[tuple[str, str]] = []
        for code in overseas_codes:
            selected.append(("overseas", code))
        for code in domestic_codes:
            selected.append(("domestic", code))
        selected = selected[:max_ws_subscriptions]
        domestic_codes = [code for market, code in selected if market == "domestic"]
        overseas_codes = [code for market, code in selected if market == "overseas"]

    for code in domestic_codes:
        aggregators[code] = CandleAggregator(period_minutes=1)

    ws = KISWebSocket(comp["auth"])
    comp["ws"] = ws  # set_watch에서 동적 구독에 사용
    agent = comp.get("agent")
    executor = getattr(agent, "_executor", None)
    if executor is not None:
        executor._ws = ws
    logger.info("WebSocket 연결 시작 — 국내 %d종목, 해외 %d종목", len(domestic_codes), len(overseas_codes))
    await ws.connect_and_subscribe(
        domestic_codes=domestic_codes,
        overseas_codes=overseas_codes,
        callbacks=callbacks,
        overseas_exchanges=overseas_exchanges,
    )


async def _trading_loop_supervisor(config: dict, comp: dict, stop: asyncio.Event) -> None:
    global _trading_last_error
    while not stop.is_set():
        try:
            _trading_last_error = None
            await _trading_loop(config, comp)
            if not stop.is_set():
                _trading_last_error = "KIS WebSocket loop exited unexpectedly"
                logger.warning("%s — restarting in 5s", _trading_last_error)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _trading_last_error = f"{type(e).__name__}: {e}"
            logger.exception("KIS WebSocket loop failed — restarting in 5s")
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass


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


async def _auto_trading_schedule_loop(agent: AIAgent, config: dict, stop: asyncio.Event) -> None:
    schedule_cfg = config.get("schedule", {})
    if not schedule_cfg.get("auto", False):
        logger.info("자동 매매 스케줄 비활성화(schedule.auto=false)")
        return

    ran: set[str] = set()
    while not stop.is_set():
        now = datetime.now()
        hhmm = now.strftime("%H:%M")
        today = now.date().isoformat()
        mode = (config.get("mode") or "paper").lower()
        live_allowed = bool(schedule_cfg.get("allow_live_auto_trading", False))

        async def run_once(key: str, prompt: str) -> None:
            run_key = f"{today}:{key}"
            if run_key in ran:
                return
            if mode == "live" and not live_allowed:
                logger.warning("live 자동매매 차단: schedule.allow_live_auto_trading=false (%s)", key)
                ran.add(run_key)
                return
            ran.add(run_key)
            try:
                logger.info("자동매매 스케줄 실행: %s", key)
                await agent.chat(prompt)
            except Exception:
                logger.exception("자동매매 스케줄 실패: %s", key)

        if hhmm == schedule_cfg.get("domestic_analysis_time", "09:00"):
            await run_once(
                "domestic-analysis",
                (
                    "Run the autonomous Korean domestic market trading process now. "
                    "Analyze KOSPI/KRX conditions, portfolio, candidates, and charts. "
                    "If risk and setup are aligned, decide allocation percentage and place real orders automatically. "
                    "Otherwise set concrete detecting/watch rules. Save the plan and memo. "
                    "Respond in Korean only."
                ),
            )

        if hhmm == schedule_cfg.get("us_analysis_time", "22:30"):
            await run_once(
                "us-analysis",
                (
                    "Run the autonomous US market trading process now. "
                    "Analyze Nasdaq/S&P 500/Dow, US megacap and AI/semiconductor candidates, "
                    "portfolio cash, and candidate charts. If risk and setup are aligned, "
                    "decide allocation percentage and place real orders automatically. Otherwise set concrete detecting/watch rules. "
                    "Save the plan and memo. Respond in Korean only."
                ),
            )

        if hhmm == schedule_cfg.get("domestic_trading_end_time", "15:20"):
            await run_once(
                "domestic-close",
                (
                    "Korean domestic market trading window is ending. Review open domestic positions, "
                    "pending orders, and watches. Manage risk, save a memo, and respond in Korean only."
                ),
            )

        if hhmm == schedule_cfg.get("us_trading_end_time", "05:00"):
            await run_once(
                "us-close",
                (
                    "US market trading window is ending. Review open US positions, pending orders, "
                    "and watches. Manage risk, save a memo, and respond in Korean only."
                ),
            )

        await asyncio.sleep(20)


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
    logging.getLogger().addHandler(_buf_handler)  # setup_logging 이후에 붙여야 유지됨

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
    asyncio.create_task(_auto_trading_schedule_loop(_agent, config, async_stop))

    _trading_task = asyncio.create_task(_trading_loop_supervisor(config, _components, async_stop))

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
    ws = _components.get("ws")
    kis_ws_status = ws.status() if ws is not None and hasattr(ws, "status") else None
    return {
        "status": "ok",
        "mode": config.get("mode"),
        "trading_active": _trading_task is not None and not _trading_task.done(),
        "kis_ws_connected": bool(getattr(ws, "_ws", None)) if ws is not None else False,
        "kis_ws_url": getattr(ws, "ws_url", None) if ws is not None else None,
        "kis_ws_status": kis_ws_status,
        "trading_last_error": _trading_last_error,
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


@app.get("/ai/watches")
async def get_watches():
    import json as _json
    r = _components.get("redis") if _components else None
    if not r:
        return {"watches": {}}
    raw = r.get("ai:watches")
    return {"watches": _json.loads(raw) if raw else {}}


@app.get("/ai/indicators/{stock_code}")
async def get_indicators(stock_code: str):
    import json as _json
    r = _components.get("redis") if _components else None
    if not r:
        return {"stock_code": stock_code, "indicators": {}}
    raw = r.get(f"ai:indicators:{stock_code}")
    return {"stock_code": stock_code, "indicators": _json.loads(raw) if raw else {}}


@app.get("/ai/system/status")
async def system_status():
    import datetime as _dt
    r = _components.get("redis")
    redis_ok = False
    redis_keys = []
    if r:
        try:
            r.ping()
            redis_ok = True
            redis_keys = [k for k in r.keys("ai:*")]
        except Exception:
            pass

    ws_info = _components.get("ws")
    ws_status = {}
    if ws_info:
        ws_status = {
            "connected": getattr(ws_info, "connected", False),
            "subscriptions": list(getattr(ws_info, "_subscriptions", []) if isinstance(getattr(ws_info, "_subscriptions", []), list) else getattr(ws_info, "_subscriptions", {}).keys()),
            "last_message_at": str(getattr(ws_info, "_last_message_at", None)),
        }

    indicator_cache = _components.get("indicator_cache")
    cached_stocks = []
    if indicator_cache:
        cached_stocks = list(getattr(indicator_cache, "_candles", {}).keys())

    engine = _event_engine
    watches = {}
    if r:
        raw = r.get("ai:watches")
        if raw:
            import json as _j
            watches = _j.loads(raw)

    agent_history_len = 0
    if _agent:
        agent_history_len = len(getattr(_agent, "_chat_history", []))

    return {
        "timestamp": _dt.datetime.now().isoformat(),
        "server": "ok",
        "redis": {"ok": redis_ok, "ai_keys": redis_keys},
        "websocket": ws_status,
        "indicator_cache": {"cached_stocks": cached_stocks},
        "watches": {"count": len(watches), "stocks": list(watches.keys())},
        "agent": {"history_len": agent_history_len},
        "mode": _components.get("mode", "unknown"),
    }


@app.get("/ai/system/logs")
async def system_logs(level: str = "INFO", limit: int = 50, name: str = ""):
    logs = list(_log_buffer)
    if level != "ALL":
        order = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        min_idx = order.index(level) if level in order else 1
        logs = [l for l in logs if order.index(l["level"]) >= min_idx if l["level"] in order]
    if name:
        logs = [l for l in logs if name.lower() in l["name"].lower()]
    return {"logs": logs[-limit:], "total": len(logs)}


@app.get("/ai/candles/{stock_code}")
async def get_candles_for_chart(stock_code: str, candle_type: str = "daily", count: int = 30):
    overseas = _components.get("overseas")
    domestic = _components.get("domestic")
    if not overseas and not domestic:
        return {"stock_code": stock_code, "candles": []}
    try:
        from kis.constants import ExchangeCode
        from datetime import date, timedelta
        is_domestic = stock_code.isdigit()
        if is_domestic and domestic:
            df = domestic.get_daily_ohlcv(stock_code, count)
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
            {"datetime": str(row.get("datetime", idx))[:10],
             "open": float(row.get("open", 0)),
             "high": float(row.get("high", 0)),
             "low": float(row.get("low", 0)),
             "close": float(row.get("close", 0)),
             "volume": float(row.get("volume", 0))}
            for idx, row in df.iterrows()
        ]
        return {"stock_code": stock_code, "candle_type": candle_type, "candles": candles}
    except Exception as e:
        logger.exception("캔들 차트 조회 실패: %s", stock_code)
        return {"stock_code": stock_code, "candles": [], "error": str(e)}


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


@app.get("/signals")
@app.get("/api/command/signals")
def get_signals():
    market_data = _components.get("market_data")
    redis_client = _components.get("redis")
    if market_data is None:
        raise HTTPException(status_code=503, detail="초기화 중")

    signals: dict[str, dict] = {}
    prices = market_data.get_all_prices()
    watches = {}
    if redis_client is not None:
        try:
            import json
            raw = redis_client.get("ai:watches")
            watches = json.loads(raw) if raw else {}
        except Exception:
            watches = {}

    for code, data in prices.items():
        price = _to_float(data.get("current_price") or data.get("price"))
        if price <= 0:
            continue
        signals[code] = {
            "price": price,
            "source": "realtime",
            "stale": False,
            "resistance": None,
            "ema5": None,
            "ema20": None,
            "rsi": None,
            "candles": [],
            "updated_at": data.get("received_at") or datetime.now().isoformat(),
        }

    for code, watch in watches.items():
        signals.setdefault(code, {
            "price": _to_float(watch.get("baseline_price")),
            "source": "watch_baseline",
            "stale": True,
            "resistance": None,
            "ema5": None,
            "ema20": None,
            "rsi": None,
            "candles": [],
            "updated_at": watch.get("set_at") or datetime.now().isoformat(),
        })

    return signals


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
