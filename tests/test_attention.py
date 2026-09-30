import re
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
    report_model,
)
from gitreport.providers.base import empty_repo_activity, escape_user
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


# --- digest renderers (Task 4: model-based, categorized markdown) ------------


def _model(
    items=None,
    *,
    first_run=False,
    stale=(),
    last_reviewed="2026-09-27T06:00:00+00:00",
    coverage_start="2026-09-27T06:00:00+00:00",
    activity=None,
):
    """A report_model digest for renderer tests."""
    extra = {} if first_run else {"last_reviewed": last_reviewed}
    return report_model(
        _model_state(items or {}, **extra),
        generated_at=NOW,
        coverage_start=coverage_start,
        first_run=first_run,
        stale_providers=list(stale),
        activity_data=activity,
    )


def test_render_digest_markdown_structure():
    model = _model(
        {
            "gh:1": rec(
                kinds=["ci_failure", "stale_pr"],
                reasons=["3 unresolved comments"],
                first_seen="2026-09-28T06:00:00+00:00",
                last_updated=NOW,
            ),
            "gh:2": rec(
                repo="o/r2",
                first_seen="2026-09-01T06:00:00+00:00",
                last_updated="2026-09-01T06:00:00+00:00",
                reopen_count=1,
            ),
        }
    )
    md = render_digest_markdown(model)
    # Front matter: raw UTC ISO values, byte-compatible with today.
    assert md.startswith("---\n")
    assert f"generated_at: {NOW}" in md
    assert "coverage_start: 2026-09-27T06:00:00+00:00" in md
    # A single, line-anchored Status sentinel (recovery contract).
    assert re.search(r"^Status: NOT YET REVIEWED", md, re.MULTILINE)
    assert len([line for line in md.splitlines() if line.startswith("Status:")]) == 1
    assert "## Needs attention" in md
    # New tier first, repo groups beneath it.
    assert "### New since last review" in md
    assert re.search(r"^#### o/r$", md, re.MULTILINE)
    # Still-open buckets by label; empty buckets omitted.
    assert "### Still open — Last 30 days" in md
    assert re.search(r"^#### o/r2$", md, re.MULTILINE)
    assert "### Still open — Today" not in md
    assert "### Still open — Last 7 days" not in md
    assert "### Still open — Older" not in md
    # Item lines: badges in precedence order, humanized age, re-opened marker.
    assert "- [T](https://x/1) (CI, stale, today)" in md
    assert "- [T](https://x/1) (mention, 3 weeks ago, re-opened)" in md
    # Reason sub-lines indented beneath their item.
    assert "\n  - 3 unresolved comments" in md
    assert "## Recent activity" in md


def test_render_digest_markdown_empty_state():
    md = render_digest_markdown(_model({}))
    assert "_Nothing needs your attention._" in md
    assert "### New since last review" not in md
    assert "### Still open" not in md
    assert "## Recent activity" in md
    assert "_No activity in the coverage window._" in md


def test_render_digest_markdown_stale_warning():
    md = render_digest_markdown(_model({}, stale=["launchpad", "github"]))
    assert (
        "Warning: these providers failed to fetch; "
        "their sections may be stale: github, launchpad" in md
    )


def test_coverage_line_exact_format(monkeypatch):
    monkeypatch.setattr(attention, "_fmt_local", lambda ts: "PINNED")
    md = render_digest_markdown(_model({}))
    assert "Coverage: PINNED – PINNED (1 day since last review)" in md
    md2 = render_digest_markdown(
        _model(
            {},
            coverage_start="2026-09-26T06:00:00+00:00",
            last_reviewed="2026-09-26T06:00:00+00:00",
        )
    )
    assert "Coverage: PINNED – PINNED (2 days since last review)" in md2


