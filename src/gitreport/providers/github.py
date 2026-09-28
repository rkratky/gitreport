import re
import sys
from datetime import UTC, datetime, timedelta

from github import Auth, Github, GithubRetry

from .base import (
    AttentionFetch,
    AttentionItem,
    RepoActivity,
    empty_repo_activity,
    escape_user,
    is_excluded,
)

# Notification reasons that map to attention kinds. Reasons absent from this
# map (subscribed, state_change, manual, ...) are skipped.
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
      reviewThreads(first: 100) {
        pageInfo { hasNextPage }
        nodes {
          isResolved
          comments(last: 1) { nodes { author { login } updatedAt } }
        }
      }
    } }
  }
}
"""


def _max_timestamp(current: str | None, candidate: str | None) -> str | None:
    """The later of two ISO-8601 timestamps; None/unparseable never wins.

    Parse-based (not string) comparison: a "Z" and a "+00:00" suffix denote
    the same instant and must not compare as different.
    """
    if not candidate:
        return current
    if not current:
        return candidate
    try:
        current_dt = datetime.fromisoformat(current)
        candidate_dt = datetime.fromisoformat(candidate)
    except ValueError:
        return current
    return candidate if candidate_dt > current_dt else current


class GitHubProvider:
    """A provider for fetching activity data from GitHub."""

    def __init__(self, username: str, token: str | None):
        self._username = username
        auth = Auth.Token(token) if token else None
        # GitHub's search API has a low primary limit (30 req/min) and an
        # aggressive secondary limit on bursts. PyGithub's default retry sleeps
        # up to 10 times with a 60s backoff, which makes the tool look frozen.
        # Cap the retries so genuine throttling surfaces quickly rather than
        # hanging for minutes.
        self._github = Github(
            auth=auth,
            retry=GithubRetry(total=3),
            per_page=100,
        )

    def get_activity(
        self, start_date: datetime, end_date: datetime, fast: bool = False
    ) -> dict[str, RepoActivity]:
        """
        Fetches the configured user's activity from GitHub.

        All queries are scoped to the configured user, so every item in the
        result was produced by that user.

        When `fast` is True, the reviewed and merged passes rely solely on the
        search results and skip the per-PR API calls used to confirm the exact
        review/merge. This is much faster but slightly less accurate at the
        window boundaries (see the inline notes below).
        """
        repos: dict[str, RepoActivity] = {}
        date_range = f"{start_date.strftime('%Y-%m-%d')}..{end_date.strftime('%Y-%m-%d')}"

        def repo_for(repo) -> RepoActivity:
            name = repo.full_name
            if name not in repos:
                visibility = "private" if repo.private else "public"
                repos[name] = empty_repo_activity(visibility)
            return repos[name]

        # 1. PRs submitted (authored) by the user.
        query = f"is:pr author:{self._username} created:{date_range}"
        for pr in self._github.search_issues(query):
            repo_for(pr.repository)["prs_submitted"].append({"title": pr.title, "url": pr.html_url})

        # 2. Issues created by the user.
        query = f"is:issue author:{self._username} created:{date_range}"
        for issue in self._github.search_issues(query):
            repo_for(issue.repository)["issues_created"].append(
                {"title": issue.title, "url": issue.html_url}
            )

        # 3. PRs reviewed by the user (excluding the user's own PRs). Track the
        #    reviewed PRs so the merged pass can annotate rather than duplicate.
        reviewed_items: dict[tuple, dict] = {}
        query = f"is:pr reviewed-by:{self._username} -author:{self._username} updated:{date_range}"
        for issue in self._github.search_issues(query):
            key = (issue.repository.full_name, issue.html_url)

            if fast:
                # Trust the search: the `updated` filter is approximate (it is
                # the PR's last-activity date, not the review date), so this may
                # over- or under-include at the window edges.
                in_window = True
            else:
                # Confirm the user actually submitted a review within the
                # reporting window. Costs one API call per candidate PR.
                pr = issue.as_pull_request()
                in_window = any(
                    review.submitted_at
                    and review.user
                    and review.user.login == self._username
                    and start_date <= review.submitted_at <= end_date
                    for review in pr.get_reviews()
                )

            if in_window and key not in reviewed_items:
                item = {"title": issue.title, "url": issue.html_url}
                repo_for(issue.repository)["prs_reviewed"].append(item)
                reviewed_items[key] = item

        # 4. PRs merged by the user but NOT authored by the user. If such a PR
        #    was also reviewed by the user, annotate the reviewed entry with
        #    "also_merged" instead of listing it separately.
        #
        #    GitHub search has no "merged-by:" qualifier, so we cannot query
        #    "PRs I merged" directly. We bound the candidate set to PRs the user
        #    is involved in (author OR assignee OR mentioned OR commenter) and
        #    then confirm the merger via the PR's `merged_by` field. This keeps
        #    the search from matching every merged PR on GitHub.
        #
        #    In fast mode the `merged_by` confirmation (one API call per
        #    candidate) is skipped, so this pass is disabled entirely because it
        #    cannot be answered from search results alone.
        if not fast:
            query = (
                f"is:pr is:merged involves:{self._username} "
                f"-author:{self._username} merged:{date_range}"
            )
            for issue in self._github.search_issues(query):
                pr = issue.as_pull_request()
                merged_by = getattr(pr, "merged_by", None)
                if not (merged_by and merged_by.login == self._username):
                    continue

                key = (issue.repository.full_name, issue.html_url)
                reviewed_item = reviewed_items.get(key)
                if reviewed_item is not None:
                    reviewed_item["also_merged"] = True
                    continue

                activity = repo_for(issue.repository)
                if not any(item["url"] == issue.html_url for item in activity["prs_merged"]):
                    activity["prs_merged"].append({"title": issue.title, "url": issue.html_url})

        # 5. Issues closed by the user.
        query = f"is:issue closed-by:{self._username} closed:{date_range}"
        for issue in self._github.search_issues(query):
            repo_for(issue.repository)["issues_closed"].append(
                {"title": issue.title, "url": issue.html_url}
            )

        return repos

    def _item(
        self,
        *,
        mid,
        provider,
        kind,
        origin,
        repo,
        title,
        url,
        reason,
        updated_at,
        thread_url=None,
    ) -> AttentionItem:
        return AttentionItem(
            id=mid,
            provider=provider,
            kind=kind,
            origin=origin,
            repo=repo,
            title=escape_user(title),
            url=url,
            reason=escape_user(reason),
            updated_at=updated_at,
            thread_url=thread_url,
        )

    def _notification_items(self, exclusions, state_items) -> list[AttentionItem]:
        items: list[AttentionItem] = []
        for n in self._github.get_user().get_notifications(all=False):
            subject_url = getattr(n.subject, "url", "") or ""
            thread_url = getattr(n, "url", "") or ""
            subject_type = getattr(n.subject, "type", "") or ""
            repository = getattr(n, "repository", None)
            if repository is not None:
                full_name = repository.full_name
            else:
                # Fallback: parse the repo from the subject API url, stripping
                # the trailing "/<type>/<number>" segments:
                # api.github.com/repos/org/repo/pulls/1 -> "org/repo".
                match = re.search(r"/repos/(.+)/[^/]+/[^/]+$", subject_url)
                full_name = match.group(1) if match else ""
            if is_excluded(full_name, exclusions):
                continue
            if subject_type in ("PullRequest", "Issue", "Commit") and subject_url:
                # Convert the subject API url to its html url for display:
                # api.github.com/repos/o/r/pulls/3 -> github.com/o/r/pull/3
                # api.github.com/repos/o/r/commits/<sha> -> github.com/o/r/commit/<sha>
                html_url = (
                    subject_url.replace("api.github.com/repos/", "github.com/")
                    .replace("/pulls/", "/pull/")
                    .replace("/commits/", "/commit/")
                )
                id_url = html_url
            else:
                # Other subject types (releases, discussions, ...) have no
                # predictable api->html rewrite; display the repository page
                # (the thread url stays only in the thread_url field).
                html_url = getattr(repository, "html_url", None) or (
                    f"https://github.com/{full_name}" if full_name else ""
                )
                # Identity and display diverge here: every notification of
                # this kind in a repo displays the same repo page url, so the
                # id must come from the unique per-notification thread api
                # url — otherwise dedupe folds distinct releases into one.
                id_url = thread_url
            item_id = f"gh:{id_url or thread_url}"
            reason = n.reason
            if reason == "ci_activity":
                # Tri-state check: True = a check run failed (emit); False =
                # checked, no failure (skip); None = lookup error or
                # unverifiable shape. On None, re-emit ONLY when a state
                # record for this id already exists and is still open (e.g. a
                # previously verified failure whose check-run lookup now
                # fails): the fetch then still reports the item, so no
                # absence-based resolution fires on unverified data. Acked
                # records stay out of the inbox, and unverified data never
                # fabricates a new failure item.
                ci_state = self._ci_failed(full_name, subject_url)
                if ci_state is False:
                    continue
                if ci_state is None:
                    record = (state_items or {}).get(item_id)
                    if record is None or record.get("status") != "open":
                        continue
                kind = "ci_failure"
            else:
                kind = GH_REASON_KIND.get(reason)
                if kind is None:
                    continue  # subscribed, state_change, manual, etc.
            title = getattr(n.subject, "title", "") or ""
            items.append(
                self._item(
                    # Ids use the html url form so query/GraphQL-origin items
                    # for the same PR dedupe against notification items.
                    mid=item_id,
                    provider="github",
                    kind=kind,
                    origin="notification",
                    repo=full_name,
                    title=title,
                    url=html_url,
                    reason=reason,
                    updated_at=n.updated_at.isoformat() if n.updated_at else None,
                    thread_url=thread_url,
                )
            )
        return items

    def _ci_failed(self, full_name: str, subject_url: str) -> bool | None:
        """Tri-state check-run verification for a ci_activity notification.

        True when the subject's check runs contain a failure; False when the
        lookup succeeds and none fail; None when the lookup errors or the
        shape is unverifiable (null subject url). v1 limitation: GitHub sends
        ci_activity notifications with a CheckSuite subject and null url;
        those return None here — failing checks on the user's own PRs are
        still caught by the status:failure search query. The caller re-emits
        a notification whose id already has an open state record even when
        this cannot verify, so no absence-based resolution fires on
        unverified data.
        """
        try:
            number = int(subject_url.rsplit("/", 1)[1])
            repo = self._github.get_repo(full_name)
            pr = repo.get_pull(number)
            # PyGithub has no PullRequest.get_check_runs; a PR's checks are the
            # check runs on its head commit (same data the PR UI shows).
            checks = repo.get_commit(pr.head.sha).get_check_runs()
            return any(c.conclusion == "failure" for c in checks)
        except Exception:
            return None  # cannot verify -> never fabricate or skip a failure

    def _search_item(self, issue, kind: str, reason: str, exclusions) -> AttentionItem | None:
        """A query-origin item for a search match.

        Presence-only: `updated_at` is None because a search result carries no
        author information — the PR/issue's own updatedAt may be the user's own
        activity, which must never refresh or reopen an item. Reopens come
        from notification-origin events; presence keeps the item in the inbox
        and lets absence-resolution see it.
        """
        repo_full = issue.repository.full_name
        if is_excluded(repo_full, exclusions):
            return None
        return self._item(
            mid=f"gh:{issue.html_url}",
            provider="github",
            kind=kind,
            origin="query",
            repo=repo_full,
            title=issue.title,
            url=issue.html_url,
            reason=reason,
            updated_at=None,
        )

    def _unresolved_thread_items(self, exclusions) -> list[AttentionItem]:
        """Unresolved review threads on the user's own open PRs.

        `updated_at` follows the self-activity rule: it is the newest
        thread-comment (each thread's last comment) whose author is not the
        user, across the visible threads. When every visible thread's last
        comment is the user's own — or there is none — the PR's own
        `updatedAt` is NOT used (search cannot tell whether the PR was last
        touched by the user), so the item is emitted with `updated_at=None`:
        its presence keeps it in the inbox, but no event (refresh/reopen) is
        derived from it.

        Truncated threads (more than `first: 100`): the item is still emitted
        rather than dropped — dropping would fabricate an absence and
        auto-resolve the record even though unresolved state beyond the first
        100 is unknown. The reason then reads "100+ review threads
        (truncated); unresolved state unknown".
        """
        items: list[AttentionItem] = []
        cursor = None
        while True:
            variables = {"q": f"is:pr is:open author:{self._username}"}
            if cursor:
                variables["cursor"] = cursor
            # Requester.graphql_query returns (headers, data) — headers first
            # (contract with the installed PyGithub; see its docstring).
            response_headers, payload = self._github.requester.graphql_query(
                REVIEW_THREADS_QUERY, variables
            )
            search = payload["data"]["search"]
            for node in search["nodes"]:
                # html url form github.com/<owner>/<repo>/pull/<n>: the repo
                # full name is the first two segments after github.com/.
                repo_full = (
                    "/".join(node["url"].split("/github.com/")[1].split("/")[:2])
                    if "/github.com/" in node["url"]
                    else ""
                )
                if is_excluded(repo_full, exclusions):
                    continue
                # GraphQL fields can be explicitly null (key present, value
                # None), so `or {}` guards are required — `.get(key, {})`
                # would pass the null straight through.
                thread_data = node.get("reviewThreads") or {}
                threads = thread_data.get("nodes") or []
                truncated = bool((thread_data.get("pageInfo") or {}).get("hasNextPage"))
                unresolved = [t for t in threads if not t.get("isResolved")]
                if not unresolved and not truncated:
                    continue
                if truncated:
                    # Truncated threads: emit instead of dropping (a drop
                    # would fabricate an absence); unresolved state beyond
                    # the first 100 is unknown.
                    print(
                        f"Warning: {node.get('url')} has more than 100 review "
                        "threads; unresolved state beyond the first 100 is unknown.",
                        file=sys.stderr,
                    )
                    reason = "100+ review threads (truncated); unresolved state unknown"
                else:
                    reason = f"{len(unresolved)} unresolved review thread(s)"
                newest_foreign = None
                for thread in threads:
                    comments = (thread.get("comments") or {}).get("nodes") or []
                    for comment in comments:
                        author = (comment.get("author") or {}).get("login")
                        if author == self._username:
                            continue  # self-activity never refreshes an item
                        newest_foreign = _max_timestamp(newest_foreign, comment.get("updatedAt"))
                items.append(
                    self._item(
                        mid=f"gh:{node['url']}",
                        provider="github",
                        kind="thread_unresolved",
                        origin="query",
                        repo=repo_full,
                        title=node.get("title", ""),
                        url=node["url"],
                        reason=reason,
                        updated_at=newest_foreign,
                    )
                )
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
            items.extend(self._notification_items(exclusions, state_items))

            failing_checks_q = f"is:pr is:open author:{self._username} status:failure"
            for issue in self._github.search_issues(failing_checks_q):
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
                    got = self._search_item(
                        issue, "stale_pr", f"no activity for {stale_pr_days}+ days", exclusions
                    )
                    if got:
                        items.append(got)

            items.extend(self._unresolved_thread_items(exclusions))
            return AttentionFetch(ok=True, items=items, resolved_ids=[], error=None)
        except Exception as e:  # noqa: BLE001 — failure must degrade, not crash
            return AttentionFetch(ok=False, items=[], resolved_ids=[], error=str(e))
