"""
토스증권 국내주식 facade — kis/domestic.py::DomesticAPI 와 동일한 메서드명/시그니처/
반환 형태를 제공해서 order_manager.py / ai/tools.py / routers/* 호출부를 그대로 재사용한다.

시장 구분(KRX/NXT/UN)은 토스에선 별도 파라미터가 없다 — market 인자는 시그니처
호환을 위해 받되 내부적으로 무시한다 (심볼 포맷만으로 KR 시장 라우팅됨).
"""
import logging
import pickle
from datetime import date, datetime, timedelta

import pandas as pd
import redis as redispy

from .api import TossBrokerAPI

_CANDLE_CACHE_PREFIX = "toss:candles:1min:"
_CANDLE_CACHE_TTL = 86400  # 1일

logger = logging.getLogger(__name__)


class TossDomesticAPI:
    def __init__(self, broker: TossBrokerAPI, config: dict, redis_client: redispy.Redis | None = None):
        self.broker = broker
        self.config = config
        self._r = redis_client

    # ── 시세 조회 ──────────────────────────────────────────────────────

    def get_price(self, stock_code: str, market=None) -> dict:
        prices = self.broker.get_prices([stock_code])
        p = prices[0] if prices else {}
        current = self._to_float(p.get("lastPrice"))
        return {
            "stck_prpr": current,
            "prpr": current,
            "current_price": current,
            "output": p,
        }

    def get_overtime_price(self, stock_code: str, market=None) -> dict:
        # 토스는 시간외단일가를 별도 엔드포인트로 분리하지 않음 — 현재가로 대체.
        return self.get_price(stock_code)

    def get_daily_ohlcv(
        self,
        stock_code: str,
        start_date: date | None = None,
        end_date: date | None = None,
        period=None,
        market=None,
    ) -> pd.DataFrame:
        if end_date is None:
            end_date = date.today()
        if start_date is None:
            start_date = end_date - timedelta(days=100)
        count = min(200, max(1, (end_date - start_date).days + 5))

        result = self.broker.get_candles(stock_code, interval="1d", count=count)
        df = self._candles_to_df(result.get("candles", []))
        if not df.empty:
            df = df[(df["datetime"].dt.date >= start_date) & (df["datetime"].dt.date <= end_date)]
            df = df.reset_index(drop=True)

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

    def get_minute_ohlcv(
        self,
        stock_code: str,
        input_hour: str = "153000",
        market=None,
        timeout: int = 4,
    ) -> pd.DataFrame:
        """당일 분봉 조회. input_hour(HHMMSS) 이전 최근 ~30건."""
        result = self.broker.get_candles(stock_code, interval="1m", count=200)
        df = self._candles_to_df(result.get("candles", []))
        if df.empty:
            return df

        today = date.today()
        df = df[df["datetime"].dt.date == today]
        try:
            cutoff = datetime.strptime(f"{today:%Y%m%d}{input_hour}", "%Y%m%d%H%M%S")
            df = df[df["datetime"] <= cutoff]
        except ValueError:
            pass
        return df.sort_values("datetime").tail(30).reset_index(drop=True)[
            ["datetime", "open", "high", "low", "close", "volume"]
        ]

    def get_historical_minute_ohlcv(
        self,
        stock_code: str,
        lookback_days: int = 30,
        candle_minutes: int = 15,
        market=None,
    ) -> pd.DataFrame:
        cached_df = self._load_candle_cache(stock_code)
        if cached_df is not None and not cached_df.empty:
            cutoff = date.today() - timedelta(days=lookback_days)
            if cached_df["datetime"].dt.date.min() <= cutoff:
                try:
                    new_df = self._fetch_minute_range(stock_code, since=cached_df["datetime"].max())
                except Exception as e:
                    logger.warning("[%s] 토스 분봉 캐시 업데이트 실패 → 기존 캐시 사용: %s", stock_code, e)
                    new_df = pd.DataFrame()
                if not new_df.empty:
                    combined = pd.concat([cached_df, new_df]).drop_duplicates("datetime")
                    combined = combined.sort_values("datetime").reset_index(drop=True)
                    self._save_candle_cache(stock_code, combined)
                    cached_df = combined

                cutoff_dt = pd.Timestamp(date.today() - timedelta(days=lookback_days))
                result = cached_df[cached_df["datetime"] >= cutoff_dt]
                return self._aggregate(result, candle_minutes)

        logger.info("[%s] 토스 과거 %d일 분봉 수집 시작 (캐시 없음)...", stock_code, lookback_days)
        try:
            df = self._fetch_minute_range(stock_code, lookback_days=lookback_days)
        except Exception as e:
            logger.warning("[%s] 토스 분봉 수집 실패: %s", stock_code, e)
            return pd.DataFrame()
        if not df.empty:
            self._save_candle_cache(stock_code, df)
        return self._aggregate(df, candle_minutes)

    def _fetch_minute_range(
        self,
        stock_code: str,
        lookback_days: int = 30,
        since: "pd.Timestamp | None" = None,
    ) -> pd.DataFrame:
        """1분봉을 `before` 커서로 과거 방향 페이지네이션하며 수집."""
        cutoff = since.to_pydatetime() if since is not None else (
            datetime.now() - timedelta(days=lookback_days)
        )
        all_rows: list[dict] = []
        before: str | None = None
        max_pages = 100

        for _ in range(max_pages):
            result = self.broker.get_candles(stock_code, interval="1m", count=200, before=before)
            candles = result.get("candles", [])
            if not candles:
                break
            all_rows.extend(candles)

            oldest_ts = candles[-1].get("timestamp")
            if not oldest_ts:
                break
            oldest_dt = pd.to_datetime(oldest_ts)
            if oldest_dt.to_pydatetime().replace(tzinfo=None) <= cutoff:
                break
            before = oldest_ts
            next_cursor = result.get("nextBefore")
            if not next_cursor:
                break

        df = self._candles_to_df(all_rows)
        if df.empty:
            return df
        cutoff_ts = pd.Timestamp(cutoff)
        return df[df["datetime"] >= cutoff_ts].sort_values("datetime").drop_duplicates("datetime").reset_index(drop=True)

    @staticmethod
    def _aggregate(df: pd.DataFrame, candle_minutes: int) -> pd.DataFrame:
        if df.empty:
            return df
        df = df.copy()
        df["bucket"] = df["datetime"].dt.floor(f"{candle_minutes}min")
        result = df.groupby("bucket").agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        ).reset_index().rename(columns={"bucket": "datetime"})
        return result.sort_values("datetime").reset_index(drop=True)

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
            logger.warning("[%s] 토스 분봉 캐시 저장 실패: %s", stock_code, e)

    def get_volume_ranking(self, market_code: str | None = None) -> list[dict]:
        data = self.broker.get_rankings(
            ranking_type="MARKET_TRADING_AMOUNT", market_country="KR", duration="realtime", count=100,
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
                logger.warning("종목명 조회 실패(순위): %s", e)

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

    def buy(self, stock_code: str, qty: int, price: int = 0, order_type=None) -> dict:
        is_market = price <= 0
        result = self.broker.create_order(
            symbol=stock_code, side="BUY",
            order_type="MARKET" if is_market else "LIMIT",
            quantity=str(int(qty)),
            price=None if is_market else str(int(price)),
        )
        logger.info("토스 매수: %s %d주 @ %s (주문번호: %s)",
                    stock_code, qty, price, result.get("orderId"))
        return {"ODNO": result.get("orderId", "")}

    def sell(self, stock_code: str, qty: int, price: int = 0, order_type=None) -> dict:
        is_market = price <= 0
        result = self.broker.create_order(
            symbol=stock_code, side="SELL",
            order_type="MARKET" if is_market else "LIMIT",
            quantity=str(int(qty)),
            price=None if is_market else str(int(price)),
        )
        logger.info("토스 매도: %s %d주 @ %s (주문번호: %s)",
                    stock_code, qty, price, result.get("orderId"))
        return {"ODNO": result.get("orderId", "")}

    def cancel_order(self, org_no: str, order_no: str, qty: int, price: int, order_type=None) -> dict:
        return self.broker.cancel_order(order_no)

    # ── 계좌 조회 ────────────────────────────────────────────────────────

    def get_balance(self) -> dict:
        holdings = self.broker.get_holdings()
        buying_power = self.broker.get_buying_power("KRW")
        items = holdings.get("items", [])
        positions = []
        for it in items:
            if it.get("marketCountry") != "KR":
                continue
            market_value = it.get("marketValue") or {}
            profit_loss = it.get("profitLoss") or {}
            rate = self._to_float(profit_loss.get("rate"))
            positions.append({
                "pdno": it.get("symbol", ""),
                "prdt_name": it.get("name", it.get("symbol", "")),
                "hldg_qty": it.get("quantity", "0"),
                "pchs_avg_pric": it.get("averagePurchasePrice", "0"),
                "prpr": it.get("lastPrice", "0"),
                "evlu_amt": market_value.get("amount", "0"),
                "evlu_pfls_amt": profit_loss.get("amount", "0"),
                "evlu_pfls_rt": round(rate * 100, 4) if rate is not None else "0",
            })
        return {
            "positions": positions,
            "summary": {
                "dnca_tot_amt": buying_power.get("cashBuyingPower", 0),
                "tot_evlu_amt": (holdings.get("marketValue") or {}).get("amount", {}).get("krw", 0),
                "evlu_amt_smtl_amt": (holdings.get("marketValue") or {}).get("amount", {}).get("krw", 0),
            },
        }

    def get_daily_orders(self) -> list[dict]:
        rows: list[dict] = []
        for status in ("OPEN", "CLOSED"):
            try:
                data = self.broker.get_orders(status=status, limit=50 if status == "CLOSED" else None)
            except Exception as e:
                logger.warning("토스 주문내역 조회 실패(status=%s): %s", status, e)
                continue
            for o in data.get("orders", []):
                rows.append(_order_to_kis_row(o))
        return rows

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


def _order_to_kis_row(o: dict) -> dict:
    """토스 /orders 응답 한 건을 trading/order_manager.py::reconcile_order_rows()가
    기대하는 KIS 필드명으로 매핑."""
    execution = o.get("execution") or {}
    status = o.get("status", "")
    row = {
        "odno": o.get("orderId", ""),
        "ft_ccld_qty": execution.get("filledQuantity", "0"),
        "ft_ccld_unpr3": execution.get("averageFilledPrice") or "0",
        "avg_prvs": execution.get("averageFilledPrice") or "0",
    }
    if status == "REJECTED":
        row["rjct_rson"] = "REJECTED"
    if status == "FILLED":
        row["ord_stat_name"] = "완료"
    return row
