from __future__ import annotations

import asyncio
from app.marketplace.models import QueueItem
from app.utils.stats import RuntimeStats


class BoundedPriorityQueue:
    def __init__(self, maxsize: int = 100) -> None:
        self.maxsize = max(0, int(maxsize))
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq = 0
        self._lock = asyncio.Lock()

    def qsize(self) -> int:
        return self._queue.qsize()

    def empty(self) -> bool:
        return self._queue.empty()

    def is_full(self) -> bool:
        if self.maxsize <= 0:
            return False
        return self._queue.qsize() >= self.maxsize

    async def put(self, item: QueueItem) -> bool:
        async with self._lock:
            if self.is_full():
                return False
            self._seq += 1
            await self._queue.put((-int(item.priority), self._seq, item))
            return True

    async def get(self) -> QueueItem:
        _priority, _seq, item = await self._queue.get()
        return item

    def task_done(self) -> None:
        self._queue.task_done()

    async def join(self) -> None:
        await self._queue.join()


class AppState:
    def __init__(self, max_queue_size: int = 100) -> None:
        self.queue = BoundedPriorityQueue(max_queue_size)
        self.stats = RuntimeStats()
        self.scanner_running = False
        self.publisher_running = False
        self.scanner_paused = False
        self.shutdown = False
        self.session_invalid = False
        self.pause_event = asyncio.Event()
        self.pause_event.set()

    def pause(self) -> None:
        self.scanner_paused = True
        self.pause_event.clear()

    def resume(self) -> None:
        self.scanner_paused = False
        self.pause_event.set()

    def request_shutdown(self) -> None:
        self.shutdown = True
        self.pause_event.set()

    def scanner_status(self) -> str:
        if not self.scanner_running:
            return "STOPPED"
        if self.scanner_paused:
            return "PAUSED"
        return "RUNNING"

    def publisher_status(self) -> str:
        if not self.publisher_running:
            return "STOPPED"
        if self.scanner_paused and self.queue.empty():
            return "WAITING"
        return "RUNNING"
