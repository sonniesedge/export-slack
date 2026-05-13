#!/usr/bin/env python3
"""
export_slack.py — Export a Slack channel to CSV, JSON, and download files.

Usage:
    uv run export_slack.py <channel> [--token TOKEN] [--output PATH] [--verbose]
"""

import csv
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import click
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHECKPOINT_FILE = ".checkpoint.json"
MESSAGES_JSON = "messages.json"
MESSAGES_CSV = "messages.csv"
METADATA_JSON = "metadata.json"
FILES_DIR = "files"
FILES_JSON = "files.json"
CHANNEL_CACHE_FILE = ".channel_cache.json"
DOWNLOADS_DIR = Path("downloads")

# Proactive rate-limit pacing: Slack recommends ≤1 req/s as a safe baseline.
# We sleep BASE_DELAY seconds between requests, plus a small random jitter.
BASE_DELAY = 1.0      # seconds between API calls
JITTER_MAX = 0.25     # max extra random seconds added to each delay

# ---------------------------------------------------------------------------
# Verbose logging
# ---------------------------------------------------------------------------

_verbose: bool = False


def vlog(msg: str) -> None:
    """Print msg only when --verbose is active."""
    if _verbose:
        click.echo(f"  [verbose] {msg}")


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------


def load_checkpoint(output_dir: Path) -> dict:
    path = output_dir / CHECKPOINT_FILE
    if path.exists():
        vlog(f"Loading checkpoint from {path}")
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            click.echo(
                f"  Warning: checkpoint at {path} is corrupt ({exc}); starting fresh.",
                err=True,
            )
            path.unlink(missing_ok=True)
    vlog("No checkpoint found, starting fresh.")
    return {
        "channel_id": None,
        "channel_name": None,
        "last_export_ts": None,
        "next_cursor": None,
        "messages_fetched": 0,
        "downloaded_file_ids": [],
        "fetched_thread_ts": [],
        "users": {},
    }


def save_checkpoint(output_dir: Path, checkpoint: dict) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / CHECKPOINT_FILE
    tmp = path.with_suffix(".tmp")
    vlog(f"Saving checkpoint ({checkpoint.get('messages_fetched', 0)} messages so far)")
    with open(tmp, "w") as f:
        json.dump(checkpoint, f, indent=2)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Channel cache
# ---------------------------------------------------------------------------


def _cache_path() -> Path:
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    return DOWNLOADS_DIR / CHANNEL_CACHE_FILE


def load_channel_cache() -> dict:
    """Load the channel name<->ID cache from disk, or return an empty cache."""
    path = _cache_path()
    if path.exists():
        vlog(f"Loading channel cache from {path}")
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            click.echo(
                f"  Warning: channel cache at {path} is corrupt ({exc}); starting fresh.",
                err=True,
            )
            path.unlink(missing_ok=True)
    vlog("No channel cache found, starting fresh.")
    return {"by_name": {}, "by_id": {}}


def save_channel_cache(cache: dict) -> None:
    """Persist the channel cache to disk using an atomic write."""
    path = _cache_path()
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)
    tmp.replace(path)
    vlog(f"Saved channel cache ({len(cache['by_id'])} entries) to {path}")


def update_channel_cache(cache: dict, channel_id: str, channel_name: str) -> None:
    """Add or refresh a single entry in the in-memory cache (no disk write)."""
    cache["by_name"][channel_name] = channel_id
    cache["by_id"][channel_id] = channel_name


def populate_channel_cache(client: WebClient) -> dict:
    """Fetch every channel the token can see and build a full cache."""
    cache: dict = {"by_name": {}, "by_id": {}}
    cursor = None
    page = 0
    total = 0
    while True:
        page += 1
        vlog(f"conversations.list page {page} (cache population)")
        resp = with_retry(
            client.conversations_list,
            types="public_channel,private_channel",
            limit=200,
            cursor=cursor,
        )
        pace()
        for ch in resp.get("channels", []):
            cache["by_name"][ch["name"]] = ch["id"]
            cache["by_id"][ch["id"]] = ch["name"]
            total += 1
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    save_channel_cache(cache)
    return cache


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------


def pace() -> None:
    """Sleep BASE_DELAY + random jitter to stay within Slack's rate limits."""
    delay = BASE_DELAY + random.uniform(0, JITTER_MAX)
    vlog(f"Pacing: sleeping {delay:.2f}s")
    time.sleep(delay)


