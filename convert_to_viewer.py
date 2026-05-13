#!/usr/bin/env python3
"""
Convert export-slack channel data to ee-slack-viewer format.

Usage:
    uv run convert_to_viewer.py <channel_dir> [<channel_dir> ...] --output <output_dir>
    uv run convert_to_viewer.py downloads/atlassian --output viewer-data

Output layout (matches ee-slack-viewer's channel-dump/ directory):
    <output_dir>/
        channels.json          # { "<CHANNEL_ID>": "<channel_name>", ... }
        <CHANNEL_ID>.json      # per-channel: { fileIndex, messages, users }
        files/                 # downloaded attachments (symlinked or copied)
            <file_id>.<ext>
            ...

The per-channel JSON is loaded directly by ee-slack-viewer's server.py via
GET /api/channel/<id>, and consumed by the viewer frontend.
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import click


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _atomic_write_json(path: Path, data: object, indent: int = 2) -> None:
    """Write JSON atomically via a temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_json(path: Path) -> object:
    """Load JSON; return None on missing or corrupt file."""
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Message trimming
# ---------------------------------------------------------------------------

# Fields that are large / API-internal and not needed by the viewer.
_MSG_DROP = frozenset({
    "blocks",
    "client_msg_id",
    "team",
    "edited",
    "display_as_bot",
    "inviter",
    "subscribed",
    "reply_users",
    "reply_users_count",
    "is_locked",
})

# Fields to keep on file objects attached to messages.
_FILE_KEEP = frozenset({
    "id", "name", "title", "mimetype", "filetype", "local_path",
})


def _trim_file(f: dict) -> dict:
    return {k: v for k, v in f.items() if k in _FILE_KEEP}


def _trim_message(msg: dict) -> dict:
    """Return a viewer-ready copy of a message, trimming bulky API fields."""
    out = {k: v for k, v in msg.items() if k not in _MSG_DROP}

    # Trim reactions: keep only name + count (drop user lists).
    if "reactions" in out:
        out["reactions"] = [
            {"name": r["name"], "count": r.get("count", len(r.get("users", [])))}
            for r in out["reactions"]
            if r.get("name")
        ]

    # Trim file attachments.
    if "files" in out:
        out["files"] = [_trim_file(f) for f in out["files"]]

    # Recursively trim thread replies.
    if "thread_replies" in out:
        out["thread_replies"] = [_trim_message(r) for r in out["thread_replies"]]

    return out


# ---------------------------------------------------------------------------
# Per-channel conversion
# ---------------------------------------------------------------------------

