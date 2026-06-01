"""
KIS API 관련 상수 정의.
- URL 계열: 환경변수에서 로드 (os.getenv, .env.example 참고)
- 코드/ID 계열: KIS API 스펙에 고정된 값 → StrEnum
"""
import os
from enum import StrEnum

# ── 환경변수 로드 (URL, Rate Limit) ──────────────────────────────────

KIS_REST_URL_LIVE  = os.getenv("KIS_REST_URL_LIVE",  "https://openapi.koreainvestment.com:9443")
KIS_REST_URL_PAPER = os.getenv("KIS_REST_URL_PAPER", "https://openapivts.koreainvestment.com:29443")
KIS_REST_URL_MOCK  = os.getenv("KIS_REST_URL_MOCK",  "http://localhost:8000")

KIS_WS_BASE_LIVE  = os.getenv("KIS_WS_BASE_LIVE",  "ws://ops.koreainvestment.com:21000")
KIS_WS_BASE_PAPER = os.getenv("KIS_WS_BASE_PAPER", "ws://ops.koreainvestment.com:31000")
KIS_WS_PATH       = os.getenv("KIS_WS_PATH",       "/tryitout/H0STCNT0")
KIS_WS_URL_MOCK   = os.getenv("KIS_WS_URL_MOCK",   "ws://localhost:8000/ws")

KIS_RATE_LIMIT_SEC = float(os.getenv("KIS_RATE_LIMIT_SEC", "0.06"))


# ── 거래 모드 ─────────────────────────────────────────────────────────

class TradingMode(StrEnum):
    LIVE  = "live"
    PAPER = "paper"
    MOCK  = "mock"


# ── 국내주식 시장 코드 ────────────────────────────────────────────────

class MarketCode(StrEnum):
    KRX   = "J"   # 한국거래소
    NXT   = "NX"  # 넥스트레이드(대체거래소)
    ALL   = "UN"  # 통합(KRX+NXT)
    INDEX = "U"   # 업종/지수 (KOSPI=0001, KOSDAQ=1001)


# ── 해외주식 거래소 코드 ──────────────────────────────────────────────

class ExchangeCode(StrEnum):
    NASDAQ    = "NAS"   # 나스닥
    NYSE      = "NYS"   # 뉴욕
    AMEX      = "AMS"   # 아멕스
    TOKYO     = "TSE"   # 도쿄
    SHANGHAI  = "SHS"   # 상해
    HONG_KONG = "HKS"   # 홍콩


# ── 주문 구분 코드 ────────────────────────────────────────────────────

class OrderDivision(StrEnum):
    LIMIT          = "00"  # 지정가
    MARKET         = "01"  # 시장가
    COND_LIMIT     = "02"  # 조건부지정가
    BEST_LIMIT     = "03"  # 최유리지정가
    PRIORITY_LIMIT = "04"  # 최우선지정가
    PRE_MARKET     = "05"  # 장전 시간외
    AFTER_MARKET   = "06"  # 장후 시간외
    SINGLE_PRICE   = "07"  # 시간외 단일가


# ── 주봉/월봉 기간 코드 ───────────────────────────────────────────────

class PeriodCode(StrEnum):
    DAY   = "D"
    WEEK  = "W"
    MONTH = "M"
    YEAR  = "Y"


# ── 매수/매도 구분 ────────────────────────────────────────────────────

class OrderSide(StrEnum):
    BUY  = "BUY"
    SELL = "SELL"


# ── 청산 사유 ─────────────────────────────────────────────────────────

class CloseReason(StrEnum):
    STOP_LOSS    = "stop_loss"
    TAKE_PROFIT  = "take_profit"
    SIGNAL       = "signal"
    MANUAL       = "manual"
    HOLD_PERIOD  = "hold_period"   # 최대 보유일 초과
    CLOSING_TIME = "closing_time"  # 장마감 강제 청산


# ── 매매 신호 ─────────────────────────────────────────────────────────

class TradeSignal(StrEnum):
    BUY  = "BUY"
    SELL = "SELL"


# ── 국내주식 TR_ID ────────────────────────────────────────────────────