def with_retry(fn, *args, max_retries: int = 8, client: "WebClient | None" = None, **kwargs):
    """Call fn(*args, **kwargs) with exponential back-off on rate limits.

    If the Slack API returns 'not_in_channel' and a ``client`` is provided along
    with a ``channel`` kwarg, the bot will attempt to join the channel once and
    then retry the original call.
    """
    vlog(f"API call: {fn.__name__} {kwargs}")
    delay = 1.0
    joined_channel = False
    for attempt in range(max_retries):
        try:
            return fn(*args, **kwargs)
        except SlackApiError as e:
            error_code = e.response.get("error", "")
            if e.response.status_code == 429:
                retry_after = int(e.response.headers.get("Retry-After", delay))
                click.echo(
                    f"  Rate limited. Waiting {retry_after}s before retry "
                    f"(retry {attempt + 1} of {max_retries} for this request)..."
                )
                time.sleep(retry_after)
                delay = min(delay * 2, 60)
            elif error_code == "not_in_channel" and client is not None and not joined_channel:
                channel_id = kwargs.get("channel")
                if channel_id:
                    click.echo(
                        f"  Bot is not in channel {channel_id}. "
                        "Attempting to join automatically..."
                    )
                    try:
                        client.conversations_join(channel=channel_id)
                        joined_channel = True
                        click.echo("  Joined channel. Retrying...")
                    except SlackApiError as join_err:
                        raise click.ClickException(
                            f"Cannot join channel {channel_id}: "
                            f"{join_err.response.get('error', join_err)}\n"
                            "Ensure the bot has been invited to the channel or "
                            "has the 'channels:join' scope."
                        ) from join_err
                else:
                    raise
            else:
                raise
    raise click.ClickException("Max retries exceeded due to rate limiting.")


def resolve_channel(
    client: WebClient,
    channel_arg: str,
    cache: dict | None = None,
) -> tuple[str, str, dict]:
    """Return (channel_id, channel_name, channel_info) from a channel name or ID.

    If ``cache`` is provided it is used as a fast-path to skip the API lookup
    where possible, and is updated with any newly resolved entries.
    """
    cache = cache if cache is not None else {"by_name": {}, "by_id": {}}

    # Already an ID?
    if re.match(r"^[CG][A-Z0-9]+$", channel_arg, re.IGNORECASE):
        channel_id = channel_arg.upper()
        # We still need full channel info, but we can log a cache hit for the name.
        if channel_id in cache["by_id"]:
            vlog(f"Cache hit: {channel_id} -> '{cache['by_id'][channel_id]}'")
        vlog(f"'{channel_arg}' looks like a channel ID, calling conversations.info")
        try:
            info = with_retry(client.conversations_info, channel=channel_id)
        except SlackApiError as e:
            if e.response.get("error") == "channel_not_found":
                raise click.ClickException(
                    f"Channel '{channel_arg}' not found or not accessible."
                )
            raise
        pace()
        ch = info["channel"]
        update_channel_cache(cache, ch["id"], ch["name"])
        return ch["id"], ch["name"], ch

    # Search by name (strip leading #)
    name_search = channel_arg.lstrip("#")

    # Fast-path: name already in cache
    if name_search in cache["by_name"]:
        cached_id = cache["by_name"][name_search]
        vlog(f"Cache hit: '{name_search}' -> {cached_id}, fetching full info")
        try:
            info = with_retry(client.conversations_info, channel=cached_id)
        except SlackApiError as e:
            if e.response.get("error") == "channel_not_found":
                raise click.ClickException(
                    f"Channel '{channel_arg}' not found or not accessible."
                )
            raise
        pace()
        ch = info["channel"]
        update_channel_cache(cache, ch["id"], ch["name"])
        return ch["id"], ch["name"], ch

    # Fall back to paginated search
    vlog(f"Searching for channel by name '{name_search}' via conversations.list")
    cursor = None
    page = 0
    while True:
        page += 1
        vlog(f"conversations.list page {page}")
        resp = with_retry(
            client.conversations_list,
            types="public_channel,private_channel",
            limit=200,
            cursor=cursor,
        )
        pace()
        for ch in resp["channels"]:
            # Opportunistically cache every channel we see
            update_channel_cache(cache, ch["id"], ch["name"])
            if ch["name"] == name_search:
                vlog(f"Found channel: {ch['id']}, fetching full info")
                info = with_retry(client.conversations_info, channel=ch["id"])
                pace()
                ch_full = info["channel"]
                update_channel_cache(cache, ch_full["id"], ch_full["name"])
                return ch_full["id"], ch_full["name"], ch_full
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    raise click.ClickException(f"Channel '{channel_arg}' not found or not accessible.")


