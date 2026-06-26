from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.gitreport.providers.github import GitHubProvider

# --- Mock Objects ---

class MockRepository:
    def __init__(self, name, private=False):
        self.full_name = name
        self.private = private
        self.pull_requests = {}

    def get_pull(self, number):
        return self.pull_requests.get(number)

class MockUser:
    def __init__(self, login):
        self.login = login

class MockPullRequest:
    def __init__(self, title, url, reviews=None):
        self.title = title
        self.html_url = url
        self.reviews = reviews or []
    
    def get_reviews(self):
        return self.reviews

class MockIssue:
    def __init__(self, title, url, repo, user_login, number):
        self.title = title
        self.html_url = url
        self.repository = repo
        self.user = MockUser(user_login)
        self.number = number


class MockReview:
    def __init__(self, user_login, submitted_at):
        self.user = MockUser(user_login)
        self.submitted_at = submitted_at


# --- Tests ---

@patch("src.gitreport.providers.github.Github")
def test_github_provider_get_activity(MockGithub):
    """Test the get_activity method of the GitHubProvider."""
    # Arrange
    mock_github_instance = MockGithub.return_value
    mock_github_instance.search_issues.return_value = []

    provider = GitHubProvider(username="testuser", token="fake-token")
    start_date = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end_date = datetime(2024, 1, 31, tzinfo=timezone.utc)
    
    # Mock repositories
    repo1 = MockRepository("org/repo1", private=False) # Public
    repo2 = MockRepository("org/repo2", private=True)  # Private

    # Mock data
    pr_submitted = MockIssue("Feat: New API", "http://pr/1", repo1, "testuser", 1)
    issue_created = MockIssue("Bug: Crash", "http://issue/1", repo1, "testuser", 2)
    # Mock data for PRs reviewed
    submitted_review = MockReview("testuser", datetime(2024, 1, 15, tzinfo=timezone.utc))
    pending_review = MockReview("testuser", None)
    pr_obj = MockPullRequest("Fix: Style issue", "http://pr/2", reviews=[pending_review, submitted_review])
    repo2.pull_requests[3] = pr_obj
    pr_reviewed_issue = MockIssue("Fix: Style issue", "http://pr/2", repo2, "anotheruser", 3)

    # Act
    mock_github_instance.search_issues.side_effect = [
        [pr_submitted],
        [issue_created],
        [pr_reviewed_issue],
        [], 
    ]
    activity = provider.get_activity(start_date, end_date)

    # Assert
    assert "org/repo1" in activity
    assert "org/repo2" in activity

    # Check visibility
    assert activity["org/repo1"]["visibility"] == "public"
    assert activity["org/repo2"]["visibility"] == "private"

    # Check content
    assert len(activity["org/repo1"]["prs_submitted"]) == 1
    assert len(activity["org/repo1"]["issues_created"]) == 1
    assert len(activity["org/repo2"]["prs_reviewed"]) == 1
