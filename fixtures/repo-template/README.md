# notse

A tiny command-line app for keeping notes on disk.

## Usage

```bash
python -m notes.cli add "Groceries" --body "milk, eggs"
python -m notes.cli list
python -m notes.cli search "groceries"
```

Entries live in `data/notes.json` and are created on the first `add`.

## Development

```bash
pip install -e .[dev]
pytest
```
