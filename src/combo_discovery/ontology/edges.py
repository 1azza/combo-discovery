"""Pattern queries over card predicates -> interaction edges + hypotheses.

Each generator below encodes one v1 combo pattern.  A pattern contributes
:class:`Edge` values (an oriented ``source -> target`` interaction with a
mechanism string and a transparent score) which the builder persists both as
``interactions`` rows and as two-card ``combo_hypotheses``.

Scoring weights (documented, additive, clamped to 1.0):

* ``base`` per pattern (see :data:`PATTERN_BASE`);
* ``+0.04`` per contributing predicate pair, capped at ``+0.16``;
* ``+0.05`` when the two cards share a colour identity;
* ``+0.03`` when the more expensive card costs <= 3 mana;
* ``+0.02`` when both cards cost <= 2 mana.

These are heuristics for *ranking*; recall is decided by the predicate rules,
not the weights.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from . import vocabulary as vocab
from .extractor import CardContext, CardEffect, CardPredicate


@dataclass(frozen=True)
class PatternDef:
    """A named, versioned matching rule (persisted in the ``patterns`` table)."""

    name: str
    description: str
    rule: dict[str, Any]
    version: int = 1


#: The v1 pattern vocabulary.  ``rule`` is stored as JSON for provenance.
PATTERNS: tuple[PatternDef, ...] = (
    PatternDef(
        "infinite_etb_loop",
        "Tap-cost engine plus an enters-the-battlefield untapper.",
        {
            "engine": ["TAPS_COST", "& one of COPIES_CREATURE/COPIES_SPELL/"
                       "DEALS_DAMAGE/LOSES_LIFE/DESTROYS/MILLS/DISCARDS/"
                       "GAINS_CONTROL/ADDS_COUNTERS"],
            "partner": ["UNTAPS", "ETB_TRIGGER"],
            "direction": "mutual",
        },
    ),
    PatternDef(
        "sacrifice_recursion",
        "Sacrifice outlet plus graveyard recursion.",
        {"outlet": ["SACRIFICE_OUTLET"], "recursion": ["RECURS_FROM_GRAVEYARD"],
         "bonus": ["DIES_TRIGGER"], "direction": "mutual"},
    ),
    PatternDef(
        "mana_engine",
        "ETB/tap mana producer plus an X-cost or artifact-mana spender.",
        {"producer": ["PRODUCES_MANA", "& (ETB_TRIGGER | TAPS_COST)"],
         "spender": ["mana_cost contains X | artifact with PRODUCES_MANA"],
         "direction": "one_way", "confidence": "low"},
    ),
    PatternDef(
        "free_cast_loops",
        "Cast-from-graveyard engine plus a free mana/cost enabler.",
        {"freecaster": ["CASTS_FROM_GRAVEYARD"],
         "enabler": ["(PRODUCES_MANA & SACRIFICES_SELF) | ALTERNATIVE_COST"],
         "direction": "one_way"},
    ),
    PatternDef(
        "storm_engine",
        "Storm payoff plus a cheap-spell/mana enabler.",
        {"payoff": ["STORM"],
         "enabler": ["PRODUCES_MANA | ALTERNATIVE_COST | UNTAPS | STORM(excl. self)"],
         "direction": "one_way"},
    ),
    PatternDef(
        "color_lock_mill",
        "Sets all cards to a colour plus a repeat-mill engine.",
        {"lock": ["SETS_COLOR"], "mill": ["MILLS"], "direction": "mutual"},
    ),
    PatternDef(
        "draw_engine",
        "Converts life into cards plus a draw payoff.",
        {"producer": ["CARDS_FROM_LIFE"], "payoff": ["DRAWS"], "direction": "one_way"},
    ),
)

PATTERN_BASE: dict[str, float] = {
    "infinite_etb_loop": 0.60,
    "color_lock_mill": 0.72,
    "free_cast_loops": 0.65,
    "sacrifice_recursion": 0.60,
    "storm_engine": 0.55,
    "draw_engine": 0.45,
    "mana_engine": 0.30,
}

#: Predicates that make a tap-cost ability an "impactful" engine.
_IMPACT_PREDICATES: tuple[str, ...] = (
    vocab.COPIES_CREATURE,
    vocab.COPIES_SPELL,
    vocab.DEALS_DAMAGE,
    vocab.LOSES_LIFE,
    vocab.DESTROYS,
    vocab.MILLS,
    vocab.DISCARDS,
    vocab.GAINS_CONTROL,
    vocab.ADDS_COUNTERS,
)


@dataclass
class CardView:
    """A card's metadata plus its extracted predicates and effect verbs."""

    context: CardContext
    predicates: list[CardPredicate] = field(default_factory=list)
    verbs: frozenset[str] = frozenset()
    _by_pred: dict[str, CardPredicate] = field(default_factory=dict, repr=False)

    @classmethod
    def build(
        cls,
        context: CardContext,
        predicates: list[CardPredicate],
        effects: Iterable[CardEffect] = (),
    ) -> "CardView":
        by_pred: dict[str, CardPredicate] = {}
        for predicate in predicates:
            by_pred.setdefault(predicate.predicate, predicate)
        return cls(
            context=context,
            predicates=list(predicates),
            verbs=frozenset(e.verb for e in effects),
            _by_pred=by_pred,
        )

    @property
    def card_id(self) -> int:
        return self.context.card_id

    @property
    def name(self) -> str:
        return self.context.name

    def has(self, predicate: str) -> bool:
        return predicate in self._by_pred

    def first(self, predicate: str) -> CardPredicate | None:
        return self._by_pred.get(predicate)

    def any_of(self, predicates: Iterable[str]) -> str | None:
        for predicate in predicates:
            if predicate in self._by_pred:
                return predicate
        return None


