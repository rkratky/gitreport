from gitreport.providers.base import AttentionItem, escape_user, is_excluded, unescape_user


def test_escape_user_neutralises_html_and_markdown():
    text = "Bug <b>bold</b> [x](javascript:evil) *em_ ph* #tag"
    escaped = escape_user(text)
    assert "<b>" not in escaped
    # Every Markdown metacharacter is backslash-escaped so it renders literally.
    assert "[x]" not in escaped
    assert "*em_" not in escaped


def test_escape_user_plain_text_unchanged():
    assert escape_user("Fix login crash") == "Fix login crash"


def test_escape_user_single_backslash_doubling():
    # One backslash in must come out as exactly two (single-pass escaping).
    assert escape_user("a\\b") == "a\\\\b"


def test_escape_user_collapses_whitespace_and_block_syntax():
    escaped = escape_user("t\n\n===")
    assert "\n" not in escaped
    assert "===" not in escaped
    # A leading dash cannot survive as a list item.
    assert escape_user("- a") == "\\- a"


def test_escape_user_hostile_exact_output():
    # Exact output for a hostile input: collapse -> html.escape(quote=False)
    # -> single-pass backslash-escaping of Markdown metacharacters.
    assert escape_user("![a](b) \\ <x>") == "\\!\\[a\\]\\(b\\) \\\\ &lt;x&gt;"


def test_attention_item_construction():
    item: AttentionItem = {
        "id": "gh-1",
        "provider": "github",
        "kind": "mention",
        "origin": "notification",
        "title": escape_user("Fix <b>login</b>"),
        "url": "https://example.com/1",
        "reason": escape_user("you were mentioned"),
    }
    assert set(item) == {
        "id",
        "provider",
        "kind",
        "origin",
        "title",
        "url",
        "reason",
    }


def test_is_excluded_globs():
    assert is_excluded("me/fork-x", ["me/fork-*"])
    assert is_excluded("me/dotfiles", ["me/*"])
    assert not is_excluded("org/repo", ["me/*"])
    assert not is_excluded("org/repo", None)


def test_unescape_user_reverses_escape_user():
    from gitreport.providers.base import escape_user

    for raw in ["Fix login crash", "Bug & <b>bold</b> [x](y) *em_", "a\\b", "C:\\path"]:
        assert unescape_user(escape_user(raw)).replace("\\", "") == raw.replace("\\", "")


def test_unescape_user_strips_backslash_before_specials():
    assert unescape_user("fix\\_login \\- ok") == "fix_login - ok"


def test_unescape_user_plain_unchanged():
    assert unescape_user("plain text") == "plain text"
