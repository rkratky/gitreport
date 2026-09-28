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
def test_launchpad_bugs_and_submitted(mock_launchpad_cls):
    """Bugs created/closed and submitted MPs are collected within range."""
    mock_lp_instance = mock_launchpad_cls.login_with.return_value
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

    mock_launchpad_cls.login_with.assert_called_with(
        "gitreport-cli",
        "production",
        credentials_file=str(LP_CREDENTIALS_PATH),
        version="devel",
    )


@patch("gitreport.providers.launchpad.Launchpad")
def test_launchpad_reviews_requested_and_claimed_merged(mock_launchpad_cls):
    """Both requested and self-claimed reviews land under prs_reviewed."""
    mock_lp_instance = mock_launchpad_cls.login_with.return_value
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
def test_launchpad_merged_by_user_not_authored(mock_launchpad_cls):
    """MP merged by the user (not authored, not reviewed) -> prs_merged."""
    mock_lp_instance = mock_launchpad_cls.login_with.return_value
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
def test_launchpad_merged_and_reviewed_annotates(mock_launchpad_cls):
    """MP both reviewed and merged by the user -> annotated, not duplicated."""
    mock_lp_instance = mock_launchpad_cls.login_with.return_value
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


def _bug_mock(project, status="New", date_last_updated=None, messages=None):
    """A bug-task mock. `messages` must be a list (launchpadlib exposes a
    paginated collection there); the default carries one foreign-authored
    message at date_last_updated so the author gate passes."""
    bug = MagicMock()
    bug.bug_target_name = project
    bug.web_link = "https://launchpad.net/bugs/77"
    bug.title = "Bug 77"
    bug.status = status
    bug.date_last_updated = date_last_updated or datetime(2026, 9, 27, tzinfo=UTC)
    bug.bug_target = MagicMock(private=False)
    if messages is None:
        reporter = MockLaunchpadObject(self_link="https://api.launchpad.net/devel/~otheruser")
        messages = [
            MockLaunchpadObject(
                owner=reporter,
                date_created=date_last_updated or datetime(2026, 9, 27, tzinfo=UTC),
            )
        ]
    bug.messages = messages
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


def test_lp_bug_self_authored_bump_is_presence_only():
    """FINAL-02: when the bug's newest message is the user's own, updated_at
    is None — the self-activity must not refresh or reopen the item."""
    provider = _lp_provider()
    me = provider._launchpad.me
    me.self_link = "https://api.launchpad.net/devel/~testuser"
    newest_self = MockLaunchpadObject(owner=me, date_created=datetime(2026, 9, 27, tzinfo=UTC))
    bug = _bug_mock("proj", messages=[newest_self])
    provider._launchpad.bugs.searchTasks.side_effect = _bug_search_side_effect(
        assigned=[bug], subscribed=[]
    )
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))
    assert [i["kind"] for i in fetch["items"]] == ["issue_assigned"]
    assert fetch["items"][0]["updated_at"] is None


def test_lp_bug_foreign_newest_message_keeps_freshness():
    """Final wave 2 BUG-B: a newest message by someone else author-gates the
    freshness through: updated_at is that foreign message's date_created —
    never bug.date_last_updated, which also moves on the user's own
    non-message edits (e.g. the user closing the task)."""
    provider = _lp_provider()
    foreign = MockLaunchpadObject(self_link="https://api.launchpad.net/devel/~otheruser")
    # T2 = date_last_updated (moved by the user's own non-message edit) is
    # newer than T1 = the newest foreign message; T1 must win.
    bug = _bug_mock(
        "proj",
        date_last_updated=datetime(2026, 9, 27, tzinfo=UTC),
        messages=[
            MockLaunchpadObject(owner=foreign, date_created=datetime(2026, 9, 26, tzinfo=UTC))
        ],
    )
    provider._launchpad.bugs.searchTasks.side_effect = _bug_search_side_effect(
        assigned=[bug], subscribed=[]
    )
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))
    assert fetch["items"][0]["updated_at"] == "2026-09-26T00:00:00+00:00"


