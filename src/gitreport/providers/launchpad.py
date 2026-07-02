import os
from datetime import datetime
from pathlib import Path

from launchpadlib.launchpad import Launchpad

from .base import RepoActivity, empty_repo_activity

LP_CREDENTIALS_PATH = Path(os.path.expanduser("~/.config/gitreport/lp_credentials"))

# All merge-proposal statuses. getRequestedReviews defaults to "Needs Review"
# only, which would hide proposals the user already reviewed and that have since
# been approved/merged/rejected. Passing every status makes the review scan
# complete.
ALL_MP_STATUSES = [
    "Work in progress",
    "Needs review",
    "Approved",
    "Rejected",
    "Merged",
    "Code failed to merge",
    "Queued",
    "Superseded",
]

# Bug-task statuses that count as "closed" (per the API's date_closed field:
# the date a task was marked Fix Released, Invalid, Won't Fix, Expired or
# Opinion). Launchpad has no "closed by" search parameter, so we approximate
# "bugs closed by the user" as "bugs assigned to the user that are now closed".
CLOSED_BUG_STATUSES = [
    "Fix Released",
    "Invalid",
    "Won't Fix",
    "Expired",
    "Opinion",
]


def _mp_title(mp) -> str:
    """A concise human title for a merge proposal (its numeric id)."""
    return mp.web_link.rstrip("/").split("/")[-1]


def _mp_repo(mp) -> tuple[str, str]:
    """
    Resolve the (repository name, visibility) for a Git-based merge proposal.
    """
    repo = mp.target_git_repository
    name = getattr(repo, "unique_name", None) or getattr(repo, "display_name", mp.web_link)
    visibility = "private" if getattr(repo, "private", False) else "public"
    return name, visibility


def _same_person(entry, person) -> bool:
    """Compare two Launchpad person entries by their stable self_link/name."""
    if entry is None:
        return False
    a = getattr(entry, "self_link", None) or getattr(entry, "name", None)
    b = getattr(person, "self_link", None) or getattr(person, "name", None)
    return a is not None and a == b


def _in_range(when: datetime | None, start: datetime, end: datetime) -> bool:
    return when is not None and start <= when <= end


