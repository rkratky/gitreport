from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from gitreport.providers.launchpad import LP_CREDENTIALS_PATH, LaunchpadProvider

# --- Mock Objects ---


class MockLaunchpadObject:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class MockLaunchpadPerson(MockLaunchpadObject):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.getMergeProposals = MagicMock(return_value=[])
        self.getRequestedReviews = MagicMock(return_value=[])


class MockComment:
    def __init__(self, date_created):
        self.date_created = date_created


class MockVote:
    def __init__(self, reviewer, registrant, comment=None, is_pending=False):
        self.reviewer = reviewer
        self.registrant = registrant
        self.comment = comment
        self.is_pending = is_pending


def _mp(web_link, repo="proj/repo", private=False, **kwargs):
    defaults = dict(
        web_link=web_link,
        target_git_repository=MockLaunchpadObject(unique_name=repo, private=private),
        votes=[],
        queue_status="Needs review",
        date_merged=None,
        date_reviewed=None,
        merge_reporter=None,
        registrant=None,
    )
    defaults.update(kwargs)
    return MockLaunchpadObject(**defaults)


# --- Tests ---


@patch("gitreport.providers.launchpad.Launchpad")
def test_launchpad_bugs_and_submitted(MockLaunchpad):
    """Bugs created/closed and submitted MPs are collected within range."""
    mock_lp_instance = MockLaunchpad.login_with.return_value
    me = MockLaunchpadPerson(name="testuser")
    mock_lp_instance.me = me
    mock_lp_instance.bugs = MagicMock()

    provider = LaunchpadProvider(username="testuser")
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 1, 31, tzinfo=UTC)

    bug_created = MockLaunchpadObject(
        title="New bug",
        web_link="http://bug/1",
        bug_target_name="proj/one",
        bug_target=MockLaunchpadObject(private=False),
        date_created=datetime(2024, 1, 5, tzinfo=UTC),
        date_closed=None,
    )
    bug_closed = MockLaunchpadObject(
        title="Old bug",
        web_link="http://bug/2",
        bug_target_name="proj/two",
        bug_target=MockLaunchpadObject(private=True),
        date_created=datetime(2024, 1, 10, tzinfo=UTC),
        date_closed=datetime(2024, 1, 20, tzinfo=UTC),
    )

    def search_tasks(**kwargs):
        if "bug_reporter" in kwargs:
            return [bug_created]
        if "assignee" in kwargs:
            return [bug_closed]
        return []

    mock_lp_instance.bugs.searchTasks.side_effect = search_tasks

    submitted = _mp(
        "http://mp/submitted",
        repo="proj/submitted",
        date_created=datetime(2024, 1, 11, tzinfo=UTC),
    )
    me.getMergeProposals.return_value = [submitted]

    activity = provider.get_activity(start, end)

    assert activity["proj/one"]["visibility"] == "public"
    assert len(activity["proj/one"]["issues_created"]) == 1
    assert activity["proj/two"]["visibility"] == "private"
    assert len(activity["proj/two"]["issues_closed"]) == 1
    assert len(activity["proj/submitted"]["prs_submitted"]) == 1

    MockLaunchpad.login_with.assert_called_with(
        "gitreport-cli",
        "production",
        credentials_file=str(LP_CREDENTIALS_PATH),
        version="devel",
    )


@patch("gitreport.providers.launchpad.Launchpad")
def test_launchpad_reviews_requested_and_claimed_merged(MockLaunchpad):
    """Both requested and self-claimed reviews land under prs_reviewed."""
    mock_lp_instance = MockLaunchpad.login_with.return_value
    me = MockLaunchpadPerson(name="testuser")
    other = MockLaunchpadPerson(name="otheruser")
    mock_lp_instance.me = me
    mock_lp_instance.bugs = MagicMock()
    mock_lp_instance.bugs.searchTasks.return_value = []

    provider = LaunchpadProvider(username="testuser")
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 1, 31, tzinfo=UTC)

    review_date = datetime(2024, 1, 15, tzinfo=UTC)

    # Requested: registrant is someone else.
    requested = _mp(
        "http://mp/requested",
        repo="proj/requested",
        private=True,
        votes=[MockVote(reviewer=me, registrant=other, comment=MockComment(review_date))],
    )
    # Claimed: registrant is the user themselves (no prior request).
    claimed = _mp(
        "http://mp/claimed",
        repo="proj/claimed",
        votes=[MockVote(reviewer=me, registrant=me, comment=MockComment(review_date))],
    )
    # Pending: review requested but not yet cast -> must be ignored.
    pending = _mp(
        "http://mp/pending",
        repo="proj/pending",
        votes=[MockVote(reviewer=me, registrant=other, is_pending=True)],
    )

    me.getRequestedReviews.return_value = [requested, claimed, pending]

    activity = provider.get_activity(start, end)

    assert len(activity["proj/requested"]["prs_reviewed"]) == 1
    assert activity["proj/requested"]["visibility"] == "private"
    assert len(activity["proj/claimed"]["prs_reviewed"]) == 1
    # Pending review created no activity, so the repo is absent.
    assert "proj/pending" not in activity


@patch("gitreport.providers.launchpad.Launchpad")
def test_launchpad_merged_by_user_not_authored(MockLaunchpad):
    """MP merged by the user (not authored, not reviewed) -> prs_merged."""
    mock_lp_instance = MockLaunchpad.login_with.return_value
    me = MockLaunchpadPerson(name="testuser")
    other = MockLaunchpadPerson(name="otheruser")
    mock_lp_instance.me = me
    mock_lp_instance.bugs = MagicMock()
    mock_lp_instance.bugs.searchTasks.return_value = []

    provider = LaunchpadProvider(username="testuser")
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 1, 31, tzinfo=UTC)

    merged = _mp(
        "http://mp/merged",
        repo="proj/merged",
        queue_status="Merged",
        date_merged=datetime(2024, 1, 20, tzinfo=UTC),
        merge_reporter=me,
        registrant=other,  # authored by someone else
        votes=[],  # not reviewed by the user
    )
    me.getRequestedReviews.return_value = [merged]

    activity = provider.get_activity(start, end)

    assert len(activity["proj/merged"]["prs_merged"]) == 1
    assert not activity["proj/merged"]["prs_reviewed"]


@patch("gitreport.providers.launchpad.Launchpad")
def test_launchpad_merged_and_reviewed_annotates(MockLaunchpad):
    """MP both reviewed and merged by the user -> annotated, not duplicated."""
    mock_lp_instance = MockLaunchpad.login_with.return_value
    me = MockLaunchpadPerson(name="testuser")
    other = MockLaunchpadPerson(name="otheruser")
    mock_lp_instance.me = me
    mock_lp_instance.bugs = MagicMock()
    mock_lp_instance.bugs.searchTasks.return_value = []

    provider = LaunchpadProvider(username="testuser")
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 1, 31, tzinfo=UTC)

    review_date = datetime(2024, 1, 12, tzinfo=UTC)
    mp = _mp(
        "http://mp/both",
        repo="proj/both",
        queue_status="Merged",
        date_merged=datetime(2024, 1, 20, tzinfo=UTC),
        merge_reporter=me,
        registrant=other,
        votes=[MockVote(reviewer=me, registrant=me, comment=MockComment(review_date))],
    )
    me.getRequestedReviews.return_value = [mp]

    activity = provider.get_activity(start, end)

    reviewed = activity["proj/both"]["prs_reviewed"]
    assert len(reviewed) == 1
    assert reviewed[0].get("also_merged") is True
    assert not activity["proj/both"]["prs_merged"]
