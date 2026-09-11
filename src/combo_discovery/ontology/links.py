"""Links between ability signatures (the "interaction algebra" edges).

Four link kinds, all derived from ports and the existing restriction engine:

* ``re_trigger`` — an engine ability creates a fresh object/phase that fires a
  listener ability again (copy, extra combat, flicker/reanimate);
* ``enables`` — a produced port structurally covers a consumed port (mana colour
  superset, untap -> tap, token -> sacrifice, draw -> discard, ...);
* ``satisfies`` — a produced port discharges a *gate* on another ability
  (mill/discard/destroy -> graveyard-fill gates; mana -> mana gates; a copyable
  partner -> a FirstAttack gate, because each iteration is a fresh token);
* ``hostile`` — an anti-enriched motif that should suppress a candidate cycle.

Nothing here is hand-tuned per card: every check is structural and goes through
:mod:`combo_discovery.ontology.restrictions` (``engine_can_copy`` /
``restriction_matches_card``).  Link generation is deterministic and deduplicated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from . import vocabulary as vocab
from .budget import SearchBudget
from .extractor import CardPredicate
from .patterns.base import CardView
from .ports import AbilitySig, Port
from .restrictions import Restriction, engine_can_copy, restriction_matches_card

#: Weights below this make a link "hostile" (anti-enriched) and suppress a cycle.
HOSTILE_LIFT = 0.5

#: Produce kinds that can discharge a gate, by gate kind.
_GRAVEYARD_FILL_KINDS = frozenset({"mill", "discard", "destroy", "sacrifice", "exile"})

#: ``consume kind -> producer kinds that can cover it`` (for indexing).
ENABLE_PRODUCERS: dict[str, tuple[str, ...]] = {
    "mana": ("mana",),
    "tap": ("untap",),
    "sacrifice": ("token", "zone_move"),
    "discard": ("draw",),
    "counters": ("counters",),
}

#: Inverse index: ``produce kind -> consume kinds it can cover``.
CONSUME_FOR_PRODUCE: dict[str, tuple[str, ...]] = {}
for _consumed, _produced in ENABLE_PRODUCERS.items():
    for _kind in _produced:
        CONSUME_FOR_PRODUCE.setdefault(_kind, ())
        CONSUME_FOR_PRODUCE[_kind] = CONSUME_FOR_PRODUCE[_kind] + (_consumed,)


@dataclass
class Link:
    """One oriented interaction between two abilities."""

    kind: str
    subkind: str
    src: AbilitySig
    dst: AbilitySig
    matched: list[tuple[Port, Port]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    weight: float = 1.0
    motif: str = ""

    def key(self) -> tuple[Any, ...]:
        return (
            self.kind,
            self.subkind,
            self.src.key,
            self.dst.key,
            tuple((p.key(), q.key()) for p, q in self.matched),
        )

    @property
    def pair(self) -> tuple[int, int]:
        return (self.src.card_id, self.dst.card_id)


# ---------------------------------------------------------------------------
# Motif tokens (shared vocabulary with the enrichment analysis)
# ---------------------------------------------------------------------------


def port_token(port: Port) -> str:
    """The enrichment-style token for a port (its originating predicate)."""
    return str(port.params.get("predicate") or port.kind.upper())


def link_motif(produced: Port, consumed: Port) -> str:
    """Canonical cross-card motif string, e.g. ``COPIES_CREATURE~ETB_TRIGGER``."""
    left, right = port_token(produced), port_token(consumed)
    return f"{left}~{right}" if left <= right else f"{right}~{left}"


# ---------------------------------------------------------------------------
# Structural helpers
# ---------------------------------------------------------------------------


def _restriction_types(spec: Restriction | None) -> frozenset[str]:
    if spec is None:
        return frozenset()
    return frozenset(
        str(kind).upper()
        for alt in spec.alternatives
        for kind in (alt.types or ())
    )


def _copy_predicate(port: Port) -> CardPredicate:
    return CardPredicate(
        card_id=0, face_index=0, predicate=vocab.COPIES_CREATURE,
        params={"copy": port.params.get("restriction"), "defined": port.params.get("defined") or ""},
    )


def copy_accepts(port: Port, listener: AbilitySig) -> bool:
    """Reuse ``engine_can_copy`` for a ``copy_permanent`` port vs a listener card."""
    context = listener.card_context()
    if context is None:  # pragma: no cover - defensive
        return False
    engine = CardView.build(context, [_copy_predicate(port)])
    partner = CardView.build(context, [])
    return engine_can_copy(engine, partner).status == "compatible"


def _mana_covers(produced: Port, required: Port) -> bool:
    if produced.params.get("x"):
        return True
    need = set(required.params.get("colors") or ())
    have = set(produced.params.get("colors") or ())
    return not need or need <= have


def ports_enable(produced: Port, consumed: Port, consumer: AbilitySig) -> bool:
    """Does ``produced`` structurally cover ``consumed``?"""
    if produced.kind == "mana" and consumed.kind == "mana":
        return _mana_covers(produced, consumed)
    if produced.kind == "untap" and consumed.kind == "tap":
        context = consumer.card_context()
        if context is None:  # pragma: no cover - defensive
            return False
        return restriction_matches_card(produced.restriction, context).status == "compatible"
    if produced.kind == "token" and consumed.kind == "sacrifice":
        if consumed.params.get("self"):
            return False
        token_types = {str(t).upper() for t in (produced.params.get("types") or ())}
        spec = consumed.restriction
        if spec is None or spec.unknown:
            return bool(token_types)
        needed = _restriction_types(spec)
        return bool(token_types & needed) if needed else bool(token_types)
    if produced.kind == "draw" and consumed.kind == "discard":
        return True
    if produced.kind == "counters" and consumed.kind == "counters":
        left = produced.params.get("type")
        right = consumed.params.get("type")
        return not right or left == right
    if produced.kind == "zone_move" and consumed.kind == "sacrifice":
        return str(produced.params.get("to") or "").lower() in ("battlefield", "hand")
    return False


def _port_satisfies_gate(produced: Port, gate: Port, holder: AbilitySig) -> bool:
    if gate.kind in ("delirium", "graveyard", "graveyard_fill"):
        if produced.kind in _GRAVEYARD_FILL_KINDS:
            return True
        return produced.kind == "zone_move" and \
            str(produced.params.get("to") or "").lower() == "graveyard"
    if gate.kind == "first_attack":
        return produced.kind == "copy_permanent" and copy_accepts(produced, holder)
    if gate.kind in ("mana", "mana_spent"):
        return produced.kind == "mana"
    if gate.kind == "counters":
        return produced.kind == "counters"
    return False


# ---------------------------------------------------------------------------
# Link constructors
# ---------------------------------------------------------------------------


#: Trigger kinds a *copy* can re-fire.  A copy creates a fresh permanent, so an
#: enters-the-battlefield or attack trigger fires again; a ``phase`` trigger
#: (e.g. White Plume Adventurer: "At the beginning of each opponent's upkeep,
#: untap a creature you control") fires once per phase occurrence and is **not**
#: re-fired by a copy.  It is deliberately excluded: modelling it only ever
#: produced non-loops (a one-shot untap per turn cannot close a copy loop), and
#: re-adding it lowers precision.  ``extra_phase`` engines remain scoped to
#: attack triggers, which are the only ones a fresh combat phase re-fires.
_COPY_RE_TRIGGER_KINDS = frozenset({"enters_battlefield", "attacks"})

#: Listener trigger kinds the re-trigger scan considers at all.  Phase triggers
#: are excluded here as well as in :data:`_COPY_RE_TRIGGER_KINDS`; both must be
#: extended together if a future engine genuinely re-fires a phase trigger.
_RE_TRIGGER_LISTENER_KINDS = frozenset({"enters_battlefield", "attacks"})


def _evidence(sig: AbilitySig, port: Port) -> dict[str, Any]:
    return {"card_id": sig.card_id, "name": sig.card_name,
            "ability_ref": sig.ability_ref, "port": port.to_dict()}


def link_re_trigger(engine: AbilitySig, listener: AbilitySig) -> Link | None:
    """A fresh copy/phase/reanimation that fires ``listener``'s trigger again.

    Only :data:`_COPY_RE_TRIGGER_KINDS` are re-fired by a ``copy_permanent``
    engine; ``phase`` triggers are intentionally absent (see the constant).
    """
    if engine.card_id == listener.card_id:
        return None
    trigger = listener.triggers_on
    if trigger is None:
        return None

    for produced in engine.produces:
        if produced.kind == "copy_permanent" and trigger.kind in _COPY_RE_TRIGGER_KINDS:
            if copy_accepts(produced, listener):
                return Link(
                    "re_trigger", "copy", engine, listener,
                    matched=[(produced, trigger)],
                    motif=link_motif(produced, trigger),
                    evidence=[_evidence(engine, produced), _evidence(listener, trigger)],
                )
        if produced.kind == "extra_phase" and trigger.kind == "attacks":
            return Link(
                "re_trigger", "combat", engine, listener,
                matched=[(produced, trigger)],
                motif=link_motif(produced, trigger),
                evidence=[_evidence(engine, produced), _evidence(listener, trigger)],
            )
        if produced.kind == "zone_move" and trigger.kind == "enters_battlefield":
            destination = str(produced.params.get("to") or "").lower()
            if destination != "battlefield":
                continue
            origin = str(produced.params.get("from") or "").lower()
            subkind = "reanimate" if "graveyard" in origin else "flicker"
            return Link(
                "re_trigger", subkind, engine, listener,
                matched=[(produced, trigger)],
                motif=link_motif(produced, trigger),
                evidence=[_evidence(engine, produced), _evidence(listener, trigger)],
            )
    return None


def _enable_subkind(produced: Port, consumed: Port) -> str:
    return f"{produced.kind}_to_{consumed.kind}"


def link_enables(producer: AbilitySig, consumer: AbilitySig) -> Link | None:
    """Producer's outputs cover consumer's inputs (same-player resource flow)."""
    if producer.card_id == consumer.card_id:
        return None
    matched: list[tuple[Port, Port]] = []
    for produced in producer.produces:
        for consumed in consumer.consumes:
            if ports_enable(produced, consumed, consumer):
                matched.append((produced, consumed))
    if not matched:
        return None
    produced, consumed = matched[0]
    return Link(
        "enables", _enable_subkind(produced, consumed), producer, consumer,
        matched=matched, motif=link_motif(produced, consumed),
        evidence=[_evidence(producer, p) for p, _ in matched]
        + [_evidence(consumer, c) for _, c in matched],
    )


def link_satisfies(producer: AbilitySig, gate_holder: AbilitySig) -> Link | None:
    """Producer's outputs discharge a gate on ``gate_holder``."""
    if not gate_holder.gates:
        return None
    matched: list[tuple[Port, Port]] = []
    for produced in producer.produces:
        for gate in gate_holder.gates:
            if _port_satisfies_gate(produced, gate, gate_holder):
                matched.append((produced, gate))
    if not matched:
        return None
    produced, gate = matched[0]
    return Link(
        "satisfies", f"gate_{gate.kind}", producer, gate_holder,
        matched=matched, motif=link_motif(produced, gate),
        evidence=[_evidence(producer, produced), _evidence(gate_holder, gate)],
    )


