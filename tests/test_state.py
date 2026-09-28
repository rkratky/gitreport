import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gitreport.state import (
    AttentionState,
    ItemState,
    load_state,
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


def test_corrupt_state_backed_up_and_rebuilt(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    state = load_state(path)
    assert state.items == {}
    assert len(list(tmp_path.glob("state.json.corrupt-*"))) == 1


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


def test_state_lock_file_created(tmp_path: Path):
    path = tmp_path / "state.json"
    with state_lock(path):
        assert Path(str(path) + ".lock").exists()
