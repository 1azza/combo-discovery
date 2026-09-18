"""The "omarchy" palette for the web console.

These are the *same* hex values the Textual console uses in
:mod:`combo_discovery.tui.theme`; the pair is asserted in
``tests/test_web.py`` so the two front-ends cannot drift. Defining them here as
plain literals keeps the web server free of a Textual import.
"""

from __future__ import annotations

# -- ground ------------------------------------------------------------------
BACKGROUND = "#141210"  # deep, slightly warm near-black
SURFACE = "#1a1815"  # raised surfaces / panels
PANEL = "#1f1c19"  # panel body
PANEL_HI = "#26221d"  # panel highlight / hover

# -- lines -------------------------------------------------------------------
BORDER = "#2f2a24"  # quiet warm border
BORDER_HI = "#3a4762"  # blue-tinted border (focus / active)

# -- single accent -----------------------------------------------------------
ACCENT = "#7aa2f7"  # omarchy blue
ACCENT_HI = "#9db8ff"
ACCENT_DIM = "#2e3b55"

# -- ink ---------------------------------------------------------------------
TEXT = "#ded8cc"  # warm off-white
MUTED = "#9a9384"  # secondary warm gray
FAINT = "#6b6459"  # tertiary / hints

# -- semantic (tiny indicators only) -----------------------------------------
OK = "#7fb08a"
WARN = "#d8a657"
ERR = "#e08a8a"

#: Palette name -> value, used for the inline ``:root`` custom properties.
OMARCHY = {
    "bg": BACKGROUND,
    "surface": SURFACE,
    "panel": PANEL,
    "panel-hi": PANEL_HI,
    "border": BORDER,
    "border-hi": BORDER_HI,
    "accent": ACCENT,
    "accent-hi": ACCENT_HI,
    "accent-dim": ACCENT_DIM,
    "text": TEXT,
    "muted": MUTED,
    "faint": FAINT,
    "ok": OK,
    "warn": WARN,
    "err": ERR,
}

#: The shared names checked against the TUI palette in the test-suite.
SHARED_NAMES = (
    "BACKGROUND",
    "SURFACE",
    "PANEL",
    "PANEL_HI",
    "BORDER",
    "BORDER_HI",
    "ACCENT",
    "ACCENT_HI",
    "ACCENT_DIM",
    "TEXT",
    "MUTED",
    "FAINT",
    "OK",
    "WARN",
    "ERR",
)


def root_variables() -> str:
    """The inline ``:root { --x: ... }`` block for the stylesheet."""
    return "".join(f"  --{name}: {value};\n" for name, value in OMARCHY.items())


__all__ = [
    "ACCENT",
    "ACCENT_DIM",
    "ACCENT_HI",
    "BACKGROUND",
    "BORDER",
    "BORDER_HI",
    "ERR",
    "FAINT",
    "MUTED",
    "OK",
    "OMARCHY",
    "PANEL",
    "PANEL_HI",
    "SHARED_NAMES",
    "SURFACE",
    "TEXT",
    "WARN",
    "root_variables",
]
