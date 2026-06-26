from typing import Dict, List, Protocol, TypedDict
from datetime import datetime

class ActivityItem(TypedDict):
    """Represents a single activity item (e.g., a PR or issue)."""
    title: str
    url: str

class RepoActivity(TypedDict):
    """Stores all activity for a single repository."""
    prs_submitted: List[ActivityItem]
    prs_reviewed: List[ActivityItem]
    issues_created: List[ActivityItem]
    issues_closed: List[ActivityItem]
    visibility: str # 'public' or 'private'

class GitProvider(Protocol):
    """
    A protocol for classes that can fetch activity data from a Git provider.
    """
    def __init__(self, username: str, token: str):
        ...

    def get_activity(
        self, start_date: datetime, end_date: datetime
    ) -> Dict[str, RepoActivity]:
        """
        Fetches the user's activity from the provider.

        Args:
            start_date: The start of the reporting period.
            end_date: The end of the reporting period.

        Returns:
            A dictionary where keys are repository names and values are
            RepoActivity objects.
        """
        ...
