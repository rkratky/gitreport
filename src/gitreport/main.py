import os
import re
import webbrowser
from datetime import UTC, datetime, timedelta
from pathlib import Path

import click
import dateparser
from github import BadCredentialsException

from .attention import (
    _parse,
    dedupe,
    merge_into_state,
    render_attention_stdout,
    render_digest_markdown,
)
from .config import load_config
from .html_report import render_html, replace_status_line, strip_front_matter
from .providers.base import GitProvider
from .providers.github import GitHubProvider
from .providers.launchpad import LaunchpadProvider
from .reporting import generate_report
from .state import (
    AttentionState,
    load_state,
    load_state_snapshot,
    load_state_with_rebuild_flag,
    prune,
    record_read,
    save_state,
    state_lock,
)

PROVIDER_MAP: dict[str, type[GitProvider]] = {
    "github": GitHubProvider,
    "launchpad": LaunchpadProvider,
}


def _load_config(config_path_str: Path | None):
    """load_config with the shared missing/invalid-config -> ClickException
    handling (same behaviour as `generate`'s own wrapping), so a bad config
    never escapes any command as a raw traceback."""
    try:
        return load_config(config_path_str)
    except (FileNotFoundError, ValueError) as e:
        raise click.ClickException(str(e)) from e


@click.group()
def cli():
    """A CLI tool to gather Git activity and generate a Markdown report."""
    pass


@cli.command()
@click.option(
    "--config",
    "config_path_str",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the configuration file.",
)
@click.option(
    "--start-date",
    type=str,
    default="1 week ago",
    help='The start date for the report (YYYY-MM-DD or natural language, e.g., "1 week ago").',
)
@click.option(
    "--end-date",
    type=str,
    default="today",
    help='The end date for the report (YYYY-MM-DD or natural language, e.g., "today").',
)
@click.option(
    "--fast",
    is_flag=True,
    default=False,
    help=(
        "Skip per-item verification API calls for speed. Faster on large "
        "windows, but slightly less accurate at the window boundaries and "
        "omits the GitHub 'merged' category (which cannot be derived from "
        "search alone)."
    ),
)
def generate(config_path_str: Path | None, start_date: str, end_date: str, fast: bool):
    """Generate the activity report."""
    try:
        # Parse dates
        start_dt = dateparser.parse(start_date)
        end_dt = dateparser.parse(end_date)
        if not start_dt or not end_dt:
            raise ValueError("Could not parse date strings. Please use a valid format.")

        # Ensure datetimes are timezone-aware (UTC) to allow comparison
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=UTC)
        else:
            start_dt = start_dt.astimezone(UTC)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=UTC)
        else:
            end_dt = end_dt.astimezone(UTC)

        config = load_config(config_path_str)
        click.echo("Configuration loaded successfully.", err=True)

        provider_data = {}
        for provider_name, provider_config in config.providers.items():
            if provider_name in PROVIDER_MAP:
                try:
                    ProviderClass = PROVIDER_MAP[provider_name]
                    token = (
                        provider_config.token.get_secret_value()
                        if provider_config.token is not None
                        else None
                    )
                    provider = ProviderClass(
                        username=provider_config.username,
                        token=token,
                    )
                    click.echo(f"Fetching data from {provider_name}...", err=True)
                    provider_data[provider_name] = provider.get_activity(
                        start_dt, end_dt, fast=fast
                    )
                except BadCredentialsException as e:
                    click.echo(
                        f"Error: Bad credentials for {provider_name}. "
                        "Please check your token in the configuration file.",
                        err=True,
                    )
                    raise click.ClickException(f"Bad credentials for {provider_name}.") from e
                except Exception as e:
                    # A provider may be temporarily unavailable (e.g. Launchpad
                    # returning HTTP 503). Fail gracefully: warn the user, skip
                    # this provider, and generate the report from whatever data
                    # was collected from the remaining providers.
                    click.echo(
                        f"Warning: could not fetch data from {provider_name} ({e}); "
                        "skipping this provider. The report will include only "
                        "data from providers that succeeded.",
                        err=True,
                    )
                    continue

        report = generate_report(provider_data)
        click.echo(report)

    except (FileNotFoundError, ValueError) as e:
        raise click.ClickException(str(e)) from e


