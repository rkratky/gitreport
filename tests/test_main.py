import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
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


class RecordingProvider(GitProvider):
    """Noop variant whose instances record the exclusions kwarg (BUG-01)."""

    get_activity = MagicMock()
    instances = []

    def __init__(self, username, token):
        self.username = username
        self.seen_exclusions = []
        RecordingProvider.instances.append(self)

    def get_attention(self, since, exclusions=None, state_items=None, stale_pr_days=None):
        self.seen_exclusions.append(exclusions)
        return {"ok": True, "items": [], "resolved_ids": [], "error": None}


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
@patch(
    "gitreport.main.PROVIDER_MAP",
    {"github": RecordingProvider, "launchpad": RecordingProvider},
)
def test_digest_passes_per_provider_exclusions(mock_load_config, tmp_path_factory):
    """Each provider receives only its own exclusion patterns (BUG-01)."""
    RecordingProvider.instances.clear()
    tmp = tmp_path_factory.mktemp("exclusions")
    config = _config_with(
        state_path=tmp / "state.json",
        stem=tmp / "digests" / "2026-09-28",
        latest=tmp / "digests" / "latest",
        providers={
            "github": ProviderConfig(username="gh-user", token="t"),
            "launchpad": ProviderConfig(username="lp-user"),
        },
    )
    config.attention.exclusions = {"github": ["gh-*"], "launchpad": ["lp-*"]}
    mock_load_config.return_value = config

    runner = CliRunner()
    result = runner.invoke(cli, ["digest"])

    assert result.exit_code == 0, result.output
    assert (tmp / "digests" / "2026-09-28.md").exists()
    by_username = {p.username: p for p in RecordingProvider.instances}
    assert by_username["gh-user"].seen_exclusions == [["gh-*"]]
    assert by_username["lp-user"].seen_exclusions == [["lp-*"]]

    # The dry-run `attention` command resolves exclusions the same way; it
    # builds fresh provider instances, so look those up by username.
    n_after_digest = len(RecordingProvider.instances)
    result = runner.invoke(cli, ["attention"])

    assert result.exit_code == 0, result.output
    attention_by_username = {p.username: p for p in RecordingProvider.instances[n_after_digest:]}
    assert attention_by_username["gh-user"].seen_exclusions == [["gh-*"]]
    assert attention_by_username["lp-user"].seen_exclusions == [["lp-*"]]


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
    # Spec format in local time: `Status: Reviewed Mon 28 Sep 09:14` (BUG-10).
    assert re.search(r"Status: Reviewed \w{3} \d{1,2} \w{3} \d{2}:\d{2}", md_text)
    assert "NOT YET REVIEWED" not in md_text
    import json

    state = json.loads(state_path.read_text())
    assert state["last_reviewed"] is not None
    assert state["reviewed_at"] is not None


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_ack_and_unack_roundtrip(mock_load_config, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ack")
    state = AttentionState(
        last_digest_run="2026-09-28T06:00:00+00:00",
        last_reviewed="2026-09-27T06:00:00+00:00",
    )
    # Seed state directly via save_state in the test.
    from gitreport.state import save_state

    item_id = "gh:https://github.com/o/r/pull/1"
    resolved_id = "gh:https://github.com/o/r/pull/3"
    state.items[item_id] = _seed_item(item_id)
    state.items[resolved_id] = _seed_item(
        resolved_id, status="resolved", resolved_at="2026-09-27T07:00:00+00:00"
    )
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

    # Unacking a resolved item also clears the resolution stamp (BUG-05).
    result = runner.invoke(cli, ["unack", "o/r/pull/3"])
    assert result.exit_code == 0, result.output

    unacked = json.loads((tmp / "state.json").read_text())
    unacked_item = unacked["items"][resolved_id]
    assert unacked_item["status"] == "open"
    assert unacked_item["resolved_at"] is None
    assert unacked_item["pinned"] is True  # previously-resolved: pinned
    assert unacked_item["reopen_count"] == 1
    assert unacked_item["acked"] is False


def _seed_item(item_id, **overrides):
    from gitreport.state import ItemState

    defaults = dict(
        status="open",
        origins=["query"],
        kinds=["mention"],
        reasons=["r"],
        repo="o/r",
        title="T",
        url=item_id.split(":", 1)[-1],  # "gh:https://..." -> the url part
        first_seen="2026-09-28T06:00:00+00:00",
        last_updated="2026-09-28T06:00:00+00:00",
    )
    defaults.update(overrides)
    return ItemState(**defaults)


def _config_with(state_path, stem, latest, providers=None, formats=None):
    from gitreport.config import AttentionConfig, Config, ProviderConfig

    return Config(
        providers=providers or {"mock": ProviderConfig(username="u", token="t")},
        attention=AttentionConfig(
            state_path=state_path,
            digest_output=stem,
            digest_latest=latest,
            digest_formats=formats or ["html", "md"],
        ),
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
def test_unack_acked_with_resolved_at_pins_against_reresolve(mock_load_config, tmp_path):
    """R3: an acked record can carry resolved_at (its source stopped
    reporting while it was acked). Unacking it must pin, or the next digest
    silently re-resolves the unack."""
    from gitreport.attention import merge_into_state
    from gitreport.state import save_state

    item_id = "mock:https://github.com/o/r/pull/5"
    state = AttentionState()
    state.items[item_id] = _seed_item(
        item_id,
        status="acked",
        acked=True,
        acked_at="2026-09-28T06:30:00+00:00",
        resolved_at="2026-09-28T07:00:00+00:00",
        provider="mock",
    )
    save_state(tmp_path / "state.json", state)
    mock_load_config.return_value = _config_with(
        state_path=tmp_path / "state.json",
        stem=tmp_path / "digests" / "2026-09-28",
        latest=tmp_path / "digests" / "latest",
    )

    result = CliRunner().invoke(cli, ["unack", "o/r/pull/5"])

    assert result.exit_code == 0, result.output
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["items"][item_id]["pinned"] is True
    # The next digest (item absent, provider ok) must not re-resolve it.
    merged = merge_into_state(saved, {}, {"mock"}, "2026-09-29T06:00:00+00:00")
    assert merged["items"][item_id]["status"] == "open"


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
    assert item["resolved_at"] is None  # un-resolving clears the stamp (BUG-05)


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


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_digest_recovers_last_reviewed_from_unreviewed_digest(mock_load_config, tmp_path):
    """FINAL-05: after a corrupt-state rebuild, `digest`/`read` recover
    last_reviewed from the newest digest's front matter — its coverage_start
    when the status line is NOT YET REVIEWED."""
    tmp = tmp_path
    stem = tmp / "digests" / "2026-09-28"
    (tmp / "digests").mkdir()
    (tmp / "digests" / "2026-09-28.md").write_text(
        "---\n"
        "generated_at: 2026-09-28T06:00:00+00:00\n"
        "coverage_start: 2026-09-20T06:00:00+00:00\n"
        "---\n\n"
        "# GitReport digest\n\n"
        "Status: NOT YET REVIEWED — run `gitreport read` after reviewing\n"
    )
    (tmp / "state.json").write_text("{corrupt")
    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json", stem=stem, latest=tmp / "digests" / "latest"
    )

    result = CliRunner().invoke(cli, ["digest"])

    assert result.exit_code == 0, result.output
    state = json.loads((tmp / "state.json").read_text())
    # Digest never advances the cursor: the recovered coverage_start survives.
    assert state["last_reviewed"] == "2026-09-20T06:00:00+00:00"


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_digest_corrupt_state_does_not_collapse_coverage(mock_load_config, tmp_path):
    """R2: when the state file is corrupt, the pre-lock snapshot is empty —
    computing `since` from it collapses coverage to 24h. The digest must
    recover the cursor read-only from the newest Reviewed digest first, so
    coverage_start is that digest's generated_at, not a 24h window."""
    tmp = tmp_path
    stem = tmp / "digests" / "2026-09-28"
    (tmp / "digests").mkdir()
    # A 7-day-old digest the user already reviewed: the cursor was its
    # generated_at (2026-09-21), ~7 days before the new digest.
    (tmp / "digests" / "2026-09-28.md").write_text(
        "---\n"
        "generated_at: 2026-09-21T06:00:00+00:00\n"
        "coverage_start: 2026-09-14T06:00:00+00:00\n"
        "---\n\n"
        "# GitReport digest\n\n"
        "Status: Reviewed Mon 21 Sep 09:14\n"
    )
    (tmp / "state.json").write_text("{corrupt")
    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json", stem=stem, latest=tmp / "digests" / "latest"
    )

    result = CliRunner().invoke(cli, ["digest"])

    assert result.exit_code == 0, result.output
    text = (tmp / "digests" / "2026-09-28.md").read_text()
    match = re.search(r"^coverage_start: (.+)$", text, re.MULTILINE)
    assert match is not None
    assert match.group(1) == "2026-09-21T06:00:00+00:00"
    state = json.loads((tmp / "state.json").read_text())
    assert state["last_reviewed"] == "2026-09-21T06:00:00+00:00"


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_digest_first_run_fresh_state(mock_load_config, tmp_path):
    """TEST-01a: a fresh state (no state file, no digests) is a first run —
    the stdout coverage line carries the suffix and the written .json model
    records coverage.first_run true."""
    tmp = tmp_path
    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json",
        stem=tmp / "digests" / "2026-09-28",
        latest=tmp / "digests" / "latest",
    )

    result = CliRunner().invoke(cli, ["digest"])

    assert result.exit_code == 0, result.output
    assert "(first run: last 24 hours)" in result.output
    model = json.loads((tmp / "digests" / "2026-09-28.json").read_text())
    assert model["coverage"]["first_run"] is True


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_digest_first_run_false_after_corrupt_state_with_reviewed_digest(
    mock_load_config, tmp_path
):
    """TEST-01b: a corrupt state plus a prior Reviewed digest on disk is NOT
    a first run — the recovered cursor continues coverage, so the suffix is
    absent from stdout and the written .json records first_run false."""
    tmp = tmp_path
    (tmp / "digests").mkdir()
    (tmp / "digests" / "2026-09-20.md").write_text(
        "---\n"
        "generated_at: 2026-09-20T06:00:00+00:00\n"
        "coverage_start: 2026-09-13T06:00:00+00:00\n"
        "---\n\n"
        "# GitReport digest\n\n"
        "Status: Reviewed Sun 20 Sep 09:14\n"
    )
    (tmp / "state.json").write_text("{corrupt")
    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json",
        stem=tmp / "digests" / "YYYY-MM-DD",
        latest=tmp / "digests" / "latest",
    )

    result = CliRunner().invoke(cli, ["digest"])

    assert result.exit_code == 0, result.output
    assert "(first run: last 24 hours)" not in result.output
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    model = json.loads((tmp / "digests" / f"{today}.json").read_text())
    assert model["coverage"]["first_run"] is False


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_digest_first_run_corrupt_state_no_digests(mock_load_config, tmp_path):
    """TEST-01c: a corrupt state with no digests on disk has no cursor to
    recover — the 24h first-run fallback applies and the suffix is present."""
    tmp = tmp_path
    (tmp / "digests").mkdir()
    (tmp / "state.json").write_text("{corrupt")
    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json",
        stem=tmp / "digests" / "YYYY-MM-DD",
        latest=tmp / "digests" / "latest",
    )

    result = CliRunner().invoke(cli, ["digest"])

    assert result.exit_code == 0, result.output
    assert "(first run: last 24 hours)" in result.output
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    model = json.loads((tmp / "digests" / f"{today}.json").read_text())
    assert model["coverage"]["first_run"] is True


