"""Typed ports and per-ability signatures (the "interaction algebra" input).

A :class:`Port` is a typed capability (or requirement) that an *ability* exposes.
An :class:`AbilitySig` groups one card ability's ports: what it triggers on, what
it consumes (activation costs), what it produces (effects), what gates make it
conditional, and what it can target.

This is **derived, not hand-coded**: the extractor already turns Forge scripts
into ability-scoped predicates (``card_predicates`` + ``ability_ref`` /
``root_ability_ref`` / ``chain_gates``).  ``build_signatures`` re-runs that same
machinery and projects each predicate into a typed port, so the vocabulary, the
restriction parser and the gate detection all have exactly one home.  Ports do
not invent new card knowledge.

Design notes
------------

* One signature **per ability**, not per card: effects are grouped by their root
  ability so a trigger and an unrelated activated ability never merge.
* All restriction typing goes through
  :func:`combo_discovery.ontology.restrictions.parse_restriction`; a port stores
  the serialized :class:`~combo_discovery.ontology.restrictions.Restriction` so
  link checks are structural.
* Output is deterministic: cards/abilities are emitted in sorted order and every
  port tuple is sorted by a canonical key.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import vocabulary as vocab
from .extractor import (
    CardContext,
    CardEffect,
    build_ability_links,
    classify_effect,
)
from .patterns.base import CardView
from .restrictions import Restriction, parse_restriction

# Tap symbol in a cost (same guard as the extractor's ``_TAP_COST_RE``).
_TAP_COST_RE = re.compile(r"(?<![A-Za-z])T(?![A-Za-z])")
_MANA_LETTER = frozenset("WUBRG")
_MANA_ALL = frozenset("WUBRG")
_SAC_RE = re.compile(r"Sac<([^>]*)>")
_PAYLIFE_RE = re.compile(r"PayLife<(\d+)>")
_TRUTHY = frozenset({"true", "1", "yes"})


# ---------------------------------------------------------------------------
# Port
# ---------------------------------------------------------------------------


def _canon(value: Any) -> Any:
    """Canonical, hashable projection of nested params (for stable equality)."""
    if isinstance(value, Mapping):
        return tuple(sorted((str(k), _canon(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_canon(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted(_canon(v) for v in value))
    return value


@dataclass(frozen=True, eq=False)
class Port:
    """A typed capability or requirement.

    ``params`` is an open dict; the convention across the engine is:

    * ``predicate`` — the originating vocabulary predicate (motif/enrichment token);
    * ``restriction`` — a serialized ``Restriction`` for target-shaped ports;
    * ``controller`` / ``legendary`` / ``defined`` / ``types`` / ``colors`` / ...
      — the structural signal copied from the extractor params.
    """

    kind: str
    params: dict[str, Any] = field(default_factory=dict)

    def key(self) -> tuple[Any, ...]:
        return (self.kind, _canon(self.params))

    def __hash__(self) -> int:  # pragma: no cover - trivial
        return hash(self.key())

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Port) and self.key() == other.key()

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        params = {k: v for k, v in self.params.items() if v is not None}
        return f"Port({self.kind!r}, {params!r})"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "params": self.params}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Port:
        return cls(str(data.get("kind") or "other"), dict(data.get("params") or {}))

    @property
    def predicate(self) -> str:
        return str(self.params.get("predicate") or self.kind.upper())

    @property
    def restriction(self):
        return Restriction.from_dict(self.params.get("restriction"))


def _port_sort_key(port: Port) -> tuple[str, str]:
    return (port.kind, repr(_canon(port.params)))


def _dedup_ports(ports: Iterable[Port]) -> tuple[Port, ...]:
    """Deduplicate by structural key, keeping the first, in canonical order."""
    unique: dict[tuple[Any, ...], Port] = {}
    for port in ports:
        unique.setdefault(port.key(), port)
    return tuple(sorted(unique.values(), key=_port_sort_key))


# ``Restriction`` is re-exported for callers/tests of this module.


# ---------------------------------------------------------------------------
# AbilitySig
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class AbilitySig:
    """One card ability projected into typed ports.

    ``triggers_on`` is the ability's trigger (or ``None`` for activated/static
    abilities).  ``raw`` carries the card :class:`CardContext`, root verb/cost and
    the extra evidence needed by the link layer (it is the "escape hatch" the
    spec reserves for exactly this).
    """

    card_id: int
    card_name: str
    ability_ref: str
    ability_kind: str
    triggers_on: Port | None = None
    consumes: tuple[Port, ...] = ()
    produces: tuple[Port, ...] = ()
    gates: tuple[Port, ...] = ()
    targets: tuple[Port, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[int, str]:
        return (self.card_id, self.ability_ref)

    def card_context(self) -> CardContext | None:
        return self.raw.get("context")

    def has_kind(self, kind: str) -> bool:
        return any(p.kind == kind for p in (*self.consumes, *self.produces))

    def produces_kind(self, kind: str) -> tuple[Port, ...]:
        return tuple(p for p in self.produces if p.kind == kind)

    def consumes_kind(self, kind: str) -> tuple[Port, ...]:
        return tuple(p for p in self.consumes if p.kind == kind)

    def view(self, predicates: Sequence[Any] = ()) -> CardView:
        """A ``CardView`` over this ability's card (for reuse of copy checks)."""
        context = self.card_context()
        if context is None:  # pragma: no cover - defensive
            raise ValueError(f"signature {self.key} has no card context")
        return CardView.build(context, list(predicates))

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "card_name": self.card_name,
            "ability_ref": self.ability_ref,
            "ability_kind": self.ability_kind,
            "triggers_on": self.triggers_on.to_dict() if self.triggers_on else None,
            "consumes": [p.to_dict() for p in self.consumes],
            "produces": [p.to_dict() for p in self.produces],
            "gates": [p.to_dict() for p in self.gates],
            "targets": [p.to_dict() for p in self.targets],
            "raw": {k: v for k, v in self.raw.items() if k != "context"},
        }


