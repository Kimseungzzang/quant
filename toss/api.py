"""
토스증권 Open API — 실제 HTTP 호출 구현체.

주의: /prices, /trades, /price-limits 의 정확한 쿼리 파라미터 형태는 문서 렌더링
제약으로 100% 확정하지 못했다 (candles/orderbook/stocks/rankings/holdings/orders는
실제 렌더링된 스펙으로 확인함). /prices는 /stocks와 동일하게 symbols(콤마구분,
응답이 배열) 패턴으로 구현해뒀으니, 스모크테스트 단계에서 실제 응답을 보고
어긋나면 이 파일만 고치면 된다 (호출부는 영향 없음).
"""
import logging
import time
import uuid

from .constants import (
    RateLimitGroup,
    MarketDataPath, StockInfoPath, MarketInfoPath, RankingPath,
    AssetPath, OrderPath, OrderInfoPath, AccountPath,
)

logger = logging.getLogger(__name__)


def new_client_order_id(stock_code: str) -> str:
    """멱등키. 최대 36자, 영숫자/-/_ 만 허용, 10분간 유효."""
    return f"{stock_code}-{int(time.time())}-{uuid.uuid4().hex[:6]}"


class TossBrokerAPI:
    def __init__(self, client):
        self.client = client

    # ── 계좌 ────────────────────────────────────────────────────────────

    def get_accounts(self) -> list[dict]:
        data = self.client.get(AccountPath.ACCOUNTS, RateLimitGroup.ACCOUNT)
        return data.get("result") or []

    # ── 시세 ────────────────────────────────────────────────────────────

    def get_prices(self, symbols: list[str]) -> list[dict]:
        data = self.client.get(
            MarketDataPath.PRICES, RateLimitGroup.MARKET_DATA,
            params={"symbols": ",".join(symbols)},
        )
        result = data.get("result")
        return result if isinstance(result, list) else ([result] if result else [])

    def get_orderbook(self, symbol: str) -> dict:
        data = self.client.get(
            MarketDataPath.ORDERBOOK, RateLimitGroup.MARKET_DATA,
            params={"symbol": symbol},
        )
        return data.get("result") or {}

    def get_candles(self, symbol: str, interval: str, count: int = 100,
                     before: str | None = None, adjusted: bool = True) -> dict:
        params = {"symbol": symbol, "interval": interval, "count": count, "adjusted": adjusted}
        if before:
            params["before"] = before
        data = self.client.get(MarketDataPath.CANDLES, RateLimitGroup.MARKET_DATA_CHART, params=params)
        return data.get("result") or {"candles": [], "nextBefore": None}

    def get_stocks(self, symbols: list[str]) -> list[dict]:
        data = self.client.get(
            StockInfoPath.STOCKS, RateLimitGroup.STOCK,
            params={"symbols": ",".join(symbols)},
        )
        return data.get("result") or []

    def get_rankings(self, ranking_type: str, market_country: str, duration: str,
                      count: int = 100, exclude_investment_caution: bool = False) -> dict:
        data = self.client.get(
            RankingPath.RANKINGS, RateLimitGroup.RANKING,
            params={
                "type": ranking_type,
                "marketCountry": market_country,
                "duration": duration,
                "count": count,
                "excludeInvestmentCaution": exclude_investment_caution,
            },
        )
        return data.get("result") or {"rankedAt": None, "rankings": []}

    # ── 자산 ────────────────────────────────────────────────────────────

    def get_holdings(self, symbol: str | None = None) -> dict:
        params = {"symbol": symbol} if symbol else None
        data = self.client.get(AssetPath.HOLDINGS, RateLimitGroup.ASSET, params=params, need_account=True)
        return data.get("result") or {}

    # ── 주문 가능 정보 ─────────────────────────────────────────────────────

    def get_buying_power(self, currency: str) -> dict:
        data = self.client.get(
            OrderInfoPath.BUYING_POWER, RateLimitGroup.ORDER_INFO,
            params={"currency": currency}, need_account=True,
        )
        return data.get("result") or {}

    def get_sellable_quantity(self, symbol: str) -> dict:
        data = self.client.get(
            OrderInfoPath.SELLABLE_QUANTITY, RateLimitGroup.ORDER_INFO,
            params={"symbol": symbol}, need_account=True,
        )
        return data.get("result") or {}

    # ── 주문 ────────────────────────────────────────────────────────────

    def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: str | None = None,
        order_amount: str | None = None,
        price: str | None = None,
        time_in_force: str | None = None,
        confirm_high_value_order: bool = False,
        client_order_id: str | None = None,
    ) -> dict:
        body: dict = {
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "clientOrderId": client_order_id or new_client_order_id(symbol),
            "confirmHighValueOrder": confirm_high_value_order,
        }
        if quantity is not None:
            body["quantity"] = str(quantity)
        if order_amount is not None:
            body["orderAmount"] = str(order_amount)
        if price is not None:
            body["price"] = str(price)
        if time_in_force:
            body["timeInForce"] = time_in_force
        data = self.client.post(OrderPath.ORDERS, RateLimitGroup.ORDER, json_body=body, need_account=True)
        return data.get("result") or {}

    def cancel_order(self, order_id: str) -> dict:
        path = str(OrderPath.CANCEL).format(orderId=order_id)
        data = self.client.post(path, RateLimitGroup.ORDER, need_account=True)
        return data.get("result") or {}

    def modify_order(self, order_id: str, price: str | None = None, quantity: str | None = None) -> dict:
        path = str(OrderPath.MODIFY).format(orderId=order_id)
        body = {}
        if price is not None:
            body["price"] = str(price)
        if quantity is not None:
            body["quantity"] = str(quantity)
        data = self.client.post(path, RateLimitGroup.ORDER, json_body=body, need_account=True)
        return data.get("result") or {}

    def get_orders(self, status: str, symbol: str | None = None,
                    from_date: str | None = None, to_date: str | None = None,
                    cursor: str | None = None, limit: int | None = None) -> dict:
        params: dict = {"status": status}
        if symbol:
            params["symbol"] = symbol
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        data = self.client.get(OrderPath.ORDERS, RateLimitGroup.ORDER_HISTORY, params=params, need_account=True)
        return data.get("result") or {"orders": [], "nextCursor": None, "hasNext": False}
