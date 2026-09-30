import json
import os
import re
import time

import pytest

from gitreport.attention import report_model
from gitreport.html_report import (
    atomic_write_text,
    render_dashboard_html,
    render_html,
    replace_status_line,
    strip_front_matter,
)
from gitreport.providers.base import empty_repo_activity
from gitreport.state import AttentionState

MD = """---
generated_at: 2026-09-28T06:00:00+00:00
coverage_start: 2026-09-27T06:00:00+00:00
---

# GitReport digest

Coverage: Mon 27 – Mon 28 Sep (1 day since last review)
Status: NOT YET REVIEWED — run `gitreport read` after reviewing

## Needs attention

- [T](https://github.com/o/r/pull/1)
"""


def test_strip_front_matter():
    meta, body = strip_front_matter(MD)
    assert meta["generated_at"] == "2026-09-28T06:00:00+00:00"
    assert meta["coverage_start"] == "2026-09-27T06:00:00+00:00"
    assert body.startswith("\n# GitReport digest")
    assert "generated_at" not in body


def test_replace_status_line():
    out = replace_status_line(MD, "Status: Reviewed Mon 28 Sep 09:14")
    assert "Status: Reviewed Mon 28 Sep 09:14" in out
    assert "NOT YET REVIEWED" not in out
    # Nothing else changed.
    assert (
        out.replace(
            "Status: Reviewed Mon 28 Sep 09:14",
            "Status: NOT YET REVIEWED — run `gitreport read` after reviewing",
        )
        == MD
    )


def test_render_html_self_contained():
    html = render_html(MD)
    assert html.startswith("<!DOCTYPE html>")
    assert "<script" not in html
    assert "src=" not in html  # no external references
    # Deviation from the brief: entry titles legitimately render as <a href>
    # links, so the no-external-references check is scoped to <head>, where
    # only the inline <style> block may live (no <link> stylesheet).
    assert "href=" not in html.split("<body")[0]  # only inline CSS
    assert "GitReport digest" in html


def test_render_html_hostile_title():
    hostile = MD.replace(
        "[T](https://github.com/o/r/pull/1)",
        "[<script>alert(1)</script>](javascript:alert(1)) ![x](https://tracker/pixel)",
    )
    html = render_html(hostile)
    assert "<script>alert" not in html
    assert "javascript:" not in html
    assert "<img" not in html


def test_render_html_escapes_title():
    # Hardening beyond the brief: the <title> slot is document metadata and
    # must not become a raw-HTML injection point.
    html = render_html(MD, title='<script>alert("x")</script>')
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_render_html_strips_link_title_attribute():
    # BUG-01: a user-supplied link title (`[a](url "title")`) lands in a
    # title="..." attribute after markdown conversion — strip it so hostile
    # text can never sit in an attribute value.
    md = MD.replace("[T](https://github.com/o/r/pull/1)", '[a](https://ok "user text")')
    html = render_html(md)
    assert 'title="' not in html
    assert "user text" not in html


def test_replace_status_line_backslashes_round_trip():
    # BUG-02: the new status is data — backslashes must be inserted literally,
    # not interpreted as re.sub replacement escapes (`\p` raises, `\n`
    # injects a newline).
    new_status = r"Status: Reviewed \path\to\file"
    out = replace_status_line(MD, new_status)
    assert new_status in out
    assert "NOT YET REVIEWED" not in out


def test_replace_status_line_crlf_preserves_cr():
    # BUG-02: `.` matched \r, so the CR terminator of the Status line was
    # swallowed on CRLF files. The tightened pattern must stop before \r.
    out = replace_status_line(MD.replace("\n", "\r\n"), "Status: Reviewed Mon 28")
    lines = out.split("\n")
    assert "Status: Reviewed Mon 28\r" in lines
    assert "# GitReport digest\r" in lines  # other lines keep their CRs
    assert "NOT YET REVIEWED" not in out


def test_strip_front_matter_crlf():
    # BUG-03: the fence pattern required bare \n; CRLF front matter was not
    # recognised and meta parsing silently returned nothing.
    meta, body = strip_front_matter(MD.replace("\n", "\r\n"))
    assert meta["generated_at"] == "2026-09-28T06:00:00+00:00"
    assert meta["coverage_start"] == "2026-09-27T06:00:00+00:00"
    assert body.startswith("\r\n# GitReport digest")