# ---------------------------------------------------------------------------
# Port derivation
# ---------------------------------------------------------------------------


def _mana_colors(raw: str | None) -> frozenset[str]:
    text = (raw or "").strip()
    if not text:
        return frozenset()
    if text.lower() in ("any", "anycolor", "all"):
        return _MANA_ALL
    return frozenset(ch for ch in text.upper() if ch in _MANA_LETTER)


def _restriction_params(spec_dict: dict[str, Any] | None) -> dict[str, Any]:
    if not spec_dict:
        return {"restriction": parse_restriction("").to_dict()}
    return {
        "restriction": spec_dict,
        "controller": _first_controller(spec_dict),
        "legendary": _first_legendary(spec_dict),
        "other": any(alt.get("other") for alt in spec_dict.get("alternatives") or []),
        "self": bool(spec_dict.get("self_only")),
    }


def _first_controller(spec_dict: dict[str, Any]) -> str:
    for alt in spec_dict.get("alternatives") or []:
        controller = alt.get("controller")
        if controller and controller != "any":
            return str(controller)
    return "any"


def _first_legendary(spec_dict: dict[str, Any]) -> bool | None:
    for alt in spec_dict.get("alternatives") or []:
        legendary = alt.get("legendary")
        if legendary is not None:
            return bool(legendary)
    return None


def _primary_type(spec_dict: dict[str, Any] | None) -> str | None:
    if not spec_dict:
        return None
    for alt in spec_dict.get("alternatives") or []:
        types = alt.get("types") or []
        if types:
            return str(types[0]).lower()
    return None


def _cost_ports(cost: str | None) -> list[Port]:
    """Activation-cost literals -> consume ports (tap, mana, sacrifice, ...)."""
    text = cost or ""
    ports: list[Port] = []
    if _TAP_COST_RE.search(text):
        ports.append(Port("tap", {"self": True, "predicate": "TAPS_COST"}))
    colors = {ch for ch in text.upper() if ch in _MANA_LETTER}
    generic = sum(int(t) for t in re.findall(r"(?<![A-Za-z])(\d+)(?![A-Za-z])", text))
    has_braces = bool(re.search(r"\{.*?\}", text))
    x_cost = "X" in text.upper()
    if colors or generic or has_braces or x_cost:
        ports.append(Port("mana", {
            "colors": tuple(sorted(colors)),
            "amount": generic,
            "x": x_cost,
            "predicate": "PRODUCES_MANA",
        }))
    for match in _SAC_RE.finditer(text):
        target = match.group(1).split("/")[-1].strip()
        if target.lower() in ("cardname", "self", ""):
            ports.append(Port("sacrifice", {"self": True, "predicate": "SACRIFICES_SELF"}))
        else:
            ports.append(Port("sacrifice", {
                "self": False,
                "restriction": parse_restriction(target).to_dict(),
                "predicate": "SACRIFICE_OUTLET",
            }))
    life = _PAYLIFE_RE.search(text)
    if life:
        ports.append(Port("life_loss", {"amount": int(life.group(1)), "cost": True}))
    if "Discard<" in text or "DiscardUnless" in text:
        ports.append(Port("discard", {"cost": True}))
    if "Exile<" in text:
        ports.append(Port("exile", {"cost": True}))
    return ports


