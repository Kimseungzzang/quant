"""
FastAPI — AI 트레이더 백엔드 서버 (포트 8000)
엔드포인트는 routers/ 패키지에서 관리한다.
"""
import asyncio
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime

import asyncpg
import redis
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

from kis.auth import KISAuth
from kis.rest import KISRestClient
from kis.domestic import DomesticAPI
from kis.overseas import OverseasAPI
from kis.websocket import (
    KISWebSocket,
    parse_domestic_fill_notice,
    parse_overseas_fill_notice,
)
from kis.constants import WebSocketTRID

from trading.risk import RiskManager
from trading.order_manager import OrderManager, TradeLogger

from db.pg_writer import PGWriterSync

from collector.market_data import MarketDataCollector
from collector.account import AccountCollector

from events.detector import EventDetector
from events.engine import EventEngine

from ai.memory import AgentMemory
from ai.tools import ToolExecutor
from ai.agent import AIAgent
from ai.provider import create_provider

from utils import load_config, setup_logging
from routers import state
from routers.ai_routes import router as ai_router
from routers.trade_routes import router as trade_router
from routers.system_routes import router as system_router

logger = logging.getLogger(__name__)


# ── 알림 큐 (HTTP polling용) ─────────────────────────────────────────────
_notification_queue: list[dict] = []
_NOTIFICATION_MAX = 100


def _push_notification(msg: dict) -> None:
    _notification_queue.append(msg)
    if len(_notification_queue) > _NOTIFICATION_MAX:
        _notification_queue.pop(0)


# ── 브로드캐스트 ─────────────────────────────────────────────────────────

async def _broadcast(msg: dict) -> None:
    import time
    msg["_ts"] = time.time()
    if msg.get("type") in ("ai_message", "fill_notice", "error_notice"):
        _push_notification(msg)
    dead = set()
    for ws in list(state.ws_clients):  # snapshot — disconnect 시 set 변경 방지
        try:
            await ws.send_json(msg)
        except Exception:
            dead.add(ws)
    state.ws_clients.difference_update(dead)


def _on_ai_message(source: str, message: str) -> None:
    if source in ("chat", "tool"):
        return
    asyncio.get_event_loop().call_soon_threadsafe(
        lambda: asyncio.create_task(
            _broadcast({"type": "ai_message", "source": source, "message": message, "ts": datetime.now().isoformat()})
        )
    )


# ── 컴포넌트 초기화 ───────────────────────────────────────────────────────

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

    return dict(pg_pool=pg_pool, memory=memory, agent=agent, engine=engine, indicator_cache=indicator_cache)


# ── WebSocket 거래 루프 ───────────────────────────────────────────────────