def test_render_digest_markdown_first_run_coverage_suffix(monkeypatch):
    monkeypatch.setattr(attention, "_fmt_local", lambda ts: "PINNED")
    md = render_digest_markdown(_model({"gh:1": rec()}, first_run=True))
    assert "Coverage: PINNED – PINNED (1 day since last review) (first run: last 24 hours)" in md
    assert "(first run:" not in render_digest_markdown(_model({}))


def test_render_digest_markdown_bad_timestamps_no_raise():
    """R2-01 regression: unparseable coverage bounds must not raise (the old
    datetime.min overflow when localised to a negative-offset timezone) —
    the digest still renders, with days degraded to 1."""
    model = _model(
        {"gh:1": rec(first_seen=NOW, last_updated=NOW)},
        last_reviewed="2026-09-29T00:00:00+00:00",
    )
    model["generated_at"] = "bad"
    model["coverage"] = {"start": "bad", "end": "bad", "days": None, "first_run": False}
    md = render_digest_markdown(model)
    assert "## Needs attention" in md
    assert "### Still open — Today" in md
    assert "- [T](https://x/1)" in md
    assert "(1 day since last review)" in md


def test_render_digest_markdown_escapes_plain_text_titles():
    model = _model(
        {
            "gh:1": rec(
                title="Fix & ship <3",
                reasons=["pinged [you]"],
                first_seen="2026-09-28T06:00:00+00:00",
            )
        }
    )
    md = render_digest_markdown(model)
    # The model carries plain text; escaping happens at render time only.
    assert escape_user("Fix & ship <3") in md
    assert escape_user("pinged [you]") in md
    assert "[Fix & ship <3]" not in md


def test_render_digest_markdown_unsafe_url_renders_plain_title():
    model = _model(
        {
            "gh:1": rec(
                title="Evil",
                url="javascript:alert(1)",
                first_seen="2026-09-28T06:00:00+00:00",
            )
        }
    )
    md = render_digest_markdown(model)
    assert "- Evil (" in md
    assert "javascript:" not in md


def test_render_digest_markdown_activity_verbatim():
    empty = empty_repo_activity("public")
    empty["prs_submitted"].append({"title": "T", "url": "https://x/1"})
    model = _model({}, activity={"github": {"o/r": empty}})
    md = render_digest_markdown(model)
    activity_md = model["activity"]["markdown"]
    assert "## Recent activity" in md
    assert "# Git activity report" in md
    assert activity_md.strip() in md  # embedded byte-for-byte (stripped)


