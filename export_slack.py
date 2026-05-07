#!/usr/bin/env python3
"""
export_slack.py — Export a Slack channel to CSV, JSON, and download files.

Usage:
    uv run export_slack.py <channel> [--token TOKEN] [--output PATH]
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
FILES_DIR = "files"

# Proactive rate-limit pacing: Slack recommends ≤1 req/s as a safe baseline.
# We sleep BASE_DELAY seconds between requests, plus a small random jitter.
BASE_DELAY = 1.0      # seconds between API calls
JITTER_MAX = 0.25     # max extra random seconds added to each delay

# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------


def load_checkpoint(output_dir: Path) -> dict:
    path = output_dir / CHECKPOINT_FILE
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {
        "channel_id": None,
        "channel_name": None,
        "history_complete": False,
        "next_cursor": None,
        "messages_fetched": 0,
        "downloaded_file_ids": [],
        "fetched_thread_ts": [],
        "users": {},
    }


def save_checkpoint(output_dir: Path, checkpoint: dict) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / CHECKPOINT_FILE
    with open(path, "w") as f:
        json.dump(checkpoint, f, indent=2)


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------


def pace() -> None:
    """Sleep BASE_DELAY + random jitter to stay within Slack's rate limits."""
    time.sleep(BASE_DELAY + random.uniform(0, JITTER_MAX))


def with_retry(fn, *args, max_retries: int = 8, **kwargs):
    """Call fn(*args, **kwargs) with exponential back-off on rate limits."""
    delay = 1.0
    for attempt in range(max_retries):
        try:
            return fn(*args, **kwargs)
        except SlackApiError as e:
            if e.response.status_code == 429:
                retry_after = int(e.response.headers.get("Retry-After", delay))
                click.echo(
                    f"  Rate limited. Waiting {retry_after}s before retry "
                    f"(retry {attempt + 1} of {max_retries} for this request)..."
                )
                time.sleep(retry_after)
                delay = min(delay * 2, 60)
            else:
                raise
    raise click.ClickException("Max retries exceeded due to rate limiting.")


def resolve_channel(client: WebClient, channel_arg: str) -> tuple[str, str]:
    """Return (channel_id, channel_name) from a channel name or ID."""
    # Already an ID?
    if re.match(r"^[CG][A-Z0-9]+$", channel_arg, re.IGNORECASE):
        info = with_retry(client.conversations_info, channel=channel_arg)
        pace()
        name = info["channel"]["name"]
        return channel_arg, name

    # Search by name (strip leading #)
    name_search = channel_arg.lstrip("#")
    cursor = None
    while True:
        resp = with_retry(
            client.conversations_list,
            types="public_channel,private_channel",
            limit=200,
            cursor=cursor,
        )
        pace()
        for ch in resp["channels"]:
            if ch["name"] == name_search:
                return ch["id"], ch["name"]
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    raise click.ClickException(f"Channel '{channel_arg}' not found or not accessible.")


