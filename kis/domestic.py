import logging
import pickle
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from .rest import KISRestClient
from .constants import (
    DomesticTRID, DomesticPath,
    MarketCode, OrderDivision, PeriodCode,
)

_CACHE_DIR = Path("data/cache")

logger = logging.getLogger(__name__)


class DomesticAPI:
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

    # ── 시세 조회 ──────────────────────────────────────────────────────

    def get_price(self, stock_code: str, market: MarketCode = MarketCode.KRX) -> dict:
        data = self.client.get(
            DomesticPath.PRICE,
            DomesticTRID.PRICE,
            {"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": stock_code},
        )
        return data["output"]

    def get_daily_ohlcv(
        self,
        stock_code: str,
        start_date: date | None = None,
        end_date: date | None = None,
        period: PeriodCode = PeriodCode.DAY,
        market: MarketCode = MarketCode.KRX,
    ) -> pd.DataFrame:
        """일/주/월봉 조회. 한 번에 최대 100건."""
        if end_date is None:
            end_date = date.today()
        if start_date is None:
            start_date = end_date - timedelta(days=100)

        data = self.client.get(
            DomesticPath.DAILY_CHART,
            DomesticTRID.DAILY_CHART,
            {
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_INPUT_ISCD":         stock_code,
                "FID_INPUT_DATE_1":        start_date.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2":        end_date.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE":     period,
                "FID_ORG_ADJ_PRC":        "0",
            },
        )
        df = self._to_ohlcv_df(data.get("output2", []))
        # output1에서 종목명·현재가·등락률 추출해서 df에 메타 속성으로 부착
        info = data.get("output1", {})
        df.attrs["name"]       = info.get("hts_kor_isnm", stock_code)
        df.attrs["price"]      = float(info.get("stck_prpr", 0) or 0)
        df.attrs["change_pct"] = float(info.get("prdy_ctrt",  0) or 0)
        return df

    def get_minute_ohlcv(
        self,
        stock_code: str,
        input_hour: str = "153000",
        market: MarketCode = MarketCode.KRX,
    ) -> pd.DataFrame:
        """당일 분봉 조회. 한 번에 최대 30건. input_hour: HHMMSS."""
        data = self.client.get(
            DomesticPath.MINUTE_CHART,
            DomesticTRID.MINUTE_CHART,
            {
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_INPUT_ISCD":         stock_code,
                "FID_INPUT_HOUR_1":        input_hour,
                "FID_PW_DATA_INCU_YN":    "Y",
                "FID_ETC_CLS_CODE":       "",
            },
        )
        rows = data.get("output2", [])
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows).rename(columns={
            "stck_bsop_date": "date",
            "stck_cntg_hour": "time",
            "stck_oprc":      "open",
            "stck_hgpr":      "high",
            "stck_lwpr":      "low",
            "stck_prpr":      "close",
            "cntg_vol":       "volume",
        })
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df["datetime"] = pd.to_datetime(df["date"] + df["time"], format="%Y%m%d%H%M%S")
        return df.sort_values("datetime").reset_index(drop=True)[
            ["datetime", "open", "high", "low", "close", "volume"]
        ]

    def get_historical_minute_ohlcv(
        self,
        stock_code: str,
        lookback_days: int = 30,
        candle_minutes: int = 15,
        market: MarketCode = MarketCode.KRX,
    ) -> pd.DataFrame:
        """
        과거 분봉 데이터 수집 후 N분봉으로 집계.
        캐시(data/cache/)를 사용해 당일 재호출 시 빠르게 반환.
        """
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file = _CACHE_DIR / f"{stock_code}_1min.pkl"

        # ── 캐시 로드 ──────────────────────────────────────────────
        cached_df = self._load_cache(cache_file)
        if cached_df is not None and not cached_df.empty:
            cutoff = date.today() - timedelta(days=lookback_days)
            if cached_df["datetime"].dt.date.min() <= cutoff:
                # 오늘치만 업데이트
                last_dt = cached_df["datetime"].max()
                try:
                    new_df = self._fetch_minute_range(
                        stock_code, market,
                        from_date=last_dt.date(),
                        from_hour=last_dt.strftime("%H%M%S"),
                    )
                except Exception as e:
                    logger.warning("[%s] 분봉 캐시 업데이트 실패 → 기존 캐시 사용: %s", stock_code, e)
                    new_df = pd.DataFrame()
                if not new_df.empty:
                    combined = pd.concat([cached_df, new_df]).drop_duplicates("datetime")
                    combined = combined.sort_values("datetime").reset_index(drop=True)
                    self._save_cache(cache_file, combined)
                    cached_df = combined

                cutoff_dt = pd.Timestamp(date.today() - timedelta(days=lookback_days))
                result = cached_df[cached_df["datetime"] >= cutoff_dt]
                return self._aggregate(result, candle_minutes)

        # ── 전체 수집 ──────────────────────────────────────────────
        logger.info("[%s] 과거 %d일 분봉 수집 시작 (캐시 없음)...", stock_code, lookback_days)
        end_date = date.today()
        start_date = end_date - timedelta(days=lookback_days)
        try:
            df = self._fetch_minute_range(stock_code, market, start_date=start_date)
        except Exception as e:
            logger.warning("[%s] 분봉 수집 실패 → 일봉 fallback 사용: %s", stock_code, e)
            return pd.DataFrame()
        if not df.empty:
            self._save_cache(cache_file, df)
        return self._aggregate(df, candle_minutes)

    def _fetch_minute_range(
        self,
        stock_code: str,
        market: MarketCode,
        start_date: date | None = None,
        from_date: date | None = None,
        from_hour: str = "160000",
    ) -> pd.DataFrame:
        """커서 방식으로 분봉 데이터를 과거 방향으로 수집."""
        all_records: list[dict] = []
        cursor_date = (from_date or date.today()).strftime("%Y%m%d")
        cursor_hour = from_hour
        cutoff = (start_date or date.today() - timedelta(days=30)).strftime("%Y%m%d")
        seen_cursors: set[tuple[str, str]] = set()
        max_pages = 600

        for page in range(max_pages):
            cursor_key = (cursor_date, cursor_hour)
            if cursor_key in seen_cursors:
                logger.warning(
                    "[%s] 분봉 커서 반복 감지 → 수집 중단 (%s %s, %d건)",
                    stock_code, cursor_date, cursor_hour, len(all_records),
                )
                break
            seen_cursors.add(cursor_key)

            data = self.client.get(
                DomesticPath.HIST_MINUTE,
                DomesticTRID.HIST_MINUTE,
                {
                    "FID_COND_MRKT_DIV_CODE": market,
                    "FID_INPUT_ISCD":         stock_code,
                    "FID_INPUT_DATE_1":        cursor_date,
                    "FID_INPUT_HOUR_1":        cursor_hour,
                    "FID_PW_DATA_INCU_YN":    "Y",
                    "FID_FAKE_TICK_INCU_YN":  " ",
                },
            )
            rows = data.get("output2", [])
            if not rows:
                break

            all_records.extend(rows)

            # 가장 오래된 레코드의 날짜/시각이 커서
            oldest = rows[-1]
            oldest_date = oldest.get("stck_bsop_date", "")
            oldest_time = oldest.get("stck_cntg_hour", "000000")

            if oldest_date <= cutoff:
                break

            try:
                next_cursor = datetime.strptime(oldest_date + oldest_time, "%Y%m%d%H%M%S")
                next_cursor -= timedelta(seconds=1)
                cursor_date = next_cursor.strftime("%Y%m%d")
                cursor_hour = next_cursor.strftime("%H%M%S")
            except ValueError:
                logger.warning("[%s] 분봉 커서 파싱 실패 → 수집 중단: %s %s",
                               stock_code, oldest_date, oldest_time)
                break
        else:
            logger.warning("[%s] 분봉 수집 page limit 도달 (%d pages, %d건)",
                           stock_code, max_pages, len(all_records))

        if not all_records:
            return pd.DataFrame()

        df = pd.DataFrame(all_records).rename(columns={
            "stck_bsop_date": "_date",
            "stck_cntg_hour": "_time",
            "stck_oprc":      "open",
            "stck_hgpr":      "high",
            "stck_lwpr":      "low",
            "stck_prpr":      "close",
            "cntg_vol":       "volume",
        })
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df["datetime"] = pd.to_datetime(df["_date"] + df["_time"], format="%Y%m%d%H%M%S")
        df = df[df["open"] > 0]   # 허봉 제거
        return df.sort_values("datetime").drop_duplicates("datetime").reset_index(drop=True)[
            ["datetime", "open", "high", "low", "close", "volume"]
        ]

    @staticmethod
    def _aggregate(df: pd.DataFrame, candle_minutes: int) -> pd.DataFrame:
        """1분봉 → N분봉 집계."""
        if df.empty:
            return df
        df = df.copy()
        df["bucket"] = df["datetime"].dt.floor(f"{candle_minutes}min")
        result = df.groupby("bucket").agg(
            open=("open",   "first"),
            high=("high",   "max"),
            low=("low",     "min"),
            close=("close", "last"),
            volume=("volume","sum"),
        ).reset_index().rename(columns={"bucket": "datetime"})
        return result.sort_values("datetime").reset_index(drop=True)

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

    def get_volume_ranking(self, market_code: str | None = None) -> list[dict]:
        """
        거래량 순위 조회.
        KIS API 1회 호출 최대 30건 제한 → KOSPI("0001") + KOSDAQ("1001") 분리 호출 후 합산.
        market_code 명시 시 해당 시장만 조회.
        """
        markets = [market_code] if market_code else ["0001", "1001"]
        results: list[dict] = []
        seen: set[str] = set()

        for mkt in markets:
            data = self.client.get(
                DomesticPath.VOLUME_RANK,
                DomesticTRID.VOLUME_RANK,
                {
                    "FID_COND_MRKT_DIV_CODE":  MarketCode.KRX,
                    "FID_COND_SCR_DIV_CODE":   "20171",
                    "FID_INPUT_ISCD":          mkt,
                    "FID_DIV_CLS_CODE":        "0",
                    "FID_BLNG_CLS_CODE":       "0",
                    "FID_TRGT_CLS_CODE":       "111111111",
                    "FID_TRGT_EXLS_CLS_CODE":  "000000",
                    "FID_INPUT_PRICE_1":       "",
                    "FID_INPUT_PRICE_2":       "",
                    "FID_VOL_CNT":             "",
                    "FID_INPUT_DATE_1":        "",
                },
            )
            for item in data.get("output", []):
                code = item.get("mksc_shrn_iscd", "")
                if code and code not in seen:
                    results.append(item)
                    seen.add(code)
            logger.debug("거래량순위 [%s] %d건", mkt, len(data.get("output", [])))

        logger.info("거래량순위 수집 완료: KOSPI+KOSDAQ 합산 %d건", len(results))
        return results

    # ── 주문 ────────────────────────────────────────────────────────────

    def buy(
        self,
        stock_code: str,
        qty: int,
        price: int = 0,
        order_type: OrderDivision = OrderDivision.MARKET,
    ) -> dict:
        tr_id = DomesticTRID.BUY_PAPER if self.is_paper else DomesticTRID.BUY_LIVE
        body = {
            "CANO":         self.account_no,
            "ACNT_PRDT_CD": self.acnt_prdt_cd,
            "PDNO":         stock_code,
            "ORD_DVSN":     order_type,
            "ORD_QTY":      str(qty),
            "ORD_UNPR":     str(price),
        }
        data = self.client.post(DomesticPath.ORDER, tr_id, body)
        logger.info("매수: %s %d주 @ %d (주문번호: %s)",
                    stock_code, qty, price, data["output"].get("ODNO"))
        return data["output"]

    def sell(
        self,
        stock_code: str,
        qty: int,
        price: int = 0,
        order_type: OrderDivision = OrderDivision.MARKET,
    ) -> dict:
        tr_id = DomesticTRID.SELL_PAPER if self.is_paper else DomesticTRID.SELL_LIVE
        body = {
            "CANO":         self.account_no,
            "ACNT_PRDT_CD": self.acnt_prdt_cd,
            "PDNO":         stock_code,
            "ORD_DVSN":     order_type,
            "ORD_QTY":      str(qty),
            "ORD_UNPR":     str(price),
        }
        data = self.client.post(DomesticPath.ORDER, tr_id, body)
        logger.info("매도: %s %d주 @ %d (주문번호: %s)",
                    stock_code, qty, price, data["output"].get("ODNO"))
        return data["output"]

    def cancel_order(
        self,
        org_no: str,
        order_no: str,
        qty: int,
        price: int,
        order_type: OrderDivision,
    ) -> dict:
        tr_id = DomesticTRID.CANCEL_PAPER if self.is_paper else DomesticTRID.CANCEL_LIVE
        body = {
            "CANO":              self.account_no,
            "ACNT_PRDT_CD":      self.acnt_prdt_cd,
            "KRX_FWDG_ORD_ORGNO": org_no,
            "ORGN_ODNO":          order_no,
            "ORD_DVSN":           order_type,
            "RVSE_CNCL_DVSN_CD":  "02",
            "ORD_QTY":            str(qty),
            "ORD_UNPR":           str(price),
            "QTY_ALL_ORD_YN":     "Y",
        }
        return self.client.post(DomesticPath.CANCEL, tr_id, body)["output"]

    # ── 계좌 조회 ────────────────────────────────────────────────────────

    def get_balance(self) -> dict:
        tr_id = DomesticTRID.BALANCE_PAPER if self.is_paper else DomesticTRID.BALANCE_LIVE
        data = self.client.get(
            DomesticPath.BALANCE,
            tr_id,
            {
                "CANO":                 self.account_no,
                "ACNT_PRDT_CD":         self.acnt_prdt_cd,
                "AFHR_FLPR_YN":         "N",
                "OFL_YN":               "",
                "INQR_DVSN":            "02",
                "UNPR_DVSN":            "01",
                "FUND_STTL_ICLD_YN":    "N",
                "FNCG_AMT_AUTO_RDPT_YN":"N",
                "PRCS_DVSN":            "01",
                "CTX_AREA_FK100":       "",
                "CTX_AREA_NK100":       "",
            },
        )
        return {
            "positions": data.get("output1", []),
            "summary":   data.get("output2", [{}])[0] if data.get("output2") else {},
        }

    def get_daily_orders(self) -> list[dict]:
        tr_id = DomesticTRID.DAILY_ORDERS_PAPER if self.is_paper else DomesticTRID.DAILY_ORDERS_LIVE
        today = __import__("datetime").date.today().strftime("%Y%m%d")
        data = self.client.get(
            DomesticPath.DAILY_ORDERS,
            tr_id,
            {
                "CANO":          self.account_no,
                "ACNT_PRDT_CD":  self.acnt_prdt_cd,
                "INQR_STRT_DT":  today,
                "INQR_END_DT":   today,
                "SLL_BUY_DVSN_CD": "00",
                "INQR_DVSN":     "00",
                "PDNO":          "",
                "CCLD_DVSN":     "00",
                "ORD_GNO_BRNO":  "",
                "ODNO":          "",
                "INQR_DVSN_3":   "00",
                "INQR_DVSN_1":   "",
                "CTX_AREA_FK100":"",
                "CTX_AREA_NK100":"",
            },
        )
        return data.get("output1", [])

    # ── 내부 ────────────────────────────────────────────────────────────

    @staticmethod
    def _to_ohlcv_df(rows: list[dict]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows).rename(columns={
            "stck_bsop_date": "date",
            "stck_oprc":      "open",
            "stck_hgpr":      "high",
            "stck_lwpr":      "low",
            "stck_clpr":      "close",
            "acml_vol":       "volume",
            "acml_tr_pbmn":   "trading_value",
        })
        for col in ["open", "high", "low", "close", "volume", "trading_value"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
        return df.sort_values("date").reset_index(drop=True)[
            ["date", "open", "high", "low", "close", "volume", "trading_value"]
        ]