def resolve_user(client: WebClient, user_id: str, users_cache: dict) -> str:
    """Return the display name for a user ID, caching results."""
    if user_id in users_cache:
        vlog(f"User {user_id} already in cache: '{users_cache[user_id]}'")
        return users_cache[user_id]
    vlog(f"Resolving user {user_id} via users.info")
    try:
        resp = with_retry(client.users_info, user=user_id)
        pace()
        profile = resp["user"]["profile"]
        name = profile.get("real_name") or profile.get("display_name") or user_id
    except Exception:
        name = f"<@{user_id}>"
    vlog(f"Resolved {user_id} -> '{name}'")
    users_cache[user_id] = name
    return name


# ---------------------------------------------------------------------------
# Message fetching
# ---------------------------------------------------------------------------


def fetch_all_messages(
    client: WebClient,
    channel_id: str,
    checkpoint: dict,
    output_dir: Path,
) -> list[dict]:
    """
    Fetch top-level messages from the channel.

    - First run: fetches the full history.
    - Subsequent runs: fetches only messages newer than ``last_export_ts``
      stored in the checkpoint, then merges them into the existing messages
      file (keyed by ``ts`` so duplicates are collapsed).
    """
    messages_path = output_dir / MESSAGES_JSON

    # Load whatever we already have on disk (may be empty on first run).
    existing: dict[str, dict] = {}
    if messages_path.exists():
        vlog(f"Loading existing messages from {messages_path}")
        try:
            with open(messages_path) as f:
                data = json.load(f)
            for m in data.get("messages", []):
                # Strip any replies that leaked into a previous export.
                if m.get("thread_ts") and m["thread_ts"] != m["ts"]:
                    vlog(f"Removing leaked reply from existing data: ts={m['ts']}")
                    continue
                existing[m["ts"]] = m
            vlog(f"Loaded {len(existing)} messages from disk")
        except (json.JSONDecodeError, OSError) as exc:
            click.echo(
                f"  Warning: {messages_path} is corrupt ({exc}); starting from scratch.",
                err=True,
            )
            messages_path.unlink(missing_ok=True)

    # Determine the starting point for this fetch.
    last_ts = checkpoint.get("last_export_ts")
    if last_ts:
        click.echo(f"  Incremental mode: fetching messages since ts={last_ts}")
    else:
        click.echo("  Full fetch: no previous export timestamp found.")

    # If a mid-run cursor was saved (interrupted full/incremental fetch),
    # resume from there rather than restarting.
    cursor = checkpoint.get("next_cursor") or None
    page = 0
    new_count = 0

    while True:
        page += 1
        click.echo(f"  Fetching history page {page}...")

        kwargs: dict = dict(channel=channel_id, limit=200, client=client)
        if cursor:
            kwargs["cursor"] = cursor
        elif last_ts:
            kwargs["oldest"] = last_ts

        resp = with_retry(client.conversations_history, **kwargs)
        pace()

        batch = resp.get("messages", [])
        vlog(f"Page {page}: received {len(batch)} messages")
        for msg in batch:
            vlog(f"  ts={msg.get('ts')} user={msg.get('user')} text={msg.get('text', '')[:60]!r}")
            # Slack occasionally returns thread replies in the main history
            # feed (thread_ts != ts). Skip them here; they are fetched
            # properly via conversations.replies.
            if msg.get("thread_ts") and msg["thread_ts"] != msg["ts"]:
                vlog(f"  Skipping reply leaked into history: ts={msg['ts']} thread_ts={msg['thread_ts']}")
                continue
            if msg["ts"] not in existing:
                new_count += 1
            existing[msg["ts"]] = msg

        checkpoint["messages_fetched"] = len(existing)

        # Persist after every page so interruptions don't lose progress.
        messages = sorted(existing.values(), key=lambda m: m["ts"])
        _write_json(output_dir, messages, checkpoint.get("users", {}))
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        checkpoint["next_cursor"] = cursor
        save_checkpoint(output_dir, checkpoint)

        if not resp.get("has_more") or not cursor:
            break

    checkpoint["next_cursor"] = None
    save_checkpoint(output_dir, checkpoint)

    messages = sorted(existing.values(), key=lambda m: m["ts"])
    if last_ts:
        click.echo(f"  {new_count} new message(s) fetched; {len(messages)} total.")
    else:
        click.echo(f"  Fetched {len(messages)} top-level messages.")
        new_count = len(messages)
    return messages, new_count


