# HTML Dashboard Digest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the digest's HTML output into a self-contained dark dashboard (KPI strip, two-column body, collapsible repo groups, type badges, humanized ages, search + filter chips) fed by a structured report model, and give the Markdown digest the same categorization in plain text.

**Architecture:** `attention.py` gains `unescape_user`, `humanize_age`, an extended URL guard, and `report_model()` — the serializable model both renderers consume. `html_report.py` is rewritten as the dashboard renderer (dark CSS + inline JS, HTML-escaped at render). `main.py` writes `.md` + `.json` (model snapshot, always) + `.html` atomically; `read` re-renders from the cached `.json`. CLI Markdown becomes categorized (repo groups, 4 buckets, type tags, ages).

**Tech Stack:** Python 3.11+, pydantic v2, click, pytest; vanilla inline JS/CSS in the rendered HTML (no new runtime dependencies).

## Global Constraints

- Spec of record: `docs/superpowers/specs/2026-09-30-html-dashboard-design.md` (+ parent `2026-09-28-attention-digest-design.md`; the dashboard spec's "Amendments" section governs conflicts). Read before starting.
- All timestamps UTC ISO at rest; ages/buckets computed once at digest time in the digest host's local TZ (`now = generated_at`); renderers use stored values verbatim.
- Model strings are PLAIN TEXT: attention `title`/`reasons[]` converted via `unescape_user()`; activity titles are raw. Escaping happens only at render (Markdown: `escape_user`; HTML: `html.escape(s, quote=True)`, attributes included).
- URL guard: `re.compile(r"[\s()<>\"'`]")` — links only from trusted `url` fields, http/https only, never images.
- Badge precedence: CI > PR review > thread > issue > bug > MP > mention > comment > stale; `issue_assigned` + `provider == "launchpad"` → bug badge; unknown/empty kind → "item" (muted #8a93a6).
- Buckets evaluated in order, first match wins: today = same local calendar date; week = `>= now − 7×24h`; month = `>= now − 30×24h`; else older. New items are not bucketed.
- `humanize_age`: None/unparseable → "unknown age"; future → "just now"; today/yesterday = local calendar-date delta 0/1; else floor units (day <14d, week <60d, month <365.25d; year+months at/above a year), singular when 1, zero remainders dropped.
- Sort: items `last_updated` desc, tie-break `id` asc, unparseable last; groups by newest item desc then repo name asc. New tier above Still open. Empty tiers/buckets/groups omitted; KPI cards always render (including 0).
- Artifacts: dated `.md` + `.json` always written, `.html` only when `html` in `digest_formats`; ALL written atomically (tmp + `os.replace`); `.json` gets no symlink; `schema_version: 1` in the model.
- Config validation: `digest_formats` ⊆ {html, md}; reject a config whose `digest_output` stem's `.json`/`.md`/`.html` resolves to `state_path`.
- `read`: per target `.md`, restamp + update sibling `.json` status atomically under the state lock + re-render `.html` only if enabled. Unusable `.json` (decode error / schema mismatch / generated_at mismatch) → warn, restamp `.md` only. Missing `.json` (pre-upgrade) → legacy fallback: re-render via `render_html(restamped_md)`.
- The Markdown recovery contract is unchanged: `---` front matter with `generated_at`/`coverage_start` raw UTC ISO; single line-anchored `^Status:` line.
- Line length 100; gates are `poetry run pytest -q` (192 passing at plan time — grows per task), `poetry run ruff check src tests` (0 errors), `poetry run black --check src tests`. Commit after every task.
- Worktree: `.worktrees/html-dashboard`, branch `html-dashboard`.

---

### Task 1: `unescape_user`, URL-guard extension, `humanize_age`

**Files:**
- Modify: `src/gitreport/providers/base.py` (add `unescape_user` next to `escape_user`)
- Modify: `src/gitreport/attention.py` (extend `_URL_UNSAFE`; add `humanize_age`)
- Test: `tests/test_providers_base.py`, `tests/test_attention.py`

**Interfaces:**
- Produces: `unescape_user(text: str) -> str` (base.py); `humanize_age(ts: str | None, now: datetime) -> str` (attention.py); `_URL_UNSAFE = re.compile(r"[\s()<>\"'`]")`.

- [ ] **Step 1: Failing tests**

Append to `tests/test_providers_base.py`:

