import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from kis.constants import ExchangeCode, CloseReason, TradeSignal
from .risk import RiskManager


class TradeLogger:
    """pg_writer가 실제 DB 저장을 담당하므로 Python 로거에만 기록."""

    def log_buy(self, stock_code: str, name: str, exchange: str, qty: int, price: float, order_no: str) -> None:
        logger.info("BUY | %s(%s) %s주 @%.2f order=%s", name, stock_code, qty, price, order_no)

    def log_sell(self, stock_code: str, name: str, exchange: str, qty: int, price: float, pnl_pct: float, reason: str, order_no: str) -> None:
        logger.info("SELL | %s(%s) %s주 @%.2f pnl=%.2f%% reason=%s order=%s", name, stock_code, qty, price, pnl_pct, reason, order_no)

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
        self.pg           = pg
        self.mode         = mode
        self._positions: dict[str, Position] = {}
        self._last_prices: dict[str, float] = {}
        self._pending_orders: dict[str, PendingOrder] = {}
        self._lock = threading.Lock()  # positions/pending_orders 동시 접근 보호
        self.on_fill_callback = None  # Callable[[dict], None] — 체결 시 호출

    _FILL_TIMEOUT_SEC = 30  # 이 시간 내 WebSocket 체결통보 없으면 접수가로 확정

    def _uses_fill_confirmation(self) -> bool:
        return self.mode != "mock"

    def start_fill_timeout_poller(self):
        """체결통보 누락 감시. paper/live에서는 접수만으로 포지션을 확정하지 않는다."""
        if self._uses_fill_confirmation():
            logger.info("체결통보 대기 모드: 접수 주문은 체결통보 수신 전까지 미체결로 유지")
            return

        def _poll():
            while True:
                time.sleep(5)
                now = datetime.now()
                for order_no, pending in list(self._pending_orders.items()):
                    elapsed = (now - pending.created_at).total_seconds()
                    if elapsed >= self._FILL_TIMEOUT_SEC:
                        logger.warning(
                            "[체결폴링] %s %s 주문 %s — %d초 내 체결통보 미수신, 접수가로 확정",
                            pending.side, pending.stock_code, order_no, self._FILL_TIMEOUT_SEC,
                        )
                        event = {
                            "order_no": order_no,
                            "filled": "2",
                            "rejected": "N",
                            "filled_qty": str(pending.remaining_qty()),
                            "filled_price": str(pending.requested_price),
                        }
                        self.on_order_notice(event)

        t = threading.Thread(target=_poll, daemon=True)
        t.start()
        logger.info("체결 타임아웃 폴러 시작 (타임아웃: %d초)", self._FILL_TIMEOUT_SEC)

    def start_fill_reconciliation_poller(self, interval_sec: int = 10):
        """WebSocket 체결통보 누락 시 KIS 주문체결내역으로 pending 주문을 보정한다."""
        if self.mode == "mock":
            return

        def _poll():
            while True:
                time.sleep(interval_sec)
                with self._lock:
                    pending_snapshot = list(self._pending_orders.values())
                if not pending_snapshot:
                    continue

                rows: list[dict] = []
                try:
                    if any(p.exchange == _KRX for p in pending_snapshot):
                        rows.extend(self.domestic.get_daily_orders())
                except Exception as e:
                    logger.warning("국내 체결조회 보정 실패: %s", e)
                try:
                    if any(p.exchange != _KRX for p in pending_snapshot):
                        rows.extend(self.overseas.get_daily_orders())
                except Exception as e:
                    logger.warning("해외 체결조회 보정 실패: %s", e)

                self.reconcile_order_rows(rows)

        t = threading.Thread(target=_poll, daemon=True, name="fill-reconciliation")
        t.start()
        logger.info("체결조회 보정 폴러 시작 (간격: %d초)", interval_sec)

    # ── 매수 ────────────────────────────────────────────────────────────

    _PLACING_PREFIX = "__placing__"

    def open_position(
        self,
        stock_code: str,
        name: str,
        exchange: str,
        price: float,
        strategy: str = "",
        qty_override: int | None = None,
        position_pct_override: float | None = None,
    ) -> bool:
        sentinel_key = f"{self._PLACING_PREFIX}{stock_code}"
        with self._lock:
            if not self.risk.can_open_position(len(self._positions)):
                logger.warning("최대 보유 종목 초과: %s", stock_code)
                return False
            if stock_code in self._positions:
                logger.warning("이미 보유 중: %s", stock_code)
                return False
            if self._has_pending(stock_code, "BUY"):
                logger.warning("이미 매수 주문 대기 중: %s", stock_code)
                return False
            # API 호출 전 sentinel 등록 — 동시 open_position 중복 차단 (TOCTOU 방지)
            self._pending_orders[sentinel_key] = PendingOrder(
                order_no=sentinel_key, side="BUY", stock_code=stock_code,
                name=name, exchange=exchange, qty=0, requested_price=price,
            )

        try:
            account_value = self._get_account_value(exchange)
            if qty_override is not None and qty_override > 0:
                qty = int(qty_override)
                cap_pct = position_pct_override if position_pct_override is not None and position_pct_override > 0 else self.risk.position_size_pct
                max_qty = int((account_value * cap_pct / 100) / price) if price > 0 else 0
                if max_qty <= 0:
                    logger.warning("매수 수량 0 (자금 부족): %s", stock_code)
                    return False
                if qty > max_qty:
                    logger.warning(
                        "AI 요청 수량 clamp: %s requested=%d max=%d cap_pct=%.2f",
                        stock_code, qty, max_qty, cap_pct,
                    )
                    qty = max_qty
            elif position_pct_override is not None and position_pct_override > 0:
                qty = int((account_value * position_pct_override / 100) / price) if price > 0 else 0
            else:
                qty = self.risk.calc_position_qty(account_value, price)
            if qty <= 0:
                logger.warning("매수 수량 0 (자금 부족): %s", stock_code)
                return False

            if exchange == _KRX:
                result = self.domestic.buy(stock_code, qty)
            else:
                result = self.overseas.buy(stock_code, ExchangeCode(exchange), qty, price)

            order_no = result.get("ODNO", "") or ""
            if self._uses_fill_confirmation():
                if not order_no:
                    logger.warning("[매수주문] %s 주문번호 없음 → 즉시 체결 확정 처리", stock_code)
                    with self._lock:
                        self._confirm_buy(stock_code, name, exchange, qty, price, "", strategy)
                else:
                    with self._lock:
                        self._pending_orders[order_no] = PendingOrder(
                            order_no=order_no, side="BUY", stock_code=stock_code,
                            name=name, exchange=exchange, qty=qty,
                            requested_price=price, strategy=strategy,
                        )
                    logger.info("[매수주문] %s(%s) %d주 @ %.2f 주문번호=%s",
                                name, stock_code, qty, price, order_no)
            else:
                with self._lock:
                    self._confirm_buy(stock_code, name, exchange, qty, price, order_no, strategy)
            return True
        except Exception as e:
            logger.error("매수 실패 (%s): %s", stock_code, e)
            return False
        finally:
            with self._lock:
                self._pending_orders.pop(sentinel_key, None)

    # ── 매도 ────────────────────────────────────────────────────────────

    def close_position(
        self,
        stock_code: str,
        current_price: float,
        reason: CloseReason | str = CloseReason.SIGNAL,
    ) -> bool:
        with self._lock:
            pos = self._positions.get(stock_code)
            if not pos:
                logger.warning("보유하지 않은 종목: %s", stock_code)
                return False
            if self._has_pending(stock_code, "SELL"):
                logger.warning("이미 매도 주문 대기 중: %s", stock_code)
                return False
            qty = pos.qty
            exchange = pos.exchange
            name = pos.name
            strategy = pos.strategy

        try:
            if pos.is_domestic():
                result = self.domestic.sell(stock_code, qty)
            else:
                result = self.overseas.sell(stock_code, ExchangeCode(exchange), qty, current_price)

            order_no = result.get("ODNO", "") or ""
            if self._uses_fill_confirmation():
                if not order_no:
                    logger.warning("[매도주문] %s 주문번호 없음 → 즉시 체결 확정 처리", stock_code)
                    with self._lock:
                        self._confirm_sell(stock_code, qty, current_price, "", reason)
                else:
                    with self._lock:
                        self._pending_orders[order_no] = PendingOrder(
                            order_no=order_no, side="SELL", stock_code=stock_code,
                            name=name, exchange=exchange, qty=qty,
                            requested_price=current_price, strategy=strategy, reason=reason,
                        )
                    logger.info("[매도주문] %s(%s) %d주 @ %.2f 주문번호=%s (%s)",
                                name, stock_code, qty, current_price, order_no, reason)
            else:
                with self._lock:
                    self._confirm_sell(stock_code, qty, current_price, order_no, reason)
            return True
        except Exception as e:
            logger.error("매도 실패 (%s): %s", stock_code, e)
            return False

    # ── 실시간 모니터링 ──────────────────────────────────────────────────

    def on_price_update(self, stock_code: str, current_price: float, signal: TradeSignal | None):
        close_reason: CloseReason | None = None
        has_pos = False
        with self._lock:
            pos = self._positions.get(stock_code)
            if pos:
                has_pos = True
                self._last_prices[stock_code] = current_price
                if not self._has_pending(stock_code, "SELL"):
                    if self.risk.is_stop_loss(pos.entry_price, current_price):
                        close_reason = CloseReason.STOP_LOSS
                    elif self.risk.is_take_profit(pos.entry_price, current_price):
                        close_reason = CloseReason.TAKE_PROFIT
                    elif signal == TradeSignal.SELL:
                        close_reason = CloseReason.SIGNAL
            else:
                self._last_prices[stock_code] = current_price

        if close_reason == CloseReason.STOP_LOSS:
            logger.warning("손절: %s @ %.2f", stock_code, current_price)
            self.close_position(stock_code, current_price, CloseReason.STOP_LOSS)
        elif close_reason == CloseReason.TAKE_PROFIT:
            logger.info("익절: %s @ %.2f", stock_code, current_price)
            self.close_position(stock_code, current_price, CloseReason.TAKE_PROFIT)
        elif close_reason == CloseReason.SIGNAL:
            self.close_position(stock_code, current_price, CloseReason.SIGNAL)
        elif not has_pos and signal == TradeSignal.BUY:
            logger.info("매수 신호: %s @ %.2f", stock_code, current_price)

    def get_open_positions(self) -> dict[str, Position]:
        with self._lock:
            return dict(self._positions)

    def get_pending_orders(self) -> dict[str, PendingOrder]:
        with self._lock:
            return dict(self._pending_orders)

    def get_pending_order_rows(self) -> list[dict]:
        rows = []
        for order in self.get_pending_orders().values():
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

    def restore_position(
        self,
        stock_code: str,
        name: str,
        exchange: str,
        qty: int,
        entry_price: float,
        current_price: float | None = None,
        order_no: str = "",
        strategy: str = "breakout",
    ):
        """서버 재시작 후 실제 계좌/DB 보유분을 인메모리 포지션으로 복원."""
        if qty <= 0 or entry_price <= 0:
            return
        with self._lock:
            pos = Position(
                stock_code=stock_code,
                name=name,
                exchange=exchange,
                qty=qty,
                entry_price=entry_price,
                order_no=order_no,
                strategy=strategy,
            )
            self._positions[stock_code] = pos
            self._last_prices[stock_code] = current_price or entry_price
        logger.info(
            "[포지션복원] %s(%s) %d주 @ %.2f strategy=%s",
            name, stock_code, qty, entry_price, strategy,
        )

    def record_price(self, stock_code: str, current_price: float):
        with self._lock:
            self._last_prices[stock_code] = current_price

    def get_live_positions(self) -> list[dict]:
        rows = []
        with self._lock:
            items = list(self._positions.items())
        for stock_code, pos in items:
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

        with self._lock:
            pending = self._pending_orders.get(order_no)
            if not pending:
                logger.debug("미매칭 체결통보 무시: order_no=%s", order_no)
                return False

            if str(event.get("rejected") or "").strip().upper() == "Y":
                logger.warning("주문 거부 통보: %s %s", order_no, pending.stock_code)
                self._pending_orders.pop(order_no, None)
                return False

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
            fully_filled = pending.remaining_qty() <= 0
            if fully_filled:
                self._pending_orders.pop(order_no, None)
            else:
                logger.info("부분 체결: %s %s %d/%d주", pending.side, pending.stock_code,
                            pending.filled_qty, pending.qty)

        if self.on_fill_callback:
            try:
                self.on_fill_callback({
                    "order_no": order_no,
                    "stock_code": pending.stock_code,
                    "stock_name": pending.name,
                    "side": pending.side,
                    "filled_qty": fill_qty,
                    "fill_price": fill_price,
                    "fully_filled": fully_filled,
                })
            except Exception:
                pass
        return True

    def reconcile_order_rows(self, rows: list[dict]) -> int:
        """KIS 주문체결내역 rows를 pending 주문에 반영한다."""
        matched = 0
        for row in rows:
            order_no = str(
                row.get("odno")
                or row.get("ODNO")
                or row.get("odno_no")
                or ""
            ).strip()
            if not order_no:
                continue
            with self._lock:
                pending = self._pending_orders.get(order_no)
            if not pending:
                continue

            filled_qty = (
                row.get("ft_ccld_qty")
                or row.get("tot_ccld_qty")
                or row.get("CCLD_QTY")
                or row.get("filled_qty")
                or "0"
            )
            filled_price = (
                row.get("ft_ccld_unpr3")
                or row.get("avg_prvs")
                or row.get("CCLD_UNPR")
                or row.get("filled_price")
                or pending.requested_price
            )
            rejected_reason = row.get("rjct_rson") or row.get("rjct_rson_name") or ""
            status = str(row.get("prcs_stat_name") or row.get("ord_stat_name") or "")

            if rejected_reason:
                event = {"order_no": order_no, "rejected": "Y"}
            elif self._to_int(filled_qty, 0) > 0:
                event = {
                    "order_no": order_no,
                    "filled": "2",
                    "rejected": "N",
                    "filled_qty": str(filled_qty),
                    "filled_price": str(filled_price),
                }
            elif "완료" in status:
                with self._lock:
                    self._pending_orders.pop(order_no, None)
                logger.warning("완료 상태이나 체결수량 0: %s %s", order_no, pending.stock_code)
                matched += 1
                continue
            else:
                continue

            if self.on_order_notice(event):
                matched += 1
                logger.info("체결조회 보정 반영: %s %s", order_no, pending.stock_code)
        return matched

    def _get_account_value(self, exchange: str) -> float:
        try:
            if exchange == _KRX:
                balance = self.domestic.get_balance()
                return float(balance["summary"].get("dnca_tot_amt", 0) or 0)

            cash = float(self.overseas.get_foreign_margin_usd() or 0)
            if cash > 0:
                return cash

            balance = self.overseas.get_balance()
            summary = balance.get("summary") or {}
            for key in (
                "tot_asst_amt",
                "frcr_dncl_amt1",
                "frcr_buy_amt_smtl1",
                "ord_psbl_cash",
                "ovrs_ord_psbl_amt",
            ):
                value = summary.get(key)
                if value not in (None, ""):
                    amount = float(value or 0)
                    if amount > 0:
                        return amount
            return 0.0
        except Exception as e:
            logger.error("계좌 조회 실패: %s", e)
            return 0.0

    def _has_pending(self, stock_code: str, side: str | None = None) -> bool:
        # 반드시 self._lock 보유 상태에서 호출
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
