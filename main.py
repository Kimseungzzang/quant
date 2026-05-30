import asyncio
import argparse
import logging
import threading
import schedule
import time
import yaml
from pathlib import Path

from kis.auth import KISAuth
from kis.rest import KISRestClient
from kis.domestic import DomesticAPI
from kis.overseas import OverseasAPI
from kis.websocket import (
    KISWebSocket,
    parse_domestic_price,
    parse_overseas_price,
)
from kis.constants import WebSocketTRID, TradeSignal, TradingMode, CloseReason
from analysis.screener import Screener
from trading.risk import RiskManager
from trading.strategy import DayTradingStrategy
from trading.order_manager import OrderManager
from report.logger import TradeLogger
from report.report_gen import ReportGenerator

_KRX = "KRX"


# ── 분봉 집계기 ───────────────────────────────────────────────────────

class CandleAggregator:
    """실시간 틱을 N분봉으로 집계. 완성된 봉만 DataFrame으로 반환."""

    def __init__(self, period_minutes: int, max_candles: int = 300):
        self.period = period_minutes
        self.max_candles = max_candles
        self._current: dict | None = None
        self._completed: list[dict] = []

    def update(self, tick: dict) -> bool:
        """틱 추가. 새 봉이 완성되면 True 반환."""
        time_str = str(tick.get("time", "") or "")
        if len(time_str) < 4:
            return False
        try:
            h     = int(time_str[0:2])
            m     = int(time_str[2:4])
            price = float(tick.get("price", 0) or 0)
            vol   = float(tick.get("vol",   0) or 0)
        except (ValueError, TypeError):
            return False
        if price <= 0:
            return False

        slot = (h * 60 + m) // self.period
        completed = False

        if self._current is None or self._current["slot"] != slot:
            if self._current is not None:
                c = self._current
                self._completed.append({
                    "open": c["open"], "high": c["high"],
                    "low":  c["low"],  "close": c["close"], "volume": c["volume"],
                })
                if len(self._completed) > self.max_candles:
                    self._completed.pop(0)
                completed = True
            self._current = {
                "slot": slot, "open": price, "high": price,
                "low": price, "close": price, "volume": vol,
            }
        else:
            self._current["high"]    = max(self._current["high"], price)
            self._current["low"]     = min(self._current["low"],  price)
            self._current["close"]   = price
            self._current["volume"] += vol

        return completed

    def get_df(self) -> "pd.DataFrame":
        import pandas as pd
        if not self._completed:
            return pd.DataFrame()
        return pd.DataFrame(self._completed)


# ── 전역 매매 스레드 상태 ─────────────────────────────────────────────
_trading_thread: threading.Thread | None = None
_trading_lock = threading.Lock()


def _is_trading_active() -> bool:
    return _trading_thread is not None and _trading_thread.is_alive()