```python
def test_unescape_user_reverses_escape_user():
    from gitreport.providers.base import escape_user

    for raw in ["Fix login crash", "Bug & <b>bold</b> [x](y) *em_", "a\\b", "C:\\path"]:
        assert unescape_user(escape_user(raw)).replace("\\", "") == raw.replace("\\", "")


def test_unescape_user_strips_backslash_before_specials():
    assert unescape_user("fix\\_login \\- ok") == "fix_login - ok"


def test_unescape_user_plain_unchanged():
    assert unescape_user("plain text") == "plain text"
```

(Add `unescape_user` to the import line.) Append to `tests/test_attention.py`:

```python
def test_humanize_age_units():
    from gitreport.attention import humanize_age

    now = datetime.fromisoformat(NOW)  # 2026-09-28T06:00:00+00:00
    cases = {
        "2026-09-28T05:00:00+00:00": "today",
        "2026-09-27T10:00:00+00:00": "yesterday",
        "2026-09-25T06:00:00+00:00": "3 days ago",
        "2026-09-14T06:00:00+00:00": "2 weeks ago",
        "2026-08-01T06:00:00+00:00": "1 month ago",
        "2026-05-01T06:00:00+00:00": "4 months ago",
        "2025-06-01T06:00:00+00:00": "1 year, 3 months ago",
    }
    for ts, expected in cases.items():
        assert humanize_age(ts, now) == expected, ts


def test_humanize_age_edges():
    from gitreport.attention import humanize_age

    now = datetime.fromisoformat(NOW)
    assert humanize_age(None, now) == "unknown age"
    assert humanize_age("garbage", now) == "unknown age"
    assert humanize_age("2026-09-29T06:00:00+00:00", now) == "just now"  # future
    # 1 year exactly -> no zero remainder
    assert humanize_age("2025-09-28T06:00:00+00:00", now) == "1 year ago"
```

- [ ] **Step 2: Verify red** — `poetry run pytest tests/test_providers_base.py tests/test_attention.py -q -k "unescape or humanize"` → FAIL (ImportError).

- [ ] **Step 3: Implement**

In `providers/base.py`, after `escape_user`:

```python
def unescape_user(text: str) -> str:
    """Approximate inverse of escape_user, for plain-display text.

    Removes a backslash immediately preceding a Markdown-metacharacter (one
    left-to-right pass), then resolves HTML entities. The whitespace
    collapse escape_user performs is not reversible (cosmetic only).
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and text[i + 1] in _MD_SPECIALS:
            i += 1
            out.append(text[i])
        else:
            out.append(ch)
        i += 1
    return _html.unescape("".join(out))
```

In `attention.py`: change `_URL_UNSAFE` to `re.compile(r"[\s()<>\"'`]")` and add:

```python
def humanize_age(ts: str | None, now: datetime) -> str:
    """Humanized age as of `now`: today/yesterday (local calendar dates),
    then largest single unit below one year (day/week/month), then
    years + remainder months. Unknown/future handled per spec."""
    dt = _parse(ts)
    if dt is None:
        return "unknown age"
    if dt > now:
        return "just now"
    local_dt, local_now = dt.astimezone(), now.astimezone()
    day_delta = (local_now.date() - local_dt.date()).days
    if day_delta <= 0:
        return "today"
    if day_delta == 1:
        return "yesterday"
    days = (local_now - local_dt).total_seconds() / 86400
    if days < 14:
        n = int(days)
        unit = "day"
    elif days < 60:
        n = int(days // 7)
        unit = "week"
    elif days < 365.25:
        n = int(days // 30.44)
        unit = "month"
    else:
        years = int(days // 365.25)
        months = int((days - years * 365.25) // 30.44)
        y = f"{years} year{'' if years == 1 else 's'}"
        if months == 0:
            return f"{y} ago"
        return f"{y}, {months} month{'' if months == 1 else 's'} ago"
    return f"{n} {unit}{'' if n == 1 else 's'} ago"
```

- [ ] **Step 4: Verify green** — both test files pass; full suite green.
- [ ] **Step 5: Commit** — `feat: unescape_user, extended url guard, humanize_age`

---

### Task 2: `report_model` — attention side (badges, buckets, ages, KPIs)

**Files:**
- Modify: `src/gitreport/attention.py`
- Test: `tests/test_attention.py`

