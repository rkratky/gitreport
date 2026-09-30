# GitReport Attention Digest — Design

Date: 2026-09-28
Revision: 3 — final-review fixes applied
Status: Approved (pending spec review)

## Background

GitReport today has one command, `gitreport generate`, which reports the user's
own activity (PRs/MPs submitted, reviewed, merged; issues/bugs created and
closed) from GitHub and Launchpad over a date range, rendered as Markdown on
stdout. It answers "what did I do?".

This design adds a second question the tool must answer every morning:
"what needs my attention?" — mentions, review requests, new comments on my
PRs, blocked or assigned work — with rules that keep unactioned items visible
without letting years-old quiet threads resurface.

## Goals

1. A daily digest listing everything needing attention, followed by the
   user's own activity since `last_reviewed` (24h fallback on first run).
2. Correct handling of the freshness/attention split: items leave the inbox
   when acted upon (auto-detected or manually acked), never by aging alone;
   quiet-but-irrelevant old threads do not reappear.
3. GitHub seen-state comes from GitHub's notifications API. Heuristics and
   manual acks fill the gaps (Launchpad, query-derived items).
4. Consumption via CLI Markdown plus a scheduled self-contained HTML digest.

## Non-goals (v1)

- Email or Slack delivery (renderers stay pluggable; add later).
- Security alerts (Dependabot etc.).
- A hosted dashboard or GitHub Pages.
- Any change to the existing `generate` behaviour or output (including its
  abort-on-bad-credentials behaviour).
- Note: activity titles are Markdown-escaped on the digest path; the
  standalone `generate` stdout also renders escaped titles (security fix
  accepted as a deliberate, documented deviation).

## Decisions made

| Question | Decision |
| --- | --- |
| Consumption | CLI Markdown + scheduled HTML digest file; email/Slack later |
| Ack model | GH notifications API authoritative for GH seen-state (fetched in full every run; never bulk-marked read); `ack` on a notification item marks that thread read on GitHub (best-effort); heuristics + manual ack for LP and query-derived items; thin local state as fallback/persistence |
| v1 item set | Core inbox (mentions, review requests, comments), blocked/assigned items (unresolved threads, CI failures, assigned issues, LP bug assignments), stale own PRs |
| Scope | Everything the user is involved in, minus a per-provider exclusion list |
| Digest shape | Needs Attention on top + Activity-since-`last_reviewed` below (24h fallback on first run) |
| Item lifecycle | One `status` field (`open` / `acked` / `resolved`); one reopen rule for every provider; the user's own activity never creates or refreshes an item; an item resolves only when every origin has cleared |
| Un-ack | Supported; re-opened items return to Still open, never reappear as New; un-acking a resolved item pins it open until acked again |
| Retention | Acked/resolved items pruned 30 days after the source stopped reporting them; still-matching acked items are kept as suppression records |
| Provider failure | Failed or partial fetch ⇒ no absence-based resolution for that provider in that run; its items carry over, section marked stale |
| Missed periods | One catch-up digest covers the whole gap; all windows are anchored to state, not the calendar |
| Latest-digest convenience | Every digest run refreshes the `latest.html` and `latest.md` symlinks (inside the state lock); no extra cron entry |
| Consumption clock | `last_reviewed` cursor advanced only by `gitreport read`, to the generation timestamp of the newest digest being marked read (not to "now"); all coverage windows anchor to it |
| Digest formats | `.md` always written (render cache for `read`); `digest_formats` (default `[html, md]`) controls published artifacts; `md` cannot be disabled |
| Concurrency | Every state mutation holds an exclusive `flock` on `<state>.lock` for its load–modify–write cycle |

## Architecture

```
src/gitreport/
  main.py             # click CLI: generate (existing), attention, digest, read, ack, unack
  config.py           # extended with optional `attention:` section (pydantic default)
  state.py            # NEW: inbox state store (JSON, atomic write, flock)
  attention.py        # NEW: merge provider items + state -> attention report model
  reporting.py        # existing Markdown activity renderer (unchanged)
  html_report.py      # NEW: self-contained HTML digest renderer
  providers/
    base.py           # GitProvider protocol gains get_attention(); AttentionItem model
    github.py         # notifications API + search/GraphQL queries
    launchpad.py      # person-scoped queries
```

