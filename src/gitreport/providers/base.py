import html as _html
from datetime import datetime
from fnmatch import fnmatch
from typing import Protocol, TypedDict

# The categories of change activity that a repository can accumulate, in the
# order they should appear in the report. Kept in one place so the providers,
# the reporting layer and the empty-repo check stay in sync.
ACTIVITY_CATEGORIES = (
    "prs_submitted",
    "prs_reviewed",
    "prs_merged",
    "issues_created",
    "issues_closed",
)


class ActivityItem(TypedDict, total=False):
    """Represents a single activity item (e.g., a PR/MP or an issue/bug)."""

    title: str
    url: str
    # Set on a *reviewed* item when the configured user also merged it. The
    # reporting layer renders such items inline (e.g. "-> merged, too") instead
    # of listing them again under the separate "merged" category.
    also_merged: bool


class RepoActivity(TypedDict):
    """Stores all activity for a single repository/project."""

    prs_submitted: list[ActivityItem]
    prs_reviewed: list[ActivityItem]
    prs_merged: list[ActivityItem]
    issues_created: list[ActivityItem]
    issues_closed: list[ActivityItem]
    visibility: str  # 'public' or 'private'


def empty_repo_activity(visibility: str) -> RepoActivity:
    """Create a RepoActivity with all categories empty."""
    return {
        "prs_submitted": [],
        "prs_reviewed": [],
        "prs_merged": [],
        "issues_created": [],
        "issues_closed": [],
        "visibility": visibility,
    }


def has_activity(activity: RepoActivity) -> bool:
    """Return True if the repository has at least one item in any category."""
    return any(activity[category] for category in ACTIVITY_CATEGORIES)


class GitProvider(Protocol):
    """
    A protocol for classes that can fetch activity data from a Git provider.
    """

    def __init__(self, username: str, token: str | None): ...

    def get_activity(
        self, start_date: datetime, end_date: datetime, fast: bool = False
    ) -> dict[str, RepoActivity]:
        """
        Fetches the user's activity from the provider.

        Args:
            start_date: The start of the reporting period.
            end_date: The end of the reporting period.
            fast: When True, skip expensive per-item verification calls in
                favour of speed, at the cost of some accuracy at the reporting
                window boundaries. Providers for which this makes no difference
                may ignore it.

        Returns:
            A dictionary where keys are repository names and values are
            RepoActivity objects.
        """
        ...

    def get_attention(
        self,
        since: datetime,
        exclusions: list[str] | None = None,
        state_items: "dict[str, dict] | None" = None,
        stale_pr_days: int | None = None,
    ) -> "AttentionFetch":
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
