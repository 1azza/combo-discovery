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
WITNESS_POLICY_VERSION = "witness-v1"

#: Generous default starting pool: enough to begin most loops.  Callers that
#: know the line's per-iteration cost should override it.
DEFAULT_MANA_PER_COLOR = 8
DEFAULT_MANA_COLORS = ("W", "U", "B", "R", "G", "C")
DEFAULT_LIFE = 20
DEFAULT_OPPONENT_LIFE = 20

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
GROWTH_KEYS = (
    "mana",
    "tokens",
    "life",
    "damage",
    "casts",
    "spells_resolved",
    "extra_phases",
    "permanents",
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

    def canonical(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "set": self.set,
            "tapped": self.tapped,
            "summoning_sick": self.summoning_sick,
            "counters": {str(k): int(v) for k, v in sorted(self.counters.items())},
            "damage": int(self.damage),
            "no_etb_triggers": self.no_etb_triggers,
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
    * the opponent is harmless: ``opponent_life``, empty zones.
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

    mana_map = (
        {str(k): int(v) for k, v in mana.items()}
        if mana is not None
        else {color: DEFAULT_MANA_PER_COLOR for color in DEFAULT_MANA_COLORS}
    )
    combo_player = PlayerScenario(
        player=int(player),
        life=int(life),
        mana=mana_map,
        battlefield=battlefield,
        hand=starting_hand,
        graveyard=list(graveyard or []),
        library=list(library or []),
    )
    harmless = PlayerScenario(
        player=int(opponent),
        life=int(opponent_life),
        mana={},
        library=list(opponent_library or []),
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
    to :func:`runner.default_policy`.  The cursor wraps, so one full pass is one
    loop iteration.  Progress is bounded: a stall counter force-advances the
    cursor when no link ever matches, so the policy cannot spin forever.
    """

    def __init__(
        self,
        combo: Any = None,
        *,
        links: Sequence[LinkPlan] | None = None,
        player: int = 0,
        max_stall: int = 16,
        fallback: Callable[[DecisionContext], Answer] | None = None,
    ):
        self.player = player
        self.links = list(links) if links is not None else link_plans(combo)
        self._fallback = fallback or default_policy
        self.max_stall = max(1, int(max_stall))
        self.cursor = 0
        self.active: LinkPlan | None = None
        self.iterations = 0
        self.decisions = 0
        self.stall = 0
        self.answers: list[Answer] = []

    def new_game(self) -> None:
        """Reset per-game state (run_game/run_witness call this on a new game)."""
        self.cursor = 0
        self.active = None
        self.iterations = 0
        self.decisions = 0
        self.stall = 0
        self.answers = []

    # -- cursor -------------------------------------------------------------

    def _advance(self) -> None:
        if not self.links:
            return
        self.cursor += 1
        if self.cursor >= len(self.links):
            self.cursor = 0
            self.iterations += 1

    @staticmethod
    def _match_option(options: Sequence[pb.Option], card_name: str) -> pb.Option | None:
        target = (card_name or "").strip().lower()
        if not target:
            return None
        for option in options:
            if (option.card_name or "").strip().lower() == target:
                return option
        # Description fallback on word boundaries only, so a one-letter card
        # name cannot match the "a" inside "Activate".
        edge = rf"(?<![A-Za-z0-9]){re.escape(target)}(?![A-Za-z0-9])"
        for option in options:
            text = f"{option.card_name} {option.description}".lower()
            if re.search(edge, text):
                return option
        return None

    @staticmethod
    def _candidate_id(ctx: DecisionContext, name: str) -> int | None:
        low = str(name).strip().lower()
        for candidate in ctx.candidates:
            if (candidate.name or "").strip().lower() == low:
                return int(candidate.card_id)
        return None

    # -- answers ------------------------------------------------------------

    def _priority(self, ctx: DecisionContext) -> Answer:
        if not self.links:
            return self._fallback(ctx)
        link = self.links[self.cursor]
        option = self._match_option(ctx.options, link.src)
        if option is None:
            # Search forward for a later link that is currently available.
            for offset in range(1, len(self.links) + 1):
                idx = (self.cursor + offset) % len(self.links)
                candidate = self.links[idx]
                option = self._match_option(ctx.options, candidate.src)
                if option is not None:
                    self.cursor = idx
                    link = candidate
                    break
        if option is None:
            self.stall += 1
            if self.stall >= self.max_stall:
                self.stall = 0
                self._advance()
            return self._fallback(ctx)
        self.stall = 0
        self.active = link
        self._advance()
        return ("option_id", int(option.id))

    def _from_active(self, ctx: DecisionContext) -> Answer | None:
        if self.active is None:
            return None
        params = self.active.params or {}
        t = ctx.decision_type
        if t == pb.DECISION_TYPE_CHOOSE_TARGETS:
            raw = params.get("targets")
            if raw is None:
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
                return None
            ids = [self._candidate_id(ctx, str(n)) for n in raw]
            return ("card_ids", [i for i in ids if i is not None])
        if t in (pb.DECISION_TYPE_CHOOSE_MODE, pb.DECISION_TYPE_OPTIONAL_COSTS):
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


def _zone_count(zone: Any) -> int:
    total = 0
    for card in getattr(zone, "cards", ()):  # CardRef(name, count)
        count = int(getattr(card, "count", 0) or 0)
        total += count if count > 0 else 1
    return total


def _battlefield_entries(state: pb.FullState) -> list[tuple[Any, ...]]:
    permanents = list(getattr(state, "battlefield_cards", ()) or ())
    entries: list[tuple[Any, ...]] = []
    for zone in state.battlefield:
        for pid in zone.permanents:
            if 0 <= pid < len(permanents):
                perm = permanents[pid]
                counters = tuple(
                    sorted((str(c.type), int(c.count)) for c in perm.typed_counters)
                )
                entries.append(
                    (
                        str(perm.card_name),
                        bool(perm.tapped),
                        bool(perm.is_token),
                        counters,
                    )
                )
            else:  # harness without a flat battlefield listing: fall back to names
                for card in zone.cards:
                    entries.append((str(card.name), False, False, ()))
    entries.sort()
    return entries


def witness_signature(state: pb.FullState) -> dict[str, Any]:
    """The structural projection that must recur for a loop.

    Captures battlefield names/tapped/token/counters, phase, active player,
    per-player life, zone counts and the stack shape.  Monotonic *scalars*
    (mana pool, total damage, cast counts) are deliberately excluded so they can
    accumulate across iterations without changing the signature.  ``turn`` is
    also excluded (a turn-cycling loop should recur).  Library order is not
    exposed by FullState v2 and is represented only as a count.
    """
    return {
        "phase": state.phase,
        "active_player": state.active_player,
        "life": [int(v) for v in state.life],
        "battlefield": _battlefield_entries(state),
        "zone_counts": {
            "hand": [_zone_count(z) for z in state.hand],
            "battlefield": [len(list(z.permanents)) for z in state.battlefield],
            "graveyard": [_zone_count(z) for z in state.graveyard],
            "library": [_zone_count(z) for z in state.library],
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


def detect_loop(
    observations: Sequence[Observation],
) -> tuple[str, dict[str, Any]]:
    """Classify a witness run from its per-iteration observations.

    Returns ``(verdict, evidence)`` with verdict one of ``loops`` / ``no_loop``
    / ``inconclusive``.

    Rule:

    * ``inconclusive`` with fewer than two observations;
    * ``loops`` (degenerate) when two CONSECUTIVE observations share a
      non-empty identical ``state_hash`` (the whole state is bit-identical);
    * ``loops`` when two observations share a non-empty witness signature and a
      tracked resource grew between them;
    * ``no_loop`` when a signature recurs but nothing grew, or no signature ever
      recurs.
    """
    if len(observations) < 2:
        return "inconclusive", {
            "reason": "need at least two observations",
            "n": len(observations),
        }

    for i in range(1, len(observations)):
        prev_hash = observations[i - 1].state_hash
        curr_hash = observations[i].state_hash
        if prev_hash and curr_hash and prev_hash == curr_hash:
            return "loops", {
                "kind": "degenerate",
                "pair": [i - 1, i],
                "state_hash": curr_hash,
            }

    first_repeat: tuple[int, int] | None = None
    for j in range(1, len(observations)):
        for i in range(j):
            sig = observations[i].signature
            if not sig or sig != observations[j].signature:
                continue
            grown = _grown_between(
                observations[i].resources, observations[j].resources
            )
            if grown:
                return "loops", {
                    "kind": "recurrence",
                    "pair": [i, j],
                    "signature": sig,
                    "grown": grown,
                }
            if first_repeat is None:
                first_repeat = (i, j)

    if first_repeat is not None:
        return "no_loop", {
            "reason": "signature recurred but no tracked resource grew",
            "pair": list(first_repeat),
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

    def make_result(verdict: str, evidence: dict[str, Any], error: str = "") -> WitnessResult:
        return WitnessResult(
            verdict=verdict,
            scenario=scenario,
            iterations=len(observations),
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
        iteration_seen = 0
        while len(observations) < max_iterations and decisions < max_decisions:
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
            while iteration_seen < completed and len(observations) < max_iterations:
                iteration_seen += 1
                state = client.get_state(game_id, view_as_player=view_as_player)
                batch = client.poll_events(game_id, event_cursor)
                for event in batch.events:
                    if event.type == "SpellCast":
                        cast_count += 1
                    elif event.type == "SpellResolved":
                        spells_resolved += 1
                event_cursor = int(batch.next_cursor)
                observations.append(
                    build_observation(
                        len(observations),
                        state,
                        cast_count=cast_count,
                        spells_resolved=spells_resolved,
                        event_seq=event_cursor,
                    )
                )

        state_hash_after = str(
            client.get_state(game_id, view_as_player=view_as_player).state_hash
        )
        event_end_seq = int(client.poll_events(game_id, event_cursor).next_cursor)
        verdict, evidence = detect_loop(observations)
        if verdict == "no_loop" and game_over:
            verdict = "refuted"
            evidence = {**evidence, "game_over": True}
        return make_result(verdict, evidence)
    except Exception as exc:  # noqa: BLE001 - surfaced as the "error" verdict
        logger.warning("witness run failed (seed=%s): %s", seed, exc)
        return make_result("error", {"error": f"{type(exc).__name__}: {exc}"}, error=str(exc))
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


def persist_witness(
    store: Any,
    result: WitnessResult,
    *,
    engine_commit: str = "",
    proto_version: int = PROTOCOL_VERSION,
    policy_version: str = WITNESS_POLICY_VERSION,
    params: dict[str, Any] | None = None,
    notes: str = "",
) -> tuple[int, int]:
    """Append a witness run + result to the store; return ``(run_id, result_id)``."""
    run_id = store.start_witness_run(
        engine_commit=engine_commit,
        proto_version=int(proto_version),
        policy_version=policy_version,
        scenario_json=result.scenario.canonical_json(),
        seeds=result.seeds or [result.seed],
        params=params or {},
        notes=notes,
    )
    result_id = store.record_witness_result(
        run_id,
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
    )
    return run_id, result_id


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
    return Candidate(
        cards=tuple(names),
        card_ids=tuple(ids),
        kind="pair" if len(names) <= 2 else "cycle",
        key=str(row["id"]),
        pattern=str(row["pattern"] or ""),
        mechanism=str(row["mechanism"] or ""),
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
        )

    if args.persist:
        store = ExperimentStore(Path(args.db))
        try:
            run_id, result_id = persist_witness(store, result)
        finally:
            store.close()
    else:
        run_id = result_id = None

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
    "DEFAULT_WITNESS_DECKS",
    "LinkPlan",
    "MAX_PREGAME_DECISIONS",
    "Observation",
    "PlayerScenario",
    "Scenario",
    "WITNESS_POLICY_VERSION",
    "WitnessPolicy",
    "WitnessResult",
    "build_observation",
    "build_scenario",
    "detect_loop",
    "link_plans",
    "load_candidate",
    "main",
    "persist_witness",
    "resource_totals",
    "run_witness",
    "synthetic_cycle",
    "witness_signature",
]