def test_strip_front_matter_value_with_dashes():
    # N-01: the closing fence must be line-anchored — a front-matter VALUE
    # containing `---` must not terminate the block mid-line; the value must
    # survive intact and the real closing fence must still be found.
    md = "---\na: x---y\n---\n\nbody\n"
    meta, body = strip_front_matter(md)
    assert meta["a"] == "x---y"
    assert body.startswith("\nbody\n")


def test_strip_front_matter_empty_block():
    # BUG-R2-01: empty front matter (`---\n---\nbody`) crashed — the optional
    # meta group does not participate in the match, so group(1) is None and
    # .splitlines() was called on it.
    assert strip_front_matter("---\n---\nbody\n") == ({}, "body\n")


def test_strip_front_matter_empty_block_crlf():
    # BUG-R2-01: same crash for CRLF empty front matter.
    assert strip_front_matter("---\r\n---\r\nbody\r\n") == ({}, "body\r\n")


def test_render_html_preserves_literal_title_in_prose():
    # N-02: the title-attribute strip must only touch attributes inside <a>
    # tags — literal `title="hi"` in prose must survive rendering untouched.
    md = MD.replace("[T](https://github.com/o/r/pull/1)", 'see the title="hi" note')
    html = render_html(md)
    assert 'title="hi"' in html


# --- dashboard renderer (Task 5: dark theme, static, model-based) ------------

DASH_NOW = "2026-09-28T06:00:00+00:00"


