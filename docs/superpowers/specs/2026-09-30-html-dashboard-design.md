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
   tiers (Still open bucketed by age: Today / Last 7 days / Last 30 days /
   Older), attention items sorted newest-first; type badges derived from
   each item's `kinds[]`.
3. Inline JavaScript (vanilla, zero dependencies) for search, filter chips,
   collapsibles, and live counts. The file remains self-contained: no
   network, no external references, works from `file://`.
4. The Markdown digest (`.md`, CLI stdout) gains the same categorization in
   plain-text form — repo groups, the four age buckets, humanized ages and
   type tags — staying readable, pipeable Markdown throughout.

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
| Age display | Per-item humanized badge (day/week/month/year; largest single unit below one year; years + months at or above) + four bucket sections for Still open: Today / Last 7 days / Last 30 days / Older |
| Markdown categorization | The `.md`/stdout digest mirrors the dashboard structure in plain text: New + four Still-open buckets, `#### repo` groups within each, per-item humanized age and type tags; `generate` (activity report) is unchanged |
| HTML data source | Structured report model (Approach A): the model is the shared input of both renderers; a `.json` snapshot is written each digest run as renderer infrastructure (dated file: `<stem>.json`) |
| Inspiration | foundations-engineering dashboards' card/KPI/badge/status-banner language, dark-tuned |

## Amendments to the parent spec

- **Still-open buckets 3 → 4**: the parent spec's "today / last 7 days /
  older than 7 days" buckets are superseded by Today / Last 7 days /
  Last 30 days / Older.
- **HTML source**: the dashboard renders from the report model / `.json`
  snapshot, not from a Markdown conversion; the `markdown` conversion path
  is retained only as the pre-upgrade fallback (see *CLI & artifacts*).
- **`read` mechanism**: `read` re-renders the HTML from the `.json`
  snapshot, not from the cached `.md`.
- **`.md` and `.json` are both renderer infrastructure**: the `.json`
  snapshot is written on every digest run (no symlink), like the `.md`.
- **Restamp test wording**: the `read` restamp guarantee is that "only the
  status banner changes".

## Architecture

```
src/gitreport/
  attention.py      # gains: report_model(state, *, generated_at,
                    #          coverage_start, first_run, stale_providers,
                    #          activity_data=None) -> dict  (serializable)
                    #        humanize_age(ts, now) -> str
                    #        unescape_user(s) -> str
                    #        bucket constants extended: today/week/month/older
  html_report.py    # rewritten as the dashboard renderer:
                    #   render_dashboard_html(model) -> str
                    #   (+ keeps strip/parse helpers and the legacy
                    #    markdown->HTML conversion, retained only as the
                    #    pre-upgrade fallback render path)
  main.py           # digest writes .md + .json (always) + .html (if enabled),
                    #   all atomically (tmp file + os.replace);
                    #   read re-stamps .md, advances status in .json under
                    #   the state lock, re-renders .html from .json
```

Data flow (digest): `load state -> fetch -> merge -> prune (unchanged)` then
`report_model(...)` builds the serializable dict (coverage window, banner
state, KPI counts, new/older items with repo/kinds/age/bucket, activity in
nested provider groups). `render_digest_markdown` consumes it for the `.md`;
`render_dashboard_html` consumes it for the `.html`. The `.json` is the
model snapshot: `read` rewrites it with the advanced status (atomically,
under the state lock) and re-renders the HTML from it (no provider calls,
no re-fetch).

### Report model (plain-JSON-serializable)

```json
{
  "schema_version": 1,
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
  "activity": {
    "markdown": "<generate_report output, verbatim>",
    "providers": [
      {"provider": "github", "label": "GitHub",
       "groups": [
         {"repo": "o/r", "visibility": "public",
          "categories": [
            {"key": "merged_prs", "label": "Merged PRs",
             "items": [{"title": "...", "url": "...", "also_merged": false}]}
          ]}
       ]}
    ]
  }
}
```

`coverage.first_run` is true when there is no usable `last_reviewed` **and**
no recovered cursor; the rendered coverage line then gains
"(first run: last 24 hours)".

`ItemModel` (attention): `id`, `repo`, `title`, `url` (trusted, scheme-guarded
as today), `reasons[]`, `kinds[]` (from state), `type_badges[]` (distinct
badges, precedence order — see *Type badges*), `age` (humanized),
`age_bucket` (new items: always "new"; still-open: today/week/month/older),
`last_updated`, `reopened` (bool). Activity items carry no timestamps: each
is `{title, url, also_merged}`. "Newest-first" applies to attention items
only — activity keeps provider order with alphabetical repos.

