"""
Python FastAPI — 계산 엔진 서버 (포트 8000)
Spring Boot가 이 서버를 HTTP 호출로 사용.
"""
import asyncio
import copy
import logging
import threading
from dotenv import load_dotenv
load_dotenv()
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime

import yaml
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from kis.rest import KISRestClient
from kis.auth import KISAuth
from kis.domestic import DomesticAPI
from kis.overseas import OverseasAPI
from analysis.screener import Screener
from analysis import backtester as bt_module
from analysis.indicators import calculate_indicators
from trading.order_manager import OrderManager
from trading.risk import RiskManager
from trading.strategy import DayTradingStrategy
from report.logger import TradeLogger
from db.pg_writer import PGWriter, PGWriterSync

# main.py의 기존 매매 루프 재사용
import main as _main

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

_components: dict = {}
_analysis_progress: dict[int, dict] = {}   # run_id → {done, total, current, status}
_cancel_flags: set[int] = set()            # 취소 요청된 run_id
_components_lock = threading.Lock()


def _load_config() -> dict:
    with open("config.yaml") as f:
        return yaml.safe_load(f)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _components
    config = _load_config()
    _components = _main.build_components(config)
    # 서버 시작 시 이전 비정상 종료로 stuck된 running 상태 정리
    try:
        from db.pg_writer import PGWriter
        pg = PGWriter()
        cleaned = await pg.reset_stuck_analysis_runs()
        if cleaned > 0:
            logger.warning("stuck running analysis_run %d건 failed로 정리", cleaned)
    except Exception as e:
        logger.warning("stuck analysis 정리 실패 (무시): %s", e)
    logger.info("FastAPI 엔진 초기화 완료")
    yield
    logger.info("FastAPI 엔진 종료")


