from gitreport.providers.base import AttentionItem, escape_user, is_excluded


def test_escape_user_neutralises_html_and_markdown():
    text = "Bug <b>bold</b> [x](javascript:evil) *em_ ph* #tag"
    escaped = escape_user(text)
    assert "<b>" not in escaped
    # Every Markdown metacharacter is backslash-escaped so it renders literally.
    assert "[x]" not in escaped
    assert "*em_" not in escaped


def test_escape_user_plain_text_unchanged():
    assert escape_user("Fix login crash") == "Fix login crash"


def test_is_excluded_globs():
    assert is_excluded("me/fork-x", ["me/fork-*"])
    assert is_excluded("me/dotfiles", ["me/*"])
    assert not is_excluded("org/repo", ["me/*"])
    assert not is_excluded("org/repo", None)