@pytest.fixture
def utc_tz(monkeypatch):
    """Pin the local timezone to UTC so local-time strings are deterministic
    (POSIX-only, like the rest of the suite; conftest restores after each test)."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()


def _dash_rec(**overrides):
    base = {
        "status": "open",
        "origins": ["notification"],
        "kinds": ["mention"],
        "reasons": [],
        "repo": "o/r",
        "title": "T",
        "url": "https://x/1",
        "first_seen": DASH_NOW,
        "last_updated": DASH_NOW,
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


def _dash_model(items=None, *, first_run=False, stale=(), activity=None):
    extra = {} if first_run else {"last_reviewed": "2026-09-27T06:00:00+00:00"}
    return report_model(
        AttentionState.model_validate({"version": 1, "items": items or {}, **extra}),
        generated_at=DASH_NOW,
        coverage_start="2026-09-27T06:00:00+00:00",
        first_run=first_run,
        stale_providers=list(stale),
        activity_data=activity,
    )


def _dash_items():
    return {
        "gh:1": _dash_rec(
            title="New one",
            kinds=["ci_failure", "stale_pr"],
            reasons=["3 unresolved comments"],
            url="https://github.com/o/r/pull/1",
            first_seen=DASH_NOW,
            last_updated=DASH_NOW,
        ),
        "gh:2": _dash_rec(
            repo="o/r2",
            url="https://github.com/o/r2/pull/2",
            first_seen="2026-09-01T06:00:00+00:00",
            last_updated="2026-09-01T06:00:00+00:00",
            reopen_count=1,
        ),
    }


def test_dashboard_dark_theme_static(utc_tz):
    html = render_dashboard_html(_dash_model(_dash_items()))
    assert '<html lang="en">' in html
    assert "--gr-bg" in html
    assert "#111318" in html
    assert "--gr-brand:#e95420" in html
    assert html.count("<script") == 1  # Task 6: exactly one inline script
    assert "<img" not in html
    assert "src=" not in html
    # Every href is a model item link (rows + right-column summaries), nothing else.
    assert re.findall(r'href="([^"]*)"', html) == [
        "https://github.com/o/r/pull/1",  # gh:1 row
        "https://github.com/o/r2/pull/2",  # gh:2 row
        "https://github.com/o/r/pull/1",  # CI failures summary
        "https://github.com/o/r/pull/1",  # stale PRs summary
    ]


def test_dashboard_header_coverage_status_ages(utc_tz):
    html = render_dashboard_html(_dash_model(_dash_items()))
    assert "GitReport digest" in html
    assert "Coverage: Sun 27 Sep – Mon 28 Sep (1 day since last review)" in html
    assert "ages as of Mon 28 Sep, 06:00" in html
    assert "gr-status--amber" in html
    assert "NOT YET REVIEWED — run gitreport read" in html
    # The stylesheet always defines both pill classes; assert on the element.
    assert "gr-status gr-status--green" not in html


def test_dashboard_first_run_suffix(utc_tz):
    html = render_dashboard_html(_dash_model({}, first_run=True))
    assert "(first run: last 24 hours)" in html


def test_dashboard_reviewed_status_pill(utc_tz):
    model = _dash_model(_dash_items())
    model["status"] = {"reviewed": True, "reviewed_at": "2026-09-28T09:14:00+00:00"}
    html = render_dashboard_html(model)
    assert "gr-status--green" in html
    assert "Reviewed Mon 28 Sep 09:14" in html
    assert "NOT YET REVIEWED" not in html


def test_dashboard_kpi_cards(utc_tz):
    html = render_dashboard_html(_dash_model(_dash_items()))
    assert re.findall(r'data-kpi="([a-z_]+)"', html) == [
        "new",
        "still_open",
        "assigned",
        "ci_failing",
        "stale_prs",
    ]


def test_dashboard_kpi_cards_zero(utc_tz):
    html = render_dashboard_html(_dash_model({}))
    assert re.findall(r'data-kpi="([a-z_]+)"', html) == [
        "new",
        "still_open",
        "assigned",
        "ci_failing",
        "stale_prs",
    ]
    assert html.count('class="gr-kpi-value">0<') == 5


def test_dashboard_sections_details_and_item_attrs(utc_tz):
    html = render_dashboard_html(_dash_model(_dash_items()))
    # New section first, then populated buckets only.
    assert "<h2>New since last review</h2>" in html
    assert "<h2>Last 30 days</h2>" in html
    assert "<h2>Today</h2>" not in html
    assert "<h2>Last 7 days</h2>" not in html
    assert "<h2>Older</h2>" not in html
    # Repo groups: collapsible, carry repo + bucket key for the JS contract.
    assert '<details class="gr-repo" open data-repo="o/r" data-age="new">' in html
    assert '<details class="gr-repo" open data-repo="o/r2" data-age="month">' in html
    assert "<summary>o/r (1)</summary>" in html
    assert "<summary>o/r2 (1)</summary>" in html
    # Item rows: filterable attributes, badges, age, re-opened marker, reasons.
    assert 'data-type="ci stale"' in html
    assert 'data-type="mention"' in html
    assert 'data-age="new"' in html
    assert 'data-age="month"' in html
    assert 'data-repo="o/r"' in html
    assert 'data-text="New one o/r 3 unresolved comments"' in html
    assert '<span class="gr-badge gr-badge--ci">CI</span>' in html
    assert '<span class="gr-badge gr-badge--stale">stale</span>' in html
    assert '<span class="gr-badge gr-badge--mention">mention</span>' in html
    assert '<span class="gr-age">today</span>' in html
    assert '<span class="gr-age">3 weeks ago</span>' in html
    assert '<span class="gr-reopened">re-opened</span>' in html
    assert '<div class="gr-reason">3 unresolved comments</div>' in html
    assert 'href="https://github.com/o/r/pull/1">New one</a>' in html


def test_dashboard_filter_chips(utc_tz):
    html = render_dashboard_html(_dash_model(_dash_items()))
    assert 'placeholder="Filter\u2026"' in html
    # One chip per type key present, in model (precedence) order.
    assert re.findall(r'data-dim="type" data-val="([a-z]+)"', html) == [
        "ci",
        "stale",
        "mention",
    ]
    # Age chips are the fixed five-dimension set.
    assert re.findall(r'data-dim="age" data-val="([a-z]+)"', html) == [
        "new",
        "today",
        "week",
        "month",
        "older",
    ]
    assert re.findall(r'data-dim="repo" data-val="([^"]+)"', html) == ["o/r", "o/r2"]


def test_dashboard_summary_lists(utc_tz):
    html = render_dashboard_html(_dash_model(_dash_items()))
    assert "Needs summary" in html
    assert "CI failures" in html
    assert "Stale PRs" in html
    # gh:1 (CI + stale) appears in both summaries with link and age.
    assert html.count('href="https://github.com/o/r/pull/1"') == 3


def test_dashboard_activity_card(utc_tz):
    repo = empty_repo_activity("public")
    repo["prs_merged"].append(
        {"title": "Land docs", "url": "https://github.com/o/r/pr/9", "also_merged": True}
    )
    html = render_dashboard_html(_dash_model({}, activity={"github": {"o/r": repo}}))
    assert "GitHub" in html
    assert "PRs merged" in html
    assert 'href="https://github.com/o/r/pr/9">Land docs</a>' in html
    assert "→ merged, too" in html
    assert "public" in html


def test_dashboard_activity_skips_empty_providers_and_groups(utc_tz):
    # The a/b empty case from Task 3: an all-empty repo group renders nothing,
    # and a provider with only such groups is dropped entirely.
    merged = empty_repo_activity("public")
    merged["prs_merged"].append({"title": "M", "url": "https://x/2"})
    data = {
        "ghostprov": {"ghost/repo": empty_repo_activity("private")},
        "github": {"o/r": merged, "a/b": empty_repo_activity("public")},
    }
    html = render_dashboard_html(_dash_model({}, activity=data))
    assert "Ghostprov" not in html
    assert "ghost/repo" not in html
    assert "a/b" not in html
    assert 'href="https://x/2">M</a>' in html


def test_dashboard_empty_state_and_stale_warning(utc_tz):
    html = render_dashboard_html(_dash_model({}, stale=("github", "launchpad")))
    # Same wording as the Markdown empty state (BUG-02, final wave).
    assert "Nothing needs your attention." in html
    assert "failed to fetch" in html
    assert "github, launchpad" in html


def test_dashboard_empty_state_without_stale(utc_tz):
    html = render_dashboard_html(_dash_model({}))
    assert "Nothing needs your attention." in html
    assert "failed to fetch" not in html


def test_dashboard_hostile_title_inert(utc_tz):
    items = {
        "gh:h": _dash_rec(
            title="<script>alert(1)</script>",
            url="javascript:alert(1)",
            reasons=['say "hi" & <b>'],
            first_seen=DASH_NOW,
            last_updated=DASH_NOW,
        ),
    }
    html = render_dashboard_html(_dash_model(items))
    assert "<script>alert" not in html
    assert "javascript:" not in html  # scheme guard: no link, URL never rendered
    assert "<img" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html  # title shown, escaped
    assert "say &quot;hi&quot; &amp; &lt;b&gt;" in html  # reasons escaped too


def test_dashboard_renderer_url_guard(utc_tz):
    # The model's URL is guarded upstream, but the renderer re-checks so a
    # hand-built model cannot inject a link either.
    model = _dash_model(_dash_items())
    model["attention"]["new"]["groups"][0]["items"][0]["url"] = "javascript:alert(1)"
    html = render_dashboard_html(model)
    assert "javascript:" not in html
    assert "New one" in html  # title still shown as plain text

    model = _dash_model(_dash_items())
    model["attention"]["new"]["groups"][0]["items"][0][
        "url"
    ] = 'https://ok.example/1"onmouseover="x'
    html = render_dashboard_html(model)
    assert "onmouseover" not in html


def test_dashboard_item_without_url(utc_tz):
    model = _dash_model(_dash_items())
    model["attention"]["new"]["groups"][0]["items"][0]["url"] = ""
    html = render_dashboard_html(model)
    assert '<span class="gr-item-title">New one</span>' in html


def test_dashboard_lp_bug_badge_override(utc_tz):
    items = {
        "lp:1": _dash_rec(
            provider="launchpad",
            kinds=["issue_assigned"],
            repo="lp:ubuntu",
            title="Fix crash",
            url="https://bugs.launchpad.net/bugs/1",
            first_seen=DASH_NOW,
            last_updated=DASH_NOW,
        ),
    }
    html = render_dashboard_html(_dash_model(items))
    assert '<span class="gr-badge gr-badge--bug">bug</span>' in html
    assert 'data-type="bug"' in html


def test_dashboard_assigned_kpi_matches_kinds(utc_tz):
    # BUG-01 (final wave): the Assigned KPI counts kinds ("issue_assigned"),
    # not type badges — an LP issue_assigned row badges as "bug", so a
    # badge-based JS count undercounts on load and its click misses LP bugs.
    # The row carries data-kinds for the JS pseudo-dimension; the script's
    # assigned filter is kinds-based and its click toggles the issue + bug
    # type chips together.
    items = {
        "lp:1": _dash_rec(
            provider="launchpad",
            kinds=["issue_assigned"],
            repo="lp:ubuntu",
            title="Fix crash",
            url="https://bugs.launchpad.net/bugs/1",
            first_seen=DASH_NOW,
            last_updated=DASH_NOW,
        ),
    }
    page = render_dashboard_html(_dash_model(items))
    assert 'data-kinds="issue_assigned"' in page
    script = _dash_script(page)
    assert 'assigned: [["kinds", "issue_assigned"]]' in script
    assert 'assigned: [["type", "issue"], ["type", "bug"]]' in script


def test_dashboard_json_round_trip_stable(utc_tz):
    repo = empty_repo_activity("public")
    repo["prs_merged"].append({"title": "M", "url": "https://x/2", "also_merged": True})
    model = _dash_model(
        _dash_items(),
        activity={"github": {"o/r": repo}, "gitlab": {"g/r": empty_repo_activity("public")}},
    )
    model["status"] = {"reviewed": True, "reviewed_at": DASH_NOW}
    assert render_dashboard_html(json.loads(json.dumps(model))) == render_dashboard_html(model)


def test_dashboard_schema_version_tolerated(utc_tz):
    model = _dash_model(_dash_items())
    model["schema_version"] = 99
    html = render_dashboard_html(model)
    assert "GitReport digest" in html


def test_atomic_write_text_creates_parent_and_writes(tmp_path):
    target = tmp_path / "sub" / "dir" / "out.html"
    result = atomic_write_text(target, "<p>hi</p>")
    assert result == target
    assert target.read_text(encoding="utf-8") == "<p>hi</p>"
    assert [p.name for p in target.parent.iterdir()] == ["out.html"]  # tmp cleaned up


def test_atomic_write_text_replaces_existing(tmp_path):
    target = tmp_path / "out.html"
    atomic_write_text(target, "old")
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["out.html"]


# --- fix round 1 (review findings) -------------------------------------------


def test_dashboard_omits_empty_new_section(utc_tz):
    # BUG-01: empty tiers are omitted — a model with only bucket items shows
    # no "New since last review" heading, the bucket tier still renders.
    items = {
        "gh:2": _dash_rec(
            repo="o/r2",
            url="https://github.com/o/r2/pull/2",
            first_seen="2026-09-01T06:00:00+00:00",
            last_updated="2026-09-01T06:00:00+00:00",
        ),
    }
    html = render_dashboard_html(_dash_model(items))
    assert "<h2>New since last review</h2>" not in html
    assert "<h2>Last 30 days</h2>" in html


def test_dashboard_summary_selects_on_kinds(utc_tz):
    # BUG-02: CI/stale summaries select on entry kinds ("ci_failure"/
    # "stale_pr" per spec), not on badge keys — a type key that differs from
    # the kind still lands in the summary.
    model = _dash_model(_dash_items())
    model["attention"]["new"]["groups"][0]["items"][0]["type_badges"] = [["item", "item"]]
    html = render_dashboard_html(model)
    # Row + CI-failures summary + stale-PRs summary.
    assert html.count('href="https://github.com/o/r/pull/1"') == 3


def test_dashboard_chips_fixed_labels_mp(utc_tz):
    # BUG-03: chips carry a fixed label per key — "lp_mp_comment" and
    # "lp_mp_needs_review" both badge as key "mp", so per-badge labels would
    # collapse (here to "MP review", first-seen) ; the chip row shows exactly
    # one "MP" chip regardless of kind order. The MP-review outline-variant
    # styling is deferred. Item badges keep their labels.
    items = {
        "lp:1": _dash_rec(
            provider="launchpad",
            kinds=["lp_mp_needs_review", "lp_mp_comment"],
            repo="lp:ubuntu",
            title="MP both",
            url="https://code.launchpad.net/~x/y/+merge/1",
            first_seen=DASH_NOW,
            last_updated=DASH_NOW,
        ),
    }
    html = render_dashboard_html(_dash_model(items))
    assert html.count('data-dim="type" data-val="mp"') == 1
    assert (
        '<button type="button" class="gr-chip" data-dim="type" data-val="mp" '
        'aria-pressed="false">MP</button>'
    ) in html
    assert '<span class="gr-badge gr-badge--mp">MP</span>' in html
    assert '<span class="gr-badge gr-badge--mp">MP review</span>' in html


def test_atomic_write_text_umask_mode(tmp_path):
    # BUG-04: the target's mode is 0o666 & ~umask — world-readable under
    # umask 022 (mkstemp's private 0600 must not leak into the report).
    old = os.umask(0o022)
    try:
        target = tmp_path / "out.html"
        atomic_write_text(target, "hi")
        assert (target.stat().st_mode & 0o777) == 0o644
    finally:
        os.umask(old)


def test_dashboard_activity_skips_empty_categories(utc_tz):
    # BUG-05: a category with no items renders no label, and a group renders
    # only when at least one of its categories has items.
    repo = empty_repo_activity("public")
    repo["prs_merged"].append({"title": "M", "url": "https://x/2"})
    model = _dash_model({}, activity={"github": {"o/r": repo}})
    cats = model["activity"]["providers"][0]["groups"][0]["categories"]
    cats.append({"key": "prs_submitted", "label": "PRs submitted", "items": []})
    model["activity"]["providers"][0]["groups"].append(
        {
            "repo": "o/empty",
            "visibility": "public",
            "categories": [{"key": "prs_submitted", "label": "PRs submitted", "items": []}],
        }
    )
    html = render_dashboard_html(model)
    assert "PRs reviewed" not in html
    assert "o/empty" not in html
    assert "PRs submitted" not in html
    assert 'href="https://x/2">M</a>' in html


# --- Task 6: inline JS interactions (static DOM contract, no browser) --------


def _dash_script(page: str) -> str:
    """Extract the single inline <script> block from a rendered dashboard."""
    assert page.count("<script") == 1  # exactly one block
    assert page.count("</script>") == 1
    start = page.index("<script")
    end = page.index("</script>", start)
    return page[start:end]


def test_dashboard_inline_script_contract(utc_tz):
    page = render_dashboard_html(_dash_model(_dash_items()))
    script = _dash_script(page)
    # Inline only: no src attribute anywhere on the page, and no network
    # calls — the page stays self-contained.
    assert "src=" not in page
    assert "fetch(" not in page
    assert "XMLHttpRequest" not in page
    # Interaction markers: event listeners, the shared filter routine, chip
    # toggling and data-* substring matching over the row payload.
    assert "addEventListener" in script
    assert "applyFilters" in script
    assert "aria-pressed" in script
    assert 'getAttribute("data-text")' in script
    assert 'getAttribute("data-type")' in script
    assert 'getAttribute("data-kinds")' in script
    assert 'getAttribute("data-age")' in script
    assert 'getAttribute("data-repo")' in script
    # Every sessionStorage access must sit inside a try (in-memory fallback):
    # a `try` appears within 200 chars before each occurrence.
    for match in re.finditer("sessionStorage", script):
        assert "try" in script[max(0, match.start() - 200) : match.start()]


def test_dashboard_generated_at_body_attr(utc_tz):
    # Storage keys are <generated_at>:<section>:<repo>; the timestamp comes
    # from a data-generated-at attribute on <body> and the script builds on it.
    page = render_dashboard_html(_dash_model(_dash_items()))
    assert '<body data-generated-at="2026-09-28T06:00:00+00:00">' in page
    assert "data-generated-at" in _dash_script(page)


def test_dashboard_toolbar_count_and_no_js_visibility(utc_tz):
    page = render_dashboard_html(_dash_model(_dash_items()))
    assert 'id="gr-search"' in page
    # Initial render is the no-JS view: an empty search shows "M of M".
    assert '<span id="gr-count" class="gr-count">2 of 2 items</span>' in page
    assert 'id="gr-collapse-all"' in page
    assert 'id="gr-expand-all"' in page
    # Nothing is hidden at render time: no element carries a hidden attribute.
    assert re.search(r"<[a-zA-Z][^>]*\shidden[\s=>]", page) is None


def test_dashboard_chips_render_unpressed(utc_tz):
    page = render_dashboard_html(_dash_model(_dash_items()))
    pressed = re.findall(r'data-dim="[a-z]+" data-val="[^"]*" aria-pressed="false"', page)
    assert len(pressed) == 10  # 3 type + 5 age + 2 repo


def test_dashboard_kpi_click_contract(utc_tz):
    # KPI cards act as filter shortcuts: the script selects them and maps
    # each card onto its chip set per the spec table.
    page = render_dashboard_html(_dash_model(_dash_items()))
    script = _dash_script(page)
    assert ".gr-kpi" in script
    for val in ("new", "today", "week", "month", "older", "issue", "ci", "stale"):
        assert f'"{val}"' in script


def test_dashboard_kpi_count_contract(utc_tz):
    # Spec §Interactivity: KPI counts reflect the filtered view. The script
    # recomputes each card's value on every filter pass from the rows that
    # stay visible, writing into the count element inside the card; with no
    # filters active the recomputed counts equal the rendered ones.
    page = render_dashboard_html(_dash_model(_dash_items()))
    script = _dash_script(page)
    assert "function updateKpiCounts" in script
    assert "updateKpiCounts();" in script  # invoked from the shared routine
    assert 'querySelector(".gr-kpi-value")' in script
    assert "!row.hidden" in script  # only visible rows are counted
