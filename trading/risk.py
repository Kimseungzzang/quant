import json
import logging
from datetime import date

logger = logging.getLogger(__name__)

_REDIS_KEY = "ai:risk_params"
_OVERRIDABLE_KEYS = ("stop_loss_pct", "take_profit_pct", "max_positions", "position_size_pct")

# 해외 매수 일일 한도 — set_risk_params(AI 툴)로는 못 건드림. 사람이 직접 설정하는 하드 캡.
_OVERSEAS_BUDGET_KEY = "trading:overseas_daily_budget_usd"
_OVERSEAS_SPEND_KEY_PREFIX = "trading:overseas_daily_spend:"


class RiskManager:
    """
    손절/익절/최대보유종목수/종목당 투자비율.
    config.yaml의 trading: 섹션이 기본값이며, AI가 set_risk_params 툴로 Redis에 override를
    저장하면 이후 모든 판단(손절/익절/수량계산/신규진입가능여부)에 즉시 반영된다.
    override 값에 대한 상한/하한 검증은 하지 않는다 — AI 판단에 완전히 맡긴다.
    """

    def __init__(self, config: dict, redis_client=None):
        self._r = redis_client
        self._defaults = {
            "stop_loss_pct": config["trading"]["stop_loss_pct"],
            "take_profit_pct": config["trading"]["take_profit_pct"],
            "max_positions": config["trading"]["max_positions"],
            "position_size_pct": config["trading"]["position_size_pct"],
        }

    def get_params(self) -> dict:
        return {**self._defaults, **self._load_overrides()}

    def set_params(self, **kwargs) -> dict:
        if not self._r:
            raise RuntimeError("Redis 연결이 없어 리스크 파라미터를 저장할 수 없습니다.")
        current = self._load_overrides()
        changed = {}
        for key in _OVERRIDABLE_KEYS:
            value = kwargs.get(key)
            if value is not None:
                current[key] = value
                changed[key] = value
        if changed:
            self._r.set(_REDIS_KEY, json.dumps(current))
            logger.info("리스크 파라미터 변경(AI): %s", changed)
        return self.get_params()

    def calc_position_qty(self, account_value: float, price: float) -> int:
        """종목당 투자금액 기반 매수 수량 계산."""
        if price <= 0:
            return 0
        invest_amount = account_value * self.get_params()["position_size_pct"] / 100
        qty = int(invest_amount / price)
        return max(qty, 0)

    def is_stop_loss(self, entry_price: float, current_price: float) -> bool:
        if entry_price <= 0:
            return False
        pnl_pct = (current_price - entry_price) / entry_price * 100
        return pnl_pct <= -self.get_params()["stop_loss_pct"]

    def is_take_profit(self, entry_price: float, current_price: float) -> bool:
        if entry_price <= 0:
            return False
        pnl_pct = (current_price - entry_price) / entry_price * 100
        return pnl_pct >= self.get_params()["take_profit_pct"]

    def pnl_pct(self, entry_price: float, current_price: float) -> float:
        if entry_price <= 0:
            return 0.0
        return (current_price - entry_price) / entry_price * 100

    def can_open_position(self, current_position_count: int) -> bool:
        return current_position_count < self.get_params()["max_positions"]

    # ── 해외 매수 일일 한도 (사람 전용 하드 캡, AI는 조정 불가) ────────────────

    def get_overseas_daily_budget_usd(self) -> float | None:
        if not self._r:
            return None
        try:
            raw = self._r.get(_OVERSEAS_BUDGET_KEY)
            return float(raw) if raw else None
        except Exception:
            return None

    def set_overseas_daily_budget_usd(self, amount_usd: float | None) -> None:
        if not self._r:
            raise RuntimeError("Redis 연결이 없어 저장할 수 없습니다.")
        if amount_usd is None:
            self._r.delete(_OVERSEAS_BUDGET_KEY)
            logger.info("해외 매수 일일 한도 해제")
        else:
            self._r.set(_OVERSEAS_BUDGET_KEY, str(amount_usd))
            logger.info("해외 매수 일일 한도 설정: $%.2f", amount_usd)

    def get_overseas_spend_today_usd(self) -> float:
        if not self._r:
            return 0.0
        try:
            raw = self._r.get(self._today_spend_key())
            return float(raw) if raw else 0.0
        except Exception:
            return 0.0

    def get_overseas_remaining_budget_usd(self) -> float | None:
        """None이면 한도 자체가 없음(무제한). 있으면 오늘 더 쓸 수 있는 금액(음수 가능)."""
        budget = self.get_overseas_daily_budget_usd()
        if budget is None:
            return None
        return budget - self.get_overseas_spend_today_usd()

    def record_overseas_spend(self, amount_usd: float) -> None:
        if not self._r or amount_usd <= 0:
            return
        try:
            new_total = self.get_overseas_spend_today_usd() + amount_usd
            self._r.set(self._today_spend_key(), str(new_total), ex=2 * 24 * 3600)
            logger.info("해외 매수 사용액 기록: +$%.2f (오늘 누적 $%.2f)", amount_usd, new_total)
        except Exception as e:
            logger.warning("해외 매수 사용액 기록 실패: %s", e)

    @staticmethod
    def _today_spend_key() -> str:
        return f"{_OVERSEAS_SPEND_KEY_PREFIX}{date.today().isoformat()}"

    def _load_overrides(self) -> dict:
        if not self._r:
            return {}
        try:
            raw = self._r.get(_REDIS_KEY)
            return json.loads(raw) if raw else {}
        except Exception:
            return {}
