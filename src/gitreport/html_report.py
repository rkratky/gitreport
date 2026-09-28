"""Self-contained HTML digest rendering from the cached Markdown.

Known limitation: every raw ``<`` is rewritten to ``&lt;`` before conversion,
so escaped-source digests cannot use Markdown autolinks (``<https://…>``) or
code spans containing ``<`` or ``&`` — those render literally. Accepted: the
digest format never emits those constructs.
"""

import html
import re

import markdown

_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 52rem; margin: 2rem auto;
       padding: 0 1rem; color: #1a1a1a; line-height: 1.5; }}
h1 {{ font-size: 1.4rem; }} h2 {{ font-size: 1.15rem; margin-top: 2rem; }}
h3 {{ font-size: 1rem; margin-top: 1.2rem; }}
code {{ background: #f2f2f2; padding: 0.1em 0.3em; border-radius: 3px; }}
blockquote {{ border-left: 3px solid #d0a000; margin: 0; padding: 0.2rem 1rem;
              background: #fff9e6; }}
a {{ color: #0645ad; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""

_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
# markdown always serialises attributes double-quoted. Only absolute http(s)
# URLs may be linked: attention entries are guarded by attention.py's
# _URL_UNSAFE + scheme check, activity titles are escape_user-escaped in
# reporting.py, so this is a second line of defence, not the primary guard.
_UNSAFE_HREF_RE = re.compile(r'href="(?!(?:https?)://)[^"]*"', re.IGNORECASE)
# markdown renders `[a](url "title")` with a title="..." attribute; the title
# text is user-supplied and must never sit in an attribute value. The strip is
# scoped to attributes inside <a> tags only, so literal `title="..."` in prose
# survives rendering.
_LINK_TITLE_ATTR_RE = re.compile(r'(<a\b[^>]*?)\s+title="[^"]*"')


def strip_front_matter(md_text: str) -> tuple[dict, str]:
    """Split `---`-fenced front matter from the Markdown body."""
    # The closing fence must be line-anchored: a front-matter VALUE may
    # itself contain `---` (e.g. `a: x---y`) and must not terminate the
    # block mid-line.
    match = re.match(r"\A---\r?\n(?:(.*?)\r?\n)?---\r?\n", md_text, re.DOTALL)
    if not match:
        return {}, md_text
    # BUG-R2-01: with empty front matter the optional meta group does not
    # participate in the match, so group(1) is None — treat it as "".
    meta: dict = {}
    for line in (match.group(1) or "").splitlines():
        key, _, value = line.partition(":")
        if key and value:
            meta[key.strip()] = value.strip()
    return meta, md_text[match.end() :]


def replace_status_line(md_text: str, new_status: str) -> str:
    """Replace the single `Status:` sentinel line in the cached Markdown."""
    # A function replacement keeps new_status literal (backslashes in the
    # status are data, not re.sub replacement escapes); [^\r\n]* stops before
    # the CR of a CRLF terminator so line endings are preserved.
    return re.sub(
        r"^Status: [^\r\n]*",
        lambda _m: new_status,
        md_text,
        count=1,
        flags=re.MULTILINE,
    )


def render_html(md_text: str, title: str = "GitReport digest") -> str:
    """Convert the digest Markdown to a self-contained HTML document.

    Hardening beyond the plain markdown pass, verified against markdown 3.11
    (whose default parser passes raw inline HTML and javascript: URLs through
    verbatim):

    - Every raw ``<`` in the body is rewritten to ``&lt;`` before conversion.
      Attention titles/reasons are escape_user-guarded upstream and activity
      titles are escape_user-escaped in reporting.py, so raw HTML
      (script/svg onload/iframe/...) must never reach the parser. Only ``<``
      is rewritten: ``>`` stays intact so blockquote syntax keeps working.
      Trade-off: a literal ``<`` inside a code span would double-escape on
      display; the digest format emits no such span (the only backticked text
      is the fixed `gitreport read` sentinel).
    - markdown-generated ``<img>`` tags are stripped: the digest must stay
      self-contained (no external references), and no digest section emits
      images intentionally.
    - ``href`` values that are not absolute http(s) URLs are replaced with
      ``#`` (e.g. the javascript: link in the hostile-title test).
    - markdown-generated link ``title="..."`` attributes are stripped:
      user-supplied link titles must never land in an attribute value.
    - The ``title`` argument is HTML-escaped before it enters the template.
    """
    _meta, body = strip_front_matter(md_text)
    safe_body = body.replace("<", "&lt;")
    rendered = markdown.markdown(safe_body, output_format="html")
    rendered = _IMG_TAG_RE.sub("", rendered)
    rendered = _UNSAFE_HREF_RE.sub('href="#"', rendered)
    rendered = _LINK_TITLE_ATTR_RE.sub(r"\1", rendered)
    return _TEMPLATE.format(title=html.escape(title), body=rendered)