**Escaping contract**: the model carries plain text. Attention
`title`/`reasons[]` are stored markdown-escaped in state; `report_model`
converts them to plain text via `unescape_user()` = `html.unescape()` plus
one left-to-right pass removing a backslash immediately preceding a
`_MD_SPECIALS` character. The whitespace collapse applied at store time is
not reversible (cosmetic only; renders identically). Activity titles arrive
raw from providers. Escaping happens only at render time (see *Escaping*).

`coverage.start/end` and `generated_at` carry raw UTC ISO strings; all
formatting is render-side. Empty tiers, buckets and repo groups are omitted
by both renderers; KPI cards always render, including 0.

### Type badges (derived from `kinds[]`, no provider changes)

Items carry `kinds[]`; `type_badges[]` lists the distinct badges in
precedence order: CI > PR review > thread > issue > bug > MP > mention >
comment > stale. Item rows render all badges (or the first badge plus
"+N"). Filter chips and KPI counts match if ANY badge/kind matches.

| kind (`ATTENTION_KINDS`) | badge | accent |
| --- | --- | --- |
| review_requested | PR review | orange #e95420 |
| mention | mention | teal #0f95a1 |
| comment | comment | blue #4c8dff |
| ci_failure | CI | red #e9545b |
| thread_unresolved | thread | purple #a871ff |
| issue_assigned (renders the **bug** badge when `provider == "launchpad"`) | issue | amber #f99b11 |
| stale_pr | stale | grey #8a93a6 |
| lp_mp_comment | MP | orange #e95420, outline |
| lp_mp_needs_review | MP review | orange #e95420, outline variant |
| lp_bug_activity | bug | amber #f99b11 |
| * (unknown or empty) | item | muted #8a93a6 |

GH review requests and LP review requests are both review requests; the MP
badge marks the Launchpad provider taxonomy deliberately.

### Age buckets and humanization

