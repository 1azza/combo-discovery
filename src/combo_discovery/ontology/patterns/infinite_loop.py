"""``infinite_etb_loop``: copy engine + ETB untapper in the same engine."""

from __future__ import annotations

from collections.abc import Iterator

from .. import vocabulary as vocab
from ..restrictions import check_compatibility
from .base import (
    _IMPACT_PREDICATES,
    CardView,
    Edge,
    PatternDef,
    _evidence,
    _make_edge,
    _same_ability,
    check_ability_link,
)


def infinite_etb_loop(
    views: dict[int, CardView], stats: dict[str, int] | None = None
) -> Iterator[Edge]:
    stats = stats if stats is not None else {}
    engines = [v for v in views.values() if v.has(vocab.TAPS_COST)
               and v.any_of(_IMPACT_PREDICATES)]
    untappers = [v for v in views.values() if v.has(vocab.UNTAPS) and v.has(vocab.ETB_TRIGGER)]
    for engine in engines:
        impact = engine.any_of(_IMPACT_PREDICATES)
        if impact is None:
            continue
        # Hard gate: the engine must re-trigger the partner's ETB by copying it.
        # A tap-cost engine with no copy predicate needs a third card for a loop;
        # that synergy is out of scope for this pattern.
        if engine.first(vocab.COPIES_CREATURE) is None:
            stats["engine_no_copy_pairs"] = stats.get("engine_no_copy_pairs", 0) + sum(
                1 for partner in untappers if partner.card_id != engine.card_id
            )
            continue
        # The tap cost and the impact must be the same activated ability.
        if not _same_ability(engine.first(vocab.TAPS_COST), engine.first(impact)):
            stats["engine_split_ability"] = stats.get("engine_split_ability", 0) + 1
            continue
        tap = engine.first(vocab.TAPS_COST)
        tap_verb = (tap.params.get("verb") if tap is not None else "") or ""
        for partner in untappers:
            if engine.card_id == partner.card_id:
                continue
            # Precision gates: target type/controller, copyability and
            # ability-scope must all hold.
            compat = check_compatibility(engine, partner)
            if not compat.target_ok:
                stats["target_mismatch"] = stats.get("target_mismatch", 0) + 1
                continue
            if compat.copy.status != "compatible":
                stats["copy_mismatch"] = stats.get("copy_mismatch", 0) + 1
                continue
            link = check_ability_link(partner)
            if not link.linked:
                stats["unlinked"] = stats.get("unlinked", 0) + 1
                continue
            untap = partner.first(vocab.UNTAPS)
            untap_verb = (untap.params.get("verb") if untap is not None else "") or ""
            mechanism = (
                f"{engine.name} has a tap-cost {tap_verb} engine "
                f"({impact.replace('_', ' ').lower()}); {partner.name}'s "
                f"enters-the-battlefield trigger untaps {compat.target_raw or 'a permanent'}, "
                f"untapping {engine.name} to repeat the loop."
            )
            copy_engine = impact == vocab.COPIES_CREATURE
            bonus = 0.10 if copy_engine else 0.0
            if copy_engine and partner.context.is_creature:
                bonus += 0.05
            if untap_verb == "TapOrUntap":
                bonus -= 0.03  # may tap instead of untap (weaker signal)
            marker = compat.evidence_entry()
            marker.update({
                "ability_linked": True,
                "link_kind": link.kind,
                "ability_ref": link.ability_ref,
                "root_ability_ref": link.root_ref,
                "via": list(link.via),
            })
            yield _make_edge(
                "infinite_etb_loop", engine, partner, mechanism,
                [_evidence(engine, vocab.TAPS_COST), _evidence(engine, impact),
                 _evidence(partner, vocab.UNTAPS), _evidence(partner, vocab.ETB_TRIGGER),
                 marker],
                direction="mutual",
                score_bonus=bonus,
            )


PATTERN = PatternDef(
    name="infinite_etb_loop",
    description="Copy engine with a tap-cost ability plus an enters-the-battlefield "
                "untapper that is part of the same repeatable engine.",
    rule={
        "engine": ["TAPS_COST", "& COPIES_CREATURE", "& same ability"],
        "partner": ["UNTAPS", "& ETB_TRIGGER", "& ability-linked (no conditional gate)"],
        "gates": ["ability-scope: untap must be built into the self-ETB engine",
                  "copyability hard gate: engine must be able to copy the partner",
                  "reject FirstAttack/Delirium/RolledDie/ActivationLimit/turn/phase gates",
                  "untapper target type/controller must match engine"],
        "direction": "mutual",
        "confidence": "high",
    },
    matcher=infinite_etb_loop,
)

__all__ = ["PATTERN", "infinite_etb_loop"]
