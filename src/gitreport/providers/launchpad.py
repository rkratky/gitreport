import os
from datetime import UTC, datetime
from pathlib import Path

from launchpadlib.launchpad import Launchpad

from .base import (
    AttentionFetch,
    AttentionItem,
    RepoActivity,
    empty_repo_activity,
    escape_user,
    is_excluded,
)

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


def _parse_naive_utc(ts: str | None) -> datetime | None:
    """ISO timestamp string -> aware datetime; naive values are read as UTC.

    None and unparseable input return None rather than raising: one malformed
    stored field must never abort the resolved-id pass.
    """
    if ts is None:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _aware_utc(dt: datetime | None) -> datetime | None:
    """Naive launchpadlib datetimes are read as UTC; aware ones pass through."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _api_url(web_url: str) -> str:
    """
    Map a Launchpad web URL to its API URL.

    launchpadlib's load() accepts any URL, but web URLs return HTML, not
    JSON — the parse fails and every load() silently resolves to nothing.
    Rewriting the host to api.launchpad.net/devel makes load() work.
    """
    for prefix in (
        "https://api.launchpad.net/",
        "https://bugs.launchpad.net/",
        "https://code.launchpad.net/",
        "https://launchpad.net/",
    ):
        if web_url.startswith(prefix):
            return "https://api.launchpad.net/devel/" + web_url[len(prefix) :]
    return web_url


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

    # -- Attention ---------------------------------------------------------

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
            if since.tzinfo is None:
                since = since.replace(tzinfo=UTC)
            person = self._launchpad.me
            items: list[AttentionItem] = []

            # 1. MPs requesting my review (default status: Needs review).
            for mp in person.getRequestedReviews():
                name, _visibility = _mp_repo(mp)
                if is_excluded(name, exclusions):
                    continue
                items.append(
                    self._mp_item(
                        mp, name, "lp_mp_needs_review", "review requested from you", mp.date_created
                    )
                )

            # 2. My MPs with new comments/votes since `since`.
            for mp in person.getMergeProposals():
                name, _visibility = _mp_repo(mp)
                if is_excluded(name, exclusions):
                    continue
                latest = self._latest_foreign_event(mp, person, since)
                if latest is not None:
                    items.append(
                        self._mp_item(
                            mp,
                            name,
                            "lp_mp_comment",
                            "new comment/vote on your merge proposal",
                            latest,
                        )
                    )

            # 3. Bugs assigned to me (open; no time window — GH parity).
            for bug in self._launchpad.bugs.searchTasks(assignee=person):
                if is_excluded(bug.bug_target_name, exclusions):
                    continue
                items.append(self._bug_item(bug, person, "issue_assigned", "assigned to you"))

            # 4. Subscribed bugs with activity since `since`.
            for bug in self._launchpad.bugs.searchTasks(
                bug_subscriber=person, modified_since=since.isoformat()
            ):
                if is_excluded(bug.bug_target_name, exclusions):
                    continue
                items.append(
                    self._bug_item(bug, person, "lp_bug_activity", "new activity on subscribed bug")
                )

            return AttentionFetch(
                ok=True,
                items=self._dedupe(items),
                resolved_ids=self._proven_resolved(state_items),
                error=None,
            )
        except Exception as e:  # noqa: BLE001
            return AttentionFetch(ok=False, items=[], resolved_ids=[], error=str(e))

    @staticmethod
    def _dedupe(items: list[AttentionItem]) -> list[AttentionItem]:
        """Drop duplicate ids, merging reasons (a bug can be assigned AND subscribed)."""
        merged: dict[str, AttentionItem] = {}
        for item in items:
            if item["id"] in merged:
                first = merged[item["id"]]
                if item["reason"] not in first["reason"]:
                    first["reason"] += "; " + item["reason"]
                    if item.get("updated_at") and item["updated_at"] > (
                        first.get("updated_at") or ""
                    ):
                        first["updated_at"] = item["updated_at"]
            else:
                merged[item["id"]] = item
        return list(merged.values())

    def _mp_item(self, mp, repo_name, kind, reason, updated_at) -> AttentionItem:
        return AttentionItem(
            id=f"lp:{mp.web_link}",
            provider="launchpad",
            kind=kind,
            origin="query",
            repo=repo_name,
            title=escape_user(_mp_title(mp)),
            url=mp.web_link,
            reason=escape_user(reason),
            updated_at=updated_at.isoformat() if updated_at else None,
            thread_url=None,
        )

    @staticmethod
    def _bug_messages(bug):
        """The bug's message collection, whether `bug` is a bug or a bug task.

        searchTasks returns bug *tasks*; the messages live on the underlying
        bug (task.bug.messages). A missing attribute on either level means the
        stream is unavailable (launchpadlib raises AttributeError lazily, so
        getattr-with-default is the right probe).
        """
        messages = getattr(bug, "messages", None)
        if messages is None:
            bug_entry = getattr(bug, "bug", None)
            if bug_entry is not None:
                messages = getattr(bug_entry, "messages", None)
        return messages

    def _bug_updated_at(self, bug, person) -> str | None:
        """Freshness timestamp for a bug item, or None (presence-only).

        Self-activity rule: the timestamp is only used when the bug's newest
        message was authored by someone other than the user — walk
        `bug.messages` to find that author. The timestamp is that foreign
        message's `date_created`, never `bug.date_last_updated`: the latter
        also moves on the user's own non-message edits (status changes,
        assignee changes), which must not refresh or reopen the item. When
        messages are unavailable or the newest message is the user's own,
        `updated_at` is None: the item stays in the inbox by presence, but no
        event (refresh/reopen) is derived from it.

        Perf note: walking `bug.messages` costs an extra launchpadlib
        collection round-trip per bug (plus one for `bug.bug` on tasks);
        bounded by the number of assigned/subscribed bugs in the inbox.
        """
        messages = self._bug_messages(bug)
        if messages is None:
            return None
        newest, newest_owner = None, None
        for message in messages:
            when = getattr(message, "date_created", None)
            if when is not None and (newest is None or when > newest):
                newest, newest_owner = when, getattr(message, "owner", None)
        if newest is None or _same_person(newest_owner, person):
            return None
        return newest.isoformat()

    def _bug_item(self, bug, person, kind, reason) -> AttentionItem:
        return AttentionItem(
            id=f"lp:{bug.web_link}",
            provider="launchpad",
            kind=kind,
            origin="query",
            repo=bug.bug_target_name,
            title=escape_user(bug.title),
            url=bug.web_link,
            reason=escape_user(reason),
            updated_at=self._bug_updated_at(bug, person),
            thread_url=None,
        )

    def _latest_foreign_event(self, mp, person, since) -> datetime | None:
        """Latest comment/vote on `mp` after `since` by someone other than me."""
        latest: datetime | None = None

        def consider(when: datetime | None) -> None:
            nonlocal latest
            if when and when > since and (latest is None or when > latest):
                latest = when

        # Plain comments (author-annotated).
        for comment in getattr(mp, "all_comments", []) or []:
            if _same_person(getattr(comment, "author", None), person):
                continue
            consider(getattr(comment, "date_created", None))
        # Vote comments (reviewer-annotated; the comment's own author is the
        # reviewer, so both must be someone other than the user).
        for vote in mp.votes:
            if _same_person(vote.reviewer, person):
                continue
            comment = getattr(vote, "comment", None)
            if comment is not None and _same_person(getattr(comment, "author", None), person):
                continue
            consider(getattr(comment, "date_created", None) if comment else None)
        return latest

    def _proven_resolved(self, state_items: dict[str, dict] | None) -> list[str]:
        """Re-load open or acked LP items from state; report ids proven closed.

        Two proof paths (R5): a closing status on the live object, and — for
        open records only — the leave rule: the user commented/voted on the
        item after its recorded last_updated, so it no longer needs
        attention (spec: "user commented/voted after last_updated"). Every
        probe failure is skipped, never treated as proof.
        """
        resolved: list[str] = []
        person = self._launchpad.me
        for mid, rec in (state_items or {}).items():
            if not mid.startswith("lp:") or rec.get("status") not in ("open", "acked"):
                continue
            try:
                obj = self._launchpad.load(_api_url(rec["url"]))
            except Exception:
                continue  # load failure is not proof
            status = getattr(obj, "status", None) or getattr(obj, "queue_status", None)
            if status in CLOSED_BUG_STATUSES or status in self.MP_CLOSED_STATUSES:
                resolved.append(mid)
                continue
            if rec.get("status") != "open":
                continue  # the leave rule applies to open records only
            try:
                if self._user_responded_after(obj, rec, person):
                    resolved.append(mid)
            except Exception:  # noqa: BLE001
                continue  # heuristic failure is not proof
        return resolved

    def _user_responded_after(self, obj, rec: dict, person) -> bool:
        """Leave rule: did the user's own activity postdate last_updated?

        Merge proposals: the user's vote-comment dates and `all_comments`
        entries authored by the user. Bugs: the user's own messages (the
        object may be a bug task; `_bug_messages` walks to the underlying
        bug). Naive timestamps on either side are read as UTC.
        """
        last_updated = _parse_naive_utc(rec.get("last_updated"))
        if last_updated is None:
            return False
        responded: datetime | None = None

        def consider(when: datetime | None) -> None:
            nonlocal responded
            when = _aware_utc(when)
            if when is not None and (responded is None or when > responded):
                responded = when

        if getattr(obj, "votes", None) is not None:  # merge proposal
            for vote in obj.votes:
                if getattr(vote, "is_pending", False):
                    continue  # requested but not cast: not user activity
                if _same_person(vote.reviewer, person):
                    comment = getattr(vote, "comment", None)
                    if comment is not None:
                        consider(getattr(comment, "date_created", None))
            for comment in getattr(obj, "all_comments", []) or []:
                if _same_person(getattr(comment, "author", None), person):
                    consider(getattr(comment, "date_created", None))
        messages = self._bug_messages(obj)
        if messages is not None:  # bug (or bug task)
            for message in messages:
                if _same_person(getattr(message, "owner", None), person):
                    consider(getattr(message, "date_created", None))
        return responded is not None and responded > last_updated
