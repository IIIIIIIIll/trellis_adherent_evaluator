"""JSON-file storage for notes."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from notes.models import Note

DEFAULT_STORE = Path("data") / "notes.json"


class Store:
    def __init__(self, path: str | Path = DEFAULT_STORE) -> None:
        self.path = Path(path)

    def _read_entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write_entries(self, entries: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _entry_to_note(entry: dict) -> Note:
        return Note(
            id=entry["id"],
            title=entry["title"],
            body=entry.get("body", ""),
            created_at=entry["created_at"],
        )

    def _iter_notes(self):
        for entry in self._read_entries():
            yield self._entry_to_note(entry)

    def list_notes(self) -> list[Note]:
        by_id = {note.id: note for note in self._iter_notes()}
        return sorted(by_id.values(), key=lambda note: note.created_at)

    def search(self, query: str) -> list[Note]:
        needle = query.lower()
        return [
            note
            for note in self._iter_notes()
            if needle in note.title.lower() or needle in note.body.lower()
        ]

    def add(self, title: str, body: str = "") -> Note:
        note = Note(title=title, body=body)
        entries = self._read_entries()
        entries.append(asdict(note))
        self._write_entries(entries)
        return note

    def edit(
        self,
        note_id: str,
        title: str | None = None,
        body: str | None = None,
    ) -> Note:
        entries = self._read_entries()
        current = next((e for e in entries if e["id"] == note_id), None)
        if current is None:
            raise KeyError(f"no note with id {note_id!r}")
        updated = Note(
            id=note_id,
            title=title if title is not None else current["title"],
            body=body if body is not None else current.get("body", ""),
            created_at=current["created_at"],
        )
        entries.append(asdict(updated))
        self._write_entries(entries)
        return updated
