import logging
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from kis.constants import ExchangeCode
from .backtester import (
    run_backtest,
    run_strategy_backtest,
    BacktestResult,
    swing_entry,
)
from .news import get_domestic_news, get_overseas_news, score_sentiment

logger = logging.getLogger(__name__)

_KRX = "KRX"   # 국내주식 내부 구분자 (ExchangeCode는 해외 거래소만 정의)


@dataclass
class Candidate:
    stock_code: str
    name: str
    exchange: str          # "KRX" 또는 ExchangeCode 값
    current_price: float
    change_pct: float
    backtest: BacktestResult
    news_sentiment: float  # -1.0 ~ 1.0
    trading_value: float = 0.0
    horizon: str = "swing"
    atr_pct: float = 0.0   # 단타용: 5일 평균 일중 변동폭 (%)
    final_score: float = 0.0
    context: dict = None   # 전략 진입 시 참조 (저항선, 전일 종가 등)

    def __post_init__(self):
        self.final_score = self._calc_final_score()

    def _calc_final_score(self) -> float:
        if self.horizon == "daytrade":
            # 백테스트 없음 — 기술적 상태 + 유동성 + 변동폭으로만 평가
            score  = self.backtest.signal_score * 0.35   # EMA·RSI·MACD 기술적 상태
            score += _liquidity_score(self.trading_value) * 0.45  # 거래대금 (슬리피지 직결)
            score += _atr_score(self.atr_pct) * 0.20    # 일중 변동폭 (수익 가능성)
            return round(score, 1)

        adjusted_win_rate = _adjusted_win_rate(
            self.backtest.win_rate_pct, self.backtest.total_trades, self.horizon
        )
        return_score = max(min(self.backtest.total_return_pct, 30), -30)
        if self.horizon == "long":
            score = self.backtest.signal_score * 0.5
            score += adjusted_win_rate * 0.15
            score += max(min(self.backtest.total_return_pct, 50), -50) * 0.2
            score += _liquidity_score(self.trading_value) * 0.05
            score += self.news_sentiment * 20 * 0.1
        else:  # swing
            score = self.backtest.signal_score * 0.35
            score += adjusted_win_rate * 0.20
            score += _liquidity_score(self.trading_value) * 0.10
            score += self.news_sentiment * 20 * 0.10
            score += return_score * 0.15
            score += _momentum_score(self.change_pct) * 0.10
        score -= _low_trade_penalty(self.backtest.total_trades, self.horizon)
        return round(score, 1)


