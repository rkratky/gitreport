# HTML Dashboard Digest — Design

Date: 2026-09-30
Status: Approved (pending spec review)
Parent spec: `2026-09-28-attention-digest-design.md` (the digest data model,
state store, lifecycle, and CLI are unchanged by this design unless stated).

## Background

The digest's HTML file is currently a single-column conversion of the
canonical Markdown: plain, light, no interactivity. The user wants a
prettier, modern **dark dashboard** — colours, cards, more than one column,
interactivity — with smart categorization (by repo, PR vs issue vs bug vs MP,
by age) and humanized time display ("2 months ago", "1 year, 3 months ago").
Visual language takes inspiration from the Ubuntu-styled dashboards in
`foundations-engineering` (cards, KPIs, badges, status banners), re-tuned for
a dark theme.

## Goals

1. The HTML digest is a self-contained dark dashboard: KPI strip, two-column
   body (attention left, activity + summaries right), collapsible repo
   groups, type badges, humanized age badges, search box and filter chips.
2. Smart categorization: items grouped by repo within the New / Still open
   tiers, bucketed by age (Today / Last 7 days / Last 30 days / Older),
   sorted newest-first throughout; PR/issue/bug/MP/CI type badges derived
   from the existing `kind` field.
3. Inline JavaScript (vanilla, zero dependencies) for search, filter chips,
   collapsibles, and live counts. The file remains self-contained: no
   network, no external references, works from `file://`.
4. The canonical Markdown digest (`.md`, CLI stdout, tests) is unchanged.
   The dashboard is a second view over the same report model.

## Non-goals (v1)

- Configurable themes (dark is the theme).
- Charts/graphs.
- Changes to the digest data model, lifecycle rules, state store, or CLI
  behaviour (except where stated below).
- Server, hosting, external CSS/JS/fonts.

## Decisions made

| Question | Decision |
| --- | --- |
| JavaScript | Allowed: inline, vanilla, zero-dependency, self-contained file; dark theme. **Amends the parent spec's "no JavaScript" constraint for the HTML digest** (the `.md` and CLI output are unaffected) |
| Default organisation | New / Still open tiers preserved; within each, collapsible groups per repo, sorted newest-first; search + repo/type/age filter chips above |
| Age display | Per-item humanized badge (day/week/month/year, largest two units) + four bucket sections for Still open: Today / Last 7 days / Last 30 days / Older |
| HTML data source | Structured report model (Approach A): the model is the shared input of both renderers; a `.json` snapshot is written each digest run as renderer infrastructure (dated file: `<stem>.json`) |
| Inspiration | foundations-engineering dashboards' card/KPI/badge/status-banner language, dark-tuned |

## Architecture

```
src/gitreport/
  attention.py      # gains: report_model(state, ...) -> dict  (serializable)
                    #        humanize_age(ts, now) -> str
                    #        bucket constants extended: today/week/month/older
  html_report.py    # rewritten as the dashboard renderer:
                    #   render_dashboard_html(model, status_line) -> str
                    #   (+ keeps strip/parse helpers; markdown conversion
                    #    of the .md is no longer the HTML source)
  main.py           # digest writes .md + .json (always) + .html (if enabled);
                    #   read re-stamps .md and re-renders .html from .json
```

Data flow (digest): `load state -> fetch -> merge -> prune (unchanged)` then
`report_model(...)` builds the serializable dict (coverage window, banner
state, KPI counts, new/older items with repo/kind/age/bucket, activity
grouped by repo). `render_digest_markdown` consumes it for the `.md`;
`render_dashboard_html` consumes it for the `.html`. The `.json` is the
model snapshot: `read` re-renders the HTML from it after advancing the
status (no provider calls, no re-fetch).

### Report model (plain-JSON-serializable)

```json
{
  "generated_at": "<UTC ISO>",
  "coverage": {"start": "...", "end": "...", "days": 3, "first_run": false},
  "status": {"reviewed": false, "reviewed_at": null},
  "stale_providers": ["github"],
  "kpi": {"new": 2, "still_open": 5, "assigned": 3, "ci_failing": 1, "stale_prs": 2},
  "attention": {
    "new": {"groups": [{"repo": "o/r", "items": [ItemModel]}]},
    "buckets": [
      {"key": "today",  "label": "Today",            "groups": [...]},
      {"key": "week",   "label": "Last 7 days",      "groups": [...]},
      {"key": "month",  "label": "Last 30 days",     "groups": [...]},
      {"key": "older",  "label": "Older",            "groups": [...]}
    ]
  },
  "activity": {"groups": [{"repo": "o/r", "items": [ActivityItemModel]}]}
}
```

`ItemModel` (attention): `id`, `repo`, `title`, `url` (trusted, scheme-guarded
as today), `reasons[]`, `kind`, `type_badge` (derived), `age` (humanized),
`age_bucket` (new items: always "new"; still-open: today/week/month/older),
`last_updated`, `reopened` (bool). `ActivityItemModel`: `repo`, `title`,
`url`, `category`, `also_merged`.

### Type badges (derived from `kind`, no provider changes)

