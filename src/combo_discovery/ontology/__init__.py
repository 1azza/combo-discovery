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

from .builder import OntologyReport, build_ontology, latest_import_id, probe_interactions
from .cycles import Combo, Graph, build_graph, find_combos, load_motif_weights
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
    "CardContext",
    "CardEffect",
    "CardPredicate",
    "CardView",
    "Combo",
    "ComboContext",
    "Edge",
    "Graph",
    "Link",
    "OntologyReport",
    "PATTERNS",
    "PREDICATE_DESCRIPTIONS",
    "Port",
    "QUERIES",
    "Query",
    "build_edges",
    "build_graph",
    "build_ontology",
    "build_signatures",
    "describe",
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
    "load_motif_weights",
    "probe_interactions",
    "score_edge",
    "signatures_from_import",
]
