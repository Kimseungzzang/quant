import json
import logging
from datetime import datetime
from typing import Any

import requests
from bs4 import BeautifulSoup

import redis

from ai.memory import AgentMemory
from collector.account import AccountCollector
from collector.market_data import MarketDataCollector
from trading.order_manager import OrderManager

_WATCHES_KEY = "ai:watches"

logger = logging.getLogger(__name__)

_NEWS_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


TOOL_DEFINITIONS = [
    {
        "name": "get_price",
        "description": "특정 종목의 현재가, 거래량, 등락률을 조회합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "stock_code": {"type": "string", "description": "종목 코드 (예: 005930, NVDA)"},
            },
            "required": ["stock_code"],
        },
    },
    {
        "name": "get_orderbook",
        "description": "특정 종목의 실시간 호가(매수/매도 잔량)를 조회합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "stock_code": {"type": "string", "description": "종목 코드"},
            },
            "required": ["stock_code"],
        },
    },
    {
        "name": "get_portfolio",
        "description": "현재 보유 포지션과 계좌 잔고를 조회합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "market": {"type": "string", "enum": ["domestic", "overseas", "both"], "default": "both"},
            },
        },
    },
    {
        "name": "get_rankings",
        "description": "거래량 또는 거래대금 상위 종목 순위를 조회합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "rank_type": {"type": "string", "enum": ["volume", "value"], "description": "거래량 또는 거래대금"},
                "market": {"type": "string", "enum": ["domestic", "overseas"], "default": "domestic"},
            },
            "required": ["rank_type"],
        },
    },
    {
        "name": "get_candles",
        "description": "특정 종목의 최근 분봉 또는 일봉 차트 데이터를 조회합니다. 추세 파악에 사용하세요.",
        "input_schema": {
            "type": "object",
            "properties": {
                "stock_code": {"type": "string", "description": "종목 코드"},
                "market": {"type": "string", "enum": ["domestic", "overseas"], "default": "domestic"},
                "candle_type": {"type": "string", "enum": ["minute", "daily"], "default": "minute"},
                "count": {"type": "integer", "description": "가져올 봉 개수", "default": 30},
            },
            "required": ["stock_code"],
        },
    },
    {
        "name": "get_market_summary",
        "description": "KOSPI, KOSDAQ, 나스닥 등 주요 지수 현황과 시장 레짐을 조회합니다.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "search_news",
        "description": "종목명 또는 키워드로 최신 뉴스를 검색합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색어 (예: '삼성전자 실적', '반도체 섹터 뉴스')"},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "place_order",
        "description": "주식 매수 또는 매도 주문을 실행합니다. 주문 전 반드시 잔고와 포지션을 확인하세요.",
        "input_schema": {
            "type": "object",
            "properties": {
                "stock_code": {"type": "string", "description": "종목 코드"},
                "stock_name": {"type": "string", "description": "종목명"},
                "side": {"type": "string", "enum": ["BUY", "SELL"]},
                "quantity": {"type": "integer", "description": "주문 수량"},
                "price": {"type": "number", "description": "주문 가격 (0이면 시장가)"},
                "reason": {"type": "string", "description": "매매 이유 (필수)"},
            },
            "required": ["stock_code", "stock_name", "side", "quantity", "reason"],
        },
    },
    {
        "name": "cancel_order",
        "description": "미체결 주문을 취소합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "취소할 주문 ID"},
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "save_plan",
        "description": "오늘의 시장 전망과 매매 전략을 저장합니다. 아침 브리핑 시 사용.",
        "input_schema": {
            "type": "object",
            "properties": {
                "market_outlook": {"type": "string", "description": "오늘 시장 전반 전망"},
                "watch_stocks": {
                    "type": "array",
                    "description": "주목할 종목 목록",
                    "items": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string"},
                            "name": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                    },
                },
                "strategy": {"type": "string", "description": "오늘 전략 (공격적/보수적/관망 등)"},
            },
            "required": ["market_outlook", "watch_stocks", "strategy"],
        },
    },
    {
        "name": "save_memo",
        "description": "판단 내용, 분석 메모, 특이사항을 기록합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "기록할 내용"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "get_history",
        "description": (
            "특정 종목 또는 전체에 대한 과거 판단 이력을 조회합니다. "
            "'내가 왜 이 종목을 샀지?', '이전에 어떤 결정을 했지?' 등을 확인할 때 사용하세요."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "stock_code": {"type": "string", "description": "종목 코드. 비워두면 전체 이력"},
                "limit": {"type": "integer", "default": 20, "description": "가져올 최대 개수"},
                "action_filter": {
                    "type": "string",
                    "description": "BUY/SELL/HOLD 등 특정 액션만 필터",
                },
            },
        },
    },
    {
        "name": "set_watch",
        "description": (
            "특정 종목에 대한 감시 조건을 설정합니다. 조건 충족 시 AI가 다시 호출됩니다. "
            "매수 후 손절/익절 조건 설정, 주목 종목 모니터링 등에 사용하세요."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "stock_code": {"type": "string"},
                "stock_name": {"type": "string"},
                "market": {"type": "string", "enum": ["domestic", "overseas"]},
                "conditions": {
                    "type": "array",
                    "description": "감시 조건 목록",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["price_change", "price_above", "price_below", "volume_spike"],
                                "description": "price_change: 현재가 대비 ±X% | price_above: X원 이상 | price_below: X원 이하 | volume_spike: 평균 거래량의 X배",
                            },
                            "threshold": {"type": "number"},
                            "note": {"type": "string", "description": "이 조건을 거는 이유"},
                        },
                        "required": ["type", "threshold"],
                    },
                },
            },
            "required": ["stock_code", "stock_name", "market", "conditions"],
        },
    },
    {
        "name": "clear_watch",
        "description": "특정 종목의 감시 조건을 해제합니다. 매도 완료 후 사용하세요.",
        "input_schema": {
            "type": "object",
            "properties": {
                "stock_code": {"type": "string"},
            },
            "required": ["stock_code"],
        },
    },
    {
        "name": "list_watches",
        "description": "현재 설정된 모든 감시 조건을 조회합니다.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]


class ToolExecutor:
    def __init__(
        self,
        market_data: MarketDataCollector,
        account: AccountCollector,
        order_manager: OrderManager,
        memory: AgentMemory,
        redis_client: redis.Redis | None = None,
        ws: Any = None,
        domestic_api: Any = None,
        overseas_api: Any = None,
        regime_fn: Any = None,
    ):
        self._market = market_data
        self._account = account
        self._order_manager = order_manager
        self._memory = memory
        self._r = redis_client
        self._ws = ws
        self._domestic = domestic_api
        self._overseas = overseas_api
        self._regime_fn = regime_fn

    async def execute(self, tool_name: str, tool_input: dict) -> str:
        logger.info("도구 실행: %s %s", tool_name, tool_input)
        try:
            match tool_name:
                case "get_price":
                    return self._get_price(tool_input["stock_code"])
                case "get_orderbook":
                    return self._get_orderbook(tool_input["stock_code"])
                case "get_portfolio":
                    return self._get_portfolio(tool_input.get("market", "both"))
                case "get_rankings":
                    return self._get_rankings(tool_input["rank_type"], tool_input.get("market", "domestic"))
                case "get_candles":
                    return await self._get_candles(
                        tool_input["stock_code"],
                        tool_input.get("market", "domestic"),
                        tool_input.get("candle_type", "minute"),
                        tool_input.get("count", 30),
                    )
                case "get_market_summary":
                    return self._get_market_summary()
                case "search_news":
                    return self._search_news(tool_input["query"], tool_input.get("max_results", 5))
                case "place_order":
                    return await self._place_order(tool_input)
                case "cancel_order":
                    return self._cancel_order(tool_input["order_id"])
                case "save_plan":
                    return await self._save_plan(tool_input)
                case "save_memo":
                    return await self._save_memo(tool_input["content"])
                case "get_history":
                    return await self._get_history(
                        tool_input.get("stock_code", ""),
                        tool_input.get("limit", 20),
                        tool_input.get("action_filter", ""),
                    )
                case "set_watch":
                    return self._set_watch(tool_input)
                case "clear_watch":
                    return self._clear_watch(tool_input["stock_code"])
                case "list_watches":
                    return self._list_watches()
                case _:
                    return json.dumps({"error": f"알 수 없는 도구: {tool_name}"})
        except Exception as e:
            logger.exception("도구 실행 오류: %s", tool_name)
            return json.dumps({"error": str(e)})

    def _get_price(self, stock_code: str) -> str:
        data = self._market.get_price(stock_code)
        if not data:
            return json.dumps({"error": f"{stock_code} 가격 데이터 없음"})
        return json.dumps(data, ensure_ascii=False)

    def _get_orderbook(self, stock_code: str) -> str:
        data = self._market.get_orderbook(stock_code)
        if not data:
            return json.dumps({"error": f"{stock_code} 호가 데이터 없음"})
        return json.dumps(data, ensure_ascii=False)

    def _get_portfolio(self, market: str) -> str:
        positions = self._account.get_positions()
        domestic_balance = self._account.get_balance("domestic")
        overseas_balance = self._account.get_balance("overseas")
        result = {
            "positions": positions,
            "balance": {
                "domestic": domestic_balance,
                "overseas": overseas_balance,
            },
        }
        return json.dumps(result, ensure_ascii=False, default=str)

    def _get_rankings(self, rank_type: str, market: str) -> str:
        try:
            if market == "overseas":
                from kis.constants import ExchangeCode
                items = self._overseas.get_volume_ranking(ExchangeCode.NASDAQ) if self._overseas else []
            else:
                if rank_type == "volume":
                    items = self._domestic.get_volume_rank(top_n=30) if self._domestic else []
                else:
                    items = self._domestic.get_trading_value_rank(top_n=30) if self._domestic else []
        except Exception as e:
            return json.dumps({"error": f"순위 조회 실패: {e}"})
        return json.dumps({"rank_type": rank_type, "market": market, "items": items}, ensure_ascii=False)

    async def _get_candles(self, stock_code: str, market: str, candle_type: str, count: int) -> str:
        import asyncio
        loop = asyncio.get_event_loop()
        try:
            if candle_type == "daily":
                from datetime import date, timedelta
                end = date.today()
                start = end - timedelta(days=count * 2)
                if market == "overseas":
                    from kis.constants import ExchangeCode
                    df = await loop.run_in_executor(
                        None, lambda: self._overseas.get_daily_ohlcv(stock_code, ExchangeCode.NASDAQ, start, end)
                    )
                else:
                    df = await loop.run_in_executor(
                        None, lambda: self._domestic.get_daily_ohlcv(stock_code, start, end)
                    )
            else:
                if market == "overseas":
                    df = await loop.run_in_executor(
                        None, lambda: self._overseas.get_historical_minute_ohlcv(stock_code, lookback_days=3)
                    )
                else:
                    df = await loop.run_in_executor(
                        None, lambda: self._domestic.get_historical_minute_ohlcv(stock_code, lookback_days=3, candle_minutes=1)
                    )
            if df is None or df.empty:
                return json.dumps({"error": f"{stock_code} 차트 데이터 없음"})
            tail = df.tail(count)
            records = tail[["datetime", "open", "high", "low", "close", "volume"]].to_dict("records")
            return json.dumps({"stock_code": stock_code, "candle_type": candle_type, "count": len(records), "candles": records}, ensure_ascii=False, default=str)
        except Exception as e:
            return json.dumps({"error": f"차트 조회 실패: {e}"})

    def _get_market_summary(self) -> str:
        regime = self._regime_fn() if self._regime_fn else {}
        all_prices = self._market.get_all_prices()
        summary = {
            "regime": regime,
            "tracking_count": len(all_prices),
            "timestamp": datetime.now().isoformat(),
        }
        return json.dumps(summary, ensure_ascii=False, default=str)

    def _search_news(self, query: str, max_results: int) -> str:
        try:
            url = f"https://search.naver.com/search.naver?where=news&query={requests.utils.quote(query)}&sort=1"
            resp = requests.get(url, headers=_NEWS_HEADERS, timeout=8)
            resp.encoding = "utf-8"
            soup = BeautifulSoup(resp.text, "lxml")
            articles = []
            for item in soup.select("div.news_wrap")[:max_results]:
                title_tag = item.select_one("a.news_tit")
                desc_tag = item.select_one("div.dsc_wrap")
                press_tag = item.select_one("a.info.press")
                date_tag = item.select_one("span.info")
                if title_tag:
                    articles.append({
                        "title": title_tag.get_text(strip=True),
                        "description": desc_tag.get_text(strip=True)[:200] if desc_tag else "",
                        "press": press_tag.get_text(strip=True) if press_tag else "",
                        "date": date_tag.get_text(strip=True) if date_tag else "",
                    })
            return json.dumps({"query": query, "articles": articles}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"뉴스 검색 실패: {e}"})

    async def _place_order(self, inp: dict) -> str:
        stock_code = inp["stock_code"]
        stock_name = inp["stock_name"]
        side = inp["side"]
        quantity = inp["quantity"]
        price = inp.get("price", 0)
        reason = inp["reason"]

        pending = self._order_manager.get_pending_orders()
        if stock_code in pending:
            return json.dumps({"error": f"{stock_code} 이미 미체결 주문 존재"})

        positions = self._order_manager.get_open_positions()
        if side == "BUY":
            exchange = "KRX" if len(stock_code) == 6 and stock_code.isdigit() else "NAS"
            from trading.order_manager import TradeSignal
            signal = TradeSignal(
                action="BUY",
                stock_code=stock_code,
                stock_name=stock_name,
                exchange=exchange,
                confidence=1.0,
                reason=reason,
            )
            result = self._order_manager.open_position(
                stock_code=stock_code,
                name=stock_name,
                exchange=exchange,
                price=price,
                signal=signal,
            )
        else:
            if stock_code not in positions:
                return json.dumps({"error": f"{stock_code} 보유 포지션 없음"})
            result = self._order_manager.close_position(
                stock_code=stock_code,
                current_price=price,
                reason=reason,
            )

        return json.dumps({"status": "주문 요청 완료", "side": side, "stock_code": stock_code, "quantity": quantity, "result": str(result)}, ensure_ascii=False)

    def _cancel_order(self, order_id: str) -> str:
        return json.dumps({"status": "취소 기능 미구현 — order_manager에 추가 필요", "order_id": order_id})

    async def _save_plan(self, inp: dict) -> str:
        session_id = await self._memory.save_plan(
            market_outlook=inp["market_outlook"],
            watch_stocks=inp["watch_stocks"],
            strategy=inp["strategy"],
        )
        return json.dumps({"status": "계획 저장 완료", "session_id": session_id}, ensure_ascii=False)

    async def _save_memo(self, content: str) -> str:
        await self._memory.save_memo(content)
        return json.dumps({"status": "메모 저장 완료"})

    async def _get_history(self, stock_code: str, limit: int, action_filter: str) -> str:
        decisions = await self._memory.get_decisions(
            stock_code=stock_code or None,
            limit=limit,
            action_filter=action_filter or None,
        )
        memos = await self._memory.get_recent_memos(limit=5)
        return json.dumps({
            "decisions": decisions,
            "recent_memos": memos,
        }, ensure_ascii=False, default=str)

    def _set_watch(self, inp: dict) -> str:
        if not self._r:
            return json.dumps({"error": "Redis 미연결"})
        stock_code = inp["stock_code"]
        market = inp["market"]

        price_data = self._market.get_price(stock_code)
        baseline_price = float(price_data.get("current_price", 0)) if price_data else 0.0
        baseline_volume = float(price_data.get("acml_volume", 0)) if price_data else 0.0

        watches = self._load_watches()
        watches[stock_code] = {
            "stock_code": stock_code,
            "stock_name": inp["stock_name"],
            "market": market,
            "conditions": inp["conditions"],
            "baseline_price": baseline_price,
            "baseline_volume": baseline_volume,
            "set_at": datetime.now().isoformat(),
            "triggered_types": [],
        }
        self._r.set(_WATCHES_KEY, json.dumps(watches, ensure_ascii=False))

        ws_subscribed = False
        if self._ws is not None:
            import asyncio
            from kis.constants import WebSocketTRID
            loop = asyncio.get_event_loop()

            def _on_price(tr_id, fields):
                from kis.websocket import parse_domestic_price, parse_overseas_price
                parsed = parse_domestic_price(fields) if market == "domestic" else parse_overseas_price(fields)
                code = parsed.get("stock_code", "")
                if code:
                    self._market.on_price_tick(code, {
                        "stock_code": code,
                        "current_price": parsed.get("price", 0),
                        "volume": parsed.get("vol", 0),
                        "time": parsed.get("time", ""),
                        "exchange": "KRX" if market == "domestic" else "NAS",
                        "stock_name": inp["stock_name"],
                    })

            tr_id = WebSocketTRID.DOMESTIC_PRICE if market == "domestic" else WebSocketTRID.OVERSEAS_PRICE
            tr_key = stock_code if market == "domestic" else f"DNAS{stock_code}"
            future = asyncio.run_coroutine_threadsafe(
                self._ws.add_live_subscription(tr_id, tr_key, _on_price), loop
            )
            try:
                ws_subscribed = future.result(timeout=3)
            except Exception:
                ws_subscribed = False

        return json.dumps({
            "status": "감시 설정 완료",
            "stock_code": stock_code,
            "baseline_price": baseline_price,
            "ws_subscribed": ws_subscribed,
            "conditions": inp["conditions"],
        }, ensure_ascii=False)

    def _clear_watch(self, stock_code: str) -> str:
        if not self._r:
            return json.dumps({"error": "Redis 미연결"})
        watches = self._load_watches()
        if stock_code in watches:
            del watches[stock_code]
            self._r.set(_WATCHES_KEY, json.dumps(watches, ensure_ascii=False))
            return json.dumps({"status": "감시 해제 완료", "stock_code": stock_code})
        return json.dumps({"status": "감시 없음", "stock_code": stock_code})

    def _list_watches(self) -> str:
        watches = self._load_watches()
        return json.dumps({"watches": list(watches.values()), "count": len(watches)}, ensure_ascii=False, default=str)

    def _load_watches(self) -> dict:
        if not self._r:
            return {}
        raw = self._r.get(_WATCHES_KEY)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return {}
