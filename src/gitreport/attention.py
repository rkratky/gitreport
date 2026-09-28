"""Merge provider attention results with state; render the digest."""

import copy
import re
from datetime import UTC, datetime, timedelta

PRUNE_AFTER = timedelta(days=30)
MAX_REASONS = 10
BUCKETS = ("today", "week", "older")
BUCKET_TITLES = {"today": "Today", "week": "Last 7 days", "older": "Older than 7 days"}

# Launchpad kinds whose queries are time-windowed by `since`: falling out of
# the window is normal ageing, not a leave path, so these records are never
# resolved by absence — only via proven resolved_ids.
WINDOWED_KINDS = frozenset({"lp_bug_activity", "lp_mp_comment"})

# A URL is rendered as a Markdown link only when it is http(s) and free of
# characters that would break the [title](url) syntax or smuggle markup.
_URL_UNSAFE = re.compile(r"[\s()<>]")
_EPOCH = datetime.min.replace(tzinfo=UTC)


def _parse(ts: str) -> datetime | None:
    """Parse an ISO timestamp; naive values are treated as UTC.

    Unparseable input returns None rather than raising: stored state must
    never abort a merge or report over one malformed field.
    """
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _is_later(a: str | None, b: str | None) -> bool:
    """True when a is strictly after b. None / unparseable is never 'later'."""
    if a is None:
        return False
    if b is None:
        return True
    a_dt, b_dt = _parse(a), _parse(b)
    if a_dt is None:
        return False
    if b_dt is None:
        return True
    return a_dt > b_dt


def dedupe(results: dict[str, dict]) -> dict[str, dict]:
    """Union per-origin provider items into merged items keyed by id."""
    merged: dict[str, dict] = {}
    for provider, result in results.items():
        for item in result.get("items", []):
            mid = item["id"]
            m = merged.setdefault(
                mid,
                {
                    "id": mid,
                    "provider": item.get("provider", provider),
                    "origins": set(),
                    "kinds": set(),
                    "reasons": [],
                    "repo": item.get("repo", ""),
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "updated_at": item.get("updated_at"),
                    "thread_url": item.get("thread_url"),
                },
            )
            if item.get("origin"):
                m["origins"].add(item["origin"])
            if item.get("kind"):
                m["kinds"].add(item["kind"])
            reason = item.get("reason")
            if reason and reason not in m["reasons"]:
                m["reasons"].append(reason)
            if item.get("thread_url"):
                m["thread_url"] = item["thread_url"]
            if _is_later(item.get("updated_at"), m["updated_at"]):
                m["updated_at"] = item["updated_at"]
                m["repo"] = item.get("repo", m["repo"])
                m["title"] = item.get("title", m["title"])
                m["url"] = item.get("url", m["url"])
    for m in merged.values():
        m["origins"] = sorted(m["origins"])
        m["kinds"] = sorted(m["kinds"])
        m["reasons"] = m["reasons"][:MAX_REASONS]
    return merged


def merge_into_state(
    state: dict,
    merged_items: dict[str, dict],
    ok_providers: set[str],
    generated_at: str,
    resolved_ids: set[str] | frozenset[str] = frozenset(),
) -> dict:
    """Pure: apply enters/reopens/resolutions/prune; return the new state."""
    st = copy.deepcopy(state)
    items = st["items"]

    # 1. Enter / reopen / refresh, from the fetched items.
    for mid, m in merged_items.items():
        r = items.get(mid)
        if r is None:
            items[mid] = {
                "status": "open",
                "origins": list(m["origins"]),
                "kinds": list(m["kinds"]),
                "reasons": list(m["reasons"]),
                "repo": m["repo"],
                "title": m["title"],
                "url": m["url"],
                "first_seen": generated_at,
                "last_updated": m["updated_at"] or generated_at,
                "acked": False,
                "acked_at": None,
                "resolved_at": None,
                "pinned": False,
                "reopen_count": 0,
                "thread_url": m.get("thread_url"),
                "provider": m["provider"],
            }
            continue
        new_event = _is_later(m["updated_at"], r.get("last_updated"))
        for key in ("origins", "kinds"):
            r[key] = sorted(set(r[key]) | set(m[key]))
        for reason in m["reasons"]:
            if reason not in r["reasons"]:
                r["reasons"].append(reason)
        r["reasons"] = r["reasons"][:MAX_REASONS]
        r["resolved_at"] = None  # a source reports it again
        if m.get("thread_url"):
            r["thread_url"] = m["thread_url"]
        if new_event:
            r["repo"], r["title"], r["url"] = m["repo"], m["title"], m["url"]
            # The latest event advances freshness for every record, pinned
            # included; pinned only keeps status/reopen_count untouched below.
            r["last_updated"] = m["updated_at"]
            if r.get("pinned"):
                pass  # pinned: refresh display only; stays open
            elif r["status"] in ("acked", "resolved"):
                r.update(
                    status="open",
                    acked=False,
                    acked_at=None,
                    resolved_at=None,
                    reopen_count=r.get("reopen_count", 0) + 1,
                )

    # 2. Resolution pass — absence-based, only for fully-fetched providers.
    for mid, r in items.items():
        if r.get("pinned") or mid in merged_items:
            continue  # still reported, or pinned: never auto-resolve
        if r.get("provider") in ok_providers or mid in resolved_ids:
            kinds = set(r.get("kinds") or [])
            windowed_only = bool(kinds) and kinds <= WINDOWED_KINDS
            if windowed_only and mid not in resolved_ids:
                continue  # windowed LP kinds: absence is not a leave path
            if r["status"] == "open":
                r["status"] = "resolved"
                r["resolved_at"] = generated_at
            elif r["status"] in ("acked", "resolved") and r.get("resolved_at") is None:
                r["resolved_at"] = generated_at

    # 3. Retention prune.
    gen = _parse(generated_at)
    for mid in list(items):
        r = items[mid]
        resolved = _parse(r["resolved_at"]) if r.get("resolved_at") else None
        if r["status"] not in ("acked", "resolved") or resolved is None or gen is None:
            continue  # unparseable resolved_at: skip, never prune on bad data
        if (gen - resolved) >= PRUNE_AFTER:
            del items[mid]

    st["last_digest_run"] = generated_at
    return st


