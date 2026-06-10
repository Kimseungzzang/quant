import json
import logging
from datetime import datetime, time as dtime
from typing import Any
from zoneinfo import ZoneInfo

import requests
import redis

from ai.memory import AgentMemory
from collector.account import AccountCollector
from collector.market_data import MarketDataCollector
from trading.order_manager import OrderManager

_WATCHES_KEY = "ai:watches"

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")
_NY = ZoneInfo("America/New_York")


def _to_float(value) -> float:
    try:
        return float(str(value or "0").replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _to_int(value) -> int:
    try:
        return int(float(str(value or "0").replace(",", "")))
    except (TypeError, ValueError):
        return 0



TOOL_DEFINITIONS = [
    {
        "name": "get_market_session",
        "description": "현재 한국시간 기준 국내/해외 주식 거래 세션과 사용할 가격/주문 API 기준을 확인합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "market": {"type": "string", "enum": ["domestic", "overseas", "both"], "default": "both"},
            },
        },
    },
    {
        "name": "get_price",
        "description": "특정 종목의 현재가, 거래량, 등락률을 조회합니다. 국내/해외 거래 세션과 가격 출처를 함께 반환합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "stock_code": {"type": "string", "description": "종목 코드 (예: 005930, NVDA)"},
                "market": {"type": "string", "enum": ["domestic", "overseas"], "description": "시장. 비우면 종목 코드로 추정"},
                "exchange": {"type": "string", "description": "해외 거래소 NAS | NYS | AMS. 비우면 자동 판별"},
                "detail": {"type": "boolean", "default": False, "description": "국내 KRX/NXT/통합/시간외 원천별 시세를 함께 조회"},
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
        "name": "screen_candidates",
        "description": (
            "거래량 상위 종목에 대해 일봉 데이터를 조회하고 기술 지표(RSI, MA20, MA60, MACD)를 계산해 "
            "전략에 맞는 후보를 필터링합니다. "
            "종목 선정 시 get_rankings 대신 이 툴을 사용하세요. "
            "strategy: 'intraday'=단타(필터 없음), 'swing'=스윙(MA20>MA60 + RSI 30~65), "
            "'longterm'=장기(MA20>MA60 + RSI>50), 'all'=필터 없이 전체 지표만 반환."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "market":   {"type": "string", "enum": ["domestic", "overseas"], "default": "domestic"},
                "strategy": {"type": "string", "enum": ["intraday", "swing", "longterm", "all"], "default": "all"},
                "top_n":    {"type": "integer", "description": "스크리닝할 거래량 상위 종목 수 (기본 20)", "default": 20},
            },
            "required": [],
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
                "exchange": {"type": "string", "description": "해외 거래소 NAS | NYS | AMS. 비우면 자동 판별"},
                "candle_type": {"type": "string", "enum": ["minute", "daily"], "default": "minute"},
                "count": {"type": "integer", "description": "가져올 봉 개수", "default": 30},
            },
            "required": ["stock_code"],
        },
    },
    {
        "name": "get_indicators",
        "description": (
            "종목의 기술 지표를 5분봉(단기)과 일봉(중장기) 두 타임프레임으로 조회합니다. "
            "5분봉 지표(rsi, macd, ma5~60, bb_pct, stoch_k/d): 단타 진입 타이밍 판단에 사용. "
            "일봉 지표(rsi_daily, macd_daily, ma20_daily, ma60_daily, bb_pct_daily 등): 추세 방향·스윙·장기 판단에 사용. "
            "두 타임프레임을 함께 읽어 전략을 결정하세요: "
            "단타 = 5분봉 신호 중심 / 스윙 = 일봉 추세 + 5분봉 타이밍 / 장기 = 일봉 MA60·추세 위주. "
            "감시 조건 수립 전, 과매도/과매수 판단 시 반드시 호출하세요."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "stock_code": {"type": "string", "description": "종목 코드 (예: 005930, NVDA)"},
            },
            "required": ["stock_code"],
        },
    },
    {
        "name": "set_trading_mode",
        "description": (
            "거래 모드를 paper(모의투자) 또는 live(실거래)로 변경합니다. "
            "사용자가 명시적으로 요청할 때만 사용하세요."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "description": "paper | live"},
            },
            "required": ["mode"],
        },
    },
    {
        "name": "search_web",
        "description": (
            "웹에서 최신 정보를 검색합니다. 뉴스, 시황, 종목 분석, 경제 지표 등 실시간 정보가 필요할 때 사용하세요. "
            "예: '삼성전자 오늘 뉴스', '미국 증시 마감', '코스피 시황', 'NVDA 실적 발표'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색어"},
                "max_results": {"type": "integer", "default": 5, "description": "결과 수 (최대 10)"},
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
                "position_pct": {"type": "number", "description": "Portfolio allocation percentage. The AI decides this from risk."},
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
        "name": "get_chat_history",
        "description": (
            "오늘 또는 특정 날짜의 대화 히스토리를 조회합니다. "
            "'오늘 어떤 이벤트가 있었지?', '아까 브리핑에서 뭐라고 했지?' 등을 확인할 때 사용하세요."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "조회할 날짜 (YYYY-MM-DD). 비워두면 오늘"},
                "source": {
                    "type": "string",
                    "description": "chat | event | morning_brief. 비워두면 전체",
                },
                "limit": {"type": "integer", "default": 20, "description": "가져올 최대 개수"},
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
                                "enum": ["price_change", "price_above", "price_below", "volume_spike", "expr"],
                                "description": (
                                    "price_change: 설정 시점 대비 ±X% | price_above: X원 이상 | price_below: X원 이하 | volume_spike: 평균 거래량의 X배 | "
                                    "expr: 자유 수식 — formula 필드에 파이썬 비교식 작성. "
                                    "사용 가능 변수: price, volume, change_pct, volume_ratio, baseline_price, baseline_volume, "
                                    "rsi, macd, ma5, ma10, ma20, ma60, avg_volume, "
                                    "bb_pct(볼린저 %B: 0=하단,1=상단), bb_upper, bb_lower, stoch_k, stoch_d. "
                                    "과매도: rsi<30 or stoch_k<20 or bb_pct<0.1 | 과매수: rsi>70 or stoch_k>80 or bb_pct>0.9. "
                                    "예시: 'rsi < 30 and bb_pct < 0.1', 'stoch_k < 20 and change_pct < -2'"
                                ),
                            },
                            "threshold": {"type": "number", "description": "expr 타입에서는 불필요 (생략 가능)"},
                            "formula": {"type": "string", "description": "expr 타입일 때 평가할 파이썬 비교식"},
                            "note": {"type": "string", "description": "이 조건을 거는 이유"},
                        },
                        "required": ["type"],
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
    {
        "name": "get_system_status",
        "description": "서버 시스템 상태를 조회합니다. Redis 연결, WebSocket 구독, 지표 캐시, 감시 종목 수, 에이전트 히스토리 등을 확인할 수 있습니다.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_logs",
        "description": "서버 로그를 조회합니다. 최근 이벤트, 에러, 툴 실행 내역 등을 확인할 수 있습니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "level": {"type": "string", "enum": ["ALL", "INFO", "WARNING", "ERROR"], "default": "INFO", "description": "조회할 최소 로그 레벨"},
                "limit": {"type": "integer", "default": 30, "description": "가져올 줄 수"},
                "name": {"type": "string", "default": "", "description": "모듈명 필터 (예: detector, agent, tools)"},
            },
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
        config: dict | None = None,
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
        self._config = config or {}
        self._regime_fn = regime_fn
        self._overseas_exchange_by_code = self._build_overseas_exchange_map(self._config)
        self.executed_tools: list[str] = []
        self.allow_orders = False

    def reset_executed_tools(self) -> None:
        self.executed_tools.clear()

    async def execute(self, tool_name: str, tool_input: dict) -> str:
        import asyncio as _asyncio
        logger.info("도구 실행: %s %s", tool_name, tool_input)
        self.executed_tools.append(tool_name)
        try:
            match tool_name:
                case "get_market_session":
                    return self._get_market_session(tool_input.get("market", "both"))
                case "get_price":
                    # KIS REST fallback이 동기 HTTP — 이벤트 루프 블로킹 방지
                    return await _asyncio.to_thread(
                        self._get_price,
                        tool_input["stock_code"],
                        tool_input.get("market"),
                        tool_input.get("exchange"),
                        bool(tool_input.get("detail", False)),
                    )
                case "get_orderbook":
                    return self._get_orderbook(tool_input["stock_code"])
                case "get_portfolio":
                    return await _asyncio.to_thread(self._get_portfolio, tool_input.get("market", "both"))
                case "get_rankings":
                    return await _asyncio.to_thread(
                        self._get_rankings,
                        tool_input.get("rank_type", "volume"),
                        tool_input.get("market", "domestic"),
                    )
                case "screen_candidates":
                    return await _asyncio.to_thread(
                        self._screen_candidates,
                        tool_input.get("market", "domestic"),
                        tool_input.get("strategy", "all"),
                        tool_input.get("top_n", 20),
                    )
                case "get_candles":
                    stock_code = tool_input["stock_code"]
                    market = tool_input.get("market") or ("domestic" if self._is_domestic_code(stock_code) else "overseas")
                    return await self._get_candles(
                        stock_code,
                        market,
                        tool_input.get("exchange"),
                        tool_input.get("candle_type") or ("daily" if market == "overseas" else "minute"),
                        tool_input.get("count", 30),
                    )
                case "get_indicators":
                    return self._get_indicators(tool_input["stock_code"])
                case "set_trading_mode":
                    return await _asyncio.to_thread(self._set_trading_mode, tool_input["mode"])
                case "search_web":
                    return await _asyncio.to_thread(
                        self._search_web, tool_input["query"], tool_input.get("max_results", 5)
                    )
                case "place_order":
                    if not self.allow_orders:
                        return json.dumps({"error": "주문 실행 차단: 명시적 주문 실행 요청이 없어서 계획/감시만 설정합니다."}, ensure_ascii=False)
                    missing_precheck = [
                        name for name in ("get_portfolio", "get_price")
                        if name not in set(self.executed_tools)
                    ]
                    if missing_precheck:
                        return json.dumps({
                            "error": "주문 실행 차단: 주문 전 필수 확인 도구가 누락되었습니다.",
                            "missing_precheck": missing_precheck,
                            "instruction": "같은 판단 루프에서 get_portfolio, get_price를 먼저 호출한 뒤 주문하세요.",
                        }, ensure_ascii=False)
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
                case "get_chat_history":
                    return await self._get_chat_history(
                        tool_input.get("date"),
                        tool_input.get("source"),
                        tool_input.get("limit", 20),
                    )
                case "set_watch":
                    return await self._set_watch(tool_input)
                case "clear_watch":
                    return self._clear_watch(tool_input["stock_code"])
                case "list_watches":
                    return self._list_watches()
                case "get_system_status":
                    return await self._get_system_status()
                case "get_logs":
                    return await self._get_logs(
                        tool_input.get("level", "INFO"),
                        tool_input.get("limit", 30),
                        tool_input.get("name", ""),
                    )
                case _:
                    return json.dumps({"error": f"알 수 없는 도구: {tool_name}"})
        except Exception as e:
            logger.exception("도구 실행 오류: %s", tool_name)
            return json.dumps({"error": str(e)})

    def _get_market_session(self, market: str = "both") -> str:
        result: dict[str, Any] = {"timestamp": datetime.now(_KST).isoformat()}
        if market in ("domestic", "both"):
            result["domestic"] = self._domestic_market_session()
        if market in ("overseas", "both"):
            result["overseas"] = self._overseas_market_session()
        return json.dumps(result, ensure_ascii=False, default=str)

    def _get_price(self, stock_code: str, market: str | None = None, exchange: str | None = None, detail: bool = False) -> str:
        data = self._market.get_price(stock_code)
        inferred = market or ("domestic" if self._is_domestic_code(stock_code) else "overseas")
        if data and not (detail and inferred == "domestic"):
            session = self._domestic_market_session() if inferred == "domestic" else self._overseas_market_session()
            return json.dumps({
                "stock_code": stock_code,
                "market": inferred,
                "source": data.get("price_source", "websocket"),
                "session": session,
                "data": data,
            }, ensure_ascii=False, default=str)

        market = inferred
        try:
            if market == "domestic" and self._domestic:
                return self._get_domestic_price(stock_code, detail=detail)
            if market == "overseas" and self._overseas:
                exch, data = self._fetch_overseas_data(stock_code, exchange, "price")
                if data:
                    return json.dumps({
                        "stock_code": stock_code,
                        "market": "overseas",
                        "exchange": str(exch),
                        "source": "rest",
                        "session": self._overseas_market_session(),
                        "data": data,
                    }, ensure_ascii=False, default=str)
        except Exception as e:
            return json.dumps({"error": f"{stock_code} 가격 조회 실패: {e}"}, ensure_ascii=False)
        return json.dumps({"error": f"{stock_code} 가격 데이터 없음"}, ensure_ascii=False)

    def _get_domestic_price(self, stock_code: str, detail: bool = False) -> str:
        from kis.constants import MarketCode

        session = self._domestic_market_session()
        quotes: dict[str, Any] = {}
        primary = self._domestic.get_price(stock_code, MarketCode.ALL)
        quotes["unified"] = primary

        if detail:
            for key, market_code in (("krx", MarketCode.KRX), ("nxt", MarketCode.NXT)):
                try:
                    quotes[key] = self._domestic.get_price(stock_code, market_code)
                except Exception as e:
                    quotes[key] = {"error": str(e)}
        if detail or session["session"] in ("after_hours_single", "nxt_after", "closed"):
            try:
                quotes["overtime"] = self._domestic.get_overtime_price(stock_code)
            except Exception as e:
                quotes["overtime"] = {"error": str(e)}

        return json.dumps({
            "stock_code": stock_code,
            "market": "domestic",
            "source": "unified_rest",
            "session": session,
            "data": primary,
            "quotes": quotes,
        }, ensure_ascii=False, default=str)

    def _get_orderbook(self, stock_code: str) -> str:
        data = self._market.get_orderbook(stock_code)
        if not data:
            return json.dumps({"error": f"{stock_code} 호가 데이터 없음"})
        return json.dumps(data, ensure_ascii=False)

    def _get_portfolio(self, market: str) -> str:
        positions = []
        domestic_balance = self._account.get_balance("domestic")
        overseas_balance = self._account.get_balance("overseas")
        errors: dict[str, str] = {}

        if market in ("domestic", "both") and self._domestic:
            try:
                balance = self._domestic.get_balance()
                summary = balance.get("summary") or {}
                domestic_balance = {
                    "cash": _to_float(summary.get("dnca_tot_amt")),
                    "totalAssets": _to_float(summary.get("tot_evlu_amt") or summary.get("nass_amt")),
                    "positionValue": _to_float(summary.get("evlu_amt_smtl_amt")),
                    "positionCount": len(balance.get("positions") or []),
                    "raw": summary,
                }
                for p in balance.get("positions") or []:
                    qty = _to_int(p.get("hldg_qty"))
                    if qty <= 0:
                        continue
                    balance_price = _to_float(p.get("prpr"))
                    market_price = balance_price
                    price_source = "balance"
                    try:
                        quote = self._domestic.get_price(p.get("pdno"))
                        quoted_price = _to_float(quote.get("stck_prpr"))
                        if quoted_price > 0:
                            market_price = quoted_price
                            price_source = "unified_rest"
                    except Exception as e:
                        errors[f"price:{p.get('pdno')}"] = str(e)
                    avg_price = _to_float(p.get("pchs_avg_pric"))
                    eval_amount = market_price * qty
                    pnl = (market_price - avg_price) * qty
                    cost = avg_price * qty
                    positions.append({
                        "market": "domestic",
                        "stock_code": p.get("pdno"),
                        "stock_name": p.get("prdt_name"),
                        "quantity": qty,
                        "avg_price": avg_price,
                        "current_price": market_price,
                        "balance_price": balance_price,
                        "price_source": price_source,
                        "eval_amount": eval_amount,
                        "pnl": pnl,
                        "pnl_pct": round(pnl / cost * 100, 4) if cost > 0 else 0,
                        "balance_eval_amount": _to_float(p.get("evlu_amt")),
                        "balance_pnl": _to_float(p.get("evlu_pfls_amt")),
                        "balance_pnl_pct": _to_float(p.get("evlu_pfls_rt")),
                    })
            except Exception as e:
                errors["domestic"] = str(e)
                positions.extend(self._account.get_positions())
        else:
            positions.extend(self._account.get_positions())

        result = {
            "positions": positions,
            "balance": {
                "domestic": domestic_balance,
                "overseas": overseas_balance,
            },
        }
        if errors:
            result["errors"] = errors
        return json.dumps(result, ensure_ascii=False, default=str)

    def _get_rankings(self, rank_type: str, market: str) -> str:
        try:
            if market == "overseas":
                from kis.constants import ExchangeCode
                items = self._overseas.get_volume_ranking(ExchangeCode.NASDAQ) if self._overseas else []
            else:
                items = self._domestic.get_volume_ranking() if self._domestic else []
        except Exception as e:
            return json.dumps({"error": f"순위 조회 실패: {e}"})
        slim = [
            {"name": it.get("hts_kor_isnm", ""), "code": it.get("mksc_shrn_iscd", ""), "rank": it.get("data_rank", ""),
             "price": it.get("stck_prpr", ""), "change_rate": it.get("prdy_ctrt", "")}
            for it in items[:10]
        ]
        return json.dumps({"rank_type": rank_type, "market": market, "items": slim}, ensure_ascii=False)

    def _screen_candidates(self, market: str, strategy: str, top_n: int) -> str:
        from datetime import date, timedelta
        from events.detector import _compute_indicators

        try:
            if market == "overseas":
                from kis.constants import ExchangeCode
                raw = self._overseas.get_volume_ranking(ExchangeCode.NASDAQ) if self._overseas else []
            else:
                raw = self._domestic.get_volume_ranking() if self._domestic else []
        except Exception as e:
            return json.dumps({"error": f"순위 조회 실패: {e}"})

        end = date.today()
        start = end - timedelta(days=180)
        results = []

        for item in raw[:top_n]:
            code = item.get("mksc_shrn_iscd", "")
            name = item.get("hts_kor_isnm", code)
            if not code:
                continue
            try:
                if market == "domestic" and self._domestic:
                    df = self._domestic.get_daily_ohlcv(code, start, end)
                elif market == "overseas" and self._overseas:
                    from kis.constants import ExchangeCode as _EC
                    df = self._overseas.get_daily_ohlcv(code, _EC.NASDAQ, start, end)
                else:
                    continue
                if df is None or df.empty or len(df) < 20:
                    continue

                ind = _compute_indicators(
                    df["close"].tolist(),
                    df["volume"].tolist() if "volume" in df.columns else [],
                    highs=df["high"].tolist() if "high" in df.columns else None,
                    lows=df["low"].tolist() if "low" in df.columns else None,
                )
                rsi = ind.get("rsi")
                ma20 = ind.get("ma20")
                ma60 = ind.get("ma60")
                macd = ind.get("macd")
                bb_pct = ind.get("bb_pct")
                uptrend = (ma20 or 0) > (ma60 or 0)

                # 전략 필터
                if strategy == "swing" and not (uptrend and rsi is not None and 30 <= rsi <= 65):
                    continue
                if strategy == "longterm" and not (uptrend and rsi is not None and rsi > 50):
                    continue

                results.append({
                    "code": code,
                    "name": name,
                    "price": item.get("stck_prpr", ""),
                    "change_rate": item.get("prdy_ctrt", ""),
                    "rsi_daily": round(rsi, 1) if rsi is not None else None,
                    "ma20_daily": round(ma20, 0) if ma20 is not None else None,
                    "ma60_daily": round(ma60, 0) if ma60 is not None else None,
                    "macd_daily": round(macd, 2) if macd is not None else None,
                    "bb_pct_daily": round(bb_pct, 3) if bb_pct is not None else None,
                    "trend": "상승" if uptrend else "하락",
                })
            except Exception:
                logger.warning("스크리닝 실패: %s", code)
                continue

        return json.dumps({
            "strategy": strategy,
            "screened": len(raw[:top_n]),
            "filtered": len(results),
            "candidates": results,
        }, ensure_ascii=False)

    async def _get_candles(self, stock_code: str, market: str, exchange: str | None, candle_type: str, count: int) -> str:
        import asyncio
        loop = asyncio.get_event_loop()
        try:
            if candle_type == "daily":
                from datetime import date, timedelta
                end = date.today()
                start = end - timedelta(days=count * 2)
                if market == "overseas":
                    df = await asyncio.wait_for(loop.run_in_executor(
                        None, lambda: self._fetch_overseas_data(stock_code, exchange, "daily", start=start, end=end)[1]
                    ), timeout=12)
                else:
                    df = await asyncio.wait_for(loop.run_in_executor(
                        None, lambda: self._domestic.get_daily_ohlcv(stock_code, start, end)
                    ), timeout=12)
            else:
                if market == "overseas":
                    try:
                        df = await asyncio.wait_for(loop.run_in_executor(
                            None, lambda: self._fetch_overseas_data(stock_code, exchange, "minute")[1]
                        ), timeout=8)
                    except Exception:
                        from datetime import date, timedelta
                        end = date.today()
                        start = end - timedelta(days=count * 2)
                        df = await asyncio.wait_for(loop.run_in_executor(
                            None, lambda: self._fetch_overseas_data(stock_code, exchange, "daily", start=start, end=end)[1]
                        ), timeout=12)
                        candle_type = "daily_fallback"
                else:
                    from datetime import datetime as _dt
                    input_hour = _dt.now().strftime("%H%M%S")
                    df = await asyncio.wait_for(loop.run_in_executor(
                        None, lambda h=input_hour: self._domestic.get_minute_ohlcv(stock_code, input_hour=h)
                    ), timeout=10)
            if df is None or df.empty:
                return json.dumps({"error": f"{stock_code} 차트 데이터 없음"})
            tail = df.tail(count)
            records = tail[["datetime", "open", "high", "low", "close", "volume"]].to_dict("records")
            used_exchange = getattr(df, "attrs", {}).get("exchange")
            return json.dumps({"stock_code": stock_code, "market": market, "exchange": used_exchange, "candle_type": candle_type, "count": len(records), "candles": records}, ensure_ascii=False, default=str)
        except Exception as e:
            return json.dumps({"error": f"차트 조회 실패: {e}"})

    def _get_indicators(self, stock_code: str) -> str:
        if not self._r:
            return json.dumps({"error": "Redis 미연결"})
        raw = self._r.get(f"ai:indicators:{stock_code}")
        if not raw:
            return json.dumps({
                "stock_code": stock_code,
                "status": "캐시 없음 — 감시 설정 후 5분 이내 자동 계산됩니다.",
            }, ensure_ascii=False)
        try:
            indicators = json.loads(raw)
        except Exception:
            return json.dumps({"error": "지표 파싱 실패"})

        intraday_keys = {"rsi", "macd", "ma5", "ma10", "ma20", "ma60",
                         "bb_upper", "bb_lower", "bb_pct", "stoch_k", "stoch_d", "avg_volume"}
        labels_5min = {
            "rsi": "RSI(14) [5분봉]", "macd": "MACD [5분봉]",
            "ma5": "MA5 [5분봉]", "ma10": "MA10 [5분봉]",
            "ma20": "MA20 [5분봉]", "ma60": "MA60 [5분봉]",
            "bb_upper": "볼린저 상단 [5분봉]", "bb_lower": "볼린저 하단 [5분봉]",
            "bb_pct": "볼린저 %B [5분봉]", "stoch_k": "스토캐스틱 %K [5분봉]",
            "stoch_d": "스토캐스틱 %D [5분봉]", "avg_volume": "평균거래량 [5분봉]",
        }
        labels_daily = {
            "rsi_daily": "RSI(14) [일봉]", "macd_daily": "MACD [일봉]",
            "ma5_daily": "MA5 [일봉]", "ma10_daily": "MA10 [일봉]",
            "ma20_daily": "MA20 [일봉]", "ma60_daily": "MA60 [일봉]",
            "bb_upper_daily": "볼린저 상단 [일봉]", "bb_lower_daily": "볼린저 하단 [일봉]",
            "bb_pct_daily": "볼린저 %B [일봉]", "stoch_k_daily": "스토캐스틱 %K [일봉]",
            "stoch_d_daily": "스토캐스틱 %D [일봉]",
        }

        intraday = {k: v for k, v in indicators.items() if k in intraday_keys}
        daily = {k: v for k, v in indicators.items() if k.endswith("_daily")}

        def _fmt(d: dict, lbls: dict) -> dict:
            return {lbls.get(k, k): round(v, 4) if isinstance(v, float) else v for k, v in d.items()}

        signals = []
        rsi = indicators.get("rsi")
        bb_pct = indicators.get("bb_pct")
        stoch_k = indicators.get("stoch_k")
        rsi_d = indicators.get("rsi_daily")
        ma20_d = indicators.get("ma20_daily")
        ma60_d = indicators.get("ma60_daily")

        if rsi is not None:
            if rsi < 30:   signals.append(f"5분봉 RSI {rsi} → 단기 과매도")
            elif rsi > 70: signals.append(f"5분봉 RSI {rsi} → 단기 과매수")
        if bb_pct is not None:
            if bb_pct < 0.1:  signals.append(f"5분봉 볼린저 %B {bb_pct} → 하단 이탈")
            elif bb_pct > 0.9: signals.append(f"5분봉 볼린저 %B {bb_pct} → 상단 돌파")
        if stoch_k is not None:
            if stoch_k < 20:  signals.append(f"5분봉 스토캐스틱 K {stoch_k} → 단기 과매도")
            elif stoch_k > 80: signals.append(f"5분봉 스토캐스틱 K {stoch_k} → 단기 과매수")
        if rsi_d is not None:
            if rsi_d < 30:   signals.append(f"일봉 RSI {rsi_d} → 중기 과매도 (스윙 진입 고려)")
            elif rsi_d > 70: signals.append(f"일봉 RSI {rsi_d} → 중기 과매수 (스윙 청산 고려)")
        if ma20_d is not None and ma60_d is not None:
            if ma20_d > ma60_d: signals.append("일봉 MA20 > MA60 → 중기 상승 추세")
            else:               signals.append("일봉 MA20 < MA60 → 중기 하락 추세")

        strategy_hint = []
        if rsi is not None and rsi_d is not None:
            if rsi < 35 and rsi_d < 40:
                strategy_hint.append("단타+스윙 동시 과매도 — 반등 시 단타/스윙 진입 고려")
            elif rsi_d > 50 and ma20_d and ma60_d and ma20_d > ma60_d:
                strategy_hint.append("일봉 추세 양호 — 스윙 or 장기 보유 전략 적합")
            else:
                strategy_hint.append("5분봉 단기 신호 위주로 판단 — 단타 전략 적합")

        return json.dumps({
            "stock_code": stock_code,
            "intraday_indicators": _fmt(intraday, labels_5min),
            "daily_indicators": _fmt(daily, labels_daily) if daily else "일봉 지표 계산 중 (watch 등록 후 30분 이내 갱신)",
            "signals": signals or ["특이 신호 없음"],
            "strategy_hint": strategy_hint,
        }, ensure_ascii=False)

    def _set_trading_mode(self, mode: str) -> str:
        current = self._config.get("mode", "unknown")
        return json.dumps({
            "error": f"런타임 모드 변경 불가. config.yaml의 mode를 '{mode}'로 수정 후 서버를 재시작해야 합니다.",
            "current_mode": current,
        }, ensure_ascii=False)

    def _search_web(self, query: str, max_results: int) -> str:
        try:
            from ddgs import DDGS
            with DDGS() as ddgs:
                results = list(ddgs.text(query, region="kr-kr", max_results=min(max_results, 5)))
            items = [
                {
                    "title": r.get("title", ""),
                    "body": r.get("body", "")[:150],
                }
                for r in results
            ]
            return json.dumps({"query": query, "results": items}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"웹 검색 실패: {e}"})

    async def _place_order(self, inp: dict) -> str:
        stock_code = inp["stock_code"]
        stock_name = inp["stock_name"]
        side = inp["side"]
        quantity = int(inp.get("quantity") or 0)
        position_pct = float(inp.get("position_pct") or 0)
        price = float(inp.get("price") or 0)
        reason = inp["reason"]

        pending = self._order_manager.get_pending_orders()
        if stock_code in pending:
            return json.dumps({"error": f"{stock_code} 이미 미체결 주문 존재"})

        positions = self._order_manager.get_open_positions()
        if side == "BUY":
            exchange = "KRX" if self._is_domestic_code(stock_code) else str(self._exchange_candidates(stock_code, inp.get("exchange"))[0])
            price = price or self._resolve_order_price(stock_code, exchange)
            if price <= 0:
                return json.dumps({"error": f"{stock_code} 주문 기준가 확인 실패"}, ensure_ascii=False)
            result = self._order_manager.open_position(
                stock_code=stock_code,
                name=stock_name,
                exchange=exchange,
                price=price,
                strategy=reason[:80],
                qty_override=quantity if quantity > 0 else None,
                position_pct_override=position_pct if position_pct > 0 else None,
            )
        else:
            if stock_code not in positions:
                return json.dumps({"error": f"{stock_code} 보유 포지션 없음"})
            exchange = positions[stock_code].exchange
            price = price or self._resolve_order_price(stock_code, exchange)
            if price <= 0:
                return json.dumps({"error": f"{stock_code} 주문 기준가 확인 실패"})
            result = self._order_manager.close_position(
                stock_code=stock_code,
                current_price=price,
                reason=reason,
            )

        return json.dumps({
            "status": "주문 요청 완료" if result else "주문 요청 실패",
            "side": side,
            "stock_code": stock_code,
            "requested_quantity": quantity,
            "position_pct": position_pct,
            "order_price": price,
            "result": bool(result),
            "pending_orders": self._order_manager.get_pending_order_rows(),
        }, ensure_ascii=False, default=str)

    def _resolve_order_price(self, stock_code: str, exchange: str) -> float:
        cached = self._market.get_price(stock_code)
        cached_price = self._first_float(cached or {}, ["current_price", "price", "last"])
        if cached_price > 0:
            return cached_price

        try:
            if exchange == "KRX" and self._domestic:
                data = self._domestic.get_price(stock_code)
                return self._first_float(data, ["stck_prpr", "price", "current_price", "last"])
            if self._overseas:
                _, data = self._fetch_overseas_data(stock_code, exchange, "price")
                price = self._first_float(data or {}, self._PRICE_FIELDS)
                if price > 0:
                    return price
                from datetime import date, timedelta
                _, df = self._fetch_overseas_data(
                    stock_code, exchange, "daily",
                    start=date.today() - timedelta(days=10),
                    end=date.today(),
                )
                if df is not None and not df.empty:
                    return float(df.iloc[-1]["close"])
        except Exception:
            logger.exception("주문 기준가 조회 실패: %s", stock_code)
        return 0.0

    _PRICE_FIELDS = ["last", "price", "current_price", "ovrs_nmix_prpr", "stck_prpr", "tlast", "base", "clos"]

    def _fetch_overseas_data(
        self,
        stock_code: str,
        exchange: str | None,
        mode: str,  # "price" | "daily" | "minute"
        *,
        start=None,
        end=None,
    ) -> tuple[Any, Any]:
        """해외 데이터 조회: exchange 우선순위대로 첫 성공 반환. (exch, data) 튜플 반환."""
        for exch in self._exchange_candidates(stock_code, exchange):
            try:
                if mode == "price":
                    data = self._overseas.get_price(stock_code, exch)
                    if data and self._first_float(data, self._PRICE_FIELDS) > 0:
                        return exch, data
                elif mode == "daily":
                    df = self._overseas.get_daily_ohlcv(stock_code, exch, start, end)
                    if df is not None and not df.empty:
                        df.attrs["exchange"] = str(exch)
                        return exch, df
                elif mode == "minute":
                    df = self._overseas.get_historical_minute_ohlcv(stock_code, exch, lookback_days=3)
                    if df is not None and not df.empty:
                        df.attrs["exchange"] = str(exch)
                        return exch, df
            except Exception:
                continue
        return None, None

    def _exchange_candidates(self, stock_code: str, exchange: str | None = None) -> list[Any]:
        from kis.constants import ExchangeCode

        def parse(value: str | None):
            if not value:
                return None
            normalized = value.upper()
            aliases = {
                "NASDAQ": ExchangeCode.NASDAQ,
                "NAS": ExchangeCode.NASDAQ,
                "NASD": ExchangeCode.NASDAQ,
                "NYSE": ExchangeCode.NYSE,
                "NYS": ExchangeCode.NYSE,
                "AMEX": ExchangeCode.AMEX,
                "AMS": ExchangeCode.AMEX,
            }
            return aliases.get(normalized)

        ordered: list[Any] = []
        for candidate in (
            parse(exchange),
            parse(self._overseas_exchange_by_code.get(stock_code.upper())),
            ExchangeCode.NASDAQ,
            ExchangeCode.NYSE,
            ExchangeCode.AMEX,
        ):
            if candidate is not None and candidate not in ordered:
                ordered.append(candidate)
        return ordered

    @staticmethod
    def _build_overseas_exchange_map(config: dict) -> dict[str, str]:
        result: dict[str, str] = {}
        for item in config.get("universe", {}).get("overseas", {}).get("stocks", []):
            if isinstance(item, dict) and item.get("code"):
                result[str(item["code"]).upper()] = str(item.get("exchange", "")).upper()
        result.setdefault("HPE", "NYS")
        return result

    @staticmethod
    def _first_float(data: dict, keys: list[str]) -> float:
        for key in keys:
            try:
                value = data.get(key)
                if value is None or value == "":
                    continue
                parsed = float(str(value).replace(",", ""))
                if parsed > 0:
                    return parsed
            except Exception:
                continue
        return 0.0

    def _cancel_order(self, order_id: str) -> str:
        return json.dumps({"status": "취소 기능 미구현 — order_manager에 추가 필요", "order_id": order_id})

    async def _save_plan(self, inp: dict) -> str:
        blocked = [
            stock for stock in inp.get("watch_stocks", [])
            if self._is_domestic_code(str(stock.get("code", ""))) and not self._is_domestic_market_open()
        ]
        if blocked:
            return json.dumps({
                "error": "국내장 시간 외 국내 종목 계획 저장 차단",
                "blocked_symbols": blocked,
                "instruction": "현재 KRX 정규장이 아니므로 해외/미국 후보를 선택하거나 국내장은 장 시작 전 전용 계획으로만 다루세요.",
            }, ensure_ascii=False)
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

    async def _get_chat_history(self, date: str | None, source: str | None, limit: int) -> str:
        rows = await self._memory.get_chat_history(date=date, source=source, limit=limit)
        return json.dumps(rows, ensure_ascii=False, default=str)

    async def _set_watch(self, inp: dict) -> str:
        if not self._r:
            return json.dumps({"error": "Redis 미연결"})
        stock_code = inp["stock_code"]
        market = inp["market"]
        if market == "domestic" and not self._is_domestic_market_open():
            return json.dumps({
                "error": "국내장 시간 외 국내 종목 감시 설정 차단",
                "stock_code": stock_code,
                "instruction": "현재 한국 정규장이 닫혀 있습니다. 이 시간대에는 해외/미국 후보를 선택하세요.",
            }, ensure_ascii=False)

        err = self._validate_watch_conditions(inp.get("conditions", []))
        if err:
            return err

        exchange, baseline_price, baseline_volume = await self._resolve_watch_baseline(
            stock_code, market, inp.get("exchange")
        )
        ws_subscribed = await self._store_and_subscribe_watch(
            stock_code, inp["stock_name"], market, exchange,
            baseline_price, baseline_volume, inp["conditions"],
        )
        ws_status = "WebSocket 실시간 구독 성공" if ws_subscribed else "WebSocket 구독 실패 — 실시간 가격 수신 불가, 감시 조건이 평가되지 않습니다"
        return json.dumps({
            "status": "감시 설정 완료",
            "stock_code": stock_code,
            "market": market,
            "baseline_price": baseline_price,
            "baseline_volume": baseline_volume,
            "exchange": exchange,
            "ws_subscribed": ws_subscribed,
            "ws_status": ws_status,
            "conditions": inp["conditions"],
        }, ensure_ascii=False)

    def _validate_watch_conditions(self, conditions: list) -> str | None:
        """허용되지 않는 조건 타입이 있으면 에러 JSON 반환, 없으면 None."""
        bad = [c.get("type") for c in conditions if c.get("type") not in ("expr", "price_above", "price_below")]
        if not bad:
            return None
        return json.dumps({
            "error": f"조건 타입 거부: {bad}. price_change/volume_spike는 사용 불가.",
            "instruction": (
                "반드시 expr 타입을 사용하고 formula 필드에 파이썬 비교식을 작성하세요. "
                "2개 이상 지표를 조합해야 합니다. "
                "예시: 'rsi < 30 and bb_pct < 0.15 and change_pct < -2' "
                "사용 가능 변수: price, volume, change_pct, volume_ratio, "
                "rsi, macd, ma5, ma10, ma20, ma60, bb_pct, bb_upper, bb_lower, stoch_k, stoch_d"
            ),
        }, ensure_ascii=False)

    async def _resolve_watch_baseline(
        self, stock_code: str, market: str, exchange_hint: str | None
    ) -> tuple[str, float, float]:
        """캐시 → REST → 일봉 순서로 기준가/거래량 조회. (exchange, price, volume) 반환."""
        price_data = self._market.get_price(stock_code)
        exchange = "KRX"
        baseline_price = self._first_float(price_data or {}, ["current_price", "price", "last"])
        baseline_volume = self._first_float(price_data or {}, ["acml_volume", "volume", "acml_vol"])

        if market == "overseas" and self._overseas:
            if baseline_price <= 0:
                try:
                    exch_obj, rest_price = self._fetch_overseas_data(stock_code, exchange_hint, "price")
                    if exch_obj:
                        exchange = str(exch_obj)
                    baseline_price = self._first_float(rest_price or {}, self._PRICE_FIELDS)
                    baseline_volume = self._first_float(rest_price or {}, ["acml_vol", "volume", "tvol"])
                    if baseline_price <= 0:
                        from datetime import date, timedelta
                        exch_obj, df = self._fetch_overseas_data(
                            stock_code, exchange_hint, "daily",
                            start=date.today() - timedelta(days=10),
                            end=date.today(),
                        )
                        if exch_obj:
                            exchange = str(exch_obj)
                        if df is not None and not df.empty:
                            last = df.iloc[-1]
                            baseline_price = float(last.get("close") or 0)
                            baseline_volume = float(last.get("volume") or 0)
                except Exception:
                    logger.exception("감시 기준가 조회 실패: %s", stock_code)
            else:
                exchange = str(self._exchange_candidates(stock_code, exchange_hint)[0])

        return exchange, baseline_price, baseline_volume

    async def _store_and_subscribe_watch(
        self,
        stock_code: str,
        stock_name: str,
        market: str,
        exchange: str,
        baseline_price: float,
        baseline_volume: float,
        conditions: list,
    ) -> bool:
        """Redis에 감시 저장 + WebSocket 동적 구독. ws_subscribed 반환."""
        watches = self._load_watches()
        watches[stock_code] = {
            "stock_code": stock_code,
            "stock_name": stock_name,
            "market": market,
            "exchange": exchange,
            "conditions": conditions,
            "baseline_price": baseline_price,
            "baseline_volume": baseline_volume,
            "set_at": datetime.now().isoformat(),
            "triggered_types": [],
        }
        self._r.set(_WATCHES_KEY, json.dumps(watches, ensure_ascii=False))

        if self._ws is None:
            return False

        from kis.constants import WebSocketTRID

        def _on_price(tr_id, fields):
            from kis.websocket import parse_domestic_price, parse_overseas_price
            parsed = parse_domestic_price(fields) if market == "domestic" else parse_overseas_price(fields)
            code = parsed.get("stock_code", "")
            if code:
                self._market.on_price_tick(code, {
                    "stock_code": code,
                    "current_price": parsed.get("price", 0),
                    "volume": parsed.get("vol", 0),
                    "acml_volume": parsed.get("acml_vol", 0),
                    "time": parsed.get("time", ""),
                    "exchange": exchange,
                    "price_source": "websocket",
                    "stock_name": stock_name,
                })

        if market == "domestic":
            tr_id = (
                WebSocketTRID.DOMESTIC_PRICE_UNIFIED
                if str(self._config.get("kis", {}).get("domestic_market", "UN")).upper() == "UN"
                else WebSocketTRID.DOMESTIC_PRICE
            )
        else:
            tr_id = WebSocketTRID.OVERSEAS_PRICE
        tr_key = stock_code if market == "domestic" else f"D{exchange}{stock_code}"
        try:
            return await self._ws.add_live_subscription(tr_id, tr_key, _on_price)
        except Exception:
            logger.exception("감시 종목 동적 구독 실패: %s", stock_code)
            return False

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

    async def _get_system_status(self) -> str:
        import aiohttp
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get("http://127.0.0.1:8000/ai/system/status", timeout=aiohttp.ClientTimeout(total=5)) as r:
                    data = await r.json()
            return json.dumps(data, ensure_ascii=False, default=str)
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _get_logs(self, level: str, limit: int, name: str) -> str:
        import aiohttp
        try:
            params = f"level={level}&limit={limit}&name={name}"
            async with aiohttp.ClientSession() as s:
                async with s.get(f"http://127.0.0.1:8000/ai/system/logs?{params}", timeout=aiohttp.ClientTimeout(total=5)) as r:
                    data = await r.json()
            logs = data.get("logs", [])
            lines = [f"[{l['ts']}] {l['level']:7} {l['name']:12} {l['msg']}" for l in logs]
            return "\n".join(lines) if lines else "로그 없음"
        except Exception as e:
            return json.dumps({"error": str(e)})

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

    @staticmethod
    def _between(now: dtime, start: dtime, end: dtime) -> bool:
        if start <= end:
            return start <= now < end
        return now >= start or now < end

    @staticmethod
    def _us_summer_time() -> bool:
        return bool(datetime.now(_NY).dst())

    def _domestic_market_session(self) -> dict[str, Any]:
        now = datetime.now(_KST)
        t = now.time()
        weekday = now.weekday()
        is_weekday = weekday < 5

        session = "closed"
        tradable = False
        order_market = None
        price_api = "closed_reference"
        websocket_tr = None

        if is_weekday and self._between(t, dtime(8, 0), dtime(8, 50)):
            session, tradable, order_market, price_api, websocket_tr = (
                "nxt_pre", True, "NXT/SOR", "unified_rest", "H0UNCNT0"
            )
        elif is_weekday and self._between(t, dtime(8, 50), dtime(9, 0)):
            session, tradable, order_market, price_api, websocket_tr = (
                "opening_auction", True, "KRX/SOR", "unified_rest", "H0UNCNT0"
            )
        elif is_weekday and self._between(t, dtime(9, 0), dtime(15, 20)):
            session, tradable, order_market, price_api, websocket_tr = (
                "regular_unified", True, "KRX+NXT/SOR", "unified_rest", "H0UNCNT0"
            )
        elif is_weekday and self._between(t, dtime(15, 20), dtime(15, 30)):
            session, tradable, order_market, price_api, websocket_tr = (
                "krx_closing_auction", True, "KRX/SOR", "unified_rest", "H0UNCNT0"
            )
        elif is_weekday and self._between(t, dtime(15, 30), dtime(16, 0)):
            session, tradable, order_market, price_api, websocket_tr = (
                "nxt_after", True, "NXT/SOR", "unified_rest", "H0UNCNT0"
            )
        elif is_weekday and self._between(t, dtime(16, 0), dtime(18, 0)):
            session, tradable, order_market, price_api, websocket_tr = (
                "after_hours_single", True, "NXT or KRX overtime", "unified_rest+overtime_price", "H0UNCNT0"
            )
        elif is_weekday and self._between(t, dtime(18, 0), dtime(20, 0)):
            session, tradable, order_market, price_api, websocket_tr = (
                "nxt_after", True, "NXT/SOR", "unified_rest", "H0UNCNT0"
            )

        return {
            "market": "domestic",
            "timezone": "Asia/Seoul",
            "now": now.isoformat(),
            "session": session,
            "tradable": tradable,
            "order_market": order_market,
            "price_api": price_api,
            "websocket_tr": websocket_tr,
            "notes": "UN=통합(KRX+NXT). 시간외 단일가 구간은 overtime_price도 함께 확인.",
        }

    def _overseas_market_session(self) -> dict[str, Any]:
        now = datetime.now(_KST)
        t = now.time()
        wd = now.weekday()
        summer = self._us_summer_time()
        daytime_end = dtime(17, 0) if summer else dtime(18, 0)
        pre_start = daytime_end
        regular_start = dtime(22, 30) if summer else dtime(23, 30)
        regular_end = dtime(5, 0) if summer else dtime(6, 0)
        after_start = regular_end

        session = "closed"
        tradable = False
        order_api = None
        price_api = "overseas_price_reference"
        realtime = False

        if wd < 5 and self._between(t, dtime(10, 0), daytime_end):
            session, tradable, order_api, realtime = "daytime", True, "daytime-order", False
        elif wd < 5 and self._between(t, pre_start, regular_start):
            session, tradable, order_api, realtime = "pre_market", True, "order", True
        elif (wd < 5 and t >= regular_start) or (1 <= wd <= 5 and t < regular_end):
            session, tradable, order_api, realtime = "regular", True, "order", True
        elif 1 <= wd <= 5 and self._between(t, after_start, dtime(7, 0)):
            session, tradable, order_api, realtime = "after_market", True, "order", True
        elif 1 <= wd <= 5 and self._between(t, dtime(7, 0), dtime(9, 0)):
            session, tradable, order_api, realtime = "after_market_extended", True, "order", True

        return {
            "market": "overseas",
            "timezone": "Asia/Seoul",
            "now": now.isoformat(),
            "us_summer_time": summer,
            "session": session,
            "tradable": tradable,
            "order_api": order_api,
            "price_api": price_api,
            "websocket_realtime_expected": realtime,
            "hours_kst": {
                "daytime": f"10:00-{daytime_end.strftime('%H:%M')}",
                "pre_market": f"{pre_start.strftime('%H:%M')}-{regular_start.strftime('%H:%M')}",
                "regular": f"{regular_start.strftime('%H:%M')}-{regular_end.strftime('%H:%M')}",
                "after_market": f"{after_start.strftime('%H:%M')}-07:00",
                "after_market_extended": "07:00-09:00",
            },
        }

    @staticmethod
    def _is_domestic_code(stock_code: str) -> bool:
        return len(stock_code) == 6 and stock_code.isdigit()

    def _is_domestic_market_open(self) -> bool:
        return bool(self._domestic_market_session().get("tradable"))