def _build_providers(config) -> dict[str, GitProvider | None]:
    """Instantiate providers; name -> provider, or None when construction fails.

    A constructor failure (e.g. missing Launchpad credentials) must not abort
    the command: the None placeholder makes the fetch loops record a failed
    fetch, so the section is marked stale and no items resolve by that
    provider's absence.
    """
    providers: dict[str, GitProvider | None] = {}
    for name, pc in config.providers.items():
        cls = PROVIDER_MAP.get(name)
        if cls is None:
            continue
        token = pc.token.get_secret_value() if pc.token is not None else None
        try:
            providers[name] = cls(username=pc.username, token=token)
        except Exception as e:  # noqa: BLE001
            click.echo(
                f"Warning: could not initialise provider {name} ({e}); its section may be stale.",
                err=True,
            )
            providers[name] = None
    return providers


def _fetch_attention(providers, since, exclusions, state_items, stale_pr_days):
    """Fetch attention items from every provider; network, outside the lock.

    Returns (results, stale_providers). A provider failure — constructor or
    fetch — yields ok=False and the provider name in stale_providers.
    `exclusions` maps provider name -> list of glob patterns; each provider
    only receives its own patterns (BUG-01).
    """
    results = {}
    stale = []
    for name, provider in providers.items():
        if provider is None:
            fetch = {"ok": False, "items": [], "resolved_ids": [], "error": "not initialised"}
        else:
            try:
                fetch = provider.get_attention(
                    since,
                    exclusions=exclusions.get(name, []),
                    state_items=state_items,
                    stale_pr_days=stale_pr_days,
                )
            except Exception as e:  # noqa: BLE001
                fetch = {"ok": False, "items": [], "resolved_ids": [], "error": str(e)}
        results[name] = fetch
        if not fetch["ok"]:
            stale.append(name)
    return results, stale


def _fetch_activity(providers, start, end, fast=False):
    """Fetch activity from every provider; degrade on failure, never abort.

    Returns (provider_data: dict[str, dict[str, RepoActivity]], stale: list).
    A None provider (constructor failure, already warned by _build_providers)
    is treated as a failed fetch: no data, section marked stale. Bad
    credentials are a per-provider failure here too (warning + skip) — only
    the standalone `generate` command aborts on them (non-goal to change).
    """
    provider_data: dict = {}
    stale: list[str] = []
    for name, provider in providers.items():
        if provider is None:
            stale.append(name)
            continue
        try:
            provider_data[name] = provider.get_activity(start, end, fast=fast)
        except BadCredentialsException:
            click.echo(
                f"Warning: bad credentials for {name}; its activity is missing from this digest.",
                err=True,
            )
            stale.append(name)
        except Exception as e:  # noqa: BLE001
            click.echo(
                f"Warning: could not fetch activity from {name} ({e}); its section may be stale.",
                err=True,
            )
            stale.append(name)
    return provider_data, stale