@dataclass
class Edge:
    """One oriented interaction between two cards for a pattern."""

    pattern: str
    source: CardView
    target: CardView
    mechanism: str
    score: float
    evidence: list[dict[str, Any]] = field(default_factory=list)
    direction: str = "one_way"

    @property
    def pair_key(self) -> tuple[str, frozenset[int]]:
        return (self.pattern, frozenset((self.source.card_id, self.target.card_id)))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_edge(pattern: str, source: CardView, target: CardView, evidence_count: int) -> float:
    score = PATTERN_BASE.get(pattern, 0.2)
    score += 0.04 * min(4, evidence_count)
    if source.context.color_identity & target.context.color_identity:
        score += 0.05
    if max(source.context.cmc, target.context.cmc) <= 3:
        score += 0.03
    if source.context.cmc <= 2 and target.context.cmc <= 2:
        score += 0.02
    return round(min(score, 1.0), 4)


def _evidence(view: CardView, predicate: str) -> dict[str, Any]:
    found = view.first(predicate)
    if found is None:
        return {"card_id": view.card_id, "name": view.name, "predicate": predicate}
    return {
        "card_id": view.card_id,
        "name": view.name,
        "predicate": predicate,
        "params": found.params,
        "lines": [entry.get("line") for entry in found.evidence if entry.get("line")],
    }


def _make_edge(
    pattern: str,
    source: CardView,
    target: CardView,
    mechanism: str,
    evidence: list[dict[str, Any]],
    *,
    direction: str = "one_way",
    score_bonus: float = 0.0,
) -> Edge:
    return Edge(
        pattern=pattern,
        source=source,
        target=target,
        mechanism=mechanism,
        score=min(1.0, round(score_edge(pattern, source, target, len(evidence)) + score_bonus, 4)),
        evidence=evidence,
        direction=direction,
    )


# ---------------------------------------------------------------------------
# Pattern generators
# ---------------------------------------------------------------------------