`digest` flow:

1. Load config and a snapshot of state. Take the **generation timestamp**
   (`now`, UTC) immediately before fetching; it becomes the digest's
   `generated_at`, every new item's `first_seen`, and `last_digest_run`.
2. For each provider, fetch attention items and the activity window (since
   `last_reviewed`; 24h fallback on a first-ever run). Fetching happens
   outside the lock; a concurrent `read` can only move `last_reviewed`
   forward to an older digest's timestamp, so nothing is lost.
3. Deduplicate items by `id`: `origins`, `kinds` and `reasons` are unioned;
   notification origin supplies seen-state, query origin supplies resolution
   checks.
4. Take the exclusive state lock, reload state, and merge: new items get
   `first_seen = generated_at`; existing items apply the reopen and
   self-activity rules (see "Item lifecycle"); auto-resolution runs only for
   providers whose fetch fully succeeded; prune per the retention rule.
5. Write state atomically (`last_digest_run = generated_at`; never
   `last_reviewed` — see "Review gaps and consumption").
6. Render: Markdown to stdout; write the dated `.md` (always) and `.html`
   (if enabled); refresh the `latest.*` symlinks; release the lock.

Providers stay behind the existing `GitProvider` protocol; the protocol gains
one method, `get_attention(since, exclusions)` (the username stays a
constructor argument), returning a list of `AttentionItem`s.

## Data model

### AttentionItem

- `id`: provider + stable URL, e.g. `gh:https://github.com/org/repo/pull/123`
  or `lp:https://launchpad.net/bugs/456`.
- `kind`: `review_requested`, `mention`, `comment`, `ci_failure`,
  `thread_unresolved`, `issue_assigned`, `stale_pr`, `lp_mp_comment`,
  `lp_bug_activity`, `lp_mp_needs_review`.
- `origin`: `notification` (from the notifications API) or `query`
  (search/person-scoped).
- `provider`, `repo` (owner/name or project), `title`, `url`, `reason`
  (human-readable string), `updated_at` (latest event by someone other than
  the user).

Provider results are per-origin; after deduplication a state item carries the
union as `origins`, `kinds`, `reasons`.

### State store

Path: `~/.local/state/gitreport/state.json` (XDG state dir; configurable).
All stored timestamps are UTC.

```json
{
  "version": 1,
  "last_digest_run": "2026-09-28T06:00:00Z",
  "last_reviewed": "2026-09-27T06:00:00Z",
  "reviewed_at": "2026-09-27T09:14:00Z",
  "items": {
    "gh:https://github.com/org/repo/pull/123": {
      "status": "open",
      "origins": ["notification", "query"],
      "kinds": ["comment", "thread_unresolved"],
      "reasons": ["New comment by alice", "1 unresolved review thread"],
      "first_seen": "2026-09-27T06:00:00Z",
      "last_updated": "2026-09-27T18:02:11Z",
      "acked": false,
      "acked_at": null,
      "resolved_at": null,
      "pinned": false,
      "reopen_count": 0
    }
  }
}
```

- `status`: `open` (in the inbox), `acked` (user acted), `resolved`
  (auto-resolved while unacked). `acked` mirrors `status = acked`.
- `resolved_at`: when every origin last stopped reporting the item; `null`
  while any origin still reports it. Set for both `acked` and `resolved`
  items; cleared if a source reports the item again.
- `pinned`: set by un-acking a resolved item; cleared on ack.
- `last_updated`: the latest event by someone other than the user.
- Writes are atomic (tmp file + rename) and happen under the state lock
  (see "Concurrency").
- **Retention**: an item with `status` `acked` or `resolved` is pruned when
  `resolved_at` is older than 30 days. While any source still reports it,
  it is retained as a suppression record, so an acked-but-still-matching
  item never re-enters the inbox as New. Consequence: closed/resolved
  sources eventually prune; permanently-matching acked items (e.g. a
  long-open assigned issue) persist.
