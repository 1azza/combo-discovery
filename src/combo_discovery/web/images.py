"""Adapter over the card-image helper owned by a parallel lane.

The real implementation lives in :mod:`combo_discovery.corpus.images` and may
not exist yet, so the import is guarded and every failure degrades to ``None``
(the Gallery then renders a typographic card tile instead of a broken image).
"""

from __future__ import annotations

from typing import Any

try:  # pragma: no cover - exercised when the parallel module is present
    from ..corpus.images import image_url as _image_url
except Exception:  # noqa: BLE001 - the module arrives in a parallel lane
    _image_url = None


def image_url(card_name: Any, face: int = 0, size: str = "normal") -> str | None:
    """Return a Scryfall image URL, or ``None`` when there is no usable art."""
    if _image_url is None or not card_name:
        return None
    try:
        return _image_url(card_name, face=face, size=size)
    except Exception:  # noqa: BLE001 - a lookup must never break a page
        return None


__all__ = ["image_url"]
