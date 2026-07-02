from datetime import datetime

from github import Auth, Github, GithubRetry

from .base import RepoActivity, empty_repo_activity


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
