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


# --- Attention tests ---


class MockMp:
    def __init__(
        self, web_link, project="~u/+git/repo", votes=None, date_created=None, registrant=None
    ):
        self.web_link = web_link
        self.queue_status = "Needs review"
        self.votes = votes or []
        self.date_created = date_created or datetime(2026, 1, 1, tzinfo=UTC)
        self.registrant = registrant
        # _mp_repo() reads target_git_repository.unique_name
        self.target_git_repository = MagicMock(unique_name=f"~u/+git/{project}", private=False)


def _lp_provider():
    with patch("gitreport.providers.launchpad.Launchpad"):
        provider = LaunchpadProvider(username="testuser", token=None)
        provider._launchpad.me = MagicMock()
        provider._launchpad.me.getMergeProposals.return_value = []
        provider._launchpad.bugs = MagicMock()
        provider._launchpad.bugs.searchTasks.return_value = []
        provider._launchpad.load = MagicMock(return_value=None)
        return provider


def _bug_mock(project, status="New", date_last_updated=None):
    bug = MagicMock()
    bug.bug_target_name = project
    bug.web_link = "https://launchpad.net/bugs/77"
    bug.title = "Bug 77"
    bug.status = status
    bug.date_last_updated = date_last_updated or datetime(2026, 9, 27, tzinfo=UTC)
    bug.bug_target = MagicMock(private=False)
    return bug


def _bug_search_side_effect(assigned=None, subscribed=None):
    def side_effect(**kwargs):
        if "assignee" in kwargs:
            return list(assigned or [])
        if "bug_subscriber" in kwargs:
            return list(subscribed or [])
        return []

    return side_effect


def test_lp_mp_needs_review():
    provider = _lp_provider()
    provider._launchpad.me.getRequestedReviews.return_value = [
        MockMp("https://launchpad.net/~u/+git/repo/+merge/1")
    ]
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))
    assert fetch["ok"] is True
    kinds = [i["kind"] for i in fetch["items"]]
    assert "lp_mp_needs_review" in kinds
    # Exclusions apply to the MP's repo name.
    provider._launchpad.me.getRequestedReviews.return_value = [
        MockMp("https://launchpad.net/~u/+git/noise/+merge/2", project="noise")
    ]
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC), exclusions=["*noise*"])
    assert fetch["items"] == []


def test_lp_assigned_bugs_no_time_window():
    provider = _lp_provider()
    # A bug with an old date_last_updated must still appear (parity with GH).
    provider._launchpad.bugs.searchTasks.side_effect = _bug_search_side_effect(
        assigned=[_bug_mock("proj", date_last_updated=datetime(2026, 1, 1, tzinfo=UTC))],
        subscribed=[],
    )
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))
    kinds = [i["kind"] for i in fetch["items"]]
    assert "issue_assigned" in kinds


def test_lp_resolved_ids_proven_closed():
    provider = _lp_provider()
    provider._launchpad.bugs.searchTasks.side_effect = _bug_search_side_effect()
    closed_bug = _bug_mock("proj", status="Fix Released")
    provider._launchpad.load.return_value = closed_bug
    state_items = {
        "lp:https://launchpad.net/bugs/77": {
            "url": "https://launchpad.net/bugs/77",
            "provider": "launchpad",
            "status": "open",
        }
    }
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC), state_items=state_items)
    assert "lp:https://launchpad.net/bugs/77" in fetch["resolved_ids"]


def test_lp_fetch_failure_marks_not_ok():
    provider = _lp_provider()
    provider._launchpad.me.getRequestedReviews.side_effect = RuntimeError("lp down")
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))
    assert fetch["ok"] is False
    assert "lp down" in fetch["error"]


# --- Fix round 1 regression tests ---


def test_proven_resolved_uses_api_url():
    """BUG-01: web URLs must be mapped to API URLs before launchpadlib load()."""
    provider = _lp_provider()
    provider._launchpad.load.return_value = _bug_mock("proj", status="Fix Released")
    state_items = {
        "lp:https://launchpad.net/bugs/77": {
            "url": "https://launchpad.net/bugs/77",
            "provider": "launchpad",
            "status": "open",
        }
    }
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC), state_items=state_items)
    called_url = provider._launchpad.load.call_args[0][0]
    assert called_url.startswith("https://api.launchpad.net/devel/")
    assert "lp:https://launchpad.net/bugs/77" in fetch["resolved_ids"]


def test_latest_foreign_event_walks_all_comments():
    """BUG-02a: a plain comment (all_comments) by someone else after since -> item."""
    provider = _lp_provider()
    comment = MockComment(datetime(2026, 9, 28, tzinfo=UTC))
    other = MockLaunchpadPerson(name="otheruser")
    mp = _mp(
        "https://launchpad.net/~u/+git/repo/+merge/9",
        votes=[],
        all_comments=[MockLaunchpadObject(author=other, date_created=comment.date_created)],
    )
    provider._launchpad.me.getMergeProposals.return_value = [mp]
    fetch = provider.get_attention(datetime(2026, 9, 27, tzinfo=UTC))
    kinds = [i["kind"] for i in fetch["items"]]
    assert "lp_mp_comment" in kinds


