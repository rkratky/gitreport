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
    # Merge-layer extras: attention.merge_into_state emits both keys in its
    # dict output; declaring them keeps the round-trip (older state files
    # without these keys load fine — both default to None).
    thread_url: str | None = None
    provider: str | None = None


class AttentionState(BaseModel):
    version: int = 1
    last_digest_run: str | None = None
    last_reviewed: str | None = None
    reviewed_at: str | None = None
    items: dict[str, ItemState] = Field(default_factory=dict)


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def _warn(message: str) -> None:
    """Print a WARNING line to stderr (load/prune degrade instead of crashing)."""
    print(f"WARNING: {message}", file=sys.stderr)


class state_lock:  # noqa: N801 -- lowercase by design: it reads as a context manager
    """Exclusive flock on `<state>.lock` for a whole load-modify-write cycle."""

    def __init__(self, path: Path):
        self._lock_path = Path(str(path) + ".lock")
        self._fd: int | None = None

    def __enter__(self) -> "state_lock":
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except BaseException:
            os.close(fd)  # flock failed: nothing owns the fd yet, do not leak it
            raise
        self._fd = fd
        return self

    def __exit__(self, *exc) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def _corrupt_backup_path(path: Path) -> Path:
    """Unique `<path>.corrupt-<UTC stamp>` path; -1, -2, ... suffix on collision."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    backup = Path(f"{path}.corrupt-{stamp}")
    seq = 0
    while backup.exists():
        seq += 1
        backup = Path(f"{path}.corrupt-{stamp}-{seq}")
    return backup


def _parse_state(path: Path) -> tuple[AttentionState | None, Exception | None]:
    """Parse+validate the state file with no side effects.

    Returns (state, error): (state, None) on success, (None, error) when the
    file is corrupt, (None, None) when it does not exist. Callers decide the
    failure policy: mutate (backup + rebuild) under the lock, or degrade to
    an empty state on a non-mutating read.
    """
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text())
        return AttentionState.model_validate(data), None
    except (json.JSONDecodeError, ValueError) as e:
        return None, e


def _warn_on_version(state: AttentionState, path: Path) -> None:
    if state.version != 1:
        _warn(
            f"state file {path} declares version {state.version}, but this release "
            "writes version 1; it may have been written by another release. "
            "Loading it as-is; no data is destroyed."
        )


def load_state(path: Path) -> AttentionState:
    """Load the inbox state; missing file -> empty, corrupt -> rebuild.

    A file written by another release (version != 1) is loaded as-is with a
    stderr warning: rebuilding would destroy ack/read history, which is worse
    than loading data we can still read.

    Note: the corrupt-file backup rename assumes the caller holds `state_lock`.
    """
    state, err = _parse_state(path)
    if state is None and err is None:
        return AttentionState()
    if state is None:
        backup = _corrupt_backup_path(path)
        os.replace(path, backup)
        _warn(
            f"state file {path} was corrupt ({err}); backed up to "
            f"{backup} and rebuilt empty. Ack/read history is lost."
        )
        return AttentionState()
    _warn_on_version(state, path)
    return state


def load_state_snapshot(path: Path) -> AttentionState:
    """Non-mutating read of the inbox state.

    Same semantics as `load_state` except a corrupt file degrades to an empty
    state WITHOUT the rename/backup (that mutation assumes the caller holds
    `state_lock`). Use for reads outside the lock, e.g. pre-lock snapshots.
    """
    state, _err = _parse_state(path)
    if state is None:
        return AttentionState()  # missing or corrupt: degrade, never touch the file
    _warn_on_version(state, path)
    return state


def save_state(path: Path, state: AttentionState) -> None:
    """Atomically and durably write the state (tmp 0600 + fsync + rename + dir fsync).

    A crash at any point must never leave an empty or truncated state.json.

    Note: may raise after a successful rename if the directory fsync fails; state.json
    is complete, but the rename may not survive a crash.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.unlink(missing_ok=True)  # a stale tmp (or symlink) must not be followed
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            f = os.fdopen(fd, "w")
        except BaseException:
            os.close(fd)  # fdopen failed, so nothing owns the fd yet
            raise
        with f:
            f.write(json.dumps(state.model_dump(), indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)  # never leave a .json.tmp behind
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)  # persist the rename itself across crashes
    finally:
        os.close(dir_fd)


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
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)  # naive `now` is treated as UTC
    pruned: list[str] = []
    for item_id in list(state.items):
        item = state.items[item_id]
        if item.status not in ("acked", "resolved") or item.resolved_at is None:
            continue
        try:
            resolved = datetime.fromisoformat(item.resolved_at)
        except ValueError:
            _warn(
                f"item {item_id} has unparseable resolved_at "
                f"{item.resolved_at!r}; keeping it instead of pruning."
            )
            continue
        if resolved.tzinfo is None:
            resolved = resolved.replace(tzinfo=UTC)
        if (now - resolved).days >= retention_days:
            del state.items[item_id]
            pruned.append(item_id)
    return pruned
