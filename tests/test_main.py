from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from gitreport.config import Config, ProviderConfig
from gitreport.main import cli
from gitreport.providers.base import GitProvider


class MockProvider(GitProvider):
    get_activity = MagicMock()

    def __init__(self, username, token):
        pass


@patch("gitreport.main.load_config")
@patch("gitreport.main.generate_report")
@patch("gitreport.main.PROVIDER_MAP", {"mock_provider": MockProvider})
def test_generate_command_passes_aware_datetimes(mock_generate_report, mock_load_config):
    """Test the generate command passes aware datetimes to providers."""
    # Reset mock from previous runs
    MockProvider.get_activity.reset_mock()

    mock_config = Config(providers={"mock_provider": ProviderConfig(username="test", token="test")})
    mock_load_config.return_value = mock_config

    runner = CliRunner()
    result = runner.invoke(cli, ["generate", "--start-date", "2024-01-01"])

    assert result.exit_code == 0

    # Assert that get_activity was called
    MockProvider.get_activity.assert_called_once()

    # Get the call args from the get_activity method
    call_args = MockProvider.get_activity.call_args
    start_dt, end_dt = call_args[0]

    assert start_dt.tzinfo is not None
    assert end_dt.tzinfo is not None


@patch("gitreport.main.load_config")
def test_generate_command_invalid_date(mock_load_config):
    """Test the generate command with an invalid date string."""
    runner = CliRunner()
    result = runner.invoke(cli, ["generate", "--start-date", "not a date"])

    # An unparseable date is a usage error: the command exits non-zero and
    # reports the problem.
    assert result.exit_code != 0
    assert "Could not parse date strings." in result.output


@patch("gitreport.main.load_config")
@patch("gitreport.main.generate_report")
@patch("gitreport.main.dateparser")
@patch("gitreport.main.PROVIDER_MAP", {})
def test_generate_command_date_parsing(mock_dateparser, mock_generate_report, mock_load_config):
    """Test that date strings are parsed correctly."""
    runner = CliRunner()
    runner.invoke(cli, ["generate", "--start-date", "last monday", "--end-date", "yesterday"])

    assert mock_dateparser.parse.call_count == 2
    mock_dateparser.parse.assert_any_call("last monday")
    mock_dateparser.parse.assert_any_call("yesterday")


# --- New command tests ---

from gitreport.state import AttentionState  # noqa: E402