def _capability_ports(sig: AbilitySig) -> list[Port]:
    """Every port whose token can define a cross-card motif for this ability."""
    ports = list(sig.produces) + list(sig.consumes) + list(sig.gates)
    if sig.triggers_on is not None:
        ports.append(sig.triggers_on)
    return ports


def link_hostile(a: AbilitySig, b: AbilitySig, weights: dict[str, float] | None) -> Link | None:
    """Return a suppression link when an anti-enriched motif joins ``a`` and ``b``."""
    if not weights:
        return None
    for left in _capability_ports(a):
        for right in _capability_ports(b):
            motif = link_motif(left, right)
            weight = weights.get(motif)
            if weight is not None and weight < HOSTILE_LIFT:
                return Link(
                    "hostile", "anti_enriched", a, b,
                    matched=[(left, right)], weight=weight, motif=motif,
                    evidence=[_evidence(a, left), _evidence(b, right)],
                )
    return None


# ---------------------------------------------------------------------------
# Indexed link enumeration
# ---------------------------------------------------------------------------


@dataclass
class LinkOptions:
    """Generation bounds (documented pruning for large corpora)."""

    max_retrigger: int | None = 256
    max_enables: int | None = 128
    include_satisfies: bool = True
    scope: str | None = None  # None = full build; "retrigger" = endpoint-bounded
    closure_depth: int = 3  # scoped build: 2 = 2-card cycles only, 3 = one hop out

    @classmethod
    def safe(cls, **overrides: Any) -> "LinkOptions":
        """The recommended full-corpus preset: tight, bounded, depth-2.

        This is the "safe default" the builder and the demo use.  The full
        ``LinkOptions()`` default (an unbounded ``scope=None`` build at
        ``closure_depth=3``) is retained only for small/hermetic corpora; the
        broad pool is opt-in and must pass ``allow_over_budget=True``.
        """
        base: dict[str, Any] = {
            "scope": "retrigger",
            "max_retrigger": 256,
            "max_enables": 8,
            "closure_depth": 2,
            "include_satisfies": True,
        }
        base.update(overrides)
        return cls(**base)


