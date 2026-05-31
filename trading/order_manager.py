import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from kis.constants import ExchangeCode, CloseReason, TradeSignal
from report.logger import TradeLogger
from .risk import RiskManager

logger = logging.getLogger(__name__)

_KRX = "KRX"   # ExchangeCode에 없는 내부 구분자 (국내주식)


@dataclass
class Position:
    stock_code: str
    name: str
    exchange: str          # "KRX" 또는 ExchangeCode 값
    qty: int
    entry_price: float
    entry_time: datetime = field(default_factory=datetime.now)
    order_no: str = ""
    strategy: str = ""     # 진입에 사용한 전략명 (breakout / pullback / gap)

    def is_domestic(self) -> bool:
        return self.exchange == _KRX


@dataclass
class PendingOrder:
    order_no: str
    side: str
    stock_code: str
    name: str
    exchange: str
    qty: int
    requested_price: float
    strategy: str = ""
    reason: CloseReason | str = CloseReason.SIGNAL
    filled_qty: int = 0
    filled_amount: float = 0.0
    created_at: datetime = field(default_factory=datetime.now)

    def remaining_qty(self) -> int:
        return max(self.qty - self.filled_qty, 0)


class OrderManager:
    def __init__(self, domestic_api, overseas_api, risk: RiskManager,
                 trade_logger: TradeLogger, pg=None, mode: str = "live"):
        self.domestic     = domestic_api
        self.overseas     = overseas_api
        self.risk         = risk
        self.trade_logger = trade_logger
        self.pg           = pg   # PGWriterSync — 없으면 SQLite만 사용
        self.mode         = mode
        self._positions: dict[str, Position] = {}
        self._last_prices: dict[str, float] = {}
        self._pending_orders: dict[str, PendingOrder] = {}

    def _uses_fill_confirmation(self) -> bool:
        return self.mode != "mock"

    # ── 매수 ────────────────────────────────────────────────────────────

    def open_position(self, stock_code: str, name: str, exchange: str, price: float,
                      strategy: str = "") -> bool:
        if not self.risk.can_open_position(len(self._positions)):
            logger.warning("최대 보유 종목 초과: %s", stock_code)
            return False
        if stock_code in self._positions:
            logger.warning("이미 보유 중: %s", stock_code)
            return False
        if self._has_pending(stock_code, "BUY"):
            logger.warning("이미 매수 주문 대기 중: %s", stock_code)
            return False

        account_value = self._get_account_value(exchange)
        qty = self.risk.calc_position_qty(account_value, price)
        if qty <= 0:
            logger.warning("매수 수량 0 (자금 부족): %s", stock_code)
            return False

        try:
            if exchange == _KRX:
                result = self.domestic.buy(stock_code, qty)
            else:
                result = self.overseas.buy(stock_code, ExchangeCode(exchange), qty, price)

            order_no = result.get("ODNO", "")
            if self._uses_fill_confirmation():
                self._register_pending(PendingOrder(
                    order_no=order_no,
                    side="BUY",
                    stock_code=stock_code,
                    name=name,
                    exchange=exchange,
                    qty=qty,
                    requested_price=price,
                    strategy=strategy,
                ))
                logger.info("[매수주문] %s(%s) %d주 @ %.2f 주문번호=%s",
                            name, stock_code, qty, price, order_no)
            else:
                self._confirm_buy(stock_code, name, exchange, qty, price, order_no, strategy)
            return True
        except Exception as e:
            logger.error("매수 실패 (%s): %s", stock_code, e)
            return False

    # ── 매도 ────────────────────────────────────────────────────────────

    def close_position(
        self,
        stock_code: str,
        current_price: float,
        reason: CloseReason | str = CloseReason.SIGNAL,
    ) -> bool:
        pos = self._positions.get(stock_code)
        if not pos:
            logger.warning("보유하지 않은 종목: %s", stock_code)
            return False
        if self._has_pending(stock_code, "SELL"):
            logger.warning("이미 매도 주문 대기 중: %s", stock_code)
            return False

        try:
            if pos.is_domestic():
                result = self.domestic.sell(stock_code, pos.qty)
            else:
                result = self.overseas.sell(stock_code, ExchangeCode(pos.exchange), pos.qty, current_price)

            order_no = result.get("ODNO", "")
            if self._uses_fill_confirmation():
                self._register_pending(PendingOrder(
                    order_no=order_no,
                    side="SELL",
                    stock_code=stock_code,
                    name=pos.name,
                    exchange=pos.exchange,
                    qty=pos.qty,
                    requested_price=current_price,
                    strategy=pos.strategy,
                    reason=reason,
                ))
                logger.info("[매도주문] %s(%s) %d주 @ %.2f 주문번호=%s (%s)",
                            pos.name, stock_code, pos.qty, current_price, order_no, reason)
            else:
                self._confirm_sell(stock_code, pos.qty, current_price, order_no, reason)
            return True
        except Exception as e:
            logger.error("매도 실패 (%s): %s", stock_code, e)
            return False

    # ── 실시간 모니터링 ──────────────────────────────────────────────────

    def on_price_update(self, stock_code: str, current_price: float, signal: TradeSignal | None):
        pos = self._positions.get(stock_code)
        if pos:
            self._last_prices[stock_code] = current_price
            if self.risk.is_stop_loss(pos.entry_price, current_price):
                logger.warning("손절: %s @ %.2f", stock_code, current_price)
                self.close_position(stock_code, current_price, CloseReason.STOP_LOSS)
            elif self.risk.is_take_profit(pos.entry_price, current_price):
                logger.info("익절: %s @ %.2f", stock_code, current_price)
                self.close_position(stock_code, current_price, CloseReason.TAKE_PROFIT)
            elif signal == TradeSignal.SELL:
                self.close_position(stock_code, current_price, CloseReason.SIGNAL)
        else:
            if signal == TradeSignal.BUY:
                logger.info("매수 신호: %s @ %.2f", stock_code, current_price)

    def get_open_positions(self) -> dict[str, Position]:
        return dict(self._positions)

    def get_pending_orders(self) -> dict[str, PendingOrder]:
        return dict(self._pending_orders)

    def get_pending_order_rows(self) -> list[dict]:
        rows = []
        for order in self._pending_orders.values():
            rows.append({
                "id": order.order_no,
                "orderNo": order.order_no,
                "side": order.side,
                "stockCode": order.stock_code,
                "stockName": order.name,
                "market": "domestic" if order.exchange == _KRX else "overseas",
                "quantity": order.qty,
                "filledQuantity": order.filled_qty,
                "remainingQuantity": order.remaining_qty(),
                "requestedPrice": order.requested_price,
                "currency": "KRW" if order.exchange == _KRX else "USD",
                "mode": self.mode,
                "strategy": order.strategy,
                "reason": str(order.reason),
                "createdAt": order.created_at.isoformat(),
            })
        return rows

    def record_price(self, stock_code: str, current_price: float):
        self._last_prices[stock_code] = current_price

    def get_live_positions(self) -> list[dict]:
        rows = []
        for stock_code, pos in self._positions.items():
            current = self._last_prices.get(stock_code, pos.entry_price)
            unrealized_pnl = pos.qty * (current - pos.entry_price)
            unrealized_pct = self.risk.pnl_pct(pos.entry_price, current)
            rows.append({
                "id": stock_code,
                "stockCode": stock_code,
                "stockName": pos.name,
                "market": "domestic" if pos.is_domestic() else "overseas",
                "quantity": pos.qty,
                "avgPrice": pos.entry_price,
                "currency": "KRW" if pos.is_domestic() else "USD",
                "currentPrice": current,
                "marketValue": pos.qty * current,
                "unrealizedPnl": unrealized_pnl,
                "unrealizedPct": unrealized_pct,
                "mode": self.mode,
                "openedAt": pos.entry_time.isoformat(),
                "updatedAt": datetime.now().isoformat(),
                "strategy": pos.strategy,
            })
        return rows

    def on_order_notice(self, event: dict) -> bool:
        """KIS 체결통보 이벤트로 주문을 확정한다."""
        order_no = str(event.get("order_no") or "").strip()
        if not order_no:
            return False
        pending = self._pending_orders.get(order_no)
        if not pending:
            logger.debug("미매칭 체결통보 무시: order_no=%s event=%s", order_no, event)
            return False

        if str(event.get("rejected") or "").strip().upper() == "Y":
            logger.warning("주문 거부 통보: %s %s", order_no, pending.stock_code)
            self._pending_orders.pop(order_no, None)
            return False

        # 공식 샘플 기준 CNTG_YN=2가 실제 체결, 1은 접수/정정/취소/거부 접수 통보.
        if str(event.get("filled") or "").strip() != "2":
            logger.info("주문 접수 통보: %s %s", order_no, pending.stock_code)
            return False

        fill_qty = self._to_int(event.get("filled_qty"), pending.remaining_qty())
        fill_price = self._to_float(
            event.get("filled_price") or event.get("filled_price_12"),
            pending.requested_price,
        )
        if fill_qty <= 0 or fill_price <= 0:
            logger.warning("체결통보 값 이상: order_no=%s qty=%s price=%s",
                           order_no, event.get("filled_qty"), event.get("filled_price"))
            return False

        fill_qty = min(fill_qty, pending.remaining_qty())
        if fill_qty <= 0:
            return False

        if pending.side == "BUY":
            self._confirm_buy(
                pending.stock_code, pending.name, pending.exchange,
                fill_qty, fill_price, order_no, pending.strategy,
            )
        else:
            self._confirm_sell(
                pending.stock_code, fill_qty, fill_price, order_no, pending.reason,
            )

        pending.filled_qty += fill_qty
        pending.filled_amount += fill_qty * fill_price
        if pending.remaining_qty() <= 0:
            self._pending_orders.pop(order_no, None)
        else:
            logger.info("부분 체결: %s %s %d/%d주", pending.side, pending.stock_code,
                        pending.filled_qty, pending.qty)
        return True

    def _get_account_value(self, exchange: str) -> float:
        try:
            if exchange == _KRX:
                balance = self.domestic.get_balance()
                return float(balance["summary"].get("dnca_tot_amt", 0) or 0)
            else:
                balance = self.overseas.get_balance()
                return float(balance["summary"].get("tot_asst_amt", 0) or 0)
        except Exception as e:
            logger.error("계좌 조회 실패: %s", e)
            return 0.0

    def _register_pending(self, order: PendingOrder):
        if not order.order_no:
            raise RuntimeError("KIS 주문번호가 없어 체결 확인을 추적할 수 없습니다")
        self._pending_orders[order.order_no] = order

    def _has_pending(self, stock_code: str, side: str | None = None) -> bool:
        return any(
            order.stock_code == stock_code and (side is None or order.side == side)
            for order in self._pending_orders.values()
        )

    def _confirm_buy(
        self,
        stock_code: str,
        name: str,
        exchange: str,
        qty: int,
        price: float,
        order_no: str,
        strategy: str,
    ):
        existing = self._positions.get(stock_code)
        if existing:
            total_qty = existing.qty + qty
            avg = ((existing.entry_price * existing.qty) + (price * qty)) / total_qty
            existing.qty = total_qty
            existing.entry_price = avg
            existing.order_no = order_no or existing.order_no
        else:
            pos = Position(stock_code, name, exchange, qty, price, order_no=order_no)
            pos.strategy = strategy
            self._positions[stock_code] = pos
        self._last_prices[stock_code] = price
        self.trade_logger.log_buy(stock_code, name, exchange, qty, price, order_no)
        if self.pg and self.mode != "mock":
            self.pg.save_buy(stock_code, name, exchange, qty, price, order_no, mode=self.mode)
        logger.info("[매수체결] %s(%s) %d주 @ %.2f", name, stock_code, qty, price)

    def _confirm_sell(
        self,
        stock_code: str,
        qty: int,
        price: float,
        order_no: str,
        reason: CloseReason | str,
    ):
        pos = self._positions.get(stock_code)
        if not pos:
            logger.warning("체결통보를 받았지만 보유 포지션 없음: %s", stock_code)
            return
        sell_qty = min(qty, pos.qty)
        pnl = self.risk.pnl_pct(pos.entry_price, price)
        self.trade_logger.log_sell(
            stock_code, pos.name, pos.exchange, sell_qty,
            pos.entry_price, price, pnl, reason, order_no,
        )
        if self.pg and self.mode != "mock":
            self.pg.save_sell(
                stock_code, pos.name, pos.exchange, sell_qty,
                pos.entry_price, price, pnl, order_no, mode=self.mode,
                close_position=sell_qty >= pos.qty,
            )
        logger.info("[매도체결] %s(%s) %d주 @ %.2f → PnL %.2f%% (%s)",
                    pos.name, stock_code, sell_qty, price, pnl, reason)
        if sell_qty >= pos.qty:
            del self._positions[stock_code]
        else:
            pos.qty -= sell_qty
            self._last_prices[stock_code] = price

    @staticmethod
    def _to_int(value, default: int = 0) -> int:
        try:
            return int(float(str(value).replace(",", "")))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_float(value, default: float = 0.0) -> float:
        try:
            return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            return default
