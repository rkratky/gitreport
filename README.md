# Git Report

A CLI tool to gather a user's Git activity from configured Git providers (e.g., GitHub, Launchpad) and output it as a Markdown report.

## Features

*   Gathers activity from multiple Git providers (GitHub and Launchpad supported).
*   Generates a Markdown report grouped by provider and repository.
*   Reports PRs/merge proposals submitted, reviewed, and merged (when merged by
    the user but authored by someone else), plus issues/bugs created and closed.
*   Empty categories, repositories, and providers are omitted from the report.
*   Attention digest: a daily "what needs my attention" inbox with an
    ack/unack lifecycle, catch-up-safe scheduling, and Markdown/HTML digests.
    The HTML digest is a self-contained dark dashboard: KPI strip, two-column
    body, collapsible repo groups, PR/issue/bug/MP/CI badges, humanized ages
    ("2 months ago"), search plus type/age/repo filter chips, inline JS, no
    network.
*   Configurable via a YAML file.
*   Flexible date handling, including natural language.

### Known limitation: Launchpad reviewed merge proposals

The Launchpad web-service API has **no endpoint for "merge proposals I have
reviewed."** The only person-scoped method, `getRequestedReviews`, is a
*to-do list* — it returns only MPs where a review is still **pending**
(not yet cast). Once a review is completed, the MP is removed from the list
regardless of the `status` parameter.

This means the "Merge Proposals Reviewed" section for Launchpad will typically
be empty or very incomplete for users who complete their reviews promptly.
The tool prints a warning to stderr when this happens. This is a fundamental
Launchpad API limitation, not a bug in this tool.

## Installation

Two ways to install, depending on what you want:

### As a CLI tool (recommended for daily use)