class NoopProvider(GitProvider):
    get_activity = MagicMock()
    get_attention = MagicMock(
        return_value={"ok": True, "items": [], "resolved_ids": [], "error": None}
    )

    def __init__(self, username, token):
        pass


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_digest_creates_files_and_symlinks(mock_load_config, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("digest")
    state_path = tmp / "state.json"
    stem = tmp / "digests" / "2026-09-28"
    latest = tmp / "digests" / "latest"
    mock_load_config.return_value = _config_with(state_path=state_path, stem=stem, latest=latest)

    runner = CliRunner()
    result = runner.invoke(cli, ["digest"])

    assert result.exit_code == 0, result.output
    assert (tmp / "digests" / "2026-09-28.md").exists()
    assert (tmp / "digests" / "2026-09-28.html").exists()
    assert latest.with_suffix(".md").is_symlink()
    assert latest.with_suffix(".html").is_symlink()
    assert "Status: NOT YET REVIEWED" in (tmp / "digests" / "2026-09-28.md").read_text()


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_digest_provider_failure_degrades(mock_load_config, tmp_path_factory):
    class BrokenProvider(GitProvider):
        get_activity = MagicMock()
        get_attention = MagicMock(
            return_value={"ok": False, "items": [], "resolved_ids": [], "error": "boom"}
        )

        def __init__(self, username, token):
            pass

    tmp = tmp_path_factory.mktemp("digest2")
    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json",
        stem=tmp / "digests" / "2026-09-28",
        latest=tmp / "digests" / "latest",
    )
    with patch("gitreport.main.PROVIDER_MAP", {"mock": BrokenProvider}):
        runner = CliRunner()
        result = runner.invoke(cli, ["digest"])

    assert result.exit_code == 0  # degraded, not aborted
    assert "stale" in result.output.lower()


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_read_advances_cursor_and_restamps(mock_load_config, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("read")
    state_path = tmp / "state.json"
    stem = tmp / "digests" / "2026-09-28"
    latest = tmp / "digests" / "latest"
    mock_load_config.return_value = _config_with(state_path=state_path, stem=stem, latest=latest)

    runner = CliRunner()
    runner.invoke(cli, ["digest"])  # generate first
    result = runner.invoke(cli, ["read"])

    assert result.exit_code == 0, result.output
    md_text = (tmp / "digests" / "2026-09-28.md").read_text()
    assert "Status: Reviewed" in md_text
    assert "NOT YET REVIEWED" not in md_text
    import json

    state = json.loads(state_path.read_text())
    assert state["last_reviewed"] is not None
    assert state["reviewed_at"] is not None


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_ack_and_unack_roundtrip(mock_load_config, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ack")
    item_id = "gh:https://github.com/o/r/pull/1"
    state = AttentionState(
        last_digest_run="2026-09-28T06:00:00+00:00",
        last_reviewed="2026-09-27T06:00:00+00:00",
    )
    # Seed state directly via save_state in the test.
    from gitreport.state import save_state

    state.items[item_id] = _seed_item(item_id)
    save_state(tmp / "state.json", state)

    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json",
        stem=tmp / "digests" / "2026-09-28",
        latest=tmp / "digests" / "latest",
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["ack", "o/r/pull/1"])
    assert result.exit_code == 0, result.output
    import json

    acked_state = json.loads((tmp / "state.json").read_text())
    assert acked_state["items"][item_id]["status"] == "acked"

    result = runner.invoke(cli, ["unack", "o/r/pull/1"])
    assert result.exit_code == 0, result.output
    import json

    reopened = json.loads((tmp / "state.json").read_text())
    assert reopened["items"][item_id]["status"] == "open"
    assert reopened["items"][item_id]["reopen_count"] == 1


def _seed_item(item_id):
    from gitreport.state import ItemState

    return ItemState(
        status="open",
        origins=["query"],
        kinds=["mention"],
        reasons=["r"],
        repo="o/r",
        title="T",
        url="https://github.com/o/r/pull/1",
        first_seen="2026-09-28T06:00:00+00:00",
        last_updated="2026-09-28T06:00:00+00:00",
    )


def _config_with(state_path, stem, latest, providers=None):
    from gitreport.config import AttentionConfig, Config, ProviderConfig

    return Config(
        providers=providers or {"mock": ProviderConfig(username="u", token="t")},
        attention=AttentionConfig(state_path=state_path, digest_output=stem, digest_latest=latest),
    )


class ThreadPatchProvider(GitProvider):
    """GitHub-like provider whose get_attention yields one notification item."""

    get_activity = MagicMock()
    get_attention = MagicMock(
        return_value={
            "ok": True,
            "items": [
                {
                    "id": "gh:https://github.com/o/r/pull/1",
                    "provider": "github",
                    "kind": "mention",
                    "origin": "notification",
                    "repo": "o/r",
                    "title": "PR title",
                    "url": "https://github.com/o/r/pull/1",
                    "reason": "mentioned you",
                    "updated_at": "2026-09-28T05:00:00+00:00",
                    "thread_url": "https://api.github.com/notifications/threads/1",
                }
            ],
            "resolved_ids": [],
            "error": None,
        }
    )
    instances: list = []

    def __init__(self, username, token):
        self._github = MagicMock()
        ThreadPatchProvider.instances.append(self)


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"github": ThreadPatchProvider})
def test_digest_persists_thread_url_for_ack(mock_load_config, tmp_path):
    mock_load_config.return_value = _config_with(
        state_path=tmp_path / "state.json",
        stem=tmp_path / "digests" / "2026-09-28",
        latest=tmp_path / "digests" / "latest",
        providers={"github": ProviderConfig(username="u", token="t")},
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["digest"])

    assert result.exit_code == 0, result.output
    import json

    item = json.loads((tmp_path / "state.json").read_text())["items"][
        "gh:https://github.com/o/r/pull/1"
    ]
    assert item["thread_url"] == "https://api.github.com/notifications/threads/1"
    assert item["provider"] == "github"
    assert item["status"] == "open"


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"github": ThreadPatchProvider})
def test_ack_marks_github_notification_thread_read(mock_load_config, tmp_path):
    mock_load_config.return_value = _config_with(
        state_path=tmp_path / "state.json",
        stem=tmp_path / "digests" / "2026-09-28",
        latest=tmp_path / "digests" / "latest",
        providers={"github": ProviderConfig(username="u", token="t")},
    )

    runner = CliRunner()
    runner.invoke(cli, ["digest"])  # persist the notification item with its thread url
    result = runner.invoke(cli, ["ack", "o/r/pull/1"])

    assert result.exit_code == 0, result.output
    # The ack-time provider instance PATCHes the thread exactly once.
    patcher = ThreadPatchProvider.instances[-1]
    patcher._github.requester.requestJsonAndCheck.assert_called_once_with(
        "PATCH", "https://api.github.com/notifications/threads/1"
    )
    import json

    acked = json.loads((tmp_path / "state.json").read_text())
    assert acked["items"]["gh:https://github.com/o/r/pull/1"]["status"] == "acked"