**Interfaces:**
- Consumes: `AttentionState`, `humanize_age`, `_parse`, existing `dedupe`-free state items (`kinds`, `provider`, `reopen_count`, `first_seen`, `last_updated`).
- Produces:
  - `BADGE_BY_KIND: dict[str, tuple[str, str]]` and `badges_for(kinds: list[str], provider: str | None) -> list[tuple[str, str]]` (precedence-ordered, deduped, unknown → `("item", "item")`).
  - `BUCKETS = ("today", "week", "month", "older")`, `BUCKET_TITLES` extended with `month: "Last 30 days"`.
  - `age_bucket(last_updated: str | None, now: datetime) -> str` (in-order first-match per spec).
  - `report_model(state: AttentionState, *, generated_at: str, coverage_start: str, first_run: bool, stale_providers: list[str], activity_data: dict | None = None) -> dict` returning the spec's model dict (attention fully populated; `activity` populated in Task 3 — for now `{"markdown": "", "providers": []}` when `activity_data is None`).
  - Model `status` = `{"reviewed": False, "reviewed_at": None}` (digest-time default; `read` mutates the cached copy).

- [ ] **Step 1: Failing tests** (append to `tests/test_attention.py`; seed state via the existing `rec()`/`state_with()` helpers, wrapped in `AttentionState.model_validate`)

```python
def _model_state(items):
    from gitreport.state import AttentionState

    return AttentionState.model_validate(state_with(items=items))


def test_report_model_groups_and_buckets():
    from gitreport.attention import report_model

    state = _model_state({
        "gh:1": rec(title="A", repo="o/r", kinds=["ci_failure", "stale_pr"],
                    provider="github",
                    first_seen="2026-09-28T06:00:00+00:00",
                    last_updated=datetime.now(UTC).isoformat()),
        "gh:2": rec(title="B", repo="o/r", kinds=["issue_assigned"], provider="github",
                    first_seen="2026-09-01T06:00:00+00:00",
                    last_updated="2026-09-01T06:00:00+00:00"),
        "lp:1": rec(title="C", repo="proj", kinds=["issue_assigned"], provider="launchpad",
                    url="https://launchpad.net/bugs/1",
                    first_seen="2026-08-01T06:00:00+00:00",
                    last_updated="2026-08-01T06:00:00+00:00"),
    })
    now = datetime.now(UTC)
    model = report_model(state, generated_at=now.isoformat(),
                         coverage_start="2026-09-27T06:00:00+00:00",
                         first_run=False, stale_providers=[])
    # New tier: gh:1 only (first_seen today, last_reviewed unset -> all new
    # would make gh:2 new too; set last_reviewed to split them instead)
```

Adjust: set `state_with(last_reviewed="2026-09-20T06:00:00+00:00", items={...})` so gh:1 (first_seen 09-28) is New; gh:2 (09-01) and lp:1 (08-01) are Still open. Assert: `model["kpi"] == {"new": 1, "still_open": 2, "assigned": 2, "ci_failing": 1, "stale_prs": 1}`; `model["attention"]["new"]["groups"]` has one group repo `"o/r"` whose item badges (in precedence order) are `[("CI","ci"), ("stale","stale")]`; bucket "week" contains gh:2 with badge `[("issue","issue")]`; bucket "older" contains lp:1 with badge `[("bug","bug")]` (LP provider override); ages are humanized strings; groups sorted; items within groups newest-first.

Plus tests: `test_badges_for_precedence_and_lp_bug` (multi-kind order, unknown kind → item, LP override); `test_age_bucket_first_match` (same-local-date → today even if >24h old; exactly now−7d → week; now−30d → month); `test_report_model_unparseable_last_updated` (goes to "older", age "unknown age", sorted last); `test_report_model_first_run` (no last_reviewed, first_run=True → all open items in New tier, coverage line suffix data present via `model["coverage"]["first_run"] is True`).

- [ ] **Step 2: Verify red.**
- [ ] **Step 3: Implement** `badges_for`, `age_bucket`, extend `BUCKETS`/`BUCKET_TITLES`, and `report_model` per the Interfaces block. Implementation sketch (complete):

