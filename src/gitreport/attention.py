"""Merge provider attention results with state; render the digest."""

import copy
import re
from datetime import UTC, datetime, timedelta

from .providers.base import unescape_user
from .state import AttentionState, ItemState

MAX_REASONS = 10
BUCKETS = ("today", "week", "month", "older")
BUCKET_TITLES = {"today": "Today", "week": "Last 7 days", "month": "Last 30 days", "older": "Older"}

# Type badges for the report model, derived from item kinds (no provider
# changes). Precedence: CI > PR review > thread > issue > bug > MP > mention >
# comment > stale; unknown kinds render the muted "item" badge, last.
BADGE_BY_KIND = {
    "ci_failure": ("CI", "ci"),
    "review_requested": ("PR review", "pr"),
    "thread_unresolved": ("thread", "thread"),
    "issue_assigned": ("issue", "issue"),
    "lp_bug_activity": ("bug", "bug"),
    "lp_mp_comment": ("MP", "mp"),
    "lp_mp_needs_review": ("MP review", "mp"),
    "stale_pr": ("stale", "stale"),
    "mention": ("mention", "mention"),
    "comment": ("comment", "comment"),
}
BADGE_PRECEDENCE = [
    "ci",
    "pr",
    "thread",
    "issue",
    "bug",
    "mp",
    "mention",
    "comment",
    "stale",
    "item",
]

# Launchpad kinds whose queries are time-windowed by `since`: falling out of
# the window is normal ageing, not a leave path, so these records are never
# resolved by absence — only via proven resolved_ids.
# Open windowed-only LP records persist by design: quiet subscribed-bug/
# MP-comment items stay in the inbox until acked or proven closed (spec:
# falling out of a modified_since window is not a leave path).
WINDOWED_KINDS = frozenset({"lp_bug_activity", "lp_mp_comment"})

# A URL is rendered as a Markdown link only when it is http(s) and free of
# characters that would break the [title](url) syntax or smuggle markup.
_URL_UNSAFE = re.compile(r"[\s()<>\"'`]")
_EPOCH = datetime.min.replace(tzinfo=UTC)


def _safe_url(url: str) -> str | None:
    """The URL when safe to render as a [title](url) link, else None.

    A URL is safe only when it is http(s) and free of characters that would
    break the Markdown link syntax or smuggle markup (_URL_UNSAFE). The
    single guard both renderers and the report model go through.
    """
    if url.startswith(("http://", "https://")) and not _URL_UNSAFE.search(url):
        return url
    return None


def badges_for(kinds: list[str], provider: str | None) -> list[tuple[str, str]]:
    """Distinct type badges in precedence order for an item's kinds.

    issue_assigned renders the bug badge under the Launchpad provider
    taxonomy; unknown kinds fall back to the muted item badge, ordered last.
    """
    out: list[tuple[str, str]] = []
    for kind in kinds or []:
        if kind == "issue_assigned" and provider == "launchpad":
            badge = ("bug", "bug")
        else:
            badge = BADGE_BY_KIND.get(kind, ("item", "item"))
        if badge not in out:
            out.append(badge)
    out.sort(key=lambda b: BADGE_PRECEDENCE.index(b[1]))
    return out


