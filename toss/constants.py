"""
토스증권 Open API 관련 상수 정의.
https://developers.tossinvest.com/docs
"""
import os
from enum import StrEnum

# ── 환경변수 로드 (URL) ───────────────────────────────────────────────

TOSS_REST_URL = os.getenv("TOSS_REST_URL", "https://openapi.tossinvest.com")

# 액세스 토큰은 client당 1개만 유효하며 재발급 시 이전 토큰은 즉시 무효화된다.
# expires_in은 응답에서 매번 읽지만, 문서 기준 기본값은 86400초(24시간).
TOSS_TOKEN_DEFAULT_EXPIRES_IN = 86400


# ── Rate Limit 그룹 (그룹당 초당 허용 요청 수) ───────────────────────────
# 09:00~09:10 KST 피크시간은 ORDER/ORDER_INFO가 더 빡빡하게 제한된다.

class RateLimitGroup(StrEnum):
    AUTH = "AUTH"
    ACCOUNT = "ACCOUNT"
    ASSET = "ASSET"
    STOCK = "STOCK"
    MARKET_INFO = "MARKET_INFO"
    MARKET_DATA = "MARKET_DATA"
    MARKET_DATA_CHART = "MARKET_DATA_CHART"
    RANKING = "RANKING"
    MARKET_INDICATOR_PRICE = "MARKET_INDICATOR_PRICE"
    MARKET_INDICATOR = "MARKET_INDICATOR"
    MARKET_INDICATOR_CHART = "MARKET_INDICATOR_CHART"
    ORDER = "ORDER"
    ORDER_HISTORY = "ORDER_HISTORY"
    ORDER_INFO = "ORDER_INFO"
    CONDITIONAL_ORDER = "CONDITIONAL_ORDER"
    CONDITIONAL_ORDER_HISTORY = "CONDITIONAL_ORDER_HISTORY"


RATE_LIMIT_PER_SEC: dict[str, float] = {
    RateLimitGroup.AUTH: 5,
    RateLimitGroup.ACCOUNT: 1,
    RateLimitGroup.ASSET: 5,
    RateLimitGroup.STOCK: 5,
    RateLimitGroup.MARKET_INFO: 3,
    RateLimitGroup.MARKET_DATA: 10,
    RateLimitGroup.MARKET_DATA_CHART: 5,
    RateLimitGroup.RANKING: 5,
    RateLimitGroup.MARKET_INDICATOR_PRICE: 10,
    RateLimitGroup.MARKET_INDICATOR: 10,
    RateLimitGroup.MARKET_INDICATOR_CHART: 5,
    RateLimitGroup.ORDER: 6,
    RateLimitGroup.ORDER_HISTORY: 5,
    RateLimitGroup.ORDER_INFO: 6,
    RateLimitGroup.CONDITIONAL_ORDER: 5,
    RateLimitGroup.CONDITIONAL_ORDER_HISTORY: 10,
}

# 오전 동시호가 직후 혼잡 시간대 — ORDER/ORDER_INFO 그룹만 더 낮게 제한된다.
PEAK_HOUR_START = (9, 0)
PEAK_HOUR_END = (9, 10)
PEAK_RATE_LIMIT_PER_SEC: dict[str, float] = {
    RateLimitGroup.ORDER: 3,
    RateLimitGroup.ORDER_INFO: 3,
}


# ── 매매 관련 enum ────────────────────────────────────────────────────

class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class TimeInForce(StrEnum):
    DAY = "DAY"
    CLS = "CLS"   # 장마감주문 (LIMIT + US만 지원)


class OrderStatusGroup(StrEnum):
    OPEN = "OPEN"      # PENDING, PARTIAL_FILLED, PENDING_CANCEL, PENDING_REPLACE
    CLOSED = "CLOSED"  # FILLED, CANCELED, REJECTED, REPLACED, ...


class ConditionalOrderType(StrEnum):
    SINGLE = "SINGLE"
    OCO = "OCO"
    OTO = "OTO"


class Currency(StrEnum):
    KRW = "KRW"
    USD = "USD"


class MarketCountry(StrEnum):
    KR = "KR"
    US = "US"


# ── API 경로 ──────────────────────────────────────────────────────────

class AuthPath(StrEnum):
    TOKEN = "/oauth2/token"


class AccountPath(StrEnum):
    ACCOUNTS = "/api/v1/accounts"


class AssetPath(StrEnum):
    HOLDINGS = "/api/v1/holdings"


class MarketDataPath(StrEnum):
    ORDERBOOK = "/api/v1/orderbook"
    PRICES = "/api/v1/prices"
    TRADES = "/api/v1/trades"
    PRICE_LIMITS = "/api/v1/price-limits"
    CANDLES = "/api/v1/candles"


class StockInfoPath(StrEnum):
    STOCKS = "/api/v1/stocks"
    WARNINGS = "/api/v1/stocks/{symbol}/warnings"


class MarketInfoPath(StrEnum):
    EXCHANGE_RATE = "/api/v1/exchange-rate"
    MARKET_CALENDAR_KR = "/api/v1/market-calendar/KR"
    MARKET_CALENDAR_US = "/api/v1/market-calendar/US"


class RankingPath(StrEnum):
    RANKINGS = "/api/v1/rankings"


class OrderPath(StrEnum):
    ORDERS = "/api/v1/orders"
    ORDER_DETAIL = "/api/v1/orders/{orderId}"
    MODIFY = "/api/v1/orders/{orderId}/modify"
    CANCEL = "/api/v1/orders/{orderId}/cancel"


class OrderInfoPath(StrEnum):
    BUYING_POWER = "/api/v1/buying-power"
    SELLABLE_QUANTITY = "/api/v1/sellable-quantity"
    COMMISSIONS = "/api/v1/commissions"


class ConditionalOrderPath(StrEnum):
    CREATE = "/api/v1/conditional-orders"
    DETAIL = "/api/v1/conditional-orders/{conditionalOrderId}"
    MODIFY = "/api/v1/conditional-orders/{conditionalOrderId}/modify"
    CANCEL = "/api/v1/conditional-orders/{conditionalOrderId}"
