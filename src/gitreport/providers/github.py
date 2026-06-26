from datetime import datetime
from typing import Dict, List

from github import Github
from github.Issue import Issue
from github.PullRequest import PullRequest

from .base import ActivityItem, RepoActivity


class GitHubProvider:
    """A provider for fetching activity data from GitHub."""

    def __init__(self, username: str, token: str):
        self._username = username
        self._github = Github(token)

    def get_activity(
        self, start_date: datetime, end_date: datetime
    ) -> Dict[str, RepoActivity]:
        """
        Fetches the user's activity from GitHub.

        Args:
            start_date: The start of the reporting period.
            end_date: The end of the reporting period.

        Returns:
            A dictionary where keys are repository names and values are
            RepoActivity objects.
        """
        repos: Dict[str, RepoActivity] = {}

        # 1. PRs submitted by the user
        query = f"is:pr author:{self._username} created:{start_date.strftime('%Y-%m-%d')}..{end_date.strftime('%Y-%m-%d')}"
        for pr in self._github.search_issues(query):
            repo_name = pr.repository.full_name
            if repo_name not in repos:
                visibility = "private" if pr.repository.private else "public"
                repos[repo_name] = self._create_empty_repo_activity(visibility)
            repos[repo_name]["prs_submitted"].append(
                {"title": pr.title, "url": pr.html_url}
            )

        # 2. Issues created by the user
        query = f"is:issue author:{self._username} created:{start_date.strftime('%Y-%m-%d')}..{end_date.strftime('%Y-%m-%d')}"
        for issue in self._github.search_issues(query):
            repo_name = issue.repository.full_name
            if repo_name not in repos:
                visibility = "private" if issue.repository.private else "public"
                repos[repo_name] = self._create_empty_repo_activity(visibility)
            repos[repo_name]["issues_created"].append(
                {"title": issue.title, "url": issue.html_url}
            )
            
        # 3. PRs reviewed by the user
        query = f"is:pr reviewed-by:{self._username} updated:{start_date.strftime('%Y-%m-%d')}..{end_date.strftime('%Y-%m-%d')}"
        for issue in self._github.search_issues(query):
            repo_name = issue.repository.full_name
            # Ensure the user is not the author
            if issue.user.login == self._username:
                continue

            # Get the full PullRequest object to access reviews
            pr = issue.repository.get_pull(issue.number)
            
            # We need to check if the review was in the date range. 
            # The search query on `updated` is a broad filter.
            for review in pr.get_reviews():
                if (
                    review.submitted_at
                    and review.user.login == self._username
                    and start_date <= review.submitted_at <= end_date
                ):
                    if repo_name not in repos:
                        visibility = "private" if issue.repository.private else "public"
                        repos[repo_name] = self._create_empty_repo_activity(visibility)

                    # Avoid duplicates
                    if not any(item['url'] == pr.html_url for item in repos[repo_name]["prs_reviewed"]):
                        repos[repo_name]["prs_reviewed"].append(
                           {"title": pr.title, "url": pr.html_url}
                       )
                    break # Move to the next PR after finding a valid review

        # 4. Issues closed by the user (as closer)
        query = f"is:issue closed-by:{self._username} closed:{start_date.strftime('%Y-%m-%d')}..{end_date.strftime('%Y-%m-%d')}"
        for issue in self._github.search_issues(query):
             repo_name = issue.repository.full_name
             if repo_name not in repos:
                visibility = "private" if issue.repository.private else "public"
                repos[repo_name] = self._create_empty_repo_activity(visibility)
             repos[repo_name]["issues_closed"].append(
                 {"title": issue.title, "url": issue.html_url}
             )

        return repos

    def _create_empty_repo_activity(self, visibility: str) -> RepoActivity:
        return {
            "prs_submitted": [],
            "prs_reviewed": [],
            "prs_claimed": [],
            "prs_merged": [],
            "issues_created": [],
            "issues_closed": [],
            "visibility": visibility,
        }

