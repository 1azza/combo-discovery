"""Textual research console for combo-discovery.

Entry points::

    uv run combo-tui                 # installed script entry
    uv run python -m combo_discovery.tui
"""

from .app import ComboDiscoveryApp, main

__all__ = ["ComboDiscoveryApp", "main"]
