"""Self-contained HTML digest rendering from the cached Markdown.

Known limitation: every raw ``<`` is rewritten to ``&lt;`` before conversion,
so escaped-source digests cannot use Markdown autolinks (``<https://…>``) or
code spans containing ``<`` or ``&`` — those render literally. Accepted: the
digest format never emits those constructs.
"""

import contextlib
import html
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import markdown

from .attention import _safe_url

_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 52rem; margin: 2rem auto;
       padding: 0 1rem; color: #1a1a1a; line-height: 1.5; }}
h1 {{ font-size: 1.4rem; }} h2 {{ font-size: 1.15rem; margin-top: 2rem; }}
h3 {{ font-size: 1rem; margin-top: 1.2rem; }}
code {{ background: #f2f2f2; padding: 0.1em 0.3em; border-radius: 3px; }}
blockquote {{ border-left: 3px solid #d0a000; margin: 0; padding: 0.2rem 1rem;
              background: #fff9e6; }}
a {{ color: #0645ad; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""

_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
# markdown always serialises attributes double-quoted. Only absolute http(s)
# URLs may be linked: attention entries are guarded by attention.py's
# _URL_UNSAFE + scheme check, activity titles are escape_user-escaped in
# reporting.py, so this is a second line of defence, not the primary guard.
_UNSAFE_HREF_RE = re.compile(r'href="(?!(?:https?)://)[^"]*"', re.IGNORECASE)
# markdown renders `[a](url "title")` with a title="..." attribute; the title
# text is user-supplied and must never sit in an attribute value. The strip is
# scoped to attributes inside <a> tags only, so literal `title="..."` in prose
# survives rendering.
_LINK_TITLE_ATTR_RE = re.compile(r'(<a\b[^>]*?)\s+title="[^"]*"')


def strip_front_matter(md_text: str) -> tuple[dict, str]:
    """Split `---`-fenced front matter from the Markdown body."""
    # The closing fence must be line-anchored: a front-matter VALUE may
    # itself contain `---` (e.g. `a: x---y`) and must not terminate the
    # block mid-line.
    match = re.match(r"\A---\r?\n(?:(.*?)\r?\n)?---\r?\n", md_text, re.DOTALL)
    if not match:
        return {}, md_text
    # BUG-R2-01: with empty front matter the optional meta group does not
    # participate in the match, so group(1) is None — treat it as "".
    meta: dict = {}
    for line in (match.group(1) or "").splitlines():
        key, _, value = line.partition(":")
        if key and value:
            meta[key.strip()] = value.strip()
    return meta, md_text[match.end() :]


def replace_status_line(md_text: str, new_status: str) -> str:
    """Replace the single `Status:` sentinel line in the cached Markdown."""
    # A function replacement keeps new_status literal (backslashes in the
    # status are data, not re.sub replacement escapes); [^\r\n]* stops before
    # the CR of a CRLF terminator so line endings are preserved.
    return re.sub(
        r"^Status: [^\r\n]*",
        lambda _m: new_status,
        md_text,
        count=1,
        flags=re.MULTILINE,
    )


def render_html(md_text: str, title: str = "GitReport digest") -> str:
    """Convert the digest Markdown to a self-contained HTML document.

    Hardening beyond the plain markdown pass, verified against markdown 3.11
    (whose default parser passes raw inline HTML and javascript: URLs through
    verbatim):

    - Every raw ``<`` in the body is rewritten to ``&lt;`` before conversion.
      Attention titles/reasons are escape_user-guarded upstream and activity
      titles are escape_user-escaped in reporting.py, so raw HTML
      (script/svg onload/iframe/...) must never reach the parser. Only ``<``
      is rewritten: ``>`` stays intact so blockquote syntax keeps working.
      Trade-off: a literal ``<`` inside a code span would double-escape on
      display; the digest format emits no such span (the only backticked text
      is the fixed `gitreport read` sentinel).
    - markdown-generated ``<img>`` tags are stripped: the digest must stay
      self-contained (no external references), and no digest section emits
      images intentionally.
    - ``href`` values that are not absolute http(s) URLs are replaced with
      ``#`` (e.g. the javascript: link in the hostile-title test).
    - markdown-generated link ``title="..."`` attributes are stripped:
      user-supplied link titles must never land in an attribute value.
    - The ``title`` argument is HTML-escaped before it enters the template.
    """
    _meta, body = strip_front_matter(md_text)
    safe_body = body.replace("<", "&lt;")
    rendered = markdown.markdown(safe_body, output_format="html")
    rendered = _IMG_TAG_RE.sub("", rendered)
    rendered = _UNSAFE_HREF_RE.sub('href="#"', rendered)
    rendered = _LINK_TITLE_ATTR_RE.sub(r"\1", rendered)
    return _TEMPLATE.format(title=html.escape(title), body=rendered)


# --- dashboard (Task 5+6: dark theme, model-based, inline interactions) ------
#
# The renderer consumes the report model only. The model carries PLAIN text
# (state-stored titles/reasons are unescaped upstream, activity titles arrive
# raw): every dynamic string goes through escape_html here, attribute values
# included. URLs are guarded again by attention._safe_url (the single guard
# both renderers share) — html.escape alone cannot neutralise a javascript:
# href, so an unsafe URL degrades to plain text. The page carries one inline
# <script> (Task 6) that reads only DOM text and data-* attributes — the
# model JSON is never embedded; no <img>, no external references.

_DARK_CSS = """
:root {
  --gr-bg:#111318;
  --gr-card:#1c1f26;
  --gr-border:#2e3340;
  --gr-text:#e8eaf0;
  --gr-muted:#8a93a6;
  --gr-brand:#e95420;
  --gr-accent:#0f95a1;
  --gr-pos:#3fb54a;
  --gr-caution:#f99b11;
  --gr-neg:#e9545b;
  --gr-info:#4c8dff;
  --gr-purple:#a871ff;
  --gr-grey:#8a93a6;
  --gr-radius: 12px;
  --gr-radius-sm: 8px;
  --gr-shadow: 0 1px 3px rgba(0, 0, 0, 0.35);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 1.5rem 1rem 3rem;
  background: var(--gr-bg);
  color: var(--gr-text);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  line-height: 1.5;
}
a { color: var(--gr-info); }
.gr-header { max-width: 1280px; margin: 0 auto 1rem; }
.gr-header h1 { margin: 0 0 0.25rem; font-size: 1.5rem; }
.gr-coverage { margin: 0; color: var(--gr-muted); }
.gr-asof {
  margin: 0.15rem 0 0.6rem;
  font-size: 0.8rem;
  font-style: italic;
  color: var(--gr-muted);
}
.gr-status {
  display: inline-block;
  padding: 0.2rem 0.7rem;
  border: 1px solid transparent;
  border-radius: 999px;
  font-size: 0.78rem;
  font-weight: 600;
}
.gr-status--amber {
  color: var(--gr-caution);
  border-color: var(--gr-caution);
  background: rgba(249, 155, 17, 0.12);
}
.gr-status--green {
  color: var(--gr-pos);
  border-color: var(--gr-pos);
  background: rgba(63, 181, 74, 0.12);
}
.gr-banner {
  max-width: 1280px;
  margin: 0 auto 1rem;
  padding: 0.6rem 0.9rem;
  border-radius: var(--gr-radius-sm);
  font-size: 0.85rem;
}
.gr-banner--warn {
  border: 1px solid var(--gr-caution);
  background: rgba(249, 155, 17, 0.1);
  color: var(--gr-caution);
}
.gr-kpis {
  max-width: 1280px;
  margin: 0 auto 1rem;
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 0.75rem;
}
@media (max-width: 720px) { .gr-kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.gr-kpi {
  background: var(--gr-card);
  border: 1px solid var(--gr-border);
  border-top: 3px solid var(--gr-grey);
  border-radius: var(--gr-radius-sm);
  padding: 0.6rem 0.8rem;
  display: flex;
  flex-direction: column;
  box-shadow: var(--gr-shadow);
  cursor: pointer;
}
.gr-kpi-value { font-size: 1.35rem; font-weight: 650; }
.gr-kpi-label {
  font-size: 0.72rem;
  color: var(--gr-muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.gr-cols {
  max-width: 1280px;
  margin: 0 auto;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 1rem;
  align-items: start;
}
@media (min-width: 900px) { .gr-cols { grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr); } }
.gr-card {
  background: var(--gr-card);
  border: 1px solid var(--gr-border);
  border-radius: var(--gr-radius);
  padding: 1rem;
  box-shadow: var(--gr-shadow);
}
.gr-col-right .gr-card { margin-bottom: 1rem; }
.gr-card h2 { font-size: 1rem; margin: 0 0 0.5rem; }
.gr-card h3 { font-size: 0.85rem; margin: 0.8rem 0 0.25rem; color: var(--gr-muted); }
.gr-section { margin-bottom: 1.25rem; }
.gr-section > h2 { font-size: 1.05rem; margin: 0 0 0.5rem; }
.gr-search {
  width: 100%;
  margin-bottom: 0.5rem;
  padding: 0.45rem 0.7rem;
  background: var(--gr-card);
  color: var(--gr-text);
  border: 1px solid var(--gr-border);
  border-radius: var(--gr-radius-sm);
  font: inherit;
}
.gr-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 0.75rem;
}
.gr-toolbar .gr-search { flex: 1 1 14rem; width: auto; margin-bottom: 0; }
.gr-count { color: var(--gr-muted); font-size: 0.78rem; }
.gr-chips { display: flex; flex-wrap: wrap; gap: 0.35rem; margin-bottom: 0.75rem; }
.gr-chip {
  border: 1px solid var(--gr-border);
  background: transparent;
  color: var(--gr-muted);
  border-radius: 999px;
  padding: 0.1rem 0.6rem;
  font: inherit;
  font-size: 0.75rem;
  cursor: pointer;
}
.gr-chip[aria-pressed="true"] {
  color: var(--gr-text);
  border-color: var(--gr-brand);
  background: rgba(233, 84, 32, 0.16);
}
.gr-repo {
  background: var(--gr-card);
  border: 1px solid var(--gr-border);
  border-radius: var(--gr-radius-sm);
  margin-bottom: 0.6rem;
  box-shadow: var(--gr-shadow);
}
.gr-repo > summary { cursor: pointer; padding: 0.5rem 0.8rem; font-size: 0.9rem; font-weight: 600; }
.gr-items { padding: 0 0.8rem 0.6rem; }
.gr-item { padding: 0.45rem 0; border-top: 1px solid var(--gr-border); }
.gr-item-main { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.4rem; }
.gr-item-title, .gr-sum-title, .gr-act-title {
  color: var(--gr-text);
  text-decoration: none;
  font-weight: 550;
}
a.gr-item-title:hover, a.gr-sum-title:hover, a.gr-act-title:hover {
  color: var(--gr-brand);
  text-decoration: underline;
}
.gr-badge {
  display: inline-block;
  padding: 0 0.45rem;
  border: 1px solid var(--gr-grey);
  border-radius: 999px;
  font-size: 0.68rem;
  font-weight: 600;
  letter-spacing: 0.02em;
  color: var(--gr-grey);
  background: rgba(138, 147, 166, 0.12);
}
.gr-badge--ci {
  color: var(--gr-neg);
  border-color: var(--gr-neg);
  background: rgba(233, 84, 91, 0.13);
}
.gr-badge--pr {
  color: var(--gr-brand);
  border-color: var(--gr-brand);
  background: rgba(233, 84, 32, 0.13);
}
.gr-badge--thread {
  color: var(--gr-purple);
  border-color: var(--gr-purple);
  background: rgba(168, 113, 255, 0.13);
}
.gr-badge--issue {
  color: var(--gr-caution);
  border-color: var(--gr-caution);
  background: rgba(249, 155, 17, 0.13);
}
.gr-badge--bug {
  color: var(--gr-caution);
  border-color: var(--gr-caution);
  background: rgba(249, 155, 17, 0.13);
}
.gr-badge--mp { color: var(--gr-brand); border-color: var(--gr-brand); background: transparent; }
.gr-badge--mention {
  color: var(--gr-accent);
  border-color: var(--gr-accent);
  background: rgba(15, 149, 161, 0.13);
}
.gr-badge--comment {
  color: var(--gr-info);
  border-color: var(--gr-info);
  background: rgba(76, 141, 255, 0.13);
}
.gr-badge--stale {
  color: var(--gr-grey);
  border-color: var(--gr-grey);
  background: rgba(138, 147, 166, 0.12);
}
.gr-badge--item {
  color: var(--gr-muted);
  border-color: var(--gr-muted);
  background: rgba(138, 147, 166, 0.1);
}
.gr-age { color: var(--gr-muted); font-size: 0.75rem; }
.gr-reopened { color: var(--gr-caution); font-size: 0.75rem; font-weight: 600; }
.gr-reason { color: var(--gr-muted); font-size: 0.8rem; margin-top: 0.1rem; }
.gr-muted { color: var(--gr-muted); }
.gr-empty { color: var(--gr-muted); }
.gr-sum-item {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 0.5rem;
  padding: 0.25rem 0;
  border-top: 1px solid var(--gr-border);
}
.gr-act-provider { margin-bottom: 0.75rem; }
.gr-act-provider > h3 { margin-top: 0.25rem; }
.gr-act-repo { margin-top: 0.5rem; }
.gr-act-repo-head { font-weight: 600; font-size: 0.9rem; }
.gr-vis {
  margin-left: 0.35rem;
  padding: 0 0.4rem;
  border: 1px solid var(--gr-border);
  border-radius: 999px;
  font-size: 0.72rem;
  font-weight: 400;
  color: var(--gr-muted);
}
.gr-act-cat {
  margin-top: 0.35rem;
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  color: var(--gr-accent);
}
.gr-act-item { padding: 0.1rem 0; font-size: 0.85rem; }
.gr-merged { margin-left: 0.35rem; font-size: 0.75rem; color: var(--gr-pos); }
"""

# KPI strip: key order and labels are the Task 6 JS contract (data-kpi); the
# accent colours give each card its top border.
_KPI_DEFS = [
    ("new", "New", "var(--gr-accent)"),
    ("still_open", "Still open", "var(--gr-info)"),
    ("assigned", "Assigned", "var(--gr-caution)"),
    ("ci_failing", "CI failing", "var(--gr-neg)"),
    ("stale_prs", "Stale PRs", "var(--gr-grey)"),
]

# Fixed five-chip age dimension; data-val matches item data-age / group
# data-age ("new" included even though it is a tier, not an age bucket).
_AGE_CHIPS = [
    ("new", "New"),
    ("today", "Today"),
    ("week", "Last 7 days"),
    ("month", "Last 30 days"),
    ("older", "Older"),
]

# The inline interaction script (Task 6): vanilla, ES5-style, no network
# calls, reads only DOM text and data-* attributes — the model JSON is never
# embedded in the page. Rendered once, immediately before </body>, so every
# element above it is already parsed.
_DASH_JS = r"""(function () {
  "use strict";

  // Dashboard interactions (Task 6). The page is fully usable without this
  // script: everything renders visible and unfiltered. The script reads only
  // DOM text and data-* attributes — the model JSON is never embedded — and
  // makes no network calls.

  var search = null;
  var countEl = null;
  var chips = [];
  var rows = [];
  var groups = [];
  var sections = [];
  var kpis = [];
  var generatedAt = "";
  var memStore = {};

  // Storage access wrapped in try/catch — sessionStorage may be blocked or
  // full (private browsing); an in-memory map then keeps state page-wide.
  function storageGet(key) {
    var value;
    try {
      value = sessionStorage.getItem(key);
    } catch (err) {
      value = memStore[key];
    }
    return value;
  }

  function storageSet(key, value) {
    try {
      sessionStorage.setItem(key, value);
    } catch (err) {
      memStore[key] = value;
    }
  }

  // Collapsible state key: <generated_at>:<section>:<repo>, where section is
  // the bucket key carried by the group's data-age attribute.
  function groupKey(group) {
    return [
      generatedAt,
      group.getAttribute("data-age") || "",
      group.getAttribute("data-repo") || "",
    ].join(":");
  }

  function restoreOpen(group) {
    var stored = storageGet(groupKey(group));
    if (stored === "0") {
      group.open = false;
    } else if (stored === "1") {
      group.open = true;
    }
  }

  function persistOpen(group) {
    storageSet(groupKey(group), group.open ? "1" : "0");
  }

  function toggleChip(chip) {
    chip.setAttribute(
      "aria-pressed",
      chip.getAttribute("aria-pressed") === "true" ? "false" : "true"
    );
  }

  // data-val values pressed within one dimension; an empty selection matches
  // every row (OR within the dimension, AND across dimensions).
  function pressedValues(dimension) {
    var values = {};
    chips.forEach(function (chip) {
      if (
        chip.getAttribute("data-dim") === dimension &&
        chip.getAttribute("aria-pressed") === "true"
      ) {
        values[chip.getAttribute("data-val")] = true;
      }
    });
    return values;
  }

  // Row matches one chip dimension: OR within the dimension's selected
  // values, an empty selection matching every row. Shared by the row filter
  // and the KPI counters. "type" (badge keys) and "kinds" (raw item kinds,
  // the Assigned KPI's pseudo-dimension) are space-joined attribute lists;
  // age and repo are single attribute values.
  function matchesDimension(row, dimension, values) {
    if (Object.keys(values).length === 0) {
      return true;
    }
    if (dimension === "type" || dimension === "kinds") {
      var list =
        dimension === "type"
          ? row.getAttribute("data-type") || ""
          : row.getAttribute("data-kinds") || "";
      return list
        .split(/\s+/)
        .some(function (key) {
          return key !== "" && values[key];
        });
    }
    return !!values[row.getAttribute("data-" + dimension) || ""];
  }

  // A row stays visible iff the search and every chip dimension match; an
  // empty search or dimension selection always matches. Empty groups and
  // sections are hidden live, the "N of M" count is refreshed and each KPI
  // card is updated to the number of visible rows matching its own filter.
  function applyFilters() {
    var query = search && search.value ? search.value.trim().toLowerCase() : "";
    var typeSel = pressedValues("type");
    var ageSel = pressedValues("age");
    var repoSel = pressedValues("repo");
    var visible = 0;
    rows.forEach(function (row) {
      var text = (row.getAttribute("data-text") || "").toLowerCase();
      var ok =
        (!query || text.indexOf(query) !== -1) &&
        matchesDimension(row, "type", typeSel) &&
        matchesDimension(row, "age", ageSel) &&
        matchesDimension(row, "repo", repoSel);
      row.hidden = !ok;
      if (ok) {
        visible += 1;
      }
    });
    groups.forEach(function (group) {
      group.hidden = !rows.some(function (row) {
        return !row.hidden && group.contains(row);
      });
    });
    sections.forEach(function (section) {
      section.hidden = !rows.some(function (row) {
        return !row.hidden && section.contains(row);
      });
    });
    if (countEl) {
      countEl.textContent = visible + " of " + rows.length + " items";
    }
    updateKpiCounts();
  }

  // KPI cards double as filter shortcuts (spec mapping): each toggles its
  // chip set as a group — all on when not all were on, all off otherwise
  // (KPI_CHIPS below). The KPI_FILTERS mapping is the single source of truth
  // for the live counts: each card shows the number of visible rows matching
  // its own filter (OR within a dimension, AND across dimensions — the chip
  // rule). With no filters active the recomputed counts equal the rendered
  // ones. Assigned counts the kinds pseudo-dimension: the server-side KPI
  // (attention.py) counts "issue_assigned" kinds, and the type badge alone
  // would miss LP rows that badge as "bug".
  var KPI_FILTERS = {
    new: [["age", "new"]],
    still_open: [["age", "today"], ["age", "week"], ["age", "month"], ["age", "older"]],
    assigned: [["kinds", "issue_assigned"]],
    ci_failing: [["type", "ci"]],
    stale_prs: [["type", "stale"]],
  };

  // Chip sets toggled when a KPI card is clicked (spec mapping). Assigned
  // counts kinds, not type badges — an LP issue_assigned row badges as
  // "bug" — so its click toggles the issue AND bug type chips together to
  // include LP bugs in the filtered view (kinds have no chip of their own).
  var KPI_CHIPS = {
    new: [["age", "new"]],
    still_open: [["age", "today"], ["age", "week"], ["age", "month"], ["age", "older"]],
    assigned: [["type", "issue"], ["type", "bug"]],
    ci_failing: [["type", "ci"]],
    stale_prs: [["type", "stale"]],
  };

  function findChip(dimension, value) {
    var found = null;
    chips.forEach(function (chip) {
      if (
        found === null &&
        chip.getAttribute("data-dim") === dimension &&
        chip.getAttribute("data-val") === value
      ) {
        found = chip;
      }
    });
    return found;
  }

  function updateKpiCounts() {
    kpis.forEach(function (card) {
      var byDim = {};
      (KPI_FILTERS[card.getAttribute("data-kpi")] || []).forEach(
        function (spec) {
          (byDim[spec[0]] = byDim[spec[0]] || {})[spec[1]] = true;
        }
      );
      var dims = Object.keys(byDim);
      var value = 0;
      rows.forEach(function (row) {
        if (
          !row.hidden &&
          dims.every(function (dim) {
            return matchesDimension(row, dim, byDim[dim]);
          })
        ) {
          value += 1;
        }
      });
      var valueEl = card.querySelector(".gr-kpi-value");
      if (valueEl) {
        valueEl.textContent = String(value);
      }
    });
  }

  function wireKpiCards() {
    kpis.forEach(function (card) {
      // Clickability is a script enhancement: without JS the cards stay
      // plain, non-interactive divs.
      card.setAttribute("role", "button");
      card.setAttribute("tabindex", "0");
      var activate = function () {
        var mapped = (KPI_CHIPS[card.getAttribute("data-kpi")] || [])
          .map(function (spec) {
            return findChip(spec[0], spec[1]);
          })
          .filter(function (chip) {
            return chip !== null;
          });
        if (mapped.length === 0) {
          return;
        }
        var allOn = mapped.every(function (chip) {
          return chip.getAttribute("aria-pressed") === "true";
        });
        mapped.forEach(function (chip) {
          chip.setAttribute("aria-pressed", allOn ? "false" : "true");
        });
        applyFilters();
      };
      card.addEventListener("click", activate);
      card.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          activate();
        }
      });
    });
  }

  function init() {
    search = document.getElementById("gr-search");
    countEl = document.getElementById("gr-count");
    chips = [].slice.call(document.querySelectorAll(".gr-chip[data-dim]"));
    rows = [].slice.call(document.querySelectorAll(".gr-item"));
    groups = [].slice.call(document.querySelectorAll("details.gr-repo"));
    sections = [].slice.call(document.querySelectorAll(".gr-section"));
    kpis = [].slice.call(document.querySelectorAll(".gr-kpi[data-kpi]"));
    generatedAt = document.body.getAttribute("data-generated-at") || "";
    if (search) {
      search.addEventListener("input", applyFilters);
    }
    chips.forEach(function (chip) {
      chip.addEventListener("click", function () {
        toggleChip(chip);
        applyFilters();
      });
    });
    groups.forEach(function (group) {
      restoreOpen(group);
      group.addEventListener("toggle", function () {
        persistOpen(group);
      });
    });
    var collapseAll = document.getElementById("gr-collapse-all");
    var expandAll = document.getElementById("gr-expand-all");
    if (collapseAll) {
      collapseAll.addEventListener("click", function () {
        groups.forEach(function (group) {
          group.open = false;
        });
      });
    }
    if (expandAll) {
      expandAll.addEventListener("click", function () {
        groups.forEach(function (group) {
          group.open = true;
        });
      });
    }
    wireKpiCards();
    applyFilters();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
"""


def escape_html(s) -> str:
    """Escape plain model text for text nodes AND attribute values."""
    return html.escape(str(s), quote=True)


def atomic_write_text(path: Path, text: str) -> Path:
    """Write `text` to `path` atomically: parent dirs are created, the text
    lands in a sibling temp file that is flushed, fsynced and chmodded to
    0o666 & ~umask (mkstemp's private 0600 must not leak into the report)
    before os.replace()ing the target, so a reader never observes a
    half-written artifact."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp_name, 0o666 & ~umask)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return path


def _parse_ts(ts) -> datetime | None:
    """Parse an ISO timestamp; naive values are treated as UTC (same
    convention as attention). Unparseable/absent input returns None."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _fmt_local(ts, with_time: bool = False) -> str:
    """Local-calendar display of a stored UTC ISO string ("Mon 27 Sep",
    with a "HH:MM" suffix when with_time); "?" for unparseable input."""
    dt = _parse_ts(ts)
    if dt is None:
        return "?"
    local = dt.astimezone()
    if with_time:
        return f"{local:%a} {local.day} {local:%b}, {local:%H:%M}"
    return f"{local:%a} {local.day} {local:%b}"


def _coverage_days(coverage: dict) -> int:
    """Coverage days with the same defensive degrade as the Markdown
    renderer: stored value when sane, else derived from the bounds, else 1."""
    days = coverage.get("days")
    if isinstance(days, int) and not isinstance(days, bool) and days >= 1:
        return days
    start_dt, end_dt = _parse_ts(coverage.get("start")), _parse_ts(coverage.get("end"))
    if start_dt is None or end_dt is None:
        return 1
    return max(1, (end_dt - start_dt).days)


def _status_pill(model: dict) -> str:
    """The banner state: amber until reviewed, green with the local review
    time once model.status says so."""
    status = model.get("status") or {}
    if status.get("reviewed"):
        at = _parse_ts(status.get("reviewed_at") or "")
        label = "Reviewed"
        if at is not None:
            local = at.astimezone()
            label = f"Reviewed {local:%a} {local.day} {local:%b} {local:%H:%M}"
        return f'<span class="gr-status gr-status--green">{escape_html(label)}</span>'
    return '<span class="gr-status gr-status--amber">NOT YET REVIEWED — run gitreport read</span>'


def _anchor(url, text, cls: str) -> str:
    """Attribute-escaped link for a model URL; plain escaped text when the
    URL is falsy or fails the re-checked scheme/unsafe-char guard."""
    safe = _safe_url(url or "")
    if safe:
        return f'<a class="{cls}" href="{escape_html(safe)}">{escape_html(text)}</a>'
    return f'<span class="{cls}">{escape_html(text)}</span>'


def _display_title(entry: dict) -> str:
    return entry.get("title") or entry.get("id") or "(untitled)"


def _badge_keys(entry: dict) -> list[str]:
    return [badge[1] for badge in entry.get("type_badges", [])]


def _attention_items(model: dict) -> list[dict]:
    """Every attention item in model order (new tier, then the buckets)."""
    att = model.get("attention") or {}
    items: list[dict] = []
    for group in (att.get("new") or {}).get("groups", []):
        items += group.get("items", [])
    for bucket in att.get("buckets", []):
        for group in bucket.get("groups", []):
            items += group.get("items", [])
    return items


def _render_item(entry: dict, bucket: str) -> str:
    """One .gr-item row: title link, badge pills, age, re-opened marker and
    muted reasons. The data-* attributes are the Task 6 filter/search
    contract: data-type (space-joined badge keys), data-kinds (space-joined
    raw kinds — the Assigned KPI's pseudo-dimension, immune to the LP bug
    badge override), data-age, data-repo and data-text (title + repo +
    reasons, all escaped)."""
    repo = entry.get("repo", "")
    reasons = entry.get("reasons", [])
    kinds = entry.get("kinds", [])
    main = [_anchor(entry.get("url", ""), _display_title(entry), "gr-item-title")]
    main += [
        f'<span class="gr-badge gr-badge--{escape_html(key)}">{escape_html(label)}</span>'
        for label, key in entry.get("type_badges", [])
    ]
    main.append(f'<span class="gr-age">{escape_html(entry.get("age") or "unknown age")}</span>')
    if entry.get("reopened"):
        main.append('<span class="gr-reopened">re-opened</span>')
    reason_html = "".join(f'<div class="gr-reason">{escape_html(r)}</div>' for r in reasons)
    data_text = " ".join([_display_title(entry), repo, *reasons])
    return (
        f'<div class="gr-item" data-type="{escape_html(" ".join(_badge_keys(entry)))}" '
        f'data-kinds="{escape_html(" ".join(kinds))}" '
        f'data-age="{escape_html(bucket)}" data-repo="{escape_html(repo)}" '
        f'data-text="{escape_html(data_text)}">'
        f'<div class="gr-item-main">{"".join(main)}</div>{reason_html}</div>'
    )


def _render_group(group: dict, bucket: str) -> str:
    """A collapsible repo group; collapsing works without JavaScript."""
    repo = group.get("repo", "")
    items = group.get("items", [])
    rows = "".join(_render_item(entry, bucket) for entry in items)
    return (
        f'<details class="gr-repo" open data-repo="{escape_html(repo)}" '
        f'data-age="{escape_html(bucket)}">'
        f"<summary>{escape_html(repo or '(unknown repo)')} ({len(items)})</summary>"
        f'<div class="gr-items">{rows}</div></details>'
    )


def _render_section(heading: str, groups: list[dict], bucket: str) -> str:
    body = "".join(_render_group(group, bucket) for group in groups)
    return f'<section class="gr-section"><h2>{escape_html(heading)}</h2>{body}</section>'


# Fixed chip labels per type key (BUG-03): two kinds can share a badge key
# ("lp_mp_comment"/"lp_mp_needs_review" both → "mp"), so first-seen badge
# labels would collapse into whichever item came first. Chips show one fixed
# label per key; item-row badges keep their kind-specific labels. Deferred:
# an outline-variant chip/badge styling for MP review.
_CHIP_LABELS = {
    "ci": "CI",
    "pr": "PR review",
    "thread": "thread",
    "issue": "issue",
    "bug": "bug",
    "mp": "MP",
    "mention": "mention",
    "comment": "comment",
    "stale": "stale",
    "item": "item",
}


def _render_chips(model: dict) -> str:
    """Filter chips as toggle buttons (aria-pressed starts false; the inline
    script wires clicking and filtering): one per type key present with its
    fixed label, the fixed age set, one per repo group — deduped in model
    order."""
    type_chips: list[str] = []
    seen_types: set[str] = set()
    repo_chips: list[str] = []
    seen_repos: set[str] = set()
    for entry in _attention_items(model):
        for label, key in entry.get("type_badges", []):
            if key not in seen_types:
                seen_types.add(key)
                type_chips.append(
                    f'<button type="button" class="gr-chip" data-dim="type" '
                    f'data-val="{escape_html(key)}" aria-pressed="false">'
                    f"{escape_html(_CHIP_LABELS.get(key, label))}</button>"
                )
        repo = entry.get("repo", "")
        if repo and repo not in seen_repos:
            seen_repos.add(repo)
            repo_chips.append(
                f'<button type="button" class="gr-chip" data-dim="repo" '
                f'data-val="{escape_html(repo)}" aria-pressed="false">'
                f"{escape_html(repo)}</button>"
            )
    age_chips = [
        f'<button type="button" class="gr-chip" data-dim="age" data-val="{key}" '
        f'aria-pressed="false">{escape_html(label)}</button>'
        for key, label in _AGE_CHIPS
    ]
    return f'<div class="gr-chips">{"".join(type_chips + age_chips + repo_chips)}</div>'


def _render_toolbar(model: dict) -> str:
    """Search box, live "N of M" count (server-rendered as M of M with no
    filters — the script refreshes it on every change) and the collapse /
    expand controls for the repo groups."""
    total = len(_attention_items(model))
    return (
        '<div class="gr-toolbar">'
        '<input type="search" id="gr-search" class="gr-search" '
        'placeholder="Filter\u2026" aria-label="Filter attention items">'
        f'<span id="gr-count" class="gr-count">{total} of {total} items</span>'
        '<button type="button" id="gr-collapse-all" class="gr-chip">'
        "Collapse all</button>"
        '<button type="button" id="gr-expand-all" class="gr-chip">'
        "Expand all</button>"
        "</div>"
    )


def _render_attention(model: dict) -> str:
    """Left column: search box, chips, the New section when non-empty,
    populated bucket sections — or the empty-state card when nothing needs
    attention (spec: empty tiers are omitted)."""
    att = model.get("attention") or {}
    new_groups = (att.get("new") or {}).get("groups", [])
    buckets = [b for b in att.get("buckets", []) if b.get("groups")]
    if not new_groups and not buckets:
        return '<div class="gr-card gr-empty">Nothing needs your attention.</div>'
    parts = [
        _render_toolbar(model),
        _render_chips(model),
    ]
    if new_groups:  # empty tiers are omitted (BUG-01)
        parts.append(_render_section("New since last review", new_groups, "new"))
    parts += [
        _render_section(
            bucket.get("label", bucket.get("key", "")),
            bucket.get("groups", []),
            bucket.get("key", ""),
        )
        for bucket in buckets
    ]
    return "".join(parts)


def _render_kpis(model: dict) -> str:
    """Five always-rendered cards (including 0) with accent top-borders."""
    kpi = model.get("kpi") or {}
    cards = [
        f'<div class="gr-kpi" data-kpi="{key}" style="border-top-color:{accent}">'
        f'<span class="gr-kpi-value">{escape_html(kpi.get(key, 0))}</span>'
        f'<span class="gr-kpi-label">{escape_html(label)}</span></div>'
        for key, label, accent in _KPI_DEFS
    ]
    return f'<section class="gr-kpis" aria-label="Key numbers">{"".join(cards)}</section>'


def _render_stale(model: dict) -> str:
    names = ", ".join(sorted(model.get("stale_providers", [])))
    if not names:
        return ""
    return (
        '<div class="gr-banner gr-banner--warn" role="alert">'
        f"Warning: these providers failed to fetch; their sections may be stale: "
        f"{escape_html(names)}</div>"
    )


def _render_summary_card(model: dict) -> str:
    """Inline CI/stale summaries derived at render time from the attention
    items' kinds ("ci_failure"/"stale_pr", mirroring the KPI counts) — never
    filtered by the Task 6 search/chips and never counted there."""

    def _list(entries: list[dict]) -> str:
        if not entries:
            return '<p class="gr-muted">None.</p>'
        rows = "".join(
            f'<div class="gr-sum-item">'
            f"{_anchor(e.get('url', ''), _display_title(e), 'gr-sum-title')}"
            f'<span class="gr-age">{escape_html(e.get("age") or "unknown age")}</span></div>'
            for e in entries
        )
        return f'<div class="gr-sum-list">{rows}</div>'

    items = _attention_items(model)
    ci = [e for e in items if "ci_failure" in e.get("kinds", [])]
    stale = [e for e in items if "stale_pr" in e.get("kinds", [])]
    return (
        '<div class="gr-card"><h2>Needs summary</h2>'
        "<h3>CI failures</h3>"
        f"{_list(ci)}"
        "<h3>Stale PRs</h3>"
        f"{_list(stale)}</div>"
    )


def _render_activity_card(model: dict) -> str:
    """Recent activity from the nested provider model. Categories with no
    items render nothing, groups whose categories are all empty are skipped,
    and providers without any renderable groups are dropped (the Task 3
    empty a/b case); category labels come from the model. Titles are raw
    provider text — escaped here."""
    providers = [
        p
        for p in (model.get("activity") or {}).get("providers", [])
        if any(any(c.get("items") for c in g.get("categories", [])) for g in p.get("groups", []))
    ]
    if not providers:
        return ""
    provider_blocks = []
    for p in providers:
        repo_blocks = []
        for g in p.get("groups", []):
            categories = [c for c in (g.get("categories") or []) if c.get("items")]
            if not categories:
                continue
            visibility = g.get("visibility") or ""
            vis_html = (
                f'<span class="gr-vis">{escape_html(visibility)}</span>' if visibility else ""
            )
            cat_blocks = []
            for c in categories:
                item_rows = "".join(
                    f'<div class="gr-act-item">'
                    f"{_anchor(i.get('url', ''), i.get('title', ''), 'gr-act-title')}"
                    + (
                        '<span class="gr-merged">→ merged, too</span>'
                        if i.get("also_merged")
                        else ""
                    )
                    + "</div>"
                    for i in c.get("items", [])
                )
                label = c.get("label", c.get("key", ""))
                cat_blocks.append(f'<div class="gr-act-cat">{escape_html(label)}</div>{item_rows}')
            repo_blocks.append(
                f'<div class="gr-act-repo"><div class="gr-act-repo-head">'
                f"{escape_html(g.get('repo', ''))}{vis_html}</div>"
                f"{''.join(cat_blocks)}</div>"
            )
        provider_blocks.append(
            '<div class="gr-act-provider">'
            f"<h3>{escape_html(p.get('label', p.get('provider', '')))}</h3>"
            f"{''.join(repo_blocks)}</div>"
        )
    return f'<div class="gr-card"><h2>Recent activity</h2>{"".join(provider_blocks)}</div>'


def render_dashboard_html(model: dict) -> str:
    """The dark, self-contained dashboard page from the report model.

    Header (title, coverage line with first-run suffix, "ages as of" caption,
    status pill), the always-rendered KPI strip, the stale-provider banner,
    then the two-column body: attention (search + chips + sections) left,
    Needs summary + Recent activity right. Interactions (search, chips,
    collapsibles, KPI shortcuts) live in one inline script that reads only
    DOM text and data-* attributes — the model JSON is never embedded in the
    page, and the page degrades to a fully expanded, unfiltered view without
    JavaScript.
    """
    coverage = model.get("coverage") or {}
    days = _coverage_days(coverage)
    unit = "day" if days == 1 else "days"
    coverage_line = (
        f"Coverage: {_fmt_local(coverage.get('start', ''))} – "
        f"{_fmt_local(coverage.get('end', ''))} ({days} {unit} since last review)"
    )
    if coverage.get("first_run"):
        coverage_line += " (first run: last 24 hours)"

    header = (
        '<header class="gr-header">'
        "<h1>GitReport digest</h1>"
        f'<p class="gr-coverage">{escape_html(coverage_line)}</p>'
        '<p class="gr-asof">ages as of '
        f"{_fmt_local(model.get('generated_at', ''), with_time=True)}</p>"
        + _status_pill(model)
        + "</header>"
    )
    left = f'<div class="gr-col gr-col-left">{_render_attention(model)}</div>'
    right = (
        '<div class="gr-col gr-col-right">'
        + _render_summary_card(model)
        + _render_activity_card(model)
        + "</div>"
    )
    body = (
        header
        + _render_stale(model)
        + _render_kpis(model)
        + f'<div class="gr-cols">{left}{right}</div>'
    )
    generated_at = escape_html(model.get("generated_at") or "")
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>GitReport digest</title>\n"
        f"<style>{_DARK_CSS}</style>\n"
        "</head>\n"
        f'<body data-generated-at="{generated_at}">\n'
        f"{body}\n"
        f"<script>\n{_DASH_JS}</script>\n"
        "</body>\n"
        "</html>\n"
    )
