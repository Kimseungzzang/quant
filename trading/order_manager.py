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


class OrderManager:
    def __init__(self, domestic_api, overseas_api, risk: RiskManager,
                 trade_logger: TradeLogger, pg=None):
        self.domestic     = domestic_api
        self.overseas     = overseas_api
        self.risk         = risk
        self.trade_logger = trade_logger
        self.pg           = pg   # PGWriterSync — 없으면 SQLite만 사용
        self._positions: dict[str, Position] = {}

    # ── 매수 ────────────────────────────────────────────────────────────

    def open_position(self, stock_code: str, name: str, exchange: str, price: float,
                      strategy: str = "") -> bool:
        if not self.risk.can_open_position(len(self._positions)):
            logger.warning("최대 보유 종목 초과: %s", stock_code)
            return False
        if stock_code in self._positions:
            logger.warning("이미 보유 중: %s", stock_code)
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
            pos = Position(stock_code, name, exchange, qty, price, order_no=order_no)
            pos.strategy = strategy  # 어떤 전략으로 진입했는지 기록
            self._positions[stock_code] = pos
            self.trade_logger.log_buy(stock_code, name, exchange, qty, price, order_no)
            if self.pg:
                self.pg.save_buy(stock_code, name, exchange, qty, price, order_no)
            logger.info("[매수] %s(%s) %d주 @ %.2f", name, stock_code, qty, price)
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

        try:
            if pos.is_domestic():
                result = self.domestic.sell(stock_code, pos.qty)
            else:
                result = self.overseas.sell(stock_code, ExchangeCode(pos.exchange), pos.qty, current_price)

            pnl      = self.risk.pnl_pct(pos.entry_price, current_price)
            order_no = result.get("ODNO", "")

            self.trade_logger.log_sell(
                stock_code, pos.name, pos.exchange, pos.qty,
                pos.entry_price, current_price, pnl, reason, order_no,
            )
            if self.pg:
                self.pg.save_sell(
                    stock_code, pos.name, pos.exchange, pos.qty,
                    pos.entry_price, current_price, pnl, order_no,
                )
            logger.info("[매도] %s(%s) %d주 @ %.2f → PnL %.2f%% (%s)",
                        pos.name, stock_code, pos.qty, current_price, pnl, reason)
            del self._positions[stock_code]
            return True
        except Exception as e:
            logger.error("매도 실패 (%s): %s", stock_code, e)
            return False

    # ── 실시간 모니터링 ──────────────────────────────────────────────────

    def on_price_update(self, stock_code: str, current_price: float, signal: TradeSignal | None):
        pos = self._positions.get(stock_code)
        if pos:
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
