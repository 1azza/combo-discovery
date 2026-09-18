"""Adapter over the card-image helper owned by a parallel lane.

The real implementation lives in :mod:`combo_discovery.corpus.images` and may
not exist, so the import is guarded and every failure degrades to ``None`` (the
Gallery then renders a typographic card tile instead of a broken image).

Its first call loads a ~24 MB Scryfall bulk in-process. :func:`warm_images` lets
the server pay that cost at startup instead of on the first page paint, and a
small in-process cache keeps repeat lookups free.
"""

from __future__ import annotations

import time
from typing import Any

try:  # pragma: no cover - exercised when the parallel module is present
    from ..corpus.images import image_url as _image_url
except Exception:  # noqa: BLE001 - the module arrives in a parallel lane
    _image_url = None

_CACHE: dict[tuple[str, int, str], str] = {}


def image_url(card_name: Any, face: int = 0, size: str = "normal") -> str | None:
    """Return a Scryfall image URL, or ``None`` when there is no usable art."""
    if _image_url is None or not card_name:
        return None
    try:
        key = (str(card_name), int(face), str(size))
    except (TypeError, ValueError):
        return None
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    try:
        url = _image_url(key[0], face=key[1], size=key[2])
    except Exception:  # noqa: BLE001 - a lookup must never break a page
        return None
    if url:
        _CACHE[key] = url
    return url


def warm_images() -> float:
    """Force the resolver's first (slow) load; returns the seconds it took.

    Call this once at server start so no reader waits on the bulk load.
    """
    started = time.perf_counter()
    image_url("Plains", size="small")
    return time.perf_counter() - started


__all__ = ["image_url", "warm_images"]