def _ports_from_match(
    predicate: str,
    params: dict[str, Any],
    effect: CardEffect,
    context: CardContext,
) -> tuple[list[Port], list[Port]]:
    """Project one ``(predicate, params)`` into ``(produces, consumes)`` ports."""
    produces: list[Port] = []
    consumes: list[Port] = []

    if predicate == vocab.PRODUCES_MANA:
        colors = _mana_colors(params.get("color"))
        source = str(params.get("source") or "ability")
        if source == "treasure" or not colors:
            colors = _MANA_ALL
        produces.append(Port("mana", {
            "colors": tuple(sorted(colors)),
            "amount": params.get("amount"),
            "x": bool(params.get("x")),
            "source": source,
            "predicate": "PRODUCES_MANA",
        }))
    elif predicate == vocab.UNTAPS:
        spec = params.get("target") or parse_restriction(params.get("valid")).to_dict()
        base = _restriction_params(spec)
        base.update({"predicate": "UNTAPS", "verb": params.get("verb"),
                     "qualifier": _primary_type(spec)})
        produces.append(Port("untap", base))
    elif predicate == vocab.COPIES_CREATURE:
        spec = params.get("copy") or parse_restriction(params.get("valid")).to_dict()
        base = _restriction_params(spec)
        base.update({
            "predicate": "COPIES_CREATURE",
            "defined": params.get("defined") or "",
            "qualifier": _primary_type(spec),
        })
        produces.append(Port("copy_permanent", base))
    elif predicate == vocab.COPIES_SPELL:
        produces.append(Port("copy_spell", {"predicate": "COPIES_SPELL"}))
    elif predicate == vocab.CREATES_TOKEN:
        script = str(params.get("script") or "")
        types = ("CREATURE",)
        low = script.lower()
        if "treasure" in low or bool(params.get("treasure")):
            types = ("ARTIFACT",)
        produces.append(Port("token", {
            "types": types, "script": script or None,
            "predicate": "CREATES_TOKEN",
        }))
    elif predicate == vocab.DRAWS:
        produces.append(Port("draw", {"predicate": "DRAWS"}))
    elif predicate == vocab.DISCARDS:
        spec = params.get("target") or {}
        # An effect discard is a resource; a discard *cost* is a consume.
        if params.get("cost"):
            consumes.append(Port("discard", {"cost": True}))
        else:
            produces.append(Port("discard", {"predicate": "DISCARDS"}))
    elif predicate == vocab.MILLS:
        produces.append(Port("mill", {"predicate": "MILLS", "amount": params.get("amount")}))
    elif predicate == vocab.DEALS_DAMAGE:
        produces.append(Port("damage", {"predicate": "DEALS_DAMAGE",
                                        "amount": params.get("amount")}))
    elif predicate == vocab.GAINS_LIFE:
        produces.append(Port("life_gain", {"predicate": "GAINS_LIFE",
                                           "amount": params.get("amount")}))
    elif predicate == vocab.LOSES_LIFE:
        produces.append(Port("life_loss", {"predicate": "LOSES_LIFE",
                                           "amount": params.get("amount")}))
    elif predicate == vocab.ADDS_COUNTERS:
        produces.append(Port("counters", {
            "type": params.get("counter"), "predicate": "ADDS_COUNTERS",
        }))
    elif predicate == vocab.GAINS_CONTROL:
        spec = params.get("target") or {}
        base = _restriction_params(spec)
        base.update({"predicate": "GAINS_CONTROL"})
        produces.append(Port("control", base))
        # ``GainControl ... Untap$ True`` (Zealous Conscripts) untaps the target.
        if str(effect.params.get("Untap") or "").lower() in _TRUTHY:
            untap = _restriction_params(spec)
            untap.update({"predicate": "UNTAPS", "qualifier": _primary_type(spec)})
            produces.append(Port("untap", untap))
    elif predicate == vocab.MOVES_ZONE:
        origin = params.get("origin") or ""
        dest = params.get("destination") or ""
        produces.append(Port("zone_move", {
            "from": origin or None, "to": dest or None, "predicate": "MOVES_ZONE",
        }))
    elif predicate == vocab.RECURS_FROM_GRAVEYARD:
        produces.append(Port("zone_move", {
            "from": params.get("origin") or "Graveyard",
            "to": params.get("destination") or None,
            "predicate": "RECURS_FROM_GRAVEYARD",
        }))
    elif predicate == vocab.EXILES:
        spec = params.get("target") or {}
        base = _restriction_params(spec)
        base.update({"predicate": "EXILES"})
        produces.append(Port("exile", base))
    elif predicate == vocab.DESTROYS:
        spec = params.get("target") or {}
        base = _restriction_params(spec)
        base.update({"predicate": "DESTROYS"})
        produces.append(Port("destroy", base))
    elif predicate == vocab.SACRIFICE_OUTLET:
        spec = params.get("target") or {}
        base = _restriction_params(spec)
        base.update({"predicate": "SACRIFICE_OUTLET", "cost_verb": params.get("verb")})
        consumes.append(Port("sacrifice", base))
    elif predicate == vocab.SACRIFICES_SELF:
        consumes.append(Port("sacrifice", {"self": True, "predicate": "SACRIFICES_SELF"}))
    elif predicate == vocab.REDUCES_COST:
        produces.append(Port("cost_reduce", {"predicate": "REDUCES_COST"}))
    elif predicate == vocab.ALTERNATIVE_COST:
        produces.append(Port("alternative_cost", {"predicate": "ALTERNATIVE_COST"}))

    # Direct verbs the predicate vocabulary does not name but the algebra needs.
    if effect.verb == "AddPhase":
        produces.append(Port("extra_phase", {
            "phase": effect.params.get("ExtraPhase") or "Combat",
            "predicate": "ADD_PHASE",
        }))
    if effect.verb == "Tap" and predicate == vocab.OTHER:
        spec = params.get("target") or parse_restriction(params.get("valid")).to_dict()
        base = _restriction_params(spec)
        base.update({"predicate": "TAP", "qualifier": _primary_type(spec)})
        produces.append(Port("tap", base))
    if effect.verb in ("Pump", "PumpAll") and predicate == vocab.OTHER:
        produces.append(Port("pt_boost", {"predicate": "PT_BOOST"}))
    if predicate == vocab.OTHER and effect.verb not in ("AddPhase", "Tap", "Pump", "PumpAll"):
        produces.append(Port("other", {"verb": effect.verb, "predicate": "OTHER"}))
    return produces, consumes


