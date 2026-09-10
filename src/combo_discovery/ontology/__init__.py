"""Layer-2 predicate ontology: typed predicates + interaction edges.

Public surface:

* :mod:`combo_discovery.ontology.vocabulary` — the controlled predicate
  vocabulary and its documented extraction rules;
* :mod:`combo_discovery.ontology.extractor` — ``card_effects`` rows ->
  per-card predicates;
* :mod:`combo_discovery.ontology.edges` — pattern queries -> interactions;
* :func:`build_ontology` — persist all of the above (schema v3).
"""

from __future__ import annotations

from .builder import OntologyReport, build_ontology, latest_import_id, probe_interactions
from .edges import PATTERNS, CardView, Edge, build_edges, score_edge
from .extractor import (
    CardContext,
    CardEffect,
    CardPredicate,
    extract_card_predicates,
    extract_import,
)
from .vocabulary import ALL_PREDICATES, PREDICATE_DESCRIPTIONS, describe

__all__ = [
    "ALL_PREDICATES",
    "CardContext",
    "CardEffect",
    "CardPredicate",
    "CardView",
    "Edge",
    "OntologyReport",
    "PATTERNS",
    "PREDICATE_DESCRIPTIONS",
    "build_edges",
    "build_ontology",
    "describe",
    "extract_card_predicates",
    "extract_import",
    "latest_import_id",
    "probe_interactions",
    "score_edge",
]
