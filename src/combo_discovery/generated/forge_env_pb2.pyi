from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class PlayerType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PLAYER_TYPE_UNSPECIFIED: _ClassVar[PlayerType]
    PLAYER_TYPE_REMOTE: _ClassVar[PlayerType]
    PLAYER_TYPE_GOLDFISH: _ClassVar[PlayerType]
    PLAYER_TYPE_FORGE_AI: _ClassVar[PlayerType]

class Outcome(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    OUTCOME_UNSPECIFIED: _ClassVar[Outcome]
    OUTCOME_WIN: _ClassVar[Outcome]
    OUTCOME_DRAW: _ClassVar[Outcome]
    OUTCOME_TURN_LIMIT: _ClassVar[Outcome]
    OUTCOME_TIMEOUT: _ClassVar[Outcome]
    OUTCOME_ERROR: _ClassVar[Outcome]
    OUTCOME_STOPPED: _ClassVar[Outcome]

class DecisionType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DECISION_TYPE_UNSPECIFIED: _ClassVar[DecisionType]
    DECISION_TYPE_PRIORITY: _ClassVar[DecisionType]
    DECISION_TYPE_MULLIGAN_KEEP: _ClassVar[DecisionType]
    DECISION_TYPE_MULLIGAN_TUCK: _ClassVar[DecisionType]
    DECISION_TYPE_DECLARE_ATTACKERS: _ClassVar[DecisionType]
    DECISION_TYPE_DECLARE_BLOCKERS: _ClassVar[DecisionType]
    DECISION_TYPE_ASSIGN_COMBAT_DAMAGE: _ClassVar[DecisionType]
    DECISION_TYPE_ORDER_BLOCKERS: _ClassVar[DecisionType]
    DECISION_TYPE_CHOOSE_CARDS: _ClassVar[DecisionType]
    DECISION_TYPE_ANNOUNCE: _ClassVar[DecisionType]
    DECISION_TYPE_SCRY_ARRANGE: _ClassVar[DecisionType]
    DECISION_TYPE_CHOOSE_TARGETS: _ClassVar[DecisionType]
    DECISION_TYPE_CHOOSE_MODE: _ClassVar[DecisionType]
    DECISION_TYPE_OPTIONAL_COSTS: _ClassVar[DecisionType]
PLAYER_TYPE_UNSPECIFIED: PlayerType
PLAYER_TYPE_REMOTE: PlayerType
PLAYER_TYPE_GOLDFISH: PlayerType
PLAYER_TYPE_FORGE_AI: PlayerType
OUTCOME_UNSPECIFIED: Outcome
OUTCOME_WIN: Outcome
OUTCOME_DRAW: Outcome
OUTCOME_TURN_LIMIT: Outcome
OUTCOME_TIMEOUT: Outcome
OUTCOME_ERROR: Outcome
OUTCOME_STOPPED: Outcome
DECISION_TYPE_UNSPECIFIED: DecisionType
DECISION_TYPE_PRIORITY: DecisionType
DECISION_TYPE_MULLIGAN_KEEP: DecisionType
DECISION_TYPE_MULLIGAN_TUCK: DecisionType
DECISION_TYPE_DECLARE_ATTACKERS: DecisionType
DECISION_TYPE_DECLARE_BLOCKERS: DecisionType
DECISION_TYPE_ASSIGN_COMBAT_DAMAGE: DecisionType
DECISION_TYPE_ORDER_BLOCKERS: DecisionType
DECISION_TYPE_CHOOSE_CARDS: DecisionType
DECISION_TYPE_ANNOUNCE: DecisionType
DECISION_TYPE_SCRY_ARRANGE: DecisionType
DECISION_TYPE_CHOOSE_TARGETS: DecisionType
DECISION_TYPE_CHOOSE_MODE: DecisionType
DECISION_TYPE_OPTIONAL_COSTS: DecisionType

class Empty(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class Pong(_message.Message):
    __slots__ = ("version", "game_active", "protocol_version")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    GAME_ACTIVE_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    version: str
    game_active: bool
    protocol_version: int
    def __init__(self, version: _Optional[str] = ..., game_active: _Optional[bool] = ..., protocol_version: _Optional[int] = ...) -> None: ...

class DeckSpec(_message.Message):
    __slots__ = ("name", "path")
    NAME_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    name: str
    path: str
    def __init__(self, name: _Optional[str] = ..., path: _Optional[str] = ...) -> None: ...

class StartRequest(_message.Message):
    __slots__ = ("decks", "seed", "player_types", "max_turns", "timeout_seconds", "force_stop_active")
    DECKS_FIELD_NUMBER: _ClassVar[int]
    SEED_FIELD_NUMBER: _ClassVar[int]
    PLAYER_TYPES_FIELD_NUMBER: _ClassVar[int]
    MAX_TURNS_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_SECONDS_FIELD_NUMBER: _ClassVar[int]
    FORCE_STOP_ACTIVE_FIELD_NUMBER: _ClassVar[int]
    decks: _containers.RepeatedCompositeFieldContainer[DeckSpec]
    seed: int
    player_types: _containers.RepeatedScalarFieldContainer[PlayerType]
    max_turns: int
    timeout_seconds: int
    force_stop_active: bool
    def __init__(self, decks: _Optional[_Iterable[_Union[DeckSpec, _Mapping]]] = ..., seed: _Optional[int] = ..., player_types: _Optional[_Iterable[_Union[PlayerType, str]]] = ..., max_turns: _Optional[int] = ..., timeout_seconds: _Optional[int] = ..., force_stop_active: _Optional[bool] = ...) -> None: ...

class StartResponse(_message.Message):
    __slots__ = ("game_id", "starting_life")
    GAME_ID_FIELD_NUMBER: _ClassVar[int]
    STARTING_LIFE_FIELD_NUMBER: _ClassVar[int]
    game_id: int
    starting_life: _containers.RepeatedScalarFieldContainer[int]
    def __init__(self, game_id: _Optional[int] = ..., starting_life: _Optional[_Iterable[int]] = ...) -> None: ...

class GameQuery(_message.Message):
    __slots__ = ("game_id",)
    GAME_ID_FIELD_NUMBER: _ClassVar[int]
    game_id: int
    def __init__(self, game_id: _Optional[int] = ...) -> None: ...

class GameViewQuery(_message.Message):
    __slots__ = ("game_id", "view_as_player")
    GAME_ID_FIELD_NUMBER: _ClassVar[int]
    VIEW_AS_PLAYER_FIELD_NUMBER: _ClassVar[int]
    game_id: int
    view_as_player: int
    def __init__(self, game_id: _Optional[int] = ..., view_as_player: _Optional[int] = ...) -> None: ...

class Option(_message.Message):
    __slots__ = ("id", "kind", "card_name", "target_names", "description")
    ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    CARD_NAME_FIELD_NUMBER: _ClassVar[int]
    TARGET_NAMES_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    id: int
    kind: str
    card_name: str
    target_names: _containers.RepeatedScalarFieldContainer[str]
    description: str
    def __init__(self, id: _Optional[int] = ..., kind: _Optional[str] = ..., card_name: _Optional[str] = ..., target_names: _Optional[_Iterable[str]] = ..., description: _Optional[str] = ...) -> None: ...

class CardCandidate(_message.Message):
    __slots__ = ("card_id", "name")
    CARD_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    card_id: int
    name: str
    def __init__(self, card_id: _Optional[int] = ..., name: _Optional[str] = ...) -> None: ...

class DecisionRequest(_message.Message):
    __slots__ = ("game_id", "decision_id", "player", "turn", "phase", "decision_type", "prompt", "options", "candidates", "min_choices", "max_choices", "optional", "min_number", "max_number", "defender_players", "attacker_cards", "damage_amount", "damage_source_card", "cards_to_return", "spell_description", "allow_repeat", "mode_options", "mandatory")
    GAME_ID_FIELD_NUMBER: _ClassVar[int]
    DECISION_ID_FIELD_NUMBER: _ClassVar[int]
    PLAYER_FIELD_NUMBER: _ClassVar[int]
    TURN_FIELD_NUMBER: _ClassVar[int]
    PHASE_FIELD_NUMBER: _ClassVar[int]
    DECISION_TYPE_FIELD_NUMBER: _ClassVar[int]
    PROMPT_FIELD_NUMBER: _ClassVar[int]
    OPTIONS_FIELD_NUMBER: _ClassVar[int]
    CANDIDATES_FIELD_NUMBER: _ClassVar[int]
    MIN_CHOICES_FIELD_NUMBER: _ClassVar[int]
    MAX_CHOICES_FIELD_NUMBER: _ClassVar[int]
    OPTIONAL_FIELD_NUMBER: _ClassVar[int]
    MIN_NUMBER_FIELD_NUMBER: _ClassVar[int]
    MAX_NUMBER_FIELD_NUMBER: _ClassVar[int]
    DEFENDER_PLAYERS_FIELD_NUMBER: _ClassVar[int]
    ATTACKER_CARDS_FIELD_NUMBER: _ClassVar[int]
    DAMAGE_AMOUNT_FIELD_NUMBER: _ClassVar[int]
    DAMAGE_SOURCE_CARD_FIELD_NUMBER: _ClassVar[int]
    CARDS_TO_RETURN_FIELD_NUMBER: _ClassVar[int]
    SPELL_DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    ALLOW_REPEAT_FIELD_NUMBER: _ClassVar[int]
    MODE_OPTIONS_FIELD_NUMBER: _ClassVar[int]
    MANDATORY_FIELD_NUMBER: _ClassVar[int]
    game_id: int
    decision_id: int
    player: int
    turn: int
    phase: str
    decision_type: DecisionType
    prompt: str
    options: _containers.RepeatedCompositeFieldContainer[Option]
    candidates: _containers.RepeatedCompositeFieldContainer[CardCandidate]
    min_choices: int
    max_choices: int
    optional: bool
    min_number: int
    max_number: int
    defender_players: _containers.RepeatedScalarFieldContainer[int]
    attacker_cards: _containers.RepeatedScalarFieldContainer[int]
    damage_amount: int
    damage_source_card: int
    cards_to_return: int
    spell_description: str
    allow_repeat: bool
    mode_options: _containers.RepeatedCompositeFieldContainer[ModeOption]
    mandatory: bool
    def __init__(self, game_id: _Optional[int] = ..., decision_id: _Optional[int] = ..., player: _Optional[int] = ..., turn: _Optional[int] = ..., phase: _Optional[str] = ..., decision_type: _Optional[_Union[DecisionType, str]] = ..., prompt: _Optional[str] = ..., options: _Optional[_Iterable[_Union[Option, _Mapping]]] = ..., candidates: _Optional[_Iterable[_Union[CardCandidate, _Mapping]]] = ..., min_choices: _Optional[int] = ..., max_choices: _Optional[int] = ..., optional: _Optional[bool] = ..., min_number: _Optional[int] = ..., max_number: _Optional[int] = ..., defender_players: _Optional[_Iterable[int]] = ..., attacker_cards: _Optional[_Iterable[int]] = ..., damage_amount: _Optional[int] = ..., damage_source_card: _Optional[int] = ..., cards_to_return: _Optional[int] = ..., spell_description: _Optional[str] = ..., allow_repeat: _Optional[bool] = ..., mode_options: _Optional[_Iterable[_Union[ModeOption, _Mapping]]] = ..., mandatory: _Optional[bool] = ...) -> None: ...

class IntList(_message.Message):
    __slots__ = ("values",)
    VALUES_FIELD_NUMBER: _ClassVar[int]
    values: _containers.RepeatedScalarFieldContainer[int]
    def __init__(self, values: _Optional[_Iterable[int]] = ...) -> None: ...

class AttackerAssignment(_message.Message):
    __slots__ = ("attacker_card", "defender_player")
    ATTACKER_CARD_FIELD_NUMBER: _ClassVar[int]
    DEFENDER_PLAYER_FIELD_NUMBER: _ClassVar[int]
    attacker_card: int
    defender_player: int
    def __init__(self, attacker_card: _Optional[int] = ..., defender_player: _Optional[int] = ...) -> None: ...

class BlockerAssignment(_message.Message):
    __slots__ = ("blocker_card", "attacker_card")
    BLOCKER_CARD_FIELD_NUMBER: _ClassVar[int]
    ATTACKER_CARD_FIELD_NUMBER: _ClassVar[int]
    blocker_card: int
    attacker_card: int
    def __init__(self, blocker_card: _Optional[int] = ..., attacker_card: _Optional[int] = ...) -> None: ...

class DamageAssignment(_message.Message):
    __slots__ = ("card_id", "player", "amount")
    CARD_ID_FIELD_NUMBER: _ClassVar[int]
    PLAYER_FIELD_NUMBER: _ClassVar[int]
    AMOUNT_FIELD_NUMBER: _ClassVar[int]
    card_id: int
    player: int
    amount: int
    def __init__(self, card_id: _Optional[int] = ..., player: _Optional[int] = ..., amount: _Optional[int] = ...) -> None: ...

class CardPartition(_message.Message):
    __slots__ = ("top", "bottom")
    TOP_FIELD_NUMBER: _ClassVar[int]
    BOTTOM_FIELD_NUMBER: _ClassVar[int]
    top: _containers.RepeatedScalarFieldContainer[int]
    bottom: _containers.RepeatedScalarFieldContainer[int]
    def __init__(self, top: _Optional[_Iterable[int]] = ..., bottom: _Optional[_Iterable[int]] = ...) -> None: ...

class AttackerList(_message.Message):
    __slots__ = ("values",)
    VALUES_FIELD_NUMBER: _ClassVar[int]
    values: _containers.RepeatedCompositeFieldContainer[AttackerAssignment]
    def __init__(self, values: _Optional[_Iterable[_Union[AttackerAssignment, _Mapping]]] = ...) -> None: ...

class BlockerList(_message.Message):
    __slots__ = ("values",)
    VALUES_FIELD_NUMBER: _ClassVar[int]
    values: _containers.RepeatedCompositeFieldContainer[BlockerAssignment]
    def __init__(self, values: _Optional[_Iterable[_Union[BlockerAssignment, _Mapping]]] = ...) -> None: ...

class DamageList(_message.Message):
    __slots__ = ("values",)
    VALUES_FIELD_NUMBER: _ClassVar[int]
    values: _containers.RepeatedCompositeFieldContainer[DamageAssignment]
    def __init__(self, values: _Optional[_Iterable[_Union[DamageAssignment, _Mapping]]] = ...) -> None: ...

class ModeOption(_message.Message):
    __slots__ = ("id", "description")
    ID_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    id: int
    description: str
    def __init__(self, id: _Optional[int] = ..., description: _Optional[str] = ...) -> None: ...

class TargetSelection(_message.Message):
    __slots__ = ("card_ids", "player_slots")
    CARD_IDS_FIELD_NUMBER: _ClassVar[int]
    PLAYER_SLOTS_FIELD_NUMBER: _ClassVar[int]
    card_ids: _containers.RepeatedScalarFieldContainer[int]
    player_slots: _containers.RepeatedScalarFieldContainer[int]
    def __init__(self, card_ids: _Optional[_Iterable[int]] = ..., player_slots: _Optional[_Iterable[int]] = ...) -> None: ...

class DecisionSubmit(_message.Message):
    __slots__ = ("game_id", "decision_id", "option_id", "boolean_answer", "card_ids", "number_answer", "attackers", "blockers", "damage", "scry", "targets")
    GAME_ID_FIELD_NUMBER: _ClassVar[int]
    DECISION_ID_FIELD_NUMBER: _ClassVar[int]
    OPTION_ID_FIELD_NUMBER: _ClassVar[int]
    BOOLEAN_ANSWER_FIELD_NUMBER: _ClassVar[int]
    CARD_IDS_FIELD_NUMBER: _ClassVar[int]
    NUMBER_ANSWER_FIELD_NUMBER: _ClassVar[int]
    ATTACKERS_FIELD_NUMBER: _ClassVar[int]
    BLOCKERS_FIELD_NUMBER: _ClassVar[int]
    DAMAGE_FIELD_NUMBER: _ClassVar[int]
    SCRY_FIELD_NUMBER: _ClassVar[int]
    TARGETS_FIELD_NUMBER: _ClassVar[int]
    game_id: int
    decision_id: int
    option_id: int
    boolean_answer: bool
    card_ids: IntList
    number_answer: int
    attackers: AttackerList
    blockers: BlockerList
    damage: DamageList
    scry: CardPartition
    targets: TargetSelection
    def __init__(self, game_id: _Optional[int] = ..., decision_id: _Optional[int] = ..., option_id: _Optional[int] = ..., boolean_answer: _Optional[bool] = ..., card_ids: _Optional[_Union[IntList, _Mapping]] = ..., number_answer: _Optional[int] = ..., attackers: _Optional[_Union[AttackerList, _Mapping]] = ..., blockers: _Optional[_Union[BlockerList, _Mapping]] = ..., damage: _Optional[_Union[DamageList, _Mapping]] = ..., scry: _Optional[_Union[CardPartition, _Mapping]] = ..., targets: _Optional[_Union[TargetSelection, _Mapping]] = ...) -> None: ...

class GameEvent(_message.Message):
    __slots__ = ("seq", "game_id", "turn", "phase", "type", "player", "card_id", "card_name", "detail_raw", "old_value", "new_value", "extra")
    SEQ_FIELD_NUMBER: _ClassVar[int]
    GAME_ID_FIELD_NUMBER: _ClassVar[int]
    TURN_FIELD_NUMBER: _ClassVar[int]
    PHASE_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    PLAYER_FIELD_NUMBER: _ClassVar[int]
    CARD_ID_FIELD_NUMBER: _ClassVar[int]
    CARD_NAME_FIELD_NUMBER: _ClassVar[int]
    DETAIL_RAW_FIELD_NUMBER: _ClassVar[int]
    OLD_VALUE_FIELD_NUMBER: _ClassVar[int]
    NEW_VALUE_FIELD_NUMBER: _ClassVar[int]
    EXTRA_FIELD_NUMBER: _ClassVar[int]
    seq: int
    game_id: int
    turn: int
    phase: str
    type: str
    player: int
    card_id: int
    card_name: str
    detail_raw: str
    old_value: int
    new_value: int
    extra: str
    def __init__(self, seq: _Optional[int] = ..., game_id: _Optional[int] = ..., turn: _Optional[int] = ..., phase: _Optional[str] = ..., type: _Optional[str] = ..., player: _Optional[int] = ..., card_id: _Optional[int] = ..., card_name: _Optional[str] = ..., detail_raw: _Optional[str] = ..., old_value: _Optional[int] = ..., new_value: _Optional[int] = ..., extra: _Optional[str] = ...) -> None: ...

class StepResult(_message.Message):
    __slots__ = ("events", "decision_pending", "game_over")
    EVENTS_FIELD_NUMBER: _ClassVar[int]
    DECISION_PENDING_FIELD_NUMBER: _ClassVar[int]
    GAME_OVER_FIELD_NUMBER: _ClassVar[int]
    events: _containers.RepeatedCompositeFieldContainer[GameEvent]
    decision_pending: bool
    game_over: bool
    def __init__(self, events: _Optional[_Iterable[_Union[GameEvent, _Mapping]]] = ..., decision_pending: _Optional[bool] = ..., game_over: _Optional[bool] = ...) -> None: ...

class PollRequest(_message.Message):
    __slots__ = ("game_id", "cursor")
    GAME_ID_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    game_id: int
    cursor: int
    def __init__(self, game_id: _Optional[int] = ..., cursor: _Optional[int] = ...) -> None: ...

class EventBatch(_message.Message):
    __slots__ = ("events", "next_cursor", "game_over")
    EVENTS_FIELD_NUMBER: _ClassVar[int]
    NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    GAME_OVER_FIELD_NUMBER: _ClassVar[int]
    events: _containers.RepeatedCompositeFieldContainer[GameEvent]
    next_cursor: int
    game_over: bool
    def __init__(self, events: _Optional[_Iterable[_Union[GameEvent, _Mapping]]] = ..., next_cursor: _Optional[int] = ..., game_over: _Optional[bool] = ...) -> None: ...

class TypedCounter(_message.Message):
    __slots__ = ("type", "count")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    COUNT_FIELD_NUMBER: _ClassVar[int]
    type: str
    count: int
    def __init__(self, type: _Optional[str] = ..., count: _Optional[int] = ...) -> None: ...

class Permanent(_message.Message):
    __slots__ = ("id", "card_name", "controller", "owner", "tapped", "attacking", "blocking", "counters", "attachments", "is_token", "type_line", "typed_counters", "damage")
    class CountersEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: int
        def __init__(self, key: _Optional[str] = ..., value: _Optional[int] = ...) -> None: ...
    ID_FIELD_NUMBER: _ClassVar[int]
    CARD_NAME_FIELD_NUMBER: _ClassVar[int]
    CONTROLLER_FIELD_NUMBER: _ClassVar[int]
    OWNER_FIELD_NUMBER: _ClassVar[int]
    TAPPED_FIELD_NUMBER: _ClassVar[int]
    ATTACKING_FIELD_NUMBER: _ClassVar[int]
    BLOCKING_FIELD_NUMBER: _ClassVar[int]
    COUNTERS_FIELD_NUMBER: _ClassVar[int]
    ATTACHMENTS_FIELD_NUMBER: _ClassVar[int]
    IS_TOKEN_FIELD_NUMBER: _ClassVar[int]
    TYPE_LINE_FIELD_NUMBER: _ClassVar[int]
    TYPED_COUNTERS_FIELD_NUMBER: _ClassVar[int]
    DAMAGE_FIELD_NUMBER: _ClassVar[int]
    id: int
    card_name: str
    controller: int
    owner: int
    tapped: bool
    attacking: bool
    blocking: bool
    counters: _containers.ScalarMap[str, int]
    attachments: _containers.RepeatedScalarFieldContainer[str]
    is_token: bool
    type_line: str
    typed_counters: _containers.RepeatedCompositeFieldContainer[TypedCounter]
    damage: int
    def __init__(self, id: _Optional[int] = ..., card_name: _Optional[str] = ..., controller: _Optional[int] = ..., owner: _Optional[int] = ..., tapped: _Optional[bool] = ..., attacking: _Optional[bool] = ..., blocking: _Optional[bool] = ..., counters: _Optional[_Mapping[str, int]] = ..., attachments: _Optional[_Iterable[str]] = ..., is_token: _Optional[bool] = ..., type_line: _Optional[str] = ..., typed_counters: _Optional[_Iterable[_Union[TypedCounter, _Mapping]]] = ..., damage: _Optional[int] = ...) -> None: ...

class StackEntry(_message.Message):
    __slots__ = ("stack_index", "sa_description", "card_name", "controller")
    STACK_INDEX_FIELD_NUMBER: _ClassVar[int]
    SA_DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    CARD_NAME_FIELD_NUMBER: _ClassVar[int]
    CONTROLLER_FIELD_NUMBER: _ClassVar[int]
    stack_index: int
    sa_description: str
    card_name: str
    controller: int
    def __init__(self, stack_index: _Optional[int] = ..., sa_description: _Optional[str] = ..., card_name: _Optional[str] = ..., controller: _Optional[int] = ...) -> None: ...

class ManaPool(_message.Message):
    __slots__ = ("white", "blue", "black", "red", "green", "colorless")
    WHITE_FIELD_NUMBER: _ClassVar[int]
    BLUE_FIELD_NUMBER: _ClassVar[int]
    BLACK_FIELD_NUMBER: _ClassVar[int]
    RED_FIELD_NUMBER: _ClassVar[int]
    GREEN_FIELD_NUMBER: _ClassVar[int]
    COLORLESS_FIELD_NUMBER: _ClassVar[int]
    white: int
    blue: int
    black: int
    red: int
    green: int
    colorless: int
    def __init__(self, white: _Optional[int] = ..., blue: _Optional[int] = ..., black: _Optional[int] = ..., red: _Optional[int] = ..., green: _Optional[int] = ..., colorless: _Optional[int] = ...) -> None: ...

class FullState(_message.Message):
    __slots__ = ("game_id", "turn", "phase", "active_player", "life", "hand", "battlefield", "graveyard", "library", "mana_pools", "recent_events", "state_hash", "typed_mana_pools", "stack", "exile", "command", "battlefield_cards")
    class ManaPoolsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: int
        def __init__(self, key: _Optional[str] = ..., value: _Optional[int] = ...) -> None: ...
    GAME_ID_FIELD_NUMBER: _ClassVar[int]
    TURN_FIELD_NUMBER: _ClassVar[int]
    PHASE_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_PLAYER_FIELD_NUMBER: _ClassVar[int]
    LIFE_FIELD_NUMBER: _ClassVar[int]
    HAND_FIELD_NUMBER: _ClassVar[int]
    BATTLEFIELD_FIELD_NUMBER: _ClassVar[int]
    GRAVEYARD_FIELD_NUMBER: _ClassVar[int]
    LIBRARY_FIELD_NUMBER: _ClassVar[int]
    MANA_POOLS_FIELD_NUMBER: _ClassVar[int]
    RECENT_EVENTS_FIELD_NUMBER: _ClassVar[int]
    STATE_HASH_FIELD_NUMBER: _ClassVar[int]
    TYPED_MANA_POOLS_FIELD_NUMBER: _ClassVar[int]
    STACK_FIELD_NUMBER: _ClassVar[int]
    EXILE_FIELD_NUMBER: _ClassVar[int]
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    BATTLEFIELD_CARDS_FIELD_NUMBER: _ClassVar[int]
    game_id: int
    turn: int
    phase: str
    active_player: int
    life: _containers.RepeatedScalarFieldContainer[int]
    hand: _containers.RepeatedCompositeFieldContainer[Zone]
    battlefield: _containers.RepeatedCompositeFieldContainer[Zone]
    graveyard: _containers.RepeatedCompositeFieldContainer[Zone]
    library: _containers.RepeatedCompositeFieldContainer[Zone]
    mana_pools: _containers.ScalarMap[str, int]
    recent_events: _containers.RepeatedCompositeFieldContainer[GameEvent]
    state_hash: str
    typed_mana_pools: _containers.RepeatedCompositeFieldContainer[ManaPool]
    stack: _containers.RepeatedCompositeFieldContainer[StackEntry]
    exile: _containers.RepeatedCompositeFieldContainer[Zone]
    command: _containers.RepeatedCompositeFieldContainer[Zone]
    battlefield_cards: _containers.RepeatedCompositeFieldContainer[Permanent]
    def __init__(self, game_id: _Optional[int] = ..., turn: _Optional[int] = ..., phase: _Optional[str] = ..., active_player: _Optional[int] = ..., life: _Optional[_Iterable[int]] = ..., hand: _Optional[_Iterable[_Union[Zone, _Mapping]]] = ..., battlefield: _Optional[_Iterable[_Union[Zone, _Mapping]]] = ..., graveyard: _Optional[_Iterable[_Union[Zone, _Mapping]]] = ..., library: _Optional[_Iterable[_Union[Zone, _Mapping]]] = ..., mana_pools: _Optional[_Mapping[str, int]] = ..., recent_events: _Optional[_Iterable[_Union[GameEvent, _Mapping]]] = ..., state_hash: _Optional[str] = ..., typed_mana_pools: _Optional[_Iterable[_Union[ManaPool, _Mapping]]] = ..., stack: _Optional[_Iterable[_Union[StackEntry, _Mapping]]] = ..., exile: _Optional[_Iterable[_Union[Zone, _Mapping]]] = ..., command: _Optional[_Iterable[_Union[Zone, _Mapping]]] = ..., battlefield_cards: _Optional[_Iterable[_Union[Permanent, _Mapping]]] = ...) -> None: ...

class Zone(_message.Message):
    __slots__ = ("cards", "permanents")
    CARDS_FIELD_NUMBER: _ClassVar[int]
    PERMANENTS_FIELD_NUMBER: _ClassVar[int]
    cards: _containers.RepeatedCompositeFieldContainer[CardRef]
    permanents: _containers.RepeatedScalarFieldContainer[int]
    def __init__(self, cards: _Optional[_Iterable[_Union[CardRef, _Mapping]]] = ..., permanents: _Optional[_Iterable[int]] = ...) -> None: ...

class CardRef(_message.Message):
    __slots__ = ("name", "count")
    NAME_FIELD_NUMBER: _ClassVar[int]
    COUNT_FIELD_NUMBER: _ClassVar[int]
    name: str
    count: int
    def __init__(self, name: _Optional[str] = ..., count: _Optional[int] = ...) -> None: ...

class StateToken(_message.Message):
    __slots__ = ("token", "game_id")
    TOKEN_FIELD_NUMBER: _ClassVar[int]
    GAME_ID_FIELD_NUMBER: _ClassVar[int]
    token: bytes
    game_id: int
    def __init__(self, token: _Optional[bytes] = ..., game_id: _Optional[int] = ...) -> None: ...

class SnapshotResponse(_message.Message):
    __slots__ = ("token", "state_hash")
    TOKEN_FIELD_NUMBER: _ClassVar[int]
    STATE_HASH_FIELD_NUMBER: _ClassVar[int]
    token: StateToken
    state_hash: str
    def __init__(self, token: _Optional[_Union[StateToken, _Mapping]] = ..., state_hash: _Optional[str] = ...) -> None: ...

class RestoreRequest(_message.Message):
    __slots__ = ("game_id", "token")
    GAME_ID_FIELD_NUMBER: _ClassVar[int]
    TOKEN_FIELD_NUMBER: _ClassVar[int]
    game_id: int
    token: bytes
    def __init__(self, game_id: _Optional[int] = ..., token: _Optional[bytes] = ...) -> None: ...

class CardSpec(_message.Message):
    __slots__ = ("name", "set", "tapped", "summoning_sick", "counters", "damage", "no_etb_triggers", "id", "attached_to")
    class CountersEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: int
        def __init__(self, key: _Optional[str] = ..., value: _Optional[int] = ...) -> None: ...
    NAME_FIELD_NUMBER: _ClassVar[int]
    SET_FIELD_NUMBER: _ClassVar[int]
    TAPPED_FIELD_NUMBER: _ClassVar[int]
    SUMMONING_SICK_FIELD_NUMBER: _ClassVar[int]
    COUNTERS_FIELD_NUMBER: _ClassVar[int]
    DAMAGE_FIELD_NUMBER: _ClassVar[int]
    NO_ETB_TRIGGERS_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    ATTACHED_TO_FIELD_NUMBER: _ClassVar[int]
    name: str
    set: str
    tapped: bool
    summoning_sick: bool
    counters: _containers.ScalarMap[str, int]
    damage: int
    no_etb_triggers: bool
    id: int
    attached_to: int
    def __init__(self, name: _Optional[str] = ..., set: _Optional[str] = ..., tapped: _Optional[bool] = ..., summoning_sick: _Optional[bool] = ..., counters: _Optional[_Mapping[str, int]] = ..., damage: _Optional[int] = ..., no_etb_triggers: _Optional[bool] = ..., id: _Optional[int] = ..., attached_to: _Optional[int] = ...) -> None: ...

class PlayerScenario(_message.Message):
    __slots__ = ("player", "life", "mana", "battlefield", "hand", "graveyard", "library", "exile")
    class ManaEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: int
        def __init__(self, key: _Optional[str] = ..., value: _Optional[int] = ...) -> None: ...
    PLAYER_FIELD_NUMBER: _ClassVar[int]
    LIFE_FIELD_NUMBER: _ClassVar[int]
    MANA_FIELD_NUMBER: _ClassVar[int]
    BATTLEFIELD_FIELD_NUMBER: _ClassVar[int]
    HAND_FIELD_NUMBER: _ClassVar[int]
    GRAVEYARD_FIELD_NUMBER: _ClassVar[int]
    LIBRARY_FIELD_NUMBER: _ClassVar[int]
    EXILE_FIELD_NUMBER: _ClassVar[int]
    player: int
    life: int
    mana: _containers.ScalarMap[str, int]
    battlefield: _containers.RepeatedCompositeFieldContainer[CardSpec]
    hand: _containers.RepeatedCompositeFieldContainer[CardSpec]
    graveyard: _containers.RepeatedCompositeFieldContainer[CardSpec]
    library: _containers.RepeatedCompositeFieldContainer[CardSpec]
    exile: _containers.RepeatedCompositeFieldContainer[CardSpec]
    def __init__(self, player: _Optional[int] = ..., life: _Optional[int] = ..., mana: _Optional[_Mapping[str, int]] = ..., battlefield: _Optional[_Iterable[_Union[CardSpec, _Mapping]]] = ..., hand: _Optional[_Iterable[_Union[CardSpec, _Mapping]]] = ..., graveyard: _Optional[_Iterable[_Union[CardSpec, _Mapping]]] = ..., library: _Optional[_Iterable[_Union[CardSpec, _Mapping]]] = ..., exile: _Optional[_Iterable[_Union[CardSpec, _Mapping]]] = ...) -> None: ...

class SetupScenarioRequest(_message.Message):
    __slots__ = ("game_id", "players", "active_player", "turn", "phase", "require_outstanding_decision")
    GAME_ID_FIELD_NUMBER: _ClassVar[int]
    PLAYERS_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_PLAYER_FIELD_NUMBER: _ClassVar[int]
    TURN_FIELD_NUMBER: _ClassVar[int]
    PHASE_FIELD_NUMBER: _ClassVar[int]
    REQUIRE_OUTSTANDING_DECISION_FIELD_NUMBER: _ClassVar[int]
    game_id: int
    players: _containers.RepeatedCompositeFieldContainer[PlayerScenario]
    active_player: int
    turn: int
    phase: str
    require_outstanding_decision: bool
    def __init__(self, game_id: _Optional[int] = ..., players: _Optional[_Iterable[_Union[PlayerScenario, _Mapping]]] = ..., active_player: _Optional[int] = ..., turn: _Optional[int] = ..., phase: _Optional[str] = ..., require_outstanding_decision: _Optional[bool] = ...) -> None: ...

class SetupScenarioResponse(_message.Message):
    __slots__ = ("state_hash", "applied_events")
    STATE_HASH_FIELD_NUMBER: _ClassVar[int]
    APPLIED_EVENTS_FIELD_NUMBER: _ClassVar[int]
    state_hash: str
    applied_events: int
    def __init__(self, state_hash: _Optional[str] = ..., applied_events: _Optional[int] = ...) -> None: ...

class GameOver(_message.Message):
    __slots__ = ("over", "winner", "reason", "outcome")
    OVER_FIELD_NUMBER: _ClassVar[int]
    WINNER_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    OUTCOME_FIELD_NUMBER: _ClassVar[int]
    over: bool
    winner: int
    reason: str
    outcome: Outcome
    def __init__(self, over: _Optional[bool] = ..., winner: _Optional[int] = ..., reason: _Optional[str] = ..., outcome: _Optional[_Union[Outcome, str]] = ...) -> None: ...
