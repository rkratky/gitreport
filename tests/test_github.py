from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from gitreport.providers.github import GitHubProvider

# --- Mock Objects ---


class MockRepository:
    def __init__(self, name, private=False):
        self.full_name = name
        self.private = private
        self.html_url = f"https://github.com/{name}"


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
        self.updated_at = datetime(2026, 9, 28, tzinfo=UTC)
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


# --- Attention tests ---


class MockNotification:
    def __init__(
        self,
        reason,
        repo="org/repo",
        title="T",
        subject_url="https://api.github.com/repos/org/repo/pulls/1",
        thread_url="https://api.github.com/notifications/threads/1",
        updated_at=None,
        subject_type="PullRequest",
        repository: bool | None = True,  # None: the notification carries no repository
    ):
        self.reason = reason
        self.url = thread_url
        self.updated_at = updated_at or datetime(2026, 9, 28, tzinfo=UTC)
        self.subject = type("S", (), {"title": title, "url": subject_url, "type": subject_type})()
        self.repository = MockRepository(repo) if repository else repository


class MockCheckRun:
    def __init__(self, conclusion):
        self.conclusion = conclusion


def _provider_with_notifications(notifications, check_runs=None):
    """GitHubProvider whose Github client returns the given notifications."""
    with patch("gitreport.providers.github.Github") as mock_github_cls:
        instance = mock_github_cls.return_value
        instance.get_user.return_value.get_notifications.return_value = notifications
        mock_repo = MagicMock()
        mock_pr = MagicMock()
        mock_pr.head.sha = "head-sha"
        mock_repo.get_pull.return_value = mock_pr
        mock_repo.get_commit.return_value.get_check_runs.return_value = [
            MockCheckRun(c) for c in (check_runs or [])
        ]
        instance.get_repo.return_value = mock_repo
        instance.search_issues.return_value = []
        empty_search = {"data": {"search": {"pageInfo": {"hasNextPage": False}, "nodes": []}}}
        instance.requester.graphql_query.return_value = (empty_search, ())
        provider = GitHubProvider(username="testuser", token="fake-token")
        # Keep the mocked client for the call under test (the `with` block
        # only scopes the constructor patch).
        provider._github = instance
    return provider


def _notification_mock(reason, repo="org/repo"):
    # repo="me/fork-x" cases pass a full name; derive the subject url from it.
    subject_url = f"https://api.github.com/repos/{repo}/pulls/1"
    thread_url = "https://api.github.com/notifications/threads/1"
    return MockNotification(reason, repo=repo, subject_url=subject_url, thread_url=thread_url)


def test_github_notification_reason_kinds():
    reasons = ["review_requested", "mention", "team_mention", "comment", "author", "assign"]
    notifications = [_notification_mock(r) for r in reasons]
    provider = _provider_with_notifications(notifications)
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))
    assert fetch["ok"] is True
    assert sorted(i["kind"] for i in fetch["items"]) == sorted(
        ["review_requested", "mention", "mention", "comment", "comment", "issue_assigned"]
    )


def test_github_subscribed_reason_excluded():
    provider = _provider_with_notifications([_notification_mock("subscribed")])
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))
    assert fetch["ok"] is True
    assert fetch["items"] == []


def test_github_ci_activity_only_on_failure():
    provider = _provider_with_notifications(
        [_notification_mock("ci_activity")],
        check_runs=["success"],
    )
    assert provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))["items"] == []

    provider = _provider_with_notifications(
        [_notification_mock("ci_activity")],
        check_runs=["failure"],
    )
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))
    assert len(fetch["items"]) == 1
    assert fetch["items"][0]["kind"] == "ci_failure"
    # The check-run lookup must target the repo derived from the notification.
    provider._github.get_repo.assert_called_with("org/repo")


def test_github_exclusion_globs():
    provider = _provider_with_notifications([_notification_mock("mention", repo="me/fork-x")])
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC), exclusions=["me/fork-*"])
    assert fetch["items"] == []


def test_github_notification_repo_full_name_from_repository():
    """The repo name comes from the notification's repository, not the subject url."""
    provider = _provider_with_notifications([_notification_mock("mention")])
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))
    assert [i["repo"] for i in fetch["items"]] == ["org/repo"]


def test_github_notification_exact_repo_exclusion_drops_item():
    provider = _provider_with_notifications([_notification_mock("mention")])
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC), exclusions=["org/repo"])
    assert fetch["items"] == []


