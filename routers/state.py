"""
공유 애플리케이션 상태.
fastapi_app.py 가 lifespan 에서 채우고, 라우터 파일들이 읽는다.
"""
import asyncio
import collections
import logging
import threading
from typing import Any

from fastapi import WebSocket

ws_clients: set[WebSocket] = set()
components: dict[str, Any] = {}
agent: Any = None
event_engine: Any = None
components_lock = threading.Lock()
trading_task: asyncio.Task | None = None
trading_last_error: str | None = None

log_buffer: collections.deque = collections.deque(maxlen=500)


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        import datetime as _dt
        log_buffer.append({
            "ts": _dt.datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
            "level": record.levelname,
            "name": record.name.split(".")[-1],
            "msg": record.getMessage(),
        })


buf_handler = _BufferHandler()
buf_handler.setLevel(logging.INFO)