def _closure_rank(sig: AbilitySig) -> int:
    """Prefer listener/engine abilities that can close a resource loop."""
    if any(p.kind in ("untap", "mana", "token", "counters") for p in sig.produces):
        return 0
    return 1


def _is_retrigger_engine(sig: AbilitySig) -> bool:
    for port in sig.produces:
        if port.kind in ("copy_permanent", "extra_phase"):
            return True
        if port.kind == "zone_move" and \
                str(port.params.get("to") or "").lower() == "battlefield":
            return True
    return False


def _capped(members: Iterable[AbilitySig], cap: int | None) -> list[AbilitySig]:
    ordered = sorted(members, key=lambda s: (_closure_rank(s), s.card_name, s.ability_ref))
    return ordered if cap is None else ordered[:cap]


_SATISFY_PRODUCER_KINDS = (
    "mill", "discard", "destroy", "exile", "sacrifice",
    "mana", "counters", "copy_permanent", "zone_move",
)


def _add_satisfies(
    holder: AbilitySig,
    produced_index: dict[str, list[AbilitySig]],
    add,
    *,
    cap: int | None = None,
    budget: SearchBudget | None = None,
) -> None:
    if not holder.gates:
        return
    for kind in _SATISFY_PRODUCER_KINDS:
        for producer in _capped(produced_index.get(kind, ()), cap):
            if budget is not None and not budget.tick():
                return
            if producer.card_id == holder.card_id:
                continue
            add(link_satisfies(producer, holder))