@patch("gitreport.main.load_config")
def test_read_recovers_last_reviewed_from_reviewed_digest(mock_load_config, tmp_path):
    """FINAL-05: after a corrupt-state rebuild, `read` recovers last_reviewed
    from the newest digest's generated_at when its status line says Reviewed.

    The newest digest's timestamp equals the recovered cursor, so the
    forward-only guard blocks record_read (reviewed_at stays unset) — proving
    recovery, not record_read, placed the cursor; the rebuild+recovery is
    still persisted.
    """
    tmp = tmp_path
    stem = tmp / "digests" / "2026-09-28"
    (tmp / "digests").mkdir()
    (tmp / "digests" / "2026-09-28.md").write_text(
        "---\n"
        "generated_at: 2026-09-28T06:00:00+00:00\n"
        "coverage_start: 2026-09-20T06:00:00+00:00\n"
        "---\n\n"
        "# GitReport digest\n\n"
        "Status: Reviewed Mon 28 Sep 09:14\n"
    )
    (tmp / "state.json").write_text("{corrupt")
    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json", stem=stem, latest=tmp / "digests" / "latest"
    )

    result = CliRunner().invoke(cli, ["read"])

    assert result.exit_code == 0, result.output
    state = json.loads((tmp / "state.json").read_text())
    assert state["last_reviewed"] == "2026-09-28T06:00:00+00:00"
    assert state["reviewed_at"] is None


@patch("gitreport.main.load_config")
def test_read_recovers_last_reviewed_ignores_mid_line_reviewed_text(mock_load_config, tmp_path):
    """Final wave 2 BUG-C: the "Status: Reviewed" recovery check is
    line-anchored — an item title carrying the text "Status: Reviewed by bob"
    mid-line (with a list-item prefix) must not make the digest count as
    reviewed; the cursor recovers to coverage_start, so `read` still advances
    it (record_read runs and stamps the status line). Under the old substring
    check the recovery wrongly claimed generated_at, the forward-only guard
    blocked record_read, and reviewed_at stayed None."""
    tmp = tmp_path
    stem = tmp / "digests" / "2026-09-28"
    (tmp / "digests").mkdir()
    (tmp / "digests" / "2026-09-28.md").write_text(
        "---\n"
        "generated_at: 2026-09-28T06:00:00+00:00\n"
        "coverage_start: 2026-09-20T06:00:00+00:00\n"
        "---\n\n"
        "# GitReport digest\n\n"
        "  - ci_failure T: Status: Reviewed by bob in the PR thread\n\n"
        "Status: NOT YET REVIEWED — run `gitreport read` after reviewing\n"
    )
    (tmp / "state.json").write_text("{corrupt")
    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json", stem=stem, latest=tmp / "digests" / "latest"
    )

    result = CliRunner().invoke(cli, ["read"])

    assert result.exit_code == 0, result.output
    state = json.loads((tmp / "state.json").read_text())
    # record_read ran: the recovered cursor was coverage_start, so the newest
    # digest was still "later" — recovery did not claim this digest reviewed.
    assert state["reviewed_at"] is not None
    assert state["last_reviewed"] == "2026-09-28T06:00:00+00:00"
    digest_text = (tmp / "digests" / "2026-09-28.md").read_text()
    assert "Status: NOT YET REVIEWED" not in digest_text


@patch("gitreport.main.load_config")
def test_read_does_not_move_cursor_back(mock_load_config, tmp_path):
    """FINAL-06: `read` never moves the cursor backwards; a hand-aged digest
    (generated_at older than the cursor) leaves the cursor untouched."""
    from gitreport.state import save_state

    tmp = tmp_path
    stem = tmp / "digests" / "2026-09-28"
    (tmp / "digests").mkdir()
    (tmp / "digests" / "2026-09-28.md").write_text(
        "---\n"
        "generated_at: 2026-09-28T06:00:00+00:00\n"
        "coverage_start: 2026-09-27T06:00:00+00:00\n"
        "---\n\n"
        "# GitReport digest\n\n"
        "Status: NOT YET REVIEWED — run `gitreport read` after reviewing\n"
    )
    state = AttentionState(
        last_reviewed="2026-09-28T12:00:00+00:00",
        reviewed_at="2026-09-28T13:00:00+00:00",
    )
    save_state(tmp / "state.json", state)
    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json", stem=stem, latest=tmp / "digests" / "latest"
    )

    result = CliRunner().invoke(cli, ["read"])

    assert result.exit_code == 0, result.output
    assert "Newest digest is older than your review cursor" in result.output
    saved = json.loads((tmp / "state.json").read_text())
    assert saved["last_reviewed"] == "2026-09-28T12:00:00+00:00"  # unchanged
    assert saved["reviewed_at"] == "2026-09-28T13:00:00+00:00"  # unchanged


@patch("gitreport.main.load_config")
def test_read_survives_unparseable_last_reviewed(mock_load_config, tmp_path):
    """R8: a garbage last_reviewed must not wedge `read` — it counts as
    never-reviewed (report_model's convention), so the newest digest is
    restamped and the cursor moves to its generated_at."""
    from gitreport.state import save_state

    tmp = tmp_path
    stem = tmp / "digests" / "2026-09-28"
    (tmp / "digests").mkdir()
    (tmp / "digests" / "2026-09-28.md").write_text(
        "---\n"
        "generated_at: 2026-09-28T06:00:00+00:00\n"
        "coverage_start: 2026-09-20T06:00:00+00:00\n"
        "---\n\n"
        "# GitReport digest\n\n"
        "Status: NOT YET REVIEWED — run `gitreport read` after reviewing\n"
    )
    save_state(tmp / "state.json", AttentionState(last_reviewed="garbage-cursor"))
    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json", stem=stem, latest=tmp / "digests" / "latest"
    )

    result = CliRunner().invoke(cli, ["read"])

    assert result.exit_code == 0, result.output
    saved = json.loads((tmp / "state.json").read_text())
    assert saved["last_reviewed"] == "2026-09-28T06:00:00+00:00"
    digest_text = (tmp / "digests" / "2026-09-28.md").read_text()
    assert "Status: Reviewed" in digest_text
    assert "NOT YET REVIEWED" not in digest_text


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_digest_prunes_old_resolved_items(mock_load_config, tmp_path):
    """FINAL-07: retention pruning runs via state.prune on the merged state."""
    from datetime import UTC, timedelta

    from gitreport.state import save_state

    item_id = "mock:https://github.com/o/r/pull/40"
    old_resolved = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    state = AttentionState()
    state.items[item_id] = _seed_item(
        item_id, status="resolved", resolved_at=old_resolved, provider="mock"
    )
    save_state(tmp_path / "state.json", state)
    mock_load_config.return_value = _config_with(
        state_path=tmp_path / "state.json",
        stem=tmp_path / "digests" / "2026-09-28",
        latest=tmp_path / "digests" / "latest",
    )

    result = CliRunner().invoke(cli, ["digest"])

    assert result.exit_code == 0, result.output
    saved = json.loads((tmp_path / "state.json").read_text())
    assert item_id not in saved["items"]


