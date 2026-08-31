"""Data model for stored notes."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


def utc_now() -> str:
    """Timestamp recorded for new notes."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class Note:
    title: str
    id: str = field(default_factory=new_id)
    body: str = ""
    created_at: str = field(default_factory=utc_now)