| kind | badge | accent |
| --- | --- | --- |
| review_requested | PR review | orange #e95420 |
| mention, team_mention | mention | teal #0f95a1 |
| comment, author | comment | blue #4c8dff |
| ci_failure | CI | red #e9545b |
| thread_unresolved | thread | purple #a871ff |
| issue_assigned (GH issues and LP assigned bugs) | issue | amber #f99b11 |
| stale_pr | stale | grey #8a93a6 |
| lp_mp_comment, lp_mp_needs_review | MP | orange outline |
| lp_bug_activity | bug | amber |

### Age buckets and humanization

- Buckets are computed in **local time** from `last_updated`: Today (same
  local date), Last 7 days, Last 30 days, Older. New items are not bucketed
  (they render under "New since last review").
- `humanize_age(ts, now)`: "today", "yesterday", then, below one year, the
  largest single unit — "3 days ago", "2 weeks ago", "2 months ago"; at or
  above one year, two units: years + remainder months — "1 year, 3 months
  ago". A future-dated timestamp renders as "just now". Deterministic fixed
  arithmetic: week = 7 days, month = 30.44 days, year = 365.25 days.
- Sorting: within every repo group, `last_updated` descending; repo groups
  ordered by their newest item descending; New tier renders above Still
  open; the Recent Activity column groups by repo, categories as today.

## HTML dashboard (rendered view)

- One self-contained dark file: inline CSS **and** inline JS, no network, no
  external references, no `<img>`, works from `file://`.
- **Palette** (CSS custom properties, dark-tuned Ubuntu accents): page
  `#111318`, cards `#1c1f26`, borders `#2e3340`, text `#e8eaf0`, muted
  `#8a93a6`; accents per the badge table; status banner: amber for NOT YET
  REVIEWED, green for Reviewed.
- **Header**: title + coverage line + status pill (the banner carries the
  `Status:` semantics; front matter never appears in the page).
- **KPI strip**: cards for New, Still open, Assigned, CI failing, Stale PRs —
  clicking one applies the matching filter.
- **Body grid**: two columns on wide screens (stacked below ~900px): left
  (approx. 62%) attention; right (approx. 38%) Recent activity + inline
  summaries for CI failures and stale PRs.
- **Item rows**: title link (trusted URL only), type badge, age badge,
  reasons muted, "re-opened" amber marker.
- **Interactivity (inline JS, progressive degradation)**:
  - Search box filters rows by title/repo/reason substring, hides empty
    groups, live "N of M" count.
  - Filter chips: type and age bucket; multiple chips AND; KPI counts
    reflect the filtered view; chips and search combine.
  - Collapsible repo groups; state kept in `sessionStorage`; collapse-all /
    expand-all control.
  - Without JS: fully expanded, unfiltered page (all sections present).
- **Escaping**: user strings are HTML-escaped by the renderer
  (`escape_html`, distinct from markdown's `escape_user`); links only from
  trusted `url` fields (http/https, no whitespace/parens/angle brackets —
  same guard as today); no images; hostile titles cannot inject markup.

## CLI & artifacts

- `digest` prints the same Markdown as today (snapshot tests must not
  change) and writes dated `.md` + `.json` always, `.html` when
  `html` is in `digest_formats`. Symlinks refresh for `.md`/`.html` as
  today; `.json` gets no symlink (infrastructure).
- `read --open` / `digest --open` open `.html` (fallback `.md` as today).
- `read` re-stamps the `.md` `Status:` line and re-renders `.html` from the
  cached `.json` with the new status banner. If `.json` is missing or
  corrupt, `read` warns and updates the `.md` only (HTML keeps its old
  banner until the next digest).
- `attention` is unchanged.

## Error handling

- A missing/unreadable `.json` during `read`: warn, restamp `.md` only —
  never crash, never mutate state beyond the cursor.
- `report_model` building with zero items/failed providers renders the same
  empty-state dashboard ("Nothing needs your attention.") with the stale
  warning list — same semantics as the Markdown renderer today.
- Renderer never calls providers; failures are impossible from data fetches
  by construction.

## Testing

- `humanize_age`: exhaustive units (today, yesterday, 3 days, 2 weeks,
  2 months, 1 year 3 months, future → "just now"); TZ-deterministic
  (inject `now`; local-time buckets tested with pinned TZs).
- `report_model`: bucket boundaries (today/7d/30d/older), repo grouping,
  sort order, KPI counts, type-badge mapping, first-run and failed-provider
  cases.
- Renderer (from model): dark CSS present, badges, no external refs, no
  `<img`, hostile-title injection tests, `fetch`/`XMLHttpRequest` absent,
  `.json`->HTML byte-identical to direct render.
- `read` round-trip: generate -> read re-renders HTML with only the banner
  changed; missing `.json` -> warn + `.md`-only restamp.
- CLI snapshot: `digest` Markdown output byte-identical to today.

## Future seams

- Light theme via CSS custom properties (all colours are variables).
- Email/Slack renderers consume the same report model / `.json`.
- Persisted filter preferences (localStorage) beyond sessionStorage.