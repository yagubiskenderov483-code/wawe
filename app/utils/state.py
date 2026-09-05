from __future__ import annotations

import asyncio
from app.marketplace.models import QueueItem
from app.utils.stats import RuntimeStats


def pick_diversified_index(
    gift_ids: list[int | None],
    last_gift_id: int | None,
    streak: int,
    max_same_gift_streak: int,
    enabled: bool,
) -> int:
    if not gift_ids:
        return -1
    if not enabled or last_gift_id is None or streak < max_same_gift_streak:
        return 0
    for index, gift_id in enumerate(gift_ids):
        if gift_id != last_gift_id:
            return index
    return 0


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

    def get_nowait(self) -> QueueItem | None:
        try:
            _priority, _seq, item = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None
        return item

    def drain(self) -> list[QueueItem]:
        items: list[QueueItem] = []
        while True:
            item = self.get_nowait()
            if item is None:
                break
            items.append(item)
            self.task_done()
        return items

    async def get_diversified(
        self,
        last_gift_id: int | None,
        streak: int,
        max_same_gift_streak: int,
        enabled: bool,
    ) -> QueueItem:
        if not enabled or last_gift_id is None or streak < max_same_gift_streak:
            return await self.get()

        first = await self.get()
        if first.listing.gift_id != last_gift_id:
            return first

        rest: list[QueueItem] = []
        chosen: QueueItem | None = None
        while True:
            nxt = self.get_nowait()
            if nxt is None:
                break
            if chosen is None and nxt.listing.gift_id != last_gift_id:
                chosen = nxt
            else:
                rest.append(nxt)
                self.task_done()

        if chosen is None:
            for item in rest:
                await self.put(item)
            return first

        self.task_done()
        await self.put(first)
        for item in rest:
            await self.put(item)
        return chosen

    def task_done(self) -> None:
        try:
            self._queue.task_done()
        except ValueError:
            pass

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
        self.user_authorized = False
        self.workers_started = False
        self.scanner_mode = "INITIAL_SNAPSHOT"
        self.last_published_gift_id: int | None = None
        self.same_gift_streak = 0

    def pause(self) -> list[QueueItem]:
        self.scanner_paused = True
        self.pause_event.clear()
        return self.queue.drain()

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
        if self.scanner_paused:
            return "PAUSED"
        return "RUNNING"

    def note_published_gift(self, gift_id: int | None) -> None:
        if gift_id is None:
            self.last_published_gift_id = None
            self.same_gift_streak = 0
            return
        if gift_id == self.last_published_gift_id:
            self.same_gift_streak += 1
        else:
            self.last_published_gift_id = gift_id
            self.same_gift_streak = 1
