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


def test_render_html_preserves_literal_title_in_prose():
    # N-02: the title-attribute strip must only touch attributes inside <a>
    # tags — literal `title="hi"` in prose must survive rendering untouched.
    md = MD.replace("[T](https://github.com/o/r/pull/1)", 'see the title="hi" note')
    html = render_html(md)
    assert 'title="hi"' in html