class Screener:
    def __init__(self, domestic_api, overseas_api, config: dict):
        self.domestic = domestic_api
        self.overseas = overseas_api
        self.stop_loss = config["trading"]["stop_loss_pct"]
        self.take_profit = config["trading"]["take_profit_pct"]
        self.domestic_top_n = config["universe"]["domestic"]["top_n"]
        self.overseas_top_n = config["universe"]["overseas"]["top_n"]
        self.overseas_exchanges = config["universe"]["overseas"]["exchanges"]
        self._domestic_fallback = config["universe"]["domestic"].get("stocks", [])
        self._overseas_fallback = config["universe"]["overseas"].get("stocks", [])
        bt_cfg = config.get("backtest", {})
        self._bt_days         = bt_cfg.get("lookback_days", 30)
        self._daytrade_bt_days = bt_cfg.get("daytrade_lookback_days", self._bt_days)

    def run_domestic(self, top_n: int = 10, lookback_days: int | None = None,
                     on_progress=None, regime=None, horizon: str = "swing") -> list[Candidate]:
        if horizon == "daytrade":
            top_n = max(top_n, 30)   # 단타는 재정렬 여유분 위해 최소 30개 저장
        logger.info("국내주식 유니버스 스크리닝 시작...")
        if regime:
            logger.info("장세 적용: %s", regime)
        ohlcv_days = lookback_days if lookback_days is not None else 30
        raw_list = self._prepare_domestic_universe(self._get_domestic_universe(), horizon)
        candidates = self._evaluate_batch(
            raw_list[:self.domestic_top_n], _KRX, on_progress, ohlcv_days, regime, horizon
        )
        result = sorted(candidates, key=lambda x: x.final_score, reverse=True)[:top_n]
        logger.info("국내 추천 종목 %d개 선정 완료 (전략: %s)",
                    len(result),
                    ", ".join(regime.preferred_strategies) if regime else "기본")
        return result

    def run_overseas(self, top_n: int = 10, lookback_days: int | None = None,
                     on_progress=None, horizon: str = "swing") -> list[Candidate]:
        if horizon == "daytrade":
            top_n = max(top_n, 30)
        logger.info("해외주식 유니버스 스크리닝 시작...")
        ohlcv_days = lookback_days if lookback_days is not None else 30
        candidates = []
        for exch in [ExchangeCode(e) for e in self.overseas_exchanges]:
            raw_list = self._get_overseas_universe(exch)
            raw_list = self._prepare_overseas_universe(raw_list, horizon)
            batch = self._evaluate_overseas_batch(
                raw_list[:self.overseas_top_n], exch, on_progress, ohlcv_days, horizon
            )
            candidates.extend(batch)
        result = sorted(candidates, key=lambda x: x.final_score, reverse=True)[:top_n]
        logger.info("해외 추천 종목 %d개 선정 완료", len(result))
        return result

    # ── 내부 메서드 ────────────────────────────────────────────────────

    def _get_domestic_universe(self) -> list[dict]:
        """거래량순위 API 시도 → 당일 캐시 → 빈 결과/실패 시 config의 stocks 사용."""
        import json
        cache_path = Path("data/universe_domestic.json")
        today_str  = date.today().isoformat()

        # 당일 캐시 있으면 재사용
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text())
                if cached.get("date") == today_str and cached.get("stocks"):
                    logger.info("국내 유니버스 당일 캐시 사용 (%d개)", len(cached["stocks"]))
                    return cached["stocks"]
            except Exception:
                pass

        reason = ""
        try:
            result = self.domestic.get_volume_ranking()
            if result:
                logger.info("국내 거래량순위 API: %d개 종목", len(result))
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps({"date": today_str, "stocks": result}, ensure_ascii=False))
                return result
            reason = "결과 없음 (장 마감 또는 데이터 없음)"
        except Exception as e:
            reason = str(e)[:80]

        if self._domestic_fallback:
            logger.info("거래량순위 fallback → config.yaml 종목 사용 (%d개) [%s]",
                        len(self._domestic_fallback), reason)
            return [{"mksc_shrn_iscd": code, "hts_kor_isnm": code,
                     "stck_prpr": "0", "prdy_ctrt": "0"}
                    for code in self._domestic_fallback]

        logger.error("국내 유니버스 없음 — config.yaml universe.domestic.stocks 를 채워주세요")
        return []

    def _prepare_domestic_universe(self, raw_list: list[dict], horizon: str) -> list[dict]:
        merged = self._merge_forced_domestic(raw_list)
        if horizon in ("daytrade", "swing"):
            before = len(merged)
            merged = [item for item in merged if not _is_product_name(item.get("hts_kor_isnm", ""))]
            logger.info("국내 유니버스 파생상품 제외: %d → %d개", before, len(merged))
            before = len(merged)
            merged = [item for item in merged if not _is_bio_pharma(item.get("hts_kor_isnm", ""))]
            logger.info("국내 유니버스 바이오/제약 제외: %d → %d개", before, len(merged))

        if horizon == "daytrade":
            merged = sorted(
                merged,
                key=lambda item: (
                    _to_float(item.get("acml_tr_pbmn")),
                    _to_float(item.get("acml_vol")),
                ),
                reverse=True,
            )
            logger.info("단타 유니버스 거래대금순 정렬")
        return merged

    def _merge_forced_domestic(self, raw_list: list[dict]) -> list[dict]:
        merged = list(raw_list)
        seen = {item.get("mksc_shrn_iscd") for item in merged}
        for code in self._domestic_fallback:
            if code in seen:
                continue
            try:
                price = self.domestic.get_price(code)
            except Exception as e:
                logger.warning("[%s] 핵심 종목 현재가 조회 실패, 기본 유니버스에만 추가: %s", code, e)
                price = {}
            merged.append({
                "mksc_shrn_iscd": code,
                "hts_kor_isnm": price.get("hts_kor_isnm") or code,
                "stck_prpr": price.get("stck_prpr", "0"),
                "prdy_ctrt": price.get("prdy_ctrt", "0"),
                "acml_vol": price.get("acml_vol", "0"),
                "acml_tr_pbmn": price.get("acml_tr_pbmn", "0"),
                "data_rank": "forced",
            })
            seen.add(code)
        return merged

    def _prepare_overseas_universe(self, raw_list: list[dict], horizon: str) -> list[dict]:
        """해외 유니버스에서 ETF/레버리지/인버스 상품 제거."""
        if horizon not in ("daytrade", "swing"):
            return raw_list
        before = len(raw_list)
        filtered = [
            item for item in raw_list
            if not _is_overseas_product(item.get("name", ""), item.get("symb", ""))
        ]
        logger.info("해외 유니버스 ETF/레버리지 제외: %d → %d개", before, len(filtered))
        return filtered

    def _get_overseas_universe(self, exchange: ExchangeCode) -> list[dict]:
        """거래량순위 API 시도 → 당일 캐시 → 빈 결과/실패 시 config의 stocks 사용."""
        import json
        cache_path = Path(f"data/universe_overseas_{exchange}.json")
        today_str  = date.today().isoformat()

        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text())
                if cached.get("date") == today_str and cached.get("stocks"):
                    logger.info("해외 유니버스 당일 캐시 사용 (%s, %d개)", exchange, len(cached["stocks"]))
                    return cached["stocks"]
            except Exception:
                pass

        reason = ""
        try:
            result = self.overseas.get_volume_ranking(exchange)
            if result:
                logger.info("해외 거래량순위 API (%s): %d개 종목", exchange, len(result))
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps({"date": today_str, "stocks": result}, ensure_ascii=False))
                return result
            reason = "결과 없음 (장 마감 또는 데이터 없음)"
        except Exception as e:
            reason = str(e)[:80]

        fallback = [s for s in self._overseas_fallback
                    if s.get("exchange") == str(exchange)]
        if fallback:
            logger.info("거래량순위 fallback → config.yaml 종목 사용 (%d개, %s) [%s]",
                        len(fallback), exchange, reason)
            return [{"symb": s["code"], "name": s["name"],
                     "last": "0", "rate": "0"}
                    for s in fallback]

        return []

    def _evaluate_batch(self, stock_list: list[dict], exchange: str,
                        on_progress=None, ohlcv_days: int = 30, regime=None,
                        horizon: str = "swing") -> list[Candidate]:
        candidates = []
        end_date = date.today()
        fetch_start    = end_date - timedelta(days=ohlcv_days + 90)
        analysis_start = end_date - timedelta(days=ohlcv_days)
        total = len(stock_list)

        # regime에서 우선 전략 추출 (없으면 기본 평가)
        preferred = regime.preferred_strategies if regime else []

        for idx, item in enumerate(stock_list):
            code = item.get("mksc_shrn_iscd", "")
            if not code:
                continue
            if on_progress:
                on_progress(idx, total, code)
            try:
                # ① OHLCV (분석기간 + EMA60 워밍업)
                df = self.domestic.get_daily_ohlcv(code, fetch_start, end_date)
                if df.empty:
                    logger.warning("[%s] ① OHLCV 비어있음 — 스킵", code)
                    continue
                name   = df.attrs.get("name",       item.get("hts_kor_isnm", code))
                price  = df.attrs.get("price",       float(item.get("stck_prpr", 0) or 0))
                change = df.attrs.get("change_pct",  float(item.get("prdy_ctrt",  0) or 0))
                trading_value = _resolve_trading_value(item, price)
                logger.info("[%s] %s | ① OHLCV %d일치 (현재가 %s, 등락 %+.2f%%, 거래대금 %.0f억)",
                            code, name, len(df), f"{price:,.0f}", change, trading_value / 100_000_000)

                # ② 지표 계산 (backtester 내부에서 수행, 여기선 미리 계산해서 로그)
                from .indicators import calculate_indicators, get_signal_score
                df_ind = calculate_indicators(df)
                row = df_ind.iloc[-1]
                logger.info("[%s] ② 지표 | EMA5=%s EMA20=%s EMA60=%s | RSI=%.1f | MACD_hist=%s | vol_ratio=%s",
                            code,
                            f"{row.get('ema5'):.0f}"    if row.get('ema5')    else "N/A",
                            f"{row.get('ema20'):.0f}"   if row.get('ema20')   else "N/A",
                            f"{row.get('ema60'):.0f}"   if row.get('ema60')   else "N/A",
                            row.get("rsi") or 0,
                            f"{row.get('macd_hist'):.1f}" if row.get('macd_hist') else "N/A",
                            f"{row.get('vol_ratio'):.2f}" if row.get('vol_ratio') else "N/A")
                signal_score = get_signal_score(df_ind)
                ema_ok  = (row.get('ema5') or 0) > (row.get('ema20') or 0) > (row.get('ema60') or 0)
                logger.info("[%s] ② 지표점수=%s | EMA정배열=%s | RSI구간=%s",
                            code, signal_score,
                            "✅" if ema_ok else "❌",
                            "50~70(매수적합)" if 50 <= (row.get("rsi") or 0) <= 70 else f"{row.get('rsi') or 0:.1f}")

                context = self._build_context(df_ind, preferred)

                if horizon == "daytrade":
                    # ③ 단타: 백테스트 없음 — ATR(일중 변동폭)만 계산
                    atr_pct = _calc_atr_pct(df)
                    bt = BacktestResult(stock_code=code, signal_score=round(signal_score, 1))
                    logger.info("[%s] ③ 단타 ATR=%.2f%% (5일 평균 일중 변동폭)", code, atr_pct)
                else:
                    # ③ 장타/스윙 백테스트 (일봉 기반)
                    bt = run_backtest(
                        code,
                        df,
                        self.stop_loss,
                        self.take_profit,
                        entry_fn=swing_entry if horizon == "swing" else None,
                        start_from=analysis_start,
                    )
                    logger.info("[%s] ③ %s 백테스트(일봉/%d일) | 거래=%d회 | 승률=%.1f%% | 수익률=%+.2f%% | MDD=%.2f%% | Sharpe=%.2f",
                                code, horizon, ohlcv_days,
                                bt.total_trades, bt.win_rate_pct,
                                bt.total_return_pct, bt.max_drawdown_pct, bt.sharpe_ratio)

                # ④ 뉴스 감성 (단타는 점수에 미반영 — 스킵)
                if horizon != "daytrade":
                    articles  = get_domestic_news(code)
                    sentiment = score_sentiment(articles)
                    logger.info("[%s] ④ 뉴스 | 기사=%d개 | 감성점수=%+.2f",
                                code, len(articles), sentiment)
                else:
                    sentiment = 0.0

                # ⑤ regime 필터 — 장세에 맞지 않는 종목 제외
                if preferred and not context.get("regime_fit"):
                    logger.info("[%s] 장세 부적합 (전략=%s) — 제외", code, preferred)
                    continue

                # ⑥ 최종 점수
                cand = Candidate(
                    code, name, exchange, price, change, bt, sentiment,
                    trading_value=trading_value, horizon=horizon,
                    atr_pct=atr_pct if horizon == "daytrade" else 0.0,
                )
                cand.context = context  # 매매 루프에서 전략 진입 시 사용
                if horizon == "daytrade":
                    logger.info("[%s] ⑥ 최종점수=%.1f | signal=%.1f | 유동성=%.1f | ATR점수=%.1f (%.2f%%)",
                                code, cand.final_score, bt.signal_score,
                                _liquidity_score(trading_value),
                                _atr_score(atr_pct), atr_pct)
                else:
                    logger.info("[%s] ⑥ 최종점수=%.1f | signal=%.1f | 보정승률=%.1f | 거래대금점수=%.1f | 수익=%.1f",
                                code, cand.final_score,
                                bt.signal_score,
                                _adjusted_win_rate(bt.win_rate_pct, bt.total_trades, horizon),
                                _liquidity_score(trading_value),
                                max(min(bt.total_return_pct, 30), -30))
                logger.info("─" * 60)
                candidates.append(cand)
            except Exception as e:
                logger.warning("종목 평가 실패 (%s): %s", code, e)
        return candidates

    @staticmethod
    def _build_context(df, preferred_strategies: list[str]) -> dict:
        """
        종목별 전략 context 생성.
        - resistance: 최근 20일 고점 (돌파 전략용)
        - prev_close:  전일 종가 (갭 전략용)
        - gap_open:    당일 시가 (갭 전략용)
        - regime_fit:  현재 장세 전략에 적합한지 여부
        """
        if df.empty:
            return {"regime_fit": False}

        context: dict = {}

        # 저항선: 최근 20일 고가
        if "high" in df.columns and len(df) >= 2:
            context["resistance"] = float(df["high"].tail(20).max())

        # 전일 종가 / 당일 시가 (갭)
        if "close" in df.columns and len(df) >= 2:
            context["prev_close"] = float(df["close"].iloc[-2])
        if "open" in df.columns and len(df) >= 1:
            context["gap_open"] = float(df["open"].iloc[-1])

        # regime_fit: 전략이 없거나 종목이 조건에 맞으면 True
        if not preferred_strategies:
            context["regime_fit"] = True
            return context

        fit = False
        last = df.iloc[-1]
        e5  = last.get("ema5")
        e20 = last.get("ema20")

        if "breakout" in preferred_strategies:
            # 최근 고점 근처 (5% 이내) 종목
            resistance = context.get("resistance", 0)
            cur_price  = float(last.get("close", 0) or 0)
            if resistance > 0 and cur_price > resistance * 0.95:
                fit = True

        if "pullback" in preferred_strategies:
            # 상승 추세 + 단기 조정 중
            import pandas as _pd
            if _pd.notna(e5) and _pd.notna(e20):
                if float(e5) > float(e20):  # 상승 추세
                    cur = float(last.get("close", 0) or 0)
                    if cur < float(e5):      # 단기 이평 아래 눌림
                        fit = True

        if "gap" in preferred_strategies:
            prev_close = context.get("prev_close", 0)
            gap_open   = context.get("gap_open", 0)
            if prev_close > 0 and gap_open > prev_close * 1.02:
                fit = True

        context["regime_fit"] = fit
        return context

    def _evaluate_overseas_batch(self, stock_list: list[dict], exchange: str,
                                on_progress=None, ohlcv_days: int = 30,
                                horizon: str = "swing") -> list[Candidate]:
        candidates = []
        end_date = date.today()
        fetch_start    = end_date - timedelta(days=ohlcv_days + 90)
        analysis_start = end_date - timedelta(days=ohlcv_days)
        total = len(stock_list)

        for idx, item in enumerate(stock_list):
            code = item.get("symb", "")
            if not code:
                continue
            if on_progress:
                on_progress(idx, total, code)
            try:
                # ① OHLCV (분석기간 + EMA60 워밍업)
                df = self.overseas.get_daily_ohlcv(code, ExchangeCode(str(exchange)),
                                                   fetch_start, end_date)
                name   = item.get("name") or df.attrs.get("name", code)
                price  = df.attrs.get("price",      float(item.get("last", 0) or 0))
                change = df.attrs.get("change_pct", float(item.get("rate",  0) or 0))
                if df.empty:
                    logger.warning("[%s] ① OHLCV 비어있음 — 스킵", code)
                    continue
                logger.info("[%s] %s | ① OHLCV %d일치 (현재가 %.2f, 등락 %+.2f%%)",
                            code, name, len(df), price, change)

                # ② 지표
                from .indicators import calculate_indicators, get_signal_score
                df_ind = calculate_indicators(df)
                row = df_ind.iloc[-1]
                signal_score = get_signal_score(df_ind)
                logger.info("[%s] ② 지표점수=%s | EMA5=%s EMA20=%s | RSI=%.1f | vol_ratio=%s",
                            code, signal_score,
                            f"{row.get('ema5'):.2f}"    if row.get('ema5')    else "N/A",
                            f"{row.get('ema20'):.2f}"   if row.get('ema20')   else "N/A",
                            row.get("rsi") or 0,
                            f"{row.get('vol_ratio'):.2f}" if row.get('vol_ratio') else "N/A")

                if horizon == "daytrade":
                    # 해외 단타도 백테스트 없음 — ATR만 계산
                    atr_pct = _calc_atr_pct(df)
                    bt = BacktestResult(stock_code=code, signal_score=round(signal_score, 1))
                    logger.info("[%s] ③ 단타 ATR=%.2f%% (5일 평균 일중 변동폭)", code, atr_pct)
                else:
                    bt = run_backtest(
                        code,
                        df,
                        self.stop_loss,
                        self.take_profit,
                        entry_fn=swing_entry if horizon == "swing" else None,
                        start_from=analysis_start,
                    )
                    logger.info("[%s] ③ %s 백테스트 | 거래=%d회 | 승률=%.1f%% | 수익률=%+.2f%%",
                                code, horizon, bt.total_trades, bt.win_rate_pct, bt.total_return_pct)

                # ④ 뉴스 감성 (단타는 점수에 미반영 — 스킵)
                if horizon != "daytrade":
                    articles  = get_overseas_news(code)
                    sentiment = score_sentiment(articles)
                    logger.info("[%s] ④ 뉴스 | 기사=%d개 | 감성=%+.2f", code, len(articles), sentiment)
                else:
                    sentiment = 0.0

                # ⑤ 최종
                trading_value = _resolve_trading_value(item, price) * 1300
                cand = Candidate(
                    code, name, exchange, price, change, bt, sentiment,
                    trading_value=trading_value, horizon=horizon,
                    atr_pct=atr_pct if horizon == "daytrade" else 0.0,
                )
                if horizon == "daytrade":
                    logger.info("[%s] ⑤ 최종점수=%.1f | signal=%.1f | 유동성=%.1f | ATR점수=%.1f (%.2f%%)",
                                code, cand.final_score, bt.signal_score,
                                _liquidity_score(trading_value), _atr_score(atr_pct), atr_pct)
                else:
                    logger.info("[%s] ⑤ 최종점수=%.1f", code, cand.final_score)
                logger.info("─" * 60)
                candidates.append(cand)
            except Exception as e:
                logger.warning("종목 평가 실패 (%s): %s", code, e)
        return candidates