# ── 설정 / 초기화 ─────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def setup_logging(config: dict):
    log_cfg   = config.get("logging", {})
    log_level = getattr(logging, log_cfg.get("level", "INFO"))
    log_file  = log_cfg.get("file", "data/trading.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


def build_components(config: dict) -> dict:
    from db.pg_writer import PGWriterSync
    from analysis.market_regime import MarketRegimeDetector
    from trading.strategy_router import StrategyRouter
    auth         = KISAuth(config)
    client       = KISRestClient(auth)
    domestic     = DomesticAPI(client, config)
    overseas     = OverseasAPI(client, config)
    trade_logger = TradeLogger()
    risk         = RiskManager(config)
    strategy     = DayTradingStrategy(config)
    order_mgr    = OrderManager(domestic, overseas, risk, trade_logger, pg=PGWriterSync())
    screener     = Screener(domestic, overseas, config)
    reporter     = ReportGenerator(trade_logger)
    ws           = KISWebSocket(auth)
    regime_detector = MarketRegimeDetector(domestic)
    strategy_router = StrategyRouter(config)
    return dict(
        auth=auth, config=config,
        domestic=domestic, overseas=overseas,
        trade_logger=trade_logger, risk=risk, strategy=strategy,
        order_mgr=order_mgr, screener=screener, reporter=reporter, ws=ws,
        regime_detector=regime_detector, strategy_router=strategy_router,
    )


# ── Feature 1: 장전 분석 ──────────────────────────────────────────────

def run_analysis(comp: dict, market: str = "domestic") -> list:
    from analysis.market_regime import MarketRegimeDetector
    screener: Screener = comp["screener"]

    # 시장 상황 분석
    regime = None
    if "regime_detector" in comp:
        try:
            regime = comp["regime_detector"].detect()
        except Exception as e:
            logging.getLogger("main").warning("장세 분석 실패: %s → 기본 평가 진행", e)

    candidates = (
        screener.run_domestic(top_n=10, regime=regime) if market == "domestic"
        else screener.run_overseas(top_n=10)
    )

    label = "국내" if market == "domestic" else "해외"
    regime_info = f" [{', '.join(regime.preferred_strategies)}]" if regime and regime.preferred_strategies else ""
    print(f"\n{'='*60}")
    print(f"  {label} 추천 종목 TOP 10{regime_info}")
    print(f"{'='*60}")
    for i, c in enumerate(candidates, 1):
        print(
            f"  {i:2}. [{c.stock_code}] {c.name:<12} "
            f"현재가: {c.current_price:>10,.0f}  "
            f"등락: {c.change_pct:+.2f}%  "
            f"점수: {c.final_score:.1f}  "
            f"승률: {c.backtest.win_rate_pct:.1f}%"
        )
    print()
    _save_candidates(candidates, market)
    return candidates


def _save_candidates(candidates, market: str):
    """분석 결과를 PostgreSQL analysis_results에 저장 (JSON 파일 불필요)."""
    # FastAPI 경로에서는 PGWriter(async)가 저장. main.py 직접 실행 시엔 sync로 저장.
    try:
        import psycopg2, os
        dsn = os.getenv("DATABASE_URL", "postgresql://kimseungzzang@localhost/quant_trading")
        conn = psycopg2.connect(dsn)
        market_label = "domestic" if market == "domestic" else "overseas"
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO analysis_runs (market, top_n, status) VALUES (%s,%s,'completed') RETURNING id",
                    (market_label, len(candidates)),
                )
                run_id = cur.fetchone()[0]
                rows = [
                    (run_id, rank, c.stock_code, c.name, market_label,
                     float(c.current_price), float(c.change_pct), float(c.final_score),
                     float(c.backtest.win_rate_pct), float(c.backtest.total_return_pct),
                     float(c.backtest.max_drawdown_pct), int(c.backtest.total_trades),
                     c.exchange)
                    for rank, c in enumerate(candidates, 1)
                ]
                cur.executemany(
                    """INSERT INTO analysis_results
                       (run_id,rank,stock_code,stock_name,market,current_price,change_pct,
                        final_score,win_rate_pct,backtest_return,max_drawdown,trade_count,exchange)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    rows,
                )
        conn.close()
        logging.getLogger("main").info("분석 결과 %d개 PostgreSQL 저장 완료 (run_id=%d)", len(candidates), run_id)
    except Exception as e:
        logging.getLogger("main").error("PostgreSQL 저장 실패: %s", e)


def _load_candidates(market: str) -> list[dict]:
    """PostgreSQL에서 가장 최근 완료된 분석 결과를 로드."""
    try:
        import psycopg2, os
        dsn = os.getenv("DATABASE_URL", "postgresql://kimseungzzang@localhost/quant_trading")
        market_label = "domestic" if market == "domestic" else "overseas"
        conn = psycopg2.connect(dsn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.stock_code, r.stock_name, r.exchange, r.current_price, r.final_score
                FROM analysis_results r
                WHERE r.run_id = (
                    SELECT id FROM analysis_runs
                    WHERE market = %s AND status = 'completed'
                    ORDER BY run_at DESC LIMIT 1
                )
                ORDER BY r.rank
                """,
                (market_label,),
            )
            rows = cur.fetchall()
        conn.close()
        if not rows:
            logging.getLogger("main").warning("PostgreSQL에 %s 분석 결과 없음", market)
            return []
        return [
            {
                "stock_code":    r[0],
                "name":          r[1],
                "exchange":      r[2] or ("KRX" if market == "domestic" else "NAS"),
                "current_price": float(r[3]),
                "final_score":   float(r[4]),
            }
            for r in rows
        ]
    except Exception as e:
        logging.getLogger("main").error("PostgreSQL 로드 실패: %s", e)
        return []


# ── Feature 2: 실시간 자동매매 ────────────────────────────────────────

# 실전 매매 상수
_LIVE_ENTRY_LIMITS   = {"gap": 1, "breakout": 2, "pullback": 3}
_REGIME_REFRESH_SEC  = 30 * 60   # 30분마다 장세 재감지
_CLOSE_HOUR, _CLOSE_MIN = 15, 20 # 장마감 자동 청산 시각
_MAX_HOLD_DAYS       = {"gap": 0, "breakout": 1, "pullback": 2}  # 전략별 최대 오버나잇
_INTRADAY_ONLY       = {"gap"}    # 15:20 당일 강제 청산 전략