def test_lp_bug_no_foreign_message_updated_at_none():
    """Final wave 2 BUG-B: when no foreign message exists, updated_at is None
    even though date_last_updated is set — the item stays presence-only."""
    provider = _lp_provider()
    me = provider._launchpad.me
    me.self_link = "https://api.launchpad.net/devel/~testuser"
    bug = _bug_mock(
        "proj",
        date_last_updated=datetime(2026, 9, 27, tzinfo=UTC),
        messages=[MockLaunchpadObject(owner=me, date_created=datetime(2026, 9, 26, tzinfo=UTC))],
    )
    provider._launchpad.bugs.searchTasks.side_effect = _bug_search_side_effect(
        assigned=[bug], subscribed=[]
    )
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))
    assert fetch["items"][0]["updated_at"] is None


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


def test_proven_resolved_user_voted_after_last_updated():
    """R5 leave rule: the user's own vote-comment after the record's
    last_updated proves the user responded — the open item leaves the inbox."""
    provider = _lp_provider()
    me = provider._launchpad.me
    me.self_link = "https://api.launchpad.net/devel/~testuser"
    mp = _mp(
        "https://launchpad.net/~u/+git/repo/+merge/11",
        votes=[
            MockVote(
                reviewer=me,
                registrant=me,
                comment=MockComment(datetime(2026, 9, 29, tzinfo=UTC)),
            )
        ],
    )
    provider._launchpad.load.return_value = mp
    state_items = {
        "lp:https://launchpad.net/~u/+git/repo/+merge/11": {
            "url": "https://launchpad.net/~u/+git/repo/+merge/11",
            "provider": "launchpad",
            "status": "open",
            "last_updated": "2026-09-28T00:00:00+00:00",
        }
    }
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC), state_items=state_items)
    assert "lp:https://launchpad.net/~u/+git/repo/+merge/11" in fetch["resolved_ids"]


def test_proven_resolved_user_comment_after_naive_last_updated():
    """R5 leave rule (naive-safe): a naive stored last_updated is read as
    UTC, so a later all_comments entry by the user still proves the leave."""
    provider = _lp_provider()
    me = provider._launchpad.me
    me.self_link = "https://api.launchpad.net/devel/~testuser"
    mp = _mp(
        "https://launchpad.net/~u/+git/repo/+merge/12",
        votes=[],
        all_comments=[
            MockLaunchpadObject(author=me, date_created=datetime(2026, 9, 28, 12, 0, tzinfo=UTC))
        ],
    )
    provider._launchpad.load.return_value = mp
    state_items = {
        "lp:https://launchpad.net/~u/+git/repo/+merge/12": {
            "url": "https://launchpad.net/~u/+git/repo/+merge/12",
            "provider": "launchpad",
            "status": "open",
            "last_updated": "2026-09-26T00:00:00",  # naive: read as UTC
        }
    }
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC), state_items=state_items)
    assert "lp:https://launchpad.net/~u/+git/repo/+merge/12" in fetch["resolved_ids"]


def test_proven_resolved_user_bug_message_after_last_updated():
    """R5 leave rule on bugs: the user's own message after the record's
    last_updated proves the user responded (naive stored ts read as UTC)."""
    provider = _lp_provider()
    me = provider._launchpad.me
    me.self_link = "https://api.launchpad.net/devel/~testuser"
    bug = _bug_mock(
        "proj",
        status="New",
        messages=[MockLaunchpadObject(owner=me, date_created=datetime(2026, 9, 28, 12, 0))],
    )
    provider._launchpad.load.return_value = bug
    state_items = {
        "lp:https://launchpad.net/bugs/77": {
            "url": "https://launchpad.net/bugs/77",
            "provider": "launchpad",
            "status": "open",
            "last_updated": "2026-09-26T00:00:00",  # naive stored ts
        }
    }
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC), state_items=state_items)
    assert "lp:https://launchpad.net/bugs/77" in fetch["resolved_ids"]


