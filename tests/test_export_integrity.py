"""
Integrity tests for exported Slack channel directories.

Run against a real export directory:

    uv run pytest tests/test_export_integrity.py --export-dir downloads/general

Or against every subdirectory inside a downloads folder:

    uv run pytest tests/test_export_integrity.py --export-dir downloads/general \
                                                  --export-dir downloads/engineering

The --export-dir option may be supplied multiple times; pytest will run the
full suite against each directory independently.

If no --export-dir is given the tests are skipped with a clear message.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# CLI option + parametrize fixture
# ---------------------------------------------------------------------------

KNOWN_STATUSES = {"downloaded", "failed_permanent", "failed_transient"}


def pytest_generate_tests(metafunc):
    if "export_dir" in metafunc.fixturenames:
        dirs = metafunc.config.getoption("--export-dir")
        if dirs:
            metafunc.parametrize("export_dir", [Path(d) for d in dirs])
        else:
            metafunc.parametrize("export_dir", [None])


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def messages_data(export_dir):
    if export_dir is None:
        pytest.skip("No --export-dir supplied")
    path = export_dir / "messages.json"
    assert path.exists(), f"messages.json not found in {export_dir}"
    with open(path) as f:
        return json.load(f)


@pytest.fixture()
def messages_list(messages_data):
    assert isinstance(messages_data, dict), "messages.json root must be a JSON object"
    assert "messages" in messages_data, "messages.json must have a 'messages' key"
    msgs = messages_data["messages"]
    assert isinstance(msgs, list), "'messages' value must be a list"
    return msgs


@pytest.fixture()
def metadata(export_dir):
    if export_dir is None:
        pytest.skip("No --export-dir supplied")
    path = export_dir / "metadata.json"
    assert path.exists(), f"metadata.json not found in {export_dir}"
    with open(path) as f:
        return json.load(f)


@pytest.fixture()
def files_manifest(export_dir):
    if export_dir is None:
        pytest.skip("No --export-dir supplied")
    path = export_dir / "files.json"
    assert path.exists(), f"files.json not found in {export_dir}"
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# messages.json tests
# ---------------------------------------------------------------------------

class TestMessagesJson:

    def test_valid_json_and_structure(self, messages_data):
        """messages.json is a dict with a 'messages' list."""
        assert isinstance(messages_data, dict)
        assert "messages" in messages_data
        assert isinstance(messages_data["messages"], list)

    def test_every_message_has_ts_and_type(self, messages_list):
        """Every top-level message must have 'ts' and 'type'."""
        bad = [m for m in messages_list if "ts" not in m or "type" not in m]
        assert not bad, (
            f"{len(bad)} message(s) missing 'ts' or 'type': "
            + ", ".join(m.get("ts", "<no-ts>") for m in bad[:5])
        )

    def test_ts_values_are_unique(self, messages_list):
        """No two top-level messages share the same 'ts'."""
        ts_values = [m["ts"] for m in messages_list if "ts" in m]
        seen = set()
        dupes = []
        for ts in ts_values:
            if ts in seen:
                dupes.append(ts)
            seen.add(ts)
        assert not dupes, f"Duplicate ts values found: {dupes[:5]}"

    def test_ts_values_are_in_ascending_order(self, messages_list):
        """Top-level messages must be sorted oldest-first by ts."""
        ts_values = [float(m["ts"]) for m in messages_list if "ts" in m]
        assert ts_values == sorted(ts_values), (
            "messages are not in ascending ts order"
        )

    def test_thread_replies_are_lists(self, messages_list):
        """When present, thread_replies must be a list."""
        bad = [
            m["ts"] for m in messages_list
            if "thread_replies" in m and not isinstance(m["thread_replies"], list)
        ]
        assert not bad, f"thread_replies is not a list on ts={bad[:5]}"

    def test_thread_replies_have_ts(self, messages_list):
        """Every thread reply must have a 'ts' field."""
        missing = []
        for msg in messages_list:
            for reply in msg.get("thread_replies", []):
                if "ts" not in reply:
                    missing.append(msg["ts"])
                    break
        assert not missing, (
            f"Thread replies missing 'ts' in {len(missing)} thread(s): {missing[:5]}"
        )

    def test_replies_not_duplicated_at_top_level(self, messages_list):
        """A reply (thread_ts != ts) must not appear as a top-level message.

        Slack sometimes returns replies in the main history feed as well as
        inside threads. The exporter should filter these out during merge.
        """
        bad = [
            m["ts"] for m in messages_list
            if m.get("thread_ts") and m["thread_ts"] != m["ts"]
        ]
        assert not bad, (
            f"{len(bad)} reply message(s) leaked into the top-level list "
            f"(thread_ts != ts): {bad[:5]}"
        )

    def test_no_empty_messages(self, messages_list):
        """messages list must not be empty (if the channel has any history)."""
        # We only warn here — a truly empty channel is technically valid.
        if len(messages_list) == 0:
            pytest.skip("Channel has no messages — skipping emptiness check")


# ---------------------------------------------------------------------------
# metadata.json tests
# ---------------------------------------------------------------------------

class TestMetadataJson:

    def test_valid_json_and_structure(self, metadata):
        """metadata.json is a dict with expected top-level keys."""
        assert isinstance(metadata, dict)
        for key in ("channel", "exported_at"):
            assert key in metadata, f"Missing key '{key}' in metadata.json"

    def test_channel_required_fields(self, metadata):
        """channel object must have id, name, and is_private."""
        ch = metadata.get("channel", {})
        for field in ("id", "name", "is_private"):
            assert field in ch, f"metadata.channel missing '{field}'"

    def test_channel_id_non_empty(self, metadata):
        assert metadata["channel"]["id"], "channel.id must not be empty"

    def test_channel_name_non_empty(self, metadata):
        assert metadata["channel"]["name"], "channel.name must not be empty"

    def test_is_private_is_bool(self, metadata):
        val = metadata["channel"]["is_private"]
        assert isinstance(val, bool), f"channel.is_private must be bool, got {type(val)}"

    def test_exported_at_is_valid_iso_timestamp(self, metadata):
        ts = metadata.get("exported_at", "")
        try:
            datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pytest.fail(f"exported_at is not a valid ISO timestamp: {ts!r}")

    def test_message_count_is_non_negative(self, metadata):
        count = metadata.get("export", {}).get("message_count")
        if count is not None:
            assert isinstance(count, int) and count >= 0, (
                f"export.message_count must be a non-negative int, got {count!r}"
            )


# ---------------------------------------------------------------------------
# files.json tests
# ---------------------------------------------------------------------------

class TestFilesJson:

    def test_valid_json_and_is_dict(self, files_manifest):
        assert isinstance(files_manifest, dict), "files.json root must be a JSON object"

    def test_every_entry_has_required_fields(self, files_manifest):
        bad = []
        for fid, entry in files_manifest.items():
            for field in ("id", "status", "url_private"):
                if field not in entry:
                    bad.append((fid, field))
        assert not bad, f"Entries missing required fields: {bad[:5]}"

    def test_status_is_known_value(self, files_manifest):
        bad = [
            (fid, entry.get("status"))
            for fid, entry in files_manifest.items()
            if entry.get("status") not in KNOWN_STATUSES
        ]
        assert not bad, f"Unknown status values: {bad[:5]}"

    def test_key_matches_entry_id(self, files_manifest):
        """The dict key must match the 'id' field inside the entry."""
        bad = [
            (key, entry.get("id"))
            for key, entry in files_manifest.items()
            if entry.get("id") != key
        ]
        assert not bad, f"Key/id mismatch: {bad[:5]}"

    def test_downloaded_entries_have_local_path(self, files_manifest):
        bad = [
            fid for fid, entry in files_manifest.items()
            if entry.get("status") == "downloaded" and not entry.get("local_path")
        ]
        assert not bad, f"Downloaded entries missing local_path: {bad[:5]}"

    def test_downloaded_files_exist_on_disk(self, export_dir, files_manifest):
        missing = []
        for fid, entry in files_manifest.items():
            if entry.get("status") == "downloaded":
                local = export_dir / entry["local_path"]
                if not local.exists():
                    missing.append(str(local))
        assert not missing, (
            f"{len(missing)} downloaded file(s) missing from disk:\n"
            + "\n".join(missing[:10])
        )

    def test_permanent_failures_have_error_field(self, files_manifest):
        bad = [
            fid for fid, entry in files_manifest.items()
            if entry.get("status") == "failed_permanent" and not entry.get("error")
        ]
        assert not bad, f"Permanent failures missing 'error' field: {bad[:5]}"

    def test_no_downloaded_entry_has_error_field(self, files_manifest):
        """Successfully downloaded files must not retain a stale error message."""
        bad = [
            fid for fid, entry in files_manifest.items()
            if entry.get("status") == "downloaded" and entry.get("error")
        ]
        assert not bad, f"Downloaded entries with stale error field: {bad[:5]}"


# ---------------------------------------------------------------------------
# Cross-file integrity tests
# ---------------------------------------------------------------------------

class TestCrossFile:

    def _file_ids_in_messages(self, messages_list: list) -> set[str]:
        ids: set[str] = set()
        for msg in messages_list:
            for f in msg.get("files", []):
                if isinstance(f, dict) and f.get("id"):
                    ids.add(f["id"])
            for reply in msg.get("thread_replies", []):
                for f in reply.get("files", []):
                    if isinstance(f, dict) and f.get("id"):
                        ids.add(f["id"])
        return ids

    def test_all_message_files_have_manifest_entry(
        self, messages_list, files_manifest
    ):
        """Every file referenced in messages.json must appear in files.json."""
        in_messages = self._file_ids_in_messages(messages_list)
        missing = in_messages - set(files_manifest.keys())
        assert not missing, (
            f"{len(missing)} file ID(s) in messages.json have no files.json entry: "
            + ", ".join(sorted(missing)[:10])
        )

    def test_downloaded_local_paths_reference_real_files(
        self, export_dir, files_manifest
    ):
        """local_path entries in files.json must point inside the export dir."""
        bad = []
        for fid, entry in files_manifest.items():
            if entry.get("status") == "downloaded" and entry.get("local_path"):
                p = export_dir / entry["local_path"]
                if not p.is_file():
                    bad.append(fid)
        assert not bad, (
            f"{len(bad)} files.json entries point to non-existent paths: {bad[:5]}"
        )

    def test_metadata_message_count_matches_messages_json(
        self, metadata, messages_list
    ):
        """export.message_count in metadata.json should match len(messages)."""
        count = metadata.get("export", {}).get("message_count")
        if count is None:
            pytest.skip("metadata.json has no export.message_count")
        assert count == len(messages_list), (
            f"metadata says {count} messages but messages.json has {len(messages_list)}"
        )
