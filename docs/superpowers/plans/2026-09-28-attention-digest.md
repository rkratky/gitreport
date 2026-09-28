# Attention Digest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "needs my attention" inbox to gitreport — a scheduled morning digest (HTML + Markdown) built on GitHub's notifications API, person-scoped Launchpad queries, and a local state store with ack/read lifecycle, per `docs/superpowers/specs/2026-09-28-attention-digest-design.md` (Revision 2).

**Architecture:** Providers gain `get_attention()` behind the existing `GitProvider` protocol, returning `AttentionItem`s. A pure `attention.py` module merges provider results into a JSON state store (`state.py`, flock + atomic writes), computes the report model, and renders the canonical Markdown digest. `html_report.py` converts cached Markdown to a self-contained HTML file. `main.py` wires five new click commands. The existing `generate` command is untouched.

**Tech Stack:** Python 3.11, click, pydantic v2, PyGithub ^2.6 (public `Github.requester.graphql_query`), launchpadlib, python-markdown, pytest. State = JSON file + `fcntl.flock`.

## Global Constraints

- Spec of record: `docs/superpowers/specs/2026-09-28-attention-digest-design.md` (Revision 2). Read it before starting.
- All stored timestamps are UTC ISO-8601 with offset (`datetime.now(UTC)`); parse back with `datetime.fromisoformat`. Report buckets and display are local time (`datetime.astimezone()`).
- Line length 100 (black/ruff config in `pyproject.toml`).
- Every state mutation (`digest`, `read`, `ack`, `unack`) takes an exclusive `fcntl.flock` on `<state>.lock` for its whole load–modify–write cycle. Network fetches happen **outside** the lock.
- No absence-based resolution for a provider whose fetch failed or returned partial data.
- User-supplied strings (titles, reasons, logins, repo names) are escaped for HTML **and** Markdown metacharacters via `escape_user()` before entering any report. Links only from trusted `url` fields, `http(s)` schemes only, never images.
- `digest_formats` must include `md` (config validation error otherwise); the `.md` cache is always written.
- Never bulk-mark GitHub notifications as read. `ack` marks a single thread read (best-effort).
- Existing `generate` behaviour/output is a non-goal: do not modify it.
- Run tests with `rtk poetry run pytest tests/<file> -v` (or `rtk poetry run pytest -q` for the whole suite). All tests pass before each commit.
- Work happens on branch `attention-digest` in worktree `.worktrees/attention-digest`.

---

### Task 1: Dependencies and `AttentionConfig`

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/gitreport/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `AttentionConfig(BaseModel)` — fields `exclusions: dict[str, list[str]] = {}`, `stale_pr_days: int = 7`, `state_path: Path`, `digest_formats: list[str]` (default `["html", "md"]`), `digest_output: Path`, `digest_latest: Path`; the three path fields default to `~/.local/state/gitreport/...` and are `~`-expanded by `load_config`. `Config` gains `attention: AttentionConfig = Field(default_factory=AttentionConfig)`. Validation: `"md"` must be in `digest_formats`, else `ValueError("`gitreport read` requires the `md` digest format")`.

- [ ] **Step 1: Update `pyproject.toml` dependencies**

In `[tool.poetry.dependencies]` change `pygithub = "^2.3.0"` to `pygithub = "^2.6"` and add `markdown = "^3.6"`. Then run in the worktree:

```bash
rtk poetry lock && rtk poetry install
rtk poetry run python -c "import github, markdown; print(github.__version__)"
```

Expected: version `2.6.0` or higher.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_attention_block_optional_with_defaults(tmp_path: Path):
    """An existing config without `attention:` validates and gets defaults."""
    config_content = {"providers": {"github": {"username": "u", "token": "t"}}}
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(config_content))

    config = load_config(config_file)
    assert config.attention.stale_pr_days == 7
    assert config.attention.digest_formats == ["html", "md"]
    assert config.attention.exclusions == {}
    assert str(config.attention.state_path).endswith("state.json")


def test_attention_custom_values(tmp_path: Path):
    config_content = {
        "providers": {"github": {"username": "u", "token": "t"}},
        "attention": {
            "exclusions": {"github": ["me/fork-*"]},
            "stale_pr_days": 3,
            "state_path": "~/tmp/state.json",
            "digest_formats": ["html", "md"],
            "digest_output": "~/tmp/digests/YYYY-MM-DD",
            "digest_latest": "~/tmp/digests/latest",
        },
    }
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(config_content))

    config = load_config(config_file)
    assert config.attention.stale_pr_days == 3
    assert config.attention.exclusions == {"github": ["me/fork-*"]}
    assert not str(config.attention.state_path).startswith("~")


def test_attention_md_format_required(tmp_path: Path):
    config_content = {
        "providers": {"github": {"username": "u", "token": "t"}},
        "attention": {"digest_formats": ["html"]},
    }
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(config_content))

    with pytest.raises(ValueError, match="requires the .md. digest format"):
        load_config(config_file)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `rtk poetry run pytest tests/test_config.py -v`
Expected: the 3 new tests FAIL (`Config has no attribute attention` / no validation).

- [ ] **Step 4: Implement `AttentionConfig`**

In `src/gitreport/config.py`, extend the pydantic import line to include `model_validator`:

```python
from pydantic import BaseModel, Field, SecretStr, ValidationError, model_validator
```

Add after `ProviderConfig`:

```python
class AttentionConfig(BaseModel):
    """Configuration for the attention digest feature."""

    exclusions: dict[str, list[str]] = Field(default_factory=dict)
    stale_pr_days: int = 7
    state_path: Path = Path("~/.local/state/gitreport/state.json")
    digest_formats: list[str] = Field(default_factory=lambda: ["html", "md"])
    digest_output: Path = Path("~/.local/state/gitreport/digests/YYYY-MM-DD")
    digest_latest: Path = Path("~/.local/state/gitreport/digests/latest")

    @model_validator(mode="after")
    def _validate_formats(self) -> "AttentionConfig":
        if "md" not in self.digest_formats:
            raise ValueError("`gitreport read` requires the `md` digest format")
        return self
```

Add `attention` to `Config`:

```python
class Config(BaseModel):
    """Root model for the application's configuration."""

    providers: dict[str, ProviderConfig]
    attention: AttentionConfig = Field(default_factory=AttentionConfig)
```

In `load_config`, after `config = Config.model_validate(config_data)` succeeds, expand `~`:

```python
    try:
        config = Config.model_validate(config_data)
    except ValidationError as e:
        raise ValueError(f"Configuration validation error: {e}")
    config.attention.state_path = config.attention.state_path.expanduser()
    config.attention.digest_output = config.attention.digest_output.expanduser()
    config.attention.digest_latest = config.attention.digest_latest.expanduser()
    return config
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `rtk poetry run pytest tests/test_config.py -v`
Expected: all PASS (6 existing + 3 new).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml poetry.lock src/gitreport/config.py tests/test_config.py
git commit -m "feat: attention config with md-format validation"
```

---

### Task 2: State store

**Files:**
- Create: `src/gitreport/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Consumes: nothing from other tasks (pydantic only).
- Produces (used by Tasks 4, 5, 8):
  - `ItemState(BaseModel)`: `status: str = "open"` (`"open"`/`"acked"`/`"resolved"`), `origins: list[str] = []`, `kinds: list[str] = []`, `reasons: list[str] = []`, `repo: str = ""`, `title: str = ""`, `url: str = ""`, `first_seen: str = ""`, `last_updated: str = ""`, `acked: bool = False`, `acked_at: str | None = None`, `resolved_at: str | None = None`, `pinned: bool = False`, `reopen_count: int = 0`
  - `AttentionState(BaseModel)`: `version: int = 1`, `last_digest_run: str | None`, `last_reviewed: str | None`, `reviewed_at: str | None`, `items: dict[str, ItemState]`
  - `load_state(path: Path) -> AttentionState` — missing file → empty; corrupt → rename to `<path>.corrupt-<YYYYmmdd-HHMMSS>` and return empty state with a stderr warning
  - `save_state(path: Path, state: AttentionState) -> None` — atomic (tmp + `os.replace`)
  - `state_lock(path: Path)` — context manager, exclusive `fcntl.flock` on `<path>.lock`
  - `record_read(state: AttentionState, generation_ts: str, reviewed_at: str) -> None`
  - `prune(state: AttentionState, now: datetime, retention_days: int = 30) -> list[str]` — prunes acked/resolved items whose `resolved_at` is ≥ retention_days old; items with `resolved_at is None` are still-matching suppression records and are kept

- [ ] **Step 1: Write the failing tests**

Create `tests/test_state.py`:

```python
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gitreport.state import (
    AttentionState,
    ItemState,
    load_state,
    prune,
    record_read,
    save_state,
    state_lock,
)


def make_item(item_id: str = "gh:https://x/1", **overrides) -> tuple[str, ItemState]:
    defaults = dict(
        status="open",
        origins=["query"],
        kinds=["mention"],
        reasons=["mentioned you"],
        repo="org/repo",
        title="t",
        url="https://x/1",
        first_seen="2026-09-01T06:00:00+00:00",
        last_updated="2026-09-01T06:00:00+00:00",
    )
    defaults.update(overrides)
    return item_id, ItemState(**defaults)


def test_load_missing_file_gives_empty_state(tmp_path: Path):
    state = load_state(tmp_path / "state.json")
    assert state.items == {}
    assert state.last_reviewed is None
    assert state.version == 1


def test_save_and_load_roundtrip(tmp_path: Path):
    path = tmp_path / "state.json"
    state = AttentionState()
    item_id, item = make_item()
    state.items[item_id] = item
    save_state(path, state)

    loaded = load_state(path)
    assert loaded.items[item_id].status == "open"
    assert loaded.items[item_id].kinds == ["mention"]


def test_corrupt_state_backed_up_and_rebuilt(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    state = load_state(path)
    assert state.items == {}
    assert len(list(tmp_path.glob("state.json.corrupt-*"))) == 1


def test_record_read_sets_cursor_fields():
    state = AttentionState()
    record_read(state, "2026-09-28T06:00:00+00:00", "2026-09-28T09:14:00+00:00")
    assert state.last_reviewed == "2026-09-28T06:00:00+00:00"
    assert state.reviewed_at == "2026-09-28T09:14:00+00:00"


def test_prune_removes_old_acked_and_resolved_items():
    now = datetime(2026, 9, 28, tzinfo=UTC)
    old = (now - timedelta(days=31)).isoformat()
    recent = (now - timedelta(days=1)).isoformat()
    state = AttentionState()
    state.items["gh:1"] = make_item("gh:1", status="acked", acked=True, resolved_at=old)[1]
    state.items["gh:2"] = make_item("gh:2", status="resolved", resolved_at=old)[1]
    state.items["gh:3"] = make_item("gh:3", status="acked", acked=True, resolved_at=recent)[1]
    state.items["gh:4"] = make_item("gh:4")[1]  # open: never pruned
    state.items["gh:5"] = make_item("gh:5", status="acked", acked=True, resolved_at=None)[1]

    pruned = prune(state, now)

    assert set(pruned) == {"gh:1", "gh:2"}
    assert "gh:3" in state.items  # within retention window
    assert "gh:4" in state.items  # open items are never pruned
    assert "gh:5" in state.items  # still-matching acked item: suppression record


def test_state_lock_file_created(tmp_path: Path):
    path = tmp_path / "state.json"
    with state_lock(path):
        assert Path(str(path) + ".lock").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `rtk poetry run pytest tests/test_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gitreport.state'`.

- [ ] **Step 3: Implement `src/gitreport/state.py`**

```python
"""Attention inbox state store: JSON file, atomic writes, exclusive flock."""

import fcntl
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

RETENTION_DAYS = 30


class ItemState(BaseModel):
    """State of one attention item, keyed by its stable id."""

    status: str = "open"  # open | acked | resolved
    origins: list[str] = Field(default_factory=list)
    kinds: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    repo: str = ""
    title: str = ""
    url: str = ""
    first_seen: str = ""
    last_updated: str = ""
    acked: bool = False
    acked_at: str | None = None
    resolved_at: str | None = None
    pinned: bool = False
    reopen_count: int = 0


class AttentionState(BaseModel):
    version: int = 1
    last_digest_run: str | None = None
    last_reviewed: str | None = None
    reviewed_at: str | None = None
    items: dict[str, ItemState] = Field(default_factory=dict)


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


class state_lock:
    """Exclusive flock on `<state>.lock` for a whole load-modify-write cycle."""

    def __init__(self, path: Path):
        self._lock_path = Path(str(path) + ".lock")
        self._fd: int | None = None

    def __enter__(self) -> "state_lock":
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def load_state(path: Path) -> AttentionState:
    """Load the inbox state; missing file -> empty, corrupt -> rebuild."""
    if not path.exists():
        return AttentionState()
    try:
        data = json.loads(path.read_text())
        return AttentionState.model_validate(data)
    except (json.JSONDecodeError, ValueError) as e:
        backup = Path(f"{path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
        path.rename(backup)
        print(
            f"WARNING: state file {path} was corrupt ({e}); backed up to "
            f"{backup} and rebuilt empty. Ack/read history is lost.",
            file=sys.stderr,
        )
        return AttentionState()


def save_state(path: Path, state: AttentionState) -> None:
    """Atomically write the state (tmp file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state.model_dump(), indent=2))
    os.replace(tmp, path)


def record_read(state: AttentionState, generation_ts: str, reviewed_at: str) -> None:
    """Advance the consumption cursor to a digest's generation timestamp.

    `generation_ts` is that digest's `last_digest_run`/front-matter
    `generated_at` — NOT the wall-clock time of the read, so nothing that
    happened between generation and the read is lost from future coverage.
    """
    state.last_reviewed = generation_ts
    state.reviewed_at = reviewed_at


def prune(
    state: AttentionState, now: datetime, retention_days: int = RETENTION_DAYS
) -> list[str]:
    """Prune acked/resolved items whose source stopped reporting >30d ago.

    An item with `resolved_at is None` is still reported by its source and is
    kept as a suppression record: it must never re-enter the inbox as New.
    """
    pruned: list[str] = []
    for item_id in list(state.items):
        item = state.items[item_id]
        if item.status not in ("acked", "resolved") or item.resolved_at is None:
            continue
        resolved = datetime.fromisoformat(item.resolved_at)
        if resolved.tzinfo is None:
            resolved = resolved.replace(tzinfo=UTC)
        if (now - resolved).days >= retention_days:
            del state.items[item_id]
            pruned.append(item_id)
    return pruned
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `rtk poetry run pytest tests/test_state.py -v`
Expected: 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gitreport/state.py tests/test_state.py
git commit -m "feat: attention state store with flock and atomic writes"
```

### Task 3: `AttentionItem`, fetch-result type, and shared helpers

**Files:**
- Modify: `src/gitreport/providers/base.py`
- Test: `tests/test_providers_base.py` (new)

**Interfaces:**
- Produces (used by Tasks 4–8):
  - `AttentionItem(TypedDict, total=False)`: `id: str` (e.g. `gh:https://github.com/o/r/pull/1`, `lp:https://launchpad.net/bugs/456`), `provider: str` (`"github"`/`"launchpad"`), `kind: str`, `origin: str` (`"notification"`/`"query"`), `repo: str`, `title: str` (escaped), `url: str`, `reason: str` (escaped), `updated_at: str | None` (UTC ISO, latest event by someone other than the user), `thread_url: str` (GH only; the notification's subject API URL, used by `ack` to mark the thread read)
  - `AttentionFetch(TypedDict)`: `ok: bool`, `items: list[AttentionItem]`, `resolved_ids: list[str]` (ids this provider can prove no longer need attention), `error: str | None`
  - `escape_user(text: str) -> str` — HTML-escape + backslash-escape Markdown metacharacters
  - `is_excluded(repo: str, patterns: list[str] | None) -> bool` — `fnmatch` glob match
  - `GitProvider` protocol gains `get_attention(self, since: datetime, exclusions: list[str] | None = None, state_items: dict[str, dict] | None = None, stale_pr_days: int | None = None) -> AttentionFetch` (`state_items` maps item id → plain-dict record so the provider can compute absence-based `resolved_ids`; `stale_pr_days` is only used by GitHub)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_providers_base.py`:

```python
from gitreport.providers.base import AttentionItem, escape_user, is_excluded


def test_escape_user_neutralises_html_and_markdown():
    text = "Bug <b>bold</b> [x](javascript:evil) *em_ ph* #tag"
    escaped = escape_user(text)
    assert "<b>" not in escaped
    # Every Markdown metacharacter is backslash-escaped so it renders literally.
    assert "[x]" not in escaped
    assert "*em_" not in escaped


def test_escape_user_plain_text_unchanged():
    assert escape_user("Fix login crash") == "Fix login crash"


def test_is_excluded_globs():
    assert is_excluded("me/fork-x", ["me/fork-*"])
    assert is_excluded("me/dotfiles", ["me/*"])
    assert not is_excluded("org/repo", ["me/*"])
    assert not is_excluded("org/repo", None)
```

- [ ] **Step 2: Run to verify failure**

Run: `rtk poetry run pytest tests/test_providers_base.py -v`
Expected: FAIL — `ImportError: cannot import name 'escape_user'`.

- [ ] **Step 3: Implement in `src/gitreport/providers/base.py`**

Add to the imports at the top: `import html as _html` and `from fnmatch import fnmatch`.

Append at the end of the file:

```python
ATTENTION_KINDS = (
    "review_requested",
    "mention",
    "comment",
    "ci_failure",
    "thread_unresolved",
    "issue_assigned",
    "stale_pr",
    "lp_mp_comment",
    "lp_bug_activity",
    "lp_mp_needs_review",
)

_MD_SPECIALS = "\\`*_{}[]()#+.!|>~"


def escape_user(text: str) -> str:
    """Escape user-supplied text for Markdown that will render as HTML.

    Neutralises HTML special characters and backslash-escapes Markdown
    metacharacters, so hostile titles cannot form links, images or emphasis.
    """
    text = _html.escape(text, quote=False)
    text = text.replace("\\", "\\\\")
    for ch in _MD_SPECIALS:
        text = text.replace(ch, "\\" + ch)
    return text


def is_excluded(repo: str, patterns: list[str] | None) -> bool:
    """True when `repo` matches any exclusion glob."""
    return any(fnmatch(repo, pattern) for pattern in (patterns or []))


class AttentionItem(TypedDict, total=False):
    """One thing that may need the user's attention."""

    id: str
    provider: str
    kind: str
    origin: str  # 'notification' or 'query'
    repo: str
    title: str  # escaped via escape_user
    url: str
    reason: str  # escaped via escape_user
    updated_at: str | None  # UTC ISO; latest event by someone other than the user
    thread_url: str | None  # GH only: the notification THREAD api url
    # (https://api.github.com/notifications/threads/{id}); used by ack to
    # mark that thread read via a PATCH to this exact url.


class AttentionFetch(TypedDict):
    """Outcome of one provider's attention fetch."""

    ok: bool
    items: list[AttentionItem]
    resolved_ids: list[str]
    error: str | None
```

Extend the `GitProvider` protocol (after `get_activity`) with:

```python
    def get_attention(
        self,
        since: datetime,
        exclusions: list[str] | None = None,
        state_items: "dict[str, dict] | None" = None,
        stale_pr_days: int | None = None,
    ) -> AttentionFetch:
        """
        Fetch attention items.

        Args:
            since: window start for event-derived queries (LP comments,
                subscribed-bug activity). GitHub queries are current-state
                and ignore it.
            exclusions: glob patterns on repo/project name; matching items
                are dropped.
            state_items: snapshot of the user's current inbox records
                (id -> plain dict with at least `url`), so the provider can
                compute absence-based resolved_ids.
            stale_pr_days: stale-PR threshold (GitHub only).

        Returns:
            AttentionFetch. `ok=False` means the fetch failed or was partial:
            the caller must skip absence-based resolution for this provider.
        """
        ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `rtk poetry run pytest tests/test_providers_base.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gitreport/providers/base.py tests/test_providers_base.py
git commit -m "feat: AttentionItem model, escape_user, get_attention protocol"
```

---

### Task 4: attention.py — merge, lifecycle, report model, Markdown digest

**Files:**
- Create: `src/gitreport/attention.py`
- Test: `tests/test_attention.py`

**Interfaces:**
- Consumes: `AttentionFetch` dicts from providers (Task 3), plain-dict state (the `model_dump()` form of Task 2's models).
- Produces (used by Tasks 6 and 8):
  - `dedupe(results: dict[str, AttentionFetch]) -> dict[str, dict]` — union per-origin provider items by `id`; unions `origins`/`kinds`/`reasons` (reasons capped at 10, first-seen order); display fields (`repo`, `title`, `url`) from the origin with the latest `updated_at`.
  - `merge_into_state(state: dict, merged_items: dict[str, dict], ok_providers: set[str], generated_at: str, resolved_ids: set[str] = frozenset()) -> dict` — pure; takes and returns a plain-dict state. Implements: enter (`first_seen = generated_at`, `status = open`), reopen rule (new event by someone else on acked/resolved → open, clears acked/acked_at/resolved_at, `reopen_count += 1`; a pinned item simply stays open), new-event definition (`updated_at` later than the record's `last_updated`; a still-present unchanged match is not a new event), resolution pass (only for providers whose fetch succeeded; item absent from the merged items and not pinned → open items become `resolved`, acked items keep status and gain `resolved_at`), `resolved_ids` override (provider-proven resolutions, applied even if `ok_providers` doesn't include the provider), retention prune (acked/resolved with `resolved_at` ≥ 30 days old), sets `last_digest_run = generated_at`.
  - `build_report(state: dict, now: datetime) -> dict` — `{"new": [...], "today": [...], "week": [...], "older": [...]}`; entry = `{"id", "repo", "title", "url", "reasons", "last_updated", "reopen_count"}`. New = `status == "open"` and (`last_reviewed` unset or `first_seen > last_reviewed`). Still open = open and not New, bucketed by `last_updated` in local time. New items sorted by `last_updated` descending. Disjoint by construction.
  - `render_digest_markdown(state, activity_markdown, coverage_start, generated_at, stale_providers) -> str` — front matter (`generated_at`, `coverage_start`), H1, one-format coverage line `Coverage: <start> – <end> (<N> day(s) since last review)`, `Status:` sentinel line, `## Needs attention` (New, then buckets), `## Recent activity` (verbatim activity Markdown).
  - `render_attention_stdout(state, stale_providers, now) -> str` — dry-run view: `# Needs attention` + body, no front matter/status/coverage.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_attention.py`:

```python
from datetime import UTC, datetime

from gitreport.attention import (
    build_report,
    dedupe,
    merge_into_state,
    render_attention_stdout,
    render_digest_markdown,
)

NOW = "2026-09-28T06:00:00+00:00"
LATER = "2026-09-29T10:00:00+00:00"


def item(mid="gh:https://github.com/o/r/pull/1", origin="notification", kind="mention",
         reason="mentioned you", updated_at=NOW, provider="github", **extra):
    base = {"id": mid, "provider": provider, "kind": kind, "origin": origin,
            "repo": "o/r", "title": "T", "url": "https://github.com/o/r/pull/1",
            "reason": reason, "updated_at": updated_at}
    base.update(extra)
    return base


def rec(**overrides):
    """A plain-dict state item record."""
    base = {
        "status": "open", "origins": ["notification"], "kinds": ["mention"],
        "reasons": [], "repo": "o/r", "title": "T", "url": "https://x/1",
        "first_seen": NOW, "last_updated": NOW, "acked": False,
        "acked_at": None, "resolved_at": None, "pinned": False,
        "reopen_count": 0, "thread_url": None, "provider": "github",
    }
    base.update(overrides)
    return base


def state_with(**kwargs):
    base = {"version": 1, "last_digest_run": None, "last_reviewed": None,
            "reviewed_at": None, "items": {}}
    base.update(kwargs)
    return base


def test_dedupe_unions_origins_kinds_reasons():
    results = {"github": {"ok": True, "error": None, "resolved_ids": [], "items": [
        item(origin="notification", kind="comment", reason="new comment"),
        item(origin="query", kind="thread_unresolved", reason="unresolved thread"),
    ]}}
    merged = dedupe(results)
    m = merged["gh:https://github.com/o/r/pull/1"]
    assert sorted(m["origins"]) == ["notification", "query"]
    assert sorted(m["kinds"]) == ["comment", "thread_unresolved"]
    assert set(m["reasons"]) == {"new comment", "unresolved thread"}


def test_merge_creates_open_item_with_first_seen():
    merged = {"gh:1": {**dedupe({"github": {"ok": True, "error": None,
                "resolved_ids": [], "items": [item(mid="gh:1")]}})["gh:1"]}}
    new_state = merge_into_state(state_with(), merged, {"github"}, NOW)
    r = new_state["items"]["gh:1"]
    assert r["status"] == "open"
    assert r["first_seen"] == NOW
    assert r["last_updated"] == NOW


def test_reopen_rule_someone_else_event():
    state = state_with(items={"gh:1": rec(status="acked", acked=True,
        acked_at="2026-09-28T07:00:00+00:00",
        first_seen="2026-09-28T06:00:00+00:00",
        last_updated="2026-09-28T06:00:00+00:00")})
    merged = {"gh:1": dedupe({"github": {"ok": True, "error": None,
              "resolved_ids": [], "items": [item(mid="gh:1", updated_at=LATER,
              reason="new comment")]}})["gh:1"]}
    new_state = merge_into_state(state, merged, {"github"}, LATER)
    r = new_state["items"]["gh:1"]
    assert r["status"] == "open"
    assert r["acked"] is False
    assert r["reopen_count"] == 1
    assert r["first_seen"] == "2026-09-28T06:00:00+00:00"  # unchanged: not New again


def test_pinned_item_does_not_reopen():
    state = state_with(items={"gh:1": rec(pinned=True, reopen_count=2,
        first_seen="2026-09-01T06:00:00+00:00",
        last_updated="2026-09-01T06:00:00+00:00")})
    merged = {"gh:1": dedupe({"github": {"ok": True, "error": None,
              "resolved_ids": [], "items": [item(mid="gh:1", updated_at=LATER)]}})["gh:1"]}
    new_state = merge_into_state(state, merged, {"github"}, LATER)
    r = new_state["items"]["gh:1"]
    assert r["status"] == "open"
    assert r["reopen_count"] == 2  # pinned: no further reopen increments


def test_resolution_requires_every_origin():
    state = state_with(items={"gh:1": rec(origins=["notification", "query"])})
    merged = {"gh:1": dedupe({"github": {"ok": True, "error": None,
              "resolved_ids": [],
              "items": [item(mid="gh:1", origin="query")]}})["gh:1"]}
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


def test_resolved_id_override():
    state = state_with(items={"lp:1": rec(provider="launchpad")})
    new_state = merge_into_state(state, {}, {"launchpad"}, "2026-09-28T07:00:00+00:00",
                                 resolved_ids={"lp:1"})
    r = new_state["items"]["lp:1"]
    assert r["status"] == "resolved"


def test_retention_prune():
    old_resolved = "2026-08-01T00:00:00+00:00"
    state = state_with(items={
        "gh:old": rec(status="resolved", resolved_at=old_resolved),
        "gh:new": rec(resolved_at="2026-09-27T00:00:00+00:00"),
    })
    # gh:new is open with resolved_at set only for the prune-status guard;
    # open items are never pruned regardless of resolved_at.
    new_state = merge_into_state(state, {}, {"github"}, "2026-09-28T07:00:00+00:00")
    assert "gh:old" not in new_state["items"]
    assert "gh:new" in new_state["items"]


def test_build_report_new_vs_still_open_disjoint():
    state = state_with(
        last_reviewed="2026-09-27T06:00:00+00:00",
        items={
            "gh:1": rec(title="New one", first_seen="2026-09-28T06:00:00+00:00",
                        last_updated="2026-09-28T06:00:00+00:00"),
            "gh:2": rec(title="Old", first_seen="2026-09-20T06:00:00+00:00",
                        last_updated="2026-09-20T06:00:00+00:00"),
        },
    )
    report = build_report(state, datetime.fromisoformat(NOW))
    assert [e["id"] for e in report["new"]] == ["gh:1"]
    still = [e["id"] for b in ("today", "week", "older") for e in report[b]
             if e["id"] == "gh:2"]
    assert still == ["gh:2"]
    # Disjoint: gh:1 never appears in a bucket.
    assert not [e for b in ("today", "week", "older") for e in report[b]
                if e["id"] == "gh:1"]


def test_build_report_bucketing():
    state = state_with(
        last_reviewed="2026-09-01T00:00:00+00:00",
        items={
            "gh:t": rec(first_seen="2026-09-20T00:00:00+00:00",
                        last_updated=datetime.now(UTC).isoformat()),
            "gh:w": rec(first_seen="2026-09-20T00:00:00+00:00",
                        last_updated="2026-09-25T00:00:00+00:00"),
            "gh:o": rec(first_seen="2026-09-20T00:00:00+00:00",
                        last_updated="2026-08-01T00:00:00+00:00"),
        },
    )
    report = build_report(state, datetime.now(UTC))
    ids_today = [e["id"] for e in report["today"]]
    assert "gh:t" in ids_today
    assert [e["id"] for e in report["week"]] == ["gh:w"]
    assert [e["id"] for e in report["older"]] == ["gh:o"]


def test_render_digest_markdown_shape():
    state = state_with(last_reviewed="2026-09-27T06:00:00+00:00", items={})
    md = render_digest_markdown(state, "# Git activity report", NOW, NOW,
                                stale_providers=[])
    assert md.startswith("---\n")
    assert f"generated_at: {NOW}" in md
    assert "Status: NOT YET REVIEWED" in md
    assert "Coverage: " in md
    assert "## Recent activity" in md
    assert "# Git activity report" in md


def test_render_attention_stdout_is_dry_view():
    out = render_attention_stdout(state_with(), stale_providers=["launchpad"],
                                  now=datetime.fromisoformat(NOW))
    assert "# Needs attention" in out
    assert "launchpad" in out
    assert "Status:" not in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `rtk poetry run pytest tests/test_attention.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gitreport.attention'`.

- [ ] **Step 3: Implement `src/gitreport/attention.py`**

```python
"""Merge provider attention results with state; render the digest."""

import copy
from datetime import UTC, datetime, timedelta

PRUNE_AFTER = timedelta(days=30)
MAX_REASONS = 10
BUCKETS = ("today", "week", "older")
BUCKET_TITLES = {"today": "Today", "week": "Last 7 days", "older": "Older than 7 days"}


def _is_later(a: str | None, b: str | None) -> bool:
    if a is None:
        return False
    if b is None:
        return True
    return a > b


def dedupe(results: dict[str, dict]) -> dict[str, dict]:
    """Union per-origin provider items into merged items keyed by id."""
    merged: dict[str, dict] = {}
    for provider, result in results.items():
        for item in result.get("items", []):
            mid = item["id"]
            m = merged.setdefault(mid, {
                "id": mid,
                "provider": item.get("provider", provider),
                "origins": set(),
                "kinds": set(),
                "reasons": [],
                "repo": item.get("repo", ""),
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "updated_at": item.get("updated_at"),
                "thread_url": item.get("thread_url"),
            })
            if item.get("origin"):
                m["origins"].add(item["origin"])
            if item.get("kind"):
                m["kinds"].add(item["kind"])
            reason = item.get("reason")
            if reason and reason not in m["reasons"]:
                m["reasons"].append(reason)
            if item.get("thread_url"):
                m["thread_url"] = item["thread_url"]
            if _is_later(item.get("updated_at"), m["updated_at"]):
                m["updated_at"] = item["updated_at"]
                m["repo"] = item.get("repo", m["repo"])
                m["title"] = item.get("title", m["title"])
                m["url"] = item.get("url", m["url"])
    for m in merged.values():
        m["origins"] = sorted(m["origins"])
        m["kinds"] = sorted(m["kinds"])
        m["reasons"] = m["reasons"][:MAX_REASONS]
    return merged


def merge_into_state(
    state: dict,
    merged_items: dict[str, dict],
    ok_providers: set[str],
    generated_at: str,
    resolved_ids: set[str] = frozenset(),
) -> dict:
    """Pure: apply enters/reopens/resolutions/prune; return the new state."""
    st = copy.deepcopy(state)
    items = st["items"]

    # 1. Enter / reopen / refresh, from the fetched items.
    for mid, m in merged_items.items():
        r = items.get(mid)
        if r is None:
            items[mid] = {
                "status": "open",
                "origins": m["origins"],
                "kinds": m["kinds"],
                "reasons": m["reasons"],
                "repo": m["repo"],
                "title": m["title"],
                "url": m["url"],
                "first_seen": generated_at,
                "last_updated": m["updated_at"] or generated_at,
                "acked": False,
                "acked_at": None,
                "resolved_at": None,
                "pinned": False,
                "reopen_count": 0,
                "thread_url": m.get("thread_url"),
                "provider": m["provider"],
            }
            continue
        new_event = _is_later(m["updated_at"], r.get("last_updated"))
        for key in ("origins", "kinds"):
            r[key] = sorted(set(r[key]) | set(m[key]))
        for reason in m["reasons"]:
            if reason not in r["reasons"]:
                r["reasons"].append(reason)
        r["reasons"] = r["reasons"][:MAX_REASONS]
        r["resolved_at"] = None  # a source reports it again
        if m.get("thread_url"):
            r["thread_url"] = m["thread_url"]
        if new_event:
            r["repo"], r["title"], r["url"] = m["repo"], m["title"], m["url"]
            if r.get("pinned"):
                pass  # pinned: refresh display only; stays open
            elif r["status"] == "open":
                r["last_updated"] = m["updated_at"]
            elif r["status"] in ("acked", "resolved"):
                r.update(
                    status="open", acked=False, acked_at=None, resolved_at=None,
                    reopen_count=r.get("reopen_count", 0) + 1,
                    last_updated=m["updated_at"],
                )

    # 2. Resolution pass — absence-based, only for fully-fetched providers.
    for mid, r in items.items():
        if r.get("pinned") or mid in merged:
            continue  # still reported, or pinned: never auto-resolve
        if r.get("provider") in ok_providers or mid in resolved_ids:
            if r["status"] == "open":
                r["status"] = "resolved"
            r["resolved_at"] = generated_at

    # 3. Retention prune.
    gen = datetime.fromisoformat(generated_at)
    for mid in list(items):
        r = items[mid]
        if r["status"] not in ("acked", "resolved") or r.get("resolved_at") is None:
            continue
        resolved = datetime.fromisoformat(r["resolved_at"])
        if resolved.tzinfo is None:
            resolved = resolved.replace(tzinfo=UTC)
        if (gen - resolved) >= PRUNE_AFTER:
            del items[mid]

    st["last_digest_run"] = generated_at
    return st


def build_report(state: dict, now: datetime) -> dict:
    """Split open items into New and Still open (today / week / older)."""
    last_reviewed = state.get("last_reviewed")
    new_items: list[dict] = []
    still_open: list[dict] = []
    for mid, r in sorted(state["items"].items()):
        if r.get("status") != "open":
            continue
        is_new = not last_reviewed or (r.get("first_seen") or "") > last_reviewed
        entry = {
            "id": mid,
            "repo": r.get("repo", ""),
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "reasons": r.get("reasons", []),
            "last_updated": r.get("last_updated", ""),
            "reopen_count": r.get("reopen_count", 0),
        }
        (new_items if is_new else still_open).append(entry)

    buckets: dict[str, list[dict]] = {b: [] for b in BUCKETS}
    now_local = now.astimezone()
    for entry in still_open:
        lu = entry["last_updated"]
        when = datetime.fromisoformat(lu).astimezone() if lu else None
        if when is None or when.date() == now_local.date():
            bucket = "today"
        elif when >= now_local - timedelta(days=7):
            bucket = "week"
        else:
            bucket = "older"
        buckets[bucket].append(entry)

    new_items.sort(key=lambda e: e["last_updated"] or "", reverse=True)
    return {"new": new_items, **buckets}


def _fmt_local(ts: str) -> str:
    dt = datetime.fromisoformat(ts).astimezone()
    return f"{dt:%a} {dt.day} {dt:%b}"


def _entry_line(entry: dict) -> str:
    title = entry.get("title") or entry["id"]
    url = entry.get("url", "")
    if url.startswith(("http://", "https://")):
        line = f"- [{title}]({url})"
    else:
        line = f"- {title}"
    if entry.get("reopen_count"):
        line += " (re-opened)"
    for reason in entry.get("reasons", []):
        line += f"\n  - {reason}"
    return line


def render_attention_body(report: dict, stale_providers: list[str]) -> str:
    lines: list[str] = []
    if report["new"]:
        lines += ["### New since last review", ""]
        lines += [_entry_line(e) for e in report["new"]]
        lines.append("")
    for bucket in BUCKETS:
        entries = report[bucket]
        if not entries:
            continue
        lines += [f"### Still open — {BUCKET_TITLES[bucket]}", ""]
        lines += [_entry_line(e) for e in entries]
        lines.append("")
    if not report["new"] and not any(report[b] for b in BUCKETS):
        lines += ["_Nothing needs your attention._", ""]
    if stale_providers:
        names = ", ".join(sorted(stale_providers))
        lines += [
            f"Warning: these providers failed to fetch; their sections may be "
            f"stale: {names}",
            "",
        ]
    return "\n".join(lines)


def render_attention_stdout(
    state: dict, stale_providers: list[str], now: datetime
) -> str:
    """Dry-run attention view (no state machinery, no front matter)."""
    report = build_report(state, now)
    return "# Needs attention\n\n" + render_attention_body(report, stale_providers)


def render_digest_markdown(
    state: dict,
    activity_markdown: str,
    coverage_start: str,
    generated_at: str,
    stale_providers: list[str],
) -> str:
    start_dt = datetime.fromisoformat(coverage_start)
    end_dt = datetime.fromisoformat(generated_at)
    days = max(1, (end_dt - start_dt).days)
    unit = "day" if days == 1 else "days"
    coverage = (
        f"Coverage: {_fmt_local(coverage_start)} – {_fmt_local(generated_at)} "
        f"({days} {unit} since last review)"
    )
    report = build_report(state, end_dt)
    lines = [
        "---",
        f"generated_at: {generated_at}",
        f"coverage_start: {coverage_start}",
        "---",
        "",
        "# GitReport digest",
        "",
        coverage,
        "Status: NOT YET REVIEWED — run `gitreport read` after reviewing",
        "",
        "## Needs attention",
        "",
        render_attention_body(report, stale_providers),
        "## Recent activity",
        "",
        activity_markdown.strip() or "_No activity in the coverage window._",
        "",
    ]
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `rtk poetry run pytest tests/test_attention.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gitreport/attention.py tests/test_attention.py
git commit -m "feat: attention merge/bucket logic and Markdown digest renderer"
```

### Task 5: GitHub attention provider

**Files:**
- Modify: `src/gitreport/providers/github.py`
- Test: `tests/test_github.py` (extend)

**Interfaces:**
- Consumes: `AttentionItem`, `AttentionFetch`, `escape_user`, `is_excluded` (Task 3); provider's own `Github` client.
- Produces: `GitHubProvider.get_attention(since, exclusions=None, state_items=None, stale_pr_days=None) -> AttentionFetch`.

**Behaviour (spec "GitHub specifics"):**
1. Wrap the whole method body in `try/except Exception as e: return AttentionFetch(ok=False, items=[], resolved_ids=[], error=str(e))` — partial data from the middle of a failure is not returned; failure means no resolution.
2. Notifications: `self._github.get_user().get_notifications(all=False)` — unread only, no `since` filter, never bulk-marked read. Map reasons via `GH_REASON_KIND = {"review_requested": "review_requested", "mention": "mention", "team_mention": "mention", "comment": "comment", "author": "comment", "assign": "issue_assigned"}`; `ci_activity` handled separately; everything else (`subscribed` included) skipped.
3. `ci_activity`: map to `ci_failure` only when the subject's check conclusion is a failure. Resolve the subject via `self._github.get_repo(full_name).get_pull(number)` then `any(c.conclusion == "failure" for c in pr.get_check_runs())`; on any exception resolving the subject, skip the notification (do not fail the fetch).
4. Query items (search, `self._github.search_issues`):
   - failing checks: `is:pr is:open author:{user} status:failure` → kind `ci_failure`
   - assigned issues: `is:issue is:open assignee:{user}` → kind `issue_assigned`
   - stale PRs: `is:pr is:open author:{user} updated:<{cutoff}>` where `cutoff = (now - stale_pr_days).strftime("%Y-%m-%d")`; skipped when `stale_pr_days is None` → kind `stale_pr`
5. Unresolved review threads: one batched GraphQL query via `self._github.requester.graphql_query(query, variables)` (public property on pygithub ≥ 2.6):

   ```python
   QUERY = """
   query($q: String!, $cursor: String) {
     search(query: $q, type: ISSUE, first: 50, after: $cursor) {
       pageInfo { hasNextPage endCursor }
       nodes { ... on PullRequest {
         url title updatedAt
         reviewThreads(first: 100) { nodes { isResolved } }
       } }
     }
   }
   """
   ```

   Paginate with `endCursor` while `hasNextPage`. A PR with any `isResolved == False` thread → kind `thread_unresolved`. GraphQL failures raise `GithubException`, caught by the outer handler (fetch marked failed — correct: partial thread data would corrupt absence-resolution).
6. Exclusions: `is_excluded(repo_full_name, exclusions)` drops the item before it is appended.
7. All `title`/`reason` strings go through `escape_user`; `updated_at` from notification `updated_at` or search-item `updated_at` (both are PyGithub datetimes → `.isoformat()`); item ids are `gh:{html_url}`.
8. `resolved_ids`: for each `state_items` record with provider `"github"` whose id is **not** among this run's fetched ids — leave it to the caller's absence pass instead. Do **not** populate `resolved_ids` from absence here (the merge module owns absence); only use it for provider-proven facts (v1: none — return `[]`). This keeps absence semantics in one place.

- [ ] **Step 1: Write the failing tests (append to tests/test_github.py)**

```python
# --- Attention tests ---

from gitreport.providers.base import AttentionFetch  # noqa: E402


class MockNotification:
    def __init__(self, reason, repo="org/repo", title="T",
                 subject_url="https://api.github.com/repos/org/repo/pulls/1",
                 thread_url="https://api.github.com/notifications/threads/1",
                 updated_at=None):
        self.reason = reason
        self.url = thread_url
        self.updated_at = updated_at or datetime(2026, 9, 28, tzinfo=UTC)
        self.subject = type("S", (), {"title": title, "url": subject_url})()


class MockCheckRun:
    def __init__(self, conclusion):
        self.conclusion = conclusion


def _provider_with_notifications(notifications, check_runs=None):
    """GitHubProvider whose Github client returns the given notifications."""
    with patch("gitreport.providers.github.Github") as MockGithub:
        instance = MockGithub.return_value
        instance.get_user.return_value.get_notifications.return_value = notifications
        mock_pr = type("P", (), {})()
        mock_pr.get_check_runs.return_value = [MockCheckRun(c) for c in (check_runs or [])]
        instance.get_repo.return_value.get_pull.return_value = mock_pr
        instance.search_issues.return_value = []
        provider = GitHubProvider(username="testuser", token="fake-token")
        # Keep the mocked client for the call under test (the `with` block
        # only scopes the constructor patch).
        provider._github = instance
    return provider


def test_github_notification_reason_kinds():
    notifications = [_notification_mock(r) for r in [
        "review_requested",
        "mention",
        "team_mention",
        "comment",
        "author",
        "assign",
    ]]
    provider = _provider_with_notifications(notifications)
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))
    assert fetch["ok"] is True
    assert sorted(i["kind"] for i in fetch["items"]) == sorted(
        ["review_requested", "mention", "mention", "comment", "comment", "issue_assigned"]
    )


def _notification_mock(reason, repo="org/repo"):
    # repo="me/fork-x" cases pass a full name; derive the subject url from it.
    subject_url = f"https://api.github.com/repos/{repo}/pulls/1"
    thread_url = "https://api.github.com/notifications/threads/1"
    return MockNotification(reason, subject_url=subject_url, thread_url=thread_url)


def test_github_subscribed_reason_excluded():
    provider = _provider_with_notifications([_notification_mock("subscribed")])
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))
    assert fetch["ok"] is True
    assert fetch["items"] == []


def test_github_ci_activity_only_on_failure():
    provider = _provider_with_notifications(
        [_notification_mock("ci_activity")], check_runs=["success"])
    assert provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))["items"] == []

    provider = _provider_with_notifications(
        [_notification_mock("ci_activity")], check_runs=["failure"])
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))
    assert len(fetch["items"]) == 1
    assert fetch["items"][0]["kind"] == "ci_failure"


def test_github_exclusion_globs():
    provider = _provider_with_notifications(
        [_notification_mock("mention", repo="me/fork-x")],
    )
    fetch = provider.get_attention(
        datetime(2026, 9, 28, tzinfo=UTC), exclusions=["me/fork-*"])
    assert fetch["items"] == []


def test_github_query_items_kinds():
    with patch("gitreport.providers.github.Github") as MockGithub:
        instance = MockGithub.return_value
        instance.get_user.return_value.get_notifications.return_value = []

        failing_pr = MockIssue("Fix CI", "https://github.com/o/r/pull/9",
                               MockRepository("o/r"), "testuser", 9)
        assigned_issue = MockIssue("Bug", "https://github.com/o/r/issues/2",
                                   MockRepository("o/r"), "other", 2)
        stale_pr = MockIssue("Stale", "https://github.com/o/r/pull/3",
                             MockRepository("o/r"), "testuser", 3)

        def search_side_effect(query, **kwargs):
            if "status:failure" in query:
                return [failing_pr]
            if "assignee:" in query:
                return [assigned_issue]
            if "updated:<" in query:
                return [stale_pr]
            return []

        instance.search_issues.side_effect = search_side_effect
        instance.requester.graphql_query.return_value = (
            {"data": {"search": {"pageInfo": {"hasNextPage": False},
                                  "nodes": []}}}, ()
        )
        provider = GitHubProvider(username="testuser", token="fake-token")
        provider._github = instance
        fetch = provider.get_attention(
            datetime(2026, 9, 28, tzinfo=UTC), stale_pr_days=7)

    kinds = sorted(i["kind"] for i in fetch["items"])
    assert kinds == ["ci_failure", "issue_assigned", "stale_pr"]


