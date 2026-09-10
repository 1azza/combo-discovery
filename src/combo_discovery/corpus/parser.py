"""Pure parser for Forge card scripts (``cardsfolder/<letter>/*.txt``).

Forge scripts are line oriented ``Key:Value`` text.  Multiface cards live in a
single file: the front face ends at ``AlternateMode:<Type>``, a blank line and a
marker line (``ALTERNATE`` or ``SPECIALIZE:<COLOR>``) precede the next face.

This module is deliberately free of I/O and of any database knowledge: it turns
one script *text* into a normalized :class:`ParsedCard`.  It never raises on
malformed input — unrecoverable lines are recorded in ``parse_errors`` as
``{line, reason}`` so the importer can persist them instead of dropping cards.

Grammar notes (measured against the 33,688-script Forge cardsfolder):

* the first colon always separates the top level key from the value; values may
  themselves contain colons (``Variant:...``, ``SVar:Name:DB$ ...``);
* effect lines split into ``|`` separated ``Param$ Value`` segments, but a value
  may itself contain a ``|`` — segments without a ``$`` are folded back into the
  previous value;
* ``SVar:<Name>:<expr>`` is an *expression* unless ``expr`` starts with
  ``AB$``/``SP$``/``DB$``/``ST$``, in which case it is an effect definition;
* trigger/static/replacement lines carry their mode in ``Mode$``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Top-level keys we surface in :class:`Face` (or the card).
_FACE_KEYS = {
    "Name": "name",
    "ManaCost": "mana_cost",
    "Types": "types",
    "PT": "pt",
    "Oracle": "oracle",
    "Colors": "colors",
    "Loyalty": "loyalty",
    "Defense": "defense",
    "Text": "text",
}

# Extra top-level keys kept verbatim (recognised Forge syntax, not normalized).
_EXTRA_KEYS = {
    "DeckHas",
    "DeckNeeds",
    "DeckHints",
    "DeckRule",
    "Draft",
    "HandLifeModifier",
    "Lights",
    "DBCleanup",
    "ODeckHints",
    "SETCOLORID",
}

_EFFECT_KIND_BY_PREFIX = {
    "A": "ability",
    "T": "trigger",
    "S": "static",
    "R": "replacement",
}

# Prefixes allowed on an ``A:`` / ``SVar`` effect segment.
_EFFECT_TYPES = ("AB", "SP", "DB", "ST")

# Where an effect is active when the script does not say.  Activated abilities
# (AB) live on the battlefield, spells (SP) on the stack; derived effects are
# only meaningful through their parent, so no default is assumed for them.
_DEFAULT_ZONE = {"AB": "Battlefield", "SP": "Stack"}

# Params consulted (in order) for an explicit zone.
_ZONE_KEYS = ("Zone", "TriggerZones", "AffectedZone", "ActiveZones", "PumpZone")

# Marker lines that begin a new face.
_ALTERNATE_MARKER = "ALTERNATE"


@dataclass
class ParseError:
    """One malformed line, kept instead of raising."""

    line: int
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {"line": self.line, "reason": self.reason}


@dataclass
class Face:
    """The card-identity fields of a single face."""

    index: int = 0
    name: str = ""
    mana_cost: str = ""
    types: str = ""
    pt: str = ""
    oracle: str = ""
    colors: str = ""
    loyalty: str = ""
    defense: str = ""
    text: str = ""
    marker: str | None = None  # ALTERNATE / SPECIALIZE:COLOR / None (front)

    def as_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "name": self.name,
            "mana_cost": self.mana_cost,
            "types": self.types,
            "pt": self.pt,
            "oracle": self.oracle,
            "colors": self.colors,
            "loyalty": self.loyalty,
            "defense": self.defense,
            "text": self.text,
            "marker": self.marker,
        }


@dataclass
class Ability:
    """A ``K:<Keyword>[:arg...]`` line."""

    keyword: str
    args: tuple[str, ...]
    raw: str
    line: int = 0
    face: int = 0

    @property
    def arg_text(self) -> str:
        return ":".join(self.args)


@dataclass
class SVar:
    """A named ``SVar:<Name>:<expression>`` entry."""

    name: str
    expression: str
    kind: str  # AB | SP | DB | ST | expression
    line: int = 0
    face: int = 0

    @property
    def is_effect(self) -> bool:
        return self.kind in _EFFECT_TYPES


@dataclass
class Effect:
    """One ``A:`` / ``T:`` / ``S:`` / ``R:`` line (or ``SVar`` effect)."""

    kind: str  # ability | trigger | static | replacement
    ability_type: str  # AB/SP/DB/ST, or the trigger/static/replacement mode
    verb: str  # e.g. CopyPermanent / Phase / Continuous
    params: dict[str, str] = field(default_factory=dict)
    description: str = ""
    line: int = 0
    face: int = 0
    svar_name: str | None = None

    @property
    def zone(self) -> str | None:
        for key in _ZONE_KEYS:
            value = self.params.get(key)
            if value:
                return value
        return _DEFAULT_ZONE.get(self.ability_type)

    @property
    def is_optional(self) -> bool:
        if any("optional" in key.lower() for key in self.params):
            return True
        target_min = self.params.get("TargetMin", "").strip()
        return target_min == "0"

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "ability_type": self.ability_type,
            "verb": self.verb,
            "params": dict(self.params),
            "description": self.description,
            "line": self.line,
            "face": self.face,
            "svar_name": self.svar_name,
        }


@dataclass
class ParsedCard:
    """Normalized representation of one card script."""

    faces: list[Face] = field(default_factory=list)
    abilities: list[Ability] = field(default_factory=list)
    effects: list[Effect] = field(default_factory=list)
    svars: dict[str, SVar] = field(default_factory=dict)
    alternate_mode: str | None = None
    meld_pair: str | None = None
    copy_face_from: str | None = None
    flavor_name: str | None = None
    extra: dict[str, str] = field(default_factory=dict)
    parse_errors: list[ParseError] = field(default_factory=list)
    source: str = ""

    @property
    def name(self) -> str:
        return self.faces[0].name if self.faces else ""

    @property
    def is_multiface(self) -> bool:
        return len(self.faces) > 1

    @property
    def parse_ok(self) -> bool:
        return not self.parse_errors

    @property
    def all_effects(self) -> list[Effect]:
        """Every effect, including ``DB``/``AB``/``SP``/``ST`` SVar definitions."""
        effects = list(self.effects)
        for svar in self.svars.values():
            if not svar.is_effect:
                continue
            parsed = parse_effect("A", svar.expression, svar.line, svar.face, svar_name=svar.name)
            if parsed is not None:
                effects.append(parsed)
        return effects


def _unescape_oracle(value: str) -> str:
    """Forge stores literal ``\\n``; normalize for display/consistency."""
    return value.replace("\\n", "\n")


def _split_params(value: str) -> list[str]:
    """Split a ``Param$ Value | Param$ Value`` body, preserving ``|`` in values."""
    segments: list[str] = []
    for raw in value.split("|"):
        segment = raw.strip()
        if not segment:
            continue
        if segments and "$" not in segment:
            # A pipe inside a value: fold it back into the previous segment.
            segments[-1] = f"{segments[-1]} | {segment}"
        else:
            segments.append(segment)
    return segments


def _parse_ability(value: str, line: int, face: int) -> Ability | None:
    text = value.strip()
    if not text:
        return None
    parts = [part.strip() for part in text.split(":")]
    keyword = parts[0]
    if not keyword:
        return None
    return Ability(keyword=keyword, args=tuple(parts[1:]), raw=text, line=line, face=face)


def parse_effect(
    prefix: str, value: str, line: int, face: int, *, svar_name: str | None = None
) -> Effect | None:
    kind = _EFFECT_KIND_BY_PREFIX.get(prefix)
    if kind is None:
        return None
    segments = _split_params(value)
    if not segments or "$" not in segments[0]:
        return None
    ability_type, verb = segments[0].split("$", 1)
    ability_type = ability_type.strip()
    verb = verb.strip()
    if not verb:
        return None
    params: dict[str, str] = {}
    for segment in segments[1:]:
        if "$" not in segment:
            continue
        key, val = segment.split("$", 1)
        key = key.strip()
        if key:
            params[key] = val.strip()
    description = (
        params.get("SpellDescription")
        or params.get("TriggerDescription")
        or params.get("Description")
        or ""
    )
    return Effect(
        kind=kind,
        ability_type=ability_type if kind == "ability" else verb,
        verb=verb,
        params=params,
        description=description,
        line=line,
        face=face,
        svar_name=svar_name,
    )


def _parse_svar(value: str, line: int, face: int) -> SVar | None:
    if ":" not in value:
        return None
    name, expression = value.split(":", 1)
    name = name.strip()
    expression = expression.strip()
    if not name or not expression:
        return None
    kind = "expression"
    for prefix in _EFFECT_TYPES:
        if expression.startswith(f"{prefix}$"):
            kind = prefix
            break
    return SVar(name=name, expression=expression, kind=kind, line=line, face=face)


def _is_marker(line: str) -> bool:
    return line == _ALTERNATE_MARKER or line.startswith("SPECIALIZE:")


def parse_script(text: str, *, source: str = "") -> ParsedCard:
    """Parse one Forge card script.  Never raises; malformed lines land in
    :attr:`ParsedCard.parse_errors`."""
    card = ParsedCard(source=source)
    front = Face(index=0, marker=None)
    card.faces.append(front)
    current = front

    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _is_marker(stripped):
            current = Face(index=len(card.faces), marker=stripped)
            card.faces.append(current)
            continue
        if ":" not in raw:
            card.parse_errors.append(ParseError(lineno, "not a Key:Value line"))
            continue

        key, value = raw.split(":", 1)
        key = key.strip()
        if not key:
            card.parse_errors.append(ParseError(lineno, "empty key"))
            continue
        value = value.strip() if key != "Oracle" else value

        try:
            _apply(card, current, key, value, lineno)
        except Exception as exc:  # noqa: BLE001 - a parser must never crash
            card.parse_errors.append(ParseError(lineno, f"{type(exc).__name__}: {exc}"))

    return card


def _apply(card: ParsedCard, face: Face, key: str, value: str, lineno: int) -> None:
    if key in _FACE_KEYS:
        attr = _FACE_KEYS[key]
        setattr(face, attr, _unescape_oracle(value) if attr == "oracle" else value)
        return

    if key == "K":
        ability = _parse_ability(value, lineno, face.index)
        if ability is None:
            raise ValueError("empty keyword")
        card.abilities.append(ability)
        return

    if key in _EFFECT_KIND_BY_PREFIX:
        effect = parse_effect(key, value, lineno, face.index)
        if effect is None:
            raise ValueError(f"malformed {key}: effect line")
        card.effects.append(effect)
        return

    if key == "SVar":
        svar = _parse_svar(value, lineno, face.index)
        if svar is None:
            raise ValueError("malformed SVar (expected SVar:Name:expression)")
        # Names are face-scoped in Forge; disambiguate collisions so a
        # multi-face card keeps every definition (a common pattern).
        svar_key = svar.name
        if svar_key in card.svars:
            svar_key = f"{svar.name}#f{svar.face}"
            suffix = 2
            while svar_key in card.svars:
                svar_key = f"{svar.name}#f{svar.face}.{suffix}"
                suffix += 1
        card.svars[svar_key] = svar
        return

    if key == "AlternateMode":
        card.alternate_mode = value
        return
    if key == "MeldPair":
        card.meld_pair = value
        return
    if key == "CopyFaceFrom":
        card.copy_face_from = value
        return
    if key == "Variant":
        _apply_variant(card, value)
        return
    if key == "SPECIALIZE":
        # Only reachable if a marker was not seen first; keep it verbatim.
        card.extra[key] = value
        return

    # Recognised-but-unmodeled or future keys are stored, never an error.
    card.extra[key] = value


def _apply_variant(card: ParsedCard, value: str) -> None:
    """``Variant:UniversesWithin:FlavorName:<name>`` -> flavour alias."""
    parts = value.split(":")
    for i, part in enumerate(parts):
        if part == "FlavorName" and i + 1 < len(parts):
            card.flavor_name = ":".join(parts[i + 1 :]).strip() or None
            return
    card.extra["Variant"] = value


__all__ = [
    "Ability",
    "Effect",
    "Face",
    "ParseError",
    "ParsedCard",
    "SVar",
    "parse_effect",
    "parse_script",
]