- Bucket assignment (`now = generated_at`, digest host's local TZ),
  evaluated in order, first match wins, all boundaries inclusive:
  **today** = same local calendar date as `now`; **week** =
  `last_updated >= now − 7×24h`; **month** = `>= now − 30×24h`; else
  **older**. New items are not bucketed (they render under "New since last
  review"). Ages and buckets are computed once at digest time and stored;
  renderers use the stored values verbatim and never recompute.
- `humanize_age(ts, now)`: None/unparseable input → "unknown age"
  (bucketed Older, sorted last); "today"/"yesterday" = local calendar-date
  difference 0/1; otherwise, below one year, the largest single unit —
  "3 days ago", "2 weeks ago", "1 month ago" (singular when 1); at or
  above one year, years + remainder months — "1 year, 3 months ago", zero
  remainders dropped ("1 year ago", never "1 year, 0 months ago"). A
  future-dated timestamp renders as "just now". Deterministic fixed
  arithmetic: week = 7 days, month = 30.44 days, year = 365.25 days.
- Sorting: items `last_updated` descending, tie-break `id` ascending,
  unparseable timestamps last; repo groups by newest item descending,
  tie-break repo name ascending; New tier renders above Still open. The
  Recent Activity column keeps provider order with alphabetical repos.

## HTML dashboard (rendered view)

- One self-contained dark file: inline CSS **and** inline JS, no network, no
  external references, no `<img>`, works from `file://`.
- **Palette** (CSS custom properties, dark-tuned Ubuntu accents): page
  `#111318`, cards `#1c1f26`, borders `#2e3340`, text `#e8eaf0`, muted
  `#8a93a6`; accents per the badge table; status banner: amber for NOT YET
  REVIEWED, green for Reviewed.
- **Header**: title + coverage line + status pill (the banner carries the
  `Status:` semantics; front matter never appears in the page). A caption
  notes "ages as of <generated_at, local>" — ages are stored, never
  recomputed at render time.
- **KPI strip**: cards for New, Still open, Assigned, CI failing, Stale
  PRs — clicking one applies the matching filter: New → age = New;
  Still open → age ∈ {Today, 7d, 30d, Older}; Assigned → type = issue;
  CI failing → type = CI; Stale PRs → type = stale. Matching is by ANY
  badge/kind.
- **Body grid**: two columns on wide screens (stacked below ~900px): left
  (approx. 62%) attention; right (approx. 38%) Recent activity + inline
  summaries for CI failures and stale PRs. The summaries are derived at
  render time from attention items whose `kinds` include `ci_failure` /
  `stale_pr`: compact lists of title links with age badges. They are not
  filtered and not counted in the KPI chips.
- **Item rows**: title link (trusted URL only), type badges (all badges,
  or the first + "+N"), age badge, reasons muted, "re-opened" amber
  marker.
- **Interactivity (inline JS, progressive degradation)**:
  - Search: case-insensitive, trimmed substring match over the row's text
    (title, repo, reasons); hides empty groups; live "N of M" count — N
    and M refer to attention items only; the right-column summaries and
    Recent activity are not filtered.
  - Filter chips: OR within a dimension (tier/age, type, repo), AND across
    dimensions and with search. Chip set — tier/age: New, Today, Last 7
    days, Last 30 days, Older; type: one per badge; repo: one per repo
    group. KPI counts reflect the filtered view.
  - Collapsible repo groups via `<details open>`/`<summary>` (collapsing
    works without JavaScript); collapse-all / expand-all control; state
    kept in `sessionStorage` (keys are opaque composites:
    `<generated_at>:<section>:<repo>`; all access wrapped in try/catch
    with an in-memory fallback).
  - The inline JS reads only DOM text and `data-*` attributes; the model
    JSON is never embedded in the page.
  - Without JS: fully expanded, unfiltered page (all sections present).
- **Escaping**: the model carries plain text (see the escaping contract
  under *Report model*); escaping happens only at render time. The
  Markdown renderer applies `escape_user` to plain text; the HTML renderer
  applies `html.escape(s, quote=True)` to text **and** attribute values.
  Repo names are escaped like all user strings. Links only from trusted
  `url` fields (http/https, no whitespace/parens/angle brackets, and no
  quote or backtick — guard regex ``[\s()<>\"'`]``); attribute values are
  quote-escaped; no images; hostile titles cannot inject markup.

## CLI & artifacts

- `digest` prints a **categorized Markdown digest** — same data, same
  organization as the dashboard, in plain text: `## Needs attention` holds
  "New since last review" and the four Still-open buckets (Today / Last 7
  days / Last 30 days / Older); within every section, items are grouped
  under `#### <repo>` headings; each item line carries its title link,
  type tags, one per kind in badge-precedence order (`(PR review)`,
  `(issue)`, `(bug)`, `(MP)`, `(MP review)`, `(mention)`, `(CI)`,
  `(stale)`, `(comment)`, `(thread)`), humanized age (`2 months ago`), and
  "re-opened" marker where applicable, with reason lines beneath. The
  coverage line renders `(first run: last 24 hours)` on first runs. The
  Markdown digest keeps the recovery contract unchanged: `---` front
  matter with `generated_at`/`coverage_start` as raw UTC ISO strings, and
  a single line-anchored line beginning `Status:`. `## Recent activity`
  embeds `activity.markdown` verbatim — byte-identical to `generate_report`
  output. This replaces today's flat item lists; the "byte-identical
  output" guarantee is dropped — the snapshot tests are updated to the new
  structure. `generate` (the standalone activity report) remains unchanged.
- `digest` writes dated `.md` + `.json` always, `.html` when `html` is in
  `digest_formats`. All three artifacts are written atomically (tmp file +
  `os.replace`). Symlinks refresh for `.md`/`.html` as today; `.json` gets
  no symlink (infrastructure).
- Config validation: `digest_formats` values must be a subset of
  {html, md}; a config whose `digest_output` stem's `.json`/`.md`/`.html`
  paths resolve to `state_path` is rejected.
- `read --open` / `digest --open` open `.html` (fallback `.md` as today).
- `read` re-stamps the `.md` `Status:` line; for each target `.md` it then
  loads the sibling `<stem>.json`, sets
  `model["status"] = {"reviewed": true, "reviewed_at": "<UTC ISO>"}`,
  rewrites the `.json` atomically (tmp + `os.replace`) **under the state
  lock**, and re-renders `.html` only if `html` is in `digest_formats`
  (multiple targets are processed independently). The banner text is
  rendered from `model.status` — display format ("Reviewed Mon 28 Sep
  09:14", local time) is renderer-side. An **unusable** `.json` (JSON
  decode error, `schema_version` mismatch, missing required keys — extra
  top-level keys are tolerated for forward compatibility — or a
  `generated_at` differing from the `.md` front matter) → warn and
  restamp the `.md` only. A **missing** `.json` (pre-upgrade digests) →
  legacy fallback: restamp the `.md` and re-convert via `render_html`; the
  fallback's HTML shows the legacy single-column look (the `markdown`
  dependency is retained for this fallback).
- `attention` prints `# Needs attention` plus the categorized body only —
  no front matter, no status/coverage lines (dry run).

## Error handling

- An unusable `.json` during `read` (corrupt, schema mismatch, missing
  required keys — extra top-level keys are tolerated for forward
  compatibility — or `generated_at` divergence — see *CLI & artifacts*):
  warn, restamp `.md` only — never crash, never mutate state beyond the
  cursor. A missing `.json` uses the legacy fallback path.
- `report_model` building with zero items/failed providers renders the same
  empty-state dashboard ("Nothing needs your attention.") with the stale
  warning list — same semantics as the Markdown renderer today.
- Renderer never calls providers; failures are impossible from data fetches
  by construction.

## Testing

- `humanize_age`: exhaustive units (today, yesterday, 3 days, 2 weeks,
  1 month, 2 months, 1 year, 1 year 3 months, future → "just now");
  boundaries (1/2, 6/7, 13/14, 29/30/31, 364/365/366 days, 1 year +
  0/1/11 months); singular forms; zero remainders dropped;
  None/unparseable → "unknown age"; TZ-deterministic (inject `now`;
  local-time buckets tested with pinned TZs).
- `report_model`: bucket boundaries (today/7d/30d/older, first-match
  edges), repo grouping, sort order incl. tie-breaks (`id` ascending, repo
  name ascending) and unparseable timestamps last, KPI counts, type-badge
  mapping (every kind in `ATTENTION_KINDS` maps to a badge; unknown-kind
  fallback badge; multi-kind items), first-run and failed-provider cases;
  unparseable `last_updated` in the model; `attention` stdout shape
  (`# Needs attention` + body only); `digest` stdout with front matter
  stripped.
- Escaping: round-trip `escape_user` → `unescape_user` → `escape_user`
  visual identity; a title containing `&`, `_`, `(` renders identically in
  both renderers; a URL containing `"` cannot break out of an attribute;
  hostile-title and hostile-repo-name injection tests.
- Renderer (from model): dark CSS present, badges, no external refs, no
  `<img`, `fetch`/`XMLHttpRequest` absent, `.json`->HTML byte-identical to
  direct render; JS verified via static DOM-contract tests (`data-*`
  attributes, structure, no `<script src>`; no browser tests);
  `sessionStorage` failure falls back to in-memory; empty tiers/buckets/
  groups omitted.
- Artifacts: `.json` always written (incl. when html is disabled) with no
  symlink; atomic-write behaviour; `read` with multiple targets; `read`
   with html disabled (no `.html` created/updated); each unusable-`.json`
   condition (decode error, schema mismatch, missing keys,
   `generated_at` divergence) → warn + `.md`-only restamp, while extra
   top-level keys are tolerated (they take the normal reviewed path);
   pre-upgrade
  missing-`.json` fallback renders the legacy single-column HTML; config
  validation (bad `digest_formats` value; `digest_output`/`state_path`
  collision).
- `read` round-trip: generate -> read re-renders HTML with only the status
  banner changed.
- Markdown renderer (from model): repo groups, four buckets, humanized
  ages, type tags, re-opened markers, coverage/status/front-matter lines;
  recovery regression: `_recover_last_reviewed_candidate` works on the new
  Markdown; activity section byte-identical to `generate_report` output.
- Existing tests that hard-code three buckets (`BUCKETS` in
  `attention.py` and its tests) are updated to the four-bucket model.

## Docs

- README: the `digest_formats` / artifact rows gain `.json` (always
  written, no symlink); a note that the HTML digest is a dark JavaScript
  dashboard; a note that the digest Markdown is now categorized.
- Parent-spec pointers per *Amendments to the parent spec*.

## Future seams

- Light theme via CSS custom properties (all colours are variables).
- Email/Slack renderers consume the same report model / `.json`.
- Persisted filter preferences (localStorage) beyond sessionStorage.