import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from events.detector import EventDetector
from events.types import MarketEvent

if TYPE_CHECKING:
    from events.indicator_cache import IndicatorCache

logger = logging.getLogger(__name__)

EventHandler = Callable[[MarketEvent], Awaitable[None]]

POLL_INTERVAL_SEC = 10


class EventEngine:
    def __init__(self, detector: EventDetector, indicator_cache: "IndicatorCache | None" = None):
        self._detector = detector
        self._indicator_cache = indicator_cache
        self._handlers: list[EventHandler] = []
        self._queue: asyncio.Queue[MarketEvent] = asyncio.Queue()
        self._running = False

    def register(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    async def emit(self, event: MarketEvent) -> None:
        await self._queue.put(event)

    async def run(self) -> None:
        self._running = True
        asyncio.create_task(self._poll_loop())
        asyncio.create_task(self._dispatch_loop())
        if self._indicator_cache:
            asyncio.create_task(self._indicator_cache.refresh_loop())
            logger.info("IndicatorCache refresh loop 시작 (5분 주기)")
        logger.info("EventEngine 시작")

    async def stop(self) -> None:
        self._running = False

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                for event in self._detector.detect():
                    logger.info("이벤트 감지: %s", event)
                    await self._queue.put(event)
            except Exception:
                logger.exception("이벤트 감지 오류")
            await asyncio.sleep(POLL_INTERVAL_SEC)

    async def _dispatch_loop(self) -> None:
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                for handler in self._handlers:
                    try:
                        await handler(event)
                    except Exception:
                        logger.exception("이벤트 핸들러 오류: %s", handler)
                self._queue.task_done()
            except asyncio.TimeoutError:
                pass
            except Exception:
                logger.exception("디스패치 루프 오류")
