import logging
import pandas as pd
from datetime import date, timedelta, datetime, time as dtime
from pathlib import Path
import pickle

from .rest import KISRestClient
from .constants import (
    OverseasTRID, OverseasPath,
    ExchangeCode, OrderDivision, PeriodCode,
)

logger = logging.getLogger(__name__)

_DEFAULT_BALANCE_EXCHANGE = ExchangeCode.NASDAQ
_CACHE_DIR = Path("data/cache")

# 주간거래 시간대 (KST): 10:00~22:00
_DAYTIME_START = dtime(10, 0)
_DAYTIME_END   = dtime(22, 0)


def _is_daytime() -> bool:
    """현재 시각이 KIS 주간거래 시간대인지 확인."""
    now = datetime.now().time()
    return _DAYTIME_START <= now < _DAYTIME_END


class OverseasAPI:
    def __init__(self, client: KISRestClient, config: dict):
        self.client = client
        self.is_paper = config["mode"] in ("paper", "mock")
        kis = config["kis"]
        if self.is_paper:
            self.account_no   = kis.get("paper_account_no") or kis.get("account_no", "")
            self.acnt_prdt_cd = kis.get("paper_account_product_code") or kis.get("account_product_code", "01")
        else:
            self.account_no   = kis.get("live_account_no") or kis.get("account_no", "")
            self.acnt_prdt_cd = kis.get("live_account_product_code") or kis.get("account_product_code", "01")

    def _buy_tr_id(self) -> str:
        if self.is_paper:
            return OverseasTRID.BUY_PAPER
        return OverseasTRID.BUY_LIVE if not _is_daytime() else OverseasTRID.DAYTIME_BUY_LIVE

    def _sell_tr_id(self) -> str:
        if self.is_paper:
            return OverseasTRID.SELL_PAPER
        return OverseasTRID.SELL_LIVE if not _is_daytime() else OverseasTRID.DAYTIME_SELL_LIVE

    # ── 시세 조회 ──────────────────────────────────────────────────────

    def get_price(self, stock_code: str, exchange: ExchangeCode = ExchangeCode.NASDAQ) -> dict:
        data = self.client.get(
            OverseasPath.PRICE,
            OverseasTRID.PRICE,
            {"AUTH": "", "EXCD": exchange, "SYMB": stock_code},
        )
        return data.get("output", {})

    def get_daily_ohlcv(
        self,
        stock_code: str,
        exchange: ExchangeCode = ExchangeCode.NASDAQ,
        start_date: date | None = None,
        end_date: date | None = None,
        period: PeriodCode = PeriodCode.DAY,
    ) -> pd.DataFrame:
        if end_date is None:
            end_date = date.today()
        if start_date is None:
            start_date = end_date - timedelta(days=30)

        data = self.client.get(
            OverseasPath.DAILY_CHART,
            OverseasTRID.DAILY_CHART,
            {
                "AUTH": "",
                "EXCD": exchange,
                "SYMB": stock_code,
                "GUBN": "0" if period == PeriodCode.DAY else "1",
                "BYMD": end_date.strftime("%Y%m%d"),
                "MODP": "1",
            },
        )
        df = self._to_ohlcv_df(data.get("output2", []))
        df.attrs["name"] = stock_code
        if len(df) >= 2:
            last_close = float(df.iloc[-1]["close"])
            prev_close = float(df.iloc[-2]["close"])
            df.attrs["price"]      = last_close
            df.attrs["change_pct"] = round((last_close - prev_close) / prev_close * 100, 2) \
                                     if prev_close else 0.0
        elif len(df) == 1:
            df.attrs["price"]      = float(df.iloc[-1]["close"])
            df.attrs["change_pct"] = 0.0
        else:
            df.attrs["price"]      = 0.0
            df.attrs["change_pct"] = 0.0
        return df

    def get_historical_minute_ohlcv(
        self,
        stock_code: str,
        exchange: ExchangeCode = ExchangeCode.NASDAQ,
        lookback_days: int = 2,
        candle_minutes: int = 1,
    ) -> pd.DataFrame:
        """해외주식 분봉 데이터 (캐시 사용)."""
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file = _CACHE_DIR / f"{stock_code}_{exchange}_1min.pkl"

        cached_df = self._load_cache(cache_file)
        if cached_df is not None and not cached_df.empty:
            cutoff = pd.Timestamp(date.today() - timedelta(days=lookback_days))
            if cached_df["datetime"].min() <= cutoff:
                result = cached_df[cached_df["datetime"] >= cutoff]
                return self._aggregate(result, candle_minutes)

        logger.info("[%s] 해외 분봉 수집 시작 (캐시 없음)...", stock_code)
        try:
            df = self._fetch_minute_data(stock_code, exchange, lookback_days)
        except Exception as e:
            logger.warning("[%s] 해외 분봉 수집 실패: %s", stock_code, e)
            return pd.DataFrame()
        if not df.empty:
            self._save_cache(cache_file, df)
        return self._aggregate(df, candle_minutes)

    def _fetch_minute_data(
        self,
        stock_code: str,
        exchange: ExchangeCode,
        lookback_days: int,
    ) -> pd.DataFrame:
        all_records: list[dict] = []
        end_dt = datetime.now()
        cutoff = date.today() - timedelta(days=lookback_days)
        max_pages = 200

        for _ in range(max_pages):
            data = self.client.get(
                OverseasPath.MINUTE_CHART,
                OverseasTRID.MINUTE_CHART,
                {
                    "AUTH": "",
                    "EXCD": exchange,
                    "SYMB": stock_code,
                    "NMIN": "1",
                    "PINC": "1",
                    "NEXT": "",
                    "NREC": "120",
                    "FILL": "",
                    "KEYB": end_dt.strftime("%Y%m%d%H%M%S"),
                },
            )
            rows = data.get("output2", [])
            if not rows:
                break
            all_records.extend(rows)
            last = rows[-1]
            last_dt = pd.to_datetime(last.get("kymd", "") + last.get("khms", ""),
                                     format="%Y%m%d%H%M%S", errors="coerce")
            if pd.isna(last_dt) or last_dt.date() < cutoff:
                break
            end_dt = last_dt

        if not all_records:
            return pd.DataFrame()

        df = pd.DataFrame(all_records)
        df["datetime"] = pd.to_datetime(
            df["kymd"].astype(str) + df["khms"].astype(str),
            format="%Y%m%d%H%M%S", errors="coerce",
        )
        df = df.dropna(subset=["datetime"])
        for col, src in [("open","open"),("high","high"),("low","low"),("close","last"),("volume","evol")]:
            df[col] = pd.to_numeric(df.get(src, 0), errors="coerce").fillna(0)
        cutoff_ts = pd.Timestamp(cutoff)
        df = df[df["datetime"] >= cutoff_ts]
        return df[["datetime","open","high","low","close","volume"]].sort_values("datetime").reset_index(drop=True)

    def _aggregate(self, df: pd.DataFrame, candle_minutes: int) -> pd.DataFrame:
        if df.empty or candle_minutes == 1:
            return df
        df = df.copy()
        df["slot"] = df["datetime"].dt.floor(f"{candle_minutes}min")
        agg = df.groupby("slot").agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        ).reset_index().rename(columns={"slot": "datetime"})
        return agg.sort_values("datetime").reset_index(drop=True)

    @staticmethod
    def _load_cache(path: Path) -> pd.DataFrame | None:
        if not path.exists():
            return None
        try:
            with path.open("rb") as f:
                return pickle.load(f)
        except Exception:
            return None

    @staticmethod
    def _save_cache(path: Path, df: pd.DataFrame):
        with path.open("wb") as f:
            pickle.dump(df, f)

    def get_volume_ranking(self, exchange: ExchangeCode = ExchangeCode.NASDAQ) -> list[dict]:
        data = self.client.get(
            OverseasPath.VOLUME_RANK,
            OverseasTRID.VOLUME_RANK,
            {
                "AUTH":     "",
                "EXCD":     exchange,
                "KEYB":     "",
                "NDAY":     "0",
                "PRC1":     "",
                "PRC2":     "",
                "VOL_RANG": "0",
            },
        )
        return data.get("output", [])

    # ── 주문 ────────────────────────────────────────────────────────────

    def buy(
        self,
        stock_code: str,
        exchange: ExchangeCode,
        qty: int,
        price: float,
        order_type: OrderDivision = OrderDivision.MARKET,
    ) -> dict:
        order_price = 0 if order_type == OrderDivision.MARKET else price
        tr_id = self._buy_tr_id()
        body = {
            "CANO":            self.account_no,
            "ACNT_PRDT_CD":    self.acnt_prdt_cd,
            "OVRS_EXCG_CD":    exchange,
            "PDNO":            stock_code,
            "ORD_DVSN":        order_type,
            "ORD_QTY":         str(qty),
            "OVRS_ORD_UNPR":   str(order_price),
            "ORD_SVR_DVSN_CD": "0",
        }
        data = self.client.post(OverseasPath.ORDER, tr_id, body)
        logger.info("해외 매수: %s(%s) %d주 @ %.2f [TR:%s]", stock_code, exchange, qty, price, tr_id)
        return data["output"]

    def sell(
        self,
        stock_code: str,
        exchange: ExchangeCode,
        qty: int,
        price: float,
        order_type: OrderDivision = OrderDivision.MARKET,
    ) -> dict:
        order_price = 0 if order_type == OrderDivision.MARKET else price
        tr_id = self._sell_tr_id()
        body = {
            "CANO":            self.account_no,
            "ACNT_PRDT_CD":    self.acnt_prdt_cd,
            "OVRS_EXCG_CD":    exchange,
            "PDNO":            stock_code,
            "ORD_DVSN":        order_type,
            "ORD_QTY":         str(qty),
            "OVRS_ORD_UNPR":   str(order_price),
            "ORD_SVR_DVSN_CD": "0",
            "SLL_TYPE":        "00",
        }
        data = self.client.post(OverseasPath.ORDER, tr_id, body)
        logger.info("해외 매도: %s(%s) %d주 @ %.2f [TR:%s]", stock_code, exchange, qty, price, tr_id)
        return data["output"]

    # ── 계좌 조회 ────────────────────────────────────────────────────────

    def get_balance(self, exchange: ExchangeCode = _DEFAULT_BALANCE_EXCHANGE) -> dict:
        tr_id = OverseasTRID.BALANCE_PAPER if self.is_paper else OverseasTRID.BALANCE_LIVE
        data = self.client.get(
            OverseasPath.BALANCE,
            tr_id,
            {
                "CANO":          self.account_no,
                "ACNT_PRDT_CD":  self.acnt_prdt_cd,
                "OVRS_EXCG_CD":  exchange,
                "TR_CRCY_CD":    "USD",
                "CTX_AREA_FK200":"",
                "CTX_AREA_NK200":"",
            },
        )
        return {
            "positions": data.get("output1", []),
            "summary":   data.get("output2", [{}])[0] if data.get("output2") else {},
        }

    # ── 내부 ────────────────────────────────────────────────────────────

    @staticmethod
    def _to_ohlcv_df(rows: list[dict]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows).rename(columns={
            "xymd": "date",
            "open": "open",
            "high": "high",
            "low":  "low",
            "clos": "close",
            "tvol": "volume",
            "tamt": "trading_value",
        })
        for col in ["open", "high", "low", "close", "volume", "trading_value"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
        return df.sort_values("date").reset_index(drop=True)[
            ["date", "open", "high", "low", "close", "volume", "trading_value"]
        ]
