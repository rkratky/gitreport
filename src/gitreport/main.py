from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Type

import click

import dateparser
from github import BadCredentialsException

from .config import load_config
from .providers.base import GitProvider
from .providers.github import GitHubProvider
from .providers.launchpad import LaunchpadProvider
from .reporting import generate_report

PROVIDER_MAP: Dict[str, Type[GitProvider]] = {
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
def generate(config_path_str: Optional[Path], start_date: str, end_date: str):
    """Generate the activity report."""
    try:
        # Parse dates
        start_dt = dateparser.parse(start_date)
        end_dt = dateparser.parse(end_date)
        if not start_dt or not end_dt:
            raise ValueError("Could not parse date strings. Please use a valid format.")

        # Ensure datetimes are timezone-aware (UTC) to allow comparison
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)

        config = load_config(config_path_str)
        click.echo("Configuration loaded successfully.", err=True)

        provider_data = {}
        for provider_name, provider_config in config.providers.items():
            if provider_name in PROVIDER_MAP:
                try:
                    ProviderClass = PROVIDER_MAP[provider_name]
                    provider = ProviderClass(
                        username=provider_config.username, token=provider_config.token
                    )
                    click.echo(f"Fetching data from {provider_name}...", err=True)
                    provider_data[provider_name] = provider.get_activity(
                        start_dt, end_dt
                    )
                except BadCredentialsException:
                    click.echo(
                        f"Error: Bad credentials for {provider_name}. "
                        "Please check your token in the configuration file.",
                        err=True,
                    )
                except Exception as e:
                    click.echo(
                        f"Error fetching data from {provider_name}: {e}", err=True
                    )

        report = generate_report(provider_data)
        click.echo(report)

    except (FileNotFoundError, ValueError) as e:
        click.echo(f"Error: {e}", err=True)




if __name__ == "__main__":
    cli()