async def _trading_loop(config: dict, comp: dict) -> None:
    order_mgr: OrderManager = comp["order_mgr"]
    market_data: MarketDataCollector = comp["market_data"]
    engine: EventEngine = comp["engine"]

    def on_domestic_price(tr_id, fields: list) -> None:
        from kis.websocket import parse_domestic_price as _parse
        parsed = _parse(fields)
        code = parsed.get("stock_code", "")
        if not code:
            return
        exchange = "UNIFIED" if str(tr_id) == str(WebSocketTRID.DOMESTIC_PRICE_UNIFIED) else (
            "NXT" if str(tr_id) == str(WebSocketTRID.DOMESTIC_PRICE_NXT) else "KRX"
        )
        tick = {
            "stock_code": code, "current_price": parsed.get("price", 0),
            "volume": parsed.get("vol", 0), "acml_volume": parsed.get("acml_vol", 0),
            "time": parsed.get("time", ""), "exchange": exchange,
            "price_source": "websocket",
            "stock_name": parsed.get("stock_name", code),
            "received_at": datetime.now().isoformat(),
        }
        market_data.on_price_tick(code, tick)
        order_mgr.on_price_update(code, tick["current_price"], signal=None)
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
            "volume": parsed.get("vol", 0), "acml_volume": parsed.get("acml_vol", 0),
            "time": parsed.get("time", ""), "exchange": exchange, "stock_name": code,
            "received_at": datetime.now().isoformat(),
        }
        market_data.on_price_tick(code, tick)
        order_mgr.on_price_update(code, tick["current_price"], signal=None)

    def on_fill(tr_id, fields: list) -> None:
        parser = parse_domestic_fill_notice if tr_id in (
            WebSocketTRID.DOMESTIC_FILL_PAPER, WebSocketTRID.DOMESTIC_FILL_LIVE,
        ) else parse_overseas_fill_notice
        order_mgr.on_order_notice(parser(fields))

    def _on_fill_broadcast(info: dict) -> None:
        side_kr = "매수" if info["side"] == "BUY" else "매도"
        status = "전량 체결" if info["fully_filled"] else "부분 체결"
        msg = (
            f"[{status}] {info.get('stock_name') or info['stock_code']} "
            f"{side_kr} {info['filled_qty']}주 @ {info['fill_price']:,.0f}원"
        )
        asyncio.get_event_loop().call_soon_threadsafe(
            lambda: asyncio.create_task(
                _broadcast({"type": "fill_notice", "message": msg, "info": info, "ts": datetime.now().isoformat()})
            )
        )

    def _on_error_broadcast(message: str) -> None:
        asyncio.get_event_loop().call_soon_threadsafe(
            lambda: asyncio.create_task(
                _broadcast({"type": "error_notice", "message": message})
            )
        )

    order_mgr.on_fill_callback = _on_fill_broadcast
    order_mgr.on_error_callback = _on_error_broadcast

    domestic_fill_trid = WebSocketTRID.DOMESTIC_FILL_PAPER if comp["auth"].is_paper else WebSocketTRID.DOMESTIC_FILL_LIVE
    overseas_fill_trid = WebSocketTRID.OVERSEAS_FILL_PAPER if comp["auth"].is_paper else WebSocketTRID.OVERSEAS_FILL_LIVE

    callbacks = {
        WebSocketTRID.DOMESTIC_PRICE: on_domestic_price,
        WebSocketTRID.DOMESTIC_PRICE_UNIFIED: on_domestic_price,
        WebSocketTRID.DOMESTIC_PRICE_NXT: on_domestic_price,
        WebSocketTRID.DOMESTIC_ASKBID: on_domestic_askbid,
        WebSocketTRID.DOMESTIC_ASKBID_UNIFIED: on_domestic_askbid,
        WebSocketTRID.DOMESTIC_ASKBID_NXT: on_domestic_askbid,
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
        for s in overseas_items if isinstance(s, dict) and s.get("code")
    }
    overseas_exchanges.setdefault("HPE", "NYS")

    try:
        raw_watches = comp["redis"].get("ai:watches")
        watches = json.loads(raw_watches) if raw_watches else {}
        watched_codes = list(watches.keys()) if isinstance(watches, dict) else []
    except Exception:
        watches, watched_codes = {}, []

    domestic_set, overseas_set = set(domestic_codes), set(overseas_codes)
    watched_domestic = [
        c for c in watched_codes
        if c in domestic_set or str(watches.get(c, {}).get("market", "")).lower() == "domestic"
    ]
    watched_overseas = [
        c for c in watched_codes
        if c in overseas_set or str(watches.get(c, {}).get("market", "")).lower() == "overseas"
    ]
    for code in watched_overseas:
        if exch := watches.get(code, {}).get("exchange"):
            overseas_exchanges[str(code).upper()] = str(exch).upper()

    domestic_codes = watched_domestic + [c for c in domestic_codes if c not in watched_codes]
    overseas_codes = watched_overseas + [c for c in overseas_codes if c not in watched_codes]
    max_ws = int(config.get("kis", {}).get("max_ws_subscriptions", 3))

    if watched_codes:
        domestic_codes = [c for c in domestic_codes if c in watched_codes]
        overseas_codes = [c for c in overseas_codes if c in watched_codes]

    if max_ws > 0:
        selected = [("domestic", c) for c in domestic_codes] + [("overseas", c) for c in overseas_codes]
        selected = selected[:max_ws]
        domestic_codes = [c for m, c in selected if m == "domestic"]
        overseas_codes = [c for m, c in selected if m == "overseas"]

    ws = KISWebSocket(comp["auth"])
    comp["ws"] = ws
    executor = getattr(comp.get("agent"), "_executor", None)
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
    while not stop.is_set():
        try:
            state.trading_last_error = None
            await _trading_loop(config, comp)
            if not stop.is_set():
                state.trading_last_error = "KIS WebSocket loop exited unexpectedly"
                logger.warning("%s — restarting in 5s", state.trading_last_error)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            state.trading_last_error = f"{type(e).__name__}: {e}"
            logger.exception("KIS WebSocket loop failed — restarting in 5s")
            await _broadcast({"type": "error_notice", "message": f"KIS WebSocket 연결 오류 (5초 후 재연결): {e}"})
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass


# ── 스케줄러 ──────────────────────────────────────────────────────────────

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
                logger.warning("live 자동매매 차단: %s", key)
                ran.add(run_key)
                return
            ran.add(run_key)
            try:
                logger.info("자동매매 스케줄 실행: %s", key)
                await agent.chat(prompt)
            except Exception:
                logger.exception("자동매매 스케줄 실패: %s", key)

        schedules = [
            (schedule_cfg.get("domestic_analysis_time", "09:00"), "domestic-analysis",
             "Run the autonomous Korean domestic market trading process now. "
             "Analyze KOSPI/KRX conditions, portfolio, candidates, and charts. "
             "If risk and setup are aligned, decide allocation percentage and place real orders automatically. "
             "Otherwise set concrete detecting/watch rules. Save the plan and memo. Respond in Korean only."),
            (schedule_cfg.get("us_analysis_time", "22:30"), "us-analysis",
             "Run the autonomous US market trading process now. "
             "Analyze Nasdaq/S&P 500/Dow, US megacap and AI/semiconductor candidates, "
             "portfolio cash, and candidate charts. If risk and setup are aligned, "
             "decide allocation percentage and place real orders automatically. "
             "Otherwise set concrete detecting/watch rules. Save the plan and memo. Respond in Korean only."),
            (schedule_cfg.get("domestic_trading_end_time", "15:20"), "domestic-close",
             "Korean domestic market trading window is ending. Review open domestic positions, "
             "pending orders, and watches. Manage risk, save a memo, and respond in Korean only."),
            (schedule_cfg.get("us_trading_end_time", "05:00"), "us-close",
             "US market trading window is ending. Review open US positions, pending orders, "
             "and watches. Manage risk, save a memo, and respond in Korean only."),
        ]
        for time_str, key, prompt in schedules:
            if hhmm == time_str:
                await run_once(key, prompt)
        await asyncio.sleep(20)