def _trigger_port(effect: CardEffect) -> Port | None:
    """The trigger a root ability fires on, as a typed port."""
    params = effect.params
    mode = effect.verb
    valid = str(params.get("ValidCard") or params.get("ValidCards") or "")
    self_triggered = "Self" in valid or "CARDNAME" in valid
    if mode == "ChangesZone":
        origin = str(params.get("Origin") or params.get("OriginZone") or "")
        dest = str(params.get("Destination") or params.get("DestinationZone") or "")
        if dest == "Battlefield":
            kind, predicate = "enters_battlefield", "ETB_TRIGGER"
        elif origin == "Battlefield" and dest == "Graveyard":
            kind, predicate = "dies", "DIES_TRIGGER"
        else:
            kind, predicate = "zone_change", "CHANGES_ZONE"
        return Port(kind, {"self": self_triggered, "origin": origin or None,
                           "destination": dest or None, "predicate": predicate})
    if mode == "Attacks":
        first = str(params.get("FirstAttack") or "").lower() in _TRUTHY
        return Port("attacks", {"self": self_triggered, "first": first,
                                "predicate": "ATTACKS"})
    if mode in ("DamageDone", "DamageDealt", "CombatDamage"):
        return Port("damage", {"self": self_triggered, "predicate": "DEALS_DAMAGE"})
    if mode in ("SpellCast", "SpellCastOpponent"):
        return Port("cast", {"self": self_triggered, "predicate": "SPELL_CAST"})
    if mode in ("TapsForMana", "TappedForMana", "TapsForManaOnce"):
        return Port("taps_for_mana", {"self": self_triggered, "predicate": "TAPS_FOR_MANA"})
    if mode in ("ManaSpent", "ManaExpend", "ManaExpended"):
        return Port("mana_spent", {"self": self_triggered, "predicate": "MANA_SPENT"})
    if mode == "Phase":
        return Port("phase", {"phase": params.get("Phase") or None,
                              "predicate": "PHASE"})
    return Port("other_trigger", {"mode": mode, "self": self_triggered})


