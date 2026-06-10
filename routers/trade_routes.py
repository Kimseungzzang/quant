import json
import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db.pg_writer import PGWriter
from kis.constants import ExchangeCode
from routers import state

logger = logging.getLogger(__name__)
router = APIRouter()

_balance_cache: dict = {}
_BALANCE_TTL = 30


def _to_float(value) -> float:
    try:
        return float(str(value or "0").replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


@router.get("/account/balance")
def account_balance(market: str = "domestic", mode: str | None = None):
    config = state.components.get("config") or {}
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
                    balance = state.components["overseas"].get_balance(ExchangeCode(exch))
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
            raw_cash = state.components["overseas"].get_foreign_margin_usd()
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
            balance = state.components["domestic"].get_balance()
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


class ModeRequest(BaseModel):
    mode: str


@router.post("/mode")
def set_mode(req: ModeRequest):
    # 런타임 모드 변경은 KISAuth.base_url, DomesticAPI.is_paper 등이 재초기화되지 않아
    # DB 기록과 실제 API 호출 대상이 불일치하는 critical한 오작동을 유발한다.
    # 모드 변경은 config.yaml 수정 후 서버 재시작으로만 가능하다.
    current = (state.components.get("config") or {}).get("mode", "unknown")
    raise HTTPException(
        status_code=400,
        detail=f"런타임 모드 변경 불가. config.yaml의 mode를 '{req.mode}'로 수정 후 서버를 재시작하세요. (현재: {current})",
    )


@router.get("/trade/positions")
async def get_positions():
    return await PGWriter().get_positions()


@router.get("/trade/positions/live")
def get_live_positions(mode: str | None = None):
    domestic = state.components.get("domestic")
    overseas = state.components.get("overseas")
    if not domestic and not overseas:
        raise HTTPException(status_code=503, detail="초기화 중")
    positions = []
    try:
        if domestic:
            bal = domestic.get_balance()
            for p in bal.get("positions") or []:
                qty = int(p.get("hldg_qty") or 0)
                if qty <= 0:
                    continue
                positions.append({
                    "market": "domestic",
                    "stock_code": p.get("pdno"),
                    "stock_name": p.get("prdt_name"),
                    "quantity": qty,
                    "avg_price": float(p.get("pchs_avg_pric") or 0),
                    "current_price": float(p.get("prpr") or 0),
                    "eval_amount": float(p.get("evlu_amt") or 0),
                    "pnl": float(p.get("evlu_pfls_amt") or 0),
                    "pnl_pct": float(p.get("evlu_pfls_rt") or 0),
                })
    except Exception as e:
        logger.warning("국내 포지션 조회 실패: %s", e)
    return positions


@router.get("/trade/orders/pending")
def get_pending_orders():
    order_mgr = state.components.get("order_mgr")
    if order_mgr is None:
        raise HTTPException(status_code=503, detail="초기화 중")
    return order_mgr.get_pending_order_rows()


@router.get("/trade/orders/fills")
def get_order_fills(market: str = "overseas"):
    try:
        if market == "overseas":
            return state.components["overseas"].get_daily_orders()
        return state.components["domestic"].get_daily_orders()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/signals")
@router.get("/api/command/signals")
def get_signals():
    market_data = state.components.get("market_data")
    redis_client = state.components.get("redis")
    if market_data is None:
        raise HTTPException(status_code=503, detail="초기화 중")
    signals: dict = {}
    prices = market_data.get_all_prices()
    watches: dict = {}
    if redis_client is not None:
        try:
            raw = redis_client.get("ai:watches")
            watches = json.loads(raw) if raw else {}
        except Exception:
            pass
    now_iso = datetime.now().isoformat()
    for code, data in prices.items():
        price = _to_float(data.get("current_price") or data.get("price"))
        if price <= 0:
            continue
        signals[code] = {
            "price": price, "source": "realtime", "stale": False,
            "resistance": None, "ema5": None, "ema20": None, "rsi": None, "candles": [],
            "updated_at": data.get("received_at") or now_iso,
        }
    for code, watch in watches.items():
        signals.setdefault(code, {
            "price": _to_float(watch.get("baseline_price")), "source": "watch_baseline",
            "stale": True, "resistance": None, "ema5": None, "ema20": None, "rsi": None, "candles": [],
            "updated_at": watch.get("set_at") or now_iso,
        })
    return signals


@router.get("/trades")
async def get_trades(mode: str = "paper", page: int = 0, size: int = 20, period: str = "all", stockCode: str = ""):
    return await PGWriter().get_trades(mode=mode, page=page, size=size, period=period, stock_code=stockCode)


@router.get("/trades/pnl/summary")
async def get_pnl_summary(mode: str = "paper"):
    return await PGWriter().get_pnl_summary(mode=mode)


@router.get("/trades/pnl/chart")
async def get_pnl_chart(mode: str = "paper", days: int = 30):
    return await PGWriter().get_pnl_chart(mode=mode, days=days)


@router.get("/trades/performance/stocks")
async def get_stock_performance(mode: str = "paper", period: str = "month"):
    return await PGWriter().get_stock_performance(mode=mode, period=period)


@router.get("/trades/reports/daily")
async def get_daily_reports(mode: str = "paper", period: str = "month"):
    return await PGWriter().get_daily_reports(mode=mode, period=period)