def _to_float(value) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _resolve_trading_value(item: dict, price: float) -> float:
    trading_value = _to_float(
        item.get("acml_tr_pbmn") or item.get("tr_pbmn") or item.get("tamt")
    )
    if trading_value > 0:
        return trading_value
    volume = _to_float(item.get("acml_vol") or item.get("tvol") or item.get("volume"))
    return max(price, 0.0) * max(volume, 0.0)


def _liquidity_score(trading_value: float) -> float:
    if trading_value <= 0:
        return 0.0
    # 100억원 이하 0점, 1조원 이상 100점으로 로그 스케일 보정.
    low = math.log10(10_000_000_000)
    high = math.log10(1_000_000_000_000)
    score = (math.log10(trading_value) - low) / (high - low) * 100
    return round(max(0.0, min(score, 100.0)), 1)


def _adjusted_win_rate(win_rate: float, trades: int, horizon: str) -> float:
    min_trades = {"daytrade": 10, "swing": 3, "long": 2}.get(horizon, 3)
    confidence = min(max(trades, 0) / min_trades, 1.0)
    return round(win_rate * confidence, 1)


def _low_trade_penalty(trades: int, horizon: str) -> float:
    if horizon == "daytrade":
        if trades == 0:
            return 18.0
        if trades < 5:
            return 10.0
        if trades < 10:
            return 4.0
    if horizon == "swing":
        if trades == 1:
            return 10.0
        if trades == 2:
            return 5.0
    return 0.0