def infinite_etb_loop(views: dict[int, CardView]) -> Iterator[Edge]:
    engines = [v for v in views.values() if v.has(vocab.TAPS_COST)
               and v.any_of(_IMPACT_PREDICATES)]
    untappers = [v for v in views.values() if v.has(vocab.UNTAPS) and v.has(vocab.ETB_TRIGGER)]
    for engine in engines:
        impact = engine.any_of(_IMPACT_PREDICATES)
        tap = engine.first(vocab.TAPS_COST)
        tap_verb = (tap.params.get("verb") if tap is not None else "") or ""
        for partner in untappers:
            if engine.card_id == partner.card_id or impact is None:
                continue
            mechanism = (
                f"{engine.name} has a tap-cost {tap_verb} engine "
                f"({impact.replace('_', ' ').lower()}); {partner.name}'s "
                f"enters-the-battlefield trigger untaps a permanent, untapping "
                f"{engine.name} to repeat the loop."
            )
            # Canonical "copy a creature, then untap it" loops rank above the
            # broader tap-cost-engine synergies; a creature untapper (the real
            # Kiki/Exarch shape) edges out an aura/land untapper.
            copy_engine = impact == vocab.COPIES_CREATURE
            bonus = 0.10 if copy_engine else 0.0
            if copy_engine and partner.context.is_creature:
                bonus += 0.05
            yield _make_edge(
                "infinite_etb_loop", engine, partner, mechanism,
                [_evidence(engine, vocab.TAPS_COST), _evidence(engine, impact),
                 _evidence(partner, vocab.UNTAPS), _evidence(partner, vocab.ETB_TRIGGER)],
                direction="mutual",
                score_bonus=bonus,
            )


def _mentions_artifact(view: CardView) -> bool:
    """True when a card is an artifact or its script talks about artifacts.

    Sacrifice/recursion is otherwise a quadratic free-for-all; the v1 pattern is
    scoped to the artifact-recursion archetype (Goblin Welder, Myr Retriever,
    Scrap Trawler), which is what the known-combo set exercises.
    """
    if view.context.is_artifact:
        return True
    if "artifact" in (view.context.oracle_text or "").lower():
        return True
    for predicate in view.predicates:
        for value in predicate.params.values():
            if isinstance(value, str) and "artifact" in value.lower():
                return True
    return False


def sacrifice_recursion(views: dict[int, CardView]) -> Iterator[Edge]:
    outlets = [v for v in views.values() if v.has(vocab.SACRIFICE_OUTLET)]
    recursion = [v for v in views.values() if v.has(vocab.RECURS_FROM_GRAVEYARD)]
    for outlet in outlets:
        if not _mentions_artifact(outlet):
            continue
        for recur in recursion:
            if outlet.card_id == recur.card_id or not _mentions_artifact(recur):
                continue
            mutual = recur.has(vocab.SACRIFICE_OUTLET) or outlet.has(vocab.RECURS_FROM_GRAVEYARD)
            mechanism = (
                f"{outlet.name} sacrifices artifacts; {recur.name} returns "
                f"artifact cards from the graveyard, rebuilding what was sacrificed"
                + (" (recursion runs both ways)." if mutual else ".")
            )
            yield _make_edge(
                "sacrifice_recursion", outlet, recur, mechanism,
                [_evidence(outlet, vocab.SACRIFICE_OUTLET),
                 _evidence(recur, vocab.RECURS_FROM_GRAVEYARD)],
                direction="mutual" if mutual else "one_way",
            )


def _strong_producer(view: CardView) -> bool:
    """A mana source worth building around (ritual, rock, or Treasure)."""
    predicate = view.first(vocab.PRODUCES_MANA)
    if predicate is None:
        return False
    params = predicate.params
    if params.get("x") or params.get("source") == "treasure":
        return True
    amount = str(params.get("amount") or "")
    return amount.isdigit() and int(amount) >= 2


def _is_spender(view: CardView) -> bool:
    return view.context.has_x_cost


def mana_engine(views: dict[int, CardView]) -> Iterator[Edge]:
    producers = [v for v in views.values()
                 if _strong_producer(v) and (v.has(vocab.ETB_TRIGGER) or v.has(vocab.TAPS_COST))]
    x_spenders = [v for v in views.values() if _is_spender(v)]
    # An X-scaling producer also pairs with artifact mana (Tolarian Academy).
    artifact_mana = [v for v in views.values()
                     if v.context.is_artifact and v.has(vocab.PRODUCES_MANA)]
    for producer in producers:
        produced = producer.first(vocab.PRODUCES_MANA)
        targets = x_spenders
        if produced is not None and produced.params.get("x"):
            targets = x_spenders + artifact_mana
        for spender in targets:
            if spender.card_id == producer.card_id:
                continue
            amount = (produced.params.get("amount") if produced else None) or ""
            mechanism = (
                f"{producer.name} produces mana"
                + (f" ({amount})" if amount else "")
                + f"; {spender.name} converts that mana into value (X-cost / artifact mana scaling)."
            )
            yield _make_edge(
                "mana_engine", producer, spender, mechanism,
                [_evidence(producer, vocab.PRODUCES_MANA)],
            )


