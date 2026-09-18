"""Scenario model and scenario construction.

Split out of ``combo_discovery.witness``; behaviour is unchanged.  This module
owns the injected board model (:class:`CardSpec`, :class:`PlayerScenario`,
:class:`Scenario`, :class:`LinkPlan`) and :func:`build_scenario`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

#: Generous default starting pool: enough to begin most loops.  Callers that
#: know the line's per-iteration cost should override it.
DEFAULT_MANA_PER_COLOR = 8
DEFAULT_MANA_COLORS = ("W", "U", "B", "R", "G", "C")
DEFAULT_LIFE = 20
DEFAULT_OPPONENT_LIFE = 20
#: Both players get a small non-empty library by default so that a witness run
#: that turns out NOT to be a loop ends naturally (instead of decking out and
#: reporting ``refuted``). Override via the ``library``/``opponent_library``
#: arguments when a scenario needs a specific library.
DEFAULT_LIBRARY_SIZE = 20
DEFAULT_LIBRARY_LAND = "Forest"

#: Cards staged into the combo player's graveyard when the combo is gated on a
#: graveyard condition (delirium and friends) and the caller did not supply one.
#: Four distinct *primary* card types satisfy delirium's "four or more card
#: types among cards in your graveyard": a land (Forest), a creature (Grizzly
#: Bears), an instant (Lightning Bolt) and a sorcery (Divination).  Each name
#: was verified against the corpus ``cards`` table (all four resolve with the
#: expected type line), so Forge can load them.
DEFAULT_GRAVEYARD: tuple[str, ...] = (
    "Forest",
    "Grizzly Bears",
    "Lightning Bolt",
    "Divination",
)

#: Structural types that mean "this card starts on the battlefield".  Anything
#: else (Instant/Sorcery/...) is presumed to need casting and starts in hand.
_PERMANENT_TYPE_TOKENS = (
    "creature",
    "artifact",
    "enchantment",
    "land",
    "planeswalker",
    "battle",
)


# ---------------------------------------------------------------------------
# Scenario model
# ---------------------------------------------------------------------------


@dataclass
class CardSpec:
    """One card in an injected zone (mirrors proto ``CardSpec``)."""

    name: str
    set: str = ""
    tapped: bool = False
    summoning_sick: bool = False
    counters: dict[str, int] = field(default_factory=dict)
    damage: int = 0
    no_etb_triggers: bool = False
    id: int = 0
    attached_to: int = 0

    def canonical(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "set": self.set,
            "tapped": self.tapped,
            "summoning_sick": self.summoning_sick,
            "counters": {str(k): int(v) for k, v in sorted(self.counters.items())},
            "damage": int(self.damage),
            "no_etb_triggers": self.no_etb_triggers,
            "id": int(self.id),
            "attached_to": int(self.attached_to),
        }


@dataclass
class PlayerScenario:
    """One player's injected state (mirrors proto ``PlayerScenario``)."""

    player: int = 0
    life: int = -1  # -1 = leave unchanged
    mana: dict[str, int] = field(default_factory=dict)
    battlefield: list[CardSpec] = field(default_factory=list)
    hand: list[CardSpec] = field(default_factory=list)
    graveyard: list[CardSpec] = field(default_factory=list)
    library: list[CardSpec] = field(default_factory=list)
    exile: list[CardSpec] = field(default_factory=list)

    def canonical(self) -> dict[str, Any]:
        # Non-library zones are order-insensitive; library order is meaningful
        # (index 0 = top) and is preserved verbatim.
        def specs(items: list[CardSpec], keep_order: bool) -> list[dict[str, Any]]:
            canon = [s.canonical() for s in items]
            if not keep_order:
                canon.sort(key=lambda d: json.dumps(d, sort_keys=True))
            return canon

        return {
            "player": self.player,
            "life": self.life,
            "mana": {str(k): int(v) for k, v in sorted(self.mana.items())},
            "battlefield": specs(self.battlefield, False),
            "hand": specs(self.hand, False),
            "graveyard": specs(self.graveyard, False),
            "library": specs(self.library, True),
            "exile": specs(self.exile, False),
        }


