from gitreport.html_report import (
    render_html,
    replace_status_line,
    strip_front_matter,
)

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