def fetch_thread_replies(
    client: WebClient,
    channel_id: str,
    messages: list[dict],
    output_dir: Path,
    checkpoint: dict,
) -> list[dict]:
    """
    For every thread parent, fetch all replies and nest them under the parent
    as a ``thread_replies`` list. Returns the updated messages list.

    On incremental runs, threads whose ``latest_reply`` timestamp is newer than
    ``last_export_ts`` are re-fetched so new replies are captured.  Threads
    with no new activity are left untouched.
    """
    already_fetched: set[str] = set(checkpoint.get("fetched_thread_ts", []))
    last_ts = checkpoint.get("last_export_ts")

    def _needs_fetch(m: dict) -> bool:
        if m.get("reply_count", 0) == 0:
            return False
        if m.get("thread_ts") != m.get("ts"):
            return False
        if m["ts"] not in already_fetched:
            return True
        # Re-fetch if there has been new reply activity since last export.
        if last_ts and m.get("latest_reply", "0") > last_ts:
            return True
        return False

    parents = [m for m in messages if _needs_fetch(m)]

    if not parents:
        click.echo("  No new threads to fetch.")
        return messages

    click.echo(f"  Fetching replies for {len(parents)} threads...")

    fetched_count = 0
    for parent in parents:
        thread_ts = parent["ts"]
        vlog(f"Fetching thread ts={thread_ts} ({parent.get('reply_count')} replies) "
             f"text={parent.get('text', '')[:60]!r}")
        replies: list[dict] = []
        cursor = None
        page = 0

        while True:
            page += 1
            resp = with_retry(
                client.conversations_replies,
                channel=channel_id,
                ts=thread_ts,
                limit=200,
                cursor=cursor,
                client=client,
            )
            pace()
            # First message in replies is the parent itself — skip it
            batch = resp.get("messages", [])[1:]
            vlog(f"  Thread page {page}: {len(batch)} replies")
            replies.extend(batch)
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not resp.get("has_more") or not cursor:
                break

        vlog(f"  Total replies fetched for thread {thread_ts}: {len(replies)}")
        parent["thread_replies"] = replies

        already_fetched.add(thread_ts)
        fetched_count += 1
        click.echo(f"  Thread {fetched_count}/{len(parents)} done ({len(replies)} replies)")

        if fetched_count % 10 == 0 or fetched_count == len(parents):
            checkpoint["fetched_thread_ts"] = list(already_fetched)
            _write_json(output_dir, messages, checkpoint.get("users", {}))
            save_checkpoint(output_dir, checkpoint)

    checkpoint["fetched_thread_ts"] = list(already_fetched)
    _write_json(output_dir, messages, checkpoint.get("users", {}))
    save_checkpoint(output_dir, checkpoint)
    click.echo("  Thread replies fetched and nested.")
    return messages


# ---------------------------------------------------------------------------
# File downloads
# ---------------------------------------------------------------------------


def _file_dest_name(file_obj: dict) -> str:
    """Return the local filename for a Slack file object."""
    file_id = file_obj["id"]
    filetype = file_obj.get("filetype") or Path(file_obj.get("name", "bin")).suffix.lstrip(".")
    return f"{file_id}.{filetype}" if filetype else file_id


def _load_files_manifest(output_dir: Path) -> dict[str, dict]:
    """Load files.json manifest keyed by file ID, or return empty dict."""
    path = output_dir / FILES_JSON
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            click.echo(f"  Warning: {path} is corrupt ({exc}); starting fresh.", err=True)
            path.unlink(missing_ok=True)
    return {}


def _save_files_manifest(output_dir: Path, manifest: dict[str, dict]) -> None:
    """Atomically write files.json manifest."""
    path = output_dir / FILES_JSON
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    tmp.replace(path)


