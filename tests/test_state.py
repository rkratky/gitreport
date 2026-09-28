import fcntl
import json
import os
import stat
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gitreport.state import (
    AttentionState,
    ItemState,
    load_state,
    load_state_snapshot,
    prune,
    record_read,
    save_state,
    state_lock,
)


def make_item(item_id: str = "gh:https://x/1", **overrides) -> tuple[str, ItemState]:
    defaults = dict(
        status="open",
        origins=["query"],
        kinds=["mention"],
        reasons=["mentioned you"],
        repo="org/repo",
        title="t",
        url="https://x/1",
        first_seen="2026-09-01T06:00:00+00:00",
        last_updated="2026-09-01T06:00:00+00:00",
    )
    defaults.update(overrides)
    return item_id, ItemState(**defaults)


def test_load_missing_file_gives_empty_state(tmp_path: Path):
    state = load_state(tmp_path / "state.json")
    assert state.items == {}
    assert state.last_reviewed is None
    assert state.version == 1


def test_save_and_load_roundtrip(tmp_path: Path):
    path = tmp_path / "state.json"
    state = AttentionState()
    item_id, item = make_item()
    state.items[item_id] = item
    save_state(path, state)

    loaded = load_state(path)
    assert loaded.items[item_id].status == "open"
    assert loaded.items[item_id].kinds == ["mention"]


def test_item_state_extras_default_none_and_old_files_load(tmp_path: Path):
    """thread_url/provider live on ItemState; files from before they existed
    must load with both defaulting to None (and extras round-trip when set)."""
    _, item = make_item()
    assert item.thread_url is None
    assert item.provider is None

    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 1, "items": {"gh:1": {"status": "open"}}}))

    loaded = load_state(path)

    assert loaded.items["gh:1"].thread_url is None
    assert loaded.items["gh:1"].provider is None

    state = AttentionState()
    state.items["gh:1"] = ItemState(status="open", thread_url="https://t/1", provider="github")
    save_state(path, state)
    reloaded = load_state(path)
    assert reloaded.items["gh:1"].thread_url == "https://t/1"
    assert reloaded.items["gh:1"].provider == "github"


def test_load_state_snapshot_is_non_mutating_on_corrupt(tmp_path: Path):
    """The snapshot read must not rename/backup; load_state still does."""
    path = tmp_path / "state.json"
    path.write_text("{not json")

    snapshot = load_state_snapshot(path)

    assert snapshot.items == {}
    assert snapshot.version == 1
    assert path.read_text() == "{not json"  # untouched
    assert not list(tmp_path.glob("state.json.corrupt-*"))

    # The mutating loader keeps its backup-and-rebuild behaviour.
    rebuilt = load_state(path)
    assert rebuilt.items == {}
    assert len(list(tmp_path.glob("state.json.corrupt-*"))) == 1
    assert not path.exists()  # renamed to the backup


def test_load_state_snapshot_returns_saved_state(tmp_path: Path):
    path = tmp_path / "state.json"
    state = AttentionState()
    item_id, item = make_item()
    state.items[item_id] = item
    save_state(path, state)

    snapshot = load_state_snapshot(path)

    assert snapshot.items[item_id].status == "open"
    assert snapshot.last_reviewed == state.last_reviewed