- `last_reviewed` is the consumption cursor: the generation timestamp of the
  newest digest the user has marked read. Only `gitreport read` advances it.
  `reviewed_at` is the wall-clock time of that `read`, used only for the
  status line.
- **First-ever run** (`last_reviewed` unset): activity window = last 24h;
  every item in the run is New; LP `modified_since` = last 24h. `read` with
  `last_reviewed` unset marks only the newest digest consumed.

## Item lifecycle

Two axes: **freshness** (`last_updated` — when someone else last moved the
thread) and **attention** (`status` — whether the user has acted). The report
never drops an open item because of age, and never resurfaces an acked item
because it is fresh unless someone else acts on it again.

### Enter

An item is created when an attention event by someone other than the user
occurs:

- GitHub: an unread notification whose reason maps to a kind (see "GitHub
  specifics"); or a query match (unresolved review thread, failing checks on
  own PR, assigned issue, own PR stale for >= `stale_pr_days`).
- Launchpad: new comment/vote on a merge proposal of mine or one requesting
  my review, since `last_reviewed` (24h fallback on first run); a bug
  assigned to me (open tasks, no time window); new activity (comment, status
  change, new duplicate) on a bug I am subscribed to, since `last_reviewed`
  (24h fallback on first run).

New items get `first_seen = generated_at` and `status = open`.
`gitreport attention` treats items not yet in state as New with an
in-memory `first_seen = now` that is never persisted.

**Self-activity rule** (all providers): activity authored by the user never
creates or refreshes an item; `last_updated` tracks the latest event by
someone else.

**Reopen rule** (all providers): a new attention event by someone other than
the user on an `acked` or `resolved` item sets `status = open`, clears
`acked`/`acked_at`/`resolved_at`, increments `reopen_count`, and updates
`last_updated`. `first_seen` is unchanged. A pinned item is already open and
simply stays open. An event is new when its `updated_at` is later than the
item's `last_updated` (a still-present, unchanged notification or a
persisting query match is not a new event).

### Show

- **New since last review**: `status = open` and `first_seen` after the
  current `last_reviewed` (see "Review gaps and consumption").
- **Still open**: `status = open` and not New, bucketed by `last_updated`:
  today / last 7 days / older than 7 days. Buckets are computed and displayed
  in local time. Un-acked and reopened items are marked "re-opened" here
  (the marker is `reopen_count > 0`); they keep their original `first_seen`.

<!-- Amended 2026-09-30: see docs/superpowers/specs/2026-09-30-html-dashboard-design.md -->
Amended 2026-09-30: still-open buckets are Today / Last 7 days / Last 30 days / Older, superseding the three-bucket wording above.

### Leave

An item leaves the inbox on ack, or when auto-resolution sets
`status = resolved` and `resolved_at` (never deletion, never `acked`).
Auto-resolution is **origin-aware**: an item resolves only when **every**
origin that reported it has cleared. Notification-origin disappearance never
resolves a query-derived reason, and vice versa. Pinned items never
auto-resolve. No absence-based resolution is applied for a provider whose
fetch failed or was partial (see "Error handling").

- GH notification origin clears when GitHub no longer reports the unread
  notification (the user viewed the thread on github.com, marked it read, or
  — after ~3 months — GitHub expired it; the expiry caveat is accepted).
- GH query origin clears when the query no longer matches (thread resolved,
  checks green, issue closed, PR merged/closed, stale PR received activity).
- LP items: only ack or an auto-heuristic: the user commented/voted after
  `last_updated`; the MP is merged/abandoned; the bug is Fix Released/
  Invalid/Won't Fix. Falling out of a `modified_since` window is not a leave
  path.
- Any item: `gitreport ack`.

### Ack / un-ack

- `gitreport ack [ID] [--list]`: `ID` is an id or substring. Sets
  `status = acked`, `acked = true`, `acked_at = now`, clears `pinned`;
  interactive disambiguation when the substring matches several items;
  `--list` (or no `ID`) browses open items. Acking an item with a
  notification origin also marks that thread read on GitHub (best-effort;
  failure is logged and tolerated), so server and local state converge.