def test_github_thread_unresolved_via_graphql():
    with patch("gitreport.providers.github.Github") as MockGithub:
        instance = MockGithub.return_value
        instance.get_user.return_value.get_notifications.return_value = []
        instance.search_issues.return_value = []
        instance.requester.graphql_query.return_value = (
            {"data": {"search": {
                "pageInfo": {"hasNextPage": False},
                "nodes": [{
                    "url": "https://github.com/o/r/pull/5",
                    "title": "PR with threads",
                    "updatedAt": "2026-09-27T10:00:00Z",
                    "reviewThreads": {"nodes": [
                        {"isResolved": True}, {"isResolved": False},
                    ]},
                }],
            }}}, ()
        )
        provider = GitHubProvider(username="testuser", token="fake-token")
        provider._github = instance
        fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))

    assert fetch["ok"] is True
    assert [i["kind"] for i in fetch["items"]] == ["thread_unresolved"]
    assert fetch["items"][0]["id"] == "gh:https://github.com/o/r/pull/5"


def test_github_fetch_failure_marks_not_ok():
    with patch("gitreport.providers.github.Github") as MockGithub:
        instance = MockGithub.return_value
        instance.get_user.return_value.get_notifications.side_effect = RuntimeError("boom")
        provider = GitHubProvider(username="testuser", token="fake-token")
        provider._github = instance
        fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))

    assert fetch["ok"] is False
    assert "boom" in fetch["error"]
    assert fetch["items"] == []