def _build_initial_context(comp: dict, stock_code: str, exchange: str) -> dict:
    """일봉 데이터로 trading loop용 초기 context 구성."""
    from analysis.indicators import calculate_indicators
    try:
        if exchange == _KRX:
            df = comp["domestic"].get_daily_ohlcv(stock_code)
        else:
            from kis.constants import ExchangeCode
            df = comp["overseas"].get_daily_ohlcv(
                stock_code, ExchangeCode(exchange)
            )
        if df.empty or len(df) < 5:
            return {}
        # resistance: 최근 20일 고점
        resistance = float(df["high"].tail(20).max()) if "high" in df.columns else 0.0
        prev_close = float(df["close"].iloc[-1]) if not df.empty else 0.0
        gap_open   = float(df["open"].iloc[-1]) if not df.empty else prev_close
        return {"resistance": resistance, "prev_close": prev_close, "gap_open": gap_open}
    except Exception as e:
        logging.getLogger("main.trading").warning(
            "초기 context 로드 실패 (%s): %s", stock_code, e
        )
        return {}


async def _run_trading_loop(comp: dict):
    """WebSocket 구독 설정 후 실시간 매매 루프 실행."""
    from datetime import datetime, date as _date
    import pandas as _pd

    logger    = logging.getLogger("main.trading")
    ws        = KISWebSocket(comp["auth"])
    order_mgr: OrderManager = comp["order_mgr"]
    router    = comp.get("strategy_router")
    detector  = comp.get("regime_detector")
    mode      = TradingMode(comp["config"]["mode"])

    all_candidates = _load_candidates("domestic") + _load_candidates("overseas")
    if not all_candidates:
        logger.warning("추천 종목 없음")
        return

    # ── 초기 장세 감지 ───────────────────────────────────────────────
    # regime_state: 모든 핸들러가 공유하는 장세 상태 (재감지 시 갱신)
    regime_state: dict = {"regime": None, "updated_at": None}
    if detector:
        try:
            r = detector.detect()
            if not r.tradeable:
                if mode == TradingMode.MOCK:
                    # mock 모드: 세션/추세 제약 무시하고 본장 상승장으로 고정
                    from analysis.market_regime import (
                        MarketRegime, MarketTrend, MarketVolatility, MarketSession,
                    )
                    r = MarketRegime(
                        trend=MarketTrend.UP,
                        trend_strength=60.0,
                        volatility=MarketVolatility.NORMAL,
                        session=MarketSession.MORNING,
                        index_change_pct=0.5,
                        preferred_strategies=["breakout", "pullback"],
                        tradeable=True,
                        reason="mock_override",
                    )
                    logger.info("Mock 모드: 장세 강제 설정 — %s", r)
                else:
                    logger.warning("매매 불가 장세: %s → 매매 시작 취소", r.reason)
                    return
            regime_state["regime"]     = r
            regime_state["updated_at"] = datetime.now()
            logger.info("초기 장세: %s", r)
        except Exception as e:
            logger.warning("장세 분석 실패: %s → 기본 전략 사용", e)

    # ── 공유 상태 (모든 종목 핸들러 공유) ────────────────────────────
    # daily_entries: {stock_code: {strategy_name: count}}
    # last_date:     오늘 날짜 (날짜 변경 시 카운터 리셋)
    live_shared: dict = {"last_date": None, "daily_entries": {}}

    logger.info("%d개 종목 실시간 모니터링 시작", len(all_candidates))

    for c in all_candidates:
        code     = c["stock_code"]
        exchange = c["exchange"]
        # 일봉에서 resistance/prev_close/gap_open 초기 로드 (전략 진입 조건용)
        context  = c.get("context") or _build_initial_context(comp, code, exchange)
        tr_id    = WebSocketTRID.DOMESTIC_PRICE if exchange == _KRX \
            else WebSocketTRID.OVERSEAS_PRICE
        parse_fn = parse_domestic_price if exchange == _KRX else parse_overseas_price

        def make_handler(stock_code, name, exch, _parse_fn, _base_ctx,
                         _regime_state, _shared):
            _aggs = {
                1:  CandleAggregator(1),
                5:  CandleAggregator(5),
                15: CandleAggregator(15),
            }
            # 핸들러 내 수정 가능한 context 복사본
            _ctx      = dict(_base_ctx)
            _prev_p   = [0.0]    # 전 틱 가격 (전일 종가 계산용)
            _last_day        = [None]   # 핸들러의 마지막 처리 날짜
            _pos_entry_dates: dict = {}  # {stock_code: date} 보유일 계산용

            def handler(recv_tr_id: str, fields: list[str]):
                tick = _parse_fn(fields)
                tick["code"] = stock_code
                try:
                    price = float(tick.get("price", 0) or 0)
                except (ValueError, TypeError):
                    return
                if price <= 0:
                    return

                now      = datetime.now()
                now_ts   = _pd.Timestamp(now)
                row_date = now.date()

                # ── 날짜 변경 처리 ────────────────────────────────────
                if row_date != _last_day[0]:
                    _last_day[0] = row_date

                    # 공유 일별 카운터 리셋
                    if _shared["last_date"] != row_date:
                        _shared["last_date"]    = row_date
                        _shared["daily_entries"] = {}

                    # 최대 보유일 초과 포지션 청산
                    entry_date = _pos_entry_dates.get(stock_code)
                    cur_pos = order_mgr.get_open_positions().get(stock_code)
                    if entry_date and cur_pos:
                        days_held = (row_date - entry_date).days
                        max_days  = _MAX_HOLD_DAYS.get(getattr(cur_pos, "strategy", ""), 1)
                        if days_held > max_days:
                            logger.info("[%s] 최대 보유일(%d일) 초과 청산", stock_code, max_days)
                            order_mgr.close_position(
                                stock_code, price,
                                reason=CloseReason.HOLD_PERIOD,
                            )
                            _pos_entry_dates.pop(stock_code, None)

                    # gap_open / prev_close 실시간 갱신
                    if _prev_p[0] > 0:
                        _ctx["prev_close"] = _prev_p[0]
                        _ctx["gap_open"]   = price
                        gap_pct = (price - _prev_p[0]) / _prev_p[0] * 100
                        logger.info("[%s] 일별 context 갱신: prev_close=%.0f "
                                    "gap_open=%.0f gap=%.2f%%",
                                    stock_code, _prev_p[0], price, gap_pct)

                _prev_p[0] = price

                # ── MarketRegime 주기적 재감지 ────────────────────────
                if detector:
                    upd = _regime_state.get("updated_at")
                    if upd is None or (now - upd).total_seconds() >= _REGIME_REFRESH_SEC:
                        try:
                            new_r = detector.detect()
                            _regime_state["regime"]     = new_r
                            _regime_state["updated_at"] = now
                            logger.info("[장세 재감지] %s", new_r)
                        except Exception as e:
                            logger.warning("장세 재감지 실패: %s", e)

                regime = _regime_state.get("regime")

                # ── 마감 자동 청산 (15:20 이후) ───────────────────────
                is_closing = (now.hour > _CLOSE_HOUR or
                              (now.hour == _CLOSE_HOUR and now.minute >= _CLOSE_MIN))
                positions = order_mgr.get_open_positions()

                if is_closing:
                    pos = positions.get(stock_code)
                    if pos and getattr(pos, "strategy", "") in _INTRADAY_ONLY:
                        logger.info("[%s] 장마감 갭전략 강제 청산", stock_code)
                        order_mgr.close_position(stock_code, price, reason=CloseReason.CLOSING_TIME)
                        _pos_entry_dates.pop(stock_code, None)
                    return  # 마감 후 신규 진입 없음

                # ── 봉 집계 ──────────────────────────────────────────
                for agg in _aggs.values():
                    agg.update(tick)
                dfs = {m: agg.get_df() for m, agg in _aggs.items()}

                # ── 진입 판단 ─────────────────────────────────────────
                if stock_code not in positions:
                    if router and regime and regime.tradeable:
                        stock_entries = _shared["daily_entries"].setdefault(stock_code, {})
                        should_enter, strat_name, reason = router.check_entry(
                            regime, dfs, tick, _ctx,
                            entry_counts=stock_entries,
                            entry_limits=_LIVE_ENTRY_LIMITS,
                            now=now_ts,
                        )
                        if should_enter:
                            order_mgr.open_position(stock_code, name, exch, price,
                                                    strategy=strat_name)
                            stock_entries[strat_name] = stock_entries.get(strat_name, 0) + 1
                            _pos_entry_dates[stock_code] = row_date
                    else:
                        order_mgr.on_price_update(stock_code, price, None)

                # ── 청산 판단 ─────────────────────────────────────────
                else:
                    pos = positions[stock_code]
                    position_dict = {
                        "entry_price": pos.entry_price,
                        "strategy":    getattr(pos, "strategy", ""),
                    }
                    if router:
                        should_exit, reason = router.check_exit(dfs, tick, position_dict)
                        if should_exit:
                            order_mgr.close_position(stock_code, price, reason=reason)
                            _pos_entry_dates.pop(stock_code, None)
                            if position_dict["strategy"] == "breakout":
                                _ctx["resistance"] = max(_ctx.get("resistance", 0), price)
                    else:
                        order_mgr.on_price_update(stock_code, price, None)

            return handler

        ws.subscribe(tr_id, code,
                     make_handler(code, c["name"], exchange, parse_fn,
                                  context, regime_state, live_shared))

    comp["ws"] = ws
    await ws.run()


