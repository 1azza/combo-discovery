"""Layer-2 predicate ontology: typed predicates + interaction edges.

Public surface:

* :mod:`combo_discovery.ontology.vocabulary` — the controlled predicate
  vocabulary and its documented extraction rules;
* :mod:`combo_discovery.ontology.extractor` — ``card_effects`` rows ->
  per-card predicates;
* :mod:`combo_discovery.ontology.patterns` — the per-pattern registry;
* :mod:`combo_discovery.ontology.edges` — pattern queries -> interactions;
* :func:`build_ontology` — persist all of the above (schema v3).
"""

from __future__ import annotations

from .budget import (
    DEFAULT_MAX_SECONDS,
    DEFAULT_MAX_STEPS,
    MAX_SAFE_CLOSURE_DEPTH,
    MAX_SAFE_POOL,
    BudgetExceeded,
    SearchBudget,
    estimate_cost,
    preflight,
)
from .builder import (
    OntologyReport,
    build_ontology,
    ensure_query_patterns,
    latest_import_id,
    probe_interactions,
)
from .coverage import (
    NET_PRESENT,
    NO_GRAPH_EDGE,
    analyze_denominator,
    capability_tags,
    classify_pair,
    combo_pairs,
    load_candidate_pairs,
    load_card_meta,
    load_known_combos,
    map_card,
    precision_view,
    primary_capability,
)
from .cycle_gate import (
    CAP_GATES,
    DEFAULT_RUN_RANGES,
    CycleEvidence,
    CycleStep,
    VerdictRow,
    candidate_finder_score,
    check_card_ids,
    check_signatures,
    engine_class,
    load_verdicts,
    resolve_card_id,
    validate,
)
from .cycles import (
    Combo,
    ComboList,
    Graph,
    build_graph,
    discover_combos,
    find_combos,
    load_motif_weights,
    loop_machinery_cards,
    tight_pool,
)
from .edges import PATTERNS, CardView, Edge, build_edges, get_pattern, iter_patterns, score_edge
from .extractor import (
    CardContext,
    CardEffect,
    CardPredicate,
    extract_card_predicates,
    extract_import,
)
from .links import Link, link_enables, link_hostile, link_re_trigger, link_satisfies
from .ports import AbilitySig, Port, build_signatures, signatures_from_import
from .queries import QUERIES, ComboContext, Query, get_query, iter_queries
from .vocabulary import ALL_PREDICATES, PREDICATE_DESCRIPTIONS, describe

__all__ = [
    "ALL_PREDICATES",
    "AbilitySig",
    "BudgetExceeded",
    "CAP_GATES",
    "CardContext",
    "CardEffect",
    "CardPredicate",
    "CardView",
    "Combo",
    "ComboContext",
    "ComboList",
    "CycleEvidence",
    "CycleStep",
    "DEFAULT_MAX_SECONDS",
    "DEFAULT_MAX_STEPS",
    "DEFAULT_RUN_RANGES",
    "Edge",
    "Graph",
    "Link",
    "MAX_SAFE_CLOSURE_DEPTH",
    "MAX_SAFE_POOL",
    "NET_PRESENT",
    "NO_GRAPH_EDGE",
    "OntologyReport",
    "PATTERNS",
    "PREDICATE_DESCRIPTIONS",
    "Port",
    "QUERIES",
    "Query",
    "SearchBudget",
    "VerdictRow",
    "analyze_denominator",
    "build_edges",
    "build_graph",
    "build_ontology",
    "build_signatures",
    "candidate_finder_score",
    "capability_tags",
    "check_card_ids",
    "check_signatures",
    "classify_pair",
    "combo_pairs",
    "describe",
    "discover_combos",
    "engine_class",
    "ensure_query_patterns",
    "estimate_cost",
    "extract_card_predicates",
    "extract_import",
    "find_combos",
    "get_pattern",
    "get_query",
    "iter_patterns",
    "iter_queries",
    "latest_import_id",
    "link_enables",
    "link_hostile",
    "link_re_trigger",
    "link_satisfies",
    "load_candidate_pairs",
    "load_card_meta",
    "load_known_combos",
    "load_motif_weights",
    "load_verdicts",
    "loop_machinery_cards",
    "map_card",
    "preflight",
    "precision_view",
    "primary_capability",
    "probe_interactions",
    "resolve_card_id",
    "score_edge",
    "signatures_from_import",
    "tight_pool",
    "validate",
]
