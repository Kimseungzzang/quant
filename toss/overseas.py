"""
토스증권 해외주식(미국) facade — kis/overseas.py::OverseasAPI 와 동일한
메서드명/시그니처/반환 형태를 제공한다.

거래소 구분(NASDAQ/NYSE/AMEX)은 토스 주문/시세 API에 없다 — exchange 인자는
시그니처 호환을 위해 받되 내부적으로 무시한다 (심볼=영문 티커만으로 US 라우팅됨).
"""
import logging
import pickle
from datetime import date, timedelta

import pandas as pd

from .api import TossBrokerAPI
from .domestic import _order_to_kis_row

logger = logging.getLogger(__name__)

_CANDLE_CACHE_PREFIX = "toss:candles:1min:overseas:"
_CANDLE_CACHE_TTL = 86400  # 1일


class TossOverseasAPI:
    def __init__(self, broker: TossBrokerAPI, config: dict, redis_client=None):
        self.broker = broker
        self.config = config
        self._r = redis_client

    # ── 시세 조회 ──────────────────────────────────────────────────────

    def get_price(self, stock_code: str, exchange=None) -> dict:
        prices = self.broker.get_prices([stock_code])
        p = prices[0] if prices else {}
        current = self._to_float(p.get("lastPrice"))
        return {"last": current, "price": current, "current_price": current, "output": p}

    def get_daily_ohlcv(
        self,
        stock_code: str,
        exchange=None,
        start_date: date | None = None,
        end_date: date | None = None,
        period=None,
    ) -> pd.DataFrame:
        if end_date is None:
            end_date = date.today()
        if start_date is None:
            start_date = end_date - timedelta(days=30)
        count = min(200, max(1, (end_date - start_date).days + 5))

        result = self.broker.get_candles(stock_code, interval="1d", count=count)
        df = self._candles_to_df(result.get("candles", []))
        if not df.empty:
            df = df[(df["datetime"].dt.date >= start_date) & (df["datetime"].dt.date <= end_date)].reset_index(drop=True)

        df.attrs["name"] = stock_code
        if len(df) >= 2:
            last_close, prev_close = float(df.iloc[-1]["close"]), float(df.iloc[-2]["close"])
            df.attrs["price"] = last_close
            df.attrs["change_pct"] = round((last_close - prev_close) / prev_close * 100, 2) if prev_close else 0.0
        elif len(df) == 1:
            df.attrs["price"] = float(df.iloc[-1]["close"])
            df.attrs["change_pct"] = 0.0
        else:
            df.attrs["price"] = 0.0
            df.attrs["change_pct"] = 0.0
        return df

    def get_historical_minute_ohlcv(
        self,
        stock_code: str,
        exchange=None,
        lookback_days: int = 2,
        candle_minutes: int = 1,
    ) -> pd.DataFrame:
        """1분봉을 Redis 캐시 + 증분 조회로 반환. 캐시 없이 매번 전체 페이지네이션하면
        지표 캐시 5분 주기 갱신마다 종목당 최대 50페이지를 새로 긁어서 갱신 주기가
        수십 분~100분대로 밀리는 문제가 있었음 (국내쪽엔 이미 있던 캐싱을 여기도 적용)."""
        cached_df = self._load_candle_cache(stock_code)
        if cached_df is not None and not cached_df.empty:
            cutoff = date.today() - timedelta(days=lookback_days)
            if cached_df["datetime"].dt.date.min() <= cutoff:
                try:
                    new_df = self._fetch_minute_range(stock_code, since=cached_df["datetime"].max())
                except Exception as e:
                    logger.warning("[%s] 토스 해외 분봉 캐시 업데이트 실패 → 기존 캐시 사용: %s", stock_code, e)
                    new_df = pd.DataFrame()
                if not new_df.empty:
                    combined = pd.concat([cached_df, new_df]).drop_duplicates("datetime")
                    combined = combined.sort_values("datetime").reset_index(drop=True)
                    self._save_candle_cache(stock_code, combined)
                    cached_df = combined

                cutoff_dt = pd.Timestamp(date.today() - timedelta(days=lookback_days))
                result = cached_df[cached_df["datetime"] >= cutoff_dt]
                return self._aggregate(result, candle_minutes)

        logger.info("[%s] 토스 해외 과거 %d일 분봉 수집 시작 (캐시 없음)...", stock_code, lookback_days)
        try:
            df = self._fetch_minute_range(stock_code, lookback_days=lookback_days)
        except Exception as e:
            logger.warning("[%s] 토스 해외 분봉 수집 실패: %s", stock_code, e)
            return pd.DataFrame()
        if not df.empty:
            self._save_candle_cache(stock_code, df)
        return self._aggregate(df, candle_minutes)

    def _fetch_minute_range(
        self,
        stock_code: str,
        lookback_days: int = 2,
        since: "pd.Timestamp | None" = None,
    ) -> pd.DataFrame:
        cutoff = since.to_pydatetime() if since is not None else (
            pd.Timestamp.now() - pd.Timedelta(days=lookback_days)
        ).to_pydatetime()
        all_rows: list[dict] = []
        before: str | None = None
        max_pages = 50

        for _ in range(max_pages):
            result = self.broker.get_candles(stock_code, interval="1m", count=200, before=before)
            candles = result.get("candles", [])
            if not candles:
                break
            all_rows.extend(candles)
            oldest_ts = candles[-1].get("timestamp")
            if not oldest_ts or pd.to_datetime(oldest_ts).tz_localize(None) <= cutoff:
                break
            next_cursor = result.get("nextBefore")
            if not next_cursor:
                break
            before = next_cursor

        df = self._candles_to_df(all_rows)
        if df.empty:
            return df
        cutoff_ts = pd.Timestamp(cutoff)
        return df[df["datetime"] >= cutoff_ts].sort_values("datetime").drop_duplicates("datetime").reset_index(drop=True)

    def _load_candle_cache(self, stock_code: str) -> pd.DataFrame | None:
        if not self._r:
            return None
        try:
            raw = self._r.get(f"{_CANDLE_CACHE_PREFIX}{stock_code}")
            if not raw:
                return None
            return pickle.loads(raw)
        except Exception:
            return None

    def _save_candle_cache(self, stock_code: str, df: pd.DataFrame) -> None:
        if not self._r:
            return
        try:
            self._r.set(f"{_CANDLE_CACHE_PREFIX}{stock_code}", pickle.dumps(df), ex=_CANDLE_CACHE_TTL)
        except Exception as e:
            logger.warning("[%s] 토스 해외 분봉 캐시 저장 실패: %s", stock_code, e)

    @staticmethod
    def _aggregate(df: pd.DataFrame, candle_minutes: int) -> pd.DataFrame:
        if df.empty or candle_minutes == 1:
            return df
        df = df.copy()
        df["slot"] = df["datetime"].dt.floor(f"{candle_minutes}min")
        agg = df.groupby("slot").agg(
            open=("open", "first"), high=("high", "max"), low=("low", "min"),
            close=("close", "last"), volume=("volume", "sum"),
        ).reset_index().rename(columns={"slot": "datetime"})
        return agg.sort_values("datetime").reset_index(drop=True)

    def get_volume_ranking(self, exchange=None) -> list[dict]:
        data = self.broker.get_rankings(
            ranking_type="MARKET_TRADING_AMOUNT", market_country="US", duration="realtime", count=100,
        )
        rankings = data.get("rankings", [])

        symbols = [r.get("symbol", "") for r in rankings if r.get("symbol")]
        names: dict[str, str] = {}
        leveraged: set[str] = set()
        if symbols:
            try:
                for s in self.broker.get_stocks(symbols):
                    sym = s.get("symbol", "")
                    names[sym] = s.get("name", "")
                    lev = s.get("leverageFactor")
                    if lev is not None and str(lev) != "1":
                        leveraged.add(sym)
            except Exception as e:
                logger.warning("종목명 조회 실패(해외 순위): %s", e)

        results = []
        for r in rankings:
            code = r.get("symbol", "")
            if code in leveraged:
                continue  # 레버리지/인버스 ETF 제외 (계좌에서 매매 불가)
            price = r.get("price") or {}
            change_rate = self._to_float(price.get("changeRate"))
            results.append({
                "mksc_shrn_iscd": code,
                "hts_kor_isnm": names.get(code, code),
                "data_rank": r.get("rank", ""),
                "stck_prpr": price.get("lastPrice", ""),
                "prdy_ctrt": round(change_rate * 100, 2) if change_rate is not None else "",
            })
        return results

    # ── 주문 ────────────────────────────────────────────────────────────

    def buy(self, stock_code: str, exchange, qty: int, price: float, order_type=None) -> dict:
        result = self.broker.create_order(
            symbol=stock_code, side="BUY", order_type="LIMIT",
            quantity=str(int(qty)), price=f"{float(price):.2f}",
        )
        logger.info("토스 해외 매수: %s %d주 @ %.2f (주문번호: %s)",
                    stock_code, qty, price, result.get("orderId"))
        return {"ODNO": result.get("orderId", "")}

    def sell(self, stock_code: str, exchange, qty: int, price: float, order_type=None) -> dict:
        result = self.broker.create_order(
            symbol=stock_code, side="SELL", order_type="LIMIT",
            quantity=str(int(qty)), price=f"{float(price):.2f}",
        )
        logger.info("토스 해외 매도: %s %d주 @ %.2f (주문번호: %s)",
                    stock_code, qty, price, result.get("orderId"))
        return {"ODNO": result.get("orderId", "")}

    # ── 계좌 조회 ────────────────────────────────────────────────────────

    def get_balance(self, exchange=None) -> dict:
        holdings = self.broker.get_holdings()
        items = holdings.get("items", [])
        positions = []
        for it in items:
            if it.get("marketCountry") != "US":
                continue
            market_value = it.get("marketValue") or {}
            profit_loss = it.get("profitLoss") or {}
            rate = self._to_float(profit_loss.get("rate"))
            positions.append({
                "ovrs_excg_cd": "NASD",  # 토스는 거래소를 구분하지 않음 — dedup 키 호환용 고정값
                "ovrs_pdno": it.get("symbol", ""),
                "prdt_name": it.get("name", it.get("symbol", "")),
                "ovrs_cblc_qty": it.get("quantity", "0"),
                "pchs_avg_pric": it.get("averagePurchasePrice", "0"),
                "now_pric2": it.get("lastPrice", "0"),
                "ovrs_stck_evlu_amt": market_value.get("amount", "0"),
                "evlu_pfls_amt": profit_loss.get("amount", "0"),
                "evlu_pfls_rt": round(rate * 100, 4) if rate is not None else "0",
            })
        market_value_usd = (holdings.get("marketValue") or {}).get("amount", {}).get("usd", 0)
        return {
            "positions": positions,
            "summary": {
                "tot_asst_amt": market_value_usd,
                "frcr_dncl_amt1": self.get_foreign_margin_usd(),
                "ord_psbl_cash": self.get_foreign_margin_usd(),
            },
        }

    def get_daily_orders(self) -> list[dict]:
        rows: list[dict] = []
        for status in ("OPEN", "CLOSED"):
            try:
                data = self.broker.get_orders(status=status, limit=50 if status == "CLOSED" else None)
            except Exception as e:
                logger.warning("토스 해외 주문내역 조회 실패(status=%s): %s", status, e)
                continue
            for o in data.get("orders", []):
                rows.append(_order_to_kis_row(o))
        return rows

    def get_foreign_margin_usd(self) -> float:
        try:
            data = self.broker.get_buying_power("USD")
            return float(data.get("cashBuyingPower") or 0)
        except Exception as e:
            logger.warning("USD 매수가능금액 조회 실패: %s", e)
            return 0.0

    @staticmethod
    def _to_float(value) -> float | None:
        try:
            return float(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _candles_to_df(candles: list[dict]) -> pd.DataFrame:
        if not candles:
            return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame(candles).rename(columns={
            "openPrice": "open", "highPrice": "high", "lowPrice": "low", "closePrice": "close",
        })
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df["datetime"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
        return df.sort_values("datetime").reset_index(drop=True)[
            ["datetime", "open", "high", "low", "close", "volume"]
        ]