```

Note: `MockIssue`/`MockRepository` already exist at the top of `tests/test_github.py`; the appended tests reuse them. PyGithub search items expose `updated_at`; the existing `MockIssue` class lacks it — modify `MockIssue.__init__` to add `self.updated_at = datetime(2026, 9, 28, tzinfo=UTC)` (a one-line, backwards-compatible edit to the existing mock — add the `datetime` import if the test file lacks it).

- [ ] **Step 2: Run tests to verify they fail**

Run: `rtk poetry run pytest tests/test_github.py -v`
Expected: new tests FAIL (`AttributeError: 'GitHubProvider' object has no attribute 'get_attention'`); existing 5 tests still PASS.

- [ ] **Step 3: Implement `get_attention` in `src/gitreport/providers/github.py`**

Add imports: `from fnmatch import fnmatch` is not needed (use `is_excluded` from base); add `from .base import AttentionFetch, AttentionItem, escape_user, is_excluded, empty_repo_activity, RepoActivity`.

Add module-level constants:

```python
GH_REASON_KIND = {
    "review_requested": "review_requested",
    "mention": "mention",
    "team_mention": "mention",
    "comment": "comment",
    "author": "comment",
    "assign": "issue_assigned",
}

REVIEW_THREADS_QUERY = """
query($q: String!, $cursor: String) {
  search(query: $q, type: ISSUE, first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest {
      url title updatedAt
      reviewThreads(first: 100) { nodes { isResolved } }
    } }
  }
}
"""
```

Add the method to `GitHubProvider`:

```python
    def _item(self, *, mid, provider, kind, origin, repo, title, url, reason,
              updated_at, thread_url=None) -> AttentionItem:
        return AttentionItem(
            id=mid, provider=provider, kind=kind, origin=origin, repo=repo,
            title=escape_user(title), url=url, reason=escape_user(reason),
            updated_at=updated_at, thread_url=thread_url,
        )

    def _notification_items(self, exclusions) -> list[AttentionItem]:
        items: list[AttentionItem] = []
        for n in self._github.get_user().get_notifications(all=False):
            subject_url = getattr(n.subject, "url", "") or ""
            thread_url = getattr(n, "url", "") or ""
            # Derive repo full name from the subject API url:
            # https://api.github.com/repos/OWNER/REPO/pulls/3
            parts = subject_url.split("/repos/")
            full_name = (
                parts[1].rsplit("/", 1)[0]
                if len(parts) == 2
                else (n.repository.full_name if n.repository else "")
            )
            if is_excluded(full_name, exclusions):
                continue
            reason = n.reason
            if reason == "ci_activity":
                if not self._ci_failed(full_name, subject_url):
                    continue
                kind = "ci_failure"
            else:
                kind = GH_REASON_KIND.get(reason)
                if kind is None:
                    continue  # subscribed, state_change, manual, etc.
            title = getattr(n.subject, "title", "") or ""
            # Convert the subject API url to its html url for display:
            # api.github.com/repos/o/r/pulls/3 -> github.com/o/r/pull/3
            html_url = (
                subject_url.replace("api.github.com/repos/", "github.com/")
                .replace("/pulls/", "/pull/")
                .replace("/issues/", "/issues/")
                if subject_url
                else ""
            )
            items.append(self._item(
                mid=f"gh:{subject_url or thread_url}", provider="github",
                kind=kind, origin="notification", repo=full_name, title=title,
                url=html_url, reason=reason,
                updated_at=n.updated_at.isoformat() if n.updated_at else None,
                thread_url=thread_url,
            ))
        return items

    def _ci_failed(self, full_name: str, subject_url: str) -> bool:
        """True when the subject's check runs contain a failure."""
        try:
            number = int(subject_url.rsplit("/", 1)[1])
            pr = self._github.get_repo(full_name).get_pull(number)
            return any(c.conclusion == "failure" for c in pr.get_check_runs())
        except Exception:
            return False  # cannot verify -> skip, never fabricate a failure

    def _search_item(self, issue, kind: str, reason: str,
                     exclusions) -> AttentionItem | None:
        repo_full = issue.repository.full_name
        if is_excluded(repo_full, exclusions):
            return None
        updated = getattr(issue, "updated_at", None)
        return self._item(
            mid=f"gh:{issue.html_url}", provider="github", kind=kind,
            origin="query", repo=repo_full, title=issue.title,
            url=issue.html_url, reason=reason,
            updated_at=updated.isoformat() if updated else None,
        )

    def _unresolved_thread_items(self, exclusions) -> list[AttentionItem]:
        items: list[AttentionItem] = []
        cursor = None
        while True:
            variables = {"q": f"is:pr is:open author:{self._username}"}
            if cursor:
                variables["cursor"] = cursor
            payload, _headers = self._github.requester.graphql_query(
                REVIEW_THREADS_QUERY, variables
            )
            search = payload["data"]["search"]
            for node in search["nodes"]:
                threads = node.get("reviewThreads", {}).get("nodes", [])
                if not any(not t.get("isResolved") for t in threads):
                    continue
                repo_full = node["url"].split("/github.com/")[1].rsplit("/", 1)[0] \
                    if "/github.com/" in node["url"] else ""
                if is_excluded(repo_full, exclusions):
                    continue
                items.append(self._item(
                    mid=f"gh:{node['url']}", provider="github",
                    kind="thread_unresolved", origin="query",
                    repo=repo_full, title=node.get("title", ""),
                    url=node["url"],
                    reason=f"{sum(1 for t in threads if not t.get('isResolved'))} unresolved review thread(s)",
                    updated_at=node.get("updatedAt"),
                ))
            page = search["pageInfo"]
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")
        return items

    def get_attention(
        self,
        since: datetime,
        exclusions: list[str] | None = None,
        state_items: dict[str, dict] | None = None,
        stale_pr_days: int | None = None,
    ) -> AttentionFetch:
        """Fetch GitHub attention items (notifications + queries)."""
        try:
            items: list[AttentionItem] = []
            items.extend(self._notification_items(exclusions))

            date_q = f"is:pr is:open author:{self._username} status:failure"
            for issue in self._github.search_issues(date_q):
                got = self._search_item(issue, "ci_failure", "check failure", exclusions)
                if got:
                    items.append(got)

            issue_q = f"is:issue is:open assignee:{self._username}"
            for issue in self._github.search_issues(issue_q):
                got = self._search_item(issue, "issue_assigned", "assigned to you", exclusions)
                if got:
                    items.append(got)

            if stale_pr_days is not None:
                cutoff = (datetime.now(UTC) - timedelta(days=stale_pr_days)).strftime("%Y-%m-%d")
                stale_q = f"is:pr is:open author:{self._username} updated:<{cutoff}"
                for issue in self._github.search_issues(stale_q):
                    got = self._search_item(issue, "stale_pr", f"no activity for {stale_pr_days}+ days", exclusions)
                    if got:
                        items.append(got)

            items.extend(self._unresolved_thread_items(exclusions))
            return AttentionFetch(ok=True, items=items, resolved_ids=[], error=None)
        except Exception as e:  # noqa: BLE001 — failure must degrade, not crash
            return AttentionFetch(ok=False, items=[], resolved_ids=[], error=str(e))
```

Also add `from datetime import UTC, datetime, timedelta` at the top (replacing the current `from datetime import datetime`), and `from github import Auth, Github, GithubRetry` stays.

- [ ] **Step 4: Run tests to verify they pass**

Run: `rtk poetry run pytest tests/test_github.py -v`
Expected: all PASS (existing 5 + ~8 new).

- [ ] **Step 5: Commit**

```bash
git add src/gitreport/providers/github.py tests/test_github.py
git commit -m "feat: GitHub attention provider (notifications + queries + GraphQL threads)"
```

---

### Task 6: Launchpad attention provider

**Files:**
- Modify: `src/gitreport/providers/launchpad.py`
- Test: `tests/test_launchpad.py` (extend)

**Interfaces:**
- Consumes: `AttentionItem`, `AttentionFetch`, `escape_user`, `is_excluded` (Task 3); `_mp_title`, `_mp_repo`, `_same_person` helpers already in the module.
- Produces: `LaunchpadProvider.get_attention(since, exclusions=None, state_items=None, stale_pr_days=None) -> AttentionFetch`.

**Behaviour (spec "Launchpad specifics"):**
1. Same outer `try/except Exception` → `ok=False` pattern as GitHub.
2. MPs requesting my review: `person.getRequestedReviews()` — **default status** (Needs review; no `status=ALL_MP_STATUSES` here, unlike the activity provider) → kind `lp_mp_needs_review`.
3. My open MPs with new comments/votes since `since`: `person.getMergeProposals()`, filter `mp.date_created <= since` is wrong — instead iterate votes/comments with `date_created > since` and author != me → kind `lp_mp_comment` with `updated_at` = latest such event.
4. Bugs assigned to me: `self._launchpad.bugs.searchTasks(assignee=person)` (open statuses default; **no** `modified_since`) → kind `issue_assigned` (GH parity), `updated_at = bug.date_last_updated`.
5. Subscribed bugs with activity: `self._launchpad.bugs.searchTasks(bug_subscriber=person, modified_since=since.isoformat())` → kind `lp_bug_activity`, filter out events authored by me (self-activity rule) by checking `bug.date_last_updated`; the provider cannot cheaply attribute bug events, so the *record* keeps `updated_at = date_last_updated` and the merge layer's new-event rule (`updated_at > last_updated`) decides reopens — self-authored comments on my own subscribed bugs will re-open; accepted v1 limitation (LP has no per-comment author in `searchTasks` results).
6. Exclusions: `is_excluded(bug.bug_target_name or mp project, exclusions)`.
7. `resolved_ids`: for ids in `state_items` (provider `launchpad`), `lp.load(url)` and check closing state — MP `queue_status in ("Merged", "Superseded", "Rejected")`... actually MP closing states per spec are merged/abandoned; bug tasks `status in CLOSED_BUG_STATUSES`. Items proven closed → append id to `resolved_ids`. Load failures are skipped (not proof).
8. All titles via `_mp_title` / `bug.title`, escaped with `escape_user`; ids `lp:{web_link}`.

- [ ] **Step 1: Write the failing tests (append to tests/test_launchpad.py)**

First read the existing mock helpers in `tests/test_launchpad.py` and reuse their patterns (they mock `Launchpad.login_with`). Add:

```python
# --- Attention tests ---


class MockMp:
    def __init__(self, web_link, project="~u/+git/repo", votes=None,
                 date_created=None, registrant=None):
        self.web_link = web_link
        self.queue_status = "Needs review"
        self.votes = votes or []
        self.date_created = date_created or datetime(2026, 1, 1, tzinfo=UTC)
        self.registrant = registrant
        # _mp_repo() reads target_git_repository.unique_name
        self.target_git_repository = MagicMock(
            unique_name=f"~u/+git/{project}", private=False)