- `gitreport unack <id-or-substring>` sets `status = open`, clears
  `acked`/`acked_at`, and increments `reopen_count`. The item returns to
  Still open on the next report, marked "re-opened". `first_seen` is
  unchanged, so it does not show up as New. Un-acking a **resolved** item
  is a manual pin (`pinned = true`): it stays open until acked again,
  regardless of source state.

### Review gaps and consumption

The report has two independent clocks: **generation** (the tool ran) and
**consumption** (the user read the report). A digest that is generated but
never reviewed behaves as if it were never generated:

- **`last_reviewed` cursor**: every coverage window — activity window,
  "New items", LP `modified_since` — anchors to `last_reviewed`, not to
  `last_digest_run`. Digest generation never advances it; only
  `gitreport read` does.
- **`gitreport read`**: marks reports consumed. Sets `last_reviewed` to the
  generation timestamp of the newest digest (`last_digest_run`, also recorded
  in that digest's front matter) and `reviewed_at = now`. Events that
  happened after that digest was generated — including between generation
  and `read` — fall after the new `last_reviewed` and appear in the next
  digest. `read` then re-renders the status line of every dated digest whose
  generation timestamp is newer than the previous `last_reviewed` (only the
  newest digest if `last_reviewed` was unset). `--open` also opens
  `latest.html` (`latest.md` if `html` is disabled).
- **Banner**: two lines with different lifetimes.
  - *Coverage line* (frozen at generation, always historically true), one
    format everywhere: "Coverage: <start> – <end> (<N> days since last
    review)", e.g. "Coverage: Mon 22 – Mon 28 Sep (6 days since last
    review)". On a first-ever run `<start>` is the 24h fallback.
  - *Status line* (live, re-rendered by `read`): a single line beginning
    with the exact sentinel `Status:` — "Status: NOT YET REVIEWED — run
    `gitreport read` after reviewing" before; "Status: Reviewed Mon 28 Sep
    09:14" (from `reviewed_at`) after. `read` replaces that one line in the
    cached `.md` text and re-runs the one HTML renderer — milliseconds, no
    network, no special-case code.

<!-- Amended 2026-09-30: see docs/superpowers/specs/2026-09-30-html-dashboard-design.md -->
Amended 2026-09-30: `read` re-renders the HTML from the `.json` snapshot, not from the cached `.md`; the `.md` is only re-stamped.

- A single missed day costs nothing; an unreviewed week accumulates into
  one wide window, exactly like a missed period. There is deliberately no
  interactive prompt at generation time (cron must never hang); the status
  line is the nudge.
- `ack`/`unack` (item level) and `read` (report level) are orthogonal.

### Missed periods and catch-up

All report windows are anchored to state (`last_reviewed` and per-item
`first_seen`/`last_updated`), never to the calendar day. If the machine is
off, suspended, the scheduler misses runs, or digests are generated but
never reviewed — for any length of time — the next run covers the entire
missed period:

- **Activity window**: since `last_reviewed`, whatever the gap. The 24h
  fallback applies only on a first-ever run (when `last_reviewed` is unset).
- **New items**: every open item whose `first_seen` falls after
  `last_reviewed` appears as New, however many days accumulated.
- **Catch-up digest**: the first digest after a gap is a single digest for
  the whole period; the tool does not synthesise per-day digests for days
  that never ran. The coverage line states the covered period in the
  standard format. New items are ordered by `last_updated`, newest first,
  so the most recent catch-up items read first.
- **Scheduling**: a systemd timer with `Persistent=true` fires shortly after
  boot if the schedule elapsed while the machine was off, so the catch-up
  digest is typically ready the morning you return. Wake-from-suspend is
  covered by a system-level resume unit (`WantedBy=suspend.target`,
  `After=suspend.target`) running `gitreport digest --catch-up`, which
  no-ops when today's digest already exists — one digest per local day
  regardless of how often the machine resumes. Plain cron simply runs at
  the next scheduled time (`@reboot` + `--catch-up` covers boot). Either
  way, coverage is identical because windows are state-anchored.
