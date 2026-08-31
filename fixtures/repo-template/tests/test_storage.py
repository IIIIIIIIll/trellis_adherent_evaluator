"""Tests for notes.storage."""

from __future__ import annotations

import json
import re

import pytest

from notes.storage import Store

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


def test_add_and_list_roundtrip(tmp_path):
    store = Store(tmp_path / "notes.json")
    store.add("Groceries", body="milk and eggs")
    store.add("Ideas", body="write the evaluator")

    notes = store.list_notes()
    assert [note.title for note in notes] == ["Groceries", "Ideas"]
    assert notes[0].body == "milk and eggs"


def test_add_persists_to_disk(tmp_path):
    path = tmp_path / "notes.json"
    Store(path).add("Persisted")

    entries = json.loads(path.read_text(encoding="utf-8"))
    assert [entry["title"] for entry in entries] == ["Persisted"]


def test_new_notes_get_ids_and_timestamps(tmp_path):
    store = Store(tmp_path / "notes.json")
    first = store.add("Stamped")
    second = store.add("Also stamped")

    assert first.id and second.id and first.id != second.id
    assert TIMESTAMP_RE.match(first.created_at)
    assert TIMESTAMP_RE.match(second.created_at)


def test_search_matches_title_and_body(tmp_path):
    store = Store(tmp_path / "notes.json")
    store.add("Coffee order", body="oat milk latte")
    store.add("Team offsite", body="plan the coffee tasting")
    store.add("Dentist", body="cleaning")

    found = store.search("coffee")
    assert {note.title for note in found} == {"Coffee order", "Team offsite"}


def test_search_is_case_insensitive(tmp_path):
    store = Store(tmp_path / "notes.json")
    store.add("Latin practice")

    assert [note.title for note in store.search("LATIN")] == ["Latin practice"]


def test_search_miss_returns_empty(tmp_path):
    store = Store(tmp_path / "notes.json")
    store.add("Groceries")

    assert store.search("dentist") == []


def test_edit_returns_updated_fields(tmp_path):
    store = Store(tmp_path / "notes.json")
    note = store.add("Draft", body="v1")

    updated = store.edit(note.id, body="v2")
    assert updated.title == "Draft"
    assert updated.body == "v2"
    assert updated.created_at == note.created_at


def test_edit_missing_id_raises(tmp_path):
    store = Store(tmp_path / "notes.json")

    with pytest.raises(KeyError):
        store.edit("no-such-id", title="x")


def test_empty_store_lists_nothing(tmp_path):
    store = Store(tmp_path / "missing.json")

    assert store.list_notes() == []
    assert store.search("anything") == []


def test_list_reads_seeded_format(tmp_path):
    path = tmp_path / "notes.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "b1b2c3d4e5f6",
                    "title": "Imported",
                    "body": "written by another tool",
                    "created_at": "2026-08-20T10:00:00",
                }
            ]
        ),
        encoding="utf-8",
    )

    notes = Store(path).list_notes()
    assert [note.title for note in notes] == ["Imported"]
    assert notes[0].id == "b1b2c3d4e5f6"