def download_files(
    client: WebClient,
    messages: list[dict],
    output_dir: Path,
    checkpoint: dict,
    token: str,
) -> None:
    """Download all files into <output_dir>/files/ and track status in files.json.

    Each entry in files.json has:
      - id, name, filetype, pretty_type, size, url_private
      - status: "downloaded" | "failed_permanent" | "failed_transient"
      - local_path: relative path within output_dir (only when downloaded)
      - error: human-readable error string (only on failure)
      - mimetype, title from the Slack file object when available

    HTTP 401/403/410 errors are marked ``failed_permanent`` and never retried.
    401/403 are common for Google Docs/Sheets links; 410 means Slack has
    purged the file from storage (common on free-tier workspaces).
    All other errors are
    ``failed_transient`` and will be retried on the next run.
    """
    import urllib.error
    import urllib.request

    files_dir = output_dir / FILES_DIR
    files_dir.mkdir(parents=True, exist_ok=True)

    manifest = _load_files_manifest(output_dir)

    # Collect all file objects across messages and their thread replies.
    seen: dict[str, dict] = {}
    for msg in _all_messages(messages):
        for f in msg.get("files", []):
            if not isinstance(f, dict) or not f.get("id"):
                continue
            fid = f["id"]
            if f.get("mode") == "tombstone":
                # File was deleted; record as permanent failure so the test
                # cross-check passes and we never attempt to download it.
                if fid not in manifest:
                    manifest[fid] = {
                        "id": fid,
                        "name": f.get("name", ""),
                        "filetype": f.get("filetype", ""),
                        "pretty_type": f.get("pretty_type", ""),
                        "mimetype": f.get("mimetype", ""),
                        "title": f.get("title", ""),
                        "size": f.get("size", 0),
                        "url_private": "",
                        "status": "failed_permanent",
                        "local_path": None,
                        "error": "tombstone: file was deleted by the user",
                    }
                continue
            if f.get("url_private"):
                seen[fid] = f

    def _needs_download(file_obj: dict) -> bool:
        fid = file_obj["id"]
        entry = manifest.get(fid, {})
        if entry.get("status") == "failed_permanent":
            return False
        if entry.get("status") == "downloaded":
            return not (output_dir / entry["local_path"]).exists()
        return True  # not yet attempted, or transient failure

    pending = [f for f in seen.values() if _needs_download(f)]

    total = len(seen)
    already_done = total - len(pending)
    if not pending:
        _save_files_manifest(output_dir, manifest)
        click.echo(f"  No new files to download ({already_done}/{total} already done).")
        return

    click.echo(f"  Downloading {len(pending)} file(s) ({already_done}/{total} already done)...")

    for i, file_obj in enumerate(pending, 1):
        file_id = file_obj["id"]
        filename = _file_dest_name(file_obj)
        dest = files_dir / filename
        url = file_obj["url_private"]

        # Build / update manifest entry with latest metadata from Slack.
        entry: dict = manifest.get(file_id, {})
        entry.update({
            "id": file_id,
            "name": file_obj.get("name", ""),
            "filetype": file_obj.get("filetype", ""),
            "pretty_type": file_obj.get("pretty_type", ""),
            "mimetype": file_obj.get("mimetype", ""),
            "title": file_obj.get("title", ""),
            "size": file_obj.get("size"),
            "url_private": url,
        })

        vlog(f"File {i}/{len(pending)}: {filename} ({entry.get('pretty_type')} "
             f"{entry.get('size')} bytes) from {url}")

        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                dest.write_bytes(resp.read())

            local_path = f"{FILES_DIR}/{filename}"
            entry["status"] = "downloaded"
            entry["local_path"] = local_path
            entry.pop("error", None)
            file_obj["local_path"] = local_path
            click.echo(f"    Downloaded: {filename}")

        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 410):
                entry["status"] = "failed_permanent"
                entry["error"] = f"HTTP {e.code} {e.reason} (will not retry)"
                click.echo(
                    f"    Skipped (permanent): {filename} — HTTP {e.code} {e.reason}",
                    err=True,
                )
            else:
                entry["status"] = "failed_transient"
                entry["error"] = f"HTTP {e.code} {e.reason}"
                click.echo(
                    f"    WARNING: {filename} — HTTP {e.code} {e.reason} (will retry)",
                    err=True,
                )
        except Exception as e:
            entry["status"] = "failed_transient"
            entry["error"] = str(e)
            click.echo(f"    WARNING: Failed to download {filename}: {e}", err=True)

        manifest[file_id] = entry
        _save_files_manifest(output_dir, manifest)

        # Keep checkpoint in sync for backwards compatibility.
        if entry["status"] == "downloaded":
            downloaded_ids: list = checkpoint.get("downloaded_file_ids", [])
            if file_id not in downloaded_ids:
                downloaded_ids.append(file_id)
            checkpoint["downloaded_file_ids"] = downloaded_ids
            save_checkpoint(output_dir, checkpoint)

    permanent = sum(1 for e in manifest.values() if e.get("status") == "failed_permanent")
    transient = sum(1 for e in manifest.values() if e.get("status") == "failed_transient")
    downloaded = sum(1 for e in manifest.values() if e.get("status") == "downloaded")
    click.echo(
        f"  Files: {downloaded} downloaded, {permanent} permanently skipped, "
        f"{transient} transient failure(s)."
    )