@cli.command()
@click.option(
    "--config",
    "config_path_str",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
def attention(config_path_str):
    """Show what needs attention (dry run; does not mutate state)."""
    config = _load_config(config_path_str)
    att = config.attention
    state = load_state_snapshot(att.state_path)
    providers = _build_providers(config)
    state_items = {mid: rec.model_dump() for mid, rec in state.items.items()}
    results, stale = _fetch_attention(
        providers, _attention_since(state), att.exclusions, state_items, att.stale_pr_days
    )
    merged = dedupe(results)
    # Dry-run view: overlay merged items on the state without persisting.
    # ok_providers stays empty so nothing resolves by absence in the view.
    view = merge_into_state(
        {**state.model_dump(), "last_digest_run": None}, merged, set(), _now_iso()
    )
    click.echo(render_attention_stdout(view, stale, datetime.now(UTC)))


@cli.command()
@click.option(
    "--config",
    "config_path_str",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--open", "open_browser", is_flag=True, default=False)
def digest(config_path_str, open_browser):
    """Morning run: attention + activity; writes the digest; updates state."""
    config = _load_config(config_path_str)
    att = config.attention
    providers = _build_providers(config)

    # Snapshot for fetches (outside the lock; non-mutating read).
    pre_state = load_state_snapshot(att.state_path)
    generated_at = datetime.now(UTC).isoformat()
    since = _attention_since(pre_state)
    state_items = {mid: rec.model_dump() for mid, rec in pre_state.items.items()}
    results, stale = _fetch_attention(
        providers, since, att.exclusions, state_items, att.stale_pr_days
    )
    activity_data, activity_stale = _fetch_activity(providers, since, datetime.now(UTC))

    merged = dedupe(results)
    resolved_ids = set().union(*(set(r.get("resolved_ids", [])) for r in results.values()))

    with state_lock(att.state_path):
        # Reload under lock; load_state alone now round-trips the merge-layer
        # extras (ItemState carries thread_url/provider).
        state, rebuilt = load_state_with_rebuild_flag(att.state_path)
        if rebuilt:
            _recover_last_reviewed(state, att.digest_output)
        ok_providers = {name for name, fetch in results.items() if fetch["ok"]}
        new_state_obj = AttentionState.model_validate(
            merge_into_state(state.model_dump(), merged, ok_providers, generated_at, resolved_ids)
        )
        prune(new_state_obj, datetime.now(UTC))
        save_state(att.state_path, new_state_obj)

        # Digest files + symlinks (inside the lock), rendered from the PRUNED
        # state so the saved and rendered views cannot diverge.
        activity_md = generate_report(activity_data)
        coverage_start = since.isoformat()
        md_text = render_digest_markdown(
            new_state_obj.model_dump(),
            activity_md,
            coverage_start,
            generated_at,
            stale + activity_stale,
        )
        stem = Path(
            str(att.digest_output).replace(
                "YYYY-MM-DD", datetime.now().astimezone().strftime("%Y-%m-%d")
            )
        )
        stem.parent.mkdir(parents=True, exist_ok=True)
        _stem_file(stem, "md").write_text(md_text)
        if "html" in att.digest_formats:
            _stem_file(stem, "html").write_text(render_html(md_text))
        _refresh_symlinks(stem, att.digest_latest, att.digest_formats)

    click.echo(strip_front_matter(md_text)[1])
    if open_browser:
        _open_in_browser(_stem_file(stem, "html" if "html" in att.digest_formats else "md"))


@cli.command()
@click.option(
    "--config",
    "config_path_str",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--open", "open_browser", is_flag=True, default=False)
def read(config_path_str, open_browser):
    """Mark the newest digest(s) consumed and re-render their status line."""
    config = _load_config(config_path_str)
    att = config.attention
    now = datetime.now(UTC).isoformat()
    with state_lock(att.state_path):
        state, rebuilt = load_state_with_rebuild_flag(att.state_path)
        if rebuilt:
            _recover_last_reviewed(state, att.digest_output)
        previous = state.last_reviewed
        newest_ts, targets = _digests_generated_after(att.digest_output, previous)
        if newest_ts is None:
            click.echo("No digests to mark read.")
            return
        # The cursor only moves forward: a newest digest older than (or as old
        # as) the cursor — e.g. a recovered cursor, or digests re-generated
        # with an old date — must not drag last_reviewed backwards and re-cover
        # already-consumed events. Unparseable timestamps are never "later".
        newest_dt, previous_dt = _parse(newest_ts), _parse(previous)
        later = previous is None or (
            newest_dt is not None and previous_dt is not None and newest_dt > previous_dt
        )
        if not later:
            click.echo("Newest digest is older than your review cursor; not moving it back.")
            if rebuilt:
                # The rebuild and the recovered cursor must survive even
                # though the cursor is not advancing here (the corrupt file
                # was already backed up).
                save_state(att.state_path, state)
            return
        record_read(state, newest_ts, now)
        save_state(att.state_path, state)
        # Spec format, in local time (the stored cursor stays UTC ISO).
        reviewed_local = datetime.fromisoformat(now).astimezone()
        stamp = f"Status: Reviewed {reviewed_local:%a %-d %b %H:%M}"
        for path in targets:
            text = path.read_text()
            stamped = replace_status_line(text, stamp)
            path.write_text(stamped)
            if path.suffix == ".md" and "html" in att.digest_formats:
                _stem_file(path.with_suffix(""), "html").write_text(render_html(stamped))
    if open_browser:
        ext = "html" if "html" in att.digest_formats else "md"
        _open_in_browser(_stem_file(att.digest_latest, ext))


@cli.command(name="ack")
@click.argument("item_id", required=False)
@click.option(
    "--config",
    "config_path_str",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--list", "list_items", is_flag=True, default=False)
def ack(item_id, config_path_str, list_items):
    """Mark item(s) done. ID is an id or substring; --list browses."""
    config = _load_config(config_path_str)
    att = config.attention
    # Lock discipline: match + interactive prompt run on a non-mutating
    # snapshot OUTSIDE the lock, so a prompt never blocks a concurrent
    # digest/ack; each chosen id is re-checked under the lock before mutating.
    state = load_state_snapshot(att.state_path)
    open_items = {mid: r for mid, r in state.items.items() if r.status == "open"}
    if list_items or item_id is None:
        _print_items(open_items)
        return
    matches = _match_items(open_items, item_id)
    if not matches:
        raise click.ClickException(f"No open item matches {item_id!r}")
    if len(matches) > 1:
        chosen = _disambiguate(matches)
        if chosen is None:
            return
        matches = [chosen]
    now = datetime.now(UTC).isoformat()
    acked = 0
    thread_urls: list[str] = []
    with state_lock(att.state_path):
        state = load_state(att.state_path)
        for mid in matches:
            rec = state.items.get(mid)
            if rec is None or rec.status != "open":
                click.echo(f"Skipping {mid}: no longer open.", err=True)
                continue
            rec.status = "acked"
            rec.acked = True
            rec.acked_at = now
            rec.pinned = False
            if "notification" in rec.origins and rec.thread_url:
                thread_urls.append(rec.thread_url)  # patched below, outside the lock
            acked += 1
        save_state(att.state_path, state)
    for url in thread_urls:
        _mark_thread_read(config, url)  # best-effort; network stays outside the lock
    click.echo(f"Acked {acked} item(s).")


@cli.command()
@click.argument("item_id")
@click.option(
    "--config",
    "config_path_str",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
def unack(item_id, config_path_str):
    """Pull item(s) back into the inbox."""
    config = _load_config(config_path_str)
    att = config.attention
    # Same out-of-lock disambiguation flow as `ack`.
    state = load_state_snapshot(att.state_path)
    candidates = {mid: r for mid, r in state.items.items() if r.status in ("acked", "resolved")}
    matches = _match_items(candidates, item_id)
    if not matches:
        raise click.ClickException(f"No acked or resolved item matches {item_id!r}")
    if len(matches) > 1:
        chosen = _disambiguate(matches, verb="un-ack")
        if chosen is None:
            return
        matches = [chosen]
    reopened = 0
    with state_lock(att.state_path):
        state = load_state(att.state_path)
        for mid in matches:
            rec = state.items.get(mid)
            if rec is None or rec.status not in ("acked", "resolved"):
                click.echo(f"Skipping {mid}: no longer acked/resolved.", err=True)
                continue
            was_resolved = rec.status == "resolved"
            rec.status = "open"
            rec.acked = False
            rec.acked_at = None
            rec.resolved_at = None  # un-resolving clears the resolution stamp too
            rec.reopen_count += 1
            if was_resolved:
                rec.pinned = True  # was auto-resolved: pin so it cannot re-resolve
            reopened += 1
        save_state(att.state_path, state)
    click.echo(f"Un-acked {reopened} item(s).")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _attention_since(state: AttentionState) -> datetime:
    """Coverage-window start shared by `attention` and `digest`: the last
    review cursor, or now-24h on a first run / unparseable cursor."""
    since = _parse(state.last_reviewed) if state.last_reviewed else None
    return since or datetime.now(UTC) - timedelta(hours=24)


def _open_in_browser(path: Path) -> None:
    if path.exists():
        webbrowser.open(f"file://{path}")


def _stem_file(stem: Path, ext: str) -> Path:
    """`stem.<ext>` — with_suffix would replace everything after the stem's
    LAST dot, collapsing a dot-containing stem (`daily.report-2026-09-28`)
    to `daily.md`; appending to the name keeps the full stem intact."""
    return stem.with_name(stem.name + "." + ext)


def _refresh_symlinks(dated_stem: Path, latest_stem: Path, formats: list[str]) -> None:
    """Atomically point latest.<ext> at the newest digest files."""
    for ext in formats:
        dated = _stem_file(dated_stem, ext)
        latest = _stem_file(latest_stem, ext)
        latest.parent.mkdir(parents=True, exist_ok=True)
        tmp = latest.with_name(latest.name + ".tmp-link")
        if tmp.exists() or tmp.is_symlink():
            tmp.unlink()
        # The target must be absolute (or relative to the link's directory):
        # a bare relative `dated` string would resolve against latest.parent
        # and dangle.
        os.symlink(os.path.abspath(dated), tmp)
        os.replace(tmp, latest)


def _match_items(items: dict, needle: str) -> list[str]:
    """Ids whose id/url/title/repo contain the needle (case-insensitive)."""
    n = needle.lower()
    return [
        mid
        for mid, r in items.items()
        if n in mid.lower()
        or n in (r.title or "").lower()
        or n in (r.repo or "").lower()
        or n in (r.url or "").lower()
    ]


def _print_items(items: dict) -> None:
    if not items:
        click.echo("Inbox is empty.")
        return
    for mid, r in items.items():
        click.echo(f"{mid}\n    {r.title}  [{', '.join(r.kinds)}]")


def _disambiguate(matches: list[str], verb: str = "ack") -> str | None:
    """Interactive pick among multiple matches; None when cancelled."""
    click.echo("Multiple items match:")
    for i, mid in enumerate(matches, 1):
        click.echo(f"  {i}. {mid}")
    answer = click.prompt(f"Number to {verb} (empty to cancel)", default="", show_default=False)
    if not answer or not answer.isdigit() or not (1 <= int(answer) <= len(matches)):
        return None
    return matches[int(answer) - 1]


def _mark_thread_read(config, thread_url: str) -> None:
    """Best-effort: PATCH the notification thread as read via the requester.

    thread_url is the notification THREAD api url
    (https://api.github.com/notifications/threads/{id}) — the same endpoint
    PyGithub's own Notification.mark_as_read() PATCHes. Any failure is
    logged and tolerated: the local ack still applies.

    Deviation from the plan sketch (providers dict param): only the GitHub
    provider is built, and only here — a lazy GitHub login keeps `ack` off
    the network unless a notification item is actually being marked read,
    and never triggers a Launchpad handshake.
    """
    try:
        pc = config.providers.get("github")
        cls = PROVIDER_MAP.get("github")
        if pc is None or cls is None or not thread_url:
            return
        token = pc.token.get_secret_value() if pc.token is not None else None
        provider = cls(username=pc.username, token=token)
        # _github is GitHubProvider-specific; the GitProvider protocol omits it.
        requester = provider._github.requester  # type: ignore[attr-defined]
        requester.requestJsonAndCheck("PATCH", thread_url)
    except Exception as e:  # noqa: BLE001
        click.echo(f"Warning: could not mark thread read ({e}).", err=True)


def _recover_last_reviewed(state: AttentionState, digest_stem: Path) -> None:
    """Recover the consumption cursor after a corrupt-state rebuild.

    The newest dated digest's front matter is the only surviving record of
    where the cursor was: its `generated_at` when the body's status line says
    "Status: Reviewed", else its `coverage_start` (generated but never
    reviewed). With no digests on disk the cursor stays unset, which falls
    back to the 24h first-run window. Only called right after `load_state`
    rebuilt from a corrupt file (digest/read, under the state lock).
    """
    newest_ts, targets = _digests_generated_after(digest_stem, None)
    if newest_ts is None or not targets:
        return
    meta, body = strip_front_matter(targets[0].read_text())
    if "Status: Reviewed" in body:
        state.last_reviewed = newest_ts
    else:
        state.last_reviewed = meta.get("coverage_start")


def _digests_generated_after(pattern: Path, previous: str | None) -> tuple[str | None, list[Path]]:
    """Newest digest generation ts + digests generated after `previous`.

    ISO-8601 UTC timestamps with the same offset sort correctly as plain
    strings, so sorting (ts, path) tuples is safe. `read` sets
    last_reviewed = newest_ts, so any event after that digest's generation
    lands in the next coverage window.
    """
    parent = pattern.parent
    stem_re = re.compile(
        re.escape(pattern.name).replace(re.escape("YYYY-MM-DD"), r"\d{4}-\d{2}-\d{2}") + r"\.md$"
    )
    digests = []
    for path in parent.glob("*.md"):
        if not stem_re.match(path.name):
            continue
        meta, _body = strip_front_matter(path.read_text())
        ts = meta.get("generated_at")
        if ts:
            digests.append((ts, path))
    digests.sort()
    if not digests:
        return None, []
    newest_ts, newest_path = digests[-1]
    if previous is None:
        return newest_ts, [newest_path]
    targets = [p for ts, p in digests if ts > previous]
    return newest_ts, targets


if __name__ == "__main__":
    cli()