@patch("gitreport.main.load_config")
def test_ack_prompts_outside_the_state_lock(mock_load_config, tmp_path):
    """BUG-02: the disambiguation prompt must not hold the state lock.

    While click.prompt is pending, a background thread must be able to
    acquire the lock (2s budget); the chosen item is then acked normally.
    """
    from gitreport.state import ItemState, save_state

    ids = ["gh:https://github.com/o/r/pull/1", "gh:https://github.com/o/r/pull/2"]
    state = AttentionState()
    for i, mid in enumerate(ids, 1):
        state.items[mid] = ItemState(
            status="open",
            origins=["notification"],
            kinds=["mention"],
            reasons=["r"],
            repo="o/r",
            title=f"PR {i}",
            url=f"https://github.com/o/r/pull/{i}",
            first_seen="2026-09-28T06:00:00+00:00",
            last_updated="2026-09-28T06:00:00+00:00",
            thread_url=f"https://api.github.com/notifications/threads/{i}",
        )
    save_state(tmp_path / "state.json", state)
    mock_load_config.return_value = _config_with(
        state_path=tmp_path / "state.json",
        stem=tmp_path / "digests" / "2026-09-28",
        latest=tmp_path / "digests" / "latest",
    )

    lock_free = threading.Event()
    acquired_during_prompt: list[bool] = []

    def fake_prompt(*args, **kwargs):
        holder = threading.Thread(
            target=lambda: _touch_lock(tmp_path / "state.json", lock_free), daemon=True
        )
        holder.start()
        acquired_during_prompt.append(lock_free.wait(timeout=2.0))
        holder.join(timeout=5.0)
        return "1"  # pick the first match

    runner = CliRunner()
    with patch("gitreport.main.click.prompt", side_effect=fake_prompt):
        result = runner.invoke(cli, ["ack", "o/r/pull"])

    assert result.exit_code == 0, result.output
    assert acquired_during_prompt == [True]  # lock was free while the prompt ran
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["items"][ids[0]]["status"] == "acked"  # mutation applied
    assert saved["items"][ids[1]]["status"] == "open"


def _touch_lock(state_path, lock_free):
    from gitreport.state import state_lock

    with state_lock(state_path):
        lock_free.set()


def _assert_clean_cli_error(result, message_fragment):
    """The command failed cleanly: exit != 0, message printed, no raw traceback.

    click's BaseCommand.main handles ClickException itself (message + exit 1),
    so CliRunner records SystemExit(1) rather than the ClickException object;
    a bare exception class leaking here means the try/except is missing.
    """
    assert result.exit_code != 0
    assert message_fragment in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


@patch("gitreport.main.load_config")
def test_digest_provider_ctor_failure_degrades(mock_load_config, tmp_path):
    """BUG-03: a provider whose constructor raises must not abort the digest."""

    class CtorBoomProvider(GitProvider):
        get_activity = MagicMock()
        get_attention = MagicMock()

        def __init__(self, username, token):
            raise RuntimeError("ctor boom")

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

    with patch("gitreport.main.PROVIDER_MAP", {"mock": CtorBoomProvider}):
        result = CliRunner().invoke(cli, ["digest"])

    assert result.exit_code == 0, result.output  # degraded, not aborted
    assert "could not initialise provider mock" in result.output
    assert "stale" in result.output.lower()
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["items"][item_id]["status"] == "open"  # ctor failure: no absence-resolution


@patch("gitreport.main.load_config")
def test_digest_stale_warning_names_provider_once(mock_load_config, tmp_path):
    """R6: a ctor-failed provider is stale in both fetch lists (attention and
    activity); the digest warning must mention the name exactly once."""

    class CtorBoomProvider(GitProvider):
        get_activity = MagicMock()
        get_attention = MagicMock()

        def __init__(self, username, token):
            raise RuntimeError("ctor boom")

    mock_load_config.return_value = _config_with(
        state_path=tmp_path / "state.json",
        stem=tmp_path / "digests" / "2026-09-28",
        latest=tmp_path / "digests" / "latest",
    )

    with patch("gitreport.main.PROVIDER_MAP", {"mock": CtorBoomProvider}):
        result = CliRunner().invoke(cli, ["digest"])

    assert result.exit_code == 0, result.output
    stale_lines = [line for line in result.output.splitlines() if "failed to fetch" in line]
    assert len(stale_lines) == 1
    assert stale_lines[0].count("mock") == 1


@patch("gitreport.main.load_config")
def test_ack_zero_matches_fails_cleanly(mock_load_config, tmp_path):
    """BUG-06: zero matches exit non-zero via ClickException, not a traceback."""
    mock_load_config.return_value = _config_with(
        state_path=tmp_path / "state.json",
        stem=tmp_path / "digests" / "2026-09-28",
        latest=tmp_path / "digests" / "latest",
    )

    result = CliRunner().invoke(cli, ["ack", "no-such-thing"])

    _assert_clean_cli_error(result, "No open item matches 'no-such-thing'")


@patch("gitreport.main.load_config")
def test_unack_zero_matches_fails_cleanly(mock_load_config, tmp_path):
    mock_load_config.return_value = _config_with(
        state_path=tmp_path / "state.json",
        stem=tmp_path / "digests" / "2026-09-28",
        latest=tmp_path / "digests" / "latest",
    )

    result = CliRunner().invoke(cli, ["unack", "no-such-thing"])

    _assert_clean_cli_error(result, "No acked or resolved item matches 'no-such-thing'")