def _all_messages(messages: list[dict]):
    """Yield every message including thread replies."""
    for msg in messages:
        yield msg
        for reply in msg.get("thread_replies", []):
            yield reply


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------


def write_metadata(output_dir: Path, channel_info: dict, message_count: int) -> None:
    """Write channel metadata to metadata.json."""
    ch = channel_info

    def _ts(ts) -> str | None:
        """Convert a Unix timestamp (int or float) to an ISO-8601 string."""
        try:
            return datetime.utcfromtimestamp(int(ts)).isoformat() + "Z"
        except (TypeError, ValueError, OSError):
            return None

    creator_id = ch.get("creator")
    purpose = ch.get("purpose", {})
    topic = ch.get("topic", {})

    metadata = {
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "channel": {
            "id": ch.get("id"),
            "name": ch.get("name"),
            "name_normalized": ch.get("name_normalized"),
            "created_at": _ts(ch.get("created")),
            "creator_id": creator_id,
            "is_private": ch.get("is_private"),
            "is_archived": ch.get("is_archived"),
            "is_general": ch.get("is_general"),
            "member_count": ch.get("num_members"),
            "topic": topic.get("value") or None,
            "topic_set_by": topic.get("creator") or None,
            "topic_set_at": _ts(topic.get("last_set")) if topic.get("last_set") else None,
            "purpose": purpose.get("value") or None,
            "purpose_set_by": purpose.get("creator") or None,
            "purpose_set_at": _ts(purpose.get("last_set")) if purpose.get("last_set") else None,
        },
        "export": {
            "message_count": message_count,
        },
    }

    path = output_dir / METADATA_JSON
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(metadata, f, indent=2)
    tmp.replace(path)
    vlog(f"Wrote metadata to {path}")


def _write_json(output_dir: Path, messages: list[dict], users: dict) -> None:
    path = output_dir / MESSAGES_JSON
    tmp = path.with_suffix(".tmp")
    output_dir.mkdir(parents=True, exist_ok=True)
    vlog(f"Writing {path} ({len(messages)} messages)")
    with open(tmp, "w") as f:
        json.dump({"messages": messages, "users": users}, f, indent=2)
    tmp.replace(path)


def _format_ts(ts: str) -> str:
    """Convert a Slack timestamp string to 'YYYY-MM-DD HH:MM:SS'."""
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return ts or ""


def _resolve_mentions(text: str, users_cache: dict) -> str:
    """Replace <@USERID> mentions with real names from the cache."""
    def replacer(m):
        uid = m.group(1)
        return users_cache.get(uid, f"<@{uid}>")
    return re.sub(r"<@([A-Z0-9]+)>", replacer, text or "")


def _message_text(msg: dict, users_cache: dict) -> str:
    """Build the text column: resolved mentions + appended file references."""
    text = _resolve_mentions(msg.get("text", ""), users_cache)
    file_lines = []
    for f in msg.get("files", []):
        if not isinstance(f, dict):
            continue
        name = f.get("name", f.get("id", "file"))
        local = f.get("local_path")
        if local:
            file_lines.append(f"{name} <{local}>")
        else:
            file_lines.append(name)
    if file_lines:
        text = text + ("\n" if text else "") + "\n".join(file_lines)
    return text


