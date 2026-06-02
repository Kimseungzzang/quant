import asyncio
import logging
import os
import time
import threading
from datetime import datetime
from pathlib import Path

import redis
import yaml
from dotenv import load_dotenv

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

from db.pg_writer import PGWriterSync
import asyncpg

from analysis.market_regime import MarketRegimeDetector
from trading.risk import RiskManager
from trading.order_manager import OrderManager, TradeLogger

from collector.market_data import MarketDataCollector
from collector.account import AccountCollector

from events.types import Market, EventKind, MarketEvent
from events.detector import EventDetector
from events.engine import EventEngine

from ai.memory import AgentMemory
from ai.tools import ToolExecutor
from ai.agent import AIAgent

logger = logging.getLogger(__name__)


_ACCOUNT_POLL_SEC = 60
_BRIEF_HOUR = 8
_BRIEF_MINUTE = 30


# ── 분봉 집계기 ───────────────────────────────────────────────────────

class CandleAggregator:
    """실시간 틱을 N분봉으로 집계."""

    def __init__(self, period_minutes: int, max_candles: int = 300):
        self.period = period_minutes
        self.max_candles = max_candles
        self._current: dict | None = None
        self._completed: list[dict] = []

    def update(self, tick: dict) -> bool:
        time_str = str(tick.get("time", "") or "")
        if len(time_str) < 4:
            return False
        try:
            h = int(time_str[0:2])
            m = int(time_str[2:4])
            price = float(tick.get("price", 0) or 0)
            vol = float(tick.get("vol", 0) or 0)
        except (ValueError, TypeError):
            return False
        if price <= 0:
            return False

        slot = (h * 60 + m) // self.period
        bucket_minute = slot * self.period
        candle_dt = datetime.now().replace(
            hour=bucket_minute // 60, minute=bucket_minute % 60,
            second=0, microsecond=0,
        )
        completed = False
        if self._current is None or self._current["slot"] != slot:
            if self._current is not None:
                c = self._current
                self._completed.append({
                    "datetime": c["datetime"],
                    "open": c["open"], "high": c["high"],
                    "low": c["low"], "close": c["close"], "volume": c["volume"],
                })
                if len(self._completed) > self.max_candles:
                    self._completed.pop(0)
                completed = True
            self._current = {
                "slot": slot, "datetime": candle_dt, "open": price,
                "high": price, "low": price, "close": price, "volume": vol,
            }
        else:
            self._current["high"] = max(self._current["high"], price)
            self._current["low"] = min(self._current["low"], price)
            self._current["close"] = price
            self._current["volume"] += vol
        return completed

    def get_df(self):
        import pandas as pd
        return pd.DataFrame(self._completed) if self._completed else pd.DataFrame()


