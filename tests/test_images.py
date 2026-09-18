"""Tests for the Scryfall image-URL resolver (``combo_discovery.corpus.images``).

The module is pure string construction over a read-only DB and the cached
Scryfall bulk, so the tests build a tiny DB + bulk in ``tmp_path`` and never
touch the network or the real corpus.
"""

from __future__ import annotations

import gzip
import json
import sqlite3

import pytest

from combo_discovery.corpus import images

BEAR_ID = "aa11bb22-0000-0000-0000-000000000001"
DELVER_ID = "bb22cc33-0000-0000-0000-000000000002"
ADVENTURE_ID = "cc33dd44-0000-0000-0000-000000000003"
OTHER_ID = "dd44ee55-0000-0000-0000-000000000004"
ART_SERIES_ID = "ee55ff66-0000-0000-0000-000000000005"

BEAR_ORACLE = "11111111-1111-1111-1111-111111111111"
DELVER_ORACLE = "22222222-2222-2222-2222-222222222222"
ADVENTURE_ORACLE = "33333333-3333-3333-3333-333333333333"
MISALIGNED_ORACLE = "99999999-9999-9999-9999-999999999999"
ART_SERIES_ORACLE = "55555555-5555-5555-5555-555555555555"

#: (name, normalized_name, oracle_id or None)
_CARDS = [
    ("Test Bear", "test bear", BEAR_ORACLE),
    ("Test Delver", "test delver", DELVER_ORACLE),
    ("Adventure Card", "adventure card", ADVENTURE_ORACLE),
    ("Misaligned Card", "misaligned card", MISALIGNED_ORACLE),
    ("Art Series Card", "art series card", ART_SERIES_ORACLE),
    ("No Art Card", "no art card", None),
]


def _entry(name, card_id, oracle_id, *, layout, faces=0, top_image=True,
           face_images=True):
    entry = {"name": name, "id": card_id, "oracle_id": oracle_id, "layout": layout}
    if top_image:
        entry["image_uris"] = {"normal": "https://example.invalid/normal.jpg"}
    if faces:
        entry["card_faces"] = [
            {
                "name": f"{name} face {i}",
                "image_uris": {"normal": "x"} if face_images else {},
            }
            for i in range(faces)
        ]
    return entry


def _write_bulk(path):
    entries = [
        # Single-faced: one top-level image, no faces.
        _entry("Test Bear", BEAR_ID, BEAR_ORACLE, layout="normal"),
        # Transform DFC: no top-level image, both faces carry their own.
        _entry(
            "Test Delver // Test Bug", DELVER_ID, DELVER_ORACLE,
            layout="transform", faces=2, top_image=False,
        ),
        # Adventure: top-level image plus two faces sharing it -> one image.
        _entry(
            "Adventure Card // Small Adventure", ADVENTURE_ID, ADVENTURE_ORACLE,
            layout="adventure", faces=2, face_images=False,
        ),
        # Oracle id points here but the front name is a different card.
        _entry("Some Other Card", OTHER_ID, MISALIGNED_ORACLE, layout="normal"),
        # Art-series prints must not shadow a real card.
        _entry(
            "Art Series Card // Art Series Card", ART_SERIES_ID, ART_SERIES_ORACLE,
            layout="art_series", faces=2, top_image=False,
        ),
    ]
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


def _write_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE cards (id INTEGER PRIMARY KEY, normalized_name TEXT,"
        " scryfall_oracle_id TEXT)"
    )
    conn.execute("CREATE TABLE card_oracle_ids (card_id INTEGER, oracle_id TEXT)")
    for index, (_name, normalized, oracle) in enumerate(_CARDS, start=1):
        conn.execute(
            "INSERT INTO cards (id, normalized_name, scryfall_oracle_id)"
            " VALUES (?, ?, '')",
            (index, normalized),
        )
        if oracle:
            conn.execute(
                "INSERT INTO card_oracle_ids (card_id, oracle_id) VALUES (?, ?)",
                (index, oracle),
            )
    conn.commit()
    conn.close()


@pytest.fixture
def resolver(tmp_path, monkeypatch):
    db_path = tmp_path / "research.db"
    bulk_path = tmp_path / "oracle_cards.jsonl.gz"
    _write_db(db_path)
    _write_bulk(bulk_path)
    monkeypatch.setattr(images, "_database_path", lambda: db_path)
    monkeypatch.setattr(images, "_bulk_path", lambda: bulk_path)
    images._reset_cache()
    yield
    images._reset_cache()


def test_single_faced_card_yields_one_url(resolver):
    assert images.image_urls("Test Bear") == [
        f"{images.SCRYFALL_IMAGE_BASE}/normal/front/a/a/{BEAR_ID}.jpg"
    ]
    assert images.image_url("Test Bear") == (
        f"{images.SCRYFALL_IMAGE_BASE}/normal/front/a/a/{BEAR_ID}.jpg"
    )


def test_double_faced_card_yields_front_and_back(resolver):
    assert images.image_urls("Test Delver") == [
        f"{images.SCRYFALL_IMAGE_BASE}/normal/front/b/b/{DELVER_ID}.jpg",
        f"{images.SCRYFALL_IMAGE_BASE}/normal/back/b/b/{DELVER_ID}.jpg",
    ]


def test_adventure_card_is_a_single_image(resolver):
    # Two faces on the card, but only one actual image.
    assert images.image_urls("Adventure Card") == [
        f"{images.SCRYFALL_IMAGE_BASE}/normal/front/c/c/{ADVENTURE_ID}.jpg"
    ]


def test_every_size_is_supported(resolver):
    for size in images.SIZES:
        url = images.image_url("Test Bear", size=size)
        assert url is not None
        assert f"/{size}/" in url


def test_unknown_card_returns_none(resolver):
    assert images.image_url("nonsense card xyz") is None
    assert images.image_urls("nonsense card xyz") == []


def test_missing_scryfall_id_returns_none(resolver):
    assert images.image_url("No Art Card") is None
    assert images.image_urls("No Art Card") == []


def test_misaligned_oracle_id_falls_back_to_name(resolver):
    # The oracle id maps to "Some Other Card"; the name guard rejects it and
    # there is no name match, so the result is None rather than wrong art.
    assert images.image_url("Misaligned Card") is None


def test_art_series_layout_is_skipped(resolver):
    assert images.image_url("Art Series Card") is None


def test_face_out_of_range_returns_none(resolver):
    assert images.image_url("Test Bear", face=1) is None
    assert images.image_url("Test Bear", face=5) is None
    assert images.image_url("Test Delver", face=5) is None


def test_unknown_size_raises(resolver):
    with pytest.raises(ValueError):
        images.image_url("Test Bear", size="huge")


def test_name_matching_is_normalized(resolver):
    assert images.image_url("  TEST   bear ") == images.image_url("Test Bear")
