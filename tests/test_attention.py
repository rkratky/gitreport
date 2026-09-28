from datetime import UTC, datetime

from gitreport.attention import (
    build_report,
    dedupe,
    merge_into_state,
    render_attention_stdout,
    render_digest_markdown,
)

NOW = "2026-09-28T06:00:00+00:00"
LATER = "2026-09-29T10:00:00+00:00"


def item(
    mid="gh:https://github.com/o/r/pull/1",
    origin="notification",
    kind="mention",
    reason="mentioned you",
    updated_at=NOW,
    provider="github",
    **extra,
):
    base = {
        "id": mid,
        "provider": provider,
        "kind": kind,
        "origin": origin,
        "repo": "o/r",
        "title": "T",
        "url": "https://github.com/o/r/pull/1",
        "reason": reason,
        "updated_at": updated_at,
    }
    base.update(extra)
    return base


def rec(**overrides):
    """A plain-dict state item record."""
    base = {
        "status": "open",
        "origins": ["notification"],
        "kinds": ["mention"],
        "reasons": [],
        "repo": "o/r",
        "title": "T",
        "url": "https://x/1",
        "first_seen": NOW,
        "last_updated": NOW,
        "acked": False,
        "acked_at": None,
        "resolved_at": None,
        "pinned": False,
        "reopen_count": 0,
        "thread_url": None,
        "provider": "github",
    }
    base.update(overrides)
    return base


def state_with(**kwargs):
    base = {
        "version": 1,
        "last_digest_run": None,
        "last_reviewed": None,
        "reviewed_at": None,
        "items": {},
    }
    base.update(kwargs)
    return base


def test_dedupe_unions_origins_kinds_reasons():
    results = {
        "github": {
            "ok": True,
            "error": None,
            "resolved_ids": [],
            "items": [
                item(origin="notification", kind="comment", reason="new comment"),
                item(origin="query", kind="thread_unresolved", reason="unresolved thread"),
            ],
        }
    }
    merged = dedupe(results)
    m = merged["gh:https://github.com/o/r/pull/1"]
    assert sorted(m["origins"]) == ["notification", "query"]
    assert sorted(m["kinds"]) == ["comment", "thread_unresolved"]
    assert set(m["reasons"]) == {"new comment", "unresolved thread"}


def test_merge_creates_open_item_with_first_seen():
    merged = {
        "gh:1": {
            **dedupe(
                {
                    "github": {
                        "ok": True,
                        "error": None,
                        "resolved_ids": [],
                        "items": [item(mid="gh:1")],
                    }
                }
            )["gh:1"]
        }
    }
    new_state = merge_into_state(state_with(), merged, {"github"}, NOW)
    r = new_state["items"]["gh:1"]
    assert r["status"] == "open"
    assert r["first_seen"] == NOW
    assert r["last_updated"] == NOW


def test_reopen_rule_someone_else_event():
    state = state_with(
        items={
            "gh:1": rec(
                status="acked",
                acked=True,
                acked_at="2026-09-28T07:00:00+00:00",
                first_seen="2026-09-28T06:00:00+00:00",
                last_updated="2026-09-28T06:00:00+00:00",
            )
        }
    )
    merged = {
        "gh:1": dedupe(
            {
                "github": {
                    "ok": True,
                    "error": None,
                    "resolved_ids": [],
                    "items": [item(mid="gh:1", updated_at=LATER, reason="new comment")],
                }
            }
        )["gh:1"]
    }
    new_state = merge_into_state(state, merged, {"github"}, LATER)
    r = new_state["items"]["gh:1"]
    assert r["status"] == "open"
    assert r["acked"] is False
    assert r["reopen_count"] == 1
    assert r["first_seen"] == "2026-09-28T06:00:00+00:00"  # unchanged: not New again


def test_pinned_item_does_not_reopen():
    state = state_with(
        items={
            "gh:1": rec(
                pinned=True,
                reopen_count=2,
                first_seen="2026-09-01T06:00:00+00:00",
                last_updated="2026-09-01T06:00:00+00:00",
            )
        }
    )
    merged = {
        "gh:1": dedupe(
            {
                "github": {
                    "ok": True,
                    "error": None,
                    "resolved_ids": [],
                    "items": [item(mid="gh:1", updated_at=LATER)],
                }
            }
        )["gh:1"]
    }
    new_state = merge_into_state(state, merged, {"github"}, LATER)
    r = new_state["items"]["gh:1"]
    assert r["status"] == "open"
    assert r["reopen_count"] == 2  # pinned: no further reopen increments