```python
BADGE_BY_KIND = {
    "ci_failure": ("CI", "ci"),
    "review_requested": ("PR review", "pr"),
    "thread_unresolved": ("thread", "thread"),
    "issue_assigned": ("issue", "issue"),
    "lp_bug_activity": ("bug", "bug"),
    "lp_mp_comment": ("MP", "mp"),
    "lp_mp_needs_review": ("MP review", "mp"),
    "stale_pr": ("stale", "stale"),
    "mention": ("mention", "mention"),
    "comment": ("comment", "comment"),
}
BADGE_PRECEDENCE = ["ci", "pr", "thread", "issue", "bug", "mp", "mention", "comment", "stale"]


def badges_for(kinds, provider=None):
    out = []
    for kind in kinds or []:
        if kind == "issue_assigned" and provider == "launchpad":
            badge = ("bug", "bug")
        else:
            badge = BADGE_BY_KIND.get(kind, ("item", "item"))
        if badge not in out:
            out.append(badge)
    out.sort(key=lambda b: BADGE_PRECEDENCE.index(b[1]))
    return out


def age_bucket(last_updated, now):
    dt = _parse(last_updated)
    if dt is None:
        return "older"
    local_dt, local_now = dt.astimezone(), now.astimezone()
    if local_dt.date() == local_now.date():
        return "today"
    if local_dt >= local_now - timedelta(hours=168):
        return "week"
    if local_dt >= local_now - timedelta(hours=720):
        return "month"
    return "older"
```

`report_model` builds `_item_entry(mid, r, now)` (plain-text title/reasons via `unescape_user`, url via the existing guard helper, badges, age, bucket, reopened), splits open items into New (`first_seen > last_reviewed` per current rules, or `first_run`) vs Still open, groups each by repo (sort rules per spec), computes KPI counts, and assembles the model dict with `schema_version: 1`, `coverage` (incl. `first_run`), `status`, `stale_providers`, `activity` placeholder.

- [ ] **Step 4: Verify green** (existing `build_report`/render tests still pass — `build_report` stays for now and is replaced in Task 4).
- [ ] **Step 5: Commit** — `feat: report_model with badges, buckets, ages, kpis`

---

### Task 3: Activity model (nested providers/groups/categories)

**Files:**
- Modify: `src/gitreport/attention.py`
- Test: `tests/test_attention.py`

**Interfaces:**
- Produces: `activity_model(activity_data: dict) -> dict` returning `{"providers": [{"provider", "label", "groups": [{"repo", "visibility", "categories": [{"key", "label", "items": [{"title", "url", "also_merged"}]}]}]}]}` — provider order as inserted, repos alphabetical, categories in `ACTIVITY_CATEGORIES` order, raw titles. `report_model(..., activity_data=...)` also sets `model["activity"]["markdown"] = generate_report(activity_data)` (import from `.reporting` inside the function to avoid a cycle) — when `activity_data is None`, `markdown` stays `""`.

- [ ] **Step 1: Failing test**

```python
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
    assert cats["prs_submitted"]["items"] == [{"title": "T", "url": "https://x/1", "also_merged": False}]
```

(Add `empty_repo_activity` import.) Second test: categories with no items are omitted.

- [ ] **Step 2: Verify red.**
- [ ] **Step 3: Implement** using `ACTIVITY_CATEGORIES` + `PROVIDER_LABELS`/`PROVIDER_DISPLAY_NAMES` from `.reporting` (module-level import is fine: reporting imports providers.base only).
- [ ] **Step 4: Verify green.**
- [ ] **Step 5: Commit** — `feat: nested activity model`

---

### Task 4: Markdown renderer consumes the model (categorized .md)

**Files:**
- Modify: `src/gitreport/attention.py` (`render_digest_markdown`, `render_attention_stdout`)
- Modify: `tests/test_attention.py` (rewrite renderer tests; migrate `BUCKETS` 3→4)

**Interfaces:**
- Consumes: `report_model` output + `activity_markdown` string.
- Produces: `render_digest_markdown(model: dict, activity_markdown: str) -> str`; `render_attention_stdout(model: dict, stale_providers: list[str], now: datetime) -> str` (unchanged signature, model-based). Front matter/coverage/`Status:` line formats byte-compatible with today (recovery contract). Structure: `## Needs attention` → `### New since last review` → `#### <repo>` groups → items `- [title](url) (badge, age, re-opened)` + reason lines; then `### Still open — <bucket label>` per non-empty bucket with repo groups; then `## Recent activity` = `activity_markdown` verbatim. Empty tiers/buckets/groups omitted; empty-state + stale warnings as today.