def build_report(state: dict, now: datetime) -> dict:
    """Split open items into New and Still open (today / week / older)."""
    last_reviewed = state.get("last_reviewed")
    new_items: list[dict] = []
    still_open: list[dict] = []
    for mid, r in sorted(state["items"].items()):
        if r.get("status") != "open":
            continue
        first_seen = r.get("first_seen") or ""
        fs = _parse(first_seen)
        lr = _parse(last_reviewed) if last_reviewed else None
        # An unparseable last_reviewed counts as never-reviewed (same as
        # unset, matching first-run semantics): every open item is New.
        # Conversely, an unparseable first_seen is treated as not-New — bad
        # stored data must not promote an item into the New bucket.
        is_new = lr is None or (fs is not None and fs > lr)
        entry = {
            "id": mid,
            "repo": r.get("repo", ""),
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "reasons": r.get("reasons", []),
            "last_updated": r.get("last_updated", ""),
            "reopen_count": r.get("reopen_count", 0),
        }
        (new_items if is_new else still_open).append(entry)

    buckets: dict[str, list[dict]] = {b: [] for b in BUCKETS}
    now_local = now.astimezone()
    for entry in still_open:
        lu = entry["last_updated"]
        when = _parse(lu) if lu else None
        when = when.astimezone() if when is not None else None
        # unparseable last_updated buckets as "older" (defensive).
        if when is not None and when.date() == now_local.date():
            bucket = "today"
        elif when is not None and when >= now_local - timedelta(days=7):
            bucket = "week"
        else:
            bucket = "older"
        buckets[bucket].append(entry)

    def _sort_key(e: dict) -> datetime:
        dt = _parse(e["last_updated"]) if e["last_updated"] else None
        return dt if dt is not None else _EPOCH

    new_items.sort(key=_sort_key, reverse=True)
    return {"new": new_items, **buckets}


def _fmt_local(ts: str) -> str:
    dt = _parse(ts)
    if dt is None:
        return "?"
    dt = dt.astimezone()
    return f"{dt:%a} {dt.day} {dt:%b}"


def _entry_line(entry: dict) -> str:
    title = entry.get("title") or entry["id"]
    url = entry.get("url", "")
    if url.startswith(("http://", "https://")) and not _URL_UNSAFE.search(url):
        line = f"- [{title}]({url})"
    else:
        line = f"- {title}"
    if entry.get("reopen_count"):
        line += " (re-opened)"
    for reason in entry.get("reasons", []):
        line += f"\n  - {reason}"
    return line


def render_attention_body(report: dict, stale_providers: list[str]) -> str:
    lines: list[str] = []
    if report["new"]:
        lines += ["### New since last review", ""]
        lines += [_entry_line(e) for e in report["new"]]
        lines.append("")
    for bucket in BUCKETS:
        entries = report[bucket]
        if not entries:
            continue
        lines += [f"### Still open — {BUCKET_TITLES[bucket]}", ""]
        lines += [_entry_line(e) for e in entries]
        lines.append("")
    if not report["new"] and not any(report[b] for b in BUCKETS):
        lines += ["_Nothing needs your attention._", ""]
    if stale_providers:
        names = ", ".join(sorted(stale_providers))
        lines += [
            f"Warning: these providers failed to fetch; their sections may be stale: {names}",
            "",
        ]
    return "\n".join(lines)


def render_attention_stdout(state: dict, stale_providers: list[str], now: datetime) -> str:
    """Dry-run attention view (no state machinery, no front matter)."""
    report = build_report(state, now)
    return "# Needs attention\n\n" + render_attention_body(report, stale_providers)


def render_digest_markdown(
    state: dict,
    activity_markdown: str,
    coverage_start: str,
    generated_at: str,
    stale_providers: list[str],
) -> str:
    start_dt = _parse(coverage_start)
    end_dt = _parse(generated_at)
    if start_dt is None or end_dt is None:
        days = 1  # defensive: unparseable coverage bounds degrade to 1 day
    else:
        days = max(1, (end_dt - start_dt).days)
    unit = "day" if days == 1 else "days"
    coverage = (
        f"Coverage: {_fmt_local(coverage_start)} – {_fmt_local(generated_at)} "
        f"({days} {unit} since last review)"
    )
    # An unparseable generated_at is a programming error; degrade the report
    # clock to a live "now" (datetime.min would overflow once localised to a
    # negative-offset timezone).
    report = build_report(state, end_dt if end_dt is not None else datetime.now(UTC))
    lines = [
        "---",
        f"generated_at: {generated_at}",
        f"coverage_start: {coverage_start}",
        "---",
        "",
        "# GitReport digest",
        "",
        coverage,
        "Status: NOT YET REVIEWED — run `gitreport read` after reviewing",
        "",
        "## Needs attention",
        "",
        render_attention_body(report, stale_providers),
        "## Recent activity",
        "",
        activity_markdown.strip() or "_No activity in the coverage window._",
        "",
    ]
    return "\n".join(lines)
