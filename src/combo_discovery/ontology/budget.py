"""Internal hard budgets for link building and cycle enumeration.

A previous broad-pool run never finished because there was no effective ceiling
on either the link-generation fan-out or the cycle walk.  External ``timeout``
was not a reliable stopping mechanism (a wrapper/orphan can outlive the shell),
so the ceiling must live *inside* the search: every long loop calls
:meth:`SearchBudget.tick`, which raises the truncation flag as soon as either the
wall-clock or the step budget is exhausted.  Callers then return the partial
result with a reason instead of hanging.

Two sibling mechanisms:

* :class:`SearchBudget` — the monotonic-clock + step-counter used by
  ``build_links`` / ``_enumerate_cycles``;
* :func:`preflight` — a *cost* check run before any work, refusing or clamping
  configurations that are known to be pathological (broad pools,
  ``closure_depth > 2``) unless the caller explicitly overrides.

Thresholds are deliberately conservative; they are the numbers measured in
``scripts/demo_algebra.py`` (see the round report).
"""

from __future__ import annotations

import time
import warnings
from typing import Any

#: Default wall-clock ceiling for one graph build / cycle search (seconds).
DEFAULT_MAX_SECONDS = 120.0
#: Default step ceiling (link-candidate / cycle-walk steps).
DEFAULT_MAX_STEPS = 2_000_000
#: Pools larger than this are refused unless ``allow_over_budget=True``.
MAX_SAFE_POOL = 8_000
#: ``closure_depth`` above this is clamped to 2 unless overridden.
MAX_SAFE_CLOSURE_DEPTH = 2


class BudgetExceeded(ValueError):
    """Raised by pre-flight for a configuration above the safe thresholds."""


class SearchBudget:
    """A wall-clock + step budget shared by one graph build and its search.

    ``max_seconds`` / ``max_steps`` may be ``None`` to disable that dimension,
    but the module-level defaults are always finite so no call is accidentally
    unbounded.  ``tick`` is cheap (a counter increment and a monotonic clock
    read); call it once per unit of real work.
    """

    __slots__ = ("max_seconds", "max_steps", "started", "steps", "truncated", "reason")

    def __init__(
        self,
        max_seconds: float | None = DEFAULT_MAX_SECONDS,
        max_steps: int | None = DEFAULT_MAX_STEPS,
    ) -> None:
        if max_seconds is not None and max_seconds <= 0:
            raise ValueError("max_seconds must be positive or None")
        if max_steps is not None and max_steps <= 0:
            raise ValueError("max_steps must be positive or None")
        self.max_seconds = max_seconds
        self.max_steps = max_steps
        self.started = time.monotonic()
        self.steps = 0
        self.truncated = False
        self.reason: str | None = None

    def tick(self, n: int = 1) -> bool:
        """Advance the budget; return ``False`` once it is exhausted."""
        self.steps += n
        if self.max_steps is not None and self.steps > self.max_steps:
            self._trip(f"max_steps={self.max_steps} exceeded ({self.steps} steps)")
            return False
        if self.max_seconds is not None and \
                time.monotonic() - self.started > self.max_seconds:
            self._trip(f"max_seconds={self.max_seconds} exceeded")
            return False
        return True

    def trip(self, reason: str) -> None:
        """Mark the budget exhausted with ``reason`` (idempotent)."""
        self._trip(reason)

    def _trip(self, reason: str) -> None:
        if not self.truncated:
            self.truncated = True
            self.reason = reason

    @property
    def expired(self) -> bool:
        return self.truncated

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def report(self) -> dict[str, Any]:
        return {
            "max_seconds": self.max_seconds,
            "max_steps": self.max_steps,
            "steps": self.steps,
            "elapsed_s": round(self.elapsed(), 3),
            "truncated": self.truncated,
            "truncation_reason": self.reason,
        }


def estimate_cost(pool_size: int, closure_depth: int) -> dict[str, int]:
    """Cheap pre-flight cost estimate (no work beyond arithmetic)."""
    return {
        "pool_size": int(pool_size),
        "pairwise_candidates": int(pool_size) * (int(pool_size) - 1) // 2,
        "closure_depth": int(closure_depth),
    }


def preflight(
    pool_size: int,
    closure_depth: int,
    *,
    max_pool: int = MAX_SAFE_POOL,
    max_closure_depth: int = MAX_SAFE_CLOSURE_DEPTH,
    allow_over_budget: bool = False,
) -> tuple[int, tuple[str, ...]]:
    """Refuse/clamp a pathological configuration; return ``(depth, warnings)``.

    * ``pool_size > max_pool`` raises :class:`BudgetExceeded` unless
      ``allow_over_budget`` is set (then a warning is emitted).
    * ``closure_depth > max_closure_depth`` is clamped to ``max_closure_depth``
      unless ``allow_over_budget`` is set (then warning only).
    """
    messages: list[str] = []
    depth = int(closure_depth)
    if pool_size > max_pool:
        message = (
            f"pool size {pool_size} exceeds the safe limit {max_pool} "
            f"(~{pool_size * (pool_size - 1) // 2:,} pairwise candidates); "
            "no safe exact search exists at this size"
        )
        if not allow_over_budget:
            raise BudgetExceeded(
                f"{message}; pass allow_over_budget=True to proceed under the "
                "internal budget"
            )
        messages.append(message + " (allowed by allow_over_budget)")
    if depth > max_closure_depth:
        message = (
            f"closure_depth {depth} exceeds the safe limit {max_closure_depth}"
        )
        if allow_over_budget:
            messages.append(message + " (allowed by allow_over_budget)")
        else:
            depth = max_closure_depth
            messages.append(message + f"; clamped to {depth}")
    for message in messages:
        warnings.warn(message, RuntimeWarning, stacklevel=3)
    return depth, tuple(messages)


__all__ = [
    "BudgetExceeded",
    "DEFAULT_MAX_SECONDS",
    "DEFAULT_MAX_STEPS",
    "MAX_SAFE_CLOSURE_DEPTH",
    "MAX_SAFE_POOL",
    "SearchBudget",
    "estimate_cost",
    "preflight",
]