- [ ] **Step 1: Rewrite/extend renderer tests**: structure assertions (front matter keys, `Status:` sentinel, `#### o/r` groups present under New and under each populated bucket, item suffix `(PR review, 2 days ago)` pattern, reason sub-lines, empty-bucket omission, `_Nothing needs your attention._` empty state, stale warnings, first-run coverage suffix). Update the three-bucket tests (`BUCKETS` migration: "week"→week+month split).
- [ ] **Step 2: Verify red** (new assertions fail against current renderer).
- [ ] **Step 3: Implement**: `_render_item_line(entry)` (title link via existing url guard, `(badge1, age, re-opened)` suffix from model fields, reasons as indented lines); `_render_groups(groups)`; rebuild `render_digest_markdown(model, activity_markdown)` around the model; `render_attention_stdout(model, stale_providers, now)` renders New + buckets only.
- [ ] **Step 4: Verify green** — full suite green (main.py still calls the old signature → if it breaks, temporarily adapt the call site in this task; full wiring lands in Task 7).
- [ ] **Step 5: Commit** — `feat: categorized markdown digest renderer`

---

### Task 5: Dashboard HTML renderer (dark theme, from model, no JS yet)

**Files:**
- Modify: `src/gitreport/html_report.py` (rewrite: keep `strip_front_matter`, `replace_status_line`, legacy `render_html`; add `escape_html`, `atomic_write_text`, `render_dashboard_html(model) -> str`)
- Test: `tests/test_html_report.py`

**Interfaces:**
- Consumes: the report model.
- Produces: `render_dashboard_html(model: dict) -> str`; `atomic_write_text(path: Path, text: str) -> str`-style helper (tmp + `os.replace`); `escape_html = lambda s: html.escape(s, quote=True)`.

- [ ] **Step 1: Failing tests** — dark CSS markers (`--gr-bg`/`#111318` present), no external refs (`src=`, `href="http` only in item links), no `<img`, hostile-title injection (`<script>`, `javascript:`, `"` in url) renders inert, badges rendered with `data-type`, `data-age`/`data-repo`/`data-text` attributes present for JS (Task 6 contract), `<details open>` repo groups, KPI cards present incl. zero, status pill amber/green by `model["status"]`, empty-state, stale warning list, coverage line text, `schema_version` tolerated.
- [ ] **Step 2: Verify red.**
- [ ] **Step 3: Implement** `render_dashboard_html`:
  - `_CSS`: dark custom properties (`--gr-bg:#111318; --gr-card:#1c1f26; --gr-border:#2e3340; --gr-text:#e8eaf0; --gr-muted:#8a93a6; --gr-brand:#e95420; --gr-accent:#0f95a1; --gr-pos:#3fb54a; --gr-caution:#f99b11; --gr-neg:#e9545b; --gr-info:#4c8dff; --gr-purple:#a871ff; --gr-grey:#8a93a6`), system-ui font, `.gr-kpis` grid, `.gr-cols` two-column grid (stacked <900px), `.gr-card` sections, `.gr-badge` pills with per-type modifier classes, `.gr-item` rows, `details.repo > summary` headers.
  - Body: header (title, coverage line — incl. first-run suffix, "ages as of" caption, status pill), KPI strip (5 cards from `model["kpi"]`), left column: search input (decorative until Task 6), chip row (rendered, `disabled` logic in Task 6), New section, four bucket sections — each with `<details class="repo" open data-repo="..." data-age="...">` per repo; right column: CI/stale summaries (from attention items with those kinds) + Recent activity from `model["activity"]["providers"]` (repo groups, category labels, items).
  - All dynamic text through `escape_html`; urls through the same guard (re-check scheme + unsafe chars; attribute-escaped).
- [ ] **Step 4: Verify green.**
- [ ] **Step 5: Commit** — `feat: dark dashboard html renderer (static)`

---

### Task 6: Inline JS interactions + DOM contract tests

**Files:**
- Modify: `src/gitreport/html_report.py` (append `<script>` to the template; chips become active)
- Test: `tests/test_html_report.py`

