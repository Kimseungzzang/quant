import logging
from datetime import datetime
from pathlib import Path

import yaml


class CandleAggregator:
    """실시간 틱을 N분봉으로 집계."""

    def __init__(self, period_minutes: int, max_candles: int = 300):
        self.period = period_minutes
        self.max_candles = max_candles
        self._current: dict | None = None
        self._completed: list[dict] = []

    def update(self, tick: dict) -> bool:
        time_str = str(tick.get("time", "") or "")
        if len(time_str) < 4:
            return False
        try:
            h = int(time_str[0:2])
            m = int(time_str[2:4])
            price = float(tick.get("price", 0) or 0)
            vol = float(tick.get("vol", 0) or 0)
        except (ValueError, TypeError):
            return False
        if price <= 0:
            return False

        slot = (h * 60 + m) // self.period
        bucket_minute = slot * self.period
        candle_dt = datetime.now().replace(
            hour=bucket_minute // 60, minute=bucket_minute % 60,
            second=0, microsecond=0,
        )
        completed = False
        if self._current is None or self._current["slot"] != slot:
            if self._current is not None:
                c = self._current
                self._completed.append({
                    "datetime": c["datetime"],
                    "open": c["open"], "high": c["high"],
                    "low": c["low"], "close": c["close"], "volume": c["volume"],
                })
                if len(self._completed) > self.max_candles:
                    self._completed.pop(0)
                completed = True
            self._current = {
                "slot": slot, "datetime": candle_dt, "open": price,
                "high": price, "low": price, "close": price, "volume": vol,
            }
        else:
            self._current["high"] = max(self._current["high"], price)
            self._current["low"] = min(self._current["low"], price)
            self._current["close"] = price
            self._current["volume"] += vol
        return completed

    def get_df(self):
        import pandas as pd
        return pd.DataFrame(self._completed) if self._completed else pd.DataFrame()


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def setup_logging(config: dict) -> None:
    log_cfg = config.get("logging", {})
    log_level = getattr(logging, log_cfg.get("level", "INFO"))
    log_file = log_cfg.get("file", "data/trading.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