class LaunchpadProvider:
    """A provider for fetching activity data from Launchpad."""

    def __init__(self, username: str, token: str | None = None):
        self._username = username
        # Launchpad authenticates via a saved OAuth credentials file (created by
        # auth_launchpad.py), not via the config `token`.
        self._launchpad = Launchpad.login_with(
            "gitreport-cli",
            "production",
            credentials_file=str(LP_CREDENTIALS_PATH),
            version="devel",
        )

    def get_activity(
        self, start_date: datetime, end_date: datetime, fast: bool = False
    ) -> dict[str, RepoActivity]:
        """
        Fetches the configured user's activity from Launchpad.

        Every query is scoped to `self._launchpad.me`, so all items belong to
        the configured user. The `fast` flag is accepted for interface
        consistency; Launchpad's queries are already inexpensive, so it has no
        effect here.
        """
        person = self._launchpad.me
        repos: dict[str, RepoActivity] = {}

        def repo_for(name: str, visibility: str) -> RepoActivity:
            if name not in repos:
                repos[name] = empty_repo_activity(visibility)
            return repos[name]

        self._collect_bugs(person, start_date, end_date, repo_for)
        reviewed_index = self._collect_reviews(person, start_date, end_date, repo_for)

        if not reviewed_index:
            import sys

            print(
                "Warning: Launchpad's API has no 'MPs I reviewed' endpoint; "
                "getRequestedReviews only returns pending reviews. "
                "Completed reviews (including self-claimed) will not appear "
                "in the report. This is a Launchpad API limitation.",
                file=sys.stderr,
            )

        self._collect_merge_proposals(person, start_date, end_date, repo_for, reviewed_index)

        return repos

    # -- Bugs -------------------------------------------------------------

    def _collect_bugs(self, person, start_date, end_date, repo_for) -> None:
        # Bugs reported by the user.
        for bug in self._launchpad.bugs.searchTasks(
            bug_reporter=person, modified_since=start_date.isoformat()
        ):
            if not _in_range(bug.date_created, start_date, end_date):
                continue
            visibility = "private" if bug.bug_target.private else "public"
            activity = repo_for(bug.bug_target_name, visibility)
            activity["issues_created"].append({"title": bug.title, "url": bug.web_link})

        # Bugs closed by the user. Launchpad has no "bug_closer" search
        # parameter, so we approximate this as bugs assigned to the user that
        # are now in a closed status, with date_closed in the reporting window.
        for bug in self._launchpad.bugs.searchTasks(
            assignee=person,
            status=CLOSED_BUG_STATUSES,
            modified_since=start_date.isoformat(),
        ):
            if not _in_range(bug.date_closed, start_date, end_date):
                continue
            visibility = "private" if bug.bug_target.private else "public"
            activity = repo_for(bug.bug_target_name, visibility)
            activity["issues_closed"].append({"title": bug.title, "url": bug.web_link})

    # -- Reviews ----------------------------------------------------------

    def _collect_reviews(self, person, start_date, end_date, repo_for) -> dict:
        """
        Record merge proposals the user actually reviewed.

        .. warning::

            The Launchpad API has **no endpoint for "MPs I have reviewed."**
            ``getRequestedReviews`` is a *todo list* — it only returns MPs
            where a review is still **pending** (``is_pending=True``).  Once a
            review is cast (``is_pending`` becomes ``False``) the MP is removed
            from the list regardless of the ``status`` parameter, so completed
            reviews are generally **not** surfaced here.

            This means the "Merge Proposals Reviewed" section for Launchpad is
            best-effort and will typically be empty or very incomplete for
            users who complete their reviews promptly.  This is a fundamental
            limitation of the Launchpad web-service API (the web UI's
            ``+reviewing`` page is a browser view, not exposed as a web-service
            operation).

        Returns an index mapping web_link -> reviewed ActivityItem so the merge
        pass can annotate items that were also merged by the user.
        """
        reviewed_index: dict = {}

        for mp in person.getRequestedReviews(status=ALL_MP_STATUSES):
            review_date = self._user_review_date(mp, person)
            if not _in_range(review_date, start_date, end_date):
                continue

            repo_name, visibility = _mp_repo(mp)
            activity = repo_for(repo_name, visibility)

            if mp.web_link not in reviewed_index:
                item = {"title": _mp_title(mp), "url": mp.web_link}
                activity["prs_reviewed"].append(item)
                reviewed_index[mp.web_link] = item

        return reviewed_index

    @staticmethod
    def _user_review_date(mp, person) -> datetime | None:
        """
        Return the date the user cast a (non-pending) review vote on `mp`, or
        None if the user has not actually reviewed it.
        """
        for vote in mp.votes:
            if not _same_person(vote.reviewer, person):
                continue
            if getattr(vote, "is_pending", False):
                # Review was requested/claimed but not yet cast.
                continue
            comment = getattr(vote, "comment", None)
            if comment is not None and getattr(comment, "date_created", None):
                return comment.date_created
            # Fall back to the proposal's review date if the comment link is
            # unavailable.
            return getattr(mp, "date_reviewed", None)
        return None

    # -- Merge proposals (submitted + merged) -----------------------------

    def _collect_merge_proposals(
        self, person, start_date, end_date, repo_for, reviewed_index
    ) -> None:
        # Merge proposals submitted (authored) by the user.
        for mp in person.getMergeProposals():
            if not _in_range(mp.date_created, start_date, end_date):
                continue
            repo_name, visibility = _mp_repo(mp)
            activity = repo_for(repo_name, visibility)
            if not any(item["url"] == mp.web_link for item in activity["prs_submitted"]):
                activity["prs_submitted"].append({"title": _mp_title(mp), "url": mp.web_link})

        # Merge proposals merged by the user but NOT authored by the user.
        # There is no "merged by me" endpoint, so the candidate universe is the
        # set of proposals the user reviewed (already fetched above). If the
        # user also merged such a proposal, annotate the reviewed entry with
        # "also_merged"; otherwise it appears under the separate merged category.
        #
        # NOTE: This relies on getRequestedReviews, which only returns pending
        # reviews (see _collect_reviews). Once a review is completed, the MP
        # drops off getRequestedReviews, so merged-by-user detection for MPs
        # the user also reviewed will typically find nothing. This is a
        # Launchpad API limitation.
        for mp in person.getRequestedReviews(status=ALL_MP_STATUSES):
            if getattr(mp, "queue_status", None) != "Merged":
                continue
            if not _in_range(getattr(mp, "date_merged", None), start_date, end_date):
                continue
            if not _same_person(getattr(mp, "merge_reporter", None), person):
                continue
            if _same_person(getattr(mp, "registrant", None), person):
                # The user authored it; it is already covered by prs_submitted.
                continue

            reviewed_item = reviewed_index.get(mp.web_link)
            if reviewed_item is not None:
                reviewed_item["also_merged"] = True
                continue

            repo_name, visibility = _mp_repo(mp)
            activity = repo_for(repo_name, visibility)
            if not any(item["url"] == mp.web_link for item in activity["prs_merged"]):
                activity["prs_merged"].append({"title": _mp_title(mp), "url": mp.web_link})
