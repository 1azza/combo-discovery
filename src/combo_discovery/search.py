"""Bounded branching search for a named card's ability/trigger repeating.

First milestone of a goal-directed search player.  From an injected board it
runs a small, deterministic depth-first search over PRIORITY options with
snapshot/restore backtracking, looking for a legal decision sequence in which
a NAMED card's ``SpellCast`` event fires at least twice (one event per physical
fire — ``SpellResolved`` is not counted, or a single trigger would score two).

Second milestone: :func:`search_then_verify` turns a searched path into a real
verdict by replaying the recorded answer sequence through the existing witness
driver (:class:`SequentialPolicy`), so ``detect_loop`` — not the search's proxy
— decides.  The search summary is preserved in the result evidence.

This is deliberately tiny: no ML, no heuristic scoring.  The only ordering is
the fixed, documented candidate order in :func:`_ordered_candidates`; the only
pruning is ``max_nodes`` / ``max_depth`` / ``max_branch``.

The module is library-only: the caller owns the game lifecycle (start /
scenario injection / stop).  The existing witness driver is untouched.

Known harness limitation: snapshot/restore only behaves as a real rewind at
PRIORITY decisions (non-PRIORITY restores fall back to an abort NO-OP), so this
search branches *only* at PRIORITY decisions and answers every other decision
type deterministically with ``policy``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .env import Answer, GameNotActiveError, StaleDecisionError
from .generated import forge_env_pb2 as pb
from .runner import DecisionContext, default_policy
from .witness import (
    Scenario,
    WitnessPolicy,
    WitnessResult,
    _absolute_deck_paths,
    _drive_pregame,
    run_witness,
)

__all__ = ["SearchResult", "SequentialPolicy", "search_for_repeat", "search_then_verify"]

_PRIORITY = pb.DECISION_TYPE_PRIORITY
# One physical ability/trigger emits BOTH SpellCast and SpellResolved, so only
# SpellCast is counted; counting both would make a single fire score as two.
_TRIGGER_EVENTS = ("SpellCast",)
_SETUP_KINDS = ("activate", "play_card")
_PASS_KIND = "pass"
_SUCCESS_TARGET = 2


@dataclass
class SearchResult:
    """Outcome of one bounded search.

    ``depth`` is the length of the successful decision trace, or (on failure)
    the deepest recursion level reached.  ``trace`` is the successful path of
    ``(decision_id, decision_type, answer)`` tuples, empty on failure.
    """

    success: bool
    target: str
    trigger_count: int
    nodes: int
    depth: int
    trace: list[tuple] = field(default_factory=list)
    reason: str = ""


def _matches(name: str, target: str) -> bool:
    """Case-insensitive exact match, else a word-boundary match.

    Same rule as ``WitnessPolicy._match_option``: a short name must not match a
    longer name that merely contains it inside a word (e.g. ``Rat`` vs
    ``Rats of Rath``).
    """
    low = (name or "").strip().lower()
    t = (target or "").strip().lower()
    if not low or not t:
        return False
    if low == t:
        return True
    edge = rf"(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9])"
    return re.search(edge, low) is not None


def _ordered_candidates(
    ctx: DecisionContext,
    policy_answer: Answer | None,
    prefer_cards: Sequence[str],
    max_branch: int,
) -> list[Answer]:
    """Deterministic PRIORITY candidate order (no randomness).

    (a) the policy's own answer; then (b) ``activate``/``play_card`` options
    whose ``card_name`` word-matches any ``prefer_cards`` entry; then (c) the
    remaining non-``pass`` options; then (d) a ``pass`` option (kind ``pass``,
    else the option with the lowest id).  Duplicates are dropped by option id
    and the list is capped at ``max_branch``.
    """
    options = list(ctx.options)
    by_id = {int(o.id): o for o in options}
    ordered: list[pb.Option] = []
    seen: set[int] = set()

    def add(option: pb.Option | None) -> None:
        if option is None:
            return
        oid = int(option.id)
        if oid in seen:
            return
        seen.add(oid)
        ordered.append(option)

    # (a) The policy's answer, when it is an option id.
    if (
        isinstance(policy_answer, tuple)
        and policy_answer
        and policy_answer[0] == "option_id"
    ):
        try:
            add(by_id.get(int(str(policy_answer[1]))))
        except (TypeError, ValueError):
            pass

    prefers = [str(c) for c in prefer_cards]
    # (b) Preferred setup options (activate/play_card on a prefer_cards name).
    for option in options:
        if int(option.id) in seen:
            continue
        kind = (option.kind or "").strip().lower()
        if kind in _SETUP_KINDS and any(
            _matches(option.card_name, card) for card in prefers
        ):
            add(option)

    # (c) Remaining non-pass options, in request order.
    for option in options:
        if int(option.id) in seen:
            continue
        if (option.kind or "").strip().lower() == _PASS_KIND:
            continue
        add(option)

    # (d) A pass option (kind "pass", else the lowest-id option).
    pass_option = next(
        (o for o in options if (o.kind or "").strip().lower() == _PASS_KIND), None
    )
    if pass_option is None and options:
        pass_option = min(options, key=lambda o: int(o.id))
    add(pass_option)

    return [("option_id", int(o.id)) for o in ordered[: max(1, int(max_branch))]]


class SequentialPolicy(WitnessPolicy):
    """Replay recorded answers positionally, then delegate to ``base``.

    A searched path (``SearchResult.trace``) is a sequence of answers, not a
    board: replaying it through the witness driver is what lets ``detect_loop``
    judge the line.  This policy consumes the recorded answers in order and,
    once exhausted, hands every further decision to ``base`` (a
    :class:`WitnessPolicy` by default).  Subclassing :class:`WitnessPolicy`
    keeps the driver's ``new_game``/``iterations``/``link_hits``/
    ``trigger_hits``/``note_card_event``/``diagnostics`` contract intact, so
    trigger credit and per-link diagnostics still work during the replay.
    """

    def __init__(
        self,
        answers: Sequence[Answer],
        *,
        base: Callable[[DecisionContext], Answer] | None = None,
        combo: Any = None,
        **kwargs: Any,
    ):
        self._replay: list[Answer] = list(answers)
        self._base = base
        self._pos = 0
        super().__init__(combo, **kwargs)

    @property
    def replayed(self) -> int:
        """How many recorded answers have been consumed so far."""
        return int(self._pos)

    def new_game(self) -> None:
        """Reset the replay position (called by the driver on a fresh game)."""
        self._pos = 0
        super().new_game()
        if self._base is not None:
            reset = getattr(self._base, "new_game", None)
            if callable(reset):
                reset()

    def __call__(self, ctx: DecisionContext) -> Answer:
        if self._pos < len(self._replay):
            answer = self._replay[self._pos]
            self._pos += 1
            self.decisions += 1
            self.answers.append(answer)
            return answer
        if self._base is not None:
            answer = self._base(ctx)
            self.answers.append(answer)
            return answer
        return super().__call__(ctx)


def search_for_repeat(
    client: Any,
    game_id: int,
    *,
    target: str,
    policy: Callable[[DecisionContext], Answer],
    prefer_cards: Sequence[str] = (),
    max_nodes: int = 200,
    max_depth: int = 60,
    max_branch: int = 3,
    view_as_player: int = 0,
    player: int = 0,
) -> SearchResult:
    """Bounded DFS for a legal sequence where ``target`` fires at least twice.

    Assumes the caller has already started the game and injected the scenario;
    the outstanding decision is (re)fetched here.  ``view_as_player`` is part of
    the API for symmetry with the witness driver (events are global, so the
    search itself does not redact by viewer).  The caller owns ``stop_game``.
    """
    max_nodes = max(0, int(max_nodes))
    max_depth = max(0, int(max_depth))
    max_branch = max(1, int(max_branch))

    state: dict[str, Any] = {
        "nodes": 0,
        "triggers": 0,
        "cursor": 0,
        "deepest": 0,
    }
    path: list[tuple] = []

    # Establish the event cursor so only events produced by the search count.
    try:
        state["cursor"] = int(
            client.poll_events(game_id, 0).next_cursor
        )
    except Exception:  # noqa: BLE001 - polling is best effort here
        state["cursor"] = 0

    def poll_new() -> None:
        try:
            batch = client.poll_events(game_id, state["cursor"])
        except Exception:  # noqa: BLE001 - a dead poll just yields no events
            return
        for event in batch.events:
            if (
                event.type in _TRIGGER_EVENTS
                and int(event.player) == player
                and _matches(str(event.card_name), target)
            ):
                state["triggers"] += 1
        state["cursor"] = int(batch.next_cursor)

    def fetch() -> pb.DecisionRequest | None:
        try:
            return client.get_decision(game_id)
        except GameNotActiveError:
            return None

    def submit(req: pb.DecisionRequest, answer: Answer) -> bool:
        try:
            client.submit_decision(game_id, req.decision_id, answer)
        except (GameNotActiveError, StaleDecisionError):
            return False
        return True

    def dfs(req: pb.DecisionRequest, depth: int) -> bool:
        if depth > max_depth or state["nodes"] >= max_nodes:
            return False
        state["deepest"] = max(int(state["deepest"]), depth)
        ctx = DecisionContext(request=req)

        if req.decision_type != _PRIORITY:
            # Non-PRIORITY: answer deterministically, no branching.
            try:
                answer = policy(ctx)
            except Exception:  # noqa: BLE001 - a policy failure fails the branch
                return False
            if answer is None or state["nodes"] >= max_nodes:
                return False
            state["nodes"] += 1
            path.append((int(req.decision_id), int(req.decision_type), answer))
            if not submit(req, answer):
                path.pop()
                return False
            poll_new()
            if state["triggers"] >= _SUCCESS_TARGET:
                return True
            nxt = fetch()
            if nxt is None:
                path.pop()
                return False
            if not dfs(nxt, depth + 1):
                path.pop()
                return False
            return True

        # PRIORITY: branch over the deterministic candidate order.
        try:
            policy_answer = policy(ctx)
        except Exception:  # noqa: BLE001 - no policy answer, still order options
            policy_answer = None
        candidates = _ordered_candidates(ctx, policy_answer, prefer_cards, max_branch)
        if not candidates:
            return False

        token: bytes | None = None
        if len(candidates) > 1:
            # One snapshot per node, reused for every alternative candidate.
            try:
                token, _ = client.snapshot(game_id)
            except (GameNotActiveError, StaleDecisionError):
                token = None

        base_triggers = int(state["triggers"])
        base_path = len(path)
        current = req
        try:
            for index, answer in enumerate(candidates):
                if state["nodes"] >= max_nodes:
                    break
                if index > 0:
                    # Backtrack: rewind the game (state only), then refetch.
                    if token is not None:
                        try:
                            client.restore(game_id, token)
                        except GameNotActiveError:
                            break
                    state["triggers"] = base_triggers
                    del path[base_path:]
                    refetched = fetch()
                    if refetched is None:
                        break
                    current = refetched
                state["nodes"] += 1
                path.append(
                    (int(current.decision_id), int(current.decision_type), answer)
                )
                if not submit(current, answer):
                    path.pop()
                    state["triggers"] = base_triggers
                    continue
                poll_new()
                if state["triggers"] >= _SUCCESS_TARGET:
                    return True
                nxt = fetch()
                if nxt is None:
                    path.pop()
                    state["triggers"] = base_triggers
                    continue
                if dfs(nxt, depth + 1):
                    return True
                state["triggers"] = base_triggers
        finally:
            if state["triggers"] < _SUCCESS_TARGET:
                state["triggers"] = base_triggers
                del path[base_path:]
        return False

    start = fetch()
    if start is None:
        return SearchResult(
            success=False,
            target=str(target),
            trigger_count=int(state["triggers"]),
            nodes=int(state["nodes"]),
            depth=0,
            trace=[],
            reason="no outstanding decision (game over or not active)",
        )

    success = dfs(start, 0)
    if success:
        return SearchResult(
            success=True,
            target=str(target),
            trigger_count=int(state["triggers"]),
            nodes=int(state["nodes"]),
            depth=len(path),
            trace=list(path),
            reason=f"target fired {int(state['triggers'])} times (>= {_SUCCESS_TARGET})",
        )
    return SearchResult(
        success=False,
        target=str(target),
        trigger_count=int(state["triggers"]),
        nodes=int(state["nodes"]),
        depth=int(state["deepest"]),
        trace=[],
        reason=(
            "no legal branch made the target fire "
            f"{_SUCCESS_TARGET} times within bounds "
            f"(nodes<={max_nodes}, depth<={max_depth}, branch<={max_branch})"
        ),
    )


def search_then_verify(
    client: Any,
    scenario: Scenario,
    combo: Any,
    *,
    target: str,
    seeds: int | Sequence[int] = 1,
    max_iterations: int = 6,
    max_decisions: int = 256,
    decks: Sequence[tuple[str, str]],
    search_nodes: int = 200,
    search_depth: int = 60,
) -> WitnessResult:
    """Search for a repeated trigger, then replay the path through the judge.

    Phase A runs :func:`search_for_repeat` on a throwaway game (same seed,
    same injected ``scenario``) to find an answer sequence.  Phase B replays
    exactly those answers through :func:`witness.run_witness` with a
    :class:`SequentialPolicy`, so the existing observation sampler and
    ``detect_loop`` decide the verdict instead of the search's proxy.

    The search summary is copied into ``result.evidence`` (``search_nodes``,
    ``search_depth``, ``search_fires``, ``search_reason``, ...).  If Phase A
    finds nothing, the returned verdict is ``inconclusive`` with a reason
    naming the search bounds: no verdict is invented.
    """
    seed_list = [int(seeds)] if isinstance(seeds, int) else [int(s) for s in seeds]
    if not seed_list:
        raise ValueError("search_then_verify needs at least one seed")
    deck_list = _absolute_deck_paths(
        [(str(name), str(path)) for name, path in decks]
    )
    search_seed = seed_list[0]

    # -- Phase A: bounded search on a throwaway game ------------------------
    game_id = client.start_game(
        deck_list,
        search_seed,
        player_types=[pb.PLAYER_TYPE_REMOTE, pb.PLAYER_TYPE_GOLDFISH],
    )
    try:
        _drive_pregame(client, game_id, default_policy)
        client.setup_scenario(game_id, scenario)
        client.get_decision(game_id)
        search = search_for_repeat(
            client,
            game_id,
            target=target,
            policy=WitnessPolicy(combo),
            prefer_cards=combo.cards,
            max_nodes=search_nodes,
            max_depth=search_depth,
        )
    finally:
        # The caller owns the game lifecycle beyond this call.
        try:
            client.stop_game(game_id)
        except Exception:  # noqa: BLE001 - never mask the search outcome
            pass

    provenance: dict[str, Any] = {
        "search_nodes": int(search.nodes),
        "search_depth": int(search.depth),
        "search_fires": int(search.trigger_count),
        "search_target": str(target),
        "search_reason": str(search.reason),
        "search_trace_len": len(search.trace),
    }

    if not search.success or not search.trace:
        return WitnessResult(
            verdict="inconclusive",
            scenario=scenario,
            iterations=0,
            seed=search_seed,
            seeds=seed_list,
            evidence={
                "reason": (
                    "search found no path where the target fired "
                    f"{_SUCCESS_TARGET} times within bounds "
                    f"(search_nodes={int(search_nodes)}, "
                    f"search_depth={int(search_depth)})"
                ),
                **provenance,
            },
            candidate_kind=str(getattr(combo, "kind", "")),
            candidate_key=str(getattr(combo, "key", "")),
            card_names=tuple(getattr(combo, "cards", ())),
            infinite=bool(getattr(combo, "infinite", False)),
        )

    # -- Phase B: replay the searched answers through the witness driver ----
    answers = [entry[2] for entry in search.trace]
    policy = SequentialPolicy(answers, base=WitnessPolicy(combo), combo=combo)
    result = run_witness(
        client,
        scenario,
        policy,
        seeds=seed_list,
        max_iterations=int(max_iterations),
        max_decisions=int(max_decisions),
        decks=deck_list,
        candidate_kind=str(getattr(combo, "kind", "")),
        candidate_key=str(getattr(combo, "key", "")),
        card_names=tuple(getattr(combo, "cards", ())),
        infinite=bool(getattr(combo, "infinite", False)),
    )
    result.evidence = {**result.evidence, **provenance, "replayed_answers": len(answers)}
    return result