def _gate_ports(gates: Mapping[str, Any]) -> list[Port]:
    return [Port(str(label), {"raw": value, "predicate": str(label).upper()})
            for label, value in sorted(gates.items())]


def build_signatures(
    contexts: Mapping[int, CardContext],
    effects_by_card: Mapping[int, Sequence[CardEffect]],
) -> list[AbilitySig]:
    """Group each card's effects by root ability and project them into ports.

    Deterministic: cards in ascending id order, abilities sorted by ref.
    """
    signatures: list[AbilitySig] = []
    for card_id in sorted(contexts):
        context = contexts[card_id]
        effects = list(effects_by_card.get(card_id, ()))
        if not effects:
            continue
        links = build_ability_links(effects)

        groups: dict[str, list[CardEffect]] = {}
        for effect in effects:
            info = links.get(effect.id)
            if info is None:
                continue
            groups.setdefault(str(info["root_ability_ref"]), []).append(effect)

        for root_ref in sorted(groups):
            group = groups[root_ref]
            root = next(
                (e for e in group if links.get(e.id, {}).get("is_root")),
                group[0],
            )
            root_info = links.get(root.id, {})
            produces: list[Port] = []
            consumes: list[Port] = []
            targets: list[Port] = []
            gate_map: dict[str, Any] = {}
            for effect in group:
                for predicate, params in classify_effect(effect):
                    made, spent = _ports_from_match(predicate, params, effect, context)
                    produces.extend(made)
                    consumes.extend(spent)
                    if predicate == vocab.UNTAPS or predicate == vocab.COPIES_CREATURE \
                            or predicate == vocab.GAINS_CONTROL or predicate == vocab.MILLS \
                            or predicate == vocab.EXILES or predicate == vocab.DESTROYS \
                            or predicate == vocab.ADDS_COUNTERS:
                        spec = params.get("target") or params.get("copy")
                        if spec:
                            targets.append(Port("target", {
                                "predicate": predicate, "restriction": spec,
                            }))
                consumed = _cost_ports(effect.params.get("Cost"))
                consumes.extend(consumed)
                info = links.get(effect.id) or {}
                gate_map.update(info.get("chain_gates") or {})
                gate_map.update(
                    {k: v for k, v in (info.get("gates") or {}).items()
                     if k not in gate_map}
                )

            trigger = _trigger_port(root) if root.effect_kind == "trigger" else None
            signatures.append(AbilitySig(
                card_id=card_id,
                card_name=context.name,
                ability_ref=str(root_info.get("root_ability_ref") or root_ref),
                ability_kind=str(root.effect_kind or ""),
                triggers_on=trigger,
                consumes=_dedup_ports(consumes),
                produces=_dedup_ports(produces),
                gates=_dedup_ports(_gate_ports(gate_map)),
                targets=_dedup_ports(targets),
                raw={
                    "context": context,
                    "root_verb": root.verb,
                    "root_cost": str(root_info.get("root_cost") or ""),
                    "is_trigger": root.effect_kind == "trigger",
                    "gates": dict(gate_map),
                },
            ))
    return signatures


def signatures_from_import(conn, import_id: str) -> list[AbilitySig]:
    """Read an already-imported corpus and build all ability signatures."""
    from .extractor import load_contexts

    contexts, effects = load_contexts(conn, import_id)
    return build_signatures(contexts, effects)


__all__ = [
    "AbilitySig",
    "Port",
    "build_signatures",
    "signatures_from_import",
    "_cost_ports",
    "_trigger_port",
]