- **Caveat**: GitHub expires unread notifications after ~3 months. An
  absence longer than that can lose notification-derived items (mention,
  comment, review-request threads). Query-derived items (assigned issues,
  unresolved threads, failing checks, stale PRs) and all Launchpad items are
  query-based and survive any absence.

## GitHub specifics

- Dependency: pygithub bumped to `^2.6`.
- Token: the notifications API does not support fine-grained personal access
  tokens; a classic PAT with the `notifications` scope is required.
- Notifications: REST endpoint via PyGithub, always fetched in full
  (`all=False`, unread only, no `since` filter) so absence-based resolution
  is meaningful. The tool never bulk-marks notifications as read; only `ack`
  marks a single thread read.
- Reason → kind mapping (other reasons are ignored; `subscribed` is
  explicitly excluded as watched-repo noise; `state_change` is deferred):

  | Notification reason | Kind |
  | --- | --- |
  | `review_requested` | `review_requested` |
  | `mention`, `team_mention` | `mention` |
  | `comment`, `author` (comments on your own PRs arrive as `author`) | `comment` |
  | `assign` | `issue_assigned` |
  | `ci_activity` | `ci_failure` (only when the check conclusion is a failure; successes skipped) |

- Query-derived items:
  - Unresolved review threads on my open PRs: GraphQL `reviewThreads` via
    the public `Github.requester.graphql_query`, one batched query over all
    open PRs (paginated), not one call per PR:

    ```graphql
    query($q: String!, $cursor: String) {
      search(query: $q, type: ISSUE, first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { ... on PullRequest {
          url title updatedAt
          reviewThreads(first: 100) { nodes {
            isResolved
            comments(last: 1) { nodes { author { login } updatedAt } }
          } }
        } }
      }
    }
    ```

    with `q = "is:pr is:open author:<user>"`.
  - Failing checks on my open PRs: `is:pr is:open author:@me status:failure`.
  - Issues assigned to me: `is:issue is:open assignee:@me`.
  - Stale PRs: `is:pr is:open author:@me updated:<now - stale_pr_days>`.
- Exclusions match `owner/repo`, glob-capable.

## Launchpad specifics

No notifications API exists; everything is person-scoped queries:

- Merge proposals requesting my review (`getRequestedReviews()`, default
  "Needs review" status; the existing activity provider passes
  `status=ALL_MP_STATUSES`, attention uses the default).
- My open merge proposals with new comments/votes since `last_reviewed`
  (24h fallback on first run).
- Bugs assigned to me: open tasks via `person.searchTasks(assignee=me)`
  without `modified_since` (parity with GH `is:issue is:open assignee:@me`).
- Subscribed bugs with activity: `modified_since = last_reviewed` (24h
  fallback on first run).
- LP resolution checks re-load each open or acked LP item from state by URL
  (`lp.load(url)`). For retention, an LP source "stops reporting" an item
  when the reload shows a leave heuristic's closing state (MP
  merged/abandoned; bug Fix Released/Invalid/Won't Fix); that sets
  `resolved_at`.
- Exclusions match project name, glob-capable.

## Config

```yaml
attention:
  exclusions:
    github: ["owner/fork-name", "me/dotfiles"]
    launchpad: ["noise-project"]
  stale_pr_days: 7
  state_path: ~/.local/state/gitreport/state.json
  digest_formats: [html, md]
  digest_output: ~/.local/state/gitreport/digests/YYYY-MM-DD
  digest_latest: ~/.local/state/gitreport/digests/latest
```

- The `attention:` block is optional: it defaults to an `AttentionConfig()`
  instance, so existing configs keep working.
- Exclusions apply to attention items only; the activity report is
  unchanged.
- `digest_output` and `digest_latest` are extension-less stems. Files are
  stem + `.html` / `.md` (`…/digests/2026-09-28.html`, `…/latest.md`).
  `digest_output` supports date substitution (`YYYY-MM-DD` tokens, local
  date); same-day reruns overwrite the dated files.
