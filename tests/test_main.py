from datetime import timezone
from unittest.mock import patch, MagicMock

from click.testing import CliRunner
from src.gitreport.main import cli
from src.gitreport.providers.base import GitProvider
from src.gitreport.config import Config, ProviderConfig

class MockProvider(GitProvider):
    get_activity = MagicMock()
    def __init__(self, username, token):
        pass

@patch("src.gitreport.main.load_config")
@patch("src.gitreport.main.generate_report")
@patch("src.gitreport.main.PROVIDER_MAP", {"mock_provider": MockProvider})
def test_generate_command_passes_aware_datetimes(
    mock_generate_report, mock_load_config
):
    """Test the generate command passes aware datetimes to providers."""
    # Reset mock from previous runs
    MockProvider.get_activity.reset_mock()
    
    mock_config = Config(
        providers={
            "mock_provider": ProviderConfig(username="test", token="test")
        }
    )
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


@patch("src.gitreport.main.load_config")
def test_generate_command_invalid_date(mock_load_config):
    """Test the generate command with an invalid date string."""
    runner = CliRunner()
    result = runner.invoke(cli, ["generate", "--start-date", "not a date"])

    assert result.exit_code == 0 # The error is handled gracefully within the command
    assert "Error: Could not parse date strings." in result.output


@patch("src.gitreport.main.load_config")
@patch("src.gitreport.main.generate_report")
@patch("src.gitreport.main.dateparser")
@patch("src.gitreport.main.PROVIDER_MAP", {})
def test_generate_command_date_parsing(
    mock_dateparser, mock_generate_report, mock_load_config
):
    """Test that date strings are parsed correctly."""
    runner = CliRunner()
    runner.invoke(cli, ["generate", "--start-date", "last monday", "--end-date", "yesterday"])

    assert mock_dateparser.parse.call_count == 2
    mock_dateparser.parse.assert_any_call("last monday")
    mock_dateparser.parse.assert_any_call("yesterday")

