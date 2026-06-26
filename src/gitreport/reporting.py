from typing import Dict, List
from .providers.base import RepoActivity

PROVIDER_LABELS = {
    "github": {
        "prs_submitted": "PRs Submitted",
        "prs_reviewed": "PRs Reviewed",
        "issues_created": "Issues Created",
        "issues_closed": "Issues Closed",
    },
    "launchpad": {
        "prs_submitted": "Merge Proposals Submitted",
        "prs_reviewed": "Merge Proposals Reviewed",
        "issues_created": "Bugs Created",
        "issues_closed": "Bugs Closed",
    },
}

def _add_repo_section(report_lines: List[str], repos: list, labels: dict):
    """Helper to add a section of repos to the report."""
    for repo_name, activity in sorted(repos, key=lambda item: item[0]):
        report_lines.append(f"\n#### {repo_name}")
        
        activity_found = False
        # The visibility key is for sorting, not reporting
        for activity_type, items in activity.items():
            if activity_type == 'visibility' or not items:
                continue
            
            activity_found = True
            title = labels.get(activity_type, activity_type.replace('_', ' ').title())
            # Add extra newline for readability
            report_lines.append(f"\n##### {title}")
            for item in items:
                report_lines.append(f"- [{item['title']}]({item['url']})")

        if not activity_found:
            report_lines.append("\nNo activity in the reporting period.")

def generate_report(provider_data: Dict[str, Dict[str, RepoActivity]]) -> str:
    """
    Generates a Markdown report from the collected activity data.

    Args:
        provider_data: A dictionary where keys are provider names and values
                       are dictionaries of repository activity.

    Returns:
        A Markdown formatted string representing the activity report.
    """
    report_lines = ["# Git Activity Report"]

    for provider_name, repos in provider_data.items():
        if not repos:
            continue

        report_lines.append(f"\n## {provider_name.title()}")
        
        labels = PROVIDER_LABELS.get(provider_name.lower(), {})

        public_repos = []
        private_repos = []
        for repo_name, activity in repos.items():
            if activity['visibility'] == 'public':
                public_repos.append((repo_name, activity))
            else:
                private_repos.append((repo_name, activity))

        if public_repos and private_repos:
            report_lines.append("\n### Public Repositories")
            _add_repo_section(report_lines, public_repos, labels)
            report_lines.append("\n### Private Repositories")
            _add_repo_section(report_lines, private_repos, labels)
        elif public_repos:
            _add_repo_section(report_lines, public_repos, labels)
        elif private_repos:
            _add_repo_section(report_lines, private_repos, labels)

    return "\n".join(report_lines)