[pipx](https://pipx.pypa.io/) installs the tool into its own persistent virtual
environment and exposes a stable `gitreport` entrypoint on your PATH — nothing
depends on a project-local or hash-named venv that can break or move:

```bash
git clone <repository-url>
cd gitreport
pipx install .
```

The tool is now available everywhere as `gitreport` (`~/.local/bin/gitreport`).

**Upgrading:** after pulling new code, reinstall in one step — run this from
anywhere:

```bash
pipx install --force /path/to/your/gitreport-clone
```

### For development

This project uses [Poetry](https://python-poetry.org/) for dependency
management. The Poetry environment is only for running tests and linters;
the pipx-installed CLI above does not depend on it.

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
        token: "your-github-personal-access-token"  # Classic PAT: repo + user scopes, plus notifications for the attention digest
      launchpad:
        username: "your-launchpad-id"
        # No token needed; Launchpad auth uses saved OAuth credentials (below).
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
*   `--fast`: Skip per-item verification API calls for speed. This is much faster on large date ranges, but is slightly less accurate at the reporting-window boundaries and omits the GitHub "PRs Merged" category (GitHub's search API cannot express "merged by me", so that category requires the per-PR calls that `--fast` skips).

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

## Attention digest

Beyond the standalone `generate` report, gitreport can run as a **daily
digest**: each morning it collects what needs your attention (GitHub
notifications and queries, Launchpad items) plus your recent activity, writes
the digest to disk, and keeps an inbox-style state so items stop nagging once
you have dealt with them. The digest Markdown is categorized — repo groups
with Today / Last 7 days / Last 30 days / Older buckets, type tags, and
humanized ages.

### Commands

*   `gitreport attention`: print the attention report to stdout. A dry run —
    it does not mutate state; items not yet in state show as `New`.
*   `gitreport digest`: the morning run — attention plus activity since your
    last review; updates state (first-seen marking, reopens, resolutions,
    prune); writes the dated digest and refreshes the `latest` links; prints
    the digest Markdown. `--open` opens the digest in a browser. Does **not**
    advance the review cursor.
*   `gitreport read`: mark the newest digest(s) consumed (on the first-ever
    read, only the newest digest) and re-render their status line. `--open`
    also opens `latest`.
*   `gitreport ack [ID]`: mark item(s) done. `ID` is an item id or substring
    (interactive disambiguation when ambiguous); `--list` (or no `ID`)
    browses open items. Acking a GitHub-notification item also marks that
    thread read on GitHub (best effort).
*   `gitreport unack <ID>`: pull item(s) back into the inbox.

```bash
poetry run gitreport digest         # morning run: writes and prints the digest
poetry run gitreport read --open    # done reading; opens the latest digest
poetry run gitreport ack --list     # browse the inbox
poetry run gitreport ack 123        # ack by id or substring
```

### The banner: coverage and status lines

Every digest starts with two lines that have different lifetimes:

*   **Coverage line** — frozen at generation, always historically true:

    ```text
    Coverage: Mon 22 Sep – Mon 28 Sep (6 days since last review)
    ```

    On a first-ever run the start is a 24-hour fallback.

*   **Status line** — live; re-rendered in place by `gitreport read` for
    every digest generated since your previous read:

    ```text
    Status: NOT YET REVIEWED — run `gitreport read` after reviewing
    ```

    becomes

    ```text
    Status: Reviewed Mon 28 Sep 09:14
    ```

    This is the nudge when a digest was generated but never reviewed — there
    is deliberately no interactive prompt at generation time, so cron can
    never hang.

### Reading and the review cursor

`gitreport read` sets the review cursor to the **newest digest's generation
timestamp — not the wall clock**. Events that happen between a digest being
generated and you reading it therefore fall after the cursor and appear in
the next digest's coverage window: nothing is lost if you read yesterday's
digest a day late. Generating digests never advances the cursor; only
`gitreport read` does.

### Item lifecycle

Items move from **open** (in the inbox) to **acked** (you marked them done)
or **resolved** (auto-resolved because every source stopped reporting them
while unacked). New activity by someone other than you reopens an item.
`gitreport unack` pulls an item back into the inbox; un-acking a *resolved*
item pins it open until you ack it again, regardless of source state.

## Scheduling

The digest is designed to run once a day from a scheduler. All report windows
are anchored to saved state, not the calendar, so a missed day costs nothing:
the next digest covers the entire gap in a single catch-up digest (the tool
does not synthesise digests for days that never ran).

### systemd user timer

```ini
# ~/.config/systemd/user/gitreport-digest.service
[Unit]
Description=GitReport daily digest

[Service]
Type=oneshot
ExecStart=%h/.local/bin/gitreport digest
```

```ini
# ~/.config/systemd/user/gitreport-digest.timer
[Unit]
Description=Run gitreport digest every morning

[Timer]
OnCalendar=*-*-* 07:30:00
Persistent=true

[Install]
WantedBy=timers.target
```

Then enable the timer:

```bash
systemctl --user enable --now gitreport-digest.timer
```

`%h` expands to your home directory. The `ExecStart` paths assume the pipx
install from [Installation](#installation) (`~/.local/bin/gitreport`), which is
stable across upgrades — systemd units never need repointing.

`Persistent=true` makes the timer fire shortly after boot if the schedule
elapsed while the machine was off, so the catch-up digest is typically ready
the morning you return.

### Late starts: run at boot/wake-up even if it's past the schedule

The timer covers "machine was off at 07:30 and booted at 09:00".
Wake-from-suspend is handled by a resume hook plus `digest --catch-up`,
which runs a full digest only when none has been generated yet today (local
date) — so suspending and resuming repeatedly never spams API calls, and a
late start still produces the report:

```bash
gitreport digest --catch-up   # skips silently if today's digest already ran
```

Add a system-level resume unit (sudo, once):

```ini
# /etc/systemd/system/gitreport-resume.service
[Unit]
Description=GitReport digest catch-up on resume from suspend
After=suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target

[Service]
Type=oneshot
User=yourusername
Environment=HOME=/home/yourusername
ExecStart=/home/yourusername/.local/bin/gitreport digest --catch-up

[Install]
WantedBy=suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable gitreport-resume.service
```

A unit with `WantedBy=suspend.target` and `After=suspend.target` starts when
the machine resumes (the target is re-reached after sleep), covering both
wake-from-suspend and boot for laptops. `--catch-up` gates it to one digest
per day; the scheduled timer keeps using plain `digest` and always runs.

### cron equivalent

```cron
30 7 * * * $HOME/.local/bin/gitreport digest
@reboot        $HOME/.local/bin/gitreport digest --catch-up
```

Plain cron simply runs at the next scheduled time; coverage is identical
either way because windows are state-anchored. The `@reboot` line covers
late-start boots; cron has no suspend/resume hook — on laptops use the
systemd timer + resume unit instead.

### Caveats

*   **GitHub expires unread notifications after ~3 months.** An absence
    longer than that can lose notification-derived items (mentions, comments,
    review requests). Query-derived items (assigned issues, unresolved review
    threads, failing checks, stale PRs) and all Launchpad items are
    query-based and survive any absence.
*   The GitHub notifications API does not support fine-grained personal
    access tokens: a **classic PAT with the `notifications` scope** is
    required for the digest's notification-derived items.

## Config reference: the `attention:` block

The `attention:` block is optional — everything defaults as shown below, so
existing configurations keep working.

| Key | Default | Meaning |
| --- | --- | --- |
| `exclusions` | `{}` | Per-provider glob patterns matched against `owner/repo` (GitHub) or the project name (Launchpad). Applies to attention items only; the activity report is unchanged. |
| `stale_pr_days` | `7` | Flag your open PRs as stale after this many days without updates. |
| `state_path` | `~/.local/state/gitreport/state.json` | Location of the inbox state file (see [State file](#state-file)). |
| `digest_formats` | `[html, md]` | Published digest formats. The `.md` and `.json` files are always written (renderer infrastructure for `gitreport read`; the `.json` has no symlink); omitting `md` is a configuration error. Omitting `html` skips the HTML file and makes `--open` open the `.md`. |
| `digest_output` | `~/.local/state/gitreport/digests/YYYY-MM-DD` | Extension-less stem for the dated digest; the `YYYY-MM-DD` token is replaced with the local date. Files are `<stem>.md` / `<stem>.json` / `<stem>.html`; same-day reruns overwrite them. |
| `digest_latest` | `~/.local/state/gitreport/digests/latest` | Extension-less stem refreshed (symlinked) to the newest digest on every run. |

## State file

The attention digest keeps its inbox in `~/.local/state/gitreport/state.json`
(configurable via `attention.state_path`). All timestamps are stored in UTC.

**What it stores:** one record per attention item — status (`open`, `acked`,
`resolved`), origins and kinds, human-readable reasons, repo, title, URL,
`first_seen` / `last_updated`, `acked_at`, `resolved_at`, `pinned`, and
`reopen_count` — plus the report-level clocks: `last_reviewed` (the
consumption cursor; only `gitreport read` advances it), `reviewed_at` (the
wall-clock time of that read, used for the status line), and
`last_digest_run`.

**Corruption:** writes are atomic (temporary file + rename) and happen under
an exclusive lock, so corruption is unlikely. If the file is ever unreadable,
the next state-mutating command (`digest`, `read`) renames it
to `state.json.corrupt-<timestamp>` and rebuilds an empty state, with a
warning on stderr; read-only views (`attention`, `ack --list`) and item
commands (`ack <ID>`, `unack`) treat the corrupt file as empty without
renaming it or warning — item commands will simply report "No open item
matches". **Ack/read history in the renamed file is lost**, though the backup
remains on disk for manual inspection.

**Retention:** an `acked` or `resolved` item is pruned 30 days after its
sources stopped reporting it. While any source still reports an acked item,
the record is kept as a *suppression record* so it never re-enters the inbox
as New — a long-open assigned issue, for example, persists for as long as it
stays acked.
