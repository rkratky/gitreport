from .providers.base import (
    ACTIVITY_CATEGORIES,
    ActivityItem,
    RepoActivity,
    escape_user,
    has_activity,
)

PROVIDER_LABELS = {
    "github": {
        "prs_submitted": "PRs submitted",
        "prs_reviewed": "PRs reviewed",
        "prs_merged": "PRs merged",
        "issues_created": "Issues created",
        "issues_closed": "Issues closed",
    },
    "launchpad": {
        "prs_submitted": "Merge proposals submitted",
        "prs_reviewed": "Merge proposals reviewed",
        "prs_merged": "Merge proposals merged",
        "issues_created": "Bugs created",
        "issues_closed": "Bugs closed",
    },
}

PROVIDER_DISPLAY_NAMES = {
    "github": "GitHub",
    "launchpad": "Launchpad",
}


def _format_item(item: ActivityItem) -> str:
    # The title is user-supplied (PR/issue titles) and would otherwise go
    # into Markdown raw — a title like "see [x](https://evil)" would render
    # as a second link. The url field stays trusted.
    line = f"- [{escape_user(item['title'])}]({item['url']})"
    if item.get("also_merged"):
        line += " → merged, too"
    return line


def _add_repo_section(
    report_lines: list[str],
    repos: list[tuple[str, RepoActivity]],
    labels: dict,
) -> None:
    """Render a group of repositories, omitting empty categories."""
    for repo_name, activity in sorted(repos, key=lambda item: item[0]):
        report_lines.append(f"\n#### {repo_name}")
        for category in ACTIVITY_CATEGORIES:
            items = activity[category]
            if not items:
                continue
            title = labels.get(category, category.replace("_", " ").title())
            report_lines.append(f"\n##### {title}")
            for item in items:
                report_lines.append(_format_item(item))


def generate_report(provider_data: dict[str, dict[str, RepoActivity]]) -> str:
    """
    Generates a Markdown report from the collected activity data.

    Empty categories, repositories with no activity, and providers with no
    active repositories are omitted entirely.
    """
    report_lines = ["# Git activity report"]

    for provider_name, repos in provider_data.items():
        # Keep only repositories that have at least one activity item.
        active_repos = {
            name: activity for name, activity in repos.items() if has_activity(activity)
        }
        if not active_repos:
            continue

        display_name = PROVIDER_DISPLAY_NAMES.get(provider_name.lower(), provider_name.title())
        report_lines.append(f"\n## {display_name}")

        labels = PROVIDER_LABELS.get(provider_name.lower(), {})

        public_repos = [
            (name, activity)
            for name, activity in active_repos.items()
            if activity["visibility"] == "public"
        ]
        private_repos = [
            (name, activity)
            for name, activity in active_repos.items()
            if activity["visibility"] != "public"
        ]

        if public_repos and private_repos:
            report_lines.append("\n### Public repositories")
            _add_repo_section(report_lines, public_repos, labels)
            report_lines.append("\n### Private repositories")
            _add_repo_section(report_lines, private_repos, labels)
        else:
            _add_repo_section(report_lines, public_repos or private_repos, labels)

    return "\n".join(report_lines)