def build_links(
    sigs: Sequence[AbilitySig],
    *,
    weights: dict[str, float] | None = None,
    options: LinkOptions | None = None,
    budget: SearchBudget | None = None,
) -> list[Link]:
    """Generate deduplicated links among ``sigs`` deterministically.

    ``budget`` bounds the work: the function returns the partial link set and
    sets ``budget.truncated`` / ``budget.reason`` when the wall-clock or step
    ceiling is hit.  When ``budget`` is ``None`` a default (finite) budget is
    used so no call is accidentally unbounded; callers that need the truncation
    report pass their own :class:`~.budget.SearchBudget`.
    """
    options = options or LinkOptions()
    budget = budget or SearchBudget()
    sigs = list(sigs)
    produced_index: dict[str, list[AbilitySig]] = {}
    consumed_index: dict[str, list[AbilitySig]] = {}
    listener_index: dict[str, list[AbilitySig]] = {}
    for sig in sigs:
        for port in sig.produces:
            produced_index.setdefault(port.kind, []).append(sig)
        for port in sig.consumes:
            consumed_index.setdefault(port.kind, []).append(sig)
        if sig.triggers_on is not None:
            listener_index.setdefault(sig.triggers_on.kind, []).append(sig)

    links: dict[tuple[Any, ...], Link] = {}

    def add(link: Link | None) -> None:
        if link is not None:
            links.setdefault(link.key(), link)

    def result() -> list[Link]:
        return sorted(links.values(), key=lambda l: (
            l.src.card_name, l.src.ability_ref, l.dst.card_name, l.dst.ability_ref,
            l.kind, l.subkind, l.motif,
        ))

    # -- re_trigger ---------------------------------------------------------
    closure_listeners: list[AbilitySig] = []
    listener_producers: dict[str, list[AbilitySig]] = {}
    for kind in sorted(_RE_TRIGGER_LISTENER_KINDS):
        for listener in listener_index.get(kind, ()):
            if _closure_rank(listener) == 0:
                closure_listeners.append(listener)
                for port in listener.produces:
                    listener_producers.setdefault(port.kind, []).append(listener)

    re_triggers: list[Link] = []
    for engine in sigs:
        if not budget.tick():
            return result()
        if not _is_retrigger_engine(engine):
            continue
        has_inputs = bool(engine.consumes)
        if options.scope == "retrigger":
            # Only listeners that can feed a resource the engine consumes can
            # close a loop; this is the large-corpus pruning.
            want_kinds: set[str] = set()
            for consumed in engine.consumes:
                want_kinds.update(ENABLE_PRODUCERS.get(consumed.kind, ()))
            if want_kinds:
                unique: dict[tuple[int, str], AbilitySig] = {}
                for kind in sorted(want_kinds):
                    for listener in listener_producers.get(kind, ()):
                        unique.setdefault(listener.key, listener)
            else:
                unique = {s.key: s for s in closure_listeners}
        else:
            unique = {}
            for kind in sorted(_RE_TRIGGER_LISTENER_KINDS):
                for listener in listener_index.get(kind, ()):
                    unique.setdefault(listener.key, listener)
        ordered = sorted(
            unique.values(),
            key=lambda s: (_closure_rank(s), s.card_name, s.ability_ref),
        )
        # Engines with a concrete input already have a bounded candidate list
        # (the producers of that input); input-less engines need the cap.
        if options.max_retrigger is not None and not has_inputs:
            ordered = ordered[: options.max_retrigger]
        for listener in ordered:
            if not budget.tick():
                return result()
            link = link_re_trigger(engine, listener)
            if link is not None:
                re_triggers.append(link)
                add(link)

    # -- enables / satisfies scope -----------------------------------------
    if options.scope == "retrigger" and re_triggers and not budget.expired:
        engine_cards = {link.src.card_id for link in re_triggers}
        listener_cards = {link.dst.card_id for link in re_triggers}
        # For 2-card cycles only the listener endpoints can feed the engine back.
        incoming_cards = listener_cards if options.closure_depth <= 2 else None

        # Precompute the capped producer/consumer fan-out once instead of
        # re-sorting the same lists for every engine/listener (the broad-pool
        # blow-up was partly this repeated ``_capped`` sort).
        capped_by_kind = {
            kind: _capped(members, options.max_enables)
            for kind, members in produced_index.items()
        }
        capped_consumers_by_kind = {
            kind: _capped(members, options.max_enables)
            for kind, members in consumed_index.items()
        }

        # incoming to endpoint engines: producers of what they consume.
        for engine in sigs:
            if not budget.tick():
                return result()
            if engine.card_id not in engine_cards:
                continue
            for consumed in engine.consumes:
                for producer_kind in ENABLE_PRODUCERS.get(consumed.kind, ()):
                    # Tap-loop untappers are left uncapped so a specific partner
                    # is never dropped by name; other resource feeds are bounded.
                    if consumed.kind == "tap" or options.max_enables is None:
                        producers = produced_index.get(producer_kind, ())
                    else:
                        producers = capped_by_kind.get(producer_kind, ())
                    for producer in producers:
                        if not budget.tick():
                            return result()
                        if producer.card_id == engine.card_id:
                            continue
                        if incoming_cards is not None and \
                                producer.card_id not in incoming_cards:
                            continue
                        add(link_enables(producer, engine))
            if options.include_satisfies:
                _add_satisfies(engine, produced_index, add, budget=budget)
                if budget.expired:
                    return result()

        # outgoing from endpoint listeners: consumers of what they produce
        # (only needed to reach a third card).
        if options.closure_depth >= 3:
            for listener in sigs:
                if not budget.tick():
                    return result()
                if listener.card_id not in listener_cards:
                    continue
                for produced in listener.produces:
                    for consume_kind in CONSUME_FOR_PRODUCE.get(produced.kind, ()):
                        for consumer in capped_consumers_by_kind.get(consume_kind, ()):
                            if not budget.tick():
                                return result()
                            if consumer.card_id != listener.card_id:
                                add(link_enables(listener, consumer))
    else:
        consumer_sigs = [s for s in sigs if s.consumes]
        producer_order: dict[str, list[AbilitySig]] = {
            kind: _capped(members, options.max_enables)
            for kind, members in produced_index.items()
        }
        seen_consumers: set[tuple[int, str]] = set()
        for consumer in consumer_sigs:
            if not budget.tick():
                return result()
            if consumer.key in seen_consumers:
                continue
            seen_consumers.add(consumer.key)
            for consumed in consumer.consumes:
                for producer_kind in ENABLE_PRODUCERS.get(consumed.kind, ()):  # type: ignore[arg-type]
                    for producer in producer_order.get(producer_kind, ()):
                        if not budget.tick():
                            return result()
                        if producer.card_id == consumer.card_id:
                            continue
                        add(link_enables(producer, consumer))
            if options.include_satisfies:
                _add_satisfies(consumer, produced_index, add, cap=options.max_enables,
                               budget=budget)
                if budget.expired:
                    return result()

    return result()


def link_weight(link: Link, weights: dict[str, float] | None) -> float:
    """Enrichment weight attached to a link (defaults to neutral 1.0)."""
    if not weights or not link.motif:
        return 1.0
    return float(weights.get(link.motif, 1.0))


__all__ = [
    "ENABLE_PRODUCERS",
    "HOSTILE_LIFT",
    "Link",
    "LinkOptions",
    "_COPY_RE_TRIGGER_KINDS",
    "_RE_TRIGGER_LISTENER_KINDS",
    "build_links",
    "copy_accepts",
    "link_enables",
    "link_hostile",
    "link_motif",
    "link_re_trigger",
    "link_satisfies",
    "link_weight",
    "port_token",
    "ports_enable",
]
