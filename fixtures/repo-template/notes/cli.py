"""Command-line interface for notes."""

from __future__ import annotations

import argparse
import sys

from notes import storage
from notes.models import Note


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="notes", description="Tiny notes CLI")
    parser.add_argument(
        "--store",
        default=str(storage.DEFAULT_STORE),
        help="path to the JSON store (default: %(default)s)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="add a note")
    p_add.add_argument("title")
    p_add.add_argument("--body", default="")

    sub.add_parser("list", help="list all notes")

    p_search = sub.add_parser("search", help="search notes by title or body")
    p_search.add_argument("query")

    return parser


def _print_note(note: Note) -> None:
    print(f"[{note.created_at}] {note.id}  {note.title}")
    if note.body:
        print(f"    {note.body}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = storage.Store(args.store)

    if args.command == "add":
        note = store.add(args.title, body=args.body)
        print(f"added {note.id}")
    elif args.command == "list":
        for note in store.list_notes():
            _print_note(note)
    elif args.command == "search":
        for note in store.search(args.query):
            _print_note(note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
