"""Bounded content-free counters and per-process request admission.

No caller-selected labels, tokens, task labels or identifiers are stored here.
"""
import logging
import threading
from collections import Counter
from fastapi.responses import JSONResponse

_lock = threading.Lock()
_counts = Counter()
_slots = threading.BoundedSemaphore(16)
log = logging.getLogger('aj.llm_layer.receiver')


def metrics_snapshot():
    with _lock:
        return dict(_counts)


class ReceiverEnvelope:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['path'] != '/internal/v1/shadow':
            return await self.app(scope, receive, send)
        if not _slots.acquire(blocking=False):
            with _lock:
                _counts['overloaded'] += 1
            return await JSONResponse({'detail':'shadow receiver busy'},status_code=503)(scope,receive,send)
        async def record(message):
            if message['type'] == 'http.response.start':
                status = message['status']
                outcome = ('accepted' if status == 200 else 'unauthorized' if status == 401
                           else 'invalid_metadata' if status == 422 else 'resource_limit' if status in (408,413)
                           else 'unavailable' if status == 503 else 'other')
                with _lock:
                    _counts[outcome] += 1
                log.info('shadow_outcome outcome=%s',outcome)
            await send(message)
        try:
            await self.app(scope,receive,record)
        finally:
            _slots.release()