class DomesticTRID(StrEnum):
    # 시세 조회 (실전/모의 공통)
    PRICE         = "FHKST01010100"
    DAILY_CHART      = "FHKST03010100"
    MINUTE_CHART     = "FHKST03010200"
    HIST_MINUTE      = "FHKST03010230"
    VOLUME_RANK   = "FHPST01710000"
    CHANGE_RANK   = "FHPST01700000"

    # 주문/계좌 (실전)
    BUY_LIVE          = "TTTC0012U"
    SELL_LIVE         = "TTTC0011U"
    CANCEL_LIVE       = "TTTC0013U"
    BALANCE_LIVE      = "TTTC8434R"
    BUYABLE_LIVE      = "TTTC8908R"
    DAILY_ORDERS_LIVE = "TTTC0081R"

    # 주문/계좌 (모의)
    BUY_PAPER          = "VTTC0012U"
    SELL_PAPER         = "VTTC0011U"
    CANCEL_PAPER       = "VTTC0013U"
    BALANCE_PAPER      = "VTTC8434R"
    BUYABLE_PAPER      = "VTTC8908R"
    DAILY_ORDERS_PAPER = "VTTC0081R"


# ── 해외주식 TR_ID ────────────────────────────────────────────────────

class OverseasTRID(StrEnum):
    # 시세 조회
    PRICE        = "HHDFS00000300"
    DAILY_CHART  = "HHDFS76240000"
    MINUTE_CHART = "HHDFS76950200"
    VOLUME_RANK  = "HHDFS76310010"

    # 주문/계좌 (실전 — 야간 정규장)
    BUY_LIVE          = "TTTT1002U"
    SELL_LIVE         = "TTTT1006U"
    BALANCE_LIVE      = "TTTS2012R"
    DAILY_ORDERS_LIVE = "TTTS2003R"

    # 주문/계좌 (실전 — 주간거래 10:00~22:00)
    DAYTIME_BUY_LIVE  = "JTTT1002U"
    DAYTIME_SELL_LIVE = "JTTT1006U"

    # 주문/계좌 (모의 — 야간/주간 공용)
    BUY_PAPER          = "VTTT1002U"
    SELL_PAPER         = "VTTT1001U"
    BALANCE_PAPER      = "VTTS2012R"
    DAILY_ORDERS_PAPER = "VTTS2003R"


# ── WebSocket TR_ID ───────────────────────────────────────────────────

class WebSocketTRID(StrEnum):
    DOMESTIC_PRICE  = "H0STCNT0"   # 국내주식 실시간체결가 (KRX)
    DOMESTIC_ASKBID = "H0STASP0"   # 국내주식 실시간호가
    OVERSEAS_PRICE  = "HDFSCNT0"   # 해외주식 실시간지연체결가
    DOMESTIC_FILL_LIVE  = "H0STCNI0"   # 국내주식 실시간 체결통보
    DOMESTIC_FILL_PAPER = "H0STCNI9"   # 국내주식 모의 실시간 체결통보
    OVERSEAS_FILL_LIVE  = "H0GSCNI0"   # 해외주식 실시간 체결통보
    OVERSEAS_FILL_PAPER = "H0GSCNI9"   # 해외주식 모의 실시간 체결통보


# ── API 경로 (KIS 스펙 고정) ──────────────────────────────────────────

class DomesticPath(StrEnum):
    ORDER         = "/uapi/domestic-stock/v1/trading/order-cash"
    CANCEL        = "/uapi/domestic-stock/v1/trading/order-rvsecncl"
    BALANCE       = "/uapi/domestic-stock/v1/trading/inquire-balance"
    BUYABLE       = "/uapi/domestic-stock/v1/trading/inquire-psbl-order"
    DAILY_ORDERS  = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
    PRICE         = "/uapi/domestic-stock/v1/quotations/inquire-price"
    DAILY_CHART   = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    MINUTE_CHART  = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
    HIST_MINUTE   = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
    VOLUME_RANK   = "/uapi/domestic-stock/v1/quotations/volume-rank"
    CHANGE_RANK   = "/uapi/domestic-stock/v1/ranking/fluctuation"


class OverseasPath(StrEnum):
    ORDER        = "/uapi/overseas-stock/v1/trading/order"
    BALANCE      = "/uapi/overseas-stock/v1/trading/inquire-balance"
    DAILY_ORDERS = "/uapi/overseas-stock/v1/trading/inquire-ccnl"
    PRICE        = "/uapi/overseas-price/v1/quotations/price"
    DAILY_CHART  = "/uapi/overseas-price/v1/quotations/dailyprice"
    MINUTE_CHART = "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice"
    VOLUME_RANK  = "/uapi/overseas-stock/v1/ranking/trade-vol"


class AuthPath(StrEnum):
    TOKEN    = "/oauth2/tokenP"
    APPROVAL = "/oauth2/Approval"
    REVOKE   = "/oauth2/revokeP"