# ── 계좌 폴러 ─────────────────────────────────────────────────────────────

def _start_pollers(comp: dict, stop: threading.Event) -> None:
    def _account_poll():
        while not stop.is_set():
            try:
                dom = comp["domestic"].get_balance()
                comp["account"].update_balance("domestic", dom)
                last_prices = comp["order_mgr"].get_last_prices()
                positions = [
                    {
                        "stock_code": p.stock_code, "stock_name": p.name,
                        "market": "domestic" if p.is_domestic() else "overseas",
                        "quantity": p.qty, "avg_price": p.entry_price,
                        "current_price": last_prices.get(p.stock_code, p.entry_price),
                        "unrealized_pct": round(
                            (last_prices.get(p.stock_code, p.entry_price) - p.entry_price) / p.entry_price * 100, 2
                        ) if p.entry_price else 0,
                    }
                    for p in comp["order_mgr"].get_open_positions().values()
                ]
                comp["account"].update_positions(positions)
            except Exception:
                logger.exception("계좌 폴링 실패")
            stop.wait(60)

    threading.Thread(target=_account_poll, daemon=True, name="account-poller").start()


# ── Paper 체결 폴링 (paper WS 서버가 H0STCNI9를 지원하지 않으므로 REST 폴링) ──

async def _paper_fill_poll_loop(comp: dict, stop: asyncio.Event) -> None:
    """paper 모드: 30초마다 KIS REST로 국내+해외 주문체결내역 조회 → 체결 반영."""
    order_mgr: OrderManager = comp["order_mgr"]
    domestic = comp.get("domestic")
    overseas = comp.get("overseas")
    loop = asyncio.get_event_loop()
    while not stop.is_set():
        try:
            if order_mgr.get_pending_order_rows():
                rows: list[dict] = []
                if domestic:
                    try:
                        rows += await loop.run_in_executor(None, domestic.get_daily_orders)
                    except Exception as e:
                        logger.warning("국내 체결 폴링 실패", exc_info=True)
                        await _broadcast({"type": "error_notice", "message": f"체결 조회 실패 (국내): {e}"})
                if overseas:
                    try:
                        rows += await loop.run_in_executor(None, overseas.get_daily_orders)
                    except Exception as e:
                        logger.warning("해외 체결 폴링 실패", exc_info=True)
                        await _broadcast({"type": "error_notice", "message": f"체결 조회 실패 (해외): {e}"})
                if rows:
                    matched = order_mgr.reconcile_order_rows(rows)
                    if matched:
                        logger.info("paper 체결 폴링: %d건 반영", matched)
        except Exception as e:
            logger.exception("paper 체결 폴링 실패")
            await _broadcast({"type": "error_notice", "message": f"체결 폴링 오류: {e}"})
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass


# ── Lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    setup_logging(config)
    logging.getLogger().addHandler(state.buf_handler)

    sync_comp = _build_sync_components(config)
    async_comp = await _build_async_components(config, sync_comp)
    state.components = {**sync_comp, **async_comp}
    state.agent = async_comp["agent"]
    state.event_engine = async_comp["engine"]

    stop_event = threading.Event()
    async_stop = asyncio.Event()

    _start_pollers(state.components, stop_event)
    await state.event_engine.run()
    asyncio.create_task(_morning_brief_loop(state.agent, config, async_stop))
    asyncio.create_task(_auto_trading_schedule_loop(state.agent, config, async_stop))
    if config.get("mode") == "paper":
        asyncio.create_task(_paper_fill_poll_loop(state.components, async_stop))
    state.trading_task = asyncio.create_task(
        _trading_loop_supervisor(config, state.components, async_stop)
    )

    logger.info("AI 트레이더 백엔드 준비 완료")
    yield

    async_stop.set()
    stop_event.set()
    if state.trading_task:
        state.trading_task.cancel()
    await state.event_engine.stop()
    await state.components["pg_pool"].close()
    logger.info("AI 트레이더 백엔드 종료")


# ── FastAPI 앱 ────────────────────────────────────────────────────────────

app = FastAPI(title="AI Trader", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ai_router)
app.include_router(trade_router)
app.include_router(system_router)


@app.get("/ai/notifications")
def get_notifications(since: float = 0):
    """Jarvis가 폴링해서 새 알림을 가져가는 엔드포인트."""
    return [m for m in _notification_queue if m.get("_ts", 0) > since]


@app.websocket("/ws/stream")
async def ws_stream(ws: WebSocket):
    await ws.accept()
    state.ws_clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        state.ws_clients.discard(ws)
