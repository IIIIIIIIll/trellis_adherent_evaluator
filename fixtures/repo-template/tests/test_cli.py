"""Tests for notes.cli."""

from __future__ import annotations

import json

from notes.cli import main


def test_add_then_list(tmp_path, capsys):
    store = tmp_path / "notes.json"

    assert main(["--store", str(store), "add", "First note", "--body", "hello"]) == 0
    assert main(["--store", str(store), "list"]) == 0

    out = capsys.readouterr().out
    assert "added " in out
    assert "First note" in out
    assert "hello" in out


def test_add_writes_store_file(tmp_path):
    store = tmp_path / "notes.json"

    main(["--store", str(store), "add", "Written"])

    entries = json.loads(store.read_text(encoding="utf-8"))
    assert [entry["title"] for entry in entries] == ["Written"]


def test_list_shows_body_lines(tmp_path, capsys):
    store = tmp_path / "notes.json"

    main(["--store", str(store), "add", "With body", "--body", "line one"])
    main(["--store", str(store), "list"])

    out = capsys.readouterr().out
    assert "line one" in out


def test_search_prints_only_matches(tmp_path, capsys):
    store = tmp_path / "notes.json"

    main(["--store", str(store), "add", "Buy oat milk", "--body", "groceries"])
    main(["--store", str(store), "add", "Standup notes"])
    main(["--store", str(store), "add", "Milk the topic", "--body", "meeting prep"])

    assert main(["--store", str(store), "search", "oat"]) == 0

    out = capsys.readouterr().out
    assert "Buy oat milk" in out
    assert "Standup notes" not in out
    assert "Milk the topic" not in out


def test_search_no_match_prints_no_notes(tmp_path, capsys):
    store = tmp_path / "notes.json"

    main(["--store", str(store), "add", "Groceries"])
    assert main(["--store", str(store), "search", "zebra"]) == 0

    out = capsys.readouterr().out
    assert "Groceries" not in out


def test_list_is_sorted_by_timestamp(tmp_path, capsys):
    store = tmp_path / "notes.json"
    store.write_text(
        json.dumps(
            [
                {
                    "id": "111111111111",
                    "title": "Older",
                    "body": "",
                    "created_at": "2026-08-01T08:00:00",
                },
                {
                    "id": "222222222222",
                    "title": "Newer",
                    "body": "",
                    "created_at": "2026-08-02T08:00:00",
                },
            ]
        ),
        encoding="utf-8",
    )

    assert main(["--store", str(store), "list"]) == 0

    out = capsys.readouterr().out
    assert out.index("Older") < out.index("Newer")
