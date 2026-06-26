import os
from pathlib import Path
from datetime import datetime
from typing import Dict

from launchpadlib.launchpad import Launchpad

from .base import RepoActivity

LP_CREDENTIALS_PATH = Path(os.path.expanduser("~/.config/gitreport/lp_credentials"))


class LaunchpadProvider:
    """A provider for fetching activity data from Launchpad."""

    def __init__(self, username: str, token: str):
        self._username = username
        self._launchpad = Launchpad.login_with(
            "gitreport-cli",
            "production",
            credentials_file=str(LP_CREDENTIALS_PATH),
            version="devel",
        )

    def get_activity(
        self, start_date: datetime, end_date: datetime
    ) -> Dict[str, RepoActivity]:
        """
        Fetches the user's activity from Launchpad.
        """
        person = self._launchpad.me
        repos: Dict[str, RepoActivity] = {}

        # 1. Bugs created and closed by the user
        for bug in self._launchpad.bugs.searchTasks(bug_reporter=person, modified_since=start_date.isoformat()):
            project_name = bug.bug_target_name
            if project_name not in repos:
                visibility = "private" if bug.bug_target.private else "public"
                repos[project_name] = self._create_empty_repo_activity(visibility)
            
            if bug.date_created and bug.date_created >= start_date and bug.date_created <= end_date:
                repos[project_name]["issues_created"].append({"title": bug.title, "url": bug.web_link})
            
            if bug.date_closed and bug.date_closed >= start_date and bug.date_closed <= end_date:
                repos[project_name]["issues_closed"].append({"title": bug.title, "url": bug.web_link})

        # 2. Merge proposals submitted by the user and directly merged by the user
        for mp in person.getMergeProposals():
            # Filter by date
            if not (mp.date_created and mp.date_created <= end_date and mp.date_created >= start_date):
                continue
            
            repo_name = mp.target_git_repository_path
            if repo_name not in repos:
                visibility = "public"
                if mp.target_git_repository and mp.target_git_repository.project:
                    visibility = "private" if mp.target_git_repository.project.private else "public"
                repos[repo_name] = self._create_empty_repo_activity(visibility)
            
            # Submitted MPs
            if not any(item["url"] == mp.web_link for item in repos[repo_name]["prs_submitted"]):
                repos[repo_name]["prs_submitted"].append({"title": mp.web_link.split('/')[-1], "url": mp.web_link})
            
            # Directly Merged MPs (from the user's submitted ones)
            if mp.queue_status == 'Merged' and getattr(mp, 'merger', None) == person:
                if not any(item["url"] == mp.web_link for item in repos[repo_name]["prs_merged"]):
                    repos[repo_name]["prs_merged"].append({"title": mp.web_link.split('/')[-1], "url": mp.web_link})

        # 3. Merge proposals reviewed (requested vs. claimed)
        for mp in person.getRequestedReviews():
            # DATE FILTER REMOVED FOR DEBUGGING
            
            is_requested = False
            is_claimed = False

            for vote in mp.votes:
                if vote.reviewer == person: # This vote concerns me
                    if vote.registrant == person: # I registered this vote for myself
                        is_claimed = True
                    else: # Someone else registered this vote for me
                        is_requested = True
            
            if not (is_requested or is_claimed):
                continue

            repo_name = mp.target_git_repository_path
            if repo_name not in repos:
                visibility = "public"
                if mp.target_git_repository and mp.target_git_repository.project:
                        visibility = "private" if mp.target_git_repository.project.private else "public"
                repos[repo_name] = self._create_empty_repo_activity(visibility)
            
            activity_item = {"title": mp.web_link.split("/")[-1], "url": mp.web_link}

            if is_claimed and not any(item["url"] == mp.web_link for item in repos[repo_name]["prs_claimed"]):
                repos[repo_name]["prs_claimed"].append(activity_item)
            
            if is_requested and not any(item["url"] == mp.web_link for item in repos[repo_name]["prs_reviewed"]):
                repos[repo_name]["prs_reviewed"].append(activity_item)
        
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
