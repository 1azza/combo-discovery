"""Cycle construction and combo discovery over the interaction algebra.

* :func:`build_graph` turns ability signatures into a card-level graph whose
  edges are ability :class:`~combo_discovery.ontology.links.Link` instances;
* :func:`find_combos` enumerates re-entrant cycles, validates resource closure
  and gates, names them with the cycle queries, and scores them with the
  ground-truth motif enrichment.

**Why card-level cycles.**  A combo is a set of cards, but the interactions are
ability-scoped.  Nodes are cards; edges are ability links.  The graph search is
anchored on ``re_trigger`` links (rare) and is bounded by ``max_len`` cards, so
the (large) ``enables`` fan-out only has to be explored around those anchors.

**Validity rules** (documented, conservative):

1. *re-entrant* — the cycle contains at least one ``re_trigger`` link (a fresh
   token/phase/reanimation fires an ability again);
2. *resource-closed* — every ``consumes`` port of every ability in the cycle is
   covered by some ``produces`` port in the cycle (type-compatibly);
3. *gates conservatively satisfiable* — a gate is discharged by a **different
   card's** ability in the cycle (a card cannot bootstrap its own gate).  A gate
   that is not discharged is carried as a precondition, never a rejection.
4. *net resources non-decreasing* — the cycle contains a repeatable producer
   (mana/token/damage/extra_phase/counters/life) and no uncovered external
   requirement, so it is flagged ``infinite``.  This is deliberately a simple
   model: it does **not** track quantities, timing, stack order, summoning
   sickness or interaction between the produced resources.

**Scoring.**  ``score = base(query) × Π enrichment_weight(link motif)`` where the
motifs are the same cross-card tokens the enrichment analysis stores
(``COPIES_CREATURE~ETB_TRIGGER``, ...).  Weights come from the latest
``motif_enrichment`` run when available (lift clipped to ``[0.1, 2.0]``), else
1.0.  Any link whose motif weight is below :data:`HOSTILE_LIFT` suppresses the
cycle.  The product is clamped to 1.0.  The whole scheme is transparent and
exposed via :func:`load_motif_weights`.
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .links import (
    HOSTILE_LIFT,
    Link,
    LinkOptions,
    _port_satisfies_gate,
    build_links,
    link_hostile,
    link_weight,
    ports_enable,
)
from .ports import AbilitySig
from .queries import ComboContext, Query, matching_queries


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------


@dataclass
class Graph:
    """A card-level interaction graph over ability signatures."""

    sigs: tuple[AbilitySig, ...]
    links: tuple[Link, ...]
    by_card: dict[int, tuple[AbilitySig, ...]]
    names: dict[int, str]
    adjacency: dict[int, tuple[Link, ...]]
    re_triggers: tuple[Link, ...]

    @property
    def nodes(self) -> tuple[int, ...]:
        return tuple(sorted(self.by_card))


def build_graph(
    sigs: Sequence[AbilitySig],
    *,
    weights: dict[str, float] | None = None,
    pool: Iterable[int] | None = None,
    options: LinkOptions | None = None,
) -> Graph:
    """Build the card-level graph.  Deterministic; ``pool`` scopes card ids."""
    selected = list(sigs)
    if pool is not None:
        allowed = set(pool)
        selected = [s for s in selected if s.card_id in allowed]
    options = options or LinkOptions()
    links = build_links(selected, weights=weights, options=options)

    by_card: dict[int, list[AbilitySig]] = defaultdict(list)
    names: dict[int, str] = {}
    for sig in selected:
        by_card[sig.card_id].append(sig)
        names.setdefault(sig.card_id, sig.card_name)
    adjacency: dict[int, list[Link]] = defaultdict(list)
    for link in links:
        adjacency[link.src.card_id].append(link)
    re_triggers = tuple(link for link in links if link.kind == "re_trigger")
    return Graph(
        sigs=tuple(selected),
        links=tuple(links),
        by_card={cid: tuple(sorted(v, key=lambda s: s.ability_ref))
                 for cid, v in by_card.items()},
        names=names,
        adjacency={cid: tuple(sorted(v, key=lambda l: (
            l.dst.card_id, l.kind, l.subkind, l.motif)))
            for cid, v in adjacency.items()},
        re_triggers=re_triggers,
    )


# ---------------------------------------------------------------------------
# Combo
# ---------------------------------------------------------------------------


@dataclass
class Combo:
    """A validated interaction cycle."""

    card_ids: tuple[int, ...]
    cards: tuple[str, ...]
    abilities: tuple[AbilitySig, ...]
    links: tuple[Link, ...]
    score: float
    mechanism: str
    preconditions: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    infinite: bool = False
    motifs: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "cards": list(self.cards),
            "card_ids": list(self.card_ids),
            "score": self.score,
            "patterns": list(self.patterns),
            "preconditions": list(self.preconditions),
            "infinite": self.infinite,
            "motifs": list(self.motifs),
            "mechanism": self.mechanism,
            "abilities": [a.ability_ref for a in self.abilities],
            "links": [
                {"kind": l.kind, "subkind": l.subkind, "motif": l.motif,
                 "src": l.src.card_name, "dst": l.dst.card_name}
                for l in self.links
            ],
        }

    def format(self) -> str:
        tags = ", ".join(self.patterns)
        pre = f"  preconditions: {', '.join(self.preconditions)}" if self.preconditions else ""
        return (
            f"[{tags}] score={self.score:.3f} infinite={self.infinite}  "
            f"{' + '.join(self.cards)}\n    {self.mechanism}" + pre
        )


# ---------------------------------------------------------------------------
# Cycle enumeration
# ---------------------------------------------------------------------------


def _enumerate_cycles(
    graph: Graph, max_len: int, max_cycles: int = 20_000
) -> list[tuple[tuple[int, ...], tuple[Link, ...]]]:
    """Re-entrant cycles as ``(ordered card ids, ordered links)``.

    Anchored on every ``re_trigger`` link ``u -> v``; a bounded DFS walks from
    ``v`` back to ``u`` using at most ``max_len`` distinct cards.  Deterministic
    and capped by ``max_cycles``.
    """
    adjacency = graph.adjacency
    cycles: list[tuple[tuple[int, ...], tuple[Link, ...]]] = []
    seen: set[frozenset[int]] = set()
    for anchor in graph.re_triggers:
        u, v = anchor.pair
        if u == v:
            continue
        stack: list[tuple[int, list[Link], frozenset[int]]] = [
            (v, [anchor], frozenset({u, v}))
        ]
        while stack:
            node, path, visited = stack.pop()
            for link in adjacency.get(node, ()):
                nxt = link.dst.card_id
                if nxt == u:
                    cards = tuple(dict.fromkeys(
                        [path[0].src.card_id]
                        + [p.dst.card_id for p in path]
                    ))
                    key = frozenset(cards)
                    if len(key) < 2 or key in seen:
                        continue
                    seen.add(key)
                    cycles.append((cards, tuple(path + [link])))
                    if len(cycles) >= max_cycles:
                        return cycles
                elif nxt not in visited and len(visited) < max_len:
                    stack.append((nxt, path + [link], visited | {nxt}))
    return cycles


# ---------------------------------------------------------------------------
# Validation + scoring
# ---------------------------------------------------------------------------


def _resource_closed(abilities: Sequence[AbilitySig]) -> bool:
    for consumer in abilities:
        for consumed in consumer.consumes:
            if not any(
                ports_enable(produced, consumed, consumer)
                for producer in abilities
                for produced in producer.produces
            ):
                return False
    return True


def _gate_preconditions(abilities: Sequence[AbilitySig]) -> tuple[str, ...]:
    """Gates not discharged by a *different* card in the cycle."""
    preconditions: list[str] = []
    for holder in abilities:
        for gate in holder.gates:
            discharged = any(
                other.card_id != holder.card_id
                and any(_port_satisfies_gate(produced, gate, holder)
                        for produced in other.produces)
                for other in abilities
            )
            if not discharged:
                preconditions.append(f"{holder.card_name}:{gate.kind}")
    return tuple(dict.fromkeys(preconditions))


def _motif_score(links: Sequence[Link], weights: Mapping[str, float] | None) -> float:
    score = 1.0
    for link in links:
        score *= link_weight(link, dict(weights) if weights else None)
    return score


def _mechanism(cards: Sequence[int], links: Sequence[Link], graph: Graph) -> str:
    names = graph.names
    parts: list[str] = []
    for link in links:
        src = names.get(link.src.card_id, link.src.card_name)
        dst = names.get(link.dst.card_id, link.dst.card_name)
        if link.kind == "re_trigger":
            verb = {
                "copy": "copies",
                "combat": "adds an extra combat for",
                "reanimate": "reanimates",
                "flicker": "flickers",
            }.get(link.subkind, "re-triggers")
            parts.append(f"{src} {verb} {dst} ({link.motif})")
        elif link.kind == "enables":
            parts.append(f"{src} feeds {dst} ({link.motif})")
        elif link.kind == "satisfies":
            parts.append(f"{src} satisfies {dst} ({link.motif})")
        else:
            parts.append(f"{src} -> {dst} ({link.motif})")
    return "; ".join(parts) + "."


def _make_combo(
    ordered_cards: Sequence[int],
    ordered_links: Sequence[Link],
    graph: Graph,
    weights: dict[str, float] | None,
) -> Combo | None:
    if len(set(ordered_cards)) < 2:
        return None
    abilities: list[AbilitySig] = []
    seen_abilities: set[tuple[int, str]] = set()
    for link in ordered_links:
        for sig in (link.src, link.dst):
            if sig.key not in seen_abilities:
                seen_abilities.add(sig.key)
                abilities.append(sig)
    if not any(link.kind == "re_trigger" for link in ordered_links):
        return None
    if not _resource_closed(abilities):
        return None

    # Hostile motifs suppress the cycle.
    for left in abilities:
        for right in abilities:
            if left.key == right.key:
                continue
            if link_hostile(left, right, weights) is not None:
                return None

    context = ComboContext(abilities=tuple(abilities), links=tuple(ordered_links))
    matched: tuple[Query, ...] = matching_queries(context)
    if not matched:
        return None
    base = max(query.base_score for query in matched)
    weight_product = _motif_score(ordered_links, weights)
    score = min(1.0, base * weight_product)

    preconditions = _gate_preconditions(abilities)
    infinite = any(
        p.kind in ("mana", "token", "damage", "extra_phase", "counters",
                   "life_gain", "copy_permanent")
        for ability in abilities for p in ability.produces
    )
    motifs = tuple(dict.fromkeys(
        link.motif for link in ordered_links if link.motif
    ))
    return Combo(
        card_ids=tuple(ordered_cards),
        cards=tuple(graph.names.get(cid, str(cid)) for cid in ordered_cards),
        abilities=tuple(abilities),
        links=tuple(ordered_links),
        score=round(score, 6),
        mechanism=_mechanism(ordered_cards, ordered_links, graph),
        preconditions=preconditions,
        patterns=tuple(query.name for query in matched),
        infinite=infinite,
        motifs=motifs,
    )


def find_combos(
    sigs: Sequence[AbilitySig],
    *,
    weights: dict[str, float] | None = None,
    max_len: int = 4,
    min_score: float = 0.0,
    pool: Iterable[int] | None = None,
    options: LinkOptions | None = None,
    graph: Graph | None = None,
    max_cycles: int = 20_000,
) -> list[Combo]:
    """Enumerate, validate and score re-entrant cycles.

    ``max_len`` is the maximum number of distinct cards in a cycle.  Results are
    deduplicated by card set (best score wins) and sorted by descending score.
    """
    graph = graph or build_graph(sigs, weights=weights, pool=pool, options=options)
    best: dict[frozenset[int], Combo] = {}
    for cards, links in _enumerate_cycles(graph, max_len, max_cycles=max_cycles):
        combo = _make_combo(cards, links, graph, weights)
        if combo is None or combo.score < min_score:
            continue
        key = frozenset(combo.card_ids)
        current = best.get(key)
        if current is None or combo.score > current.score:
            best[key] = combo
    return sorted(best.values(), key=lambda c: (-c.score, c.cards))


# ---------------------------------------------------------------------------
# Enrichment weights
# ---------------------------------------------------------------------------


def lift_to_weight(lift: float | None) -> float:
    """Clip a motif lift into a bounded multiplicative weight."""
    if lift is None:
        return 1.0
    try:
        value = float(lift)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(value):
        return 2.0
    return min(2.0, max(0.1, value))


def load_motif_weights(
    conn: sqlite3.Connection, *, run_id: int | None = None
) -> dict[str, float]:
    """Load ``motif -> weight`` from the latest (or given) enrichment run.

    Lifts are clipped by :func:`lift_to_weight`; a missing table/run returns an
    empty dict so callers fall back to neutral weights.
    """
    try:
        if run_id is None:
            row = conn.execute("SELECT MAX(id) AS run_id FROM motif_runs").fetchone()
            run_id = row["run_id"] if row is not None else None
        if run_id is None:
            return {}
        rows = conn.execute(
            "SELECT motif, lift FROM motif_enrichment WHERE run_id = ?", (run_id,)
        ).fetchall()
    except sqlite3.Error:
        return {}
    weights: dict[str, float] = {}
    for row in rows:
        try:
            motif = row["motif"]
            lift = row["lift"]
        except (KeyError, TypeError):
            motif, lift = row[0], row[1]
        if motif:
            weights[str(motif)] = lift_to_weight(lift)
    return weights


def vintage_pool(
    card_ids: Iterable[int],
    names: Mapping[int, str],
    legality,
) -> tuple[int, ...]:
    """Vintage-legal subset of ``card_ids`` (uses the forged-format helper)."""
    return tuple(
        sorted(cid for cid in card_ids
               if legality is None or legality.is_legal(names.get(cid)))
    )


__all__ = [
    "Combo",
    "Graph",
    "build_graph",
    "find_combos",
    "lift_to_weight",
    "load_motif_weights",
    "vintage_pool",
]
