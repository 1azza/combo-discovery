"""Witness search: scenario-driven combo verification (protocol v7).

The witness search stages a hypothesized combo on the board (``build_scenario``),
drives the engine along the combo's ordered links with a deterministic policy
(``WitnessPolicy``), and asks whether the line closes into a loop
(``detect_loop``).  The loop is certified by a *witness signature*: the board
shape must recur while a monotonic resource (mana, tokens, life, damage, cast
count, ...) strictly grows.  A bit-identical consecutive ``state_hash`` is a
degenerate loop (the board did not change at all).

Contract notes (the Java harness is implemented in a parallel lane):

* ``Scenario`` -> the v7 ``SetupScenarioRequest`` is built in
  :meth:`combo_discovery.env.ForgeEnvClient.setup_scenario`.
* ``SetupScenario`` now requires the first turn to have started (it rejects the
  pre-game/mulligan window with FAILED_PRECONDITION), so ``run_witness`` first
  drives the mulligan decisions to the first PRIORITY decision
  (:func:`_drive_pregame`) and only then injects.
* Injection requires a LIVE outstanding decision (the engine is parked) and
  invalidates it; the caller MUST refetch ``GetDecision`` afterwards.
* Injection appends a deterministic ``ScenarioInjected`` event and folds the
  scenario hash into ``state_hash``.
* Library list order is the library order (index 0 = top).
* Deck paths are resolved to absolute paths against the invoking CWD before
  ``start_game`` (the harness resolves them against its own CWD).

Nothing here launches a harness; ``run_witness`` drives whatever client it is
given, which is how the test-suite uses fakes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .env import (
    PROTOCOL_VERSION,
    Answer,
    ForgeEnvClient,
    GameNotActiveError,
    StaleDecisionError,
)
from .generated import forge_env_pb2 as pb
from .runner import DecisionContext, DecisionTraceEntry, default_policy

logger = logging.getLogger(__name__)

#: Version stamped on every witness run (loop-detector + policy semantics).
#: v2: cursor wraps count an iteration on the wrap itself (a link whose action is
#: not offered is skipped and, if a *previous* link is re-offered, the pass is
#: considered complete); structural signatures exclude token permanents; the
#: driver samples a baseline observation plus one per completed iteration.
#: v3: non-PRIORITY selections are *choice-aware* relative to the active link:
#: modal options prefer the untap mode when the loop needs an untap (never "tap"
#: when "untap" is offered and needed), otherwise the mode matching the link's
#: effect verb; target/card selections deterministically prefer the engine card
#: named by the link, then the other combo cards, then the first legal
#: candidate.  See :meth:`WitnessPolicy._choice_mode` /
#: :meth:`WitnessPolicy._choice_targets`.
WITNESS_POLICY_VERSION = "witness-v4"

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

#: Decks used when the caller does not supply real ones (fakes/tests).  A live
#: harness needs real ``.dck`` paths; the CLI requires ``--decks`` for live runs.
DEFAULT_WITNESS_DECKS: list[tuple[str, str]] = [
    ("witness", "witness.dck"),
    ("witness-opponent", "witness-opponent.dck"),
]

#: The combo player is remote (driven by the policy); the opponent is a harmless
#: goldfish so it never competes for decisions.
DEFAULT_PLAYER_TYPES = [pb.PLAYER_TYPE_REMOTE, pb.PLAYER_TYPE_GOLDFISH]

#: Upper bound on pre-game (mulligan) decisions answered before the first
#: PRIORITY decision.  A real London mulligan window needs only a handful; a
#: longer run means the engine is not making progress and the run fails cleanly.
MAX_PREGAME_DECISIONS = 20

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

#: Resource counters tracked for growth.  These are deliberately excluded from
#: the witness signature so a growing loop can still recur structurally.
#: ``graveyard``/``library``/``hand`` are included because discard/draw/mill
#: move cards between them monotonically every iteration; they are reported in
#: the evidence but are *not* durable enough to qualify a loop on their own (see
#: :data:`GAME_STATE_GROWTH_KEYS`).
GROWTH_KEYS = (
    "mana",
    "tokens",
    "life",
    "damage",
    "casts",
    "spells_resolved",
    "extra_phases",
    "permanents",
    "graveyard",
    "library",
    "hand",
)

#: Growth in these counters is driven by the *policy's own* repeated actions
#: (the witness activating/casting on each pass), not by an unbounded game
#: resource.  On their own they must not qualify a recurrence: a static board
#: plus the policy spinning produces `casts`/`spells_resolved` growth with no
#: loop.  Only these keys count as a real, game-state resource.
GAME_STATE_GROWTH_KEYS = ("mana", "tokens", "life", "damage", "permanents")

#: Consecutive observations with an unchanged structural signature and no
#: game-state resource growth before the driver forces a phase-advancing pass.
#: A ``Kiki-Jiki + Pestermite`` loop also has a constant signature but grows
#: ``tokens`` every iteration, so it never reaches this threshold; a pilot
#: spinning on a no-op (e.g. repeatedly equipping inside turn 1) does and is
#: nudged to the next phase, where its real trigger can fire.
SPIN_THRESHOLD = 2

#: Hard cap on forced passes per witness run: a line that cannot make progress
#: still terminates instead of passing forever (after the cap the policy's own
#: answers are used again, exactly as before this fix).
MAX_FORCED_PASSES = 8

#: Word-boundary matchers for the tap/untap modal choice.  The ``\b`` before
#: ``tap`` is essential: "untap" must never be read as an occurrence of "tap"
#: (otherwise a modal "Tap or untap target creature" could resolve to tap and
#: break the loop it was supposed to close).
_UNTAP_RE = re.compile(r"\buntap", re.IGNORECASE)
_TAP_WORD_RE = re.compile(r"\btap\b", re.IGNORECASE)

#: Link kinds that carry no effect verb of their own (structural bookkeeping).
#: A mode is only matched to a link verb when the link names a real effect.
_GENERIC_LINK_TOKENS = frozenset(
    {"", "synthetic", "re_trigger", "enables", "satisfies", "hostile"}
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
        else {color: DEFAULT_MANA_PER_COLOR for color in DEFAULT_MANA_COLORS}
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


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class WitnessPolicy:
    """Drive a game along a combo's ordered links (``DecisionContext -> Answer``).

    At each PRIORITY decision it picks the option matching the current link's
    source card and advances the cursor; non-PRIORITY decisions use the active
    link's ``params`` hints (targets/cards/modes/number) and otherwise fall back
    to :func:`runner.default_policy`.

    **Pass/iteration accounting.**  The cursor wraps, and one full pass is one
    loop iteration.  A link whose action is not offered at this decision is
    skipped: the policy searches forward for a later link that *is* offered.  If
    reaching that link crosses the end of the link list, the pass has completed
    and ``iterations`` increments.  This is what makes a real loop whose line is
    ``[Kiki -> Hippocamp, Hippocamp -> Kiki]`` (only Kiki's tap ability is a
    PRIORITY action; Hippocamp's untap is a passive ETB trigger) count one
    iteration per Kiki activation instead of stalling the cursor forever.

    **Robust matching.**  A link is matched against the option's ``card_name``,
    its ``description`` and its ``kind`` (word-boundary, case-insensitive), so
    an ability with an empty card name or a differently-worded description is
    still identified.  When ``src`` is empty the link's ``kind`` is matched
    against the option kind as a last resort.

    **Boundedness.**  ``max_stall`` force-advances the cursor when nothing in the
    line is ever offered, and ``max_decisions_per_iteration`` caps how many
    decisions a single pass may consume.  The policy therefore never stalls
    forever; every miss is recorded for the run diagnostics.
    """

    def __init__(
        self,
        combo: Any = None,
        *,
        links: Sequence[LinkPlan] | None = None,
        player: int = 0,
        max_stall: int = 16,
        max_decisions_per_iteration: int = 64,
        fallback: Callable[[DecisionContext], Answer] | None = None,
    ):
        self.player = player
        self.links = list(links) if links is not None else link_plans(combo)
        # Aura source names: an Aura's granted activated ability is offered
        # under the *host* creature's name, so only these links may fall back
        # to matching their ``dst`` (see :meth:`_match_link`).
        self._attachment_sources = {
            link.src
            for link in self.links
            if link.src and _is_attachment(_type_line_for(combo, link.src))
        }
        self._fallback = fallback or default_policy
        self.max_stall = max(1, int(max_stall))
        self.max_decisions_per_iteration = max(0, int(max_decisions_per_iteration))
        self.cursor = 0
        self.active: LinkPlan | None = None
        self.iterations = 0
        self.decisions = 0
        self.stall = 0
        self.answers: list[Answer] = []
        self.link_hits: list[int] = [0] * len(self.links)
        # Automatic abilities/triggers never pass through a PRIORITY decision,
        # so they cannot register as ``link_hits``.  The harness broadcasts
        # every stack push (including triggers) as a ``SpellCast`` event whose
        # ``card_name`` is the source card; ``note_card_event`` credits those
        # here instead (see the driver's event capture).
        self.trigger_hits: list[int] = [0] * len(self.links)
        self.link_misses: list[int] = [0] * len(self.links)
        self.skipped: list[str] = []
        self.notes: list[str] = []
        self._decisions_this_iteration = 0

    def new_game(self) -> None:
        """Reset per-game state (run_game/run_witness call this on a new game)."""
        self.cursor = 0
        self.active = None
        self.iterations = 0
        self.decisions = 0
        self.stall = 0
        self.answers = []
        self.link_hits = [0] * len(self.links)
        self.trigger_hits = [0] * len(self.links)
        self.link_misses = [0] * len(self.links)
        self.skipped = []
        self.notes = []
        self._decisions_this_iteration = 0

    # -- cursor -------------------------------------------------------------

    def _note_iteration(self) -> None:
        self.iterations += 1
        self._decisions_this_iteration = 0

    def _advance(self) -> None:
        if not self.links:
            return
        self.cursor += 1
        if self.cursor >= len(self.links):
            self.cursor = 0
            self._note_iteration()

    @staticmethod
    def _link_label(link: LinkPlan) -> str:
        label = f"{link.src or '?'} -> {link.dst or '?'}"
        if link.kind:
            label += f" [{link.kind}]"
        return label

    @staticmethod
    def _match_option(
        options: Sequence[pb.Option], card_name: str, kind: str = ""
    ) -> pb.Option | None:
        target = (card_name or "").strip().lower()
        if target:
            for option in options:
                if (option.card_name or "").strip().lower() == target:
                    return option
            # Description/ability-text fallback on word boundaries only, so a
            # one-letter card name cannot match the "a" inside "Activate".
            edge = rf"(?<![A-Za-z0-9]){re.escape(target)}(?![A-Za-z0-9])"
            for option in options:
                text = (
                    f"{option.card_name} {option.description} {option.kind}"
                ).lower()
                if re.search(edge, text):
                    return option
            return None
        # No source name to match on: fall back to the link kind (e.g. a dict
        # link with only a kind).  Never overrides a name match above.
        kind_norm = (kind or "").strip().lower()
        if kind_norm:
            for option in options:
                if (option.kind or "").strip().lower() == kind_norm:
                    return option
        return None

    def _match_link(
        self, options: Sequence[pb.Option], link: LinkPlan
    ) -> pb.Option | None:
        hit = self._match_option(options, link.src, link.kind)
        if hit is None and link.dst and link.src in self._attachment_sources:
            # An Aura's granted activated ability is offered under the host
            # creature's name, not the Aura's; fall back to the link's target.
            hit = self._match_option(options, link.dst, link.kind)
        return hit

    def note_card_event(self, name: str) -> None:
        """Credit an automatic ability/trigger to the link it belongs to.

        The harness broadcasts every stack push — including triggered
        abilities, which never pass through a PRIORITY decision — as a
        ``SpellCast``/``SpellResolved`` event carrying the source card's name.
        Such an event can never register as a ``link_hit``, so the driver calls
        this to record it in ``trigger_hits`` instead.  A link is credited when
        its ``src`` matches the event name case-insensitively, first exactly
        and then on word boundaries (the same rule as :meth:`_match_option`).
        Empty names are ignored.
        """
        target = (name or "").strip().lower()
        if not target:
            return
        for index, link in enumerate(self.links):
            src = (link.src or "").strip().lower()
            if not src:
                continue
            if src == target:
                self.trigger_hits[index] += 1
                continue
            edge = rf"(?<![A-Za-z0-9]){re.escape(src)}(?![A-Za-z0-9])"
            if re.search(edge, target):
                self.trigger_hits[index] += 1

    @staticmethod
    def _candidate_id(ctx: DecisionContext, name: str) -> int | None:
        low = str(name).strip().lower()
        for candidate in ctx.candidates:
            if (candidate.name or "").strip().lower() == low:
                return int(candidate.card_id)
        return None

    # -- choice-aware selections --------------------------------------------
    #
    # The link's *intent* is the only signal available at a non-PRIORITY
    # decision (the engine does not tell us which card the ETB belongs to).
    # The rules are therefore conservative and deterministic:
    #
    # * a modal tap/untap choice prefers the untap mode whenever the loop
    #   needs an untap (a tap would break the cycle it is meant to close);
    #   otherwise the mode matching the link's effect verb is preferred;
    # * target/card selections prefer the engine card named by the link
    #   (``link.src`` — the card that tapped and must be untapped), then the
    #   link's ``dst``, then every other combo card in line order, then the
    #   first legal engine candidate.
    #
    # Everything is derived from the link list, so it is deterministic and
    # testable with fakes; explicit ``params`` hints still win.

    @staticmethod
    def _link_text(link: LinkPlan) -> str:
        """Lowercase text used to read a link's intent (word boundaries later)."""
        parts = [link.src, link.dst, link.kind, link.subkind, link.motif]
        for key in ("action", "verb", "effect"):
            value = (link.params or {}).get(key)
            if isinstance(value, str):
                parts.append(value)
        return " ".join(part for part in parts if part)

    def _loop_needs_untap(self) -> bool:
        """True when the line needs an untap to close.

        A link that explicitly names an untap action is decisive.  Otherwise a
        tap-only line needs no untap.  A synthetic/unknown line defaults to
        ``True``: at a modal tap/untap choice the untap mode is the
        loop-closing one, so the safe default is never to self-tap.
        """
        saw_tap = False
        for link in self.links:
            text = self._link_text(link)
            if _UNTAP_RE.search(text):
                return True
            if _TAP_WORD_RE.search(text):
                saw_tap = True
        return not saw_tap

    def _link_verb(self, link: LinkPlan) -> str:
        """The link's effect verb (``subkind``/``kind``/``motif``), if any."""
        for value in (link.subkind, link.kind, link.motif):
            token = (value or "").strip().lower()
            if token and token not in _GENERIC_LINK_TOKENS:
                return token
        return ""

    def _combo_card_order(self) -> list[str]:
        """Combo card names in line order (first occurrence wins)."""
        ordered: list[str] = []
        seen: set[str] = set()
        for link in self.links:
            for name in (link.src, link.dst):
                text = (name or "").strip()
                low = text.lower()
                if text and low not in seen:
                    seen.add(low)
                    ordered.append(text)
        return ordered

    def _ordered_preferences(self, link: LinkPlan) -> list[str]:
        """Engine card first, then the link's other end, then combo order."""
        preferred: list[str] = []
        seen: set[str] = set()

        def add(name: str) -> None:
            text = (name or "").strip()
            low = text.lower()
            if text and low not in seen:
                seen.add(low)
                preferred.append(text)

        if link.src in self._attachment_sources:
            # An Aura's granted ability is offered under the *host creature's*
            # name, and the loop-closing target is the tapped host (link.dst),
            # not the Aura itself. Prefer the host.
            add(link.dst)
            add(link.src)
        else:
            add(link.src)
            add(link.dst)
        for name in self._combo_card_order():
            add(name)
        return preferred

    def _select_candidates(
        self, ctx: DecisionContext, preferred: Sequence[str], need: int
    ) -> list[int]:
        """Deterministically pick ``need`` candidates, preference order first.

        Exact name matches win; then word-boundary matches (so a short link
        name such as ``Kiki`` still finds ``Kiki-Jiki, Mirror Breaker``); then
        the request order fills the remainder.
        """
        candidates = list(ctx.candidates)
        chosen: list[int] = []
        used_ids: set[int] = set()

        def take(card_id: int) -> None:
            chosen.append(int(card_id))
            used_ids.add(int(card_id))

        for want in preferred:
            low = str(want).strip().lower()
            if not low:
                continue
            match = None
            for candidate in candidates:
                if int(candidate.card_id) in used_ids:
                    continue
                if (candidate.name or "").strip().lower() == low:
                    match = candidate
                    break
            if match is None:
                edge = rf"(?<![A-Za-z0-9]){re.escape(low)}(?![A-Za-z0-9])"
                for candidate in candidates:
                    if int(candidate.card_id) in used_ids:
                        continue
                    if re.search(edge, (candidate.name or "").lower()):
                        match = candidate
                        break
            if match is not None:
                take(match.card_id)
            if len(chosen) >= need:
                break
        if len(chosen) < need:
            for candidate in candidates:
                if int(candidate.card_id) in used_ids:
                    continue
                take(candidate.card_id)
                if len(chosen) >= need:
                    break
        return chosen

    @staticmethod
    def _bounded_need(ctx: DecisionContext) -> int | None:
        """How many selections the choice wants (``None`` when it must be empty).

        ``min_choices`` is the floor; a loop-closing choice still wants one
        selection when the decision is optional but allows at least one.
        """
        need = int(ctx.min_choices)
        maximum = int(ctx.max_choices)
        if need <= 0:
            if maximum <= 0:
                return None
            need = 1
        if maximum > 0:
            need = min(need, maximum)
        return need

    def _choice_targets(self, ctx: DecisionContext, link: LinkPlan) -> Answer | None:
        need = self._bounded_need(ctx)
        if need is None:
            return None
        card_ids = self._select_candidates(ctx, self._ordered_preferences(link), need)
        player_slots: list[int] = []
        if len(card_ids) < need:
            player_slots = list(ctx.defender_players[: need - len(card_ids)])
        if not card_ids and not player_slots:
            return None
        self.notes.append(
            f"choice: targets {card_ids} players {player_slots} for "
            f"{self._link_label(link)}"
        )
        return ("targets", (card_ids, player_slots))

    def _choice_cards(self, ctx: DecisionContext, link: LinkPlan) -> Answer | None:
        need = self._bounded_need(ctx)
        if need is None:
            return None
        ids = self._select_candidates(ctx, self._ordered_preferences(link), need)
        if not ids:
            return None
        self.notes.append(f"choice: cards {ids} for {self._link_label(link)}")
        return ("card_ids", ids)

    def _choice_mode(self, ctx: DecisionContext, link: LinkPlan) -> Answer | None:
        options = [(int(mid), str(desc or "")) for mid, desc in ctx.mode_options]
        if not options:
            return None
        need = self._bounded_need(ctx)
        if need is None:
            return None
        # 1. Modal tap/untap: the untap mode is loop-closing.  Never self-tap
        #    while the loop needs an untap.
        untap_ids = [mid for mid, desc in options if _UNTAP_RE.search(desc)]
        if untap_ids and self._loop_needs_untap():
            chosen = untap_ids[:need]
            self.notes.append(
                f"choice: untap mode {chosen} for {self._link_label(link)}"
            )
            return ("mode_selection", chosen)
        # 2. Otherwise prefer the mode whose text matches the link's verb.
        verb = self._link_verb(link)
        if verb:
            edge = re.compile(
                rf"(?<![A-Za-z0-9]){re.escape(verb)}(?![A-Za-z0-9])",
                re.IGNORECASE,
            )
            hits = [mid for mid, desc in options if edge.search(desc)]
            if hits:
                chosen = hits[:need]
                self.notes.append(
                    f"choice: {verb} mode {chosen} for {self._link_label(link)}"
                )
                return ("mode_selection", chosen)
        # 3. Deterministic first-N fallback (matches the engine order).
        chosen = [mid for mid, _ in options[:need]]
        return ("mode_selection", chosen) if chosen else None

    # -- answers ------------------------------------------------------------

    def _priority(self, ctx: DecisionContext) -> Answer:
        if not self.links:
            return self._fallback(ctx)
        self._decisions_this_iteration += 1
        if (
            self.max_decisions_per_iteration
            and self._decisions_this_iteration > self.max_decisions_per_iteration
        ):
            self.notes.append(
                f"per-iteration decision budget "
                f"{self.max_decisions_per_iteration} exceeded at cursor "
                f"{self.cursor} ({self._link_label(self.links[self.cursor])})"
            )
            self._decisions_this_iteration = 0
            self._advance()

        link = self.links[self.cursor]
        option = self._match_link(ctx.options, link)
        wrapped = False
        if option is None:
            # Search forward for a later link that is currently available.  If
            # reaching it requires crossing the end of the list, this decision
            # closes the pass (that wrap is the loop iteration boundary).
            for offset in range(1, len(self.links) + 1):
                idx = (self.cursor + offset) % len(self.links)
                candidate = self.links[idx]
                candidate_option = self._match_link(ctx.options, candidate)
                if candidate_option is not None:
                    if idx <= self.cursor:
                        wrapped = True
                    self.cursor = idx
                    link = candidate
                    option = candidate_option
                    break
        if option is None:
            # Nothing in the line is offered at this decision.  Record the miss
            # and force-advance once the stall bound is reached so the policy can
            # never spin forever.
            self.link_misses[self.cursor] += 1
            self.skipped.append(self._link_label(self.links[self.cursor]))
            self.stall += 1
            if self.stall >= self.max_stall:
                self.stall = 0
                self._advance()
            return self._fallback(ctx)
        if wrapped:
            # We moved back to an earlier link: one full pass has completed.
            self._note_iteration()
        self.stall = 0
        self.link_hits[self.cursor] += 1
        self.active = link
        self._advance()
        return ("option_id", int(option.id))

    def _from_active(self, ctx: DecisionContext) -> Answer | None:
        if self.active is None:
            return None
        params = self.active.params or {}
        t = ctx.decision_type
        # Choice-aware fallbacks are only applied to the combo player's own
        # selections: an opponent's decision must never be answered with our
        # engine card (the engine ids are not player-scoped).
        own_choice = ctx.player == self.player
        if t == pb.DECISION_TYPE_CHOOSE_TARGETS:
            raw = params.get("targets")
            if raw is None:
                if own_choice:
                    return self._choice_targets(ctx, self.active)
                return None
            card_ids: list[int] = []
            player_slots: list[int] = []
            for spec in raw:
                text = str(spec)
                if text.lower().startswith("player:"):
                    player_slots.append(int(text.split(":", 1)[1]))
                elif isinstance(spec, int):
                    player_slots.append(int(spec))
                else:
                    cid = self._candidate_id(ctx, text)
                    if cid is not None:
                        card_ids.append(cid)
            if not card_ids and not player_slots and ctx.min_choices > 0:
                return None
            return ("targets", (card_ids, player_slots))
        if t == pb.DECISION_TYPE_CHOOSE_CARDS:
            raw = params.get("cards")
            if raw is None:
                if own_choice:
                    return self._choice_cards(ctx, self.active)
                return None
            ids = [self._candidate_id(ctx, str(n)) for n in raw]
            return ("card_ids", [i for i in ids if i is not None])
        if t == pb.DECISION_TYPE_CHOOSE_MODE:
            raw = params.get("modes")
            if raw is None:
                if own_choice:
                    return self._choice_mode(ctx, self.active)
                return None
            return ("mode_selection", [int(x) for x in raw])
        if t == pb.DECISION_TYPE_OPTIONAL_COSTS:
            # Kicker-style optional costs keep the conservative "pay nothing"
            # default; only an explicit params hint overrides it.
            raw = params.get("modes")
            if raw is None:
                return None
            return ("mode_selection", [int(x) for x in raw])
        if t == pb.DECISION_TYPE_ANNOUNCE and "number" in params:
            return ("number_answer", int(params["number"]))
        return None

    def __call__(self, ctx: DecisionContext) -> Answer:
        self.decisions += 1
        if ctx.decision_type == pb.DECISION_TYPE_PRIORITY and ctx.player == self.player:
            answer = self._priority(ctx)
        else:
            answer = self._from_active(ctx)
            if answer is None:
                answer = self._fallback(ctx)
        self.answers.append(answer)
        return answer

    # -- introspection ------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        """Per-link execution/miss counts and notes, for run diagnostics.

        ``link_hits`` counts only PRIORITY decisions the policy matched to a
        link.  ``trigger_hits`` counts automatic abilities/triggers credited to
        a link from ``SpellCast``/``SpellResolved`` events (which never reach a
        PRIORITY decision).  ``executed_actions`` is their sum: the number of
        link actions the run actually executed, whether by a priority decision
        or a broadcast trigger.  When it is zero the run never executed the
        line and the verdict must be ``inconclusive`` with this diagnostic
        rather than a silent empty result.
        """
        return {
            "policy_version": WITNESS_POLICY_VERSION,
            "links": [self._link_label(link) for link in self.links],
            "link_hits": list(self.link_hits),
            "trigger_hits": list(self.trigger_hits),
            "link_misses": list(self.link_misses),
            "skipped": list(self.skipped),
            "notes": list(self.notes),
            "executed_actions": int(sum(self.link_hits) + sum(self.trigger_hits)),
            "iterations": int(self.iterations),
            "decisions": int(self.decisions),
        }


# ---------------------------------------------------------------------------
# Observations / loop detection
# ---------------------------------------------------------------------------


@dataclass
class Observation:
    """One captured witness step (usually one completed loop iteration)."""

    iteration: int
    signature: str
    resources: dict[str, int] = field(default_factory=dict)
    signature_fields: dict[str, Any] = field(default_factory=dict)
    state_hash: str = ""
    event_seq: int = 0
    turn: int = 0
    phase: str = ""


def _zone_count(zone: Any) -> int:
    total = 0
    for card in getattr(zone, "cards", ()):  # CardRef(name, count)
        count = int(getattr(card, "count", 0) or 0)
        total += count if count > 0 else 1
    return total


def _battlefield_entries(state: pb.FullState) -> list[tuple[Any, ...]]:
    """Non-token battlefield permanents as the structural signature.

    Token permanents are deliberately excluded: a token-growing loop (e.g.
    Kiki-Jiki copying a creature every iteration) adds a new token each pass, so
    including tokens would make the structural signature change on every
    iteration and the recurrence could never be detected.  Token counts are
    tracked as a *growing resource* instead (:func:`resource_totals`).
    """
    permanents = list(getattr(state, "battlefield_cards", ()) or ())
    entries: list[tuple[Any, ...]] = []
    for zone in state.battlefield:
        for pid in zone.permanents:
            if 0 <= pid < len(permanents):
                perm = permanents[pid]
                if bool(perm.is_token):
                    continue
                counters = tuple(
                    sorted((str(c.type), int(c.count)) for c in perm.typed_counters)
                )
                entries.append(
                    (
                        str(perm.card_name),
                        bool(perm.tapped),
                        counters,
                    )
                )
            else:  # harness without a flat battlefield listing: fall back to names
                for card in zone.cards:
                    entries.append((str(card.name), False, ()))
    entries.sort()
    return entries


def _non_token_battlefield_count(state: pb.FullState, zone: Any) -> int:
    """Count one player's battlefield permanents that are not tokens."""
    permanents = list(getattr(state, "battlefield_cards", ()) or ())
    total = 0
    for pid in zone.permanents:
        if 0 <= pid < len(permanents):
            if not bool(permanents[pid].is_token):
                total += 1
        else:
            total += len(list(zone.cards))
    return total


def witness_signature(state: pb.FullState) -> dict[str, Any]:
    """The structural projection that must recur for a loop.

    Captures *non-token* battlefield names/tapped/counters, phase, active
    player, the non-token battlefield count, ``exile``/``command`` counts and
    the stack shape.  Monotonic *scalars* (mana pool, **life totals**, total
    damage, cast counts, **token count**) are deliberately excluded so they can
    accumulate across iterations without changing the signature; they are
    tracked as growing resources by :func:`resource_totals`.  A token-growing
    loop therefore shows a recurring structure + a strictly growing ``tokens``
    resource.

    The *volume* zone counts ``graveyard``/``library``/``hand`` are excluded for
    the same reason: discard/draw/mill move cards between them monotonically
    every iteration, so including them stops a genuine loop from recurring
    (Fear of Missing Out's ETB "discard a card, then draw a card" grows the
    graveyard each pass).  They are reported as resources by
    :func:`resource_totals`.  The battlefield count and ``exile``/``command``
    stay structural: the non-token board must recur, and exile/command changes
    are loop-relevant board shifts rather than per-iteration churn.

    Life is excluded for a concrete reason: a combat loop damages the opponent
    every iteration, so including life would stop the structure from recurring
    and reject a genuine loop (Combat Celebrant + Kiki-Jiki).  ``turn`` is also
    excluded (a turn-cycling loop should recur).  Library order is not exposed
    by FullState v2.
    """
    return {
        "phase": state.phase,
        "active_player": state.active_player,
        "battlefield": _battlefield_entries(state),
        "zone_counts": {
            "battlefield": [
                _non_token_battlefield_count(state, z) for z in state.battlefield
            ],
            "exile": [_zone_count(z) for z in state.exile],
            "command": [_zone_count(z) for z in state.command],
        },
        "stack": [
            (str(e.card_name), str(e.sa_description), int(e.controller))
            for e in state.stack
        ],
    }


def _signature_hash(fields: dict[str, Any]) -> str:
    blob = json.dumps(fields, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def resource_totals(
    state: pb.FullState, *, cast_count: int = 0, spells_resolved: int = 0,
    extra_phases: int = 0,
) -> dict[str, int]:
    """Monotonic resource counters used to qualify a structural recurrence."""
    mana = 0
    typed = list(getattr(state, "typed_mana_pools", ()) or ())
    if typed:
        for pool in typed:
            mana += (
                int(pool.white)
                + int(pool.blue)
                + int(pool.black)
                + int(pool.red)
                + int(pool.green)
                + int(pool.colorless)
            )
    else:  # pragma: no cover - legacy harness fallback
        for value in (getattr(state, "mana_pools", None) or {}).values():
            mana += int(value or 0)

    tokens = 0
    damage = 0
    permanents = list(getattr(state, "battlefield_cards", ()) or ())
    for zone in state.battlefield:
        for pid in zone.permanents:
            if 0 <= pid < len(permanents):
                if permanents[pid].is_token:
                    tokens += 1
                damage += int(permanents[pid].damage)
    return {
        "mana": mana,
        "tokens": tokens,
        "life": sum(int(v) for v in state.life),
        "damage": damage,
        "casts": int(cast_count),
        "spells_resolved": int(spells_resolved),
        "extra_phases": int(extra_phases),
        "permanents": sum(len(list(z.permanents)) for z in state.battlefield),
        # Zone *volumes*: discard/draw/mill grow these monotonically, so they
        # are resources, not structure (see :func:`witness_signature`).
        "graveyard": sum(_zone_count(z) for z in state.graveyard),
        "library": sum(_zone_count(z) for z in state.library),
        "hand": sum(_zone_count(z) for z in state.hand),
    }


def build_observation(
    iteration: int,
    state: pb.FullState,
    *,
    cast_count: int = 0,
    spells_resolved: int = 0,
    extra_phases: int = 0,
    event_seq: int = 0,
) -> Observation:
    fields = witness_signature(state)
    return Observation(
        iteration=int(iteration),
        signature=_signature_hash(fields),
        resources=resource_totals(
            state,
            cast_count=cast_count,
            spells_resolved=spells_resolved,
            extra_phases=extra_phases,
        ),
        signature_fields=fields,
        state_hash=str(state.state_hash),
        event_seq=int(event_seq),
        turn=int(getattr(state, "turn", 0) or 0),
        phase=str(getattr(state, "phase", "") or ""),
    )


def _grown_between(before: dict[str, int], after: dict[str, int]) -> list[str]:
    grown = [
        key
        for key in GROWTH_KEYS
        if int(after.get(key, 0)) > int(before.get(key, 0))
    ]
    # Also honour any extra keys a caller supplied.
    for key in after:
        if key not in GROWTH_KEYS and int(after[key]) > int(before.get(key, 0)):
            if key not in grown:
                grown.append(key)
    return sorted(grown)


def _game_state_grew(before: dict[str, int], after: dict[str, int]) -> bool:
    """True when any *game-state* resource grew between two samples.

    Restricted to :data:`GAME_STATE_GROWTH_KEYS` on purpose: the event counters
    ``casts``/``spells_resolved`` are driven by the policy's own repeated
    actions, so a static board the pilot keeps poking shows counter growth with
    no real progress.  Such a pair is a *spin*, not a loop iteration.
    """
    return any(
        int(after.get(key, 0)) > int(before.get(key, 0))
        for key in GAME_STATE_GROWTH_KEYS
    )


def _pass_option(options: Sequence[pb.Option]) -> pb.Option | None:
    """The first ``kind == "pass"`` option in a PRIORITY request, if any."""
    for option in options:
        if str(option.kind or "").strip().lower() == "pass":
            return option
    return None


def detect_loop(
    observations: Sequence[Observation],
) -> tuple[str, dict[str, Any]]:
    """Classify a witness run from its per-iteration observations.

    Returns ``(verdict, evidence)`` with verdict one of ``loops`` / ``no_loop``
    / ``inconclusive``.

    Rule:

    * ``inconclusive`` with fewer than two *post-baseline* observations;
    * ``loops`` (degenerate) when two CONSECUTIVE post-baseline observations in
      the same turn share a non-empty identical ``state_hash``;
    * ``loops`` when two post-baseline observations in the same turn share a
      non-empty witness signature and a tracked resource grew between them;
    * ``no_loop`` when a same-turn signature recurs but nothing grew, when a
      recurrence only happens across turns, or when no signature recurs.

    Two independent traps motivated the post-baseline / same-turn guards (both
    observed live on the Forge harness):

    * **Baseline pair.** Observation 0 is sampled *before* the first iteration
      completes. When the engine has not acted yet, observations 0 and 1 are
      bit-identical, and the old code certified ``loops`` from that pair alone.
      That fired on every candidate whose policy made no immediate progress
      (Keldon Overseer, Elven Raft-Steerer, Firbolg Flutist), regardless of
      whether a loop existed.
    * **Cross-turn recurrence.** A creature that untaps during ordinary play
      (or a token army that attacks each turn) reproduces a structural
      signature across turns while a resource grows. A real infinite combo
      iterates *within one turn*, so both endpoints must share a known turn.
      An unknown turn (0) stays permissive for harnesses that do not report it.
    """
    if len(observations) < 2:
        return "inconclusive", {
            "reason": "need at least two observations",
            "n": len(observations),
        }

    # Drop the pre-injection baseline: identical baseline/post samples are not
    # evidence that anything looped.
    samples = [o for o in observations if int(o.iteration) >= 1]
    if len(samples) < 2:
        return "inconclusive", {
            "reason": "need at least two post-baseline observations",
            "n": len(samples),
        }

    def same_turn(a: Observation, b: Observation) -> bool:
        if a.turn <= 0 or b.turn <= 0:
            return True  # unknown turn: do not exclude
        return a.turn == b.turn

    for i in range(1, len(samples)):
        prev, cur = samples[i - 1], samples[i]
        if not same_turn(prev, cur):
            continue
        if prev.state_hash and cur.state_hash and prev.state_hash == cur.state_hash:
            return "loops", {
                "kind": "degenerate",
                "pair": [prev.iteration, cur.iteration],
                "turn": cur.turn,
                "state_hash": cur.state_hash,
            }

    first_repeat: tuple[int, int] | None = None
    counter_only_repeat: tuple[int, int] | None = None
    mana_draining_repeat: tuple[int, int] | None = None
    cross_turn_repeat: tuple[int, int] | None = None
    for j in range(1, len(samples)):
        for i in range(j):
            oi, oj = samples[i], samples[j]
            sig = oi.signature
            if not sig or sig != oj.signature:
                continue
            if not same_turn(oi, oj):
                if cross_turn_repeat is None:
                    cross_turn_repeat = (oi.iteration, oj.iteration)
                continue
            grown = _grown_between(oi.resources, oj.resources)
            game_grown = [k for k in grown if k in GAME_STATE_GROWTH_KEYS]
            if game_grown:
                if int(oj.resources.get("mana", 0)) < int(oi.resources.get("mana", 0)):
                    # The loop strictly consumes mana every pass and nothing in
                    # the pair replenishes it, so it is bounded by the starting
                    # pool, not infinite (Orthion / Jolly Balloon Man-style copy
                    # engines whose activation costs mana while the untapper
                    # untaps the engine, not the lands).
                    if mana_draining_repeat is None:
                        mana_draining_repeat = (oi.iteration, oj.iteration)
                    continue
                return "loops", {
                    "kind": "recurrence",
                    "pair": [oi.iteration, oj.iteration],
                    "turn": oj.turn,
                    "signature": sig,
                    "grown": game_grown,
                }
            if grown:
                # Only policy-driven event counters grew (casts/spells_resolved):
                # a static board the policy kept poking, not an unbounded loop.
                if counter_only_repeat is None:
                    counter_only_repeat = (oi.iteration, oj.iteration)
                continue
            if first_repeat is None:
                first_repeat = (oi.iteration, oj.iteration)

    if first_repeat is not None:
        return "no_loop", {
            "reason": "signature recurred but no tracked resource grew",
            "pair": list(first_repeat),
        }
    if counter_only_repeat is not None:
        return "inconclusive", {
            "reason": "signature recurred but only policy-driven event counters grew "
            "(no game-state resource); cannot confirm a loop",
            "pair": list(counter_only_repeat),
        }
    if mana_draining_repeat is not None:
        return "inconclusive", {
            "reason": "recurrence consumes mana each pass (bounded by the pool, "
            "not an infinite loop)",
            "pair": list(mana_draining_repeat),
        }
    if cross_turn_repeat is not None:
        return "no_loop", {
            "reason": "recurrence only across turns (not an infinite loop within a turn)",
            "pair": list(cross_turn_repeat),
        }
    return "no_loop", {"reason": "no signature recurrence"}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


@dataclass
class WitnessResult:
    """Outcome of one witness search (across one or more seeds)."""

    verdict: str
    scenario: Scenario
    iterations: int
    signatures: list[str] = field(default_factory=list)
    resource_deltas: list[dict[str, int]] = field(default_factory=list)
    state_hash_before: str = ""
    state_hash_after: str = ""
    event_start_seq: int = 0
    event_end_seq: int = 0
    decision_trace: list[DecisionTraceEntry] = field(default_factory=list)
    seed: int = 0
    seeds: list[int] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    candidate_kind: str = ""
    candidate_key: str = ""
    card_names: tuple[str, ...] = ()
    infinite: bool = False
    policy_version: str = WITNESS_POLICY_VERSION
    engine_commit: str = ""
    proto_version: int = PROTOCOL_VERSION


def _resource_deltas(observations: Sequence[Observation]) -> list[dict[str, int]]:
    deltas: list[dict[str, int]] = []
    for i, obs in enumerate(observations):
        before = observations[i - 1].resources if i else {}
        deltas.append(
            {
                key: int(value) - int(before.get(key, 0))
                for key, value in obs.resources.items()
            }
        )
    return deltas


def _policy_diagnostics(policy: Any) -> dict[str, Any]:
    """Best-effort per-link execution/miss diagnostics from ``policy``.

    Works for :class:`WitnessPolicy`; any other callable (or a policy without
    the method) yields ``{}`` so the driver stays usable with custom policies.
    """
    diagnostics = getattr(policy, "diagnostics", None)
    if not callable(diagnostics):
        return {}
    try:
        raw = diagnostics()
    except Exception:  # noqa: BLE001 - diagnostics must never break a run
        logger.debug("policy diagnostics failed", exc_info=True)
        return {}
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items()}
    return {}


def _is_over(client: Any, game_id: int) -> bool:
    try:
        return bool(client.is_game_over(game_id).over)
    except Exception:  # noqa: BLE001 - treated as "not over"; the next RPC surfaces it
        return False


def _drive_pregame(
    client: Any,
    game_id: int,
    policy: Callable[[DecisionContext], Answer],
) -> None:
    """Answer pre-game (mulligan) decisions until the first PRIORITY decision.

    ``setup_scenario`` now requires the first turn to have started, so injecting
    while the engine is parked on a mulligan is rejected with FAILED_PRECONDITION
    (the live-run failure this guards against).  This drives the London mulligan
    window with ``policy`` — expected to keep hands — and returns with the first
    PRIORITY decision still outstanding, so the caller can inject the scenario
    (injection invalidates it; the caller refetches).

    Deterministic: the answers are exactly the policy's, and the loop is bounded
    by :data:`MAX_PREGAME_DECISIONS`, so a non-progressing engine fails cleanly
    instead of spinning.

    Raises:
        ValueError: the game ended in the pre-game window, or the decision bound
            was hit without reaching a PRIORITY decision.
    """

    def _ended() -> ValueError:
        return ValueError(
            "game ended during the pre-game window (no first-turn decision)"
        )

    for _ in range(MAX_PREGAME_DECISIONS):
        if _is_over(client, game_id):
            raise _ended()
        try:
            req = client.get_decision(game_id)
        except GameNotActiveError as exc:
            raise _ended() from exc
        except StaleDecisionError:
            # The watchdog released a decision mid-drive; refetch.  The loop
            # bound still applies, so a persistently stale engine fails cleanly.
            continue
        if req.decision_type == pb.DECISION_TYPE_PRIORITY:
            # First turn has started and the engine is parked: ready to inject.
            return
        ctx = DecisionContext(request=req)
        try:
            client.submit_decision(game_id, req.decision_id, policy(ctx))
        except GameNotActiveError as exc:
            raise _ended() from exc
        except StaleDecisionError:
            continue
    raise ValueError(
        f"pre-game window did not reach a PRIORITY decision within "
        f"{MAX_PREGAME_DECISIONS} decisions"
    )


def _absolute_deck_paths(
    decks: Sequence[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Resolve deck paths to absolute paths against the invoking client's CWD.

    The harness resolves deck paths against *its own* working directory, not
    the client's, so a relative path (e.g. ``decks/goldfish_A.dck``) fails with
    ``INVALID_ARGUMENT: deck file not found``.  Making them absolute here is the
    single choke point before they reach ``start_game``.
    """
    return [(str(name), os.path.abspath(str(path))) for name, path in decks]


def _run_witness_seed(
    client: Any,
    scenario: Scenario,
    policy: Callable[[DecisionContext], Answer],
    *,
    pregame_policy: Callable[[DecisionContext], Answer],
    seed: int,
    max_iterations: int,
    max_decisions: int,
    decks: list[tuple[str, str]],
    player_types: Sequence[int],
    player: int,
    view_as_player: int,
    start_game_kwargs: dict[str, Any],
    candidate_kind: str,
    candidate_key: str,
    card_names: tuple[str, ...],
    infinite: bool,
    recorder: Callable[[Observation], None] | None = None,
) -> WitnessResult:
    reset = getattr(policy, "new_game", None)
    if callable(reset):
        reset()

    game_id: int | None = None
    trace: list[DecisionTraceEntry] = []
    observations: list[Observation] = []
    state_hash_before = ""
    state_hash_after = ""
    event_start_seq = 0
    event_end_seq = 0
    event_cursor = 0
    cast_count = 0
    spells_resolved = 0
    game_over = False

    def make_result(
        verdict: str,
        evidence: dict[str, Any],
        error: str = "",
        iterations: int = 0,
    ) -> WitnessResult:
        return WitnessResult(
            verdict=verdict,
            scenario=scenario,
            iterations=int(iterations),
            signatures=[o.signature for o in observations],
            resource_deltas=_resource_deltas(observations),
            state_hash_before=state_hash_before,
            state_hash_after=state_hash_after,
            event_start_seq=event_start_seq,
            event_end_seq=event_end_seq,
            decision_trace=trace,
            seed=int(seed),
            seeds=[int(seed)],
            observations=observations,
            evidence=evidence,
            error=error,
            candidate_kind=candidate_kind,
            candidate_key=candidate_key,
            card_names=card_names,
            infinite=infinite,
        )

    try:
        game_id = int(
            client.start_game(
                decks,
                seed,
                player_types=list(player_types),
                **start_game_kwargs,
            )
        )
        # setup_scenario rejects the pre-game/mulligan window (the first turn
        # must have started), so drive the mulligan decisions to the first
        # PRIORITY decision before injecting.
        _drive_pregame(client, game_id, pregame_policy)
        # SetupScenario needs a LIVE outstanding decision (the engine is parked
        # on the first PRIORITY decision).
        state_hash_before = str(
            client.get_state(game_id, view_as_player=view_as_player).state_hash
        )
        event_cursor = int(client.poll_events(game_id, 0).next_cursor)
        event_start_seq = event_cursor
        client.setup_scenario(game_id, scenario)
        # Injection invalidated the decision: refetch before submitting.
        client.get_decision(game_id)

        decisions = 0
        iterations_done = 0
        iteration_seen = 0  # watermark over policy.iterations
        triggers_seen = 0  # watermark over the policy's total trigger credit
        spin_samples = 0  # consecutive no-progress observations
        forced_passes = 0  # phase-advancing passes forced by the spin guard
        phase_seen = False  # Phase/TurnStarted event since the last sample
        last_was_phase = False  # previous appended sample came from the phase path

        def _trigger_total() -> int:
            """Current total trigger credit (0 for policies without the field)."""
            hits = getattr(policy, "trigger_hits", None)
            if hits is None:
                return 0
            try:
                return int(sum(hits))
            except (TypeError, ValueError):
                return 0

        def credit_events() -> int:
            """Poll new events, update counters, credit triggers; return total.

            Split out of :func:`capture_observation` so the decision loop can
            poll (and credit) triggers between observations without sampling the
            game state.  Only ``SpellCast``/``SpellResolved`` events from the
            combo player are credited via ``note_card_event``: automatic
            abilities and triggers never pass through a PRIORITY decision, so
            this is the only signal that they executed.  A ``Phase`` or
            ``TurnStarted`` event in the batch also raises the ``phase_seen``
            flag, which the decision loop turns into a phase/turn-boundary
            sample (see below).
            """
            nonlocal event_cursor, cast_count, spells_resolved, phase_seen
            note_event = getattr(policy, "note_card_event", None)
            batch = client.poll_events(game_id, event_cursor)
            for event in batch.events:
                if event.type in ("Phase", "TurnStarted"):
                    phase_seen = True
                if event.type not in ("SpellCast", "SpellResolved"):
                    continue
                if event.type == "SpellCast":
                    cast_count += 1
                else:
                    spells_resolved += 1
                if callable(note_event) and int(event.player) == player:
                    note_event(str(getattr(event, "card_name", "") or ""))
            event_cursor = int(batch.next_cursor)
            return _trigger_total()

        def capture_observation(
            label: int, *, skip_duplicate_hash: bool = False
        ) -> bool:
            """Sample the (quiescent) state + new events into observations.

            Called once *before* the loop (baseline), once per completed
            iteration, and once per new-trigger boundary, so a trigger-driven
            line still records the "before" and "after" samples the detector
            needs.  Also advances the ``triggers_seen`` watermark and tracks
            the run of no-progress samples that flags a pilot spin.

            With ``skip_duplicate_hash`` (used by the phase/turn-boundary path)
            a sample whose non-empty ``state_hash`` repeats the immediately
            previous sample is dropped: consecutive bit-identical samples from
            a phase boundary would otherwise feed ``detect_loop``'s degenerate
            branch and certify a loop the engine never made.  The same guard
            also applies to a *later* sample that repeats the phase-boundary
            sample it follows (a policy iteration that completed without
            changing the board): a phase boundary must never leave a duplicate
            in its wake.  Policy-iteration samples that repeat another
            policy-iteration sample are left untouched, exactly as before.
            Returns True when a sample was appended.
            """
            nonlocal triggers_seen, spin_samples, phase_seen, last_was_phase
            state = client.get_state(game_id, view_as_player=view_as_player)
            triggers_seen = credit_events()
            observation = build_observation(
                label,
                state,
                cast_count=cast_count,
                spells_resolved=spells_resolved,
                event_seq=event_cursor,
            )
            duplicate = bool(
                observation.state_hash
                and observations
                and observations[-1].state_hash == observation.state_hash
            )
            if duplicate and (skip_duplicate_hash or last_was_phase):
                phase_seen = False
                return False
            observations.append(observation)
            if recorder is not None:
                recorder(observation)
            last_was_phase = bool(skip_duplicate_hash)
            # A spin is a consecutive pair with an unchanged (non-empty)
            # structural signature and no game-state resource growth.  Only the
            # event counters may grow; a genuine loop grows tokens/mana/life/...
            if len(observations) >= 2:
                previous = observations[-2]
                if (
                    observation.signature
                    and observation.signature == previous.signature
                    and not _game_state_grew(previous.resources, observation.resources)
                ):
                    spin_samples += 1
                else:
                    spin_samples = 0
            phase_seen = False
            return True

        # Pre-loop baseline sample.
        capture_observation(0)

        while iterations_done < max_iterations and decisions < max_decisions:
            if _is_over(client, game_id):
                game_over = True
                break
            try:
                req = client.get_decision(game_id)
            except GameNotActiveError:
                game_over = True
                break
            except StaleDecisionError:
                continue
            ctx = DecisionContext(request=req)
            answer = policy(ctx)
            # Spin guard: once the combo player's own priority decisions have
            # produced ``SPIN_THRESHOLD`` consecutive no-progress samples, a
            # no-op action is being repeated instead of advancing the phase.
            # Take the engine's "pass" option instead of the policy's answer so
            # the step/turn can progress (e.g. into combat, where a beginning-
            # of-combat trigger can fire).  Bounded by ``MAX_FORCED_PASSES``.
            force_pass = (
                forced_passes < MAX_FORCED_PASSES
                and spin_samples >= SPIN_THRESHOLD
                and req.decision_type == pb.DECISION_TYPE_PRIORITY
                and req.player == player
            )
            pass_option = _pass_option(req.options) if force_pass else None
            if pass_option is not None:
                answer = ("option_id", int(pass_option.id))
                forced_passes += 1
                spin_samples = 0  # fresh evidence needed for the next force
                note = (
                    f"forced pass {forced_passes}/{MAX_FORCED_PASSES} after "
                    f"{SPIN_THRESHOLD} spinning samples "
                    f"(turn {req.turn}, phase {req.phase or '?'})"
                )
                policy_notes = getattr(policy, "notes", None)
                if isinstance(policy_notes, list):
                    policy_notes.append(note)
            try:
                client.submit_decision(game_id, req.decision_id, answer)
            except GameNotActiveError:
                game_over = True
                break
            except StaleDecisionError:
                continue
            trace.append((req.decision_id, req.decision_type, answer))
            decisions += 1

            completed = int(getattr(policy, "iterations", 0))
            while iteration_seen < completed and iterations_done < max_iterations:
                iteration_seen += 1
                iterations_done += 1
                capture_observation(iterations_done)

            # Advance until trigger: a trigger-driven line rarely completes a
            # policy iteration, so poll for newly credited triggers after every
            # decision.  A trigger increase is an observation boundary (the
            # ``capture_observation`` call polls nothing new; it only samples
            # state), which keeps the judge supplied past the single baseline
            # sample.  Only while the policy has completed no iteration:
            # otherwise the extra mid-iteration samples would feed the detector
            # non-quiescent states and reintroduce the degenerate/recurrence
            # false positives the acceptance matrix pins.  Bounded by
            # ``max_iterations``.
            if iterations_done < max_iterations and iteration_seen == 0:
                if credit_events() > triggers_seen:
                    iterations_done += 1
                    capture_observation(iterations_done)

            # Phase/turn boundary: poll once per decision so a phase or turn
            # change that arrives *after* the policy has stopped completing
            # iterations is still observed.  Deliberately not gated on
            # ``iteration_seen``: the run this fixes completes its iterations
            # and then stops advancing, so the trigger boundary never fires and
            # the judge was left with a single post-baseline sample.  The
            # duplicate guard drops a sample whose non-empty ``state_hash``
            # repeats the previous one (a phase boundary that did not change the
            # board), which keeps consecutive identical samples out of
            # ``detect_loop``.  Bounded by ``max_iterations`` like every other
            # observation boundary.
            if iterations_done < max_iterations:
                credit_events()
                if phase_seen and capture_observation(
                    iterations_done + 1, skip_duplicate_hash=True
                ):
                    iterations_done += 1

        # Post-loop sample only when the loop recorded no completed iteration:
        # a zero-iteration run still carries two observations (best-effort: a
        # stopped engine may reject GetState).
        if len(observations) < 2:
            try:
                capture_observation(iterations_done + 1)
            except Exception:  # noqa: BLE001 - diagnostic sample only
                logger.debug("post-loop observation sample failed", exc_info=True)

        state_hash_after = str(
            client.get_state(game_id, view_as_player=view_as_player).state_hash
        )
        event_end_seq = int(client.poll_events(game_id, event_cursor).next_cursor)
        diagnostics = _policy_diagnostics(policy)
        verdict, evidence = detect_loop(observations)
        evidence = {
            **evidence,
            "diagnostics": diagnostics,
            "forced_passes": int(forced_passes),
        }
        if int(diagnostics.get("executed_actions", 0)) == 0:
            # The policy never matched a single offered option to a link: report
            # the diagnostic instead of a silent zero-iteration result.
            verdict = "inconclusive"
            evidence = {
                **evidence,
                "reason": (
                    "policy never matched an offered option to a link source; "
                    "no loop action was executed"
                ),
            }
        elif verdict == "no_loop" and game_over:
            verdict = "refuted"
            evidence = {**evidence, "game_over": True}
        return make_result(verdict, evidence, iterations=iterations_done)
    except Exception as exc:  # noqa: BLE001 - surfaced as the "error" verdict
        logger.warning("witness run failed (seed=%s): %s", seed, exc)
        return make_result(
            "error", {"error": f"{type(exc).__name__}: {exc}"}, error=str(exc)
        )
    finally:
        if game_id is not None:
            try:
                client.stop_game(game_id)
            except Exception as cleanup_err:  # best effort
                logger.warning("best-effort stop_game(%s) failed: %s", game_id, cleanup_err)


def run_witness(
    client: Any,
    scenario: Scenario,
    policy: Callable[[DecisionContext], Answer],
    *,
    seeds: int | Sequence[int],
    max_iterations: int = 4,
    max_decisions: int = 256,
    decks: Sequence[tuple[str, str]] | None = None,
    player_types: Sequence[int] | None = None,
    player: int = 0,
    view_as_player: int | None = None,
    start_game_kwargs: dict[str, Any] | None = None,
    candidate_kind: str = "",
    candidate_key: str = "",
    card_names: Sequence[str] = (),
    infinite: bool = False,
    stop_on_loop: bool = True,
    pregame_policy: Callable[[DecisionContext], Answer] | None = None,
    recorder: Callable[[Observation], None] | None = None,
) -> WitnessResult:
    """Start a game, inject ``scenario``, and drive ``policy`` for N iterations.

    ``seeds`` may be a single int or a sequence.  Each seed runs its own game;
    with ``stop_on_loop`` the first ``loops`` verdict is returned immediately,
    otherwise the strongest verdict across seeds is returned (preference:
    loops > inconclusive > no_loop > refuted > error).

    Deck paths are resolved to absolute paths against the invoking CWD before
    ``start_game`` (the harness resolves them against its own CWD).  The
    pre-game/mulligan window is driven to the first PRIORITY decision with
    ``pregame_policy`` (default :func:`runner.default_policy`, which keeps
    hands) before the scenario is injected; see :func:`_drive_pregame`.

    ``recorder``, when given, is called synchronously with each
    :class:`Observation` at the moment it is captured (the pre-loop baseline
    and every iteration/trigger/phase sample).  This is the live hook the
    ``combo-witness --persist`` CLI uses to append observations as they happen;
    ``None`` leaves behaviour unchanged.
    """
    seed_list = [int(seeds)] if isinstance(seeds, int) else [int(s) for s in seeds]
    if not seed_list:
        raise ValueError("run_witness needs at least one seed")
    deck_list: list[tuple[str, str]] = _absolute_deck_paths(
        [(str(name), str(path)) for name, path in (decks or DEFAULT_WITNESS_DECKS)]
    )
    types = list(player_types) if player_types is not None else list(DEFAULT_PLAYER_TYPES)
    view = player if view_as_player is None else int(view_as_player)
    pregame = pregame_policy or default_policy

    results: list[WitnessResult] = []
    for seed in seed_list:
        result = _run_witness_seed(
            client,
            scenario,
            policy,
            pregame_policy=pregame,
            seed=seed,
            max_iterations=int(max_iterations),
            max_decisions=int(max_decisions),
            decks=deck_list,
            player_types=types,
            player=int(player),
            view_as_player=int(view),
            start_game_kwargs=dict(start_game_kwargs or {}),
            candidate_kind=candidate_kind,
            candidate_key=candidate_key,
            card_names=tuple(card_names),
            infinite=bool(infinite),
            recorder=recorder,
        )
        results.append(result)
        if stop_on_loop and result.verdict == "loops":
            return result

    for verdict in ("loops", "inconclusive", "no_loop", "refuted", "error"):
        for result in results:
            if result.verdict == verdict:
                # Aggregate the seed list for the winning verdict.
                result.seeds = seed_list
                return result
    return results[-1]


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def start_witness_recording(
    store: Any,
    *,
    scenario: Scenario,
    seeds: Sequence[int],
    params: dict[str, Any] | None = None,
    engine_commit: str = "",
    proto_version: int = PROTOCOL_VERSION,
    policy_version: str = WITNESS_POLICY_VERSION,
    notes: str = "",
) -> tuple[int, Callable[[Observation], None]]:
    """Start a ``witness_runs`` row and return ``(run_id, live_recorder)``.

    Shared by ``combo-witness --persist`` and the batch instruments: the run row
    is opened *before* the run so observations can stream to a UI, and the
    returned recorder is meant to be passed to :func:`run_witness` as
    ``recorder=``.  Finish with
    ``persist_witness(store, result, run_id=run_id, ...)``.  This is the single
    place that builds the pre-run row, so every writer records the same fields.
    """
    run_id = int(
        store.start_witness_run(
            engine_commit=engine_commit,
            proto_version=int(proto_version),
            policy_version=policy_version,
            scenario_json=scenario.canonical_json(),
            seeds=list(seeds),
            params=params or {},
            notes=notes,
        )
    )
    return run_id, observation_recorder(store, run_id)


def observation_recorder(store: Any, run_id: int) -> Callable[[Observation], None]:
    """Build a live ``recorder`` that appends each observation immediately.

    Pass the result to :func:`run_witness` (``recorder=...``) together with a
    ``witness_runs`` row started *before* the run: every captured observation is
    appended to ``witness_observations`` as it happens, which is what lets a UI
    watch a run in progress.  Each written observation is marked so
    :func:`persist_observations` will not append it a second time at end of run.
    """

    def record(observation: Observation) -> None:
        store.record_witness_observation(
            run_id,
            observation.iteration,
            turn=observation.turn,
            phase=observation.phase,
            signature=observation.signature,
            resources=observation.resources,
            event_seq=observation.event_seq,
        )
        try:
            setattr(observation, "_persisted", True)
        except Exception:  # pragma: no cover - dataclasses are mutable
            logger.debug("could not mark observation persisted", exc_info=True)

    return record


def persist_observations(store: Any, run_id: int, result: WitnessResult) -> int:
    """Append ``result``'s observations that a live recorder has not written.

    A recorder built by :func:`observation_recorder` marks each observation it
    appends; those are skipped here, so calling this after a live run is a safe
    no-op and calling it on a result captured without a recorder persists the
    whole per-step trace.  Returns the number of rows appended.
    """
    written = 0
    for observation in result.observations:
        if getattr(observation, "_persisted", False):
            continue
        store.record_witness_observation(
            run_id,
            observation.iteration,
            turn=observation.turn,
            phase=observation.phase,
            signature=observation.signature,
            resources=observation.resources,
            event_seq=observation.event_seq,
        )
        try:
            setattr(observation, "_persisted", True)
        except Exception:  # pragma: no cover - dataclasses are mutable
            logger.debug("could not mark observation persisted", exc_info=True)
        written += 1
    return written


def persist_witness(
    store: Any,
    result: WitnessResult,
    *,
    engine_commit: str = "",
    proto_version: int = PROTOCOL_VERSION,
    policy_version: str = WITNESS_POLICY_VERSION,
    params: dict[str, Any] | None = None,
    notes: str = "",
    run_id: int | None = None,
) -> tuple[int, int]:
    """Append a witness run + result to the store; return ``(run_id, result_id)``.

    ``run_id`` may be passed when the ``witness_runs`` row was already started
    before the run (the live ``combo-witness --persist`` path); otherwise a new
    run row is appended.  Per-step observations not already written live are
    appended via :func:`persist_observations`, and ``result.evidence`` (plus its
    ``diagnostics`` sub-dict) is stored on the result row.
    """
    if run_id is None:
        active_id = int(
            store.start_witness_run(
                engine_commit=engine_commit,
                proto_version=int(proto_version),
                policy_version=policy_version,
                scenario_json=result.scenario.canonical_json(),
                seeds=result.seeds or [result.seed],
                params=params or {},
                notes=notes,
            )
        )
    else:
        active_id = int(run_id)
    persist_observations(store, active_id, result)
    diagnostics: Any = None
    if isinstance(result.evidence, dict):
        diagnostics = result.evidence.get("diagnostics")
    result_id = store.record_witness_result(
        active_id,
        candidate_kind=result.candidate_kind,
        candidate_key=result.candidate_key,
        card_names=result.card_names,
        verdict=result.verdict,
        infinite=result.infinite,
        iterations=result.iterations,
        signature=result.signatures,
        resource_deltas=result.resource_deltas,
        state_hash_before=result.state_hash_before,
        state_hash_after=result.state_hash_after,
        event_start_seq=result.event_start_seq,
        event_end_seq=result.event_end_seq,
        trace=result.decision_trace,
        evidence=result.evidence if result.evidence else None,
        diagnostics=diagnostics,
    )
    return active_id, result_id


def persist_error_result(
    store: Any,
    run_id: int,
    *,
    scenario: Scenario,
    error: Any,
    candidate_kind: str = "",
    candidate_key: str = "",
    card_names: Sequence[str] = (),
    seed: int = 0,
    seeds: Sequence[int] | None = None,
) -> int:
    """Persist an ``error`` verdict for a run that raised before returning one.

    Guarantees a ``witness_runs`` row started by :func:`start_witness_recording`
    is never left without a result.  Reuses :func:`persist_witness`, so any
    observations already streamed live are kept and the error reason lands in
    ``evidence_json``.  Returns the result id.
    """
    result = WitnessResult(
        verdict="error",
        scenario=scenario,
        iterations=0,
        seed=int(seed),
        seeds=[int(s) for s in (seeds if seeds is not None else [seed])],
        evidence={"error": f"{type(error).__name__}: {error}"},
        error=str(error),
        candidate_kind=candidate_kind,
        candidate_key=candidate_key,
        card_names=tuple(card_names),
    )
    _, result_id = persist_witness(store, result, run_id=int(run_id))
    return result_id


# ---------------------------------------------------------------------------
# Candidate loading + CLI
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    """A combo hypothesis resolved to card names and a synthetic line."""

    cards: tuple[str, ...]
    card_ids: tuple[int, ...] = ()
    kind: str = "pair"
    key: str = ""
    pattern: str = ""
    mechanism: str = ""
    infinite: bool = False
    type_lines: tuple[str, ...] = ()
    oracle_texts: tuple[str, ...] = ()

    @property
    def links(self) -> list[LinkPlan]:
        return synthetic_cycle(self.cards)


def _parse_card_ids(raw: Any) -> list[int]:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if isinstance(raw, (list, tuple)):
        ids: list[int] = []
        for value in raw:
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                continue
        return ids
    return []


def load_candidate(
    store: Any, *, candidate: str | None = None, card: str | None = None
) -> Candidate:
    """Resolve ``--candidate`` (hypothesis id) or ``--card`` (name) from the DB.

    Raises ``LookupError`` when nothing matches.  The returned candidate uses a
    synthetic ping-pong line; callers with the ontology graph should build the
    real ``Combo`` instead.
    """
    conn = store._conn
    import_id: str | None = None
    row: Any = None
    if candidate is not None:
        row = conn.execute(
            "SELECT h.id, h.import_id, h.card_ids_json, h.mechanism, p.name AS pattern "
            "FROM combo_hypotheses h LEFT JOIN patterns p ON p.id = h.pattern_id "
            "WHERE h.id = ?",
            (int(candidate),),
        ).fetchone()
        if row is None:
            raise LookupError(f"no combo_hypotheses row with id={candidate}")
        import_id = row["import_id"]
    elif card is not None:
        normal = str(card).strip().lower()
        rows = conn.execute(
            "SELECT h.id, h.import_id, h.card_ids_json, h.mechanism, p.name AS pattern "
            "FROM combo_hypotheses h LEFT JOIN patterns p ON p.id = h.pattern_id "
            "ORDER BY h.score DESC"
        ).fetchall()
        hit: Any = None
        for candidate_row in rows:
            ids = _parse_card_ids(candidate_row["card_ids_json"])
            names = _names_for_ids(conn, candidate_row["import_id"], ids)
            if normal in {n.lower() for n in names}:
                hit = candidate_row
                break
        if hit is None:
            raise LookupError(f"no combo hypothesis contains card {card!r}")
        row = hit
        import_id = row["import_id"]
    else:
        raise ValueError("load_candidate needs candidate=<id> or card=<name>")

    ids = _parse_card_ids(row["card_ids_json"])
    names = _names_for_ids(conn, import_id, ids)
    type_lines = _type_lines_for_ids(conn, import_id, ids)
    oracle_texts = _oracle_texts_for_ids(conn, import_id, ids)
    return Candidate(
        cards=tuple(names),
        card_ids=tuple(ids),
        kind="pair" if len(names) <= 2 else "cycle",
        key=str(row["id"]),
        pattern=str(row["pattern"] or ""),
        mechanism=str(row["mechanism"] or ""),
        type_lines=tuple(type_lines),
        oracle_texts=tuple(oracle_texts),
    )


def _names_for_ids(conn: Any, import_id: str | None, ids: Sequence[int]) -> list[str]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    sql = (
        f"SELECT id, name FROM cards WHERE id IN ({placeholders}) "  # noqa: S608
    )
    params: list[Any] = [int(i) for i in ids]
    if import_id is not None:
        sql += "AND import_id = ?"
        params.append(import_id)
    rows = conn.execute(sql, params).fetchall()
    by_id = {int(r["id"]): str(r["name"]) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def _type_lines_for_ids(
    conn: Any, import_id: str | None, ids: Sequence[int]
) -> list[str]:
    """Type lines for ``ids`` in the same order/filter as :func:`_names_for_ids`."""
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    sql = (
        f"SELECT id, type_line FROM cards WHERE id IN ({placeholders}) "  # noqa: S608
    )
    params: list[Any] = [int(i) for i in ids]
    if import_id is not None:
        sql += "AND import_id = ?"
        params.append(import_id)
    rows = conn.execute(sql, params).fetchall()
    by_id = {int(r["id"]): str(r["type_line"] or "") for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def _oracle_texts_for_ids(
    conn: Any, import_id: str | None, ids: Sequence[int]
) -> list[str]:
    """Oracle texts for ``ids`` in the same order/filter as :func:`_names_for_ids`."""
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    sql = (
        f"SELECT id, oracle_text FROM cards WHERE id IN ({placeholders}) "  # noqa: S608
    )
    params: list[Any] = [int(i) for i in ids]
    if import_id is not None:
        sql += "AND import_id = ?"
        params.append(import_id)
    rows = conn.execute(sql, params).fetchall()
    by_id = {int(r["id"]): str(r["oracle_text"] or "") for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def _parse_seeds(raw: str) -> list[int]:
    return [int(part) for part in str(raw).split(",") if part.strip()]


def _parse_decks(raw: str | None) -> list[tuple[str, str]] | None:
    if not raw:
        return None
    decks: list[tuple[str, str]] = []
    for part in str(raw).split(","):
        if not part.strip():
            continue
        if "=" in part:
            name, path = part.split("=", 1)
        else:
            name, path = part.strip(), part.strip()
        decks.append((name.strip(), path.strip()))
    return decks or None


def main(argv: list[str] | None = None) -> int:
    """``combo-witness`` CLI: stage/verify a candidate combo loop."""
    import argparse
    from pathlib import Path

    from .store import ExperimentStore

    parser = argparse.ArgumentParser(
        prog="combo-witness",
        description="Scenario-driven witness search over a candidate combo.",
    )
    parser.add_argument("--db", default="./research.db", help="SQLite research DB")
    parser.add_argument("--candidate", default=None, help="combo_hypotheses.id")
    parser.add_argument("--card", default=None, help="card name (find a hypothesis)")
    parser.add_argument("--seeds", default="1", help="comma-separated seeds")
    parser.add_argument("--max-iterations", type=int, default=4)
    parser.add_argument("--max-decisions", type=int, default=256)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the canonical scenario without contacting a harness",
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=50051)
    parser.add_argument(
        "--decks", default=None,
        help="comma-separated name=path deck specs for a live run",
    )
    parser.add_argument("--persist", action="store_true", help="append the result to the DB")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)
    seeds = _parse_seeds(args.seeds)

    store = ExperimentStore(Path(args.db))
    try:
        combo = load_candidate(store, candidate=args.candidate, card=args.card)
    finally:
        store.close()

    scenario = build_scenario(combo)
    if args.dry_run:
        if args.json:
            print(json.dumps(scenario.canonical(), indent=2, sort_keys=True))
        else:
            print(f"candidate {combo.key} [{combo.pattern}]: {' + '.join(combo.cards)}")
            print(f"scenario hash: {scenario.scenario_hash()}")
            print(json.dumps(scenario.canonical(), indent=2, sort_keys=True))
        return 0

    decks = _parse_decks(args.decks) or DEFAULT_WITNESS_DECKS
    policy = WitnessPolicy(combo)

    # With --persist the witness_runs row is started *before* the run and a
    # recorder streams each observation into witness_observations as it is
    # captured, so a UI can watch the run live.  The result row is appended at
    # the end (same run id).
    params = {
        "max_iterations": int(args.max_iterations),
        "max_decisions": int(args.max_decisions),
    }
    store = ExperimentStore(Path(args.db)) if args.persist else None
    run_id: int | None = None
    recorder = None
    if store is not None:
        run_id, recorder = start_witness_recording(
            store, scenario=scenario, seeds=seeds, params=params
        )

    try:
        with ForgeEnvClient(host=args.host, port=args.port) as client:
            client.connect()
            result = run_witness(
                client,
                scenario,
                policy,
                seeds=seeds,
                max_iterations=args.max_iterations,
                max_decisions=args.max_decisions,
                decks=decks,
                candidate_kind=combo.kind,
                candidate_key=combo.key,
                card_names=combo.cards,
                infinite=combo.infinite,
                recorder=recorder,
            )
        result_id: int | None = None
        if store is not None:
            run_id, result_id = persist_witness(
                store, result, params=params, run_id=run_id
            )
    finally:
        if store is not None:
            store.close()

    if args.json:
        print(json.dumps({
            "verdict": result.verdict,
            "iterations": result.iterations,
            "signatures": result.signatures,
            "resource_deltas": result.resource_deltas,
            "state_hash_before": result.state_hash_before,
            "state_hash_after": result.state_hash_after,
            "event_start_seq": result.event_start_seq,
            "event_end_seq": result.event_end_seq,
            "evidence": result.evidence,
            "error": result.error,
            "run_id": run_id,
            "result_id": result_id,
        }, indent=2, sort_keys=True))
    else:
        print(f"verdict: {result.verdict}  iterations: {result.iterations}")
        if result.error:
            print(f"error: {result.error}")
        for i, delta in enumerate(result.resource_deltas):
            changed = {k: v for k, v in delta.items() if v}
            print(f"  iteration {i}: {result.signatures[i][:12]}  {changed}")
        if run_id is not None:
            print(f"persisted: run={run_id} result={result_id}")
    return 0


__all__ = [
    "Candidate",
    "CardSpec",
    "DEFAULT_GRAVEYARD",
    "DEFAULT_WITNESS_DECKS",
    "LinkPlan",
    "MAX_FORCED_PASSES",
    "MAX_PREGAME_DECISIONS",
    "Observation",
    "PlayerScenario",
    "Scenario",
    "SPIN_THRESHOLD",
    "WITNESS_POLICY_VERSION",
    "WitnessPolicy",
    "WitnessResult",
    "build_observation",
    "build_scenario",
    "detect_loop",
    "is_graveyard_gated",
    "link_plans",
    "load_candidate",
    "main",
    "observation_recorder",
    "persist_error_result",
    "persist_observations",
    "persist_witness",
    "resource_totals",
    "run_witness",
    "synthetic_cycle",
    "witness_signature",
]