def test_corrupt_state_backed_up_and_rebuilt(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    state = load_state(path)
    assert state.items == {}
    assert len(list(tmp_path.glob("state.json.corrupt-*"))) == 1


def test_corrupt_load_warns_on_stderr(tmp_path: Path, capsys):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    state = load_state(path)
    assert state.items == {}
    assert state.version == 1
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert str(path) in err
    assert "backed up" in err
    assert "Ack/read history is lost" in err


def test_corrupt_backup_collision_same_second(tmp_path: Path, monkeypatch):
    """Two corrupt loads in the same second must yield two distinct backups (UTC)."""
    frozen = "20260928-120000"
    monkeypatch.setattr(time, "strftime", lambda *args, **kwargs: frozen)
    path = tmp_path / "state.json"
    path.write_text("{one")
    load_state(path)
    assert (tmp_path / f"state.json.corrupt-{frozen}").exists()
    path.write_text("{two")
    load_state(path)
    assert (tmp_path / f"state.json.corrupt-{frozen}-1").exists()
    assert len(list(tmp_path.glob("state.json.corrupt-*"))) == 2


def test_is_state_loadable_parse_check_only(tmp_path: Path):
    """R2 helper: True for missing/valid, False for corrupt; never mutates."""
    from gitreport.state import is_state_loadable

    path = tmp_path / "state.json"
    assert is_state_loadable(path) is True  # missing: nothing to be corrupt
    path.write_text("{not json")
    assert is_state_loadable(path) is False
    assert path.read_text() == "{not json"  # no backup/rename side effect
    path.write_text(json.dumps({"version": 1, "items": {}}))
    assert is_state_loadable(path) is True


def test_unknown_fields_roundtrip_v2_file(tmp_path: Path):
    """R7: unknown top-level and per-item fields (a v2-style file) survive
    load→save→reload instead of being dropped on the next save."""
    path = tmp_path / "state.json"
    payload = {
        "version": 2,
        "future_cursor": "abc",
        "items": {"gh:1": {"status": "open", "future_flag": True}},
    }
    path.write_text(json.dumps(payload))

    state = load_state(path)
    save_state(path, state)
    reloaded = load_state(path)

    saved = json.loads(path.read_text())
    assert saved["future_cursor"] == "abc"
    assert saved["items"]["gh:1"]["future_flag"] is True
    assert reloaded.items["gh:1"].model_extra["future_flag"] is True


def test_record_read_sets_cursor_fields():
    state = AttentionState()
    record_read(state, "2026-09-28T06:00:00+00:00", "2026-09-28T09:14:00+00:00")
    assert state.last_reviewed == "2026-09-28T06:00:00+00:00"
    assert state.reviewed_at == "2026-09-28T09:14:00+00:00"


def test_prune_removes_old_acked_and_resolved_items():
    now = datetime(2026, 9, 28, tzinfo=UTC)
    old = (now - timedelta(days=31)).isoformat()
    recent = (now - timedelta(days=1)).isoformat()
    state = AttentionState()
    state.items["gh:1"] = make_item("gh:1", status="acked", acked=True, resolved_at=old)[1]
    state.items["gh:2"] = make_item("gh:2", status="resolved", resolved_at=old)[1]
    state.items["gh:3"] = make_item("gh:3", status="acked", acked=True, resolved_at=recent)[1]
    state.items["gh:4"] = make_item("gh:4")[1]  # open: never pruned
    state.items["gh:5"] = make_item("gh:5", status="acked", acked=True, resolved_at=None)[1]

    pruned = prune(state, now)

    assert set(pruned) == {"gh:1", "gh:2"}
    assert "gh:3" in state.items  # within retention window
    assert "gh:4" in state.items  # open items are never pruned
    assert "gh:5" in state.items  # still-matching acked item: suppression record


def test_prune_boundary_exactly_30_days_is_pruned():
    now = datetime(2026, 9, 28, tzinfo=UTC)
    exactly_retention = (now - timedelta(days=30)).isoformat()
    state = AttentionState()
    state.items["gh:1"] = make_item(
        "gh:1", status="acked", acked=True, resolved_at=exactly_retention
    )[1]

    pruned = prune(state, now)

    assert pruned == ["gh:1"]
    assert "gh:1" not in state.items


def test_prune_accepts_naive_now_as_utc():
    now = datetime(2026, 9, 28)  # naive: must be treated as UTC, not raise
    old = (now - timedelta(days=31)).isoformat()
    state = AttentionState()
    state.items["gh:1"] = make_item("gh:1", status="acked", acked=True, resolved_at=old)[1]

    assert prune(state, now) == ["gh:1"]


def test_prune_keeps_item_with_malformed_resolved_at(capsys):
    now = datetime(2026, 9, 28, tzinfo=UTC)
    state = AttentionState()
    state.items["gh:1"] = make_item(
        "gh:1", status="acked", acked=True, resolved_at="not-a-timestamp"
    )[1]

    pruned = prune(state, now)

    assert pruned == []
    assert "gh:1" in state.items
    assert "WARNING" in capsys.readouterr().err


def test_version_2_file_loads_with_warning(tmp_path: Path, capsys):
    """A newer-version state file must be loaded as-is, never destroyed."""
    path = tmp_path / "state.json"
    payload = {"version": 2, "items": {"gh:1": {"status": "acked", "acked": True}}}
    path.write_text(json.dumps(payload))

    state = load_state(path)

    assert state.version == 2
    assert "gh:1" in state.items
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "version" in err


def test_saved_state_file_permissions_0600(tmp_path: Path):
    path = tmp_path / "state.json"
    save_state(path, AttentionState())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_stale_tmp_file_mode_is_forced_to_0600(tmp_path: Path):
    """A pre-existing stale tmp file must not leak its old mode into state.json."""
    path = tmp_path / "state.json"
    tmp = tmp_path / "state.json.tmp"
    tmp.write_text("stale")
    tmp.chmod(0o666)

    save_state(path, AttentionState())

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_failure_leaves_no_tmp_file(tmp_path: Path, monkeypatch):
    path = tmp_path / "state.json"

    def explode(*args, **kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(OSError):
        save_state(path, AttentionState())
    assert not (tmp_path / "state.json.tmp").exists()
    assert not path.exists()


def test_state_lock_file_created(tmp_path: Path):
    path = tmp_path / "state.json"
    with state_lock(path):
        assert Path(str(path) + ".lock").exists()


def test_state_lock_is_exclusive(tmp_path: Path):
    path = tmp_path / "state.json"
    lock_path = Path(str(path) + ".lock")
    with state_lock(path):
        # A second, independent fd must not be able to acquire (non-blocking probe).
        second = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with pytest.raises(OSError):
                fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(second)
    # After release, a fresh non-blocking acquire succeeds.
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)