def free_cast_loops(views: dict[int, CardView]) -> Iterator[Edge]:
    freecasters = [v for v in views.values() if v.has(vocab.CASTS_FROM_GRAVEYARD)]
    enablers = [
        v for v in views.values()
        if (v.has(vocab.PRODUCES_MANA) and v.has(vocab.SACRIFICES_SELF))
        or v.has(vocab.ALTERNATIVE_COST)
    ]
    for caster in freecasters:
        for enabler in enablers:
            if caster.card_id == enabler.card_id:
                continue
            mechanism = (
                f"{caster.name} lets you cast cards from your graveyard; "
                f"{enabler.name} provides free mana or an alternative cost, "
                f"enabling repeated casts."
            )
            evidence = [_evidence(caster, vocab.CASTS_FROM_GRAVEYARD),
                        _evidence(enabler, vocab.PRODUCES_MANA)]
            yield _make_edge("free_cast_loops", caster, enabler, mechanism, evidence)


def _cheap_enabler(view: CardView) -> bool:
    """A mana source / untapper strong enough to chain spells in one turn."""
    produced = view.first(vocab.PRODUCES_MANA)
    if produced is not None:
        params = produced.params
        if params.get("x") or params.get("source") == "treasure":
            return True
        amount = str(params.get("amount") or "")
        if amount.isdigit() and int(amount) >= 2:
            return True
        if view.has(vocab.SACRIFICES_SELF):
            return True
    if view.has(vocab.ALTERNATIVE_COST):
        return True
    untap = view.first(vocab.UNTAPS)
    if untap is not None:
        amount = str(untap.params.get("amount") or "")
        if amount.isdigit() and int(amount) >= 3:
            return True
    return False


def storm_engine(views: dict[int, CardView]) -> Iterator[Edge]:
    payoffs = [v for v in views.values() if v.has(vocab.STORM)]
    enablers = [
        v for v in views.values()
        if _cheap_enabler(v) or (v.has(vocab.STORM) and True)
    ]
    for payoff in payoffs:
        for enabler in enablers:
            if payoff.card_id == enabler.card_id:
                continue
            kind = "a cheap mana source" if enabler.has(vocab.PRODUCES_MANA) else (
                "cost reduction" if enabler.has(vocab.ALTERNATIVE_COST) else (
                    "an untap engine" if enabler.has(vocab.UNTAPS) else "a storm engine"))
            mechanism = (
                f"{payoff.name} carries Storm; {enabler.name} is {kind}, "
                f"letting you chain spells before the storm payoff resolves."
            )
            yield _make_edge(
                "storm_engine", payoff, enabler, mechanism,
                [_evidence(payoff, vocab.STORM), _evidence(enabler, vocab.PRODUCES_MANA)],
            )


def color_lock_mill(views: dict[int, CardView]) -> Iterator[Edge]:
    locks = [v for v in views.values() if v.has(vocab.SETS_COLOR)]
    mills = [v for v in views.values() if v.has(vocab.MILLS)]
    for lock in locks:
        for mill in mills:
            if lock.card_id == mill.card_id:
                continue
            mechanism = (
                f"{lock.name} makes every card a single colour; {mill.name} "
                f"mills and repeats when two milled cards share a colour, so the "
                f"colour lock makes the mill self-sustaining."
            )
            yield _make_edge(
                "color_lock_mill", lock, mill, mechanism,
                [_evidence(lock, vocab.SETS_COLOR), _evidence(mill, vocab.MILLS)],
                direction="mutual",
            )


def draw_engine(views: dict[int, CardView]) -> Iterator[Edge]:
    producers = [v for v in views.values() if v.has(vocab.CARDS_FROM_LIFE)]
    payoffs = [v for v in views.values() if v.has(vocab.DRAWS)]
    for producer in producers:
        for payoff in payoffs:
            if producer.card_id == payoff.card_id:
                continue
            mechanism = (
                f"{producer.name} converts life into cards; {payoff.name} "
                f"turns those extra cards into value."
            )
            yield _make_edge(
                "draw_engine", producer, payoff, mechanism,
                [_evidence(producer, vocab.CARDS_FROM_LIFE), _evidence(payoff, vocab.DRAWS)],
            )


