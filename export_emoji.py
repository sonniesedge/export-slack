#!/usr/bin/env python3
"""
export_emoji.py — Download all custom emoji from a Slack workspace.

Saves each emoji as downloads/_emoji/<name>.<ext>.
Aliases (e.g. "thumbsup" -> "alias:+1") are skipped.

Usage:
    uv run export_emoji.py
    uv run export_emoji.py --token xoxb-...
    uv run export_emoji.py --output path/to/dir
"""

import os
import sys
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlparse

import click
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

DEFAULT_OUTPUT = Path("downloads/_emoji")


def get_emoji_list(client: WebClient) -> dict[str, str]:
    """Call emoji.list and return the full name->url mapping."""
    try:
        response = client.emoji_list()
    except SlackApiError as e:
        raise click.ClickException(f"Slack API error: {e.response['error']}")
    return response["emoji"]


def infer_extension(url: str) -> str:
    """Extract file extension from a URL path, defaulting to .png."""
    path = urlparse(url).path
    suffix = Path(path).suffix  # e.g. ".png", ".gif"
    return suffix if suffix else ".png"


def download_emoji(name: str, url: str, output_dir: Path) -> str:
    """
    Download a single emoji image.

    Returns one of: "downloaded", "existed", "failed"
    """
    ext = infer_extension(url)
    dest = output_dir / f"{name}{ext}"

    if dest.exists():
        return "existed"

    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = resp.read()
    except urllib.error.URLError as e:
        click.echo(f"  WARN: failed to download {name}: {e}", err=True)
        return "failed"

    dest.write_bytes(data)
    return "downloaded"


@click.command()
@click.option(
    "--token",
    envvar="SLACK_TOKEN",
    required=True,
    help="Slack bot token (or set SLACK_TOKEN env var).",
)
@click.option(
    "--output",
    default=str(DEFAULT_OUTPUT),
    show_default=True,
    help="Directory to save emoji files.",
)
def main(token: str, output: str) -> None:
    """Download all custom emoji from the Slack workspace."""
    output_dir = Path(output)
    output_dir.mkdir(parents=True, exist_ok=True)

    client = WebClient(token=token)

    click.echo("Fetching emoji list...")
    emoji_map = get_emoji_list(client)
    total = len(emoji_map)
    click.echo(f"Found {total} emoji (custom + aliases).")

    n_downloaded = 0
    n_existed = 0
    n_aliases = 0
    n_failed = 0

    with click.progressbar(emoji_map.items(), label="Downloading", length=total) as bar:
        for name, value in bar:
            if value.startswith("alias:"):
                n_aliases += 1
                continue

            result = download_emoji(name, value, output_dir)
            if result == "downloaded":
                n_downloaded += 1
            elif result == "existed":
                n_existed += 1
            else:
                n_failed += 1

    click.echo(
        f"\nDone. {n_downloaded} downloaded, {n_existed} already existed, "
        f"{n_aliases} aliases skipped, {n_failed} failed."
    )
    click.echo(f"Saved to: {output_dir.resolve()}")

    if n_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