def test_lp_mp_needs_review():
    provider = _lp_provider()
    provider._launchpad.me.getRequestedReviews.return_value = [
        MockMp("https://launchpad.net/~u/+git/repo/+merge/1")
    ]
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))
    assert fetch["ok"] is True
    kinds = [i["kind"] for i in fetch["items"]]
    assert "lp_mp_needs_review" in kinds
    # Exclusions apply to the MP's repo name.
    provider._launchpad.me.getRequestedReviews.return_value = [
        MockMp("https://launchpad.net/~u/+git/noise/+merge/2",
               project="noise")
    ]
    fetch = provider.get_attention(
        datetime(2026, 9, 28, tzinfo=UTC), exclusions=["*noise*"])
    assert fetch["items"] == []


def test_lp_assigned_bugs_no_time_window():
    provider = _lp_provider()
    # A bug with an old date_last_updated must still appear (parity with GH).
    provider._launchpad.bugs.searchTasks.side_effect = _bug_search_side_effect(
        assigned=[_bug_mock("proj", date_last_updated=datetime(2026, 1, 1, tzinfo=UTC))],
        subscribed=[],
    )
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))
    kinds = [i["kind"] for i in fetch["items"]]
    assert "issue_assigned" in kinds


def test_lp_resolved_ids_proven_closed():
    provider = _lp_provider()
    provider._launchpad.bugs.searchTasks.side_effect = _bug_search_side_effect()
    closed_bug = _bug_mock("proj", status="Fix Released")
    provider._launchpad.load.return_value = closed_bug
    state_items = {"lp:https://launchpad.net/bugs/77": {
        "url": "https://launchpad.net/bugs/77", "provider": "launchpad"}}
    fetch = provider.get_attention(
        datetime(2026, 9, 28, tzinfo=UTC), state_items=state_items)
    assert "lp:https://launchpad.net/bugs/77" in fetch["resolved_ids"]


def test_lp_fetch_failure_marks_not_ok():
    provider = _lp_provider()
    provider._launchpad.me.getRequestedReviews.side_effect = RuntimeError("lp down")
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))
    assert fetch["ok"] is False
    assert "lp down" in fetch["error"]
```

The test helpers (concrete code — read `tests/test_launchpad.py` first to confirm the existing fixtures don't already define equivalents; if they do, reuse those instead):

```python
def _lp_provider():
    with patch("gitreport.providers.launchpad.Launchpad"):
        provider = LaunchpadProvider(username="testuser", token=None)
        provider._launchpad.me = MagicMock()
        provider._launchpad.me.getMergeProposals.return_value = []
        provider._launchpad.bugs = MagicMock()
        provider._launchpad.bugs.searchTasks.return_value = []
        provider._launchpad.load = MagicMock(return_value=None)
        return provider


def _bug_mock(project, status="New", date_last_updated=None):
    bug = MagicMock()
    bug.bug_target_name = project
    bug.web_link = "https://launchpad.net/bugs/77"
    bug.title = "Bug 77"
    bug.status = status
    bug.date_last_updated = date_last_updated or datetime(2026, 9, 27, tzinfo=UTC)
    bug.bug_target = MagicMock(private=False)
    return bug


def _bug_search_side_effect(assigned=None, subscribed=None):
    def side_effect(**kwargs):
        if "assignee" in kwargs:
            return list(assigned or [])
        if "bug_subscriber" in kwargs:
            return list(subscribed or [])
        return []
    return side_effect
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `rtk poetry run pytest tests/test_launchpad.py -v`
Expected: new tests FAIL (`get_attention` missing); existing tests still PASS.

- [ ] **Step 3: Implement `get_attention` in `src/gitreport/providers/launchpad.py`**

Add import: `from .base import AttentionFetch, AttentionItem, escape_user, is_excluded, RepoActivity, empty_repo_activity`.

Add to `LaunchpadProvider`:

```python
    MP_CLOSED_STATUSES = ("Merged", "Superseded", "Rejected")

    def get_attention(
        self,
        since: datetime,
        exclusions: list[str] | None = None,
        state_items: dict[str, dict] | None = None,
        stale_pr_days: int | None = None,
    ) -> AttentionFetch:
        """Fetch Launchpad attention items (person-scoped queries)."""
        try:
            person = self._launchpad.me
            items: list[AttentionItem] = []

            # 1. MPs requesting my review (default status: Needs review).
            for mp in person.getRequestedReviews():
                name, _visibility = _mp_repo(mp)
                if is_excluded(name, exclusions):
                    continue
                items.append(self._mp_item(mp, name, "lp_mp_needs_review",
                                           "review requested from you",
                                           mp.date_created))

            # 2. My MPs with new comments/votes since `since`.
            for mp in person.getMergeProposals():
                name, _visibility = _mp_repo(mp)
                if is_excluded(name, exclusions):
                    continue
                latest = self._latest_foreign_event(mp, person, since)
                if latest is not None:
                    items.append(self._mp_item(
                        mp, name, "lp_mp_comment",
                        "new comment/vote on your merge proposal", latest))

            # 3. Bugs assigned to me (open; no time window — GH parity).
            for bug in self._launchpad.bugs.searchTasks(assignee=person):
                if is_excluded(bug.bug_target_name, exclusions):
                    continue
                items.append(self._bug_item(bug, "issue_assigned",
                                            "assigned to you"))

            # 4. Subscribed bugs with activity since `since`.
            for bug in self._launchpad.bugs.searchTasks(
                bug_subscriber=person, modified_since=since.isoformat()
            ):
                if is_excluded(bug.bug_target_name, exclusions):
                    continue
                items.append(self._bug_item(bug, "lp_bug_activity",
                                            "new activity on subscribed bug"))

            return AttentionFetch(
                ok=True, items=items,
                resolved_ids=self._proven_resolved(state_items),
                error=None)
        except Exception as e:  # noqa: BLE001
            return AttentionFetch(ok=False, items=[], resolved_ids=[], error=str(e))

    def _mp_item(self, mp, repo_name, kind, reason, updated_at) -> AttentionItem:
        return AttentionItem(
            id=f"lp:{mp.web_link}", provider="launchpad", kind=kind,
            origin="query", repo=repo_name, title=escape_user(_mp_title(mp)),
            url=mp.web_link, reason=escape_user(reason),
            updated_at=updated_at.isoformat() if updated_at else None,
            thread_url=None)

    def _bug_item(self, bug, kind, reason) -> AttentionItem:
        return AttentionItem(
            id=f"lp:{bug.web_link}", provider="launchpad", kind=kind,
            origin="query", repo=bug.bug_target_name,
            title=escape_user(bug.title), url=bug.web_link,
            reason=escape_user(reason),
            updated_at=(bug.date_last_updated.isoformat()
                        if getattr(bug, "date_last_updated", None) else None),
            thread_url=None)

    def _latest_foreign_event(self, mp, person, since) -> datetime | None:
        """Latest comment/vote on `mp` after `since` by someone other than me."""
        latest: datetime | None = None
        for vote in mp.votes:
            if _same_person(vote.reviewer, person):
                continue
            comment = getattr(vote, "comment", None)
            when = getattr(comment, "date_created", None) if comment else None
            if when and when > since and (latest is None or when > latest):
                latest = when
        return latest

    def _proven_resolved(self, state_items: dict[str, dict] | None) -> list[str]:
        """Re-load open LP items from state; report ids proven closed."""
        resolved: list[str] = []
        for mid, rec in (state_items or {}).items():
            if not mid.startswith("lp:") or rec.get("status") != "open":
                continue
            try:
                obj = self._launchpad.load(rec["url"])
            except Exception:
                continue  # load failure is not proof
            status = getattr(obj, "status", None) or getattr(obj, "queue_status", None)
            if status in CLOSED_BUG_STATUSES or status in self.MP_CLOSED_STATUSES:
                resolved.append(mid)
        return resolved
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `rtk poetry run pytest tests/test_launchpad.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gitreport/providers/launchpad.py tests/test_launchpad.py
git commit -m "feat: Launchpad attention provider (MPs, bugs, proven resolutions)"
```

---

### Task 7: html_report.py — HTML renderer

**Files:**
- Create: `src/gitreport/html_report.py`
- Test: `tests/test_html_report.py`

**Interfaces:**
- Consumes: the `markdown` library (Task 1).
- Produces (used by Task 8):
  - `strip_front_matter(md_text: str) -> tuple[dict, str]` — parses the `---`-fenced front matter into `{"generated_at": ..., "coverage_start": ...}` and returns `(meta, body_without_front_matter)`
  - `replace_status_line(md_text: str, new_status: str) -> str` — replaces the single line beginning with `Status:` in the cached Markdown (sentinel per spec)
  - `render_html(md_text: str, title: str = "GitReport digest") -> str` — strips front matter, converts body via `markdown.markdown(body, output_format="html")`, wraps in a minimal inline-CSS template. No JS, no external references.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_html_report.py`:

```python
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
    assert out.replace("Status: Reviewed Mon 28 Sep 09:14",
                       "Status: NOT YET REVIEWED — run `gitreport read` after reviewing") == MD


def test_render_html_self_contained():
    html = render_html(MD)
    assert html.startswith("<!DOCTYPE html>")
    assert "<script" not in html
    assert "src=" not in html  # no external references
    assert "href=" not in html or "href" not in html.split("<body")[1]  # only inline CSS
    assert "GitReport digest" in html


def test_render_html_hostile_title():
    hostile = MD.replace("[T](https://github.com/o/r/pull/1)",
                         "[<script>alert(1)</script>](javascript:alert(1)) "
                         "![x](https://tracker/pixel)")
    html = render_html(hostile)
    assert "<script>alert" not in html
    assert "javascript:" not in html
    assert "<img" not in html
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `rtk poetry run pytest tests/test_html_report.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/gitreport/html_report.py`**

```python
"""Self-contained HTML digest rendering from the cached Markdown."""

import re

import markdown

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


def strip_front_matter(md_text: str) -> tuple[dict, str]:
    """Split `---`-fenced front matter from the Markdown body."""
    match = re.match(r"\A---\n(.*?)\n---\n", md_text, re.DOTALL)
    if not match:
        return {}, md_text
    meta: dict = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        if key and value:
            meta[key.strip()] = value.strip()
    return meta, md_text[match.end():]


def replace_status_line(md_text: str, new_status: str) -> str:
    """Replace the single `Status:` sentinel line in the cached Markdown."""
    return re.sub(r"^Status: .*$", new_status, md_text, count=1, flags=re.MULTILINE)


def render_html(md_text: str, title: str = "GitReport digest") -> str:
    """Convert the digest Markdown to a self-contained HTML document."""
    _meta, body = strip_front_matter(md_text)
    rendered = markdown.markdown(body, output_format="html")
    return _TEMPLATE.format(title=title, body=rendered)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `rtk poetry run pytest tests/test_html_report.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gitreport/html_report.py tests/test_html_report.py
git commit -m "feat: self-contained HTML digest renderer"
```

---

### Task 8: CLI wiring — digest, attention, read, ack, unack

**Files:**
- Modify: `src/gitreport/main.py`
- Test: `tests/test_main.py` (extend)