def _start_trading_thread(comp: dict):
    """별도 스레드에서 asyncio 이벤트 루프로 매매 실행."""
    global _trading_thread

    with _trading_lock:
        if _is_trading_active():
            logging.getLogger("main").warning("이미 매매 실행 중")
            return

        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_run_trading_loop(comp))
            finally:
                loop.close()

        _trading_thread = threading.Thread(target=_run, daemon=True, name="trading-loop")
        _trading_thread.start()
        logging.getLogger("main").info("매매 스레드 시작")


def stop_trading(comp: dict):
    """실행 중인 매매를 중단하고 보유 포지션을 모두 청산."""
    logger = logging.getLogger("main.trading")
    if not _is_trading_active():
        logger.info("실행 중인 매매 없음")
        return

    # 보유 포지션 전량 청산
    order_mgr: OrderManager = comp["order_mgr"]
    ws: KISWebSocket         = comp["ws"]

    positions = dict(order_mgr.get_open_positions())
    if positions:
        logger.info("포지션 전량 청산 중 (%d개)...", len(positions))
        for code, pos in positions.items():
            try:
                if pos.is_domestic():
                    price = int(comp["domestic"].get_price(code).get("stck_prpr", 0))
                else:
                    price = float(comp["overseas"].get_price(code).get("last", 0))
                order_mgr.close_position(code, float(price), reason=CloseReason.MANUAL)
            except Exception as e:
                logger.error("청산 실패 (%s): %s", code, e)

    ws.stop()
    logger.info("매매 중단")


