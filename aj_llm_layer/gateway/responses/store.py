"""Short-lived memory of recent replies, so `previous_response_id` works.

AJ's tool loop sends only the new tool outputs plus `previous_response_id`,
expecting the server to remember the rest of the turn. Providers are called
with `store` off, so the Layer keeps that context itself:

* in this process's memory only (never written to disk or logs);
* for 30 minutes after the last use, at most 2,000 conversations;
* readable only by the app that created it (no cross-app chaining).

An unknown or expired id is reported in the shape AJ already handles: AJ
rebuilds the conversation from its own store and retries.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

TTL_SECONDS = 30 * 60
MAX_ENTRIES = 2000


@dataclass
class Entry:
    app: str
    data_class: str
    items: list
    provider: str
    model: str
    expires: float


class ResponseStore:
    def __init__(self, *, ttl: float = TTL_SECONDS, max_entries: int = MAX_ENTRIES,
                 clock: Callable[[], float] = time.monotonic):
        self._ttl, self._max, self._clock = ttl, max_entries, clock
        self._data: OrderedDict[str, Entry] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, response_id: str, *, app: str, data_class: str, items: list, provider: str, model: str) -> None:
        with self._lock:
            self._data[response_id] = Entry(app, data_class, list(items), provider, model, self._clock() + self._ttl)
            self._data.move_to_end(response_id)
            self._evict()

    def get(self, response_id: str, *, app: str) -> Entry | None:
        with self._lock:
            self._evict()
            entry = self._data.get(response_id)
            if entry is None or entry.app != app:
                return None
            entry.expires = self._clock() + self._ttl
            self._data.move_to_end(response_id)
            return entry

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def _evict(self) -> None:
        now = self._clock()
        for key in [k for k, e in self._data.items() if e.expires <= now]:
            del self._data[key]
        while len(self._data) > self._max:
            self._data.popitem(last=False)