@patch("gitreport.main.load_config")
def test_unack_open_item_is_rejected(mock_load_config, tmp_path):
    """BUG-05: unack matches only acked/resolved items, never open ones."""
    from gitreport.state import save_state

    item_id = "gh:https://github.com/o/r/pull/1"
    state = AttentionState()
    state.items[item_id] = _seed_item(item_id)
    save_state(tmp_path / "state.json", state)
    mock_load_config.return_value = _config_with(
        state_path=tmp_path / "state.json",
        stem=tmp_path / "digests" / "2026-09-28",
        latest=tmp_path / "digests" / "latest",
    )

    result = CliRunner().invoke(cli, ["unack", "o/r/pull/1"])

    _assert_clean_cli_error(result, "No acked or resolved item matches")


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_digest_writes_dot_stem_paths_correctly(mock_load_config, tmp_path_factory):
    """BUG-08: a dot in the static part of digest_output must not break paths."""
    tmp = tmp_path_factory.mktemp("dotstem")
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json",
        stem=tmp / "a.b" / today,
        latest=tmp / "a.b" / "latest",
    )

    result = CliRunner().invoke(cli, ["digest"])

    assert result.exit_code == 0, result.output
    assert (tmp / "a.b" / f"{today}.md").exists()
    assert (tmp / "a.b" / f"{today}.html").exists()
    assert (tmp / "a.b" / "latest.md").is_symlink()
    assert (tmp / "a.b" / "latest.html").is_symlink()


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_symlink_target_is_absolute_for_relative_config(mock_load_config, tmp_path, monkeypatch):
    """BUG-09: with a relative digest_output, latest.md must still resolve."""
    monkeypatch.chdir(tmp_path)
    mock_load_config.return_value = _config_with(
        state_path=tmp_path / "state.json",
        stem=Path("digests/2026-09-28"),  # literal date: stable regardless of today
        latest=Path("digests/latest"),
    )

    result = CliRunner().invoke(cli, ["digest"])

    assert result.exit_code == 0, result.output
    link = tmp_path / "digests" / "latest.md"
    assert link.is_symlink()
    target = os.readlink(link)
    assert os.path.isabs(target)
    assert link.resolve() == tmp_path / "digests" / "2026-09-28.md"
    assert link.exists()  # symlink resolves to the dated file


def test_open_in_browser_builds_proper_file_uri(tmp_path, monkeypatch):
    """R9a: file URLs must be proper URIs — spaces percent-encoded — not a
    raw path spliced after the scheme."""
    opened: list[str] = []
    monkeypatch.setattr("gitreport.main.webbrowser.open", lambda uri: opened.append(uri) or True)
    target = tmp_path / "digest dir" / "digest file.html"
    target.parent.mkdir(parents=True)
    target.write_text("<html></html>")

    from gitreport.main import _open_in_browser

    _open_in_browser(target)

    assert opened == [target.resolve().as_uri()]
    assert "%20" in opened[0]


@pytest.mark.parametrize(
    "argv",
    [["attention"], ["digest"], ["read"], ["ack", "x"], ["unack", "x"]],
)
@patch("gitreport.main.load_config")
def test_commands_fail_cleanly_on_bad_config(mock_load_config, argv):
    """BUG-11: the five new commands map config errors to ClickException."""
    mock_load_config.side_effect = FileNotFoundError("Configuration file not found.")

    result = CliRunner().invoke(cli, argv)

    _assert_clean_cli_error(result, "Configuration file not found.")


# --- digest --catch-up (start-up / resume hooks) ---


def _seed_state(state_path, last_digest_run):
    from gitreport.state import AttentionState, save_state

    save_state(state_path, AttentionState(last_digest_run=last_digest_run))


def _reset_noop():
    NoopProvider.get_attention.reset_mock()
    NoopProvider.get_activity.reset_mock()


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_digest_catch_up_skips_when_ran_today(mock_load_config, tmp_path_factory):
    """A digest generated today (local date) means --catch-up is a no-op."""
    from datetime import UTC

    tmp = tmp_path_factory.mktemp("catchup-skip")
    state_path = tmp / "state.json"
    _seed_state(state_path, datetime.now(UTC).isoformat())
    mock_load_config.return_value = _config_with(
        state_path=state_path,
        stem=tmp / "digests" / "2026-09-28",
        latest=tmp / "digests" / "latest",
    )
    _reset_noop()

    result = CliRunner().invoke(cli, ["digest", "--catch-up"])

    assert result.exit_code == 0, result.output
    assert "skipping" in result.output.lower()
    assert NoopProvider.get_attention.call_count == 0
    assert NoopProvider.get_activity.call_count == 0


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_digest_catch_up_runs_when_last_run_was_yesterday(mock_load_config, tmp_path_factory):
    """No digest today (local date) -> --catch-up does a full run."""
    from datetime import UTC, timedelta

    tmp = tmp_path_factory.mktemp("catchup-run")
    state_path = tmp / "state.json"
    _seed_state(state_path, (datetime.now(UTC) - timedelta(days=1)).isoformat())
    mock_load_config.return_value = _config_with(
        state_path=state_path,
        stem=tmp / "digests" / "2026-09-28",
        latest=tmp / "digests" / "latest",
    )
    _reset_noop()

    result = CliRunner().invoke(cli, ["digest", "--catch-up"])

    assert result.exit_code == 0, result.output
    assert NoopProvider.get_attention.call_count == 1
    assert NoopProvider.get_activity.call_count == 1


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_digest_catch_up_runs_on_first_ever(mock_load_config, tmp_path_factory):
    """Unset last_digest_run -> --catch-up runs (first morning)."""
    tmp = tmp_path_factory.mktemp("catchup-first")
    state_path = tmp / "state.json"
    _seed_state(state_path, None)
    mock_load_config.return_value = _config_with(
        state_path=state_path,
        stem=tmp / "digests" / "2026-09-28",
        latest=tmp / "digests" / "latest",
    )
    _reset_noop()

    result = CliRunner().invoke(cli, ["digest", "--catch-up"])

    assert result.exit_code == 0, result.output
    assert NoopProvider.get_attention.call_count == 1


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_digest_without_catch_up_always_runs(mock_load_config, tmp_path_factory):
    """Plain digest ignores the gate (the scheduled timer must always run)."""
    from datetime import UTC

    tmp = tmp_path_factory.mktemp("catchup-plain")
    state_path = tmp / "state.json"
    _seed_state(state_path, datetime.now(UTC).isoformat())
    mock_load_config.return_value = _config_with(
        state_path=state_path,
        stem=tmp / "digests" / "2026-09-28",
        latest=tmp / "digests" / "latest",
    )
    _reset_noop()

    result = CliRunner().invoke(cli, ["digest"])

    assert result.exit_code == 0, result.output
    assert NoopProvider.get_attention.call_count == 1