def _momentum_score(change_pct: float) -> float:
    # 단타/스윙은 당일 과열도 감점한다. +2~+8% 구간을 가장 선호.
    if change_pct < -3:
        return 10.0
    if change_pct < 0:
        return 30.0
    if change_pct <= 2:
        return 55.0
    if change_pct <= 8:
        return 100.0
    if change_pct <= 15:
        return 60.0
    return 25.0


def _atr_score(atr_pct: float) -> float:
    """5일 평균 일중 변동폭(%) 기반 단타 적합도 (0~100).
    너무 안 움직이면 수익 불가, 너무 많이 움직이면 리스크 과다.
    스윗스팟: 2~4%
    """
    if atr_pct <= 0:
        return 0.0
    if atr_pct < 1.0:
        return 10.0
    if atr_pct < 2.0:
        return 50.0
    if atr_pct <= 4.0:
        return 100.0
    if atr_pct <= 6.0:
        return 65.0
    return 30.0


def _calc_atr_pct(df) -> float:
    """일봉 DataFrame에서 최근 5일 평균 일중 변동폭(%) 계산."""
    try:
        import pandas as pd
        if df is None or df.empty or len(df) < 5:
            return 0.0
        recent = df.tail(5).copy()
        recent["range_pct"] = (recent["high"] - recent["low"]) / recent["close"] * 100
        return round(float(recent["range_pct"].mean()), 2)
    except Exception:
        return 0.0