def test_github_notification_other_glob_keeps_item():
    provider = _provider_with_notifications([_notification_mock("mention")])
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC), exclusions=["other/*"])
    assert len(fetch["items"]) == 1


def test_github_notification_and_graphql_share_id():
    """A notification (api url) and a GraphQL node (html url) for the same PR
    must produce the same dedupe id."""
    notification = MockNotification(
        "mention",
        repo="o/r",
        subject_url="https://api.github.com/repos/o/r/pulls/5",
        thread_url="https://api.github.com/notifications/threads/5",
    )
    provider = _provider_with_notifications([notification])
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))
    notification_id = fetch["items"][0]["id"]

    with patch("gitreport.providers.github.Github") as mock_github_cls:
        instance = mock_github_cls.return_value
        instance.get_user.return_value.get_notifications.return_value = []
        instance.search_issues.return_value = []
        graphql_payload = {
            "data": {
                "search": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {
                            "url": "https://github.com/o/r/pull/5",
                            "title": "PR 5",
                            "updatedAt": "2026-09-27T10:00:00Z",
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [{"isResolved": False}],
                            },
                        }
                    ],
                }
            }
        }
        instance.requester.graphql_query.return_value = (graphql_payload, ())
        thread_provider = GitHubProvider(username="testuser", token="fake-token")
        thread_provider._github = instance
        thread_fetch = thread_provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))

    assert notification_id == "gh:https://github.com/o/r/pull/5"
    assert thread_fetch["items"][0]["id"] == notification_id


def test_github_commit_subject_gets_commit_html_url():
    """Commit subjects are rewritten to the html commit url (like PR/Issue)."""
    notification = MockNotification(
        "mention",
        repo="o/r",
        subject_url="https://api.github.com/repos/o/r/commits/abc123",
        subject_type="Commit",
    )
    provider = _provider_with_notifications([notification])
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))
    assert fetch["items"][0]["url"] == "https://github.com/o/r/commit/abc123"


def test_github_release_subject_shows_repo_html_url():
    """Non-rewritable subjects (releases, ...) display the repository page;
    the thread url stays only in the thread_url field."""
    notification = MockNotification(
        "comment",
        subject_url="https://api.github.com/repos/org/repo/releases/99",
        thread_url="https://api.github.com/notifications/threads/1",
        subject_type="Release",
    )
    provider = _provider_with_notifications([notification])
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))
    assert fetch["items"][0]["url"] == "https://github.com/org/repo"
    assert fetch["items"][0]["thread_url"] == "https://api.github.com/notifications/threads/1"


def test_github_release_notifications_have_distinct_ids():
    """Two non-rewritable-subject notifications (e.g. releases) in the same
    repo must not collapse into one dedupe id: identity comes from the
    per-notification thread url, display from the repository page."""
    notifications = [
        MockNotification(
            "comment",
            repo="org/repo",
            title="v1.0",
            subject_url="https://api.github.com/repos/org/repo/releases/99",
            thread_url="https://api.github.com/notifications/threads/101",
            subject_type="Release",
        ),
        MockNotification(
            "mention",
            repo="org/repo",
            title="v2.0",
            subject_url="https://api.github.com/repos/org/repo/releases/100",
            thread_url="https://api.github.com/notifications/threads/102",
            subject_type="Release",
        ),
    ]
    provider = _provider_with_notifications(notifications)
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))
    # Distinct thread urls -> distinct ids (dedupe must not fold them).
    assert [i["id"] for i in fetch["items"]] == [
        "gh:https://api.github.com/notifications/threads/101",
        "gh:https://api.github.com/notifications/threads/102",
    ]
    # Display stays the shared repository page for both.
    assert [i["url"] for i in fetch["items"]] == [
        "https://github.com/org/repo",
        "https://github.com/org/repo",
    ]


def test_github_notification_repo_from_subject_url_when_repository_none():
    """Without a repository object the repo is parsed from the subject url."""
    notification = MockNotification(
        "mention",
        repository=None,
        subject_url="https://api.github.com/repos/org/repo/pulls/1",
    )
    provider = _provider_with_notifications([notification])
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))
    assert fetch["items"][0]["repo"] == "org/repo"