**Interfaces:**
- Consumes: everything above (Tasks 1–7).
- Produces: five new click commands on `cli`:
  - `attention [--config]` — fetch, merge with in-memory state view (items not in state treated as New, never persisted), print via `render_attention_stdout`. Read-only.
  - `digest [--config] [--open]` — full morning run: fetch outside lock; lock; reload; merge (`merge_into_state`); save; write dated `.md` (always) + `.html` (if enabled); refresh `latest.*` symlinks atomically; unlock; print Markdown; `--open` opens the digest. Marks provider sections stale on failure. Bad credentials are a per-provider failure (warning), never an abort.
  - `read [--config] [--open]` — under lock: `record_read(state, newest_digest_generation_ts, now)`; re-stamp every digest generated after the previous `last_reviewed` via `replace_status_line` + `render_html`; unlock; `--open` opens `latest.html` (or `.md` when html disabled).
  - `ack [ID] [--config] [--list]` — under lock: substring match on id/url/title/repo among `status == "open"` items; interactive disambiguation when ambiguous (`click.prompt` with numbered choices); sets `status=acked, acked=True, acked_at=now, pinned=False`; best-effort mark-thread-read on GitHub for notification items; `--list` or no ID lists open items.
  - `unack <id-or-substring> [--config]` — under lock: sets `status=open, acked=False, acked_at=None, reopen_count+=1`; a previously-resolved item additionally gets `pinned=True`.
  - Shared helper `_open_in_browser(path: Path)` — `webbrowser.open` when not a bare terminal (`BROWSER`/DISPLAY check is unnecessary; `webbrowser.open` no-ops safely), used by `digest --open` and `read --open`.
  - Shared helper `_resolve_digest_paths(config) -> tuple[Path, Path]` — returns (dated_stem, latest_stem) with the `%Y-%m-%d` date substituted for today's local date.

- [ ] **Step 1: Write the failing tests (append to tests/test_main.py)**

```python
# --- New command tests ---

from gitreport.attention import render_digest_markdown  # noqa: E402
from gitreport.state import AttentionState  # noqa: E402


class NoopProvider(GitProvider):
    get_activity = MagicMock()
    get_attention = MagicMock(return_value={
        "ok": True, "items": [], "resolved_ids": [], "error": None})

    def __init__(self, username, token):
        pass


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_digest_creates_files_and_symlinks(mock_load_config, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("digest")
    state_path = tmp / "state.json"
    stem = tmp / "digests" / "2026-09-28"
    latest = tmp / "digests" / "latest"
    mock_load_config.return_value = _config_with(state_path=state_path, stem=stem,
                                                 latest=latest)

    runner = CliRunner()
    result = runner.invoke(cli, ["digest"])

    assert result.exit_code == 0, result.output
    assert (tmp / "digests" / "2026-09-28.md").exists()
    assert (tmp / "digests" / "2026-09-28.html").exists()
    assert latest.with_suffix(".md").is_symlink()
    assert latest.with_suffix(".html").is_symlink()
    assert "Status: NOT YET REVIEWED" in (tmp / "digests" / "2026-09-28.md").read_text()


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_digest_provider_failure_degrades(mock_load_config, tmp_path_factory):
    class BrokenProvider(GitProvider):
        get_activity = MagicMock()
        get_attention = MagicMock(return_value={
            "ok": False, "items": [], "resolved_ids": [], "error": "boom"})

        def __init__(self, username, token):
            pass

    tmp = tmp_path_factory.mktemp("digest2")
    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json",
        stem=tmp / "digests" / "2026-09-28",
        latest=tmp / "digests" / "latest",
    )
    with patch("gitreport.main.PROVIDER_MAP", {"mock": BrokenProvider}):
        runner = CliRunner()
        result = runner.invoke(cli, ["digest"])

    assert result.exit_code == 0  # degraded, not aborted
    assert "stale" in result.output.lower()


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_read_advances_cursor_and_restamps(mock_load_config, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("read")
    state_path = tmp / "state.json"
    stem = tmp / "digests" / "2026-09-28"
    latest = tmp / "digests" / "latest"
    mock_load_config.return_value = _config_with(state_path=state_path, stem=stem,
                                                 latest=latest)

    runner = CliRunner()
    runner.invoke(cli, ["digest"])  # generate first
    result = runner.invoke(cli, ["read"])

    assert result.exit_code == 0, result.output
    md_text = (tmp / "digests" / "2026-09-28.md").read_text()
    assert "Status: Reviewed" in md_text
    assert "NOT YET REVIEWED" not in md_text
    import json

    state = json.loads(state_path.read_text())
    assert state["last_reviewed"] is not None
    assert state["reviewed_at"] is not None


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_ack_and_unack_roundtrip(mock_load_config, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ack")
    item_id = "gh:https://github.com/o/r/pull/1"
    state = AttentionState(last_digest_run="2026-09-28T06:00:00+00:00",
                           last_reviewed="2026-09-27T06:00:00+00:00")
    # Seed state directly via save_state in the test.
    from gitreport.state import save_state

    state.items[item_id] = _seed_item(item_id)
    save_state(tmp / "state.json", state)

    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json",
        stem=tmp / "digests" / "2026-09-28",
        latest=tmp / "digests" / "latest",
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["ack", "o/r/pull/1"])
    assert result.exit_code == 0, result.output
    import json

    acked_state = json.loads((tmp / "state.json").read_text())
    assert acked_state["items"][item_id]["status"] == "acked"

    result = runner.invoke(cli, ["unack", "o/r/pull/1"])
    assert result.exit_code == 0, result.output
    import json

    reopened = json.loads((tmp / "state.json").read_text())
    assert reopened["items"][item_id]["status"] == "open"
    assert reopened["items"][item_id]["reopen_count"] == 1


def _seed_item(item_id):
    from gitreport.state import ItemState

    return ItemState(
        status="open", origins=["query"], kinds=["mention"], reasons=["r"],
        repo="o/r", title="T", url="https://github.com/o/r/pull/1",
        first_seen="2026-09-28T06:00:00+00:00",
        last_updated="2026-09-28T06:00:00+00:00")


def _config_with(state_path, stem, latest):
    from gitreport.config import AttentionConfig, Config, ProviderConfig

    return Config(
        providers={"mock": ProviderConfig(username="u", token="t")},
        attention=AttentionConfig(state_path=state_path, digest_output=stem,
                                  digest_latest=latest),
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `rtk poetry run pytest tests/test_main.py -v`
Expected: 4 new tests FAIL (`No such command 'digest'`).

- [ ] **Step 3: Implement the commands in `src/gitreport/main.py`**

Add imports:

```python
import json
import os
import re
import webbrowser
from datetime import UTC, datetime, timedelta

from .attention import (
    build_report,
    dedupe,
    merge_into_state,
    render_attention_stdout,
    render_digest_markdown,
)
from .html_report import replace_status_line, render_html, strip_front_matter
from .state import (
    AttentionState,
    ItemState,
    load_state,
    prune,
    record_read,
    save_state,
    state_lock,
)
from github import BadCredentialsException
```

Add module-level helpers:

```python
def _build_providers(config) -> dict[str, tuple[GitProvider, str]]:
    """Instantiate providers; returns name -> (provider, display_name)."""
    providers = {}
    for name, pc in config.providers.items():
        cls = PROVIDER_MAP.get(name)
        if cls is None:
            continue
        token = pc.token.get_secret_value() if pc.token is not None else None
        providers[name] = (cls(username=pc.username, token=token), name)
    return providers


def _fetch_attention(providers, since, exclusions, state_items, stale_pr_days):
    """Fetch attention items from every provider; network, outside the lock.

    Returns (results, stale_providers). A provider failure yields
    ok=False and the provider name in stale_providers.
    """
    results = {}
    stale = []
    for name, (provider, _display) in providers.items():
        try:
            fetch = provider.get_attention(
                since, exclusions=exclusions, state_items=state_items,
                stale_pr_days=stale_pr_days)
        except Exception as e:  # noqa: BLE001
            fetch = {"ok": False, "items": [], "resolved_ids": [], "error": str(e)}
        results[name] = fetch
        if not fetch["ok"]:
            stale.append(name)
    return results, stale


def _fetch_activity(providers, start, end, fast=False):
    """Fetch activity from every provider; degrade on failure, never abort.

    Returns (provider_data: dict[str, dict[str, RepoActivity]], stale: list).
    Bad credentials are a per-provider failure here (warning + skip) — only
    the standalone `generate` command aborts on them (non-goal to change).
    """
    provider_data: dict = {}
    stale: list[str] = []
    for name, (provider, _display) in providers.items():
        try:
            provider_data[name] = provider.get_activity(start, end, fast=fast)
        except BadCredentialsException:
            click.echo(
                f"Warning: bad credentials for {name}; its activity is "
                "missing from this digest.",
                err=True,
            )
            stale.append(name)
        except Exception as e:  # noqa: BLE001
            click.echo(
                f"Warning: could not fetch activity from {name} ({e}); "
                "its section may be stale.",
                err=True,
            )
            stale.append(name)
    return provider_data, stale
