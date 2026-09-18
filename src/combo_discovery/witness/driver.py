"""Witness run driver: start, inject, drive, judge.

Split out of ``combo_discovery.witness``; behaviour is unchanged.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..env import (
    PROTOCOL_VERSION,
    Answer,
    GameNotActiveError,
    StaleDecisionError,
)
from ..generated import forge_env_pb2 as pb
from ..runner import DecisionContext, DecisionTraceEntry, default_policy
from .loop import (
    MAX_FORCED_PASSES,
    SPIN_THRESHOLD,
    Observation,
    _game_state_grew,
    _pass_option,
    build_observation,
    detect_loop,
)
from .policy import WITNESS_POLICY_VERSION
from .scenario import Scenario

logger = logging.getLogger("combo_discovery.witness")

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