def test_github_query_items_kinds():
    with patch("gitreport.providers.github.Github") as mock_github_cls:
        instance = mock_github_cls.return_value
        instance.get_user.return_value.get_notifications.return_value = []

        failing_pr = MockIssue(
            "Fix CI",
            "https://github.com/o/r/pull/9",
            MockRepository("o/r"),
            "testuser",
            9,
        )
        assigned_issue = MockIssue(
            "Bug",
            "https://github.com/o/r/issues/2",
            MockRepository("o/r"),
            "other",
            2,
        )
        stale_pr = MockIssue(
            "Stale",
            "https://github.com/o/r/pull/3",
            MockRepository("o/r"),
            "testuser",
            3,
        )

        def search_side_effect(query, **kwargs):
            if "status:failure" in query:
                return [failing_pr]
            if "assignee:" in query:
                return [assigned_issue]
            if "updated:<" in query:
                return [stale_pr]
            return []

        instance.search_issues.side_effect = search_side_effect
        instance.requester.graphql_query.return_value = (
            {"data": {"search": {"pageInfo": {"hasNextPage": False}, "nodes": []}}},
            (),
        )
        provider = GitHubProvider(username="testuser", token="fake-token")
        provider._github = instance
        fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC), stale_pr_days=7)

    kinds = sorted(i["kind"] for i in fetch["items"])
    assert kinds == ["ci_failure", "issue_assigned", "stale_pr"]


def test_github_thread_unresolved_via_graphql():
    with patch("gitreport.providers.github.Github") as mock_github_cls:
        instance = mock_github_cls.return_value
        instance.get_user.return_value.get_notifications.return_value = []
        instance.search_issues.return_value = []
        graphql_payload = {
            "data": {
                "search": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {
                            "url": "https://github.com/o/r/pull/5",
                            "title": "PR with threads",
                            "updatedAt": "2026-09-27T10:00:00Z",
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [{"isResolved": True}, {"isResolved": False}],
                            },
                        }
                    ],
                }
            }
        }
        instance.requester.graphql_query.return_value = (graphql_payload, ())
        provider = GitHubProvider(username="testuser", token="fake-token")
        provider._github = instance
        fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))

    assert fetch["ok"] is True
    assert [i["kind"] for i in fetch["items"]] == ["thread_unresolved"]
    assert fetch["items"][0]["repo"] == "o/r"  # parsed from the html url
    assert fetch["items"][0]["id"] == "gh:https://github.com/o/r/pull/5"


def test_github_truncated_review_threads_skip_item(capsys):
    """A PR with more review threads than the query fetches is skipped entirely."""
    with patch("gitreport.providers.github.Github") as mock_github_cls:
        instance = mock_github_cls.return_value
        instance.get_user.return_value.get_notifications.return_value = []
        instance.search_issues.return_value = []
        truncated = {
            "data": {
                "search": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {
                            "url": "https://github.com/o/r/pull/7",
                            "title": "Too many threads",
                            "updatedAt": "2026-09-27T10:00:00Z",
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": True},
                                "nodes": [{"isResolved": False}],
                            },
                        }
                    ],
                }
            }
        }
        instance.requester.graphql_query.return_value = (truncated, ())
        provider = GitHubProvider(username="testuser", token="fake-token")
        provider._github = instance
        fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))

    assert fetch["ok"] is True
    assert fetch["items"] == []  # truncated threads: item dropped
    assert "more than 100 review threads" in capsys.readouterr().err


def test_github_excluded_repo_truncation_does_not_warn(capsys):
    """Excluded repos are dropped before the truncation check: no warning."""
    with patch("gitreport.providers.github.Github") as mock_github_cls:
        instance = mock_github_cls.return_value
        instance.get_user.return_value.get_notifications.return_value = []
        instance.search_issues.return_value = []
        truncated = {
            "data": {
                "search": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {
                            "url": "https://github.com/me/excluded/pull/7",
                            "title": "Excluded with too many threads",
                            "updatedAt": "2026-09-27T10:00:00Z",
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": True},
                                "nodes": [{"isResolved": False}],
                            },
                        }
                    ],
                }
            }
        }
        instance.requester.graphql_query.return_value = (truncated, ())
        provider = GitHubProvider(username="testuser", token="fake-token")
        provider._github = instance
        fetch = provider.get_attention(
            datetime(2026, 9, 28, tzinfo=UTC), exclusions=["me/excluded"]
        )

    assert fetch["ok"] is True
    assert fetch["items"] == []  # excluded repo: dropped silently
    assert "more than 100 review threads" not in capsys.readouterr().err


