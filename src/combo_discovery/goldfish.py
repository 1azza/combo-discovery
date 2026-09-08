"""Goldfish policy: a deterministic placeholder player for combo validation.

Operates purely on the Option metadata the Java harness populates (kind /
description). Preference order: play_card (lands first, then cheapest mana
cost found in the description), activate, pass. Ties break by option id.
"""

from __future__ import annotations

import re

from .generated import forge_env_pb2 as pb


def _mana_cost(description: str) -> int:
    m = re.search(r"[Cc]ost[:\s$]*([0-9]+)", description)
    return int(m.group(1)) if m else 99


class GoldfishPolicy:
    def decide(self, request: pb.DecisionRequest) -> int:
        if not request.options:
            raise ValueError("no options available")
        plays = [o for o in request.options if o.kind == "play_card"]
        if plays:
            lands = [o for o in plays if "land" in o.description.lower()]
            pool = lands or sorted(plays, key=lambda o: (_mana_cost(o.description), o.id))
            return pool[0].id
        activations = [o for o in request.options if o.kind == "activate"]
        if activations:
            return sorted(activations, key=lambda o: o.id)[0].id
        passes = [o for o in request.options if "pass" in o.kind.lower()]
        if passes:
            return sorted(passes, key=lambda o: o.id)[0].id
        return sorted(request.options, key=lambda o: o.id)[-1].id
