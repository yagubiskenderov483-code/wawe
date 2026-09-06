from __future__ import annotations

import asyncio
from app.marketplace.models import QueueItem
from app.utils.stats import RuntimeStats


def _norm_model(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip().casefold()
    return text or None


def pick_diversified_index(
    gift_ids: list[int | None],
    last_gift_id: int | None,
    streak: int,
    max_same_gift_streak: int,
    enabled: bool,
    owner_ids: list[int | None] | None = None,
    last_owner_id: int | None = None,
    unique_owners: bool = False,
    models: list[str | None] | None = None,
    last_model: str | None = None,
) -> int:
    if not gift_ids:
        return -1
    last_model_key = _norm_model(last_model)
    for index, gift_id in enumerate(gift_ids):
        owner = owner_ids[index] if owner_ids is not None and index < len(owner_ids) else None
        model = models[index] if models is not None and index < len(models) else None
        gift_repeat = (
            enabled
            and last_gift_id is not None
            and streak >= max_same_gift_streak
            and gift_id == last_gift_id
        )
        owner_repeat = unique_owners and last_owner_id is not None and owner == last_owner_id
        model_repeat = enabled and last_model_key is not None and _norm_model(model) == last_model_key
        if not gift_repeat and not owner_repeat and not model_repeat:
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
        last_owner_id: int | None = None,
        unique_owners: bool = False,
        last_model: str | None = None,
    ) -> QueueItem:
        first = await self.get()
        owner = first.listing.seller_id or first.listing.owner_id
        last_model_key = _norm_model(last_model)
        gift_repeat = (
            enabled
            and last_gift_id is not None
            and streak >= max_same_gift_streak
            and first.listing.gift_id == last_gift_id
        )
        owner_repeat = unique_owners and last_owner_id is not None and owner == last_owner_id
        model_repeat = enabled and last_model_key is not None and _norm_model(first.listing.model) == last_model_key
        if not gift_repeat and not owner_repeat and not model_repeat:
            return first

        rest: list[QueueItem] = []
        chosen: QueueItem | None = None
        while True:
            nxt = self.get_nowait()
            if nxt is None:
                break
            nxt_owner = nxt.listing.seller_id or nxt.listing.owner_id
            nxt_gift_repeat = (
                enabled
                and last_gift_id is not None
                and streak >= max_same_gift_streak
                and nxt.listing.gift_id == last_gift_id
            )
            nxt_owner_repeat = unique_owners and last_owner_id is not None and nxt_owner == last_owner_id
            nxt_model_repeat = (
                enabled and last_model_key is not None and _norm_model(nxt.listing.model) == last_model_key
            )
            if chosen is None and not nxt_gift_repeat and not nxt_owner_repeat and not nxt_model_repeat:
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
        self.last_published_owner_id: int | None = None
        self.last_published_model: str | None = None
        self.catalog_primary = 0
        self.catalog_secondary = 0
        self.catalog_skipped = 0

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

    def note_published_gift(
        self,
        gift_id: int | None,
        owner_id: int | None = None,
        model: str | None = None,
    ) -> None:
        if gift_id is None:
            self.last_published_gift_id = None
            self.same_gift_streak = 0
        elif gift_id == self.last_published_gift_id:
            self.same_gift_streak += 1
        else:
            self.last_published_gift_id = gift_id
            self.same_gift_streak = 1
        if owner_id is not None:
            self.last_published_owner_id = owner_id
        self.last_published_model = model
