import logging

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, config: dict):
        self.stop_loss_pct = config["trading"]["stop_loss_pct"]
        self.take_profit_pct = config["trading"]["take_profit_pct"]
        self.max_positions = config["trading"]["max_positions"]
        self.position_size_pct = config["trading"]["position_size_pct"]

    def calc_position_qty(self, account_value: float, price: float) -> int:
        """종목당 투자금액 기반 매수 수량 계산."""
        if price <= 0:
            return 0
        invest_amount = account_value * self.position_size_pct / 100
        qty = int(invest_amount / price)
        return max(qty, 0)

    def is_stop_loss(self, entry_price: float, current_price: float) -> bool:
        if entry_price <= 0:
            return False
        pnl_pct = (current_price - entry_price) / entry_price * 100
        return pnl_pct <= -self.stop_loss_pct

    def is_take_profit(self, entry_price: float, current_price: float) -> bool:
        if entry_price <= 0:
            return False
        pnl_pct = (current_price - entry_price) / entry_price * 100
        return pnl_pct >= self.take_profit_pct

    def pnl_pct(self, entry_price: float, current_price: float) -> float:
        if entry_price <= 0:
            return 0.0
        return (current_price - entry_price) / entry_price * 100

    def can_open_position(self, current_position_count: int) -> bool:
        return current_position_count < self.max_positions