def test_latest_foreign_event_own_comment_no_item():
    """BUG-02b: the user's own all_comments entry must not produce an item."""
    provider = _lp_provider()
    me = provider._launchpad.me
    mp = _mp(
        "https://launchpad.net/~u/+git/repo/+merge/9",
        votes=[],
        all_comments=[
            MockLaunchpadObject(author=me, date_created=datetime(2026, 9, 28, tzinfo=UTC))
        ],
    )
    provider._launchpad.me.getMergeProposals.return_value = [mp]
    fetch = provider.get_attention(datetime(2026, 9, 27, tzinfo=UTC))
    assert [i for i in fetch["items"] if i["kind"] == "lp_mp_comment"] == []


def test_getRequestedReviews_called_without_kwargs():  # noqa: N802
    """BUG-04: getRequestedReviews uses default status (todo list semantics)."""
    provider = _lp_provider()
    provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))
    provider._launchpad.me.getRequestedReviews.assert_called_once_with()


def test_searchTasks_constraint_calls():  # noqa: N802
    """BUG-04: searchTasks assignee= has no modified_since; subscriber call carries it."""
    provider = _lp_provider()
    since = datetime(2026, 9, 28, tzinfo=UTC)
    provider.get_attention(since)
    calls = provider._launchpad.bugs.searchTasks.call_args_list
    assert all("modified_since" not in c.kwargs for c in calls if "assignee" in c.kwargs)
    assert any("modified_since" in c.kwargs for c in calls if "bug_subscriber" in c.kwargs)


def test_load_failure_is_not_proof():
    """BUG-04: load() raising leaves resolved_ids empty even for acked/open records."""
    provider = _lp_provider()
    provider._launchpad.load.side_effect = RuntimeError("api down")
    state_items = {
        "lp:https://launchpad.net/bugs/77": {
            "url": "https://launchpad.net/bugs/77",
            "provider": "launchpad",
            "status": "open",
        }
    }
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC), state_items=state_items)
    assert fetch["resolved_ids"] == []


def test_proven_resolved_includes_acked_records():
    """N-01: acked windowed records are re-loaded; a proven-closed bug resolves them."""
    provider = _lp_provider()
    provider._launchpad.load.return_value = _bug_mock("proj", status="Fix Released")
    state_items = {
        "lp:https://launchpad.net/bugs/88": {
            "url": "https://launchpad.net/bugs/88",
            "provider": "launchpad",
            "status": "acked",
            "kinds": ["lp_bug_activity"],
        }
    }
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC), state_items=state_items)
    assert "lp:https://launchpad.net/bugs/88" in fetch["resolved_ids"]


def test_load_failure_not_proof_for_acked():
    """BUG-04 remainder: load() raising on an acked record leaves resolved_ids empty."""
    provider = _lp_provider()
    provider._launchpad.load.side_effect = RuntimeError("api down")
    state_items = {
        "lp:https://launchpad.net/bugs/88": {
            "url": "https://launchpad.net/bugs/88",
            "provider": "launchpad",
            "status": "acked",
            "kinds": ["lp_bug_activity"],
        }
    }
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC), state_items=state_items)
    assert fetch["resolved_ids"] == []


def test_naive_since_gets_utc():
    """BUG-05: a naive `since` is treated as UTC, not a TypeError."""
    provider = _lp_provider()
    # datetime.min.isoformat has no tz; without the guard, comparison with
    # tz-aware datetimes raises TypeError and ok becomes False. A foreign
    # comment after the naive since (interpreted as UTC) must produce an
    # item — proving the window comparison actually ran.
    other = MockLaunchpadPerson(name="otheruser")
    mp = _mp(
        "https://launchpad.net/~u/+git/repo/+merge/5",
        votes=[],
        all_comments=[
            MockLaunchpadObject(author=other, date_created=datetime(2026, 9, 28, 12, 0, tzinfo=UTC))
        ],
    )
    provider._launchpad.me.getMergeProposals.return_value = [mp]
    fetch = provider.get_attention(datetime(2026, 9, 28))  # naive midnight
    assert fetch["ok"] is True
    kinds = [i["kind"] for i in fetch["items"]]
    assert "lp_mp_comment" in kinds


def test_dedupe_bug_assigned_and_subscribed():
    """BUG-06: a bug both assigned and subscribed produces ONE item with both reasons."""
    provider = _lp_provider()
    bug = _bug_mock("proj")
    provider._launchpad.bugs.searchTasks.side_effect = _bug_search_side_effect(
        assigned=[bug], subscribed=[bug]
    )
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))
    bug_items = [i for i in fetch["items"] if i["id"] == "lp:https://launchpad.net/bugs/77"]
    assert len(bug_items) == 1
    assert "assigned to you" in bug_items[0]["reason"]
    assert "new activity on subscribed bug" in bug_items[0]["reason"]