# --- Task 7: dashboard artifact wiring (.json snapshot, read re-render) ------


class CategorizedProvider(GitProvider):
    """Provider whose attention fetch yields a review request and a mention."""

    get_activity = MagicMock()
    get_attention = MagicMock(
        return_value={
            "ok": True,
            "items": [
                {
                    "id": "gh:https://github.com/o/r/pull/1",
                    "provider": "github",
                    "kind": "review_requested",
                    "origin": "notification",
                    "repo": "o/r",
                    "title": "PR one",
                    "url": "https://github.com/o/r/pull/1",
                    "reason": "review requested",
                    "updated_at": "2026-09-28T05:00:00+00:00",
                    "thread_url": None,
                },
                {
                    "id": "gh:https://github.com/o/r2/issue/2",
                    "provider": "github",
                    "kind": "mention",
                    "origin": "query",
                    "repo": "o/r2",
                    "title": "Issue two",
                    "url": "https://github.com/o/r2/issue/2",
                    "reason": "mentioned you",
                    "updated_at": "2026-09-28T05:30:00+00:00",
                    "thread_url": None,
                },
            ],
            "resolved_ids": [],
            "error": None,
        }
    )

    def __init__(self, username, token):
        pass


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_digest_writes_json_snapshot_without_symlink(mock_load_config, tmp_path_factory):
    """Every digest run writes the .md/.json/.html artifacts; the .json is
    renderer infrastructure: never symlinked as latest.json."""
    tmp = tmp_path_factory.mktemp("json-artifact")
    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json",
        stem=tmp / "digests" / "2026-09-28",
        latest=tmp / "digests" / "latest",
    )

    result = CliRunner().invoke(cli, ["digest"])

    assert result.exit_code == 0, result.output
    assert (tmp / "digests" / "2026-09-28.md").exists()
    assert (tmp / "digests" / "2026-09-28.html").exists()
    model = json.loads((tmp / "digests" / "2026-09-28.json").read_text())
    assert model["schema_version"] == 1
    assert model["status"] == {"reviewed": False, "reviewed_at": None}
    assert not (tmp / "digests" / "latest.json").exists()
    assert not (tmp / "digests" / "latest.json").is_symlink()
    assert (tmp / "digests" / "latest.md").is_symlink()
    assert (tmp / "digests" / "latest.html").is_symlink()


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_digest_md_only_writes_json_but_no_html(mock_load_config, tmp_path_factory):
    """With `md` the only configured format, .md + .json are still written
    (the snapshot is infrastructure) but no .html artifact or symlink."""
    tmp = tmp_path_factory.mktemp("md-only")
    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json",
        stem=tmp / "digests" / "2026-09-28",
        latest=tmp / "digests" / "latest",
        formats=["md"],
    )

    result = CliRunner().invoke(cli, ["digest"])

    assert result.exit_code == 0, result.output
    assert (tmp / "digests" / "2026-09-28.md").exists()
    assert (tmp / "digests" / "2026-09-28.json").exists()
    assert not (tmp / "digests" / "2026-09-28.html").exists()
    assert not (tmp / "digests" / "latest.html").exists()


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": CategorizedProvider})
def test_digest_stdout_is_categorized_markdown(mock_load_config, tmp_path_factory):
    """The digest stdout mirrors the dashboard: repo groups under `####` and
    per-item type tags in badge precedence order."""
    tmp = tmp_path_factory.mktemp("categorized")
    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json",
        stem=tmp / "digests" / "2026-09-28",
        latest=tmp / "digests" / "latest",
    )

    result = CliRunner().invoke(cli, ["digest"])

    assert result.exit_code == 0, result.output
    assert re.search(r"^#### o/r$", result.output, re.MULTILINE)
    assert re.search(r"^#### o/r2$", result.output, re.MULTILINE)
    assert "(PR review" in result.output
    assert "(mention" in result.output


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_read_flips_json_status_and_rerenders_dashboard(mock_load_config, tmp_path_factory):
    """read advances the sibling .json's status and re-renders the .html from
    the model; only the status banner line differs from the pre-read HTML."""
    tmp = tmp_path_factory.mktemp("read-rerender")
    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json",
        stem=tmp / "digests" / "2026-09-28",
        latest=tmp / "digests" / "latest",
    )

    runner = CliRunner()
    digest_result = runner.invoke(cli, ["digest"])
    assert digest_result.exit_code == 0, digest_result.output
    html_path = tmp / "digests" / "2026-09-28.html"
    before = html_path.read_text()

    result = runner.invoke(cli, ["read"])

    assert result.exit_code == 0, result.output
    md_text = (tmp / "digests" / "2026-09-28.md").read_text()
    assert "Status: Reviewed" in md_text
    model = json.loads((tmp / "digests" / "2026-09-28.json").read_text())
    assert model["schema_version"] == 1
    assert model["status"]["reviewed"] is True
    assert model["status"]["reviewed_at"]
    html_text = html_path.read_text()
    assert "Reviewed" in html_text
    assert "NOT YET REVIEWED" not in html_text
    # TEST-02 banner-only byte diff: substitute the status pill in both
    # versions and assert full-string equality — no line-dropping, so any
    # change outside the pill fails the test.
    pill = re.compile(r'<span class="gr-status[^"]*">[^<]*</span>')
    assert pill.sub("PILL", before) == pill.sub("PILL", html_text)


def _write_digest_files(directory, day_stem, generated_at):
    """Write one complete .md/.json/.html artifact set, as a digest run would."""
    from gitreport.attention import render_digest_markdown, report_model
    from gitreport.html_report import atomic_write_text, render_dashboard_html

    model = report_model(
        AttentionState(),
        generated_at=generated_at,
        coverage_start="2026-09-20T06:00:00+00:00",
        first_run=True,
        stale_providers=[],
    )
    stem = directory / day_stem
    atomic_write_text(stem.with_name(stem.name + ".md"), render_digest_markdown(model))
    atomic_write_text(stem.with_name(stem.name + ".json"), json.dumps(model, indent=2))
    atomic_write_text(stem.with_name(stem.name + ".html"), render_dashboard_html(model))
    return stem