def convert_channel(
    channel_dir: Path,
    output_dir: Path,
    copy_files: bool,
) -> tuple[str, str] | None:
    """
    Convert one channel export directory to viewer format.

    Returns (channel_id, channel_name) on success, or None on failure.
    """
    channel_dir = channel_dir.resolve()

    # --- Load source data ---
    messages_data = _load_json(channel_dir / "messages.json")
    if messages_data is None:
        click.echo(f"  [skip] {channel_dir}: messages.json missing or corrupt", err=True)
        return None

    metadata = _load_json(channel_dir / "metadata.json")
    if metadata is None:
        click.echo(f"  [skip] {channel_dir}: metadata.json missing or corrupt", err=True)
        return None

    files_manifest = _load_json(channel_dir / "files.json") or {}

    # --- Channel identity ---
    channel_info = metadata.get("channel", {})
    channel_id = channel_info.get("id")
    channel_name = channel_info.get("name") or channel_dir.name

    if not channel_id:
        click.echo(f"  [skip] {channel_dir}: no channel.id in metadata.json", err=True)
        return None

    # --- Build fileIndex from downloaded files ---
    # fileIndex: { file_id: "file_id.ext" }
    # Only include files with status=downloaded that have a local_path.
    file_index: dict[str, str] = {}
    for file_id, entry in files_manifest.items():
        if entry.get("status") == "downloaded":
            local_path = entry.get("local_path")  # e.g. "files/F0104FU3H1D.png"
            if local_path:
                basename = Path(local_path).name   # "F0104FU3H1D.png"
                file_index[file_id] = basename

    # --- Build messages ---
    raw_messages = messages_data.get("messages", [])

    # Filter to top-level messages only (skip leaked replies just in case).
    top_level = [
        m for m in raw_messages
        if not m.get("thread_ts") or m["thread_ts"] == m.get("ts")
    ]
    top_level.sort(key=lambda m: float(m.get("ts", 0)))

    trimmed_messages = [_trim_message(m) for m in top_level]

    # --- Users ---
    users: dict[str, str] = messages_data.get("users", {})

    # --- Write channel JSON ---
    output_dir.mkdir(parents=True, exist_ok=True)
    channel_json_path = output_dir / f"{channel_id}.json"

    channel_data = {
        "fileIndex": file_index,
        "messages": trimmed_messages,
        "users": users,
        "private": channel_info.get("is_private", False),
    }
    _atomic_write_json(channel_json_path, channel_data)
    click.echo(
        f"  {channel_name} ({channel_id}): "
        f"{len(trimmed_messages)} messages, "
        f"{len(file_index)} files → {channel_json_path}"
    )

    # --- Copy / symlink files ---
    files_out_dir = output_dir / "files"
    files_out_dir.mkdir(exist_ok=True)

    src_files_dir = channel_dir / "files"
    linked = skipped = 0

    for file_id, basename in file_index.items():
        src = src_files_dir / basename
        dst = files_out_dir / basename
        if dst.exists() or dst.is_symlink():
            skipped += 1
            continue
        if not src.is_file():
            click.echo(f"    [warn] source file missing: {src}", err=True)
            continue
        if copy_files:
            shutil.copy2(src, dst)
        else:
            # Symlink: use relative path so the output dir is portable.
            try:
                dst.symlink_to(src)
            except OSError:
                shutil.copy2(src, dst)
        linked += 1

    if linked or skipped:
        action = "copied" if copy_files else "linked"
        click.echo(f"    files: {linked} {action}, {skipped} already present")

    return channel_id, channel_name


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.argument(
    "channel_dirs",
    nargs=-1,
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--output", "-o",
    required=True,
    type=click.Path(path_type=Path),
    help="Output directory (becomes channel-dump/ for ee-slack-viewer).",
)
@click.option(
    "--copy/--no-copy",
    default=False,
    show_default=True,
    help="Copy files instead of symlinking (use when moving output off this machine).",
)
def main(channel_dirs: tuple[Path, ...], output: Path, copy: bool) -> None:
    """
    Convert one or more export-slack channel directories to ee-slack-viewer format.

    CHANNEL_DIRS are the per-channel export directories produced by export_slack.py
    (each should contain messages.json, metadata.json, files.json, and files/).

    Example:

        uv run convert_to_viewer.py downloads/atlassian downloads/general \\
            --output viewer-data/channel-dump
    """
    output = output.resolve()
    click.echo(f"Output: {output}")

    # Load existing channels.json so we can merge without clobbering other channels.
    channels_json_path = output / "channels.json"
    existing_channels: dict[str, object] = _load_json(channels_json_path) or {}

    errors: list[str] = []
    converted = 0

    for channel_dir in channel_dirs:
        click.echo(f"\nConverting {channel_dir} …")
        result = convert_channel(channel_dir, output, copy_files=copy)
        if result is None:
            errors.append(str(channel_dir))
        else:
            channel_id, channel_name = result
            existing_channels[channel_id] = channel_name
            converted += 1

    # Write merged channels.json
    _atomic_write_json(channels_json_path, existing_channels)
    click.echo(f"\nchannels.json: {len(existing_channels)} channel(s) → {channels_json_path}")

    click.echo(f"\nDone. {converted} channel(s) converted.")
    if errors:
        click.echo(f"{len(errors)} error(s):", err=True)
        for e in errors:
            click.echo(f"  {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