def test_proven_resolved_heuristic_exception_is_skipped():
    """R5: a probe failure inside the leave heuristic is skipped — never
    treated as proof of leaving, never a crash of the fetch."""

    class BoomIterable:
        def __iter__(self):
            raise RuntimeError("collection boom")

    provider = _lp_provider()
    me = provider._launchpad.me
    me.self_link = "https://api.launchpad.net/devel/~testuser"
    mp = _mp("https://launchpad.net/~u/+git/repo/+merge/13", votes=BoomIterable())
    provider._launchpad.load.return_value = mp
    state_items = {
        "lp:https://launchpad.net/~u/+git/repo/+merge/13": {
            "url": "https://launchpad.net/~u/+git/repo/+merge/13",
            "provider": "launchpad",
            "status": "open",
            "last_updated": "2026-09-26T00:00:00+00:00",
        }
    }
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC), state_items=state_items)
    assert fetch["ok"] is True
    assert fetch["resolved_ids"] == []


def test_proven_resolved_leave_rule_not_applied_to_acked():
    """R5 scoping: the leave heuristic only applies to open records; an
    acked record stays acked unless its live status is proven closed."""
    provider = _lp_provider()
    me = provider._launchpad.me
    me.self_link = "https://api.launchpad.net/devel/~testuser"
    mp = _mp(
        "https://launchpad.net/~u/+git/repo/+merge/14",
        votes=[
            MockVote(
                reviewer=me,
                registrant=me,
                comment=MockComment(datetime(2026, 9, 29, tzinfo=UTC)),
            )
        ],
    )
    provider._launchpad.load.return_value = mp
    state_items = {
        "lp:https://launchpad.net/~u/+git/repo/+merge/14": {
            "url": "https://launchpad.net/~u/+git/repo/+merge/14",
            "provider": "launchpad",
            "status": "acked",
            "last_updated": "2026-09-28T00:00:00+00:00",
        }
    }
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC), state_items=state_items)
    assert fetch["resolved_ids"] == []


def test_get_attention_me_failure_marks_not_ok():
    """MINOR-1 guard: a failing `self._launchpad.me` access must fail the
    whole fetch (existing behaviour), not silently degrade the pass."""
    provider = _lp_provider()

    class _MeBoom:
        @property
        def me(self):
            raise RuntimeError("me down")

    provider._launchpad = _MeBoom()
    fetch = provider.get_attention(datetime(2026, 9, 28, tzinfo=UTC))
    assert fetch["ok"] is False
    assert "me down" in fetch["error"]


def test_proven_resolved_person_none_closing_status_only():
    """MINOR-1: with person=None (failed user fetch) the closing-status
    checks still resolve items, but the user-authored leave rule is
    skipped — re-reading `me` inside the resolved pass must not let one
    transient failure mark the whole fetch stale."""
    provider = _lp_provider()
    me = provider._launchpad.me
    me.self_link = "https://api.launchpad.net/devel/~testuser"
    closed_bug = _bug_mock("proj", status="Fix Released")
    open_mp = _mp(
        "https://launchpad.net/~u/+git/repo/+merge/15",
        votes=[
            MockVote(
                reviewer=me,
                registrant=me,
                comment=MockComment(datetime(2026, 9, 29, tzinfo=UTC)),
            )
        ],
    )
    provider._launchpad.load.side_effect = lambda url: (
        closed_bug if url.endswith("bugs/77") else open_mp
    )
    state_items = {
        "lp:https://launchpad.net/bugs/77": {
            "url": "https://launchpad.net/bugs/77",
            "provider": "launchpad",
            "status": "open",
            "last_updated": "2026-09-26T00:00:00+00:00",
        },
        "lp:https://launchpad.net/~u/+git/repo/+merge/15": {
            "url": "https://launchpad.net/~u/+git/repo/+merge/15",
            "provider": "launchpad",
            "status": "open",
            "last_updated": "2026-09-26T00:00:00+00:00",
        },
    }
    resolved = provider._proven_resolved(state_items, person=None)
    assert resolved == ["lp:https://launchpad.net/bugs/77"]


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
