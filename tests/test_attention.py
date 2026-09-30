import time
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
from gitreport.state import AttentionState, prune

NOW = "2026-09-28T06:00:00+00:00"
LATER = "2026-09-29T10:00:00+00:00"


def item(
    mid="gh:https://github.com/o/r/pull/1",
    origin="notification",
    kind="mention",
    reason="mentioned you",
    updated_at: str | None = NOW,
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


def test_windowed_lp_kinds_not_resolved_by_absence():
    """BUG-03: LP windowed kinds never resolve by absence — only via resolved_ids."""
    state = state_with(
        items={"lp:1": rec(provider="launchpad", kinds=["lp_bug_activity"], origins=["query"])}
    )
    new_state = merge_into_state(state, {}, {"launchpad"}, "2026-09-28T07:00:00+00:00")
    assert new_state["items"]["lp:1"]["status"] == "open"  # absence is not a leave path
    resolved = merge_into_state(
        state, {}, {"launchpad"}, "2026-09-28T07:00:00+00:00", resolved_ids={"lp:1"}
    )
    assert resolved["items"]["lp:1"]["status"] == "resolved"  # proven resolution still works


def test_resolved_id_override():
    state = state_with(items={"lp:1": rec(provider="launchpad")})
    new_state = merge_into_state(
        state, {}, {"launchpad"}, "2026-09-28T07:00:00+00:00", resolved_ids={"lp:1"}
    )
    r = new_state["items"]["lp:1"]
    assert r["status"] == "resolved"


def test_retention_prune_is_not_part_of_the_merge():
    """FINAL-07: retention pruning left merge_into_state — it is `state.prune`'s
    job, called by `digest`. The merge keeps old resolved records; prune drops
    them (retention covered by test_state.py's prune tests)."""
    old_resolved = "2026-08-01T00:00:00+00:00"
    state = state_with(
        items={
            "gh:old": rec(status="resolved", resolved_at=old_resolved),
            "gh:new": rec(resolved_at="2026-09-27T00:00:00+00:00"),
        }
    )
    # gh:new is auto-resolved that run (absence pass) and kept because its
    # resolved_at is fresh.
    new_state = merge_into_state(state, {}, {"github"}, "2026-09-28T07:00:00+00:00")
    assert "gh:old" in new_state["items"]  # merge itself no longer prunes
    assert new_state["items"]["gh:old"]["status"] == "resolved"
    assert "gh:new" in new_state["items"]

    st = AttentionState.model_validate(new_state)
    pruned = prune(st, datetime.fromisoformat("2026-10-01T07:00:00+00:00"))
    assert pruned == ["gh:old"]
    assert "gh:old" not in st.items
    assert "gh:new" in st.items


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
    # Retention prune (FINAL-07: via state.prune, not the merge).
    st = AttentionState.model_validate(resolved_again)
    pruned = prune(st, datetime.fromisoformat("2026-10-30T10:00:00+00:00"))
    assert pruned == ["gh:1"]
    assert "gh:1" not in st.items


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
    # FINAL-03: a re-reported resolved record is open again regardless of
    # event freshness (same updated_at, no new event), resolved_at cleared.
    assert new_state["items"]["gh:1"]["status"] == "open"
    assert new_state["items"]["gh:1"]["resolved_at"] is None
    assert new_state["items"]["gh:1"]["reopen_count"] == 0  # not a new event


def test_resolved_rereported_with_new_event_counts_reopen():
    """R1: a resolved record re-reported with a genuinely new event is a
    reopen — the spec's reopen rule covers acked OR resolved records, so the
    bump must be reachable for just-resolved records too."""
    state = state_with(
        items={
            "gh:1": rec(
                status="resolved",
                resolved_at="2026-09-01T00:00:00+00:00",
                first_seen="2026-09-01T06:00:00+00:00",
                last_updated="2026-09-01T06:00:00+00:00",
            )
        }
    )
    new_state = merge_into_state(
        state, merged_with(item(mid="gh:1", updated_at=LATER)), {"github"}, LATER
    )
    r = new_state["items"]["gh:1"]
    assert r["status"] == "open"
    assert r["reopen_count"] == 1
    assert r["first_seen"] == "2026-09-01T06:00:00+00:00"  # unchanged: not New again


def test_reasons_are_replaced_not_appended():
    """R4: reasons reflect THIS run's fetch — 10 stale reasons plus a fresh
    one must not cap the fresh reason out of the list."""
    state = state_with(items={"gh:1": rec(reasons=[f"stale {i}" for i in range(10)])})
    new_state = merge_into_state(
        state,
        merged_with(item(mid="gh:1", updated_at=LATER, reason="check failure")),
        {"github"},
        LATER,
    )
    assert new_state["items"]["gh:1"]["reasons"] == ["check failure"]


def test_reasons_stale_count_dropped_on_later_fetch():
    """R4: a stale count from an earlier run must not persist once the
    current run reports a different value."""
    state = state_with(items={"gh:1": rec(reasons=["3 unresolved comments"])})
    carried = merge_into_state(
        state, merged_with(item(mid="gh:1", reason="3 unresolved comments")), {"github"}, NOW
    )
    assert carried["items"]["gh:1"]["reasons"] == ["3 unresolved comments"]
    later = merge_into_state(
        carried,
        merged_with(item(mid="gh:1", updated_at=LATER, reason="1 unresolved comment")),
        {"github"},
        LATER,
    )
    assert later["items"]["gh:1"]["reasons"] == ["1 unresolved comment"]


def test_acked_self_activity_bump_stays_acked():
    """FINAL-02: a same-run re-report with updated_at=None (the provider
    suppressed a self-authored bump) carries no new event: an acked record
    must STAY acked."""
    state = state_with(
        items={
            "gh:1": rec(
                status="acked",
                acked=True,
                acked_at="2026-09-28T06:30:00+00:00",
            )
        }
    )
    new_state = merge_into_state(
        state, merged_with(item(mid="gh:1", updated_at=None)), {"github"}, LATER
    )
    r = new_state["items"]["gh:1"]
    assert r["status"] == "acked"
    assert r["acked"] is True
    assert r["acked_at"] == "2026-09-28T06:30:00+00:00"
    assert r["reopen_count"] == 0


def test_pinned_never_auto_resolves():
    state = state_with(items={"gh:1": rec(pinned=True)})
    new_state = merge_into_state(state, {}, {"github"}, "2026-09-28T07:00:00+00:00")
    r = new_state["items"]["gh:1"]
    assert r["status"] == "open"
    assert r["resolved_at"] is None


# The 30-day boundary and malformed-resolved_at retention behaviours moved to
# test_state.py's prune tests (FINAL-07: pruning is state.prune's job); a
# digest-level prune test lives in test_main.py.


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
    return {"new": [entry], "today": [], "week": [], "month": [], "older": []}


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


def test_humanize_age_today_yesterday_pinned_utc(monkeypatch):
    """Today/yesterday are local-calendar-date checks, so the local TZ is
    pinned to UTC (POSIX TZ env var + tzset) to make the result deterministic
    on any machine. Note: time.tzset() is POSIX-only (Linux/macOS); the test
    suite targets POSIX systems."""
    from gitreport.attention import humanize_age

    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    now = datetime.fromisoformat(NOW)  # 2026-09-28T06:00:00+00:00
    assert humanize_age("2026-09-28T05:00:00+00:00", now) == "today"
    assert humanize_age("2026-09-27T10:00:00+00:00", now) == "yesterday"


def test_humanize_age_units():
    """Spec examples. Whole-day diffs at fixed UTC instants, so these are
    TZ-independent."""
    from gitreport.attention import humanize_age

    now = datetime.fromisoformat(NOW)  # 2026-09-28T06:00:00+00:00
    cases = {
        "2026-09-25T06:00:00+00:00": "3 days ago",
        "2026-09-14T06:00:00+00:00": "2 weeks ago",
        "2026-08-01T06:00:00+00:00": "1 month ago",
        "2026-05-01T06:00:00+00:00": "4 months ago",
        "2025-06-01T06:00:00+00:00": "1 year, 3 months ago",
    }
    for ts, expected in cases.items():
        assert humanize_age(ts, now) == expected, ts


def test_humanize_age_unit_boundaries():
    """BUG-01/BUG-02: the largest unit whose floor is >= 1 wins; exact
    multiples land on the larger unit. Whole-day diffs at fixed UTC instants,
    so TZ-independent."""
    from gitreport.attention import humanize_age

    now = datetime.fromisoformat(NOW)  # 2026-09-28T06:00:00+00:00
    cases = {
        "2026-09-21T06:00:00+00:00": "1 week ago",  # 7d: not "7 days ago"
        "2026-09-18T06:00:00+00:00": "1 week ago",  # 10d: floor(10/7)
        "2026-09-07T06:00:00+00:00": "3 weeks ago",  # 21d: not "21 days ago"
        "2026-08-29T06:00:00+00:00": "4 weeks ago",  # 30d < 30.44: not "0 months ago"
        "2026-08-28T06:00:00+00:00": "1 month ago",  # 31d: floor(31/30.44)
        "2025-09-29T06:00:00+00:00": "11 months ago",  # 364d: floor(364/30.44)
        "2024-09-29T06:00:00+00:00": "1 year, 11 months ago",  # 729d
        "2024-09-28T06:00:00+00:00": "2 years ago",  # 730d: zero remainder dropped
    }
    for ts, expected in cases.items():
        assert humanize_age(ts, now) == expected, ts


def test_humanize_age_naive_now_treated_as_utc():
    """BUG-03: a naive `now` is normalised to UTC instead of raising
    TypeError on the aware/naive comparison."""
    from gitreport.attention import humanize_age

    now = datetime.fromisoformat("2026-09-28T06:00:00")  # naive
    assert humanize_age("2026-09-21T06:00:00+00:00", now) == "1 week ago"
    assert humanize_age(None, now) == "unknown age"


def test_humanize_age_edges():
    from gitreport.attention import humanize_age

    now = datetime.fromisoformat(NOW)
    assert humanize_age(None, now) == "unknown age"
    assert humanize_age("garbage", now) == "unknown age"
    assert humanize_age("2026-09-29T06:00:00+00:00", now) == "just now"  # future
    # 1 year exactly -> no zero remainder
    assert humanize_age("2025-09-28T06:00:00+00:00", now) == "1 year ago"


# --- report model (Task 2: badges, buckets, ages, KPIs) ----------------------


def _model_state(items, **extra):
    return AttentionState.model_validate(state_with(items=items, **extra))


def test_badges_for_precedence_and_lp_bug():
    from gitreport.attention import badges_for

    # Precedence regardless of input order (state stores kinds sorted).
    assert badges_for(["stale_pr", "ci_failure"], "github") == [("CI", "ci"), ("stale", "stale")]
    assert badges_for(["comment", "mention", "thread_unresolved"], None) == [
        ("thread", "thread"),
        ("mention", "mention"),
        ("comment", "comment"),
    ]
    # Unknown kind -> ("item", "item"), ordered after all known badges.
    assert badges_for(["ci_failure", "mystery_kind"], "github") == [
        ("CI", "ci"),
        ("item", "item"),
    ]
    assert badges_for(["mystery_kind"], "github") == [("item", "item")]
    # Launchpad override: issue_assigned renders the bug badge.
    assert badges_for(["issue_assigned"], "launchpad") == [("bug", "bug")]
    assert badges_for(["issue_assigned"], "github") == [("issue", "issue")]
    # Duplicate kinds dedupe to one badge; no kinds -> no badges.
    assert badges_for(["mention", "mention"], "github") == [("mention", "mention")]
    assert badges_for([], "github") == []


def test_age_bucket_first_match(monkeypatch):
    """First match wins: same local calendar date -> today (the date check
    runs before any duration check); boundaries are inclusive (exactly
    now-7d -> week, exactly now-30d -> month). TZ pinned to UTC so the
    calendar-date checks are deterministic (time.tzset() is POSIX-only; see
    the humanize_age tests)."""
    from gitreport.attention import age_bucket

    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    now = datetime.fromisoformat(NOW)  # 2026-09-28T06:00:00+00:00
    assert age_bucket("2026-09-28T01:00:00+00:00", now) == "today"
    # 20h old but yesterday's local date: not today — first match continues.
    assert age_bucket("2026-09-27T10:00:00+00:00", now) == "week"
    assert age_bucket("2026-09-21T06:00:00+00:00", now) == "week"  # exactly now-7d: inclusive
    assert age_bucket("2026-09-21T05:59:59+00:00", now) == "month"  # 1s past the boundary
    assert age_bucket("2026-09-20T06:00:00+00:00", now) == "month"  # 8d: month window
    assert age_bucket("2026-08-29T06:00:00+00:00", now) == "month"  # exactly now-30d: inclusive
    assert age_bucket("2026-08-29T05:59:59+00:00", now) == "older"  # 1s past the 30d boundary
    assert age_bucket("2026-08-28T06:00:00+00:00", now) == "older"  # 31d
    assert age_bucket("2026-08-01T06:00:00+00:00", now) == "older"
    # Unparseable / None -> "older" (defensive; sorted last).
    assert age_bucket(None, now) == "older"
    assert age_bucket("GARBAGE-TS", now) == "older"


def test_report_model_groups_and_buckets():
    from gitreport.attention import report_model

    state = _model_state(
        {
            "gh:1": rec(
                title="Fix &amp; ship",
                repo="o/r",
                kinds=["ci_failure", "stale_pr"],
                reasons=["3 unresolved comments"],
                first_seen="2026-09-28T06:00:00+00:00",
                last_updated=NOW,
            ),
            "gh:2": rec(
                title="B",
                repo="o/r",
                kinds=["issue_assigned"],
                first_seen="2026-09-01T06:00:00+00:00",
                last_updated="2026-09-01T06:00:00+00:00",
            ),
            "lp:1": rec(
                title="C",
                repo="proj",
                kinds=["issue_assigned"],
                provider="launchpad",
                url="https://launchpad.net/bugs/1",
                first_seen="2026-08-01T06:00:00+00:00",
                last_updated="2026-08-01T06:00:00+00:00",
            ),
        },
        last_reviewed="2026-09-20T06:00:00+00:00",
    )
    model = report_model(
        state,
        generated_at=NOW,
        coverage_start="2026-09-27T06:00:00+00:00",
        first_run=False,
        stale_providers=[],
    )
    assert model["schema_version"] == 1
    assert model["generated_at"] == NOW
    assert model["coverage"] == {
        "start": "2026-09-27T06:00:00+00:00",
        "end": NOW,
        "days": 1,
        "first_run": False,
    }
    assert model["status"] == {"reviewed": False, "reviewed_at": None}
    assert model["stale_providers"] == []
    # New tier: gh:1 only (first_seen 09-28 > last_reviewed 09-20); gh:2
    # (09-01) and lp:1 (08-01) are Still open. KPI ANY-matches on kinds.
    assert model["kpi"] == {
        "new": 1,
        "still_open": 2,
        "assigned": 2,
        "ci_failing": 1,
        "stale_prs": 1,
    }
    # gh:2's last_updated (09-01) is 27 days before now (09-28): the "month"
    # bucket per the spec's inclusive boundaries — not "week".
    assert [b["key"] for b in model["attention"]["buckets"]] == ["month", "older"]
    buckets = {b["key"]: b for b in model["attention"]["buckets"]}
    assert buckets["month"]["label"] == "Last 30 days"
    assert buckets["older"]["label"] == "Older"
    assert [i["id"] for g in buckets["month"]["groups"] for i in g["items"]] == ["gh:2"]
    # New tier: one repo group, newest-first items, badges in precedence order.
    new_groups = model["attention"]["new"]["groups"]
    assert [g["repo"] for g in new_groups] == ["o/r"]
    gh1 = new_groups[0]["items"][0]
    assert gh1["id"] == "gh:1"
    assert gh1["age_bucket"] == "new"
    assert gh1["type_badges"] == [("CI", "ci"), ("stale", "stale")]
    assert set(gh1) == {
        "id",
        "repo",
        "title",
        "url",
        "reasons",
        "kinds",
        "type_badges",
        "age",
        "age_bucket",
        "last_updated",
        "reopened",
    }
    # Escaping contract: the model carries plain text (unescape_user).
    assert gh1["title"] == "Fix & ship"
    assert gh1["reasons"] == ["3 unresolved comments"]
    assert gh1["reopened"] is False
    gh2 = buckets["month"]["groups"][0]["items"][0]
    assert gh2["type_badges"] == [("issue", "issue")]
    assert gh2["age"] == "3 weeks ago"
    assert gh2["age_bucket"] == "month"
    lp1 = buckets["older"]["groups"][0]["items"][0]
    assert lp1["type_badges"] == [("bug", "bug")]  # LP provider override
    assert lp1["age"] == "1 month ago"
    assert lp1["age_bucket"] == "older"
    # No activity data yet (Task 3): placeholder.
    assert model["activity"] == {"markdown": "", "providers": []}


def test_report_model_sorting_group_and_item_order():
    """Items: last_updated desc, tie id asc, unparseable last. Groups: newest
    item desc, tie repo name asc. All items land in the "older" bucket so the
    unparseable one shares a group with parseable ones."""
    from gitreport.attention import report_model

    state = _model_state(
        {
            # Insertion order is deliberately NOT the expected output order.
            "gh:b": rec(
                first_seen="2026-08-01T00:00:00+00:00",
                last_updated="2026-08-02T00:00:00+00:00",
            ),
            "gh:bad": rec(
                first_seen="2026-08-01T00:00:00+00:00",
                last_updated="GARBAGE-TS",
            ),
            "gh:c": rec(
                first_seen="2026-08-01T00:00:00+00:00",
                last_updated="2026-08-05T00:00:00+00:00",
            ),
            "gh:a": rec(
                first_seen="2026-08-01T00:00:00+00:00",
                last_updated="2026-08-02T00:00:00+00:00",
            ),
            "lp:z": rec(
                repo="proj",
                provider="launchpad",
                first_seen="2026-08-01T00:00:00+00:00",
                last_updated="2026-08-04T00:00:00+00:00",
            ),
            "lp:w": rec(
                repo="aaa",
                provider="launchpad",
                first_seen="2026-08-01T00:00:00+00:00",
                last_updated="2026-08-05T00:00:00+00:00",
            ),
        },
        last_reviewed="2026-09-20T06:00:00+00:00",
    )
    model = report_model(
        state,
        generated_at=NOW,
        coverage_start=NOW,
        first_run=False,
        stale_providers=[],
    )
    assert [b["key"] for b in model["attention"]["buckets"]] == ["older"]
    groups = model["attention"]["buckets"][0]["groups"]
    # Groups by newest item desc; aaa/o/r tie on newest (08-05) -> repo asc.
    assert [g["repo"] for g in groups] == ["aaa", "o/r", "proj"]
    # Items newest-first; gh:a/gh:b tie on last_updated -> id asc; the
    # unparseable gh:bad sorts last despite being first in the state dict.
    assert [i["id"] for i in groups[1]["items"]] == ["gh:c", "gh:a", "gh:b", "gh:bad"]
    assert groups[1]["items"][-1]["age"] == "unknown age"
    assert groups[1]["items"][-1]["age_bucket"] == "older"


def test_report_model_unparseable_last_updated():
    from gitreport.attention import report_model

    state = _model_state(
        {
            "gh:bad": rec(
                title="Bad",
                first_seen="2026-08-01T06:00:00+00:00",
                last_updated="GARBAGE-TS",
            ),
            "gh:ok": rec(
                title="Ok",
                first_seen="2026-08-01T06:00:00+00:00",
                last_updated="2026-08-01T06:00:00+00:00",
            ),
        },
        last_reviewed="2026-09-20T06:00:00+00:00",
    )
    model = report_model(
        state,
        generated_at=NOW,
        coverage_start=NOW,
        first_run=False,
        stale_providers=[],
    )
    older = [b for b in model["attention"]["buckets"] if b["key"] == "older"]
    assert len(older) == 1
    items = [i for g in older[0]["groups"] for i in g["items"]]
    assert [i["id"] for i in items] == ["gh:ok", "gh:bad"]  # unparseable last
    assert items[-1]["age"] == "unknown age"
    assert items[-1]["age_bucket"] == "older"
    # Unparseable metadata must not drop the item from the KPI counts.
    assert model["kpi"]["still_open"] == 2


def test_report_model_first_run():
    from gitreport.attention import report_model

    state = _model_state(
        {
            "gh:1": rec(
                first_seen="2026-08-01T06:00:00+00:00",
                last_updated="2026-08-01T06:00:00+00:00",
            )
        }
    )
    model = report_model(
        state,
        generated_at=NOW,
        coverage_start=NOW,
        first_run=True,
        stale_providers=[],
    )
    assert model["coverage"]["first_run"] is True
    # first_run: every open item is New regardless of age.
    new_groups = model["attention"]["new"]["groups"]
    assert [i["id"] for g in new_groups for i in g["items"]] == ["gh:1"]
    assert new_groups[0]["items"][0]["age_bucket"] == "new"
    # No still-open items: every bucket is omitted.
    assert model["attention"]["buckets"] == []
    assert model["kpi"]["new"] == 1
    assert model["kpi"]["still_open"] == 0
    # Same convention without the flag: an unset last_reviewed counts as
    # never-reviewed, so the item is still New.
    model2 = report_model(
        state,
        generated_at=NOW,
        coverage_start=NOW,
        first_run=False,
        stale_providers=[],
    )
    assert [i["id"] for g in model2["attention"]["new"]["groups"] for i in g["items"]] == ["gh:1"]


def test_report_model_url_guarded_and_reopened_flag():
    """The model carries only the scheme-safe URL (unsafe -> ""), and the
    reopened flag mirrors reopen_count > 0."""
    from gitreport.attention import report_model

    state = _model_state(
        {
            "gh:1": rec(
                title="Evil",
                url="javascript:alert(1)",
                reopen_count=2,
                first_seen="2026-09-28T06:00:00+00:00",
            ),
            "gh:2": rec(
                title="Odd",
                repo="o/r2",
                url="https://example.com/a(b)",
                first_seen="2026-09-28T06:00:00+00:00",
            ),
            "gh:3": rec(
                title="Safe",
                repo="o/r3",
                url="https://example.com/1",
                first_seen="2026-09-28T06:00:00+00:00",
            ),
        },
        last_reviewed="2026-09-20T06:00:00+00:00",
    )
    model = report_model(
        state,
        generated_at=NOW,
        coverage_start=NOW,
        first_run=False,
        stale_providers=[],
    )
    items = {i["id"]: i for g in model["attention"]["new"]["groups"] for i in g["items"]}
    assert items["gh:1"]["url"] == ""
    assert items["gh:1"]["reopened"] is True
    assert items["gh:2"]["url"] == ""  # parens break [title](url): no link
    assert items["gh:3"]["url"] == "https://example.com/1"
