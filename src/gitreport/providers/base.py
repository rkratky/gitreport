from datetime import datetime
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
