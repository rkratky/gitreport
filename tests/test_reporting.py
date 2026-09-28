from gitreport.providers.base import empty_repo_activity
from gitreport.reporting import generate_report


def _repo(visibility="public", **items):
    activity = empty_repo_activity(visibility)
    activity.update(items)
    return activity


def test_generate_report_single_provider():
    """Test report generation with a single provider."""
    provider_data = {
        "github": {
            "repo1-public": _repo(
                "public",
                prs_submitted=[{"title": "Fix bug", "url": "http://a.com/1"}],
            ),
            "repo2-private": _repo(
                "private",
                issues_created=[{"title": "New feature", "url": "http://a.com/2"}],
            ),
        }
    }
    report = generate_report(provider_data)
    assert "# Git activity report" in report
    assert "## GitHub" in report
    assert "### Public repositories" in report
    assert "#### repo1-public" in report
    assert "### Private repositories" in report
    assert "#### repo2-private" in report
    assert "- [Fix bug](http://a.com/1)" in report
    assert "- [New feature](http://a.com/2)" in report


def test_generate_report_multiple_providers():
    """Test report generation with multiple providers."""
    provider_data = {
        "github": {
            "gh-repo-priv": _repo(
                "private",
                prs_submitted=[{"title": "GH PR", "url": "http://gh.com/1"}],
            )
        },
        "launchpad": {
            "lp-proj-pub": _repo(
                "public",
                issues_created=[{"title": "LP Bug", "url": "http://lp.com/1"}],
            )
        },
    }
    report = generate_report(provider_data)
    assert "## GitHub" in report
    assert "#### gh-repo-priv" in report
    assert "## Launchpad" in report
    assert "#### lp-proj-pub" in report


def test_generate_report_omits_empty_categories_repos_and_providers():
    """Empty categories, repos and providers are omitted entirely."""
    provider_data = {
        "github": {
            "active-repo": _repo(
                "public",
                prs_submitted=[{"title": "Real PR", "url": "http://x/1"}],
            ),
            "empty-repo": _repo("public"),
        },
        "launchpad": {
            "empty-proj": _repo("public"),
        },
    }
    report = generate_report(provider_data)

    # Active content present.
    assert "#### active-repo" in report
    assert "##### PRs submitted" in report
    # No placeholder text anymore.
    assert "No activity" not in report
    # Empty repo omitted.
    assert "empty-repo" not in report
    # Empty categories omitted (only submitted was populated).
    assert "PRs reviewed" not in report
    assert "Issues created" not in report
    # Provider with only empty repos omitted entirely.
    assert "## Launchpad" not in report


def test_generate_report_merged_annotation():
    """A reviewed-and-also-merged item is annotated inline, not duplicated."""
    provider_data = {
        "github": {
            "repo": _repo(
                "public",
                prs_reviewed=[
                    {
                        "title": "Reviewed+Merged",
                        "url": "http://x/9",
                        "also_merged": True,
                    }
                ],
            )
        }
    }
    report = generate_report(provider_data)
    # Titles are escape_user-guarded (XT-01): "+" is markdown-escaped.
    assert "- [Reviewed\\+Merged](http://x/9) → merged, too" in report
    # It must not appear under a separate merged section.
    assert "PRs merged" not in report


def test_generate_report_separate_merged_category():
    """A merged-but-not-reviewed item appears under the merged category."""
    provider_data = {
        "github": {
            "repo": _repo(
                "public",
                prs_merged=[{"title": "Merged only", "url": "http://x/10"}],
            )
        }
    }
    report = generate_report(provider_data)
    assert "##### PRs merged" in report
    assert "- [Merged only](http://x/10)" in report


def test_generate_report_empty_data():
    """Test report generation with empty input data."""
    report = generate_report({})
    assert report == "# Git activity report"


def test_generate_report_escapes_hostile_title():
    """A hostile title is escaped into literal text, not a second link (XT-01)."""
    provider_data = {
        "github": {
            "repo": _repo(
                "public",
                prs_submitted=[{"title": "see [x](https://evil)", "url": "http://x/1"}],
            )
        }
    }
    report = generate_report(provider_data)
    # Title renders as escaped text...
    assert "\\[x\\]" in report
    # ...and the line keeps exactly one link target (the trusted url).
    item_line = next(line for line in report.splitlines() if "http://x/1" in line)
    assert item_line.count("](") == 1
