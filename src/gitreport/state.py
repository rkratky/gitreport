"""Attention inbox state store: JSON file, atomic writes, exclusive flock."""

import fcntl
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

RETENTION_DAYS = 30


class ItemState(BaseModel):
    """State of one attention item, keyed by its stable id."""

    status: str = "open"  # open | acked | resolved
    origins: list[str] = Field(default_factory=list)
    kinds: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    repo: str = ""
    title: str = ""
    url: str = ""
    first_seen: str = ""
    last_updated: str = ""
    acked: bool = False
    acked_at: str | None = None
    resolved_at: str | None = None
    pinned: bool = False
    reopen_count: int = 0


class AttentionState(BaseModel):
    version: int = 1
    last_digest_run: str | None = None
    last_reviewed: str | None = None
    reviewed_at: str | None = None
    items: dict[str, ItemState] = Field(default_factory=dict)


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


class state_lock:
    """Exclusive flock on `<state>.lock` for a whole load-modify-write cycle."""

    def __init__(self, path: Path):
        self._lock_path = Path(str(path) + ".lock")
        self._fd: int | None = None

    def __enter__(self) -> "state_lock":
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def load_state(path: Path) -> AttentionState:
    """Load the inbox state; missing file -> empty, corrupt -> rebuild."""
    if not path.exists():
        return AttentionState()
    try:
        data = json.loads(path.read_text())
        return AttentionState.model_validate(data)
    except (json.JSONDecodeError, ValueError) as e:
        backup = Path(f"{path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
        path.rename(backup)
        print(
            f"WARNING: state file {path} was corrupt ({e}); backed up to "
            f"{backup} and rebuilt empty. Ack/read history is lost.",
            file=sys.stderr,
        )
        return AttentionState()


def save_state(path: Path, state: AttentionState) -> None:
    """Atomically write the state (tmp file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state.model_dump(), indent=2))
    os.replace(tmp, path)


def record_read(state: AttentionState, generation_ts: str, reviewed_at: str) -> None:
    """Advance the consumption cursor to a digest's generation timestamp.

    `generation_ts` is that digest's `last_digest_run`/front-matter
    `generated_at` — NOT the wall-clock time of the read, so nothing that
    happened between generation and the read is lost from future coverage.
    """
    state.last_reviewed = generation_ts
    state.reviewed_at = reviewed_at


def prune(state: AttentionState, now: datetime, retention_days: int = RETENTION_DAYS) -> list[str]:
    """Prune acked/resolved items whose source stopped reporting >30d ago.

    An item with `resolved_at is None` is still reported by its source and is
    kept as a suppression record: it must never re-enter the inbox as New.
    """
    pruned: list[str] = []
    for item_id in list(state.items):
        item = state.items[item_id]
        if item.status not in ("acked", "resolved") or item.resolved_at is None:
            continue
        resolved = datetime.fromisoformat(item.resolved_at)
        if resolved.tzinfo is None:
            resolved = resolved.replace(tzinfo=UTC)
        if (now - resolved).days >= retention_days:
            del state.items[item_id]
            pruned.append(item_id)
    return pruned