def humanize_age(ts: str | None, now: datetime) -> str:
    """Humanized age as of `now`: today/yesterday (local calendar dates),
    then the largest unit whose floor is >= 1 (day/week/month below a year),
    then years + remainder months (zero remainder dropped). Unknown/future
    handled per spec."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)  # naive `now` is treated as UTC
    dt = _parse(ts)
    if dt is None:
        return "unknown age"
    if dt > now:
        return "just now"
    local_dt, local_now = dt.astimezone(), now.astimezone()
    day_delta = (local_now.date() - local_dt.date()).days
    if day_delta <= 0:
        return "today"
    if day_delta == 1:
        return "yesterday"
    days = (local_now - local_dt).total_seconds() / 86400
    if days < 7:
        n = int(days)
        unit = "day"
    elif days < 30.44:
        n = int(days // 7)
        unit = "week"
    elif days < 365:
        n = int(days // 30.44)
        unit = "month"
    else:
        years = int(days // 365)
        months = int((days - years * 365) // 30.44)
        y = f"{years} year{'' if years == 1 else 's'}"
        if months == 0:
            return f"{y} ago"
        return f"{y}, {months} month{'' if months == 1 else 's'} ago"
    return f"{n} {unit}{'' if n == 1 else 's'} ago"


def _parse(ts: str | None) -> datetime | None:
    """Parse an ISO timestamp; naive values are treated as UTC.

    None and unparseable input return None rather than raising: stored state
    must never abort a merge or report over one malformed field.
    """
    if ts is None:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def age_bucket(last_updated: str | None, now: datetime) -> str:
    """Bucket for a still-open item's last_updated, evaluated in order, first
    match wins, all boundaries inclusive: today = same local calendar date as
    `now`; week = >= now - 7d; month = >= now - 30d; else older. Unparseable
    input buckets as older (defensive; sorted last)."""
    dt = _parse(last_updated)
    if dt is None:
        return "older"
    local_dt, local_now = dt.astimezone(), now.astimezone()
    if local_dt.date() == local_now.date():
        return "today"
    if local_dt >= local_now - timedelta(hours=168):
        return "week"
    if local_dt >= local_now - timedelta(hours=720):
        return "month"
    return "older"


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
    """Pure: apply enters/reopens/resolutions; return the new state.

    Retention pruning is deliberately NOT part of the merge: `digest` calls
    `state.prune` on the merged state under the lock (single prune site; the
    `attention` dry-run view must not prune either).
    """
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
        # R4: reasons reflect THIS run's fetch — replace, never grow. Stale
        # values from earlier runs (and the append-then-cap failure mode that
        # dropped fresh reasons once 10 accumulated) must not persist once
        # the source reports differently.
        r["reasons"] = list(m["reasons"])[:MAX_REASONS]
        # The reopen rule covers records that left the inbox as acked OR
        # auto-resolved: capture BEFORE the resolution flip below, or the
        # just-resolved case would be unreachable in the new-event branch.
        was_resolved = r.get("status") == "resolved"
        was_acked = r.get("status") == "acked"
        r["resolved_at"] = None  # a source reports it again
        if m.get("thread_url"):
            r["thread_url"] = m["thread_url"]
        # An auto-resolved record whose source reports it again is simply
        # open again: the suppression ended. With a genuinely new event it
        # is also re-opened work (reopen bump below); without one it is a
        # silent re-show, not a reopen.
        if was_resolved:
            r["status"] = "open"
        if new_event:
            r["repo"], r["title"], r["url"] = m["repo"], m["title"], m["url"]
            # The latest event advances freshness for every record, pinned
            # included; pinned only keeps status/reopen_count untouched below.
            r["last_updated"] = m["updated_at"]
            if r.get("pinned"):
                pass  # pinned: refresh display only; stays open
            elif was_acked or was_resolved:
                r.update(
                    status="open",
                    acked=False,
                    acked_at=None,
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


def _recency_sort_key(ts: str | None) -> float:
    """Ascending sort key for a last_updated string: newest item first.

    Negated epoch seconds (a newer timestamp sorts smaller); unparseable
    input returns +inf so it always sorts last. Pair with a stable pre-sort
    by id for the id-ascending tie-break.
    """
    dt = _parse(ts)
    if dt is None:
        return float("inf")
    return -dt.timestamp()


def _item_entry(mid: str, r: ItemState, now: datetime, bucket: str) -> dict:
    """One attention item for the report model.

    The model carries plain text: title/reasons pass through unescape_user
    (state stores them markdown-escaped; escaping happens only at render
    time). url is scheme-guarded here — an unsafe URL degrades to "" so no
    renderer can link it by accident.
    """
    return {
        "id": mid,
        "repo": r.repo or "",
        "title": unescape_user(r.title or ""),
        "url": _safe_url(r.url or "") or "",
        "reasons": [unescape_user(reason) for reason in r.reasons],
        "kinds": list(r.kinds),
        "type_badges": badges_for(list(r.kinds), r.provider),
        "age": humanize_age(r.last_updated, now),
        "age_bucket": bucket,
        "last_updated": r.last_updated or "",
        "reopened": r.reopen_count > 0,
    }


def _repo_groups(items: list[dict]) -> list[dict]:
    """Group model items by repo, groups ordered by newest item descending
    (tie: repo name ascending). Items must already be newest-first, so each
    group's first item is its newest."""
    by_repo: dict[str, list[dict]] = {}
    for entry in items:
        by_repo.setdefault(entry["repo"], []).append(entry)

    def _group_order(pair: tuple[str, list[dict]]) -> tuple[float, str]:
        repo, entries = pair
        return (_recency_sort_key(entries[0]["last_updated"]), repo)

    return [
        {"repo": repo, "items": entries}
        for repo, entries in sorted(by_repo.items(), key=_group_order)
    ]