app = FastAPI(title="Quant Engine", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080", "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 요청 모델 ──────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    market: str = "domestic"
    horizon: str = "swing"
    top_n: int = 10
    lookback_days: int | None = None   # None이면 config.yaml 기본값 사용


class BacktestRequest(BaseModel):
    stock_code: str
    stock_name: str = ""
    market: str = "domestic"
    exchange: str | None = None
    period_days: int = 60          # start_date 미지정 시 오늘 기준 N일 전
    start_date: str | None = None  # YYYY-MM-DD
    end_date: str | None = None    # YYYY-MM-DD (미지정 시 오늘)


class TradeStartRequest(BaseModel):
    market: str = "domestic"
    mode: str | None = None


class ModeRequest(BaseModel):
    mode: str


# ── 헬스체크 ──────────────────────────────────────────────────────────

@app.get("/health")
def health():
    config = _components.get("config") or {}
    return {
        "status": "ok",
        "trading_active": _main._is_trading_active(),
        "trading_market": _main._trading_market,
        "mode": config.get("mode"),
    }


_balance_cache: dict = {}  # key: market → {data, ts}
_BALANCE_CACHE_SEC = 30

@app.get("/account/balance")
def account_balance(market: str = "domestic", mode: str | None = None):
    config = _components.get("config") or {}
    engine_mode = config.get("mode")
    if mode and engine_mode and mode != engine_mode:
        raise HTTPException(
            status_code=400,
            detail=f"현재 엔진은 {engine_mode} 모드입니다. {mode} 잔고를 조회할 수 없습니다.",
        )

    cached = _balance_cache.get(market)
    if cached and (datetime.now().timestamp() - cached["ts"]) < _BALANCE_CACHE_SEC:
        return cached["data"]

    try:
        if market == "overseas":
            balance  = _components["overseas"].get_balance()
            summary  = balance.get("summary") or {}
            positions = balance.get("positions") or []
            # output2에 현금 예수금 없음 — 보유 주식 평가금액 합산
            stock_value  = sum(_to_float(p.get("ovrs_stck_evlu_amt")) for p in positions)
            purchase_amt = _to_float(summary.get("frcr_buy_amt_smtl1"))
            evlu_pfls    = _to_float(summary.get("tot_evlu_pfls_amt"))
            total = stock_value if stock_value > 0 else purchase_amt + evlu_pfls
            cash  = _components["overseas"].get_foreign_margin_usd()
            total = total + cash  # 주식평가금액 + 현금예수금
            result = {
                "market": "overseas",
                "mode": engine_mode,
                "currency": "USD",
                "cash": cash,
                "totalAssets": total,
                "positionValue": stock_value,
                "positionCount": len(positions),
                "totalPnl":  _to_float(summary.get("ovrs_tot_pfls")),
                "totalPnlPct": _to_float(summary.get("tot_pftrt")),
                "summary": summary,
                "updatedAt": datetime.now().isoformat(),
            }
            _balance_cache[market] = {"data": result, "ts": datetime.now().timestamp()}
            return result

        balance = _components["domestic"].get_balance()
        summary = balance.get("summary") or {}
        cash = _to_float(summary.get("dnca_tot_amt"))
        total = _to_float(summary.get("tot_evlu_amt") or summary.get("nass_amt"))
        position_value = _to_float(summary.get("evlu_amt_smtl_amt"))
        positions = balance.get("positions") or []
        result = {
            "market": "domestic",
            "mode": engine_mode,
            "currency": "KRW",
            "cash": cash,
            "totalAssets": total,
            "positionValue": position_value,
            "positionCount": len(positions),
            "summary": summary,
            "updatedAt": datetime.now().isoformat(),
        }
        _balance_cache[market] = {"data": result, "ts": datetime.now().timestamp()}
        return result
    except Exception as e:
        logger.exception("계좌 잔고 조회 실패")
        raise HTTPException(status_code=502, detail=f"KIS 계좌 잔고 조회 실패: {e}")


def _to_float(value) -> float:
    try:
        return float(str(value or "0").replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


@app.post("/mode")
def set_mode(req: ModeRequest):
    global _components
    if req.mode not in {"paper", "live"}:
        raise HTTPException(status_code=400, detail="mode는 paper/live 중 하나여야 합니다.")
    if _main._is_trading_active():
        raise HTTPException(status_code=409, detail="매매 실행 중에는 엔진 모드를 바꿀 수 없습니다.")

    with _components_lock:
        current_config = _components.get("config") or _load_config()
        if current_config.get("mode") == req.mode:
            return {"status": "ok", "mode": req.mode}

        next_config = copy.deepcopy(current_config)
        next_config["mode"] = req.mode
        _components = _main.build_components(next_config)

    logger.info("엔진 모드 전환 완료: %s", req.mode)
    return {"status": "ok", "mode": req.mode}


@app.get("/regime")
def get_regime():
    """국내/미국 시장 상황 분석 결과 반환."""
    from analysis.market_regime import MarketRegimeDetector
    from datetime import datetime
    domestic = _components.get("domestic")
    if domestic is None:
        raise HTTPException(status_code=503, detail="엔진 초기화 중")

    def _fmt(r):
        return {
            "trend":                r.trend.value,
            "trend_strength":       r.trend_strength,
            "volatility":           r.volatility.value,
            "session":              r.session.value,
            "index_change_pct":     r.index_change_pct,
            "preferred_strategies": r.preferred_strategies,
            "tradeable":            r.tradeable,
            "reason":               r.reason,
        }

    try:
        domestic_regime = MarketRegimeDetector(domestic).detect()
    except Exception as e:
        domestic_regime = None

    config = _components.get("config") or {}
    try:
        from analysis.market_regime import OverseasRegimeDetector
        overseas_regime = OverseasRegimeDetector(domestic, config).detect()
    except Exception as e:
        overseas_regime = _main._overseas_regime(datetime.now(), config)

    result = {"overseas": _fmt(overseas_regime)}
    if domestic_regime:
        result["domestic"] = _fmt(domestic_regime)
    else:
        # 국내 장세 실패 시 기존 단일 포맷 호환 유지
        result["domestic"] = None

    # 기존 단일 포맷 호환 (domestic 필드 최상위 노출)
    if domestic_regime:
        result.update(_fmt(domestic_regime))

    return result


# ── 분석 ──────────────────────────────────────────────────────────────

@app.post("/analyze/rerank")
async def rerank(market: str = "domestic", horizon: str = "daytrade"):
    """
    장 시작 후 수동 재정렬.
    DB의 최근 분석 결과를 실시간 거래량 순위 + 갭 크기로 재정렬해서 반환.
    DB를 갱신하지 않고 응답만 반환 — 화면 표시·WebSocket 구독 순서에만 사용.
    """
    pg     = PGWriter()
    loop   = asyncio.get_event_loop()
    domestic: DomesticAPI   = _components.get("domestic")
    overseas: OverseasAPI   = _components.get("overseas")

    if domestic is None:
        raise HTTPException(status_code=503, detail="엔진 초기화 중")

    # 1) DB에서 최근 분석 결과 로드
    saved = await pg.get_analysis_run_status_latest(market, horizon)
    if not saved:
        raise HTTPException(status_code=404, detail="분석 결과 없음. 먼저 분석을 실행하세요.")

    run_id   = saved["id"]
    results  = await pg.get_results_by_run(run_id)
    if not results:
        raise HTTPException(status_code=404, detail="분석 결과 없음.")

    # 2) 실시간 거래량 순위 조회 (KIS API 기본 정렬 = 거래량 기준)
    try:
        from kis.constants import ExchangeCode
        if market == "domestic":
            vol_rank = await loop.run_in_executor(None, domestic.get_volume_ranking)
            vol_map  = {r["mksc_shrn_iscd"]: idx for idx, r in enumerate(vol_rank)}
        else:
            exch     = ExchangeCode.NASDAQ
            vol_rank = await loop.run_in_executor(None, lambda: overseas.get_volume_ranking(exch))
            vol_map  = {r.get("symb", ""): idx for idx, r in enumerate(vol_rank)}
    except Exception as e:
        logger.warning("거래량순위 조회 실패: %s → 가중치 없이 반환", e)
        vol_map = {}

    # 3) 갭 + 거래대금 기반 재정렬 점수 계산
    reranked = []
    for r in results:
        code       = r["stock_code"]
        base_score = float(r.get("final_score") or 0)

        # 거래대금 순위 보너스: 1위=+30, 10위=+25, 50위=+5, 없음=0
        vol_idx   = vol_map.get(code)
        vol_bonus = max(30 - (vol_idx or 9999) * 0.5, 0) if vol_idx is not None else 0

        # 갭 보너스: KIS 현재가 응답의 prdy_ctrt(전일대비율%) 활용
        gap_bonus = 0
        try:
            if market == "domestic":
                price_data = await loop.run_in_executor(None, lambda c=code: domestic.get_price(c))
                gap_pct    = float(price_data.get("prdy_ctrt") or 0)
                gap_bonus  = min(abs(gap_pct) * 2, 20)
        except Exception:
            pass

        reranked.append({
            **r,
            "volRank":    vol_idx if vol_idx is not None else 9999,
            "volBonus":   round(vol_bonus, 1),
            "gapBonus":   round(gap_bonus, 1),
            "rerankScore": round(base_score + vol_bonus + gap_bonus, 1),
        })

    reranked.sort(key=lambda x: x["rerankScore"], reverse=True)

    # 재정렬 결과를 캐시 파일에 저장 — 매매 시작 시 WebSocket 구독 순서로 사용
    import json as _json
    from pathlib import Path as _Path
    cache = {"market": market, "horizon": horizon, "results": [
        {
            "stock_code":   r["stock_code"],
            "stock_name":   r["stock_name"],
            "exchange":     r.get("exchange") or ("KRX" if market == "domestic" else "NAS"),
            "current_price": r.get("current_price") or 0,
            "final_score":  r.get("final_score") or 0,
            "rerank_score": r["rerankScore"],
        }
        for r in reranked
    ]}
    try:
        _Path("data").mkdir(exist_ok=True)
        _Path("data/candidates_reranked.json").write_text(
            _json.dumps(cache, ensure_ascii=False), encoding="utf-8"
        )
        logger.info("재정렬 캐시 저장 완료: %s %d개", market, len(reranked))
    except Exception as e:
        logger.warning("재정렬 캐시 저장 실패: %s", e)

    return {"market": market, "horizon": horizon, "results": reranked}


@app.post("/analyze")
async def analyze(req: AnalyzeRequest, background_tasks: BackgroundTasks):
    pg = PGWriter()
    if req.horizon not in {"long", "swing", "daytrade"}:
        raise HTTPException(status_code=400, detail="horizon은 long/swing/daytrade 중 하나여야 합니다.")
    if await pg.has_running_analysis(req.market, req.horizon):
        raise HTTPException(status_code=409, detail="분석이 이미 실행 중입니다. 잠시 후 다시 시도하세요.")
    run_id = await pg.create_analysis_run(req.market, req.top_n, req.horizon)
    background_tasks.add_task(
        _run_analysis, run_id, req.market, req.top_n, req.lookback_days, req.horizon
    )
    return {"run_id": run_id, "status": "started"}


@app.get("/analyze/{run_id}/progress")
async def get_progress(run_id: int):
    p = _analysis_progress.get(run_id)
    if p is None:
        saved = await PGWriter().get_analysis_run_status(run_id)
        if saved is None:
            p = {"done": 0, "total": 0, "current": "", "status": "unknown"}
        else:
            done = int(saved.get("result_count") or 0)
            total = max(done, int(saved.get("top_n") or 0))
            p = {
                "done": done,
                "total": total,
                "current": "",
                "status": saved.get("status", "unknown"),
                "error": saved.get("error_msg"),
            }
    pct = int(p["done"] / p["total"] * 100) if p["total"] > 0 else 0
    return {**p, "pct": pct}


@app.post("/analyze/{run_id}/cancel")
async def cancel_analysis(run_id: int):
    """실행 중인 분석을 취소하고 DB를 failed로 정리."""
    _cancel_flags.add(run_id)
    _analysis_progress.pop(run_id, None)
    pg = PGWriter()
    await pg.complete_analysis_run(run_id, "failed", "사용자 취소")
    logger.info("분석 취소 요청 run_id=%d", run_id)
    return {"run_id": run_id, "status": "cancelled"}


async def _run_analysis(
    run_id: int,
    market: str,
    top_n: int,
    lookback_days: int | None = None,
    horizon: str = "swing",
):
    pg = PGWriter()
    _analysis_progress[run_id] = {"done": 0, "total": 0, "current": "", "status": "running"}

    def on_progress(done: int, total: int, current: str):
        if run_id in _cancel_flags:
            raise RuntimeError("사용자 취소")
        _analysis_progress[run_id] = {"done": done, "total": total, "current": current, "status": "running"}

    try:
        screener: Screener = _components["screener"]
        loop = asyncio.get_event_loop()
        if market == "domestic":
            candidates = await loop.run_in_executor(
                None, lambda: screener.run_domestic(
                    top_n=top_n,
                    lookback_days=lookback_days,
                    on_progress=on_progress,
                    horizon=horizon,
                )
            )
        else:
            candidates = await loop.run_in_executor(
                None, lambda: screener.run_overseas(
                    top_n=top_n,
                    lookback_days=lookback_days,
                    on_progress=on_progress,
                    horizon=horizon,
                )
            )
        await pg.save_analysis_results(run_id, candidates)
        await pg.complete_analysis_run(run_id, "completed")
        _analysis_progress[run_id] = {"done": len(candidates), "total": len(candidates),
                                       "current": "", "status": "completed"}
        logger.info(f"분석 완료 run_id={run_id} market={market} horizon={horizon} count={len(candidates)}")
    except Exception as e:
        is_cancel = "사용자 취소" in str(e)
        logger.info("분석 취소 run_id=%d", run_id) if is_cancel else logger.error("분석 실패 run_id=%d: %s", run_id, e)
        if not is_cancel:
            await pg.complete_analysis_run(run_id, "failed", str(e))
        _analysis_progress[run_id] = {**_analysis_progress.get(run_id, {}), "status": "cancelled" if is_cancel else "failed"}
    finally:
        _cancel_flags.discard(run_id)


# ── 백테스트 ──────────────────────────────────────────────────────────

@app.post("/backtest")
async def backtest(req: BacktestRequest):
    from datetime import date, timedelta
    domestic: DomesticAPI = _components["domestic"]
    overseas: OverseasAPI = _components["overseas"]
    loop = asyncio.get_event_loop()

    # 날짜 계산: start_date/end_date 우선, 없으면 period_days 사용
    end_date = date.fromisoformat(req.end_date) if req.end_date else date.today()
    if req.start_date:
        start_date = date.fromisoformat(req.start_date)
    else:
        start_date = end_date - timedelta(days=req.period_days)
    period_days = (end_date - start_date).days
    fetch_start = start_date - timedelta(days=90)  # EMA60 워밍업

    try:
        if req.market == "domestic":
            daily_df = await loop.run_in_executor(
                None,
                lambda: domestic.get_daily_ohlcv(req.stock_code, fetch_start, end_date),
            )
            minute_df = await loop.run_in_executor(
                None,
                lambda: domestic.get_historical_minute_ohlcv(
                    req.stock_code,
                    lookback_days=period_days,
                    candle_minutes=1,
                ),
            )
            daily_ind = calculate_indicators(daily_df)
            context = Screener._build_context(daily_ind, ["gap", "breakout", "pullback"])
            risk_cfg = _components.get("config", {}).get("trading", {})
            result = await loop.run_in_executor(
                None,
                lambda: bt_module.run_strategy_backtest(
                    req.stock_code,
                    minute_df,
                    context=context,
                    stop_loss_pct=risk_cfg.get("stop_loss_pct", 5.0),
                    take_profit_pct=risk_cfg.get("take_profit_pct", 5.0),
                ),
            )
        else:
            from kis.constants import ExchangeCode
            exchange = ExchangeCode(req.exchange or "NAS")
            df = await loop.run_in_executor(
                None,
                lambda: overseas.get_daily_ohlcv(
                    req.stock_code, exchange, fetch_start, end_date
                ),
            )
            result = await loop.run_in_executor(
                None,
                lambda: bt_module.run_backtest(req.stock_code, df, start_from=start_date),
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    result_dict = asdict(result)
    result_dict["stock_name"] = req.stock_name or req.stock_code
    result_dict["period_days"] = period_days
    result_dict["start_date"]  = start_date.isoformat()
    result_dict["end_date"]    = end_date.isoformat()

    # trades는 DB에 저장하지 않고 응답에만 포함
    trades = result_dict.pop("trades", [])

    pg = PGWriter()
    saved_id = await pg.save_backtest_result(result_dict, req.market)
    return {"id": saved_id, **result_dict, "trades": trades}


# ── 매매 ──────────────────────────────────────────────────────────────

@app.post("/trade/start")
def trade_start(req: TradeStartRequest):
    if _main._is_trading_active():
        raise HTTPException(status_code=409, detail="이미 매매 중입니다.")
    config = _components.get("config") or {}
    engine_mode = config.get("mode")
    if req.mode and engine_mode and req.mode != engine_mode:
        raise HTTPException(
            status_code=400,
            detail=f"현재 엔진은 {engine_mode} 모드입니다. {req.mode} 화면에서는 시작할 수 없습니다.",
        )
    _main._start_trading_thread(_components, req.market)
    return {"status": "started", "market": req.market, "mode": engine_mode}


@app.post("/trade/stop")
def trade_stop():
    if not _main._is_trading_active():
        raise HTTPException(status_code=409, detail="매매 중이 아닙니다.")
    _main.stop_trading(_components)
    return {"status": "stopping"}


@app.get("/trade/positions")
async def get_positions():
    pg = PGWriter()
    return await pg.get_positions()


@app.get("/trade/positions/live")
async def get_live_positions(mode: str | None = None):
    config     = _components.get("config") or {}
    engine_mode = config.get("mode")
    if mode and engine_mode and mode != engine_mode:
        return []
    order_mgr = _components.get("order_mgr")
    if order_mgr is None:
        raise HTTPException(status_code=503, detail="엔진 초기화 중")

    rows = order_mgr.get_live_positions()
    if not rows:
        return []

    # 매매 루프 꺼져 있을 때: _last_prices 없으면 REST로 현재가 보완
    from kis.constants import ExchangeCode as _ExchCode
    loop = asyncio.get_event_loop()
    for row in rows:
        code = row["stockCode"]
        # currentPrice == avgPrice 이면 WebSocket 미수신 → REST 조회
        if abs(row["currentPrice"] - row["avgPrice"]) < 0.001:
            try:
                if row["market"] == "domestic":
                    pd = await loop.run_in_executor(
                        None, lambda c=code: _components["domestic"].get_price(c))
                    row["currentPrice"] = float(pd.get("stck_prpr") or row["currentPrice"])
                else:
                    pd = await loop.run_in_executor(
                        None, lambda c=code: _components["overseas"].get_price(c))
                    row["currentPrice"] = float(pd.get("last") or row["currentPrice"])
                # 재계산
                entry, cur, qty = row["avgPrice"], row["currentPrice"], row["quantity"]
                row["unrealizedPnl"] = round(qty * (cur - entry), 4)
                row["unrealizedPct"] = round((cur - entry) / entry * 100, 2) if entry > 0 else 0
                row["marketValue"]   = round(qty * cur, 4)
            except Exception:
                pass
    return rows


@app.get("/trade/orders/pending")
def get_pending_orders(mode: str | None = None):
    config = _components.get("config") or {}
    engine_mode = config.get("mode")
    if mode and engine_mode and mode != engine_mode:
        return []
    order_mgr = _components.get("order_mgr")
    if order_mgr is None:
        raise HTTPException(status_code=503, detail="엔진 초기화 중")
    return order_mgr.get_pending_order_rows()


@app.get("/signals")
def get_signals():
    """종목별 실시간 신호 상태 (차트용)."""
    with _main._signal_state_lock:
        return dict(_main._signal_state)
