"""Typed synchronous client for the forge-harness gRPC service (protocol v7).

Mirrors the service defined in proto/forge_env.proto. All methods are blocking;
use WorkerPool for parallelism.

Contract notes:
  - Every per-game RPC carries an explicit game_id; start_game returns it.
  - Decisions are typed: the outstanding DecisionRequest carries a
    DecisionType enum plus per-type payload; submit_decision echoes
    (game_id, decision_id) plus a typed answer (see build_submit).
  - Events are independently pollable via poll_events/drain_events.
  - Server errors: INVALID_ARGUMENT on a decision call that is about the
    outstanding decision (stale/unknown game or decision id, invalid option,
    already-resolved decision) maps to StaleDecisionError, which callers may
    recover from by refetching. Any other INVALID_ARGUMENT — StartGame
    validation, a bad request shape, a wrong answer arm, an out-of-range
    number/option — maps to InvalidRequestError, a deterministic client/policy
    bug that must fail loudly. FAILED_PRECONDITION (wrong lifecycle state) maps
    to GameNotActiveError; DEADLINE_EXCEEDED maps to HarnessTimeoutError (slow
    call — the server may still be healthy); UNAVAILABLE maps to
    HarnessConnectionError (transport death).

Answer shapes accepted by submit_decision (one per DecisionType):
  ("option_id", int)                              PRIORITY
  ("boolean_answer", bool)                        MULLIGAN_KEEP
  ("card_ids", list[int])                         MULLIGAN_TUCK / ORDER_BLOCKERS / CHOOSE_CARDS
  ("number_answer", int)                          ANNOUNCE
  ("attackers", list[(attacker_card, defender_player)])   DECLARE_ATTACKERS
  ("blockers", list[(blocker_card, attacker_card)])       DECLARE_BLOCKERS
  ("damage", list[DamageTarget | (card_id|None, player|None, amount)])  ASSIGN_COMBAT_DAMAGE
  ("scry", (top_ids, bottom_ids))                 SCRY_ARRANGE
  ("targets", (card_ids, player_slots))           CHOOSE_TARGETS  (pb.TargetSelection)
  ("mode_selection", list[int])                   CHOOSE_MODE / OPTIONAL_COSTS

v4 note: "mode_selection" is a readability ALIAS for the card_ids arm. Per the
proto, CHOOSE_MODE and OPTIONAL_COSTS answers ride the existing IntList
`card_ids` arm interpreted as mode/cost option ids (no new oneof arm), so
("mode_selection", ids) and ("card_ids", ids) are wire-identical.

v5 note (snapshot/restore): snapshot() returns (token_bytes, state_hash) and
REQUIRES an outstanding decision (the engine thread is parked, so the state is
quiescent); it raises GameNotActiveError on FAILED_PRECONDITION (e.g. "requires
an outstanding decision"). restore() invalidates the outstanding decision — the
caller must refetch via get_decision before submitting — appends a deterministic
`SnapshotRestored` event, and re-seeds the engine RNG so identical post-restore
action sequences replay byte-identically. Restores do not consume tokens; tokens
are game-scoped and die with the game.

v6 note (normalized events + FullState v2): GameEvent carries a normalized
`type` from the fixed research vocabulary, plus card_id, detail_raw,
old_value/new_value and extra (see runner.EVENT_VOCABULARY). get_state() takes
`view_as_player` (0 = observer: other players' hands/libraries are counts only)
and FullState v2 adds typed_mana_pools, stack, exile and command zones.

v7 note (scenario injection): setup_scenario() injects a pre-configured board
state (see witness.Scenario). Like snapshot(), it REQUIRES a LIVE outstanding
decision (the engine thread is parked, so the state is quiescent) and raises
GameNotActiveError on FAILED_PRECONDITION ("requires an outstanding decision").
It INVALIDATES that decision and appends a deterministic `ScenarioInjected`
event, so the caller MUST refetch via ``get_decision`` before submitting again
(the decision_id may change, mirroring restore()). Injection consumes no RNG
and is deterministic for a fixed scenario + seed; the scenario hash is folded
into `state_hash`. Library list order is the library order (index 0 = top).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import grpc

from .generated import forge_env_pb2 as pb
from .generated.forge_env_pb2_grpc import ForgeEnvStub

if TYPE_CHECKING:  # pragma: no cover - typing only (avoids importing witness)
    from .witness import Scenario

PROTOCOL_VERSION = 7

# INVALID_ARGUMENT classification for decision calls (get_decision /
# submit_decision). The harness exposes no machine-readable subcode, so we match
# the exact detail texts emitted by the Java harness (verified against
# forge-harness/src/main/java/forge/harness/{ForgeEnvService,GameRunner}.java):
#
#   unknown game_id:   "unknown or removed game_id: <id>"
#   stale submit:      "decision_id <id> does not match outstanding decision_id <id>"
#   already resolved:  "decision_id <id> was already resolved (timeout/abort); answer discarded"
#   no outstanding:    "no decision outstanding for game_id <id>"
#   wrong arm:         "... is a <TYPE> decision; answer must use <arm>"   [policy bug]
#   option out of range: "option_id <id> is not among the <n> options of
#     decision_id <id>"  [policy bug]
#
# A parallel Java lane is adding a "stale decision:" prefix to stale rejections,
# so "stale decision" is the primary marker. Coordinate any wording change with
# the Java side; keep this list in sync.
_STALE_DECISION_MARKERS = (
    "stale decision",              # new coordinated prefix
    "does not match outstanding",  # real decision_id mismatch text
    "unknown or removed game",     # real unknown game_id text
    "unknown or removed decision",
    "already resolved",            # real timeout/abort-resolved text
    "no decision outstanding",
    "is already being validated",
    "invalid option",
)

# Phrases that mark an INVALID_ARGUMENT as a malformed/buggy request rather than
# a stale decision, even when an arm name such as "option_id" also appears.
# "must use" is the wrong-arm message; "is not among" is the out-of-range
# option/candidate message. Both are deterministic policy bugs.
_POLICY_ERROR_PHRASES = ("must use", "is not among")


def _is_stale_decision_detail(details: str) -> bool:
    """True if a decision-call INVALID_ARGUMENT detail describes a stale/resolved
    decision state (recoverable by refetching) rather than a buggy request.

    Ordering matters: explicit stale markers are checked first, but a policy
    error phrase forces False — "must use option_id" (wrong arm) and "option_id
    N is not among ..." (out of range) must never be treated as stale.
    """
    text = details.lower()
    if any(phrase in text for phrase in _POLICY_ERROR_PHRASES):
        return False
    if any(marker in text for marker in _STALE_DECISION_MARKERS):
        return True
    # "option_id" alone is only ambiguous once the policy-error phrases above
    # have been ruled out.
    return "option_id" in text

# Arm name -> constructor for the corresponding answer payload in
# pb.DecisionSubmit. Arms that carry message types (card_ids, scry, targets)
# get their payload built separately in build_submit.
_ANSWER_ARMS = (
    "option_id",
    "boolean_answer",
    "card_ids",
    "number_answer",
    "attackers",
    "blockers",
    "damage",
    "scry",
    "targets",         # CHOOSE_TARGETS
    "mode_selection",  # CHOOSE_MODE / OPTIONAL_COSTS (alias for the card_ids arm)
)


class DamageTarget:
    """One entry of an ASSIGN_COMBAT_DAMAGE answer: damage routed to a card
    (a blocker) or to the defending player. Exactly one of card_id / player
    is set. Plain (card_id | None, player | None, amount) tuples are also
    accepted by build_submit."""

    __slots__ = ("card_id", "player", "amount")

    def __init__(self, card_id: int | None = None, player: int | None = None, amount: int = 0):
        if (card_id is None) == (player is None):
            raise ValueError("DamageTarget needs exactly one of card_id or player")
        self.card_id = card_id
        self.player = player
        self.amount = amount

    def as_tuple(self) -> tuple[int | None, int | None, int]:
        return (self.card_id, self.player, self.amount)

    def __eq__(self, other) -> bool:
        if isinstance(other, DamageTarget):
            return self.as_tuple() == other.as_tuple()
        if isinstance(other, tuple):
            return self.as_tuple() == other
        return NotImplemented

    def __repr__(self) -> str:
        return f"DamageTarget(card_id={self.card_id}, player={self.player}, amount={self.amount})"


# Typed answer: (arm_name, payload) — see the module docstring for the exact
# payload shape accepted per arm. Kept loose at the type level on purpose;
# build_submit validates at runtime.
Answer = tuple[str, object] | pb.DecisionSubmit


def build_submit(game_id: int, decision_id: int, answer: Answer) -> pb.DecisionSubmit:
    """Build a pb.DecisionSubmit from a typed answer.

    ``answer`` is an (arm, payload) tuple; see the module docstring for the
    accepted shapes per arm. A pb.DecisionSubmit with the oneof already set is
    also accepted and passed through (handy for tests and manual control).
    """
    if isinstance(answer, pb.DecisionSubmit):
        req = pb.DecisionSubmit()
        req.CopyFrom(answer)
        req.game_id = game_id
        req.decision_id = decision_id
        return req
    arm, payload = answer
    # Payload shape is validated per arm at runtime; the type checker sees an
    # untyped object here by design (see Answer).
    payload = cast(Any, payload)
    if arm not in _ANSWER_ARMS:
        raise ValueError(
            f"unknown answer arm {arm!r}; expected one of {', '.join(_ANSWER_ARMS)}"
        )
    kwargs: dict = {"game_id": game_id, "decision_id": decision_id}
    if arm == "option_id":
        kwargs["option_id"] = int(payload)
    elif arm == "boolean_answer":
        kwargs["boolean_answer"] = bool(payload)
    elif arm == "card_ids":
        kwargs["card_ids"] = pb.IntList(values=[int(c) for c in payload])
    elif arm == "mode_selection":
        # v4 readability alias: CHOOSE_MODE / OPTIONAL_COSTS answers ride the
        # existing IntList `card_ids` arm (option ids), per the proto.
        kwargs["card_ids"] = pb.IntList(values=[int(c) for c in payload])
    elif arm == "targets":
        # CHOOSE_TARGETS: payload is (card_ids, player_slots), order preserved.
        card_ids, player_slots = payload
        kwargs["targets"] = pb.TargetSelection(
            card_ids=[int(c) for c in card_ids],
            player_slots=[int(p) for p in player_slots],
        )
    elif arm == "number_answer":
        kwargs["number_answer"] = int(payload)
    elif arm == "attackers":
        kwargs["attackers"] = pb.AttackerList(
            values=[
                pb.AttackerAssignment(attacker_card=a, defender_player=d) for a, d in payload
            ]
        )
    elif arm == "blockers":
        kwargs["blockers"] = pb.BlockerList(
            values=[
                pb.BlockerAssignment(blocker_card=b, attacker_card=a) for b, a in payload
            ]
        )
    elif arm == "damage":
        entries = []
        for item in payload:
            card_id, player, amount = item.as_tuple() if isinstance(item, DamageTarget) else item
            if (card_id is None) == (player is None):
                raise ValueError("damage entry needs exactly one of card_id or player")
            # Set only the winning oneof arm; explicitly assigning both in the
            # constructor makes the last one silently win.
            d = pb.DamageAssignment(amount=amount)
            if card_id is not None:
                d.card_id = card_id
            else:
                assert player is not None  # exactly-one-of guard above
                d.player = player  # may be 0 — assignment still marks the arm
            entries.append(d)
        kwargs["damage"] = pb.DamageList(values=entries)
    elif arm == "scry":
        top, bottom = payload
        kwargs["scry"] = pb.CardPartition(
            top=[int(c) for c in top], bottom=[int(c) for c in bottom]
        )
    return pb.DecisionSubmit(**kwargs)


def _card_spec_to_proto(spec: Any) -> pb.CardSpec:
    """Convert a witness.CardSpec (duck-typed) to proto."""
    return pb.CardSpec(
        name=str(spec.name),
        set=str(spec.set),
        tapped=bool(spec.tapped),
        summoning_sick=bool(spec.summoning_sick),
        counters={str(k): int(v) for k, v in spec.counters.items()},
        damage=int(spec.damage),
        no_etb_triggers=bool(spec.no_etb_triggers),
        id=int(getattr(spec, "id", 0)),
        attached_to=int(getattr(spec, "attached_to", 0)),
    )


class ForgeEnvError(RuntimeError):
    """Base error for harness RPC failures. Carries the gRPC status code."""

    def __init__(self, message: str, code: grpc.StatusCode | None = None):
        super().__init__(message)
        self.code = code


class HarnessConnectionError(ForgeEnvError):
    """Transport-level failure: the harness is unreachable or the channel
    broke mid-call (UNAVAILABLE). The channel/stub are reset."""


class HarnessTimeoutError(ForgeEnvError):
    """DEADLINE_EXCEEDED: one call was too slow. NOT proof the server died —
    under load even quick RPCs can exceed their deadline while the harness is
    still perfectly healthy. The channel is deliberately left intact; callers
    decide whether to retry (runner does for get_decision)."""


class StaleDecisionError(ForgeEnvError):
    """INVALID_ARGUMENT on a decision call that is about the outstanding
    decision: stale/unknown game or decision id, or an invalid option. This is
    a state error the runner may recover from by refetching and retrying. Only
    raised from get_decision/submit_decision (never from other RPCs) and only
    when the server detail matches a known stale marker (see
    _STALE_DECISION_MARKERS)."""


class InvalidRequestError(ForgeEnvError):
    """INVALID_ARGUMENT that is NOT about the outstanding decision: StartGame
    validation, a bad request shape, a wrong answer arm, an out-of-range
    number, a size/partition violation, etc. A deterministic client/policy bug
    — it must fail loudly, never be retried by the runner or blamed on a
    worker by the pool."""


class GameNotActiveError(ForgeEnvError):
    """FAILED_PRECONDITION: wrong lifecycle state (e.g. StartGame while active,
    submit after game over)."""


class ProtocolMismatchError(ForgeEnvError):
    """Harness speaks a different protocol version than this client."""


class ForgeEnvClient:
    def __init__(
        self,
        host: str = "localhost",
        port: int = 50051,
        timeout: float = 30.0,
        decision_timeout: float = 300.0,
    ):
        """Timeout triad (load-bearing ordering — do not shuffle):

            server decision watchdog (120s)
              < server GetDecision wait (130s, DECISION_WAIT_MS)
                < client decision_timeout (300s)

        The watchdog releases a decision before the GetDecision wait expires,
        which itself expires well before the client gives up, so a slow game
        surfaces as StaleDecisionError/GameNotActiveError instead of a client
        deadline — and a genuinely hung engine is caught by the 300s bound.
        Changing any one of these (client side, or the harness's) without
        re-deriving the others reintroduces races: e.g. a client timeout below
        130s turns normal opponent thinking into HarnessTimeoutError, and a
        client timeout above the watchdog's release makes the client wait on
        decisions the server has already abandoned.
        """
        self._target = f"{host}:{port}"
        self._timeout = timeout
        # GetDecision blocks server-side until the acting remote player has a
        # decision. The server guarantees wake-on-game-over, so this is a
        # safety bound on a slow engine stretch, not the expected wait time —
        # hence much longer than the general per-RPC timeout (see the triad
        # above).
        self._decision_timeout = decision_timeout
        self._channel: grpc.Channel | None = None
        self._stub: ForgeEnvStub | None = None

    def _ensure_stub(self) -> ForgeEnvStub:
        if self._stub is None:
            try:
                self._channel = grpc.insecure_channel(self._target)
                grpc.channel_ready_future(self._channel).result(timeout=self._timeout)
                self._stub = ForgeEnvStub(self._channel)
            except grpc.FutureTimeoutError as e:
                self._channel = None
                raise HarnessConnectionError(f"no harness at {self._target}") from e
        return self._stub

    def _call(self, fn, request, timeout: float | None = None, decision_call: bool = False):
        try:
            return fn(request, timeout=timeout or self._timeout)
        except grpc.RpcError as e:
            code = e.code()
            details = e.details() or ""
            if code == grpc.StatusCode.UNAVAILABLE:
                # Transport death: the channel is broken, reset it so the next
                # call reconnects from scratch.
                self._stub = None
                self._channel = None
                raise HarnessConnectionError(f"{code.name} from {self._target}", code=code) from e
            if code == grpc.StatusCode.DEADLINE_EXCEEDED:
                # A slow call, not a dead server: the channel is still usable.
                # Under load even quick RPCs (IsGameOver, GetState) can trip
                # their deadline while the harness stays healthy, so this must
                # NOT be conflated with connection death (pool would kill
                # healthy workers).
                raise HarnessTimeoutError(f"{code.name} from {self._target}", code=code) from e
            if code == grpc.StatusCode.INVALID_ARGUMENT:
                # Stale decision state is only meaningful on decision calls and
                # only when the server detail says so; anything else is a
                # malformed or buggy request (policy bug / bad config) and must
                # fail loudly as InvalidRequestError.
                if decision_call and _is_stale_decision_detail(details):
                    raise StaleDecisionError(
                        f"INVALID_ARGUMENT: {details}", code=code
                    ) from e
                raise InvalidRequestError(f"INVALID_ARGUMENT: {details}", code=code) from e
            if code == grpc.StatusCode.FAILED_PRECONDITION:
                raise GameNotActiveError(f"FAILED_PRECONDITION: {details}", code=code) from e
            raise ForgeEnvError(f"{code.name}: {details}", code=code) from e

    # -- health / version ---------------------------------------------------

    def ping(self) -> pb.Pong:
        """Raw ping; the Pong carries protocol_version."""
        return self._call(self._ensure_stub().Ping, pb.Empty())

    def protocol_version(self) -> int:
        """Ask the harness which protocol version it speaks."""
        return self.ping().protocol_version

    def connect(self) -> pb.Pong:
        """Ping the harness and verify the protocol version.

        Raises ProtocolMismatchError with a clear message if the harness does
        not speak the expected protocol version (PROTOCOL_VERSION).
        """
        pong = self.ping()
        if pong.protocol_version != PROTOCOL_VERSION:
            raise ProtocolMismatchError(
                f"protocol version mismatch: harness at {self._target} reports "
                f"protocol_version={pong.protocol_version}, this client requires "
                f"{PROTOCOL_VERSION} (regenerate stubs on one side?)"
            )
        return pong

    # -- game lifecycle -----------------------------------------------------

    def start_game(
        self,
        decks: list[tuple[str, str]],
        seed: int,
        player_types: list[pb.PlayerType] | None = None,
        max_turns: int = 0,
        timeout_seconds: int = 0,
        force_stop_active: bool = False,
    ) -> int:
        """Start a game and return the server-assigned game_id.

        Fails with GameNotActiveError if a game is already active on the
        harness, unless force_stop_active is set — then the leftover game is
        stopped first (used by the worker pool to recover a worker that was
        left with an active game by a crashed client).
        """
        if player_types is None:
            player_types = [pb.PLAYER_TYPE_REMOTE] * len(decks)
        req = pb.StartRequest(
            decks=[pb.DeckSpec(name=n, path=p) for n, p in decks],
            seed=seed,
            player_types=player_types,
            max_turns=max_turns,
            timeout_seconds=timeout_seconds,
            force_stop_active=force_stop_active,
        )
        resp: pb.StartResponse = self._call(self._ensure_stub().StartGame, req)
        return resp.game_id

    def get_decision(self, game_id: int) -> pb.DecisionRequest:
        """Block until the acting player of this game has a decision pending.

        Uses decision_timeout (constructor arg, default 300s) instead of the
        general 30s per-RPC timeout: the server guarantees wake-on-game-over,
        so the long bound only guards against a hung engine, not the normal
        wait for the opponent to finish acting.
        """
        return self._call(
            self._ensure_stub().GetDecision,
            pb.GameQuery(game_id=game_id),
            timeout=self._decision_timeout,
            decision_call=True,
        )

    def submit_decision(self, game_id: int, decision_id: int, answer: Answer) -> pb.StepResult:
        """Echo the outstanding DecisionRequest's (game_id, decision_id) plus a typed answer.

        ``answer`` is an (arm, payload) tuple built for the outstanding
        DecisionType — e.g. ("option_id", 3) for PRIORITY,
        ("attackers", [(card_id, defender_player), ...]) for DECLARE_ATTACKERS,
        ("scry", (top_ids, bottom_ids)) for SCRY_ARRANGE. See the module
        docstring for the full set; build_submit does the oneof encoding.
        """
        req = build_submit(game_id, decision_id, answer)
        return self._call(self._ensure_stub().SubmitDecision, req, decision_call=True)

    def get_state(self, game_id: int, view_as_player: int = 0) -> pb.FullState:
        """Full state inspection.

        ``view_as_player`` selects hidden-info redaction server-side: 0 is the
        observer view (other players' hands and libraries are reported as
        counts only, no card identities); any other value is a player slot and
        reveals that player's private information. Sent as a GameViewQuery.
        """
        req = pb.GameViewQuery(game_id=game_id, view_as_player=view_as_player)
        return self._call(self._ensure_stub().GetState, req)

    def poll_events(self, game_id: int, cursor: int = 0) -> pb.EventBatch:
        """Return all buffered events with seq > cursor, in seq order."""
        req = pb.PollRequest(game_id=game_id, cursor=cursor)
        return self._call(self._ensure_stub().PollEvents, req)

    def drain_events(self, game_id: int, cursor: int = 0) -> list[pb.GameEvent]:
        """Poll until next_cursor stops advancing; return all events concatenated."""
        events: list[pb.GameEvent] = []
        while True:
            batch = self.poll_events(game_id, cursor)
            events.extend(batch.events)
            if batch.next_cursor <= cursor:
                break
            cursor = batch.next_cursor
        return events

    def snapshot(self, game_id: int) -> tuple[bytes, str]:
        """Take a state snapshot; returns ``(token_bytes, state_hash)``.

        REQUIRES an outstanding decision (the engine thread is parked, so the
        state is quiescent). Raises GameNotActiveError on FAILED_PRECONDITION
        (no outstanding decision / game not active). Tokens are game-scoped and
        die with the game; a game holds at most a bounded number of live tokens.
        """
        resp: pb.SnapshotResponse = self._call(
            self._ensure_stub().Snapshot, pb.GameQuery(game_id=game_id)
        )
        return resp.token.token, resp.state_hash

    def restore(self, game_id: int, token: bytes) -> None:
        """Restore a snapshot token.

        Invalidates the outstanding decision — the caller MUST refetch via
        ``get_decision`` before submitting again (the decision_id may change).
        The harness appends a deterministic ``SnapshotRestored`` event and
        re-seeds the engine RNG so identical post-restore action sequences
        replay byte-identically. Restoring does not consume the token.
        Requires an outstanding decision (raises GameNotActiveError otherwise).
        """
        req = pb.RestoreRequest(game_id=game_id, token=token)
        self._call(self._ensure_stub().Restore, req)

    def setup_scenario(self, game_id: int, scenario: Scenario) -> tuple[str, int]:
        """Inject a pre-configured board state into a running game (v7).

        ``scenario`` is a :class:`combo_discovery.witness.Scenario`; this builds
        the v7 ``SetupScenarioRequest`` from it. Returns
        ``(state_hash, applied_events)`` where ``applied_events`` is the number
        of events the injection appended (a deterministic ``ScenarioInjected``
        marker among them).

        REQUIRES a LIVE outstanding decision — the engine thread is parked, so
        the state is quiescent. Raises GameNotActiveError on FAILED_PRECONDITION
        (no outstanding decision / game not active). The call INVALIDATES the
        outstanding decision, so the caller MUST refetch via ``get_decision``
        before submitting again (the decision_id may change, mirroring
        ``restore``). Library list order is the library order (index 0 = top).
        """
        players = [self._player_scenario_to_proto(p) for p in scenario.players]
        req = pb.SetupScenarioRequest(
            game_id=game_id,
            players=players,
            active_player=int(scenario.active_player),
            turn=int(scenario.turn),
            phase=str(scenario.phase),
            require_outstanding_decision=bool(scenario.require_outstanding_decision),
        )
        resp: pb.SetupScenarioResponse = self._call(
            self._ensure_stub().SetupScenario, req
        )
        return resp.state_hash, int(resp.applied_events)

    @staticmethod
    def _player_scenario_to_proto(p: Any) -> pb.PlayerScenario:
        """Convert a witness.PlayerScenario (duck-typed) to proto."""
        ps = pb.PlayerScenario(
            player=int(p.player), life=int(p.life), mana=dict(p.mana)
        )
        for spec in p.battlefield:
            ps.battlefield.append(_card_spec_to_proto(spec))
        for spec in p.hand:
            ps.hand.append(_card_spec_to_proto(spec))
        for spec in p.graveyard:
            ps.graveyard.append(_card_spec_to_proto(spec))
        for spec in p.library:
            ps.library.append(_card_spec_to_proto(spec))
        for spec in p.exile:
            ps.exile.append(_card_spec_to_proto(spec))
        return ps

    def is_game_over(self, game_id: int) -> pb.GameOver:
        return self._call(self._ensure_stub().IsGameOver, pb.GameQuery(game_id=game_id))

    def stop_game(self, game_id: int) -> None:
        """Stop the game, release resources, and drop its event buffer."""
        self._call(self._ensure_stub().StopGame, pb.GameQuery(game_id=game_id))

    def close(self) -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel = None
            self._stub = None

    def __enter__(self) -> ForgeEnvClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