**Interfaces:**
- Produces: inline JS implementing — search (case-insensitive trimmed substring over `data-text`), chips (OR within a dimension, AND across dimensions + search), live "N of M" attention count, collapsibles persisted to `sessionStorage` under `<generated_at>:<section>:<repo>` with in-memory fallback, collapse-all/expand-all buttons. Rows carry `data-type` (badge keys), `data-age` (bucket key or "new"), `data-repo`, `data-text` (title+repo+reasons, escaped).

- [ ] **Step 1: Failing DOM-contract tests** (static, no browser): script tag present exactly once and has no `src`; JS source contains `addEventListener`, `sessionStorage` inside a try/catch (assert `try` near `sessionStorage`), no `fetch(`/`XMLHttpRequest`; rows have `data-type`/`data-age`/`data-repo`/`data-text`; chips carry `data-dim`/`data-val`; KPI cards carry `data-kpi`; count element `#gr-count` present.
- [ ] **Step 2: Verify red.**
- [ ] **Step 3: Implement** the `<script>` block (vanilla): read rows once into an array; `applyFilters()` = for each row, visible iff (no search OR `data-text` contains query) AND (no type chips selected OR row's `data-type` list intersects) AND (no age chips OR `data-age` intersects) — then hide empty `<details>` groups and sections, update `#gr-count`; chip click toggles `aria-pressed`; KPI click maps to the chip set per the spec's KPI→filter table; storage helpers `loadState`/`saveState` with try/catch; wire `toggle` events on `<details>` to persist.
- [ ] **Step 4: Verify green** (full suite).
- [ ] **Step 5: Commit** — `feat: dashboard interactions (search, chips, collapsibles)`

---

### Task 7: main.py wiring — artifacts, read re-render, config validation

**Files:**
- Modify: `src/gitreport/main.py`, `src/gitreport/config.py`
- Test: `tests/test_main.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: everything above.
- Produces: digest writes `.md`/`.json`/`.html` via `atomic_write_text` (`.json` = `json.dumps(model)`); `read` per-target `.json` update + re-render with legacy fallback; config validators per spec S19.

- [ ] **Step 1: Failing tests** — digest writes all three artifacts atomically and `.json` parses to the model (`schema_version == 1`, status unreviewed) with `html` disabled too; no `latest.json` symlink; `read` (single + multiple targets): `.md` restamped, `.json` status flipped, `.html` re-rendered with Reviewed pill — and byte-diff vs pre-read HTML is confined to the banner; `read` with missing `.json` → legacy `render_html` fallback used; corrupt `.json` (bad schema_version) → warning + `.md`-only; config: `digest_formats: [html, md, json]` rejected; digest stem colliding with `state_path` rejected; digest stdout contains `#### <repo>` groups and `(PR review`-style tags.
- [ ] **Step 2: Verify red.**
- [ ] **Step 3: Implement.** digest: build model via `report_model(...)` (+ `activity_model`), `md_text = render_digest_markdown(model, generate_report(activity_data))`, write three artifacts atomically, symlinks as today; `attention` uses `report_model` without activity + `render_attention_stdout(model, ...)`. `read`: under the lock, per target — restamp `.md`; sibling `.json` missing → legacy fallback render; present → validate (`schema_version`, `generated_at` match), set `status`, atomic-write, re-render `.html` if enabled; any validation failure → warn + `.md`-only. config.py: the two validators.
- [ ] **Step 4: Verify green** — full suite + ruff + black.
- [ ] **Step 5: Commit** — `feat: wire dashboard artifacts, read re-render, config guards`

---

### Task 8: Docs + final verification

**Files:** Modify `README.md`, `docs/superpowers/specs/2026-09-28-attention-digest-design.md` (insert pointer lines at the amended paragraphs per the dashboard spec's Amendments section).

- [ ] **Step 1:** README — HTML digest description (dark dashboard, filters, badges, `.json` artifact row in the files list), Markdown digest structure note; spec pointer edits (`<!-- Amended by 2026-09-30-html-dashboard-design -->` comments + one-line notes at the five amendment sites).
- [ ] **Step 2:** Full gates: `poetry run pytest -q`, `poetry run ruff check src tests`, `poetry run black --check src tests` — all green.
- [ ] **Step 3:** Manual smoke: `poetry run gitreport digest && poetry run gitreport read --open` against a scratch `GITREPORT`-config; eyeball the dashboard.
- [ ] **Step 4: Commit** — `docs: dashboard digest docs and spec amendment pointers`
