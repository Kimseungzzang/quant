import logging
import pandas as pd
from datetime import date, timedelta

from .rest import KISRestClient
from .constants import (
    OverseasTRID, OverseasPath,
    ExchangeCode, OrderDivision, PeriodCode,
)

logger = logging.getLogger(__name__)

_DEFAULT_BALANCE_EXCHANGE = ExchangeCode.NASDAQ


class OverseasAPI:
    def __init__(self, client: KISRestClient, config: dict):
        self.client = client
        self.is_paper = config["mode"] in ("paper", "mock")
        self.account_no   = config["kis"]["account_no"]
        self.acnt_prdt_cd = config["kis"]["account_product_code"]

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
            start_date = end_date - timedelta(days=100)

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
        # 종목명은 code 사용, 가격·등락은 마지막 두 행으로 계산
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

    def get_volume_ranking(self, exchange: ExchangeCode = ExchangeCode.NASDAQ) -> list[dict]:
        data = self.client.get(
            OverseasPath.VOLUME_RANK,
            OverseasTRID.VOLUME_RANK,
            {
                "AUTH":     "",
                "EXCD":     exchange,
                "KEYB":     "",
                "NDAY":     "0",    # 당일
                "PRC1":     "",
                "PRC2":     "",
                "VOL_RANG": "0",    # 전체
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
        tr_id = OverseasTRID.BUY_PAPER if self.is_paper else OverseasTRID.BUY_LIVE
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
        logger.info("해외 매수: %s(%s) %d주 @ %.2f", stock_code, exchange, qty, price)
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
        tr_id = OverseasTRID.SELL_PAPER if self.is_paper else OverseasTRID.SELL_LIVE
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
        logger.info("해외 매도: %s(%s) %d주 @ %.2f", stock_code, exchange, qty, price)
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
