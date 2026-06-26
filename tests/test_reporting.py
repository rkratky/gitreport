import pytest
from src.gitreport.reporting import generate_report
from src.gitreport.providers.base import RepoActivity

def test_generate_report_single_provider():
    """Test report generation with a single provider."""
    provider_data = {
        "github": {
            "repo1-public": {
                "prs_submitted": [{"title": "Fix bug", "url": "http://a.com/1"}],
                "prs_reviewed": [], "issues_created": [], "issues_closed": [],
                "visibility": "public",
            },
            "repo2-private": {
                "issues_created": [{"title": "New feature", "url": "http://a.com/2"}],
                "prs_submitted": [], "prs_reviewed": [], "issues_closed": [],
                "visibility": "private",
            }
        }
    }
    report = generate_report(provider_data)
    assert "# Git Activity Report" in report
    assert "## Github" in report
    assert "### Public Repositories" in report
    assert "### repo1-public" in report
    assert "### Private Repositories" in report
    assert "### repo2-private" in report
    assert "- [Fix bug](http://a.com/1)" in report
    assert "- [New feature](http://a.com/2)" in report

def test_generate_report_multiple_providers():
    """Test report generation with multiple providers."""
    provider_data = {
        "github": {
            "gh-repo-priv": {
                "prs_submitted": [{"title": "GH PR", "url": "http://gh.com/1"}],
                "prs_reviewed": [], "issues_created": [], "issues_closed": [],
                "visibility": "private",
            }
        },
        "launchpad": {
            "lp-proj-pub": {
                "issues_created": [{"title": "LP Bug", "url": "http://lp.com/1"}],
                "prs_submitted": [], "prs_reviewed": [], "issues_closed": [],
                "visibility": "public",
            }
        },
    }
    report = generate_report(provider_data)
    assert "## Github" in report
    assert "### gh-repo-priv" in report
    assert "## Launchpad" in report
    assert "### lp-proj-pub" in report

def test_generate_report_no_activity():
    """Test report generation when there is no activity."""
    provider_data = {"github": {"repo1": {
        "prs_submitted": [], "prs_reviewed": [], "issues_created": [], "issues_closed": [],
        "visibility": "public",
    }}}
    report = generate_report(provider_data)
    assert "### repo1" in report
    assert "No activity in the reporting period." in report

def test_generate_report_empty_data():
    """Test report generation with empty input data."""
    report = generate_report({})
    assert report == "# Git Activity Report"
