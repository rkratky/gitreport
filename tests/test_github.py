from datetime import UTC, datetime
from unittest.mock import patch

from gitreport.providers.github import GitHubProvider

# --- Mock Objects ---


class MockRepository:
    def __init__(self, name, private=False):
        self.full_name = name
        self.private = private


class MockUser:
    def __init__(self, login):
        self.login = login


class MockPullRequest:
    def __init__(self, reviews=None, merged_by=None):
        self.reviews = reviews or []
        self.merged_by = merged_by

    def get_reviews(self):
        return self.reviews


class MockIssue:
    def __init__(self, title, url, repo, user_login, number, pull_request=None):
        self.title = title
        self.html_url = url
        self.repository = repo
        self.user = MockUser(user_login)
        self.number = number
        self._pull_request = pull_request

    def as_pull_request(self):
        return self._pull_request


class MockReview:
    def __init__(self, user_login, submitted_at):
        self.user = MockUser(user_login)
        self.submitted_at = submitted_at


# --- Tests ---


@patch("gitreport.providers.github.Github")
def test_github_provider_get_activity(MockGithub):
    """Test the get_activity method of the GitHubProvider."""
    mock_github_instance = MockGithub.return_value

    provider = GitHubProvider(username="testuser", token="fake-token")
    start_date = datetime(2024, 1, 1, tzinfo=UTC)
    end_date = datetime(2024, 1, 31, tzinfo=UTC)

    repo1 = MockRepository("org/repo1", private=False)  # Public
    repo2 = MockRepository("org/repo2", private=True)  # Private

    pr_submitted = MockIssue("Feat: New API", "http://pr/1", repo1, "testuser", 1)
    issue_created = MockIssue("Bug: Crash", "http://issue/1", repo1, "testuser", 2)

    # Reviewed PR (authored by someone else): the user's review is in-window.
    submitted_review = MockReview("testuser", datetime(2024, 1, 15, tzinfo=UTC))
    pending_review = MockReview("testuser", None)
    reviewed_pr = MockPullRequest(reviews=[pending_review, submitted_review])
    pr_reviewed_issue = MockIssue(
        "Fix: Style issue", "http://pr/2", repo2, "anotheruser", 3, pull_request=reviewed_pr
    )

    # search_issues is called 5 times, in order:
    # submitted, issues_created, reviewed, merged, issues_closed.
    mock_github_instance.search_issues.side_effect = [
        [pr_submitted],
        [issue_created],
        [pr_reviewed_issue],
        [],  # merged
        [],  # closed
    ]
    activity = provider.get_activity(start_date, end_date)

    assert "org/repo1" in activity
    assert "org/repo2" in activity
    assert activity["org/repo1"]["visibility"] == "public"
    assert activity["org/repo2"]["visibility"] == "private"
    assert len(activity["org/repo1"]["prs_submitted"]) == 1
    assert len(activity["org/repo1"]["issues_created"]) == 1
    assert len(activity["org/repo2"]["prs_reviewed"]) == 1


@patch("gitreport.providers.github.Github")
def test_github_fast_mode_skips_verification_and_merged(MockGithub):
    """In fast mode the reviewed PR is trusted from search and merged is skipped."""
    mock_github_instance = MockGithub.return_value
    provider = GitHubProvider(username="testuser", token="fake-token")
    start_date = datetime(2024, 1, 1, tzinfo=UTC)
    end_date = datetime(2024, 1, 31, tzinfo=UTC)

    repo = MockRepository("org/repo", private=False)
    # No pull_request attached and no review data: fast mode must not need it.
    reviewed_issue = MockIssue("Reviewed", "http://pr/2", repo, "anotheruser", 3)

    mock_github_instance.search_issues.side_effect = [
        [],  # submitted
        [],  # issues_created
        [reviewed_issue],  # reviewed
        [],  # closed  (merged pass is skipped entirely in fast mode)
    ]
    activity = provider.get_activity(start_date, end_date, fast=True)

    assert len(activity["org/repo"]["prs_reviewed"]) == 1
    # Only 4 searches should run in fast mode (no merged query).
    assert mock_github_instance.search_issues.call_count == 4


@patch("gitreport.providers.github.Github")
def test_github_merged_not_reviewed_goes_to_merged_category(MockGithub):
    """A PR merged by the user but not reviewed appears under prs_merged."""
    mock_github_instance = MockGithub.return_value
    provider = GitHubProvider(username="testuser", token="fake-token")
    start_date = datetime(2024, 1, 1, tzinfo=UTC)
    end_date = datetime(2024, 1, 31, tzinfo=UTC)

    repo = MockRepository("org/repo", private=False)
    merged_pr = MockPullRequest(merged_by=MockUser("testuser"))
    merged_issue = MockIssue(
        "Someone's PR", "http://pr/50", repo, "otheruser", 50, pull_request=merged_pr
    )

    mock_github_instance.search_issues.side_effect = [
        [],  # submitted
        [],  # issues_created
        [],  # reviewed
        [merged_issue],  # merged
        [],  # closed
    ]
    activity = provider.get_activity(start_date, end_date)

    assert len(activity["org/repo"]["prs_merged"]) == 1
    assert not activity["org/repo"]["prs_reviewed"]


@patch("gitreport.providers.github.Github")
def test_github_merged_and_reviewed_annotates_reviewed(MockGithub):
    """A PR both reviewed and merged by the user is annotated, not duplicated."""
    mock_github_instance = MockGithub.return_value
    provider = GitHubProvider(username="testuser", token="fake-token")
    start_date = datetime(2024, 1, 1, tzinfo=UTC)
    end_date = datetime(2024, 1, 31, tzinfo=UTC)

    repo = MockRepository("org/repo", private=False)
    review = MockReview("testuser", datetime(2024, 1, 10, tzinfo=UTC))
    reviewed_pr = MockPullRequest(reviews=[review])
    reviewed_issue = MockIssue(
        "Reviewed+Merged", "http://pr/60", repo, "otheruser", 60, pull_request=reviewed_pr
    )
    merged_pr = MockPullRequest(merged_by=MockUser("testuser"))
    merged_issue = MockIssue(
        "Reviewed+Merged", "http://pr/60", repo, "otheruser", 60, pull_request=merged_pr
    )

    mock_github_instance.search_issues.side_effect = [
        [],  # submitted
        [],  # issues_created
        [reviewed_issue],  # reviewed
        [merged_issue],  # merged
        [],  # closed
    ]
    activity = provider.get_activity(start_date, end_date)

    reviewed = activity["org/repo"]["prs_reviewed"]
    assert len(reviewed) == 1
    assert reviewed[0].get("also_merged") is True
    assert not activity["org/repo"]["prs_merged"]