```

`_fetch_activity` mirrors `generate`'s degrade loop but returns `(provider_data, stale)` instead of aborting on bad credentials.

Then the commands (Task 8 Step 3, part 1 — command definitions):

```python
@cli.command()
@click.option("--config", "config_path_str", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def attention(config_path_str):
    """Show what needs attention (dry run; does not mutate state)."""
    config = load_config(config_path_str)
    att = config.attention
    state = load_state(att.state_path)
    providers = _build_providers(config)
    results, stale = _fetch_attention(
        providers, datetime.now(UTC), att.exclusions.get("github", []) + att.exclusions.get("launchpad", []),
        {mid: rec.model_dump() for mid, rec in state.items.items()},
        att.stale_pr_days)
    merged = dedupe(results)
    # Dry-run view: overlay merged items on the state without persisting.
    view = merge_into_state(
        {**state.model_dump(), "last_digest_run": None}, merged, set(), _now_iso())
    click.echo(render_attention_stdout(view, stale, datetime.now(UTC)))


@cli.command()
@click.option("--config", "config_path_str", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--open", "open_browser", is_flag=True, default=False)
def digest(config_path_str, open_browser):
    """Morning run: attention + activity; writes the digest; updates state."""
    config = load_config(config_path_str)
    att = config.attention
    providers = _build_providers(config)

    # Snapshot for fetches (outside the lock).
    pre_state = load_state(att.state_path)
    generated_at = datetime.now(UTC).isoformat()
    since = (datetime.fromisoformat(pre_state.last_reviewed)
             if pre_state.last_reviewed
             else datetime.now(UTC) - timedelta(hours=24))
    exclusions = att.exclusions.get("github", []) + att.exclusions.get("launchpad", [])
    state_items = {mid: rec.model_dump() for mid, rec in pre_state.items.items()}
    results, stale = _fetch_attention(providers, since, exclusions, state_items,
                                      att.stale_pr_days)
    activity_data, activity_stale = _fetch_activity(
        providers, since, datetime.now(UTC))

    merged = dedupe(results)
    resolved_ids = set().union(*(set(r.get("resolved_ids", [])) for r in results.values()))

    with state_lock(att.state_path):
        state = load_state(att.state_path)  # reload under lock
        ok_providers = {name for name, fetch in results.items() if fetch["ok"]}
        new_state = merge_into_state(
            state.model_dump(), merged, ok_providers, generated_at, resolved_ids)
        new_state_obj = AttentionState.model_validate(new_state)
        prune(new_state_obj, datetime.now(UTC))
        save_state(att.state_path, new_state_obj)

        # Digest files + symlinks (inside the lock).
        activity_md = generate_report(activity_data)
        coverage_start = (pre_state.last_reviewed
                          or (datetime.now(UTC) - timedelta(hours=24)).isoformat())
        md_text = render_digest_markdown(
            new_state, activity_md, coverage_start, generated_at,
            stale + activity_stale)
        stem = Path(str(att.digest_output).replace(
            "YYYY-MM-DD", datetime.now().astimezone().strftime("%Y-%m-%d")))
        stem.parent.mkdir(parents=True, exist_ok=True)
        (stem.with_suffix(".md")).write_text(md_text)
        if "html" in att.digest_formats:
            (stem.with_suffix(".html")).write_text(render_html(md_text))
        _refresh_symlinks(stem, att.digest_latest, att.digest_formats)

    click.echo(strip_front_matter(md_text)[1])
    if open_browser:
        _open_in_browser(stem.with_suffix(".html" if "html" in att.digest_formats else ".md"))


@cli.command()
@click.option("--config", "config_path_str", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--open", "open_browser", is_flag=True, default=False)
def read(config_path_str, open_browser):
    """Mark the newest digest(s) consumed and re-render their status line."""
    config = load_config(config_path_str)
    att = config.attention
    now = datetime.now(UTC).isoformat()
    with state_lock(att.state_path):
        state = load_state(att.state_path)
        previous = state.last_reviewed
        newest_ts, targets = _digests_generated_after(att.digest_output, previous)
        if newest_ts is None:
            click.echo("No digests to mark read.")
            return
        record_read(state, newest_ts, now)
        save_state(att.state_path, state)
        for path in targets:
            text = path.read_text()
            stamped = replace_status_line(text, f"Status: Reviewed {now}")
            path.write_text(stamped)
            if path.suffix == ".md" and "html" in att.digest_formats:
                path.with_suffix(".html").write_text(render_html(stamped))
    if open_browser:
        suffix = ".html" if "html" in att.digest_formats else ".md"
        _open_in_browser(att.digest_latest.with_suffix(suffix))


@cli.command(name="ack")
@click.argument("item_id", required=False)
@click.option("--config", "config_path_str", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--list", "list_items", is_flag=True, default=False)
def ack(item_id, config_path_str, list_items):
    """Mark item(s) done. ID is an id or substring; --list browses."""
    config = load_config(config_path_str)
    att = config.attention
    with state_lock(att.state_path):
        state = load_state(att.state_path)
        open_items = {mid: r for mid, r in state.items.items()
                      if r.status == "open"}
        if list_items or item_id is None:
            _print_items(open_items)
            return
        matches = _match_items(open_items, item_id)
        if len(matches) > 1:
            chosen = _disambiguate(matches)
            if chosen is None:
                return
            matches = [chosen]
        for mid in matches:
            rec = state.items[mid]
            rec.status = "acked"
            rec.acked = True
            rec.acked_at = datetime.now(UTC).isoformat()
            rec.pinned = False
            if "notification" in rec.origins and rec.thread_url:
                _mark_thread_read(providers, rec.thread_url)  # best-effort
        save_state(att.state_path, state)
    click.echo(f"Acked {len(matches)} item(s).")


@cli.command()
@click.argument("item_id")
@click.option("--config", "config_path_str", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def unack(item_id, config_path_str):
    """Pull item(s) back into the inbox."""
    config = load_config(config_path_str)
    att = config.attention
    with state_lock(att.state_path):
        state = load_state(att.state_path)
        matches = _match_items(state.items, item_id)
        for mid in matches:
            rec = state.items[mid]
            was_resolved = rec.status == "resolved"
            rec.status = "open"
            rec.acked = False
            rec.acked_at = None
            rec.reopen_count += 1
            if was_resolved:
                rec.pinned = True
        save_state(att.state_path, state)
    click.echo(f"Un-acked {len(matches)} item(s).")
```

Plus the small private helpers referenced above:

```python
def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _open_in_browser(path: Path) -> None:
    if path.exists():
        webbrowser.open(f"file://{path}")


def _refresh_symlinks(dated_stem: Path, latest_stem: Path, formats: list[str]) -> None:
    """Atomically point latest.<ext> at the newest digest files."""
    for ext in formats:
        dated = dated_stem.with_suffix(ext)
        latest = latest_stem.with_suffix(ext)
        latest.parent.mkdir(parents=True, exist_ok=True)
        tmp = latest.with_name(latest.name + ".tmp-link")
        if tmp.exists() or tmp.is_symlink():
            tmp.unlink()
        os.symlink(dated, tmp)
        os.replace(tmp, latest)
```

- [ ] **Step 3 (continued): remaining helpers in `src/gitreport/main.py`**

```python
def _match_items(items: dict, needle: str) -> list[str]:
    """Ids whose id/url/title/repo contain the needle (case-insensitive)."""
    n = needle.lower()
    return [mid for mid, r in items.items()
            if n in mid.lower() or n in (r.title or "").lower()
            or n in (r.repo or "").lower() or n in (r.url or "").lower()]


def _print_items(items: dict) -> None:
    if not items:
        click.echo("Inbox is empty.")
        return
    for mid, r in items.items():
        click.echo(f"{mid}\n    {r.title}  [{', '.join(r.kinds)}]")


def _disambiguate(matches: list[str]) -> str | None:
    click.echo("Multiple items match:")
    for i, mid in enumerate(matches, 1):
        click.echo(f"  {i}. {mid}")
    answer = click.prompt("Number to ack (empty to cancel)", default="", show_default=False)
    if not answer or not answer.isdigit() or not (1 <= int(answer) <= len(matches)):
        return None
    return matches[int(answer) - 1]


def _mark_thread_read(providers: dict, thread_url: str) -> None:
    """Best-effort: PATCH the notification thread as read via the requester.

    thread_url is the notification THREAD api url
    (https://api.github.com/notifications/threads/{id}) — the same endpoint
    PyGithub's own Notification.mark_as_read() PATCHes. Any failure is
    logged and tolerated: the local ack still applies.
    """
    try:
        gh_provider = providers.get("github")
        if gh_provider is None or not thread_url:
            return
        requester = gh_provider._github.requester
        requester.requestJsonAndCheck("PATCH", thread_url)
    except Exception as e:  # noqa: BLE001
        click.echo(f"Warning: could not mark thread read ({e}).", err=True)
```

`_digests_generated_after(dated_stem_pattern: Path, previous_reviewed: str | None) -> tuple[str | None, list[Path]]` — final behaviour: glob the parent dir for `.md` files matching the dated stem pattern, parse each file's front matter `generated_at`, return the newest `generated_at` and the paths of every digest generated after `previous_reviewed` (only the newest when `previous_reviewed is None`, per spec). This is the function to use, not a sketch:

```python
def _digests_generated_after(pattern: Path, previous: str | None):
    parent = pattern.parent
    stem_re = re.compile(re.escape(pattern.name).replace(
        re.escape("YYYY-MM-DD"), r"\d{4}-\d{2}-\d{2}") + r"\.md$")
    digests = []
    for path in parent.glob("*.md"):
        if not stem_re.match(path.name):
            continue
        meta, _body = strip_front_matter(path.read_text())
        ts = meta.get("generated_at")
        if ts:
            digests.append((ts, path))
    digests.sort()
    if not digests:
        return None, []
    newest_ts, newest_path = digests[-1]
    if previous is None:
        return newest_ts, [newest_path]
    targets = [p for ts, p in digests if ts > previous]
    return newest_ts, targets
```

Note the ordering subtlety: ISO-8601 UTC timestamps with the same offset sort correctly as plain strings, so `digests.sort()` on `(ts, path)` tuples is safe. `read` sets `last_reviewed = newest_ts` (the newest digest's generation time), so any event after that digest's generation lands in the next coverage window.

- [ ] **Step 4: Run tests to verify they pass**

Run: `rtk poetry run pytest tests/test_main.py -v`
Expected: 4 new PASS + 3 existing PASS.

- [ ] **Step 5: Run the full suite + lint**

```bash
rtk poetry run pytest -q
rtk poetry run ruff check src tests
rtk poetry run black --check src tests
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/gitreport/main.py tests/test_main.py
git commit -m "feat: digest/attention/read/ack/unack CLI commands"
```

---

### Task 9: README + scheduling docs + example config

**Files:**
- Modify: `README.md`
- Modify: `config.example.yaml`

**Interfaces:**
- Consumes: final CLI surface.
- Produces: user-facing docs.

- [ ] **Step 1: Update README.md**

Add sections (after the existing Usage section):

- **Attention digest** — the five new commands with one-line descriptions; the coverage/status banner explanation; `gitreport read` marks consumed (cursor = newest digest's generation time).
- **Scheduling** — systemd timer unit with `Persistent=true`:

```ini
# ~/.config/systemd/user/gitreport-digest.service
[Unit]
Description=GitReport daily digest

[Service]
Type=oneshot
ExecStart=%h/.local/pipx/venvs/gitreport/bin/gitreport digest

# ~/.config/systemd/user/gitreport-digest.timer
[Unit]
Description=Run gitreport digest every morning

[Timer]
OnCalendar=*-*-* 07:30:00
Persistent=true

[Install]
WantedBy=timers.target
```

plus `systemctl --user enable --now gitreport-digest.timer`, and the cron equivalent `30 7 * * * $HOME/.local/pipx/venvs/gitreport/bin/gitreport digest`. Note the missed-schedule/catch-up behaviour and the GH notifications ~3-month expiry caveat. Note the classic PAT + `notifications` scope requirement.
- **Config reference** — the `attention:` block keys with defaults.
- **State file** — location, what it stores, corruption behaviour, prune rule.

- [ ] **Step 2: Update `config.example.yaml`**

Add a commented `attention:` block matching the spec's Config section.

- [ ] **Step 3: Verify docs build cleanly (no test changes needed)**

Run: `rtk poetry run pytest -q`
Expected: all PASS (docs task touches no code).

- [ ] **Step 4: Commit**

```bash
git add README.md config.example.yaml
git commit -m "docs: attention digest usage, scheduling, config reference"
```

---

### Task 10: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Full suite + lint + typecheck**

```bash
rtk poetry run pytest -q
rtk poetry run ruff check src tests
rtk poetry run black --check src tests
```

Expected: all green, zero failures.

- [ ] **Step 2: Manual smoke test (mocked, via the test suite)**

The smoke path is covered by `tests/test_main.py` (`digest` → files + symlinks; `read` → cursor + restamp; `ack`/`unack` roundtrip; provider-failure degradation). Re-run the suite and confirm; if a real-network smoke run is desired, run `poetry run gitreport attention --config <path>` with a real config and confirm exit 0 and a rendered report (optional, needs credentials).

- [ ] **Step 3: Summarise**

Report: files created/modified, test counts before/after, any deviations from the spec (with reasons), and the manual-smoke result.

---

## Plan self-review notes

- Spec coverage: config (T1), state+flock (T2), item model/protocol (T3), merge/lifecycle/render (T4), GH provider (T5), LP provider (T6), HTML renderer (T7), CLI (T8), docs (T9). Every spec section maps to a task; non-goals untouched.
- Known deliberate simplifications (record in Task 10 summary): `attention`'s dry-run overlay reuses `merge_into_state` with `ok_providers=set()` so nothing resolves; LP self-authored bug events can re-open items (LP API limitation, documented in T6); `_digests_generated_after` parses front matter rather than maintaining an index.
- Type consistency: `AttentionFetch` dict shape is `{"ok", "items", "resolved_ids", "error"}` everywhere; state records use `provider` (singular, the owning provider) while merged items in `dedupe` output carry `provider` for new records and `origins` for resolution logic — resolution keys off `provider in ok_providers` (single-owner records) which matches how providers tag items.
- `thread_url` semantics verified against the installed PyGithub: `Notification.url` is the thread endpoint (`/notifications/threads/{id}`), `subject.url` is the subject endpoint; `mark-as-read` PATCHes the thread url via `requester.requestJsonAndCheck("PATCH", thread_url)` because `Github.get_notification` does not exist.
- `Github.requester` (public property) and `Requester.graphql_query(query, variables) -> (dict, dict)` verified present in the installed version; `pygithub ^2.6` floor keeps that guarantee.
- LP test helpers mock `Launchpad.login_with` via patch context; `_mp_repo` needs `mp.target_git_repository.unique_name`, provided by `MockMp`.
- The `attention` dry-run overlay calls `merge_into_state(..., ok_providers=set(), ...)` so no absence-based resolution occurs in the read-only view; `last_digest_run` is not persisted because the result is never saved.

