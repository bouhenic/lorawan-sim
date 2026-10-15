"""Bus d'événements en mémoire : alimente le flux temps réel du tableau de bord."""

import asyncio
import collections
import itertools

HISTORY = collections.deque(maxlen=400)
_subscribers: set[asyncio.Queue] = set()
_ids = itertools.count(1)


def publish(event: dict):
    event["id"] = next(_ids)
    HISTORY.append(event)
    for q in list(_subscribers):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass  # client trop lent : il rattrapera via l'historique


def subscribe() -> asyncio.Queue:
    q = asyncio.Queue(maxsize=1000)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue):
    _subscribers.discard(q)
