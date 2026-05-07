# Slack Channel Export Tool

## Overview

CLI tool to export a Slack channel to CSV, JSON, and download associated files. Supports resumability after interruption or API timeouts.

## Output Structure

```
downloads/<channel_name>/
├── messages.json        # Full raw payloads, array of messages (with thread replies inline)
├── messages.csv         # Flattened columns derived from the JSON
├── files/               # Downloaded attachments
└── .checkpoint.json     # Cursor state for resumability
```

## Key Decisions

| Concern | Approach |
|---------|----------|
| Auth | `--token` CLI arg, falls back to `SLACK_API_TOKEN` env var |
| Output | `--output` CLI arg, defaults to `downloads/<channel_name>` |
| Channel | Positional arg — channel ID or name (resolved via API) |
| Resume | `.checkpoint.json` stores `oldest_ts` (last fully-fetched timestamp) + file download progress |
| Threads | After fetching channel history, fetch replies for each threaded message; insert inline after parent |
| Files | Downloaded with auth header to `files/`, filename collision handled with suffix |
| Rate limits | Exponential backoff on 429s, respects `Retry-After` header |
| Interruption | Checkpoint saved after each page of history + after each file download |

## Dependencies (managed by `uv`)

- `slack_sdk` — official Slack client with built-in rate-limit handling
- `click` — CLI framework
- Standard lib: `csv`, `json`, `pathlib`, `time`

## CLI Interface

```
uv run export_slack.py <channel> [--token TOKEN] [--output ./path]
```

## Resume Flow

1. On start, check for `.checkpoint.json` in output dir
2. If present, resume fetching from the stored cursor/timestamp
3. Each page of messages fetched → checkpoint updated
4. File downloads tracked individually (list of completed file IDs)
5. Final export to CSV/JSON only after all messages + files are gathered