def write_csv(output_dir: Path, messages: list[dict], users_cache: dict) -> None:
    """
    Write messages.csv with no header row.
    Top-level messages: 3 columns — timestamp, author, text
    Thread replies:     4 columns — timestamp, author, "", text
    """
    path = output_dir / MESSAGES_CSV
    vlog(f"Writing {path}")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for msg in messages:
            author = users_cache.get(msg.get("user", ""), msg.get("username") or msg.get("user", ""))
            row = [
                _format_ts(msg.get("ts", "")),
                author,
                _message_text(msg, users_cache),
            ]
            vlog(f"CSV row: ts={row[0]} author={row[1]} text={str(row[2])[:60]!r}")
            writer.writerow(row)

            for reply in msg.get("thread_replies", []):
                reply_author = users_cache.get(reply.get("user", ""), reply.get("username") or reply.get("user", ""))
                vlog(f"  CSV reply: ts={_format_ts(reply.get('ts',''))} author={reply_author}")
                writer.writerow([
                    _format_ts(reply.get("ts", "")),
                    reply_author,
                    "",
                    _message_text(reply, users_cache),
                ])


# ---------------------------------------------------------------------------
# User resolution pass
# ---------------------------------------------------------------------------


def resolve_all_users(
    client: WebClient,
    messages: list[dict],
    checkpoint: dict,
    output_dir: Path,
) -> dict:
    """
    Walk all messages and thread replies, resolve every user ID encountered,
    and return the populated users cache. Results are persisted to checkpoint.
    """
    users_cache: dict = checkpoint.get("users", {})

    user_ids: set[str] = set()
    for msg in _all_messages(messages):
        if msg.get("user"):
            user_ids.add(msg["user"])
        # Collect mention IDs from text
        for uid in re.findall(r"<@([A-Z0-9]+)>", msg.get("text", "")):
            user_ids.add(uid)

    unknown = [uid for uid in user_ids if uid not in users_cache]
    if not unknown:
        click.echo("  All users already resolved (checkpoint).")
        return users_cache

    click.echo(f"  Resolving {len(unknown)} user(s)...")
    for uid in unknown:
        resolve_user(client, uid, users_cache)

    checkpoint["users"] = users_cache
    save_checkpoint(output_dir, checkpoint)
    _write_json(output_dir, messages, users_cache)
    return users_cache


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def export_channel(
    client: WebClient,
    token: str,
    channel: str,
    cache: dict,
    output: str | None = None,
) -> None:
    """Export a single channel. Shared by single and batch modes."""
    # Resolve channel
    click.echo(f"Resolving channel '{channel}'...")
    channel_id, channel_name, channel_info = resolve_channel(client, channel, cache)
    click.echo(f"  Channel: #{channel_name} ({channel_id})")

    # Determine output directory
    output_dir = Path(output) if output else DOWNLOADS_DIR / channel_name
    output_dir.mkdir(parents=True, exist_ok=True)
    click.echo(f"  Output directory: {output_dir.resolve()}")

    # Load checkpoint
    checkpoint = load_checkpoint(output_dir)
    checkpoint["channel_id"] = channel_id
    checkpoint["channel_name"] = channel_name
    save_checkpoint(output_dir, checkpoint)

    # --- Phase 1: Fetch channel history ---
    click.echo("\n[1/4] Fetching channel history...")
    messages, new_count = fetch_all_messages(client, channel_id, checkpoint, output_dir)

    # --- Phase 2: Fetch thread replies ---
    click.echo("\n[2/4] Fetching thread replies...")
    messages = fetch_thread_replies(client, channel_id, messages, output_dir, checkpoint)

    # --- Phase 3: Resolve users ---
    click.echo("\n[3/4] Resolving users...")
    users_cache = resolve_all_users(client, messages, checkpoint, output_dir)

    # --- Phase 4: Download files ---
    click.echo("\n[4/4] Downloading files...")
    download_files(client, messages, output_dir, checkpoint, token)

    # --- Final export ---
    click.echo("\nWriting final exports...")
    _write_json(output_dir, messages, users_cache)
    write_csv(output_dir, messages, users_cache)
    write_metadata(output_dir, channel_info, len(messages))

    # Record the timestamp of the newest message so the next run knows where
    # to start. Use the current time if there are no messages.
    if messages:
        newest_ts = max(m["ts"] for m in messages)
    else:
        newest_ts = str(time.time())
    checkpoint["last_export_ts"] = newest_ts
    checkpoint["next_cursor"] = None
    save_checkpoint(output_dir, checkpoint)

    click.echo(f"\nDone. {new_count} new message(s) ({len(messages)} total) exported to {output_dir.resolve()}")
    click.echo(f"  {output_dir / METADATA_JSON}")
    click.echo(f"  {output_dir / MESSAGES_JSON}")
    click.echo(f"  {output_dir / MESSAGES_CSV}")
    click.echo(f"  {output_dir / FILES_DIR}/")