@patch("gitreport.main.load_config")
def test_read_multi_target_updates_every_newer_digest(mock_load_config, tmp_path):
    """With two digests newer than the cursor, read processes each target
    independently: both .mds restamped, both .jsons flipped, both .htmls
    re-rendered."""
    from gitreport.state import save_state

    tmp = tmp_path
    (tmp / "digests").mkdir()
    _write_digest_files(tmp / "digests", "2026-09-26", "2026-09-26T06:00:00+00:00")
    _write_digest_files(tmp / "digests", "2026-09-28", "2026-09-28T06:00:00+00:00")
    save_state(
        tmp / "state.json",
        AttentionState(last_reviewed="2026-09-25T06:00:00+00:00"),
    )
    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json",
        stem=tmp / "digests" / "YYYY-MM-DD",
        latest=tmp / "digests" / "latest",
    )

    result = CliRunner().invoke(cli, ["read"])

    assert result.exit_code == 0, result.output
    for day in ("2026-09-26", "2026-09-28"):
        md_text = (tmp / "digests" / f"{day}.md").read_text()
        assert "Status: Reviewed" in md_text
        assert "NOT YET REVIEWED" not in md_text
        model = json.loads((tmp / "digests" / f"{day}.json").read_text())
        assert model["status"]["reviewed"] is True
        assert model["status"]["reviewed_at"]
        assert "NOT YET REVIEWED" not in (tmp / "digests" / f"{day}.html").read_text()


@patch("gitreport.main.load_config")
def test_read_write_failure_warns_and_continues(mock_load_config, tmp_path):
    """BUG-03 (final wave): a broken target (its .md replaced by a directory)
    must not abort `read` — the earlier target is fully updated, the broken
    one is warned about, and the command still exits 0. The directory is
    caught by the digest-scan guard before the per-target write guard; both
    are part of the same never-abort contract."""
    from gitreport.state import save_state

    tmp = tmp_path
    (tmp / "digests").mkdir()
    _write_digest_files(tmp / "digests", "2026-09-26", "2026-09-26T06:00:00+00:00")
    _write_digest_files(tmp / "digests", "2026-09-28", "2026-09-28T06:00:00+00:00")
    broken = tmp / "digests" / "2026-09-28.md"
    broken.unlink()
    broken.mkdir()
    save_state(
        tmp / "state.json",
        AttentionState(last_reviewed="2026-09-25T06:00:00+00:00"),
    )
    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json",
        stem=tmp / "digests" / "YYYY-MM-DD",
        latest=tmp / "digests" / "latest",
    )

    result = CliRunner().invoke(cli, ["read"])

    assert result.exit_code == 0, result.output
    assert "Warning" in result.output
    assert str(broken) in result.output
    # The first target (2026-09-26, scanned before the broken one) is fully
    # updated: restamped .md and reviewed .json.
    md_text = (tmp / "digests" / "2026-09-26.md").read_text()
    assert "Status: Reviewed" in md_text
    model = json.loads((tmp / "digests" / "2026-09-26.json").read_text())
    assert model["status"]["reviewed"] is True


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_read_html_disabled_never_touches_html(mock_load_config, tmp_path_factory):
    """With html disabled, digest writes .md/.json only and read still flips
    the .json status — but never creates or updates a .html artifact."""
    tmp = tmp_path_factory.mktemp("read-nohtml")
    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json",
        stem=tmp / "digests" / "2026-09-28",
        latest=tmp / "digests" / "latest",
        formats=["md"],
    )

    runner = CliRunner()
    assert runner.invoke(cli, ["digest"]).exit_code == 0
    html_path = tmp / "digests" / "2026-09-28.html"
    assert not html_path.exists()

    result = runner.invoke(cli, ["read"])

    assert result.exit_code == 0, result.output
    assert not html_path.exists()
    model = json.loads((tmp / "digests" / "2026-09-28.json").read_text())
    assert model["status"]["reviewed"] is True


@patch("gitreport.main.load_config")
def test_read_missing_json_uses_legacy_fallback(mock_load_config, tmp_path):
    """Pre-upgrade digests carry no .json: read restamps the .md and re-renders
    the .html through the legacy Markdown conversion (without fabricating a
    .json snapshot)."""
    from gitreport.state import save_state

    tmp = tmp_path
    stem = tmp / "digests" / "2026-09-28"
    (tmp / "digests").mkdir()
    (tmp / "digests" / "2026-09-28.md").write_text(
        "---\n"
        "generated_at: 2026-09-28T06:00:00+00:00\n"
        "coverage_start: 2026-09-20T06:00:00+00:00\n"
        "---\n\n"
        "# GitReport digest\n\n"
        "Status: NOT YET REVIEWED — run `gitreport read` after reviewing\n"
    )
    (tmp / "digests" / "2026-09-28.html").write_text("LEGACY-OLD-HTML")
    save_state(tmp / "state.json", AttentionState())
    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json", stem=stem, latest=tmp / "digests" / "latest"
    )

    result = CliRunner().invoke(cli, ["read"])

    assert result.exit_code == 0, result.output
    md_text = (tmp / "digests" / "2026-09-28.md").read_text()
    assert "Status: Reviewed" in md_text
    html_text = (tmp / "digests" / "2026-09-28.html").read_text()
    assert "LEGACY-OLD-HTML" not in html_text
    assert "Status: Reviewed" in html_text
    assert "NOT YET REVIEWED" not in html_text
    assert not (tmp / "digests" / "2026-09-28.json").exists()


