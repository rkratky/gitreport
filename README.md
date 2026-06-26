# Git Report

A CLI tool to gather a user's Git activity from configured Git providers (e.g., GitHub, Launchpad) and output it as a Markdown report.

## Features

*   Gathers activity from multiple Git providers (GitHub and Launchpad supported).
*   Generates a Markdown report grouped by provider and repository.
*   Separates activity into PRs submitted, PRs reviewed, issues created, and issues closed.
*   Configurable via a YAML file.
*   Flexible date handling, including natural language.

## Installation

This project uses [Poetry](https://python-poetry.org/) for dependency management.

1.  **Clone the repository:**
    ```bash
    git clone <repository-url>
    cd gitreport
    ```

2.  **Install dependencies:**
    ```bash
    poetry install
    ```

## Configuration

The tool requires a configuration file to access Git providers.

1.  **Create the config file:**
    The tool looks for a configuration file at `~/.config/gitreport/config.yaml` by default.

    You can create the directory and file with:
    ```bash
    mkdir -p ~/.config/gitreport
    touch ~/.config/gitreport/config.yaml
    ```

2.  **Add your configuration:**
    The configuration file should be in YAML format. See `config.example.yaml` in this repository for a template.

    **Example `config.yaml`:**
    ```yaml
    providers:
      github:
        username: "your-github-username"
        token: "your-github-personal-access-token" # Needs repo and user scopes
      launchpad:
        username: "your-launchpad-id"
        token: "" # Not currently used
    ```

    ```

### Launchpad Authentication

To access private repositories on Launchpad, you need to authenticate once. The tool will then use your saved credentials.

1.  **Run the authentication script:**
    ```bash
    poetry run python auth_launchpad.py
    ```

2.  **Authorize in your browser:**
    Your web browser will open and ask you to authorize the "gitreport-cli" application. Approve the request.

3.  **Confirmation:**
    The script will confirm that your credentials have been saved to `~/.config/gitreport/lp_credentials`. The main `gitreport` tool will now be able to access your private Launchpad data.

## Development

Interested in contributing? Here's how to get started.

### Setup

1.  **Clone the repository and install dependencies:**
    ```bash
    git clone <repository-url>
    cd gitreport
    poetry install
    ```
    This will install both the main and development dependencies.

### Running Tests

This project uses `pytest` for testing. To run the full test suite:

```bash
poetry run pytest
```

## Usage

You can run the tool using `poetry run gitreport`.

The `generate` command fetches the data and prints the Markdown report.

```bash
poetry run gitreport generate [OPTIONS]
```

### Options

*   `--config FILE`: Path to a custom configuration file.
*   `--start-date TEXT`: The start date for the report. Accepts `YYYY-MM-DD` or natural language strings (e.g., `"1 week ago"`, `"last Thursday"`). Defaults to `"1 week ago"`.
*   `--end-date TEXT`: The end date for the report. Accepts `YYYY-MM-DD` or natural language. Defaults to today.

### Examples

*   **Generate a report for the last week:**
    ```bash
    poetry run gitreport generate
    ```

*   **Generate a report for a specific date range:**
    ```bash
    poetry run gitreport generate --start-date 2024-01-01 --end-date 2024-01-31
    ```

*   **Generate a report since last Tuesday:**
    ```bash
    poetry run gitreport generate --start-date "last Tuesday"
    ```