_GENERATORS: dict[str, Callable[[dict[int, CardView]], Iterator[Edge]]] = {
    "infinite_etb_loop": infinite_etb_loop,
    "sacrifice_recursion": sacrifice_recursion,
    "mana_engine": mana_engine,
    "free_cast_loops": free_cast_loops,
    "storm_engine": storm_engine,
    "color_lock_mill": color_lock_mill,
    "draw_engine": draw_engine,
}


# ---------------------------------------------------------------------------
# Pair generation with caps
# ---------------------------------------------------------------------------


#: Per-pattern ``(max_per_source, max_per_pattern)`` bounds, applied only when
#: the caller opts in (``build_edges(..., apply_caps=True)`` / the CLI
#: ``--caps``).  The structural filters already bring the full 33k-card graph to
#: ~0.5M edges, so the default build keeps every pair; these bounds exist for
#: larger corpora or latency-sensitive serving.
DEFAULT_CAPS: dict[str, tuple[int | None, int | None]] = {
    "infinite_etb_loop": (80, 20_000),
    "sacrifice_recursion": (80, 20_000),
    "mana_engine": (60, 20_000),
    "free_cast_loops": (80, 20_000),
    "storm_engine": (80, 20_000),
    "color_lock_mill": (80, 20_000),
    "draw_engine": (80, 20_000),
}


def _cap_per_source_iter(iterable: Iterable[Edge], cap: int | None) -> Iterator[Edge]:
    """Bound each source card to its ``cap`` best partners.

    Generators emit contiguous runs per source (the outer loop is the source),
    so this keeps only a single run in memory and never materialises the full
    product.
    """
    if cap is None:
        yield from iterable
        return
    buffer: list[Edge] = []
    current: tuple[str, int] | None = None
    for edge in iterable:
        key = (edge.pattern, edge.source.card_id)
        if current is not None and key != current:
            buffer.sort(key=lambda e: (-e.score, e.target.name))
            yield from buffer[:cap]
            buffer = []
        current = key
        buffer.append(edge)
    if buffer:
        buffer.sort(key=lambda e: (-e.score, e.target.name))
        yield from buffer[:cap]


def build_edges(
    views: dict[int, CardView],
    *,
    apply_caps: bool = False,
    max_per_source: int | None = None,
    max_per_pattern: int | None = None,
) -> list[Edge]:
    """Run every pattern generator and deduplicate by unordered card pair.

    ``apply_caps`` enables the per-pattern :data:`DEFAULT_CAPS` bounds (off by
    default, so the small validation corpus and the full build see every pair).
    ``max_per_source`` / ``max_per_pattern`` override the per-pattern values
    when supplied.
    """
    all_edges: list[Edge] = []
    for name, generator in _GENERATORS.items():
        source_cap, pattern_cap = DEFAULT_CAPS.get(name, (40, 20_000)) if apply_caps else (None, None)
        if max_per_source is not None:
            source_cap = max_per_source
        if max_per_pattern is not None:
            pattern_cap = max_per_pattern
        best: dict[tuple[str, frozenset[int]], Edge] = {}
        for edge in _cap_per_source_iter(generator(views), source_cap):
            key = edge.pair_key
            current = best.get(key)
            if current is None or edge.score > current.score:
                best[key] = edge
        items = sorted(best.values(), key=lambda e: (-e.score, e.source.name, e.target.name))
        if pattern_cap is not None:
            items = items[:pattern_cap]
        all_edges.extend(items)
    return sorted(all_edges, key=lambda e: (-e.score, e.pattern, e.source.name))


__all__ = [
    "CardView",
    "Edge",
    "PATTERN_BASE",
    "PATTERNS",
    "PatternDef",
    "build_edges",
    "color_lock_mill",
    "draw_engine",
    "free_cast_loops",
    "infinite_etb_loop",
    "mana_engine",
    "sacrifice_recursion",
    "score_edge",
    "storm_engine",
]