def test_resolution_requires_every_origin():
    state = state_with(items={"gh:1": rec(origins=["notification", "query"])})
    merged = {
        "gh:1": dedupe(
            {
                "github": {
                    "ok": True,
                    "error": None,
                    "resolved_ids": [],
                    "items": [item(mid="gh:1", origin="query")],
                }
            }
        )["gh:1"]
    }
    new_state = merge_into_state(state, merged, {"github"}, NOW)
    assert new_state["items"]["gh:1"]["status"] == "open"  # query still reports it


def test_resolution_when_all_origins_cleared():
    state = state_with(items={"gh:1": rec()})
    new_state = merge_into_state(state, {}, {"github"}, "2026-09-28T07:00:00+00:00")
    r = new_state["items"]["gh:1"]
    assert r["status"] == "resolved"
    assert r["resolved_at"] == "2026-09-28T07:00:00+00:00"


def test_no_resolution_when_provider_fetch_failed():
    state = state_with(items={"gh:1": rec()})
    new_state = merge_into_state(state, {}, set(), "2026-09-28T07:00:00+00:00")
    assert new_state["items"]["gh:1"]["status"] == "open"  # carried over


def test_resolved_id_override():
    state = state_with(items={"lp:1": rec(provider="launchpad")})
    new_state = merge_into_state(
        state, {}, {"launchpad"}, "2026-09-28T07:00:00+00:00", resolved_ids={"lp:1"}
    )
    r = new_state["items"]["lp:1"]
    assert r["status"] == "resolved"


def test_retention_prune():
    old_resolved = "2026-08-01T00:00:00+00:00"
    state = state_with(
        items={
            "gh:old": rec(status="resolved", resolved_at=old_resolved),
            "gh:new": rec(resolved_at="2026-09-27T00:00:00+00:00"),
        }
    )
    # gh:new is open with resolved_at set only for the prune-status guard;
    # open items are never pruned regardless of resolved_at.
    new_state = merge_into_state(state, {}, {"github"}, "2026-09-28T07:00:00+00:00")
    assert "gh:old" not in new_state["items"]
    assert "gh:new" in new_state["items"]


def test_build_report_new_vs_still_open_disjoint():
    state = state_with(
        last_reviewed="2026-09-27T06:00:00+00:00",
        items={
            "gh:1": rec(
                title="New one",
                first_seen="2026-09-28T06:00:00+00:00",
                last_updated="2026-09-28T06:00:00+00:00",
            ),
            "gh:2": rec(
                title="Old",
                first_seen="2026-09-20T06:00:00+00:00",
                last_updated="2026-09-20T06:00:00+00:00",
            ),
        },
    )
    report = build_report(state, datetime.fromisoformat(NOW))
    assert [e["id"] for e in report["new"]] == ["gh:1"]
    still = [e["id"] for b in ("today", "week", "older") for e in report[b] if e["id"] == "gh:2"]
    assert still == ["gh:2"]
    # Disjoint: gh:1 never appears in a bucket.
    assert not [e for b in ("today", "week", "older") for e in report[b] if e["id"] == "gh:1"]


def test_build_report_bucketing():
    state = state_with(
        # After the items' first_seen (2026-09-20) so they count as still-open
        # and exercise bucketing rather than the New bucket.
        last_reviewed="2026-09-28T00:00:00+00:00",
        items={
            "gh:t": rec(
                first_seen="2026-09-20T00:00:00+00:00", last_updated=datetime.now(UTC).isoformat()
            ),
            "gh:w": rec(
                first_seen="2026-09-20T00:00:00+00:00", last_updated="2026-09-25T00:00:00+00:00"
            ),
            "gh:o": rec(
                first_seen="2026-09-20T00:00:00+00:00", last_updated="2026-08-01T00:00:00+00:00"
            ),
        },
    )
    report = build_report(state, datetime.now(UTC))
    ids_today = [e["id"] for e in report["today"]]
    assert "gh:t" in ids_today
    assert [e["id"] for e in report["week"]] == ["gh:w"]
    assert [e["id"] for e in report["older"]] == ["gh:o"]


def test_render_digest_markdown_shape():
    state = state_with(last_reviewed="2026-09-27T06:00:00+00:00", items={})
    md = render_digest_markdown(state, "# Git activity report", NOW, NOW, stale_providers=[])
    assert md.startswith("---\n")
    assert f"generated_at: {NOW}" in md
    assert "Status: NOT YET REVIEWED" in md
    assert "Coverage: " in md
    assert "## Recent activity" in md
    assert "# Git activity report" in md


def test_render_attention_stdout_is_dry_view():
    out = render_attention_stdout(
        state_with(), stale_providers=["launchpad"], now=datetime.fromisoformat(NOW)
    )
    assert "# Needs attention" in out
    assert "launchpad" in out
    assert "Status:" not in out
