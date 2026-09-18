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

from ..env import (
    PROTOCOL_VERSION,
    Answer,
    ForgeEnvClient,
    GameNotActiveError,
    StaleDecisionError,
)
from ..generated import forge_env_pb2 as pb
from ..runner import DecisionContext, DecisionTraceEntry, default_policy
from .cli import (
    Candidate,
    _names_for_ids,
    _oracle_texts_for_ids,
    _parse_card_ids,
    _parse_decks,
    _parse_seeds,
    _type_lines_for_ids,
    load_candidate,
    main,
)
from .driver import (
    DEFAULT_PLAYER_TYPES,
    DEFAULT_WITNESS_DECKS,
    MAX_PREGAME_DECISIONS,
    WitnessResult,
    _absolute_deck_paths,
    _drive_pregame,
    _is_over,
    _policy_diagnostics,
    _resource_deltas,
    _run_witness_seed,
    run_witness,
)
from .loop import (
    GAME_STATE_GROWTH_KEYS,
    GROWTH_KEYS,
    MAX_FORCED_PASSES,
    SPIN_THRESHOLD,
    Observation,
    _battlefield_entries,
    _game_state_grew,
    _grown_between,
    _non_token_battlefield_count,
    _pass_option,
    _signature_hash,
    _zone_count,
    build_observation,
    detect_loop,
    resource_totals,
    witness_signature,
)
from .persist import (
    observation_recorder,
    persist_error_result,
    persist_observations,
    persist_witness,
    start_witness_recording,
)
from .policy import (
    WITNESS_POLICY_VERSION,
    WitnessPolicy,
    _GENERIC_LINK_TOKENS,
    _TAP_WORD_RE,
    _UNTAP_RE,
)
from .scenario import (
    DEFAULT_GRAVEYARD,
    DEFAULT_LIBRARY_LAND,
    DEFAULT_LIBRARY_SIZE,
    DEFAULT_LIFE,
    DEFAULT_MANA_COLORS,
    DEFAULT_MANA_PER_COLOR,
    DEFAULT_OPPONENT_LIFE,
    CardSpec,
    LinkPlan,
    PlayerScenario,
    Scenario,
    _PERMANENT_TYPE_TOKENS,
    _ability_for,
    _assign_battlefield_ids,
    _combo_cards,
    _combo_oracle_texts,
    _default_library,
    _is_attachment,
    _link_plan,
    _must_be_cast,
    _oracle_text_for,
    _type_line_for,
    build_scenario,
    is_graveyard_gated,
    link_plans,
    synthetic_cycle,
)

#: Preserve the historical ``combo_discovery.witness.logger`` attribute.
logger = logging.getLogger(__name__)

__all__ = [
    "Candidate",
    "CardSpec",
    "DEFAULT_GRAVEYARD",
    "DEFAULT_LIBRARY_LAND",
    "DEFAULT_LIBRARY_SIZE",
    "DEFAULT_LIFE",
    "DEFAULT_MANA_COLORS",
    "DEFAULT_MANA_PER_COLOR",
    "DEFAULT_OPPONENT_LIFE",
    "DEFAULT_PLAYER_TYPES",
    "DEFAULT_WITNESS_DECKS",
    "LinkPlan",
    "MAX_FORCED_PASSES",
    "MAX_PREGAME_DECISIONS",
    "Observation",
    "PROTOCOL_VERSION",
    "PlayerScenario",
    "SPIN_THRESHOLD",
    "Scenario",
    "WITNESS_POLICY_VERSION",
    "WitnessPolicy",
    "WitnessResult",
    "_absolute_deck_paths",
    "_drive_pregame",
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
