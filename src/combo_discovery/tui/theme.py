"""The "omarchy" palette and Textual theme for the research console.

One restrained accent (a soft omarchy blue) on a deep, slightly warm near-black
ground, with warm muted grays for secondary text. A handful of desaturated
status colors (amber / red / green) appear only as tiny semantic indicators in
the header, statusline and worker health — never as decoration.
"""

from __future__ import annotations

from textual.theme import Theme

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

OMARCHY_THEME = Theme(
    name="omarchy",
    primary=ACCENT,
    secondary=ACCENT_HI,
    accent=ACCENT,
    foreground=TEXT,
    background=BACKGROUND,
    surface=SURFACE,
    panel=PANEL,
    warning=WARN,
    error=ERR,
    success=OK,
    dark=True,
    variables={
        "border": BORDER,
        "border-blurred": BORDER,
        "panel": PANEL,
        "omarchy-border": BORDER,
        "omarchy-border-hi": BORDER_HI,
        "omarchy-panel-hi": PANEL_HI,
        "omarchy-muted": MUTED,
        "omarchy-faint": FAINT,
        "omarchy-accent": ACCENT,
        "omarchy-accent-hi": ACCENT_HI,
        "omarchy-accent-dim": ACCENT_DIM,
        "omarchy-ok": OK,
        "omarchy-warn": WARN,
        "omarchy-err": ERR,
        "scrollbar": ACCENT_DIM,
        "scrollbar-hover": ACCENT,
        "scrollbar-active": ACCENT_HI,
        "scrollbar-background": SURFACE,
        # Keep the focused active tab readable (Tabs uses the block cursor
        # colors when it has keyboard focus).
        "block-cursor-background": ACCENT_DIM,
        "block-cursor-foreground": ACCENT_HI,
        "block-cursor-text-style": "bold",
    },
)