@dataclass
class Scenario:
    """A complete injected game state (v7 ``SetupScenarioRequest`` payload)."""

    players: list[PlayerScenario] = field(default_factory=list)
    active_player: int = -1  # -1 = leave unchanged
    turn: int = 0  # 0 = leave unchanged
    phase: str = ""  # "" = leave unchanged
    require_outstanding_decision: bool = True

    def canonical(self) -> dict[str, Any]:
        return {
            "players": [p.canonical() for p in sorted(self.players, key=lambda p: p.player)],
            "active_player": self.active_player,
            "turn": self.turn,
            "phase": self.phase,
            "require_outstanding_decision": self.require_outstanding_decision,
        }

    def canonical_json(self) -> str:
        return json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))

    def scenario_hash(self) -> str:
        """SHA-256 of the canonical form (deterministic replay key)."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Link plans (the scripted line)
# ---------------------------------------------------------------------------


@dataclass
class LinkPlan:
    """One scripted step of the line: activate ``src`` to drive ``dst``.

    ``params`` optionally carries per-step answer hints used at non-PRIORITY
    decisions: ``targets`` (card names / ``"player:N"``), ``cards`` (names),
    ``modes`` (option ids), ``number`` (an announced value).
    """

    src: str
    dst: str
    kind: str = ""
    subkind: str = ""
    motif: str = ""
    params: dict[str, Any] = field(default_factory=dict)


def _link_plan(link: Any) -> LinkPlan:
    """Coerce a real ``Link``/dict/LinkPlan into a :class:`LinkPlan`."""
    if isinstance(link, LinkPlan):
        return link
    if isinstance(link, dict):
        return LinkPlan(
            src=str(link.get("src") or ""),
            dst=str(link.get("dst") or ""),
            kind=str(link.get("kind") or ""),
            subkind=str(link.get("subkind") or ""),
            motif=str(link.get("motif") or ""),
            params=dict(link.get("params") or {}),
        )
    src = getattr(getattr(link, "src", None), "card_name", None)
    dst = getattr(getattr(link, "dst", None), "card_name", None)
    if src is None:
        src = getattr(link, "src", "")
    if dst is None:
        dst = getattr(link, "dst", "")
    return LinkPlan(
        src=str(src),
        dst=str(dst),
        kind=str(getattr(link, "kind", "") or ""),
        subkind=str(getattr(link, "subkind", "") or ""),
        motif=str(getattr(link, "motif", "") or ""),
        params=dict(getattr(link, "params", None) or {}),
    )


def link_plans(source: Any) -> list[LinkPlan]:
    """Normalize a Combo / list of links / single LinkPlan into LinkPlans."""
    if source is None:
        return []
    if isinstance(source, LinkPlan):
        return [source]
    if isinstance(source, (list, tuple)):
        if all(isinstance(x, LinkPlan) for x in source):
            return list(source)
        return [_link_plan(x) for x in source]
    links = getattr(source, "links", None)
    if links is None:
        return []
    return [_link_plan(link) for link in links]


def _combo_cards(source: Any) -> tuple[str, ...]:
    """Card names of a Combo, a candidate pair, or an explicit name list."""
    if source is None:
        return ()
    cards = getattr(source, "cards", None)
    if cards:
        return tuple(str(c) for c in cards)
    if isinstance(source, str):
        return (source,)
    if isinstance(source, (list, tuple)):
        return tuple(str(c) for c in source)
    return ()


def synthetic_cycle(cards: Sequence[str]) -> list[LinkPlan]:
    """A deterministic ping-pong cycle over ``cards`` (fallback line order).

    Real combos should pass their ontology ``Combo.links`` (which carry the true
    order); this is the candidate-pair fallback used by the CLI.
    """
    names = [str(c) for c in cards]
    if len(names) < 2:
        return []
    return [
        LinkPlan(src=names[i], dst=names[(i + 1) % len(names)], kind="synthetic")
        for i in range(len(names))
    ]


# ---------------------------------------------------------------------------
# Scenario building
# ---------------------------------------------------------------------------


def _ability_for(combo: Any, name: str) -> Any | None:
    for ability in getattr(combo, "abilities", ()) or ():
        if getattr(ability, "card_name", None) == name:
            return ability
    return None


def _must_be_cast(combo: Any, name: str) -> bool:
    """True when ``name`` is presumed to need casting (starts in hand).

    Uses the ability's ``card_context().type_line`` when available, else the
    ability kind.  A card with no information defaults to the battlefield
    (permanents are the common loop pieces).
    """
    ability = _ability_for(combo, name)
    if ability is None:
        return False
    context = None
    get_context = getattr(ability, "card_context", None)
    if callable(get_context):
        context = get_context()
    type_line = (getattr(context, "type_line", "") or "").lower()
    if type_line:
        return not any(token in type_line for token in _PERMANENT_TYPE_TOKENS)
    kind = str(getattr(ability, "ability_kind", "") or "").lower()
    return any(word in kind for word in ("spell", "sorcery", "instant"))


def _is_attachment(type_line: str) -> bool:
    """True when ``type_line`` names an Aura or Equipment subtype (word-boundary).

    Both are staged *attached* to a host, because an attachment's granted ability
    is offered under the host's name and its loop-closing target is the host
    itself.  Equipment that grants an activated untap ability (Umbral Mantle,
    Thornbite Staff) is the drivable shape.
    """
    return bool(
        re.search(r"(?<![A-Za-z0-9])(aura|equipment)(?![A-Za-z0-9])",
                  type_line or "", re.IGNORECASE)
    )


def _type_line_for(combo: Any, name: str) -> str:
    """Best-effort type line for ``name`` from a combo/candidate.

    Candidates carry ``type_lines`` aligned with ``cards``; ontology combos and
    test fakes expose it through their abilities' ``card_context()``.  Returns
    ``""`` when no information is available.
    """
    cards: Any = getattr(combo, "cards", None) or ()
    type_lines: Any = getattr(combo, "type_lines", None) or ()
    if type_lines:
        try:
            index = list(cards).index(name)
        except ValueError:
            index = -1
        if 0 <= index < len(type_lines):
            candidate_line = str(type_lines[index] or "").strip()
            if candidate_line:
                return candidate_line
    ability = _ability_for(combo, name)
    if ability is not None:
        get_context = getattr(ability, "card_context", None)
        context = get_context() if callable(get_context) else None
        line = str(getattr(context, "type_line", "") or "").strip()
        if line:
            return line
    return ""


def _oracle_text_for(combo: Any, name: str) -> str:
    """Best-effort oracle text for ``name`` from a combo/candidate.

    Mirrors :func:`_type_line_for`: candidates carry ``oracle_texts`` aligned
    with ``cards``; ontology combos and test fakes expose it through their
    abilities' ``card_context()``.  Returns ``""`` when no information is
    available, so combos without oracle text stay empty (best-effort).
    """
    cards: Any = getattr(combo, "cards", None) or ()
    oracle_texts: Any = getattr(combo, "oracle_texts", None) or ()
    if oracle_texts:
        try:
            index = list(cards).index(name)
        except ValueError:
            index = -1
        if 0 <= index < len(oracle_texts):
            candidate_text = str(oracle_texts[index] or "").strip()
            if candidate_text:
                return candidate_text
    ability = _ability_for(combo, name)
    if ability is not None:
        get_context = getattr(ability, "card_context", None)
        context = get_context() if callable(get_context) else None
        text = str(getattr(context, "oracle_text", "") or "").strip()
        if text:
            return text
    return ""


def _combo_oracle_texts(combo: Any) -> tuple[str, ...]:
    """Oracle texts of every combo card (empty entries included), best-effort."""
    cards = _combo_cards(combo)
    return tuple(_oracle_text_for(combo, name) for name in cards)


def is_graveyard_gated(oracle_texts: Sequence[str]) -> bool:
    """True when any oracle text is gated on a graveyard condition.

    Delirium is the motivating case: *"if there are four or more card types
    among cards in your graveyard"* can never hold against the default empty
    graveyard, so :func:`build_scenario` stages a supporting graveyard for such
    combos.  The match is case-insensitive and deliberately broad; an empty or
    absent text never gates.
    """
    markers = (
        "delirium",
        "card types among cards in your graveyard",
        "cards in your graveyard",
    )
    for text in oracle_texts or ():
        low = str(text or "").lower()
        if any(marker in low for marker in markers):
            return True
    return False


def _assign_battlefield_ids(
    battlefield: Sequence[CardSpec], type_lines: dict[str, str]
) -> None:
    """Assign deterministic battlefield ids and attach attachments to a host.

    Ids are a single incrementing counter in emission order, starting at 1.
    Each Aura/Equipment is attached to the first non-attachment battlefield card
    (same player); when there is none it is left unattached.
    """
    for index, spec in enumerate(battlefield, start=1):
        spec.id = index
    host_id = 0
    for spec in battlefield:
        if not _is_attachment(type_lines.get(spec.name, "")):
            host_id = spec.id
            break
    if host_id:
        for spec in battlefield:
            if spec.id != host_id and _is_attachment(type_lines.get(spec.name, "")):
                spec.attached_to = host_id


def _default_library() -> list[CardSpec]:
    """A small basic-land library so non-loop witness runs end naturally."""
    return [CardSpec(name=DEFAULT_LIBRARY_LAND) for _ in range(DEFAULT_LIBRARY_SIZE)]


def build_scenario(
    combo: Any = None,
    *,
    cards: Sequence[str] | None = None,
    player: int = 0,
    opponent: int = 1,
    life: int = DEFAULT_LIFE,
    opponent_life: int = DEFAULT_OPPONENT_LIFE,
    mana: dict[str, int] | None = None,
    library: Sequence[CardSpec] | None = None,
    hand: Sequence[CardSpec] | None = None,
    graveyard: Sequence[CardSpec] | None = None,
    opponent_library: Sequence[CardSpec] | None = None,
    active_player: int | None = None,
    turn: int = 1,
    phase: str = "Main1",
    require_outstanding_decision: bool = True,
) -> Scenario:
    """Stage ``combo`` as a starting scenario for witness search.

    Policy:

    * combo cards the ontology says must be cast (Instant/Sorcery) go to hand;
      everything else starts on the battlefield;
    * the combo player gets ``life`` and a generous mana pool (per colour)
      so the first loop iteration can always be paid for;
    * the opponent is harmless: ``opponent_life`` and no battlefield, but both
      players get a small default library (``DEFAULT_LIBRARY_SIZE`` basic
      lands) unless ``library``/``opponent_library`` is given — so a non-loop
      line ends naturally rather than by decking out.
    """
    names = tuple(str(c) for c in cards) if cards is not None else _combo_cards(combo)
    if not names:
        raise ValueError("build_scenario needs at least one combo card")

    battlefield: list[CardSpec] = []
    starting_hand: list[CardSpec] = list(hand or [])
    for name in names:
        spec = CardSpec(name=name)
        if _must_be_cast(combo, name):
            starting_hand.append(spec)
        else:
            battlefield.append(spec)

    # Attachments are only emitted when the combo actually knows its type lines;
    # otherwise the scenario is unchanged (no ids, no attachments).
    type_lines = {name: _type_line_for(combo, name) for name in names}
    if any(type_lines.values()):
        _assign_battlefield_ids(battlefield, type_lines)

    mana_map = (
        {str(k): int(v) for k, v in mana.items()}
        if mana is not None
        else dict.fromkeys(DEFAULT_MANA_COLORS, DEFAULT_MANA_PER_COLOR)
    )
    # Graveyard-gated combos (delirium) need a supporting graveyard or their
    # condition can never hold; only stage one when the caller did not supply
    # ``graveyard`` at all (an explicit empty list still wins).
    if graveyard is None and is_graveyard_gated(_combo_oracle_texts(combo)):
        graveyard_specs: list[CardSpec] = [
            CardSpec(name=name) for name in DEFAULT_GRAVEYARD
        ]
    else:
        graveyard_specs = list(graveyard or [])
    combo_player = PlayerScenario(
        player=int(player),
        life=int(life),
        mana=mana_map,
        battlefield=battlefield,
        hand=starting_hand,
        graveyard=graveyard_specs,
        library=list(library) if library is not None else _default_library(),
    )
    harmless = PlayerScenario(
        player=int(opponent),
        life=int(opponent_life),
        mana={},
        library=(
            list(opponent_library) if opponent_library is not None else _default_library()
        ),
    )
    players = sorted([combo_player, harmless], key=lambda p: p.player)
    return Scenario(
        players=players,
        active_player=int(player) if active_player is None else int(active_player),
        turn=int(turn),
        phase=str(phase),
        require_outstanding_decision=bool(require_outstanding_decision),
    )
