from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.gitreport.providers.launchpad import LaunchpadProvider, LP_CREDENTIALS_PATH

# --- Mock Objects ---

class MockLaunchpadObject:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

class MockLaunchpadPerson(MockLaunchpadObject):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.getMergeProposals = MagicMock()
        self.getRequestedReviews = MagicMock()

class MockVote:
    def __init__(self, reviewer, registrant):
        self.reviewer = reviewer
        self.registrant = registrant

# --- Tests ---

@patch("src.gitreport.providers.launchpad.Launchpad")
def test_launchpad_provider_get_activity(MockLaunchpad):
    """Test the get_activity method of the LaunchpadProvider."""
    # Arrange
    mock_lp_instance = MockLaunchpad.login_with.return_value
    me_person = MockLaunchpadPerson(name="testuser")
    other_person = MockLaunchpadPerson(name="anotheruser")
    mock_lp_instance.me = me_person
    
    mock_bugs_collection = MagicMock()
    mock_lp_instance.bugs = mock_bugs_collection
    
    provider = LaunchpadProvider(username="testuser", token="")
    start_date = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end_date = datetime(2024, 1, 31, tzinfo=timezone.utc)

    # --- Mock Data Setup ---
    # Bugs
    bug_created = MockLaunchpadObject(
        title="New bug", web_link="http://bug/1", bug_target_name="proj/one",
        bug_target=MockLaunchpadObject(private=False),
        date_created=datetime(2024, 1, 5, tzinfo=timezone.utc),
        date_closed=None
    )
    bug_closed = MockLaunchpadObject(
        title="Old bug", web_link="http://bug/2", bug_target_name="proj/two",
        date_created=datetime(2024, 1, 10, tzinfo=timezone.utc),
        date_closed=datetime(2024, 1, 20, tzinfo=timezone.utc),
        bug_target=MockLaunchpadObject(private=True),
    )
    mock_bugs_collection.searchTasks.return_value = [bug_created, bug_closed]

    # Submitted & Merged MPs
    mp_submitted_and_merged = MockLaunchpadObject(
        web_link="http://mp/submitted_merged", date_created=datetime(2024, 1, 10, tzinfo=timezone.utc),
        queue_status='Merged', merger=me_person,
        target_git_repository_path="proj/merged",
        target_git_repository=MockLaunchpadObject(project=MockLaunchpadObject(private=False))
    )
    mp_submitted_only = MockLaunchpadObject(
        web_link="http://mp/submitted_only", date_created=datetime(2024, 1, 11, tzinfo=timezone.utc),
        queue_status='Needs Review', merger=None,
        target_git_repository_path="proj/submitted",
        target_git_repository=MockLaunchpadObject(project=MockLaunchpadObject(private=True))
    )
    me_person.getMergeProposals.return_value = [mp_submitted_and_merged, mp_submitted_only]

    # Reviewed & Claimed MPs
    claimed_vote = MockVote(reviewer=me_person, registrant=me_person)
    mp_claimed = MockLaunchpadObject(
        web_link="http://mp/claimed", date_created=datetime(2024, 1, 15, tzinfo=timezone.utc),
        target_git_repository_path="proj/claimed", votes=[claimed_vote],
        target_git_repository=MockLaunchpadObject(project=MockLaunchpadObject(private=False))
    )
    
    requested_vote = MockVote(reviewer=me_person, registrant=other_person)
    mp_requested = MockLaunchpadObject(
        web_link="http://mp/requested", date_created=datetime(2024, 1, 16, tzinfo=timezone.utc),
        target_git_repository_path="proj/requested", votes=[requested_vote],
        target_git_repository=MockLaunchpadObject(project=MockLaunchpadObject(private=True))
    )
    me_person.getRequestedReviews.return_value = [mp_claimed, mp_requested]

    # Act
    activity = provider.get_activity(start_date, end_date)

    # Assert
    # Check that all repos were created
    assert all(k in activity for k in ["proj/one", "proj/two", "proj/merged", "proj/submitted", "proj/claimed", "proj/requested"])

    # Check visibilities
    assert activity["proj/one"]["visibility"] == "public"
    assert activity["proj/two"]["visibility"] == "private"
    assert activity["proj/merged"]["visibility"] == "public"
    assert activity["proj/submitted"]["visibility"] == "private"
    assert activity["proj/claimed"]["visibility"] == "public"
    assert activity["proj/requested"]["visibility"] == "private"

    # Check content
    assert len(activity["proj/one"]["issues_created"]) == 1
    assert len(activity["proj/two"]["issues_closed"]) == 1
    assert len(activity["proj/merged"]["prs_submitted"]) == 1
    assert len(activity["proj/merged"]["prs_merged"]) == 1
    assert len(activity["proj/submitted"]["prs_submitted"]) == 1
    assert not activity["proj/submitted"]["prs_merged"]
    assert len(activity["proj/claimed"]["prs_claimed"]) == 1
    assert not activity["proj/claimed"]["prs_reviewed"]
    assert len(activity["proj/requested"]["prs_reviewed"]) == 1
    assert not activity["proj/requested"]["prs_claimed"]
    
    # Check that login_with was called
    MockLaunchpad.login_with.assert_called_with(
        "gitreport-cli", "production", credentials_file=str(LP_CREDENTIALS_PATH), version="devel"
    )