# ---------------------------------------------------------------------------
# Shared CLI options
# ---------------------------------------------------------------------------

_token_option = click.option(
    "--token",
    envvar="SLACK_API_TOKEN",
    required=True,
    help="Slack API token (or set SLACK_API_TOKEN env var).",
)
_verbose_option = click.option(
    "--verbose", "-v",
    is_flag=True,
    default=False,
    help="Enable verbose output (API calls, pacing, per-message detail).",
)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.group()
def cli() -> None:
    """Export Slack channels to CSV, JSON, and download files."""


@cli.command("export")
@click.argument("channel", required=False)
@_token_option
@click.option(
    "--output",
    default=None,
    help="Output directory. Defaults to downloads/<channel_name>. Ignored with --batch.",
)
@click.option(
    "--batch",
    "batch_file",
    default=None,
    type=click.Path(exists=True, readable=True, dir_okay=False),
    help=(
        "Path to a file containing one channel ID or name per line. "
        "Lines starting with '#' and blank lines are ignored. "
        "Cannot be combined with CHANNEL."
    ),
)
@_verbose_option
def cmd_export(
    channel: str | None,
    token: str,
    output: str | None,
    batch_file: str | None,
    verbose: bool,
) -> None:
    """Export a Slack CHANNEL to CSV, JSON, and download its files.

    CHANNEL can be a channel name (e.g. general) or a channel ID (e.g. C01234ABC).
    Use --batch to supply a file of channel IDs/names instead.
    """
    global _verbose
    _verbose = verbose

    if batch_file and channel:
        raise click.UsageError("Provide either CHANNEL or --batch, not both.")
    if not batch_file and not channel:
        raise click.UsageError("Provide a CHANNEL argument or use --batch.")

    client = WebClient(token=token)
    cache = load_channel_cache()

    if batch_file:
        channels = []
        with open(batch_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                channels.append(line)

        if not channels:
            raise click.ClickException(f"No channels found in '{batch_file}'.")

        click.echo(f"Batch mode: {len(channels)} channel(s) from '{batch_file}'.")
        errors = []
        for i, ch in enumerate(channels, 1):
            click.echo(f"\n{'='*60}")
            click.echo(f"[{i}/{len(channels)}] Exporting '{ch}'...")
            click.echo(f"{'='*60}")
            try:
                export_channel(client, token, ch, cache)
            except (click.ClickException, click.Abort) as e:
                msg = f"  ERROR exporting '{ch}': {e}"
                click.echo(msg, err=True)
                errors.append(msg)

        save_channel_cache(cache)
        click.echo(f"\n{'='*60}")
        click.echo(f"Batch complete. {len(channels) - len(errors)}/{len(channels)} succeeded.")
        if errors:
            click.echo("Failures:")
            for err in errors:
                click.echo(f"  {err}", err=True)
            sys.exit(1)
    else:
        export_channel(client, token, channel, cache, output)
        save_channel_cache(cache)


@cli.command("refresh-cache")
@_token_option
@_verbose_option
def cmd_refresh_cache(token: str, verbose: bool) -> None:
    """Fetch all visible channels from Slack and rebuild the local channel cache.

    The cache is stored at downloads/.channel_cache.json and maps channel
    names to IDs (and vice-versa). Running this command once means subsequent
    export calls can resolve channel names without paginating conversations.list.
    """
    global _verbose
    _verbose = verbose

    client = WebClient(token=token)
    click.echo("Fetching all channels to rebuild cache...")
    cache = populate_channel_cache(client)
    count = len(cache["by_id"])
    click.echo(f"Done. Cached {count} channel(s) to {_cache_path()}.")


# ---------------------------------------------------------------------------
# Backwards-compatible entry point
# ---------------------------------------------------------------------------
# Allow the script to be called as before:
#   uv run export_slack.py <channel>          (implicit "export" subcommand)
#   uv run export_slack.py export <channel>   (explicit subcommand)
#   uv run export_slack.py refresh-cache      (new subcommand)


def main() -> None:
    # If the first real argument looks like a subcommand, delegate to the group.
    # Otherwise, inject "export" so the old single-channel usage still works.
    args = sys.argv[1:]
    known_subcommands = {"export", "refresh-cache", "--help", "-h"}
    if args and args[0] in known_subcommands:
        cli()
    else:
        # Prepend "export" so existing usage is unchanged
        sys.argv.insert(1, "export")
        cli()


if __name__ == "__main__":
    main()