def report_model(
    state: AttentionState,
    *,
    generated_at: str,
    coverage_start: str,
    first_run: bool,
    stale_providers: list[str],
    activity_data: dict | None = None,
) -> dict:
    """Build the plain-JSON report model consumed by both renderers.

    New tier: every open item when `first_run`; otherwise first_seen after
    last_reviewed — an unparseable last_reviewed counts as never-reviewed
    (all New) and an unparseable first_seen never promotes into New, the
    same convention as build_report. Still-open items bucket by age. Empty
    tiers, buckets and repo groups are omitted. `activity` is filled from
    `activity_data` (the activity model lands in a later task); a placeholder
    keeps the model shape stable until then.
    """
    end_dt = _parse(generated_at)
    # An unparseable generated_at is a programming error; degrade the report
    # clock to a live "now" (datetime.min would overflow once localised to a
    # negative-offset timezone) — same policy as render_digest_markdown.
    now = end_dt if end_dt is not None else datetime.now(UTC)
    start_dt = _parse(coverage_start)
    if start_dt is None or end_dt is None:
        days = 1  # defensive: unparseable coverage bounds degrade to 1 day
    else:
        days = max(1, (end_dt - start_dt).days)

    last_reviewed = _parse(state.last_reviewed) if state.last_reviewed else None
    open_pairs = [(mid, r) for mid, r in state.items.items() if r.status == "open"]
    # Newest first, ties by id ascending, unparseable last: stable two-pass
    # sort (id ascending first, then recency).
    open_pairs.sort(key=lambda pair: pair[0])
    open_pairs.sort(key=lambda pair: _recency_sort_key(pair[1].last_updated))

    new_items: list[dict] = []
    still_open: list[dict] = []
    for mid, r in open_pairs:
        first_seen = _parse(r.first_seen or "")
        is_new = (
            first_run
            or last_reviewed is None
            or (first_seen is not None and first_seen > last_reviewed)
        )
        bucket = "new" if is_new else age_bucket(r.last_updated, now)
        entry = _item_entry(mid, r, now, bucket)
        (new_items if is_new else still_open).append(entry)

    buckets = []
    for key in BUCKETS:
        entries = [e for e in still_open if e["age_bucket"] == key]
        if not entries:
            continue  # empty buckets are omitted
        buckets.append({"key": key, "label": BUCKET_TITLES[key], "groups": _repo_groups(entries)})

    attention: dict = {"buckets": buckets}
    new_groups = _repo_groups(new_items)
    if new_groups:
        attention = {"new": {"groups": new_groups}, "buckets": buckets}

    open_entries = new_items + still_open
    kpi = {
        "new": len(new_items),
        "still_open": len(still_open),
        "assigned": sum(1 for e in open_entries if "issue_assigned" in e["kinds"]),
        "ci_failing": sum(1 for e in open_entries if "ci_failure" in e["kinds"]),
        "stale_prs": sum(1 for e in open_entries if "stale_pr" in e["kinds"]),
    }

    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "coverage": {
            "start": coverage_start,
            "end": generated_at,
            "days": days,
            "first_run": first_run,
        },
        "status": {"reviewed": False, "reviewed_at": None},
        "stale_providers": list(stale_providers),
        "kpi": kpi,
        "attention": attention,
        "activity": (
            activity_data if activity_data is not None else {"markdown": "", "providers": []}
        ),
    }


def _fmt_local(ts: str) -> str:
    dt = _parse(ts)
    if dt is None:
        return "?"
    dt = dt.astimezone()
    return f"{dt:%a} {dt.day} {dt:%b}"


def _entry_line(entry: dict) -> str:
    title = entry.get("title") or entry["id"]
    safe_url = _safe_url(entry.get("url", ""))
    if safe_url:
        line = f"- [{title}]({safe_url})"
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