def test_github_null_review_threads_does_not_crash():
    """An explicit null reviewThreads value must not crash the fetch."""
    with patch("gitreport.providers.github.Github") as mock_github_cls:
        instance = mock_github_cls.return_value
        instance.get_user.return_value.get_notifications.return_value = []
        instance.search_issues.return_value = []
        payload = {
            "data": {
                "search": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {
                            "url": "https://github.com/o/r/pull/8",
                            "title": "Null threads",
                            "updatedAt": "2026-09-27T10:00:00Z",
                            "reviewThreads": None,
                        }
                    ],
                }
            }
        }
        instance.requester.graphql_query.return_value = (payload, ())
        provider = GitHubProvider(username="testuser", token="fake-token")
        provider._github = instance
        fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))

    assert fetch["ok"] is True
    assert fetch["items"] == []


def test_github_null_nodes_in_review_threads_does_not_crash():
    """reviewThreads.nodes explicitly null (key present, value None):
    no crash, no items."""
    with patch("gitreport.providers.github.Github") as mock_github_cls:
        instance = mock_github_cls.return_value
        instance.get_user.return_value.get_notifications.return_value = []
        instance.search_issues.return_value = []
        payload = {
            "data": {
                "search": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {
                            "url": "https://github.com/o/r/pull/8",
                            "title": "Null nodes",
                            "updatedAt": "2026-09-27T10:00:00Z",
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": None,
                            },
                        }
                    ],
                }
            }
        }
        instance.requester.graphql_query.return_value = (payload, ())
        provider = GitHubProvider(username="testuser", token="fake-token")
        provider._github = instance
        fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))

    assert fetch["ok"] is True
    assert fetch["items"] == []


def test_github_null_page_info_does_not_crash():
    """An explicit null pageInfo value must not crash the fetch."""
    with patch("gitreport.providers.github.Github") as mock_github_cls:
        instance = mock_github_cls.return_value
        instance.get_user.return_value.get_notifications.return_value = []
        instance.search_issues.return_value = []
        payload = {
            "data": {
                "search": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {
                            "url": "https://github.com/o/r/pull/8",
                            "title": "Null pageInfo",
                            "updatedAt": "2026-09-27T10:00:00Z",
                            "reviewThreads": {
                                "pageInfo": None,
                                "nodes": [{"isResolved": False}],
                            },
                        }
                    ],
                }
            }
        }
        instance.requester.graphql_query.return_value = (payload, ())
        provider = GitHubProvider(username="testuser", token="fake-token")
        provider._github = instance
        fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))

    assert fetch["ok"] is True
    assert [i["kind"] for i in fetch["items"]] == ["thread_unresolved"]


def test_github_thread_pagination_uses_end_cursor():
    with patch("gitreport.providers.github.Github") as mock_github_cls:
        instance = mock_github_cls.return_value
        instance.get_user.return_value.get_notifications.return_value = []
        instance.search_issues.return_value = []
        page_one = {
            "data": {
                "search": {
                    "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                    "nodes": [
                        {
                            "url": "https://github.com/o/r/pull/5",
                            "title": "Page one PR",
                            "updatedAt": "2026-09-27T10:00:00Z",
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [{"isResolved": False}],
                            },
                        }
                    ],
                }
            }
        }
        page_two = {"data": {"search": {"pageInfo": {"hasNextPage": False}, "nodes": []}}}
        instance.requester.graphql_query.side_effect = [(page_one, ()), (page_two, ())]
        provider = GitHubProvider(username="testuser", token="fake-token")
        provider._github = instance
        fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))

    assert fetch["ok"] is True
    assert [i["kind"] for i in fetch["items"]] == ["thread_unresolved"]
    calls = instance.requester.graphql_query.call_args_list
    assert len(calls) == 2
    assert calls[0].args[1] == {"q": "is:pr is:open author:testuser"}
    assert calls[1].args[1] == {"q": "is:pr is:open author:testuser", "cursor": "cursor-1"}


def test_github_fetch_failure_marks_not_ok():
    with patch("gitreport.providers.github.Github") as mock_github_cls:
        instance = mock_github_cls.return_value
        instance.get_user.return_value.get_notifications.side_effect = RuntimeError("boom")
        provider = GitHubProvider(username="testuser", token="fake-token")
        provider._github = instance
        fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))

    assert fetch["ok"] is False
    assert "boom" in fetch["error"]
    assert fetch["items"] == []
