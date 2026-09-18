"""Witness policy: drive a game along a combo's ordered links.

Split out of ``combo_discovery.witness``; behaviour is unchanged.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

from ..env import Answer
from ..generated import forge_env_pb2 as pb
from ..runner import DecisionContext, default_policy
from .scenario import LinkPlan, _is_attachment, _type_line_for, link_plans

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