def _is_product_name(name: str) -> bool:
    product_tokens = (
        "KODEX", "TIGER", "SOL", "RISE", "ACE", "HANARO", "KOSEF",
        "KBSTAR", "ARIRANG", "TIMEFOLIO", "ETF", "ETN", "인버스",
        "레버리지", "선물",
    )
    return any(token in name for token in product_tokens)


def _is_bio_pharma(name: str) -> bool:
    """바이오·제약·의료 종목 여부. 이벤트 드리븐이라 기술적 분석 신뢰도 낮음."""
    tokens = (
        "바이오", "제약", "헬스케어", "메디", "파마", "팜",
        "의약", "의료", "치료", "줄기세포", "진단", "백신",
        "항체", "신약", "유전",
    )
    return any(t in name for t in tokens)


def _is_overseas_product(name: str, symb: str) -> bool:
    """해외 ETF / 레버리지 / 인버스 상품 여부 판단."""
    name_up = name.upper()
    symb_up = symb.upper()

    # 이름 기반: ETF 운용사·구조 키워드
    name_tokens = (
        "ETF", "FUND", "TRUST", "INDEX",
        "BULL", "BEAR", "ULTRA", "SHORT", "INVERSE",
        "PROSHARES", "ISHARES", "VANGUARD", "SPDR",
        "INVESCO", "VANECK", "DIREXION", "WISDOMTREE",
        "GLOBAL X", "AMPLIFY", "ARK ", "FIRST TRUST",
        "2X ", "3X ", " 2X", " 3X",
    )
    if any(t in name_up for t in name_tokens):
        return True

    # 심볼 기반: 자주 등장하는 ETF/레버리지 티커 직접 차단
    known_products = {
        # 주요 광의 ETF
        "SPY", "QQQ", "IWM", "GLD", "SLV", "TLT", "HYG", "LQD",
        "XLF", "XLE", "XLK", "XLV", "XLI", "XLU", "XLP", "XLY", "XLB", "XLRE",
        "VTI", "VOO", "VEA", "VWO", "BND", "AGG",
        "EEM", "EFA", "FXI", "KWEB", "MCHI",
        # 레버리지/인버스
        "TQQQ", "SQQQ", "SOXL", "SOXS",
        "SPXL", "SPXS", "UPRO", "SPXU",
        "TNA", "TZA", "FAS", "FAZ",
        "LABU", "LABD", "NUGT", "DUST",
        "UVXY", "VXX", "SVXY", "VIXY",
        "CURE", "NAIL", "WEBL", "WEBS",
        # 섹터 ETF (레버리지)
        "TECL", "TECS", "FNGU", "FNGD",
        "ROM", "REW", "USD", "SSO", "SDS",
    }
    if symb_up in known_products:
        return True

    return False
