#!/usr/bin/env python3
"""
export_slack.py — Export a Slack channel to CSV, JSON, and download files.

Usage:
    uv run export_slack.py <channel> [--token TOKEN] [--output PATH]
"""

import csv
import json
import os
import re
import sys
import time
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

# CSV columns derived from the raw message payload
CSV_COLUMNS = [
    "ts",
    "thread_ts",
    "type",
    "subtype",
    "user",
    "username",
    "bot_id",
    "text",
    "reactions",
    "reply_count",
    "reply_users_count",
    "files",
    "attachments",
    "blocks",
]

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
    }


def save_checkpoint(output_dir: Path, checkpoint: dict) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / CHECKPOINT_FILE
    with open(path, "w") as f:
        json.dump(checkpoint, f, indent=2)


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------


def resolve_channel(client: WebClient, channel_arg: str) -> tuple[str, str]:
    """Return (channel_id, channel_name) from a channel name or ID."""
    # Already an ID?
    if re.match(r"^[CG][A-Z0-9]+$", channel_arg, re.IGNORECASE):
        info = client.conversations_info(channel=channel_arg)
        name = info["channel"]["name"]
        return channel_arg, name

    # Search by name (strip leading #)
    name_search = channel_arg.lstrip("#")
    cursor = None
    while True:
        resp = client.conversations_list(
            types="public_channel,private_channel",
            limit=200,
            cursor=cursor,
        )
        for ch in resp["channels"]:
            if ch["name"] == name_search:
                return ch["id"], ch["name"]
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    raise click.ClickException(f"Channel '{channel_arg}' not found or not accessible.")


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
    Returns the accumulated list of raw message dicts.
    """
    # Load any previously fetched messages from disk so we can append
    messages_path = output_dir / MESSAGES_JSON
    if messages_path.exists():
        with open(messages_path) as f:
            messages: list[dict] = json.load(f)
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

        batch = resp.get("messages", [])
        messages.extend(batch)
        checkpoint["messages_fetched"] = len(messages)

        # Persist after every page so interruptions don't lose progress
        _write_json(output_dir, messages)
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
    For every message that is a thread parent, fetch all replies and insert
    them inline immediately after the parent. Returns the expanded list.
    """
    # Build set of thread_ts values we still need to fetch
    already_fetched: set[str] = set(checkpoint.get("fetched_thread_ts", []))

    # Identify parent messages (has reply_count > 0 and ts == thread_ts)
    parents = [
        m for m in messages
        if m.get("reply_count", 0) > 0 and m.get("thread_ts") == m.get("ts")
        and m["ts"] not in already_fetched
    ]

    if not parents:
        return messages

    click.echo(f"  Fetching replies for {len(parents)} threads...")

    # Index messages by ts for fast lookup
    ts_to_index: dict[str, int] = {m["ts"]: i for i, m in enumerate(messages)}

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
            # First message in replies is the parent itself — skip it
            batch = resp.get("messages", [])[1:]
            replies.extend(batch)
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not resp.get("has_more") or not cursor:
                break

        # Insert replies inline after parent
        idx = ts_to_index.get(thread_ts)
        if idx is not None and replies:
            messages = messages[: idx + 1] + replies + messages[idx + 1 :]
            # Rebuild index after insertion
            ts_to_index = {m["ts"]: i for i, m in enumerate(messages)}

        already_fetched.add(thread_ts)
        fetched_count += 1

        if fetched_count % 10 == 0 or fetched_count == len(parents):
            checkpoint["fetched_thread_ts"] = list(already_fetched)
            _write_json(output_dir, messages)
            save_checkpoint(output_dir, checkpoint)

    checkpoint["fetched_thread_ts"] = list(already_fetched)
    _write_json(output_dir, messages)
    save_checkpoint(output_dir, checkpoint)
    click.echo(f"  Thread replies fetched and inserted inline.")
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
    """Download all files referenced in messages into <output_dir>/files/."""
    import urllib.request

    files_dir = output_dir / FILES_DIR
    files_dir.mkdir(parents=True, exist_ok=True)

    downloaded: set[str] = set(checkpoint.get("downloaded_file_ids", []))

    # Collect all file objects across messages
    all_files: list[dict] = []
    for msg in messages:
        for f in msg.get("files", []):
            if isinstance(f, dict) and f.get("id") and f.get("url_private"):
                all_files.append(f)

    pending = [f for f in all_files if f["id"] not in downloaded]
    if not pending:
        click.echo("  No new files to download.")
        return

    click.echo(f"  Downloading {len(pending)} file(s)...")

    for file_obj in pending:
        file_id = file_obj["id"]
        url = file_obj["url_private"]
        original_name = file_obj.get("name") or file_id
        dest = _unique_path(files_dir, original_name)

        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                dest.write_bytes(resp.read())
            click.echo(f"    Downloaded: {dest.name}")
        except Exception as e:
            click.echo(f"    WARNING: Failed to download {original_name}: {e}", err=True)
            continue

        downloaded.add(file_id)
        checkpoint["downloaded_file_ids"] = list(downloaded)
        save_checkpoint(output_dir, checkpoint)


def _unique_path(directory: Path, filename: str) -> Path:
    """Return a path that doesn't collide with existing files."""
    dest = directory / filename
    if not dest.exists():
        return dest
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 1
    while dest.exists():
        dest = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    return dest


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------


def _write_json(output_dir: Path, messages: list[dict]) -> None:
    path = output_dir / MESSAGES_JSON
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(messages, f, indent=2)


def write_csv(output_dir: Path, messages: list[dict]) -> None:
    path = output_dir / MESSAGES_CSV
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for msg in messages:
            row = {col: msg.get(col, "") for col in CSV_COLUMNS}
            # Serialise list/dict fields as JSON strings for CSV readability
            for col in ("reactions", "files", "attachments", "blocks"):
                val = row[col]
                if isinstance(val, (list, dict)):
                    row[col] = json.dumps(val, ensure_ascii=False)
            writer.writerow(row)


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
    click.echo("\n[1/3] Fetching channel history...")
    messages = fetch_all_messages(client, channel_id, checkpoint, output_dir)

    # --- Phase 2: Fetch thread replies ---
    click.echo("\n[2/3] Fetching thread replies...")
    messages = fetch_thread_replies(client, channel_id, messages, output_dir, checkpoint)

    # --- Phase 3: Download files ---
    click.echo("\n[3/3] Downloading files...")
    download_files(client, messages, output_dir, checkpoint, token)

    # --- Final export ---
    click.echo("\nWriting final exports...")
    _write_json(output_dir, messages)
    write_csv(output_dir, messages)

    click.echo(f"\nDone. {len(messages)} messages exported to {output_dir.resolve()}")
    click.echo(f"  {output_dir / MESSAGES_JSON}")
    click.echo(f"  {output_dir / MESSAGES_CSV}")
    click.echo(f"  {output_dir / FILES_DIR}/")


if __name__ == "__main__":
    main()