def test_render_digest_markdown_reviewed_status_line(monkeypatch):
    """The Status line renders per model.status. `read` normally restamps
    the .md file in place, but a reviewed model must render the Reviewed
    form (same display format as read's stamp)."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    model = _model({})
    model["status"] = {"reviewed": True, "reviewed_at": NOW}
    md = render_digest_markdown(model)
    assert "Status: Reviewed Mon 28 Sep 06:00" in md
    assert "NOT YET REVIEWED" not in md


def test_render_attention_stdout_is_dry_view():
    model = _model(
        {"gh:1": rec(first_seen="2026-09-28T06:00:00+00:00")},
        stale=["launchpad"],
    )
    out = render_attention_stdout(model)
    assert out.startswith("# Needs attention\n\n")
    assert "### New since last review" in out
    assert "launchpad" in out
    # No front matter, no status/coverage lines, no activity section.
    assert "Status:" not in out
    assert "Coverage:" not in out
    assert "## Recent activity" not in out


def test_render_attention_stdout_empty_model():
    out = render_attention_stdout(_model({}))
    assert out == "# Needs attention\n\n_Nothing needs your attention._\n"


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


def _entry(**overrides):
    """A model-shaped attention item for hand-built body reports."""
    base = {
        "id": "gh:1",
        "repo": "o/r",
        "title": "T",
        "url": "https://x/1",
        "reasons": [],
        "kinds": [],
        "type_badges": [["item", "item"]],
        "age": "2 days ago",
        "age_bucket": "new",
        "last_updated": NOW,
        "reopened": False,
    }
    base.update(overrides)
    return base


def _report_with(entry):
    """Flat body report for render_attention_body; the 'month' bucket key is
    deliberately omitted — hand-built dicts may lack bucket keys."""
    return {"new": [{"repo": "o/r", "items": [entry]}], "today": [], "week": [], "older": []}


def test_render_attention_body_item_suffix_and_reasons():
    entry = _entry(
        type_badges=[["CI", "ci"], ["stale", "stale"]],
        age="today",
        reopened=True,
        reasons=["3 unresolved comments"],
    )
    out = render_attention_body(_report_with(entry), [])
    assert "- [T](https://x/1) (CI, stale, today, re-opened)" in out
    assert "\n  - 3 unresolved comments" in out


def test_render_attention_body_bucket_groups():
    report = {
        "new": [],
        "week": [{"repo": "o/r", "items": [_entry()]}],
        "older": [{"repo": "a/b", "items": [_entry(title="U", url="", age="1 month ago")]}],
    }
    out = render_attention_body(report, [])
    assert "### Still open — Last 7 days" in out
    assert "#### o/r" in out
    assert "### Still open — Older" in out
    assert "#### a/b" in out
    assert "- U (item, 1 month ago)" in out


def test_url_guard_javascript_scheme_no_link():
    entry = _entry(title="Evil", url="javascript:alert(1)")
    out = render_attention_body(_report_with(entry), [])
    assert "- Evil" in out
    assert "javascript:" not in out


def test_url_guard_parens_no_link():
    entry = _entry(title="Odd", url="https://example.com/a(b) [x]")
    out = render_attention_body(_report_with(entry), [])
    assert "- Odd" in out
    assert "](https://example.com" not in out


def test_url_guard_unbalanced_paren_no_link():
    entry = _entry(title="Odd", url="https://example.com/a)b")
    out = render_attention_body(_report_with(entry), [])
    assert "- Odd" in out
    assert "](https://example.com" not in out


def test_titles_escaped_at_render():
    entry = _entry(title="Fix & ship <3", url="https://example.com/1", reasons=["a [b] c"])
    out = render_attention_body(_report_with(entry), [])
    assert escape_user("Fix & ship <3") in out
    assert escape_user("a [b] c") in out
    assert "Fix & ship <3" not in out


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

    # Badges are [label, key] LISTS, not tuples: the model is serialised to
    # JSON (tuples become lists) and renderers/reload compare by value.
    # Precedence regardless of input order (state stores kinds sorted).
    assert badges_for(["stale_pr", "ci_failure"], "github") == [["CI", "ci"], ["stale", "stale"]]
    assert badges_for(["comment", "mention", "thread_unresolved"], None) == [
        ["thread", "thread"],
        ["mention", "mention"],
        ["comment", "comment"],
    ]
    # Unknown kind -> ["item", "item"], ordered after all known badges.
    assert badges_for(["ci_failure", "mystery_kind"], "github") == [
        ["CI", "ci"],
        ["item", "item"],
    ]
    assert badges_for(["mystery_kind"], "github") == [["item", "item"]]
    # Launchpad override: issue_assigned renders the bug badge.
    assert badges_for(["issue_assigned"], "launchpad") == [["bug", "bug"]]
    assert badges_for(["issue_assigned"], "github") == [["issue", "issue"]]
    # Duplicate kinds dedupe to one badge.
    assert badges_for(["mention", "mention"], "github") == [["mention", "mention"]]
    # Empty kinds still get the muted item badge so filters can select them.
    assert badges_for([], "github") == [["item", "item"]]


def test_type_badges_json_round_trip_stable():
    """The model's type_badges must survive a JSON round-trip unchanged —
    renderers and reloaded models compare by value, and JSON turns tuples
    into lists, so the model carries lists from the start."""
    import json

    from gitreport.attention import report_model

    model = report_model(
        _model_state({"gh:1": rec(kinds=["ci_failure", "mention"])}),
        generated_at=NOW,
        coverage_start=NOW,
        first_run=False,
        stale_providers=[],
    )
    reloaded = json.loads(json.dumps(model))
    assert reloaded["attention"]["new"]["groups"][0]["items"][0]["type_badges"] == [
        ["CI", "ci"],
        ["mention", "mention"],
    ]


def test_item_entry_provider_fallback_from_mid_prefix():
    """Legacy state items predate the provider field: the lp: id prefix still
    routes issue_assigned to the Launchpad bug badge; a non-LP item without a
    provider keeps the plain issue badge."""
    from gitreport.attention import report_model

    model = report_model(
        _model_state(
            {
                "lp:1": rec(provider=None, kinds=["issue_assigned"], repo="proj"),
                "gh:9": rec(provider=None, kinds=["issue_assigned"], repo="o/r"),
            }
        ),
        generated_at=NOW,
        coverage_start=NOW,
        first_run=False,
        stale_providers=[],
    )
    entries = {i["id"]: i for g in model["attention"]["new"]["groups"] for i in g["items"]}
    assert entries["lp:1"]["type_badges"] == [["bug", "bug"]]
    assert entries["gh:9"]["type_badges"] == [["issue", "issue"]]


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


def test_age_bucket_naive_now_treated_as_utc(monkeypatch):
    """Same naive-`now` guard as humanize_age: a naive `now` is normalised to
    UTC, never interpreted as local time. Pinned to UTC-7 so the two
    interpretations land on different local calendar dates (time.tzset() is
    POSIX-only; see the humanize_age tests)."""
    from gitreport.attention import age_bucket

    monkeypatch.setenv("TZ", "Etc/GMT+7")  # POSIX/tzdata name: UTC-7
    time.tzset()
    now = datetime.fromisoformat("2026-09-28T06:00:00")  # naive
    # 20:00Z on the 27th is 13:00 local on the 27th; the guard maps the naive
    # `now` to 06:00Z = 23:00 local on the 27th — same local date -> today.
    # Without the guard, `now` would read as 06:00 local on the 28th -> week.
    assert age_bucket("2026-09-27T20:00:00+00:00", now) == "today"
    assert age_bucket(None, now) == "older"  # unparseable still buckets last


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
    assert gh1["type_badges"] == [["CI", "ci"], ["stale", "stale"]]
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
    assert gh2["type_badges"] == [["issue", "issue"]]
    assert gh2["age"] == "3 weeks ago"
    assert gh2["age_bucket"] == "month"
    lp1 = buckets["older"]["groups"][0]["items"][0]
    assert lp1["type_badges"] == [["bug", "bug"]]  # LP provider override
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


# --- activity model (Task 3: nested providers/groups/categories) --------------


def test_activity_model_nested():
    from gitreport.attention import activity_model

    empty = empty_repo_activity("public")
    empty["prs_submitted"].append({"title": "T", "url": "https://x/1"})
    other = empty_repo_activity("private")
    data = {"github": {"o/r": empty, "a/b": other}}
    model = activity_model(data)
    prov = model["providers"][0]
    assert prov["provider"] == "github" and prov["label"] == "GitHub"
    repos = [g["repo"] for g in prov["groups"]]
    assert repos == ["a/b", "o/r"]  # alphabetical
    o_r = next(g for g in prov["groups"] if g["repo"] == "o/r")
    assert o_r["visibility"] == "public"
    cats = {c["key"]: c for c in o_r["categories"]}
    assert cats["prs_submitted"]["label"] == "PRs submitted"
    assert cats["prs_submitted"]["items"] == [
        {"title": "T", "url": "https://x/1", "also_merged": False}
    ]


def test_activity_model_omits_empty_categories():
    """Categories with no items are omitted; every repo still gets a group
    even when all of its categories are empty (a/b in the nested test)."""
    from gitreport.attention import activity_model

    repo = empty_repo_activity("public")
    repo["prs_merged"].append({"title": "M", "url": "https://x/2", "also_merged": True})
    model = activity_model({"github": {"o/r": repo}})
    groups = model["providers"][0]["groups"]
    assert [g["repo"] for g in groups] == ["o/r"]
    cats = groups[0]["categories"]
    assert [c["key"] for c in cats] == ["prs_merged"]
    assert cats[0]["items"] == [{"title": "M", "url": "https://x/2", "also_merged": True}]


def test_activity_model_category_order_and_raw_titles():
    """Categories appear in ACTIVITY_CATEGORIES order regardless of insertion
    order; labels come from the provider taxonomy; titles stay RAW (escaping
    happens at render time only)."""
    from gitreport.attention import activity_model

    repo = empty_repo_activity("public")
    repo["issues_closed"].append({"title": "Fix & ship", "url": "https://x/3"})
    repo["prs_submitted"].append({"title": "P", "url": "https://x/4"})
    model = activity_model({"launchpad": {"p": repo}})
    prov = model["providers"][0]
    assert prov["label"] == "Launchpad"
    cats = prov["groups"][0]["categories"]
    assert [c["key"] for c in cats] == ["prs_submitted", "issues_closed"]
    assert cats[0]["label"] == "Merge proposals submitted"  # Launchpad taxonomy
    # Raw title: no unescape_user/escape_user round-trip on the model.
    assert cats[1]["items"][0]["title"] == "Fix & ship"


def test_activity_model_unknown_provider_fallbacks():
    """Unknown providers fall back to .title() display names and title-cased
    category labels, mirroring generate_report's fallbacks."""
    from gitreport.attention import activity_model

    repo = empty_repo_activity("public")
    repo["prs_submitted"].append({"title": "P", "url": "https://x/5"})
    model = activity_model({"gitlab": {"g/r": repo}})
    prov = model["providers"][0]
    assert prov["provider"] == "gitlab"
    assert prov["label"] == "Gitlab"
    cat = prov["groups"][0]["categories"][0]
    assert cat["label"] == "Prs Submitted"