@patch("gitreport.main.load_config")
def test_unack_resolved_item_pins(mock_load_config, tmp_path):
    from gitreport.state import ItemState, save_state

    item_id = "gh:https://github.com/o/r/pull/2"
    state = AttentionState()
    state.items[item_id] = ItemState(
        status="resolved",
        origins=["query"],
        kinds=["ci_failure"],
        reasons=["check failure"],
        repo="o/r",
        title="PR two",
        url="https://github.com/o/r/pull/2",
        first_seen="2026-09-27T06:00:00+00:00",
        last_updated="2026-09-27T06:00:00+00:00",
        resolved_at="2026-09-27T07:00:00+00:00",
    )
    save_state(tmp_path / "state.json", state)
    mock_load_config.return_value = _config_with(
        state_path=tmp_path / "state.json",
        stem=tmp_path / "digests" / "2026-09-28",
        latest=tmp_path / "digests" / "latest",
    )

    result = CliRunner().invoke(cli, ["unack", "o/r/pull/2"])

    assert result.exit_code == 0, result.output
    import json

    item = json.loads((tmp_path / "state.json").read_text())["items"][item_id]
    assert item["status"] == "open"
    assert item["pinned"] is True  # previously-resolved: pin so it cannot re-resolve
    assert item["reopen_count"] == 1
    assert item["acked"] is False


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_digest_absence_resolution_gated_by_ok_providers(mock_load_config, tmp_path):
    """ok_providers gates absence-resolution: a failed fetch never resolves."""
    import json

    class BrokenProvider(GitProvider):
        get_activity = MagicMock()
        get_attention = MagicMock(
            return_value={"ok": False, "items": [], "resolved_ids": [], "error": "boom"}
        )

        def __init__(self, username, token):
            pass

    item_id = "mock:https://github.com/o/r/pull/9"
    (tmp_path / "state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "last_digest_run": None,
                "last_reviewed": None,
                "reviewed_at": None,
                "items": {
                    item_id: {
                        "status": "open",
                        "origins": ["query"],
                        "kinds": ["stale_pr"],
                        "reasons": ["no activity"],
                        "repo": "o/r",
                        "title": "PR nine",
                        "url": "https://github.com/o/r/pull/9",
                        "first_seen": "2026-09-27T06:00:00+00:00",
                        "last_updated": "2026-09-27T06:00:00+00:00",
                        "provider": "mock",
                    }
                },
            }
        )
    )
    mock_load_config.return_value = _config_with(
        state_path=tmp_path / "state.json",
        stem=tmp_path / "digests" / "2026-09-28",
        latest=tmp_path / "digests" / "latest",
    )

    runner = CliRunner()
    with patch("gitreport.main.PROVIDER_MAP", {"mock": BrokenProvider}):
        result = runner.invoke(cli, ["digest"])
    assert result.exit_code == 0, result.output
    assert "stale" in result.output.lower()
    item = json.loads((tmp_path / "state.json").read_text())["items"][item_id]
    assert item["status"] == "open"  # failed fetch: no absence-resolution

    # A successful fetch with the item absent: absence-resolution applies.
    result = runner.invoke(cli, ["digest"])
    assert result.exit_code == 0, result.output
    item = json.loads((tmp_path / "state.json").read_text())["items"][item_id]
    assert item["status"] == "resolved"


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_attention_dry_run_never_persists(mock_load_config, tmp_path):
    mock_load_config.return_value = _config_with(
        state_path=tmp_path / "state.json",
        stem=tmp_path / "digests" / "2026-09-28",
        latest=tmp_path / "digests" / "latest",
    )

    result = CliRunner().invoke(cli, ["attention"])

    assert result.exit_code == 0, result.output
    assert "# Needs attention" in result.output
    assert not (tmp_path / "state.json").exists()


@patch("gitreport.main.load_config")
def test_read_without_digests_is_a_noop(mock_load_config, tmp_path):
    mock_load_config.return_value = _config_with(
        state_path=tmp_path / "state.json",
        stem=tmp_path / "digests" / "2026-09-28",
        latest=tmp_path / "digests" / "latest",
    )

    result = CliRunner().invoke(cli, ["read"])

    assert result.exit_code == 0, result.output
    assert "No digests to mark read." in result.output
    assert not (tmp_path / "state.json").exists()