# ── 확인 프롬프트 (B 플로우 핵심) ─────────────────────────────────────

def _prompt_and_trade(comp: dict, market: str):
    """
    분석 완료 후 사용자 확인을 받고 매매를 시작.
    스케줄러 스레드와 별도 스레드에서 실행되므로 블로킹해도 무방.
    """
    logger = logging.getLogger("main.prompt")

    candidates = _load_candidates(market)
    if not candidates:
        logger.warning("추천 종목 없음 — 매매 건너뜀")
        return

    if _is_trading_active():
        logger.warning("이미 매매 실행 중 — 건너뜀")
        return

    timeout_sec = comp["config"].get("schedule", {}).get("confirmation_timeout_sec", 0)

    top5 = [f"{c['stock_code']}({c['name']}, {c['final_score']:.0f}점)"
            for c in candidates[:5]]
    label = "국내" if market == "domestic" else "미국"

    print(f"\n{'━'*58}")
    print(f"  [{label}] 분석 완료 — 오늘의 추천 종목")
    for item in top5:
        print(f"    • {item}")
    print(f"{'━'*58}")
    if timeout_sec > 0:
        print(f"  [Enter] 매매 시작   [q+Enter] 건너뜀   ({timeout_sec}초 후 자동 취소)")
    else:
        print(f"  [Enter] 매매 시작   [q+Enter] 건너뜀")
    print(f"{'━'*58} ", end="", flush=True)

    # 타임아웃 처리: 별도 타이머로 stdin을 강제 닫지 않고
    # input()을 직접 호출 (timeout_sec=0 이면 무제한 대기)
    user_input = _input_with_timeout(timeout_sec)

    if user_input is None:
        print("\n  → 시간 초과 — 매매 건너뜀\n")
        logger.info("[%s] 타임아웃 — 매매 건너뜀", label)
        return

    if user_input.strip().lower() == "q":
        print("  → 매매 건너뜀\n")
        logger.info("[%s] 사용자가 매매를 건너뜀", label)
        return

    print("  → 매매 시작!\n")
    _start_trading_thread(comp)


