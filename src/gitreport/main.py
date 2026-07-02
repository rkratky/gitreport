from datetime import UTC
from pathlib import Path

import click
import dateparser
from github import BadCredentialsException

from .config import load_config
from .providers.base import GitProvider
from .providers.github import GitHubProvider
from .providers.launchpad import LaunchpadProvider
from .reporting import generate_report

PROVIDER_MAP: dict[str, type[GitProvider]] = {
    "github": GitHubProvider,
    "launchpad": LaunchpadProvider,
}


@click.group()
def cli():
    """A CLI tool to gather Git activity and generate a Markdown report."""
    pass


@cli.command()
@click.option(
    "--config",
    "config_path_str",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the configuration file.",
)
@click.option(
    "--start-date",
    type=str,
    default="1 week ago",
    help='The start date for the report (YYYY-MM-DD or natural language, e.g., "1 week ago").',
)
@click.option(
    "--end-date",
    type=str,
    default="today",
    help='The end date for the report (YYYY-MM-DD or natural language, e.g., "today").',
)
@click.option(
    "--fast",
    is_flag=True,
    default=False,
    help=(
        "Skip per-item verification API calls for speed. Faster on large "
        "windows, but slightly less accurate at the window boundaries and "
        "omits the GitHub 'merged' category (which cannot be derived from "
        "search alone)."
    ),
)
def generate(config_path_str: Path | None, start_date: str, end_date: str, fast: bool):
    """Generate the activity report."""
    try:
        # Parse dates
        start_dt = dateparser.parse(start_date)
        end_dt = dateparser.parse(end_date)
        if not start_dt or not end_dt:
            raise ValueError("Could not parse date strings. Please use a valid format.")

        # Ensure datetimes are timezone-aware (UTC) to allow comparison
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=UTC)
        else:
            start_dt = start_dt.astimezone(UTC)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=UTC)
        else:
            end_dt = end_dt.astimezone(UTC)

        config = load_config(config_path_str)
        click.echo("Configuration loaded successfully.", err=True)

        provider_data = {}
        for provider_name, provider_config in config.providers.items():
            if provider_name in PROVIDER_MAP:
                try:
                    ProviderClass = PROVIDER_MAP[provider_name]
                    token = (
                        provider_config.token.get_secret_value()
                        if provider_config.token is not None
                        else None
                    )
                    provider = ProviderClass(
                        username=provider_config.username,
                        token=token,
                    )
                    click.echo(f"Fetching data from {provider_name}...", err=True)
                    provider_data[provider_name] = provider.get_activity(
                        start_dt, end_dt, fast=fast
                    )
                except BadCredentialsException as e:
                    click.echo(
                        f"Error: Bad credentials for {provider_name}. "
                        "Please check your token in the configuration file.",
                        err=True,
                    )
                    raise click.ClickException(f"Bad credentials for {provider_name}.") from e
                except Exception as e:
                    # A provider may be temporarily unavailable (e.g. Launchpad
                    # returning HTTP 503). Fail gracefully: warn the user, skip
                    # this provider, and generate the report from whatever data
                    # was collected from the remaining providers.
                    click.echo(
                        f"Warning: could not fetch data from {provider_name} ({e}); "
                        "skipping this provider. The report will include only "
                        "data from providers that succeeded.",
                        err=True,
                    )
                    continue

        report = generate_report(provider_data)
        click.echo(report)

    except (FileNotFoundError, ValueError) as e:
        raise click.ClickException(str(e)) from e


if __name__ == "__main__":
    cli()