def resolve_user(client: WebClient, user_id: str, users_cache: dict) -> str:
    """Return the display name for a user ID, caching results."""
    if user_id in users_cache:
        return users_cache[user_id]
    try:
        resp = with_retry(client.users_info, user=user_id)
        pace()
        profile = resp["user"]["profile"]
        name = profile.get("real_name") or profile.get("display_name") or user_id
    except Exception:
        name = f"<@{user_id}>"
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
    Fetch all top-level messages from the channel, resuming from checkpoint.
    Returns the accumulated list of raw message dicts (no thread replies yet).
    """
    messages_path = output_dir / MESSAGES_JSON
    if messages_path.exists():
        with open(messages_path) as f:
            data = json.load(f)
            messages: list[dict] = data.get("messages", [])
    else:
        messages = []

    if checkpoint.get("history_complete"):
        click.echo("  Channel history already fully fetched (checkpoint). Skipping.")
        return messages

    cursor = checkpoint.get("next_cursor") or None
    page = 0

    while True:
        page += 1
        click.echo(f"  Fetching history page {page}...")

        resp = with_retry(
            client.conversations_history,
            channel=channel_id,
            limit=200,
            cursor=cursor,
        )
        pace()

        batch = resp.get("messages", [])
        messages.extend(batch)
        checkpoint["messages_fetched"] = len(messages)

        # Persist after every page so interruptions don't lose progress
        _write_json(output_dir, messages, checkpoint.get("users", {}))
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        checkpoint["next_cursor"] = cursor
        save_checkpoint(output_dir, checkpoint)

        if not resp.get("has_more") or not cursor:
            break

    checkpoint["history_complete"] = True
    checkpoint["next_cursor"] = None
    save_checkpoint(output_dir, checkpoint)
    click.echo(f"  Fetched {len(messages)} top-level messages.")
    return messages


def fetch_thread_replies(
    client: WebClient,
    channel_id: str,
    messages: list[dict],
    output_dir: Path,
    checkpoint: dict,
) -> list[dict]:
    """
    For every thread parent, fetch all replies and nest them under the parent
    as a `thread_replies` list. Returns the updated messages list.
    """
    already_fetched: set[str] = set(checkpoint.get("fetched_thread_ts", []))

    parents = [
        m for m in messages
        if m.get("reply_count", 0) > 0
        and m.get("thread_ts") == m.get("ts")
        and m["ts"] not in already_fetched
    ]

    if not parents:
        click.echo("  No new threads to fetch.")
        return messages

    click.echo(f"  Fetching replies for {len(parents)} threads...")

    fetched_count = 0
    for parent in parents:
        thread_ts = parent["ts"]
        replies: list[dict] = []
        cursor = None

        while True:
            resp = with_retry(
                client.conversations_replies,
                channel=channel_id,
                ts=thread_ts,
                limit=200,
                cursor=cursor,
            )
            pace()
            # First message in replies is the parent itself — skip it
            batch = resp.get("messages", [])[1:]
            replies.extend(batch)
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not resp.get("has_more") or not cursor:
                break

        parent["thread_replies"] = replies

        already_fetched.add(thread_ts)
        fetched_count += 1

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


def download_files(
    client: WebClient,
    messages: list[dict],
    output_dir: Path,
    checkpoint: dict,
    token: str,
) -> None:
    """Download all files into <output_dir>/files/<file-id>.<filetype>."""
    import urllib.request

    files_dir = output_dir / FILES_DIR
    files_dir.mkdir(parents=True, exist_ok=True)

    downloaded: set[str] = set(checkpoint.get("downloaded_file_ids", []))

    # Collect all file objects across messages and their thread replies
    all_file_objects: list[dict] = []
    for msg in _all_messages(messages):
        for f in msg.get("files", []):
            if isinstance(f, dict) and f.get("id") and f.get("url_private"):
                all_file_objects.append(f)

    pending = [f for f in all_file_objects if f["id"] not in downloaded]
    if not pending:
        click.echo("  No new files to download.")
        return

    click.echo(f"  Downloading {len(pending)} file(s)...")

    for file_obj in pending:
        file_id = file_obj["id"]
        filetype = file_obj.get("filetype") or Path(file_obj.get("name", "bin")).suffix.lstrip(".")
        filename = f"{file_id}.{filetype}" if filetype else file_id
        dest = files_dir / filename
        url = file_obj["url_private"]

        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                dest.write_bytes(resp.read())
            click.echo(f"    Downloaded: {filename}")
        except Exception as e:
            click.echo(f"    WARNING: Failed to download {filename}: {e}", err=True)
            continue

        # Store the local path back onto the file object in-place
        file_obj["local_path"] = f"{FILES_DIR}/{filename}"

        downloaded.add(file_id)
        checkpoint["downloaded_file_ids"] = list(downloaded)
        save_checkpoint(output_dir, checkpoint)


def _all_messages(messages: list[dict]):
    """Yield every message including thread replies."""
    for msg in messages:
        yield msg
        for reply in msg.get("thread_replies", []):
            yield reply


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------


def _write_json(output_dir: Path, messages: list[dict], users: dict) -> None:
    path = output_dir / MESSAGES_JSON
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump({"messages": messages, "users": users}, f, indent=2)


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
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for msg in messages:
            author = users_cache.get(msg.get("user", ""), msg.get("username") or msg.get("user", ""))
            row = [
                _format_ts(msg.get("ts", "")),
                author,
                _message_text(msg, users_cache),
            ]
            writer.writerow(row)

            for reply in msg.get("thread_replies", []):
                reply_author = users_cache.get(reply.get("user", ""), reply.get("username") or reply.get("user", ""))
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


@click.command()
@click.argument("channel")
@click.option(
    "--token",
    envvar="SLACK_API_TOKEN",
    required=True,
    help="Slack API token (or set SLACK_API_TOKEN env var).",
)
@click.option(
    "--output",
    default=None,
    help="Output directory. Defaults to downloads/<channel_name>.",
)
def main(channel: str, token: str, output: str | None) -> None:
    """Export a Slack CHANNEL to CSV, JSON, and download its files.

    CHANNEL can be a channel name (e.g. general) or a channel ID (e.g. C01234ABC).
    """
    client = WebClient(token=token)

    # Resolve channel
    click.echo(f"Resolving channel '{channel}'...")
    channel_id, channel_name = resolve_channel(client, channel)
    click.echo(f"  Channel: #{channel_name} ({channel_id})")

    # Determine output directory
    output_dir = Path(output) if output else Path("downloads") / channel_name
    output_dir.mkdir(parents=True, exist_ok=True)
    click.echo(f"  Output directory: {output_dir.resolve()}")

    # Load checkpoint
    checkpoint = load_checkpoint(output_dir)
    checkpoint["channel_id"] = channel_id
    checkpoint["channel_name"] = channel_name
    save_checkpoint(output_dir, checkpoint)

    # --- Phase 1: Fetch channel history ---
    click.echo("\n[1/4] Fetching channel history...")
    messages = fetch_all_messages(client, channel_id, checkpoint, output_dir)

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

    click.echo(f"\nDone. {len(messages)} messages exported to {output_dir.resolve()}")
    click.echo(f"  {output_dir / MESSAGES_JSON}")
    click.echo(f"  {output_dir / MESSAGES_CSV}")
    click.echo(f"  {output_dir / FILES_DIR}/")


if __name__ == "__main__":
    main()
