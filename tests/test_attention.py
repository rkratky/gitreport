from datetime import datetime

import gitreport.attention as attention
from gitreport.attention import (
    build_report,
    dedupe,
    merge_into_state,
    render_attention_body,
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


def merged_with(*items):
    """Wrap provider items into the merged_items mapping merge_into_state expects."""
    return {
        it["id"]: dedupe(
            {"github": {"ok": True, "error": None, "resolved_ids": [], "items": [it]}}
        )[it["id"]]
        for it in items
    }


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
    assert r["last_updated"] == LATER  # pinned: display freshness still advances


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
    # gh:new is auto-resolved that run and kept because resolved_at is fresh:
    # the resolution pass stamps resolved_at = generated_at before pruning.
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
    # Fixed clock so the test cannot time-bomb around midnight or CI drift.
    now = datetime.fromisoformat("2026-09-28T12:00:00+00:00")
    state = state_with(
        # After the items' first_seen (2026-09-20) so they count as still-open
        # and exercise bucketing rather than the New bucket.
        last_reviewed="2026-09-28T00:00:00+00:00",
        items={
            "gh:t": rec(first_seen="2026-09-20T00:00:00+00:00", last_updated=now.isoformat()),
            "gh:w": rec(
                first_seen="2026-09-20T00:00:00+00:00", last_updated="2026-09-25T00:00:00+00:00"
            ),
            "gh:o": rec(
                first_seen="2026-09-20T00:00:00+00:00", last_updated="2026-08-01T00:00:00+00:00"
            ),
        },
    )
    report = build_report(state, now)
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


def test_render_digest_markdown_bad_generated_at_no_raise():
    # R2-01 regression: unparseable generated_at used to make build_report's
    # clock fall back to datetime.min, which overflows when localised to a
    # negative-offset timezone; the fallback must be a live "now" instead.
    state = state_with(
        last_reviewed="2026-09-29T00:00:00+00:00",
        items={"gh:1": rec(first_seen=NOW, last_updated=NOW)},
    )
    md = render_digest_markdown(state, "x", "bad", "bad", [])
    assert "## Needs attention" in md
    assert "Still open" in md
    assert "- [T](https://x/1)" in md


def test_render_attention_stdout_is_dry_view():
    out = render_attention_stdout(
        state_with(), stale_providers=["launchpad"], now=datetime.fromisoformat(NOW)
    )
    assert "# Needs attention" in out
    assert "launchpad" in out
    assert "Status:" not in out


def test_z_suffix_same_instant_no_false_reopen():
    # "…Z" and "…+00:00" are the same instant; string comparison would see
    # 'Z' > '+' and falsely reopen. Timestamps must be parsed, not compared.
    state = state_with(
        items={
            "gh:1": rec(
                status="acked",
                acked=True,
                last_updated="2026-09-29T10:00:00+00:00",
            )
        }
    )
    new_state = merge_into_state(
        state,
        merged_with(item(mid="gh:1", updated_at="2026-09-29T10:00:00Z")),
        {"github"},
        "2026-09-29T11:00:00+00:00",
    )
    r = new_state["items"]["gh:1"]
    assert r["status"] == "acked"
    assert r["reopen_count"] == 0


def test_resolved_rereported_then_absent_regains_resolved_at():
    # BUG-01 regression: resolved -> re-reported (same updated_at) resets
    # resolved_at to None; a later absent run must re-stamp it, else the
    # record is retained forever.
    state = state_with(
        items={"gh:1": rec(status="resolved", resolved_at="2026-09-01T07:00:00+00:00")}
    )
    suppressed = merge_into_state(state, merged_with(item(mid="gh:1")), {"github"}, NOW)
    assert suppressed["items"]["gh:1"]["resolved_at"] is None
    resolved_again = merge_into_state(suppressed, {}, {"github"}, LATER)
    assert resolved_again["items"]["gh:1"]["status"] == "resolved"
    assert resolved_again["items"]["gh:1"]["resolved_at"] == LATER
    pruned = merge_into_state(resolved_again, {}, {"github"}, "2026-10-30T10:00:00+00:00")
    assert "gh:1" not in pruned["items"]


def test_acked_absent_keeps_status_and_gains_resolved_at():
    state = state_with(
        items={"gh:1": rec(status="acked", acked=True, acked_at="2026-09-28T06:30:00+00:00")}
    )
    new_state = merge_into_state(state, {}, {"github"}, "2026-09-28T07:00:00+00:00")
    r = new_state["items"]["gh:1"]
    assert r["status"] == "acked"
    assert r["resolved_at"] == "2026-09-28T07:00:00+00:00"


def test_re_report_resets_resolved_at():
    state = state_with(
        items={"gh:1": rec(status="resolved", resolved_at="2026-09-01T00:00:00+00:00")}
    )
    new_state = merge_into_state(state, merged_with(item(mid="gh:1")), {"github"}, NOW)
    assert new_state["items"]["gh:1"]["resolved_at"] is None


def test_pinned_never_auto_resolves():
    state = state_with(items={"gh:1": rec(pinned=True)})
    new_state = merge_into_state(state, {}, {"github"}, "2026-09-28T07:00:00+00:00")
    r = new_state["items"]["gh:1"]
    assert r["status"] == "open"
    assert r["resolved_at"] is None


def test_resolved_still_reported_with_no_resolved_at_not_pruned():
    # A resolved record whose resolution was suppressed (resolved_at None)
    # and that is still reported must survive the prune pass.
    state = state_with(items={"gh:1": rec(status="resolved", resolved_at=None)})
    new_state = merge_into_state(state, merged_with(item(mid="gh:1")), {"github"}, NOW)
    assert "gh:1" in new_state["items"]
    assert new_state["items"]["gh:1"]["resolved_at"] is None


def test_exactly_30_days_boundary_prunes():
    gen = "2026-09-28T07:00:00+00:00"
    state = state_with(
        items={"gh:1": rec(status="resolved", resolved_at="2026-08-29T07:00:00+00:00")}
    )
    new_state = merge_into_state(state, {}, {"github"}, gen)
    assert "gh:1" not in new_state["items"]


def test_coverage_line_exact_format(monkeypatch):
    monkeypatch.setattr(attention, "_fmt_local", lambda ts: "PINNED")
    md = render_digest_markdown(
        state_with(), "# Git activity report", "2026-09-27T06:00:00+00:00", NOW, stale_providers=[]
    )
    assert "Coverage: PINNED – PINNED (1 day since last review)" in md
    md2 = render_digest_markdown(
        state_with(), "# Git activity report", "2026-09-26T06:00:00+00:00", NOW, stale_providers=[]
    )
    assert "Coverage: PINNED – PINNED (2 days since last review)" in md2


def test_parse_garbage_returns_none():
    assert attention._parse("not-a-timestamp") is None
    assert attention._parse("") is None


def test_is_later_never_true_on_unparseable():
    assert attention._is_later("garbage", "2026-09-28T06:00:00+00:00") is False
    assert attention._is_later("2026-09-28T06:00:00+00:00", "garbage") is True
    assert attention._is_later("garbage", "also garbage") is False


def test_merge_and_report_survive_garbage_last_updated():
    state = state_with(
        last_reviewed=NOW,
        items={
            "gh:bad": rec(last_updated="GARBAGE-TS"),
        },
    )
    # ok_providers empty and item absent from fetch: no auto-resolve, record
    # survives the merge with its garbage last_updated intact.
    new_state = merge_into_state(state, {}, set(), NOW)
    assert "gh:bad" in new_state["items"]
    report = build_report(new_state, datetime.fromisoformat(NOW))
    # unparseable last_updated buckets as "older"; record survives
    assert [e["id"] for e in report["older"]] == ["gh:bad"]
    # R2-02: unparseable last_reviewed counts as never-reviewed (first-run
    # semantics) — the open item is New.
    assert (
        build_report({**new_state, "last_reviewed": "GARBAGE-TS"}, datetime.fromisoformat(NOW))[
            "new"
        ][0]["id"]
        == "gh:bad"
    )


def test_unparseable_first_seen_not_new():
    state = state_with(
        last_reviewed="2026-09-27T06:00:00+00:00",
        items={"gh:1": rec(first_seen="GARBAGE-TS")},
    )
    report = build_report(state, datetime.fromisoformat(NOW))
    assert not report["new"]
    assert [e["id"] for e in report["older"] + report["week"] + report["today"]] == ["gh:1"]


def test_unparseable_resolved_at_not_pruned():
    state = state_with(items={"gh:1": rec(status="resolved", resolved_at="GARBAGE-TS")})
    new_state = merge_into_state(state, {}, {"github"}, "2027-10-01T00:00:00+00:00")
    assert "gh:1" in new_state["items"]


def test_new_bucket_sorted_newest_first():
    state = state_with(
        last_reviewed="2026-09-27T06:00:00+00:00",
        items={
            "gh:early": rec(first_seen="2026-09-28T06:00:00+00:00", last_updated=NOW),
            "gh:late": rec(first_seen="2026-09-28T07:00:00+00:00", last_updated=LATER),
        },
    )
    report = build_report(state, datetime.fromisoformat(NOW))
    assert [e["id"] for e in report["new"]] == ["gh:late", "gh:early"]


def _report_with(entry):
    return {"new": [entry], "today": [], "week": [], "older": []}


def test_url_guard_javascript_scheme_no_link():
    entry = {"id": "gh:1", "title": "Evil", "url": "javascript:alert(1)", "reasons": []}
    out = render_attention_body(_report_with(entry), [])
    assert "- Evil" in out
    assert "javascript:" not in out


def test_url_guard_parens_no_link():
    entry = {
        "id": "gh:1",
        "title": "Odd",
        "url": "https://example.com/a(b) [x]",
        "reasons": [],
    }
    out = render_attention_body(_report_with(entry), [])
    assert "- Odd" in out
    assert "](https://example.com" not in out


def test_url_guard_unbalanced_paren_no_link():
    entry = {
        "id": "gh:1",
        "title": "Odd",
        "url": "https://example.com/a)b",
        "reasons": [],
    }
    out = render_attention_body(_report_with(entry), [])
    assert "- Odd" in out
    assert "](https://example.com" not in out


def test_titles_not_re_escaped():
    entry = {
        "id": "gh:1",
        "title": "Fix &amp; ship &lt;3",
        "url": "https://example.com/1",
        "reasons": [],
    }
    out = render_attention_body(_report_with(entry), [])
    assert "Fix &amp; ship &lt;3" in out
    assert "&amp;amp;" not in out