@pytest.mark.parametrize(
    "make_json_text, usable, fragment",
    [
        pytest.param(
            lambda _base: "{corrupt json", False, "corrupt or unreadable", id="decode-error"
        ),
        pytest.param(lambda _base: json.dumps([1, 2]), False, "not a JSON object", id="non-object"),
        pytest.param(
            lambda base: json.dumps({**base, "schema_version": 2}),
            False,
            "schema_version",
            id="schema-version-2",
        ),
        pytest.param(
            lambda base: json.dumps({k: v for k, v in base.items() if k != "kpi"}),
            False,
            "missing key",
            id="missing-key",
        ),
        pytest.param(
            lambda base: json.dumps({**base, "generated_at": "2020-01-01T00:00:00+00:00"}),
            False,
            "generated_at does not match",
            id="generated-at-diverged",
        ),
        pytest.param(
            lambda base: json.dumps({**base, "future_field": {"new": [1]}}),
            True,
            None,
            id="extra-key-tolerated",
        ),
    ],
)
@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_read_json_usability_matrix(
    mock_load_config, tmp_path_factory, make_json_text, usable, fragment
):
    """TEST-01: every unusable-.json condition (decode error, non-object,
    schema_version mismatch, missing key, generated_at divergence) warns and
    restamps the .md only, leaving the .json and .html byte-identical —
    while an extra top-level key is tolerated (forward compat, mirrors
    state.py extra="allow") and takes the normal reviewed path."""
    tmp = tmp_path_factory.mktemp("read-corrupt")
    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json",
        stem=tmp / "digests" / "2026-09-28",
        latest=tmp / "digests" / "latest",
    )

    runner = CliRunner()
    assert runner.invoke(cli, ["digest"]).exit_code == 0
    json_path = tmp / "digests" / "2026-09-28.json"
    base = json.loads(json_path.read_text())
    json_path.write_text(make_json_text(base))
    pre_json = json_path.read_bytes()
    html_path = tmp / "digests" / "2026-09-28.html"
    pre_html = html_path.read_bytes()

    result = runner.invoke(cli, ["read"])

    assert result.exit_code == 0, result.output
    md_text = (tmp / "digests" / "2026-09-28.md").read_text()
    assert "Status: Reviewed" in md_text  # never gated on the sibling .json
    if not usable:
        assert "Warning" in result.output
        assert fragment in result.output
        assert json_path.read_bytes() == pre_json  # untouched, still unreviewed
        assert html_path.read_bytes() == pre_html
        return
    assert "Warning" not in result.output
    model = json.loads(json_path.read_text())
    assert model["schema_version"] == 1
    assert model["status"]["reviewed"] is True
    assert model["status"]["reviewed_at"]
    assert "NOT YET REVIEWED" not in html_path.read_text()


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_read_invalid_utf8_json_warns_and_restamps_md_only(mock_load_config, tmp_path_factory):
    """BUG-01: invalid UTF-8 in the .json raises UnicodeDecodeError from
    read_text (not a JSONDecodeError) — read must degrade the same way:
    warn, restamp the .md only, .json/.html byte-identical."""
    tmp = tmp_path_factory.mktemp("read-utf8")
    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json",
        stem=tmp / "digests" / "2026-09-28",
        latest=tmp / "digests" / "latest",
    )

    runner = CliRunner()
    assert runner.invoke(cli, ["digest"]).exit_code == 0
    json_path = tmp / "digests" / "2026-09-28.json"
    json_path.write_bytes(b'{"schema_version": \xff\xfe}')
    pre_json = json_path.read_bytes()
    html_path = tmp / "digests" / "2026-09-28.html"
    pre_html = html_path.read_bytes()

    result = runner.invoke(cli, ["read"])

    assert result.exit_code == 0, result.output
    assert "Warning" in result.output
    assert "corrupt or unreadable" in result.output
    assert "Status: Reviewed" in (tmp / "digests" / "2026-09-28.md").read_text()
    assert json_path.read_bytes() == pre_json
    assert html_path.read_bytes() == pre_html


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_read_directory_json_warns_and_restamps_md_only(mock_load_config, tmp_path_factory):
    """BUG-01: a .json path that is actually a directory raises
    IsADirectoryError — again an OSError, not a JSONDecodeError; the cursor
    has already advanced, so a crash here would permanently skip later
    targets. Read must degrade: warn, .md-only restamp."""
    tmp = tmp_path_factory.mktemp("read-dirjson")
    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json",
        stem=tmp / "digests" / "2026-09-28",
        latest=tmp / "digests" / "latest",
    )

    runner = CliRunner()
    assert runner.invoke(cli, ["digest"]).exit_code == 0
    json_path = tmp / "digests" / "2026-09-28.json"
    json_path.unlink()
    json_path.mkdir()
    html_path = tmp / "digests" / "2026-09-28.html"
    pre_html = html_path.read_bytes()

    result = runner.invoke(cli, ["read"])

    assert result.exit_code == 0, result.output
    assert "Warning" in result.output
    assert "corrupt or unreadable" in result.output
    assert "Status: Reviewed" in (tmp / "digests" / "2026-09-28.md").read_text()
    assert json_path.is_dir()  # nothing was written over the directory
    assert html_path.read_bytes() == pre_html


@patch("gitreport.main.load_config")
@patch("gitreport.main.PROVIDER_MAP", {"mock": NoopProvider})
def test_read_render_failure_leaves_artifacts_consistent(mock_load_config, tmp_path_factory):
    """BUG-02: a render crash after the .json loaded (here `attention`
    poisoned to a string) must not leave a reviewed .json next to a stale
    .html. The render runs FIRST; its failure is treated like an unusable
    .json: warn, keep both artifacts byte-identical (.json still
    unreviewed), .md-only restamp."""
    tmp = tmp_path_factory.mktemp("read-render-boom")
    mock_load_config.return_value = _config_with(
        state_path=tmp / "state.json",
        stem=tmp / "digests" / "2026-09-28",
        latest=tmp / "digests" / "latest",
    )

    runner = CliRunner()
    assert runner.invoke(cli, ["digest"]).exit_code == 0
    json_path = tmp / "digests" / "2026-09-28.json"
    poisoned = json.loads(json_path.read_text())
    poisoned["attention"] = "poisoned-not-a-dict"
    json_path.write_text(json.dumps(poisoned))
    pre_json = json_path.read_bytes()
    html_path = tmp / "digests" / "2026-09-28.html"
    pre_html = html_path.read_bytes()

    result = runner.invoke(cli, ["read"])

    assert result.exit_code == 0, result.output
    assert "Warning" in result.output
    assert "re-render" in result.output
    assert "Status: Reviewed" in (tmp / "digests" / "2026-09-28.md").read_text()
    assert json_path.read_bytes() == pre_json  # never written: still unreviewed
    assert json.loads(json_path.read_text()) == poisoned
    assert html_path.read_bytes() == pre_html