# ── 설정 / 로깅 ───────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def setup_logging(config: dict) -> None:
    log_cfg = config.get("logging", {})
    log_level = getattr(logging, log_cfg.get("level", "INFO"))
    log_file = log_cfg.get("file", "data/trading.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


# ── 컴포넌트 초기화 ───────────────────────────────────────────────────

def build_components(config: dict) -> dict:
    redis_cfg = config.get("redis", {})
    redis_client = redis.Redis(
        host=redis_cfg.get("host", "localhost"),
        port=redis_cfg.get("port", 6379),
        db=redis_cfg.get("db", 0),
        decode_responses=False,
    )

    auth = KISAuth(config)
    client = KISRestClient(auth)
    domestic = DomesticAPI(client, config)
    overseas = OverseasAPI(client, config)

    risk = RiskManager(config)
    pg = PGWriterSync()
    order_mgr = OrderManager(
        domestic, overseas, risk,
        trade_logger=TradeLogger(),
        pg=pg,
        mode=config["mode"],
    )

    market_data = MarketDataCollector(redis_client)
    account = AccountCollector(redis_client)

    regime_detector = MarketRegimeDetector(domestic)

    return dict(
        config=config,
        auth=auth,
        domestic=domestic,
        overseas=overseas,
        risk=risk,
        order_mgr=order_mgr,
        redis=redis_client,
        market_data=market_data,
        account=account,
        regime_detector=regime_detector,
    )


def _account_poller(comp: dict, stop_event: threading.Event) -> None:
    domestic: DomesticAPI = comp["domestic"]
    overseas: OverseasAPI = comp["overseas"]
    order_mgr: OrderManager = comp["order_mgr"]
    account: AccountCollector = comp["account"]
    config = comp["config"]
    mode = config["mode"]

    while not stop_event.is_set():
        try:
            dom_balance = domestic.get_balance()
            account.update_balance("domestic", dom_balance)

            positions = [
                {
                    "stock_code": p.stock_code,
                    "stock_name": p.name,
                    "market": "domestic" if p.is_domestic() else "overseas",
                    "quantity": p.qty,
                    "avg_price": p.entry_price,
                    "current_price": p.current_price,
                    "unrealized_pct": round(
                        (p.current_price - p.entry_price) / p.entry_price * 100, 2
                    ) if p.entry_price else 0,
                }
                for p in order_mgr.get_open_positions().values()
            ]
            account.update_positions(positions)
        except Exception:
            logger.exception("계좌 폴링 실패")
        stop_event.wait(_ACCOUNT_POLL_SEC)


# ── WebSocket 콜백 ────────────────────────────────────────────────────

def make_ws_callbacks(comp: dict, aggregators: dict, event_engine: EventEngine):
    market_data: MarketDataCollector = comp["market_data"]
    order_mgr: OrderManager = comp["order_mgr"]
    loop = asyncio.get_event_loop()

    def on_domestic_price(raw: dict) -> None:
        parsed = parse_domestic_price(list(raw.values()) if isinstance(raw, dict) else raw)
        code = parsed.get("stock_code", "")
        if not code:
            return
        tick = {
            "stock_code": code,
            "current_price": parsed.get("price", 0),
            "volume": parsed.get("vol", 0),
            "time": parsed.get("time", ""),
            "exchange": "KRX",
            "stock_name": parsed.get("stock_name", code),
        }
        market_data.on_price_tick(code, tick)
        if code in aggregators:
            aggregators[code].update({"price": tick["current_price"], "vol": tick["volume"], "time": tick["time"]})
        order_mgr.record_price(code, tick["current_price"])

    def on_domestic_askbid(raw: dict) -> None:
        parsed = parse_domestic_askbid(list(raw.values()) if isinstance(raw, dict) else raw)
        code = parsed.get("stock_code", "")
        if code:
            market_data.on_orderbook_tick(code, parsed)

    def on_overseas_price(raw: dict) -> None:
        parsed = parse_overseas_price(list(raw.values()) if isinstance(raw, dict) else raw)
        code = parsed.get("stock_code", "")
        if not code:
            return
        tick = {
            "stock_code": code,
            "current_price": parsed.get("price", 0),
            "volume": parsed.get("vol", 0),
            "time": parsed.get("time", ""),
            "exchange": "NAS",
            "stock_name": code,
        }
        market_data.on_price_tick(code, tick)
        order_mgr.record_price(code, tick["current_price"])

    def on_fill_notice(parsed: dict) -> None:
        order_mgr.on_order_notice(parsed)

    return {
        WebSocketTRID.DOMESTIC_PRICE: on_domestic_price,
        WebSocketTRID.DOMESTIC_ASKBID: on_domestic_askbid,
        WebSocketTRID.OVERSEAS_PRICE: on_overseas_price,
        WebSocketTRID.DOMESTIC_FILL: on_fill_notice,
        WebSocketTRID.OVERSEAS_FILL: on_fill_notice,
    }


# ── 아침 브리핑 스케줄러 ─────────────────────────────────────────────

async def _morning_brief_scheduler(agent: AIAgent, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        now = datetime.now()
        if now.hour == _BRIEF_HOUR and now.minute == _BRIEF_MINUTE:
            try:
                await agent.morning_brief()
            except Exception:
                logger.exception("아침 브리핑 실패")
            await asyncio.sleep(60)
        await asyncio.sleep(30)


# ── 메인 ─────────────────────────────────────────────────────────────

async def main_async(config_path: str = "config.yaml") -> None:
    config = load_config(config_path)
    setup_logging(config)
    comp = build_components(config)

    logger.info("AI 트레이더 시작 (mode=%s)", config["mode"])

    pg_pool = await asyncpg.create_pool(
        host=config.get("database", {}).get("host", "localhost"),
        port=config.get("database", {}).get("port", 5432),
        database=config.get("database", {}).get("name", "quant_trading"),
        user=config.get("database", {}).get("user", os.getenv("USER")),
        password=config.get("database", {}).get("password") or None,
        min_size=2,
        max_size=10,
    )

    memory = AgentMemory(pg_pool)

    def on_message(source: str, message: str) -> None:
        logger.info("[AI:%s] %s", source, message[:300])

    regime_fn = lambda: comp["regime_detector"].detect().__dict__ if hasattr(comp["regime_detector"].detect(), "__dict__") else {}

    tool_executor = ToolExecutor(
        market_data=comp["market_data"],
        account=comp["account"],
        order_manager=comp["order_mgr"],
        memory=memory,
        domestic_api=comp["domestic"],
        overseas_api=comp["overseas"],
        regime_fn=regime_fn,
    )

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    agent = AIAgent(
        api_key=api_key,
        tool_executor=tool_executor,
        memory=memory,
        on_message=on_message,
    )

    detector = EventDetector(comp["market_data"], comp["redis"])
    engine = EventEngine(detector)
    engine.register(agent.handle_event)

    aggregators: dict[str, CandleAggregator] = {}
    callbacks = make_ws_callbacks(comp, aggregators, engine)

    stop_event = threading.Event()
    threading.Thread(target=_account_poller, args=(comp, stop_event), daemon=True).start()

    async_stop = asyncio.Event()

    await engine.run()
    asyncio.create_task(_morning_brief_scheduler(agent, async_stop))

    logger.info("시스템 준비 완료. WebSocket 연결 중...")

    ws = KISWebSocket(comp["auth"])

    universe = config.get("universe", {})
    domestic_codes = [s["code"] for s in universe.get("domestic", {}).get("stocks", [])]
    overseas_codes = [s["code"] for s in universe.get("overseas", {}).get("stocks", [])]

    for code in domestic_codes:
        aggregators[code] = CandleAggregator(period_minutes=1)

    try:
        await ws.connect_and_subscribe(
            domestic_codes=domestic_codes,
            overseas_codes=overseas_codes,
            callbacks=callbacks,
        )
    except KeyboardInterrupt:
        logger.info("종료 신호 수신")
    finally:
        stop_event.set()
        async_stop.set()
        await engine.stop()
        await pg_pool.close()
        logger.info("AI 트레이더 종료")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="AI Trader")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    asyncio.run(main_async(args.config))


if __name__ == "__main__":
    main()