def test_activity_model_provider_insertion_order():
    """Providers keep insertion order (only repos are alphabetised)."""
    from gitreport.attention import activity_model

    data = {
        "launchpad": {"p": empty_repo_activity("public")},
        "github": {"o/r": empty_repo_activity("public")},
    }
    model = activity_model(data)
    assert [p["provider"] for p in model["providers"]] == ["launchpad", "github"]


def test_report_model_activity_wiring():
    """With activity_data, report_model fills providers from activity_model
    and markdown from generate_report; without it, the placeholder stays."""
    from gitreport.attention import activity_model, report_model

    empty = empty_repo_activity("public")
    empty["prs_submitted"].append({"title": "T", "url": "https://x/1"})
    data = {"github": {"o/r": empty}}
    model = report_model(
        _model_state({}),
        generated_at=NOW,
        coverage_start=NOW,
        first_run=False,
        stale_providers=[],
        activity_data=data,
    )
    assert model["activity"]["providers"] == activity_model(data)["providers"]
    md = model["activity"]["markdown"]
    assert "# Git activity report" in md
    assert "- [T](https://x/1)" in md


def test_render_digest_markdown_unknown_repo_group():
    """A model item with an empty repo renders the `(unknown repo)` heading
    instead of a bare `####` (Task 5 controller note)."""
    model = _model({"gh:e": rec(repo="", first_seen=NOW, last_updated=NOW)})
    md = render_digest_markdown(model)
    assert "#### (unknown repo)" in md


def test_render_digest_markdown_repo_escaped():
    """Repo headings pass through escape_user — a repo name carrying HTML
    specials cannot inject markup into the digest."""
    model = _model({"gh:x": rec(repo="a<b & c", first_seen=NOW, last_updated=NOW)})
    md = render_digest_markdown(model)
    assert "#### a&lt;b &amp; c" in md