def _input_with_timeout(timeout_sec: int) -> str | None:
    """
    timeout_sec > 0 이면 지정 시간 안에 입력 없으면 None 반환.
    timeout_sec = 0 이면 무제한 대기.
    """
    if timeout_sec <= 0:
        try:
            return input()
        except (EOFError, KeyboardInterrupt):
            return "q"

    result: list[str | None] = [None]
    done = threading.Event()

    def _reader():
        try:
            result[0] = input()
        except (EOFError, KeyboardInterrupt):
            result[0] = "q"
        finally:
            done.set()

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    done.wait(timeout=timeout_sec)
    return result[0]  # 타임아웃이면 None


def run_analysis_and_prompt(comp: dict, market: str):
    """스케줄러가 호출: 분석 실행 → 별도 스레드에서 사용자 확인."""
    run_analysis(comp, market)
    threading.Thread(
        target=_prompt_and_trade,
        args=(comp, market),
        daemon=True,
        name=f"prompt-{market}",
    ).start()


# ── Feature 3: 리포트 ─────────────────────────────────────────────────

def run_report(comp: dict):
    reporter: ReportGenerator = comp["reporter"]
    daily_path = reporter.generate_daily()
    cumul_path = reporter.generate_cumulative()
    print(f"일일 리포트: {daily_path}")
    print(f"누적 리포트: {cumul_path}")


# ── 스케줄러 ─────────────────────────────────────────────────────────

def run_scheduler(comp: dict, config: dict):
    logger = logging.getLogger("main.scheduler")
    sched  = config.get("schedule", {})

    dom_analysis  = sched.get("domestic_analysis_time",   "08:00")
    dom_end       = sched.get("domestic_trading_end_time", "15:20")
    us_analysis   = sched.get("us_analysis_time",          "22:30")
    us_end        = sched.get("us_trading_end_time",        "05:00")
    report_time   = sched.get("report_time",                "16:00")

    schedule.every().day.at(dom_analysis).do(
        run_analysis_and_prompt, comp=comp, market="domestic")
    schedule.every().day.at(dom_end).do(
        stop_trading, comp=comp)

    schedule.every().day.at(us_analysis).do(
        run_analysis_and_prompt, comp=comp, market="overseas")
    schedule.every().day.at(us_end).do(
        stop_trading, comp=comp)

    schedule.every().day.at(report_time).do(
        run_report, comp=comp)

    logger.info(
        "스케줄러 시작\n"
        "  국내: 분석 %s → 확인 후 매매 → 강제청산 %s\n"
        "  미국: 분석 %s → 확인 후 매매 → 강제청산 %s\n"
        "  리포트: %s",
        dom_analysis, dom_end, us_analysis, us_end, report_time,
    )

    while True:
        schedule.run_pending()
        time.sleep(30)


# ── 진입점 ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="KIS Quant Trading System")
    parser.add_argument(
        "--mode",
        choices=["analysis", "trade", "stop", "report", "scheduler"],
        default="scheduler",
        help="실행 모드 (기본: scheduler)",
    )
    parser.add_argument("--market", choices=["domestic", "overseas", "both"], default="both")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config)
    comp = build_components(config)

    logger = logging.getLogger("main")
    mode   = TradingMode(config["mode"])
    logger.info("KIS Quant System 시작 (mode=%s, trading_mode=%s)", args.mode, mode)

    if args.mode == "analysis":
        if args.market in ("domestic", "both"):
            run_analysis(comp, "domestic")
        if args.market in ("overseas", "both"):
            run_analysis(comp, "overseas")

    elif args.mode == "trade":
        # 분석 결과 확인 후 바로 매매 (프롬프트 포함)
        market = args.market if args.market != "both" else "domestic"
        _prompt_and_trade(comp, market)
        # 매매 스레드가 살아있는 동안 대기
        if _is_trading_active() and _trading_thread:
            try:
                _trading_thread.join()
            except KeyboardInterrupt:
                print("\n매매 중단 (Ctrl+C)")
                stop_trading(comp)

    elif args.mode == "stop":
        stop_trading(comp)

    elif args.mode == "report":
        run_report(comp)

    elif args.mode == "scheduler":
        try:
            run_scheduler(comp, config)
        except KeyboardInterrupt:
            print("\n스케줄러 종료 (Ctrl+C)")
            stop_trading(comp)
            run_report(comp)


if __name__ == "__main__":
    main()