- The `.md` is always written: it is infrastructure for `read`.
  `digest_formats` controls published artifacts and defaults to
  `[html, md]`. Omitting `md` is a config validation error ("`gitreport
  read` requires the `md` digest format"). Omitting `html` skips the HTML
  file and makes `read --open` / `digest --open` open the `.md`.

<!-- Amended 2026-09-30: see docs/superpowers/specs/2026-09-30-html-dashboard-design.md -->
Amended 2026-09-30: the `.json` snapshot is also written on every digest run (no symlink) — `.md` and `.json` are both renderer infrastructure.

## CLI surface

- `gitreport generate ...` — unchanged; standalone activity report for any
  date range.
- `gitreport attention` — attention report to stdout; **does not mutate
  state** (safe dry-run view; items not yet in state show as New).
- `gitreport digest` — the morning run: attention + activity since
  `last_reviewed`; updates state (`last_digest_run`, first-seen marking,
  resolutions, prune); writes the dated `.html`/`.md` digest and refreshes
  the `latest.html`/`latest.md` symlinks; prints Markdown to stdout.
  `--open` opens the digest in a browser. Does not advance `last_reviewed`.
  `--catch-up` runs only when no digest has been generated today (local
  date) — for start-up/resume hooks that fire alongside the scheduled run,
  so a late start still produces the report without per-resume API spam.
- `gitreport read [--open]` — mark reports consumed: sets `last_reviewed` to
  the newest digest's generation timestamp and `reviewed_at = now`,
  re-renders the status line of every digest generated after the previous
  `last_reviewed`; `--open` also opens `latest.html` (`latest.md` if HTML is
  disabled).
- `gitreport ack [ID] [--list]` — mark item(s) done.
- `gitreport unack <id-or-substring>` — pull item(s) back into the inbox.

Scheduling: README documents a systemd timer (`Persistent=true`, so a missed
schedule fires shortly after boot/resume) and an equivalent cron line, both
running `gitreport digest` daily. A missed schedule costs nothing; see
"Missed periods and catch-up".

## HTML digest

- One self-contained static file: inline CSS, no JavaScript, no server.
- The canonical intermediate is the dated Markdown report (`<stem>.md`).
  Its front matter records `generated_at` and `coverage_start`; the body
  carries the coverage line and the `Status:` line. `html_report.py` strips
  the front matter, converts the body via the `markdown` library (new
  dependency), and wraps it in a minimal HTML template with inline CSS.

<!-- Amended 2026-09-30: see docs/superpowers/specs/2026-09-30-html-dashboard-design.md -->
Amended 2026-09-30: the dashboard renders from the report model / `.json` snapshot, not from a Markdown conversion; the `markdown` conversion path above is retained only as the pre-upgrade fallback.

- **Injection**: user-supplied strings (titles, reasons, logins, repo names)
  are escaped for both HTML and Markdown metacharacters before they enter
  the Markdown. Links are emitted only from trusted `url` fields and only
  for `http`/`https` schemes; no images are rendered. A hostile title cannot
  produce a `javascript:` link, an external fetch, or mangled formatting.
- Sections: **Needs Attention** (New since last review, then Still open
  buckets) followed by **Recent Activity** (since `last_reviewed`, falling
  back to the last 24 hours on a first-ever run, so nothing silently
  disappears). A provider section whose fetch failed is marked stale.
- Banner: a *coverage line* frozen at generation and a *status line* that
  `gitreport read` re-renders from the cached `.md` (see "Review gaps and
  consumption").
- Every `digest` run refreshes the `digest_latest` symlinks (`latest.html`,
  `latest.md`) atomically (temporary symlink + rename) inside the digest's
  state lock, so a browser bookmark or shell alias always opens the newest
  report. No extra cron entry is needed.
- Future renderers (email, Slack) add a module; the Markdown model is their
  shared input.

## Concurrency

Every state mutation (`digest`, `read`, `ack`, `unack`) takes an exclusive
`fcntl.flock` on `<state>.lock` for its whole load–modify–write cycle, so a
cron `digest` cannot lose a concurrent `ack`. `digest` holds the lock from
state reload through state write, digest file writes, and symlink refresh;
network fetches happen before the lock is taken. `attention` is read-only
and relies on atomic rename for a consistent snapshot.

## Error handling

- **No resolution on failure**: if a provider's fetch fails or returns
  partial data, NO absence-based resolution is applied to that provider's
  items in this run; they carry over unchanged and the section is marked
  stale. Absence of data is never evidence that an item was handled.
- A provider failure degrades only that provider's section: the error is
  logged as a warning and the digest still renders. The whole run never
  aborts because one provider failed.
- `digest` and `attention` treat `BadCredentialsException` as a
  per-provider failure (warning, degraded section). `generate` keeps its
  current abort behaviour (non-goal).
- Marking a thread read on `ack` is best-effort; failure is logged and the
  local ack still applies.
- GitHub rate limits are respected via the existing `GithubRetry` pattern.
- State-file corruption: back up the file, rebuild, warn loudly — the next
  state-mutating command (`digest`, `read`) does the rename under the state
  lock. Acked/resolved history and `first_seen` markers are lost (the backup
  stays on disk for manual inspection). `digest` and `read` recover
  `last_reviewed` from the newest dated digest's front matter: its
  `generated_at` when the digest body contains the `Status: Reviewed`
  sentinel, else its `coverage_start` (generated but never reviewed); when
  no digests exist the cursor stays unset (24h first-run window). Read-only
  views (`attention`, `ack --list`) and item commands (`ack <ID>`, `unack`)
  treat a corrupt file as empty without renaming it or warning — item
  commands simply report "No open item matches" (per the README).

## Testing

- `state.py`: CRUD, atomic write, corruption recovery (including
  `last_reviewed` recovery from digest front matter), ack/unack, pin,
  `last_reviewed`/`reviewed_at` handling, prune — unit tests.
- `attention.py`: merge/dedupe/bucket/resolution logic as pure functions of
  (provider items, state) -> report model — unit tests, no mocks needed.
- Lifecycle: post-ack new activity by someone else reopens; self-activity
  never creates or refreshes; prune vs. still-matching (acked matching item
  kept, never New); multi-origin leave rules (one origin clearing does not
  resolve); New/Still-open disjointness.
- Provider failure (exception, partial data, `BadCredentialsException`)
  ⇒ no resolutions applied, items carried over, section marked stale.
- Heuristics: unit tests with fake provider objects.
- Catch-up/review-gap windows: unit tests covering generation gaps and
  unreviewed gaps (cron didn't run vs. ran but `read` never did); read-cursor
  boundary (events between digest generation and `read` appear in the next
  digest); first-ever run semantics.
- Concurrency: locking prevents lost updates (concurrent `digest` + `ack`);
  symlink refresh is atomic.
- CLI: command wiring, `ack` substring disambiguation, `--list`, `--open`
  fallbacks; config defaults for existing configs without `attention:`;
  `md` omitted from `digest_formats` is rejected.
- Providers: mocked tests, following the existing `test_github.py` /
  `test_launchpad.py` patterns (reason→kind mapping, `subscribed` excluded,
  batched GraphQL query, LP reload by URL).
- Renderers: Markdown snapshot tests; HTML well-formedness (parseable,
  self-contained, no external references); hostile-title injection
  (`javascript:` links, images, Markdown metacharacters); status-line
  re-render round-trip (generate -> read -> re-render changes only the
  `Status:` line).

<!-- Amended 2026-09-30: see docs/superpowers/specs/2026-09-30-html-dashboard-design.md -->
Amended 2026-09-30: the `read` restamp guarantee is that only the status banner changes, not only the `Status:` line.

## Future seams (explicitly out of scope for v1)

- Email/Slack renderers behind the same Markdown model.
- Dependabot/security alerts as an opt-in attention kind.
- `state_change` notification reason as an attention kind.
- Historical trend dashboard (GitHub Pages or local static archive of digests).
