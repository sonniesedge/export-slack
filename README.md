# Export Slack

Export Slack channels to JSON, CSV, and download file attachments.
Supports incremental updates — re-running only fetches new messages.

## Requirements

- [uv](https://github.com/astral-sh/uv)
- A Slack bot token (`xoxb-...`) with the following OAuth scopes:
  - `channels:history`, `channels:read`
  - `groups:history`, `groups:read` (private channels)
  - `files:read`
  - `conversations.join` (if the bot needs to auto-join channels)

Set the token via the `SLACK_API_TOKEN` environment variable or pass `--token` on each command.

## Quick start

```bash
# Export a single channel by name
uv run export_slack.py export general

# Export using a channel ID
uv run export_slack.py export C01234ABC

# Export with an explicit token
uv run export_slack.py export general --token xoxb-...
```

Output is written to `downloads/<channel_name>/` by default.

## Commands

### `export`

```
uv run export_slack.py export [OPTIONS] [CHANNEL]
```

Exports messages, threads, and files for one channel (or a batch of channels).

| Option | Description |
|---|---|
| `--token TEXT` | Slack API token. Falls back to `SLACK_API_TOKEN` env var. |
| `--output PATH` | Output directory. Defaults to `downloads/<channel_name>`. Ignored with `--batch`. |
| `--batch FILE` | Path to a file with one channel ID or name per line. `#` comments and blank lines are ignored. Cannot be combined with `CHANNEL`. |
| `-v, --verbose` | Print API calls, rate-limit pacing, and per-message detail. |

**Examples**

```bash
# Single channel
uv run export_slack.py export general

# Custom output directory
uv run export_slack.py export general --output /tmp/slack-export

# Batch export from a file
uv run export_slack.py export --batch channels.txt
```

`channels.txt` format:
```
# Engineering channels
C01234ABC
dev-backend
dev-frontend

# skip this one
# random-noise
```

In batch mode, channels that are not found or inaccessible are skipped with an error message; all other channels continue to be exported. A summary of any failures is printed at the end.

### `refresh-cache`

```
uv run export_slack.py refresh-cache [OPTIONS]
```

Fetches all visible channels from Slack and rebuilds the local name↔ID cache
at `downloads/.channel_cache.json`. Run this once before exporting so that
channel names resolve instantly without paginating the API on every export.

| Option | Description |
|---|---|
| `--token TEXT` | Slack API token. Falls back to `SLACK_API_TOKEN` env var. |
| `-v, --verbose` | Verbose output. |

```bash
uv run export_slack.py refresh-cache
```

## Output files

Each channel is exported to its own directory (`downloads/<channel_name>/`):

| File | Description |
|---|---|
| `messages.json` | All messages and thread replies as a JSON array. |
| `messages.csv` | Flat CSV of top-level messages (no thread replies). |
| `metadata.json` | Channel info: name, ID, `is_private`, topic, purpose, member count, export timestamp. |
| `files.json` | Manifest of every file attachment. See below. |
| `files/` | Downloaded file attachments named `<file_id>.<ext>`. |
| `.checkpoint.json` | Internal state for incremental updates. Do not edit. |

### `files.json`

Tracks the download status of every file attachment seen in the channel:

```json
{
  "F01234ABC": {
    "id": "F01234ABC",
    "name": "design.png",
    "filetype": "png",
    "pretty_type": "PNG",
    "mimetype": "image/png",
    "title": "Design mockup",
    "size": 204800,
    "url_private": "https://files.slack.com/...",
    "status": "downloaded",
    "local_path": "files/F01234ABC.png"
  },
  "F09999XYZ": {
    "id": "F09999XYZ",
    "name": "spec.gdoc",
    "filetype": "gdoc",
    "pretty_type": "GDoc",
    "status": "failed_permanent",
    "error": "HTTP 403 Forbidden (will not retry)"
  }
}
```

**Status values**

| Status | Meaning |
|---|---|
| `downloaded` | File is on disk at `local_path`. |
| `failed_permanent` | HTTP 401 or 403 — typically Google Docs/Sheets/Slides hosted outside Slack. Will never be retried. |
| `failed_transient` | Any other download error. Will be retried on the next run. |

## Incremental updates

Re-running export on a channel that has already been exported only fetches
messages newer than the previous run. Thread replies in updated threads are
also re-fetched. There is no flag needed — this is the default behaviour.

## Environment variables

| Variable | Description |
|---|---|
| `SLACK_API_TOKEN` | Default Slack API token used when `--token` is not supplied. |
