"""
Unit tests for convert_to_viewer.py.

These tests use fully synthetic fixtures — no real export directory required.
Run with:

    uv run pytest tests/test_convert_to_viewer.py -v
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

# Import the module under test.  Because it lives at the repo root we add it
# to sys.path via conftest or rely on pytest's rootdir discovery.
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from convert_to_viewer import main, convert_channel, _trim_message


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _make_channel_dir(
    tmp_path: Path,
    *,
    channel_id: str = "CABC123",
    channel_name: str = "general",
    messages: list | None = None,
    users: dict | None = None,
    files_manifest: dict | None = None,
    is_private: bool = False,
    include_files_dir: bool = True,
) -> Path:
    """Create a minimal channel export directory under tmp_path."""
    d = tmp_path / channel_name
    d.mkdir(parents=True, exist_ok=True)

    if messages is None:
        messages = [
            {
                "user": "U001",
                "type": "message",
                "ts": "1700000000.000100",
                "text": "Hello world",
                "reactions": [{"name": "wave", "users": ["U002"], "count": 1}],
            }
        ]

    _write_json(d / "messages.json", {
        "messages": messages,
        "users": users or {"U001": "Alice"},
    })

    _write_json(d / "metadata.json", {
        "exported_at": "2026-01-01T00:00:00Z",
        "channel": {
            "id": channel_id,
            "name": channel_name,
            "name_normalized": channel_name,
            "created_at": "2020-01-01T00:00:00Z",
            "creator_id": "U001",
            "is_private": is_private,
            "is_archived": False,
            "is_general": False,
            "member_count": None,
            "topic": None,
            "purpose": "Test channel",
        },
        "export": {"message_count": len(messages)},
    })

    if files_manifest is not None:
        _write_json(d / "files.json", files_manifest)
    else:
        _write_json(d / "files.json", {})

    if include_files_dir:
        (d / "files").mkdir()

    return d


# ---------------------------------------------------------------------------
# _trim_message
# ---------------------------------------------------------------------------

class TestTrimMessage:

    def test_drops_blocks(self):
        msg = {"ts": "1.0", "text": "hi", "blocks": [{"type": "rich_text"}]}
        out = _trim_message(msg)
        assert "blocks" not in out

    def test_drops_client_msg_id(self):
        msg = {"ts": "1.0", "client_msg_id": "abc-123", "text": "hi"}
        out = _trim_message(msg)
        assert "client_msg_id" not in out

    def test_drops_team(self):
        msg = {"ts": "1.0", "team": "T001", "text": "hi"}
        out = _trim_message(msg)
        assert "team" not in out

    def test_keeps_text_user_ts_type(self):
        msg = {"ts": "1.0", "user": "U001", "type": "message", "text": "hi"}
        out = _trim_message(msg)
        assert out["ts"] == "1.0"
        assert out["user"] == "U001"
        assert out["type"] == "message"
        assert out["text"] == "hi"

    def test_reactions_trimmed_to_name_and_count(self):
        msg = {
            "ts": "1.0",
            "reactions": [
                {"name": "wave", "users": ["U001", "U002"], "count": 2},
                {"name": "tada", "users": ["U003"], "count": 1},
            ],
        }
        out = _trim_message(msg)
        assert out["reactions"] == [
            {"name": "wave", "count": 2},
            {"name": "tada", "count": 1},
        ]

    def test_reactions_count_inferred_from_users_if_absent(self):
        msg = {
            "ts": "1.0",
            "reactions": [{"name": "wave", "users": ["U001", "U002"]}],
        }
        out = _trim_message(msg)
        assert out["reactions"][0]["count"] == 2

    def test_reactions_without_name_are_dropped(self):
        msg = {
            "ts": "1.0",
            "reactions": [{"users": ["U001"]}, {"name": "ok", "count": 1}],
        }
        out = _trim_message(msg)
        assert len(out["reactions"]) == 1
        assert out["reactions"][0]["name"] == "ok"

    def test_files_trimmed_to_safe_fields(self):
        msg = {
            "ts": "1.0",
            "files": [
                {
                    "id": "F001",
                    "name": "shot.png",
                    "title": "Screenshot",
                    "mimetype": "image/png",
                    "filetype": "png",
                    "local_path": "files/F001.png",
                    "url_private": "https://files.slack.com/x",
                    "size": 12345,
                    "created": 1700000000,
                }
            ],
        }
        out = _trim_message(msg)
        f = out["files"][0]
        assert f["id"] == "F001"
        assert f["local_path"] == "files/F001.png"
        assert "url_private" not in f
        assert "size" not in f
        assert "created" not in f

    def test_thread_replies_trimmed_recursively(self):
        msg = {
            "ts": "1.0",
            "thread_replies": [
                {
                    "ts": "1.1",
                    "user": "U002",
                    "text": "reply",
                    "blocks": [{"type": "rich_text"}],
                    "team": "T001",
                    "reactions": [{"name": "+1", "users": ["U001"], "count": 1}],
                }
            ],
        }
        out = _trim_message(msg)
        reply = out["thread_replies"][0]
        assert "blocks" not in reply
        assert "team" not in reply
        assert reply["reactions"] == [{"name": "+1", "count": 1}]

    def test_no_mutation_of_input(self):
        original = {"ts": "1.0", "text": "hi", "blocks": [{}], "team": "T001"}
        import copy
        before = copy.deepcopy(original)
        _trim_message(original)
        assert original == before


# ---------------------------------------------------------------------------
# convert_channel
# ---------------------------------------------------------------------------

class TestConvertChannel:

    def test_creates_channel_json(self, tmp_path):
        src = _make_channel_dir(tmp_path, channel_id="CABC1", channel_name="general")
        out = tmp_path / "output"
        result = convert_channel(src, out, copy_files=False)
        assert result == ("CABC1", "general")
        assert (out / "CABC1.json").is_file()

    def test_channel_json_structure(self, tmp_path):
        src = _make_channel_dir(tmp_path, channel_id="CABC2", channel_name="eng")
        out = tmp_path / "output"
        convert_channel(src, out, copy_files=False)
        data = json.loads((out / "CABC2.json").read_text())
        assert set(data.keys()) == {"fileIndex", "messages", "users", "private"}

    def test_messages_sorted_by_ts(self, tmp_path):
        msgs = [
            {"user": "U1", "type": "message", "ts": "1700000002.0", "text": "B"},
            {"user": "U1", "type": "message", "ts": "1700000001.0", "text": "A"},
            {"user": "U1", "type": "message", "ts": "1700000003.0", "text": "C"},
        ]
        src = _make_channel_dir(tmp_path, channel_id="CSORT", messages=msgs)
        out = tmp_path / "output"
        convert_channel(src, out, copy_files=False)
        data = json.loads((out / "CSORT.json").read_text())
        tss = [float(m["ts"]) for m in data["messages"]]
        assert tss == sorted(tss)

    def test_leaked_replies_excluded(self, tmp_path):
        """Messages where thread_ts != ts must not appear at top level."""
        msgs = [
            {"user": "U1", "type": "message", "ts": "1700000001.0",
             "thread_ts": "1700000001.0", "text": "parent"},
            # leaked reply — should be excluded
            {"user": "U2", "type": "message", "ts": "1700000002.0",
             "thread_ts": "1700000001.0", "text": "leaked reply"},
        ]
        src = _make_channel_dir(tmp_path, channel_id="CLEAK", messages=msgs)
        out = tmp_path / "output"
        convert_channel(src, out, copy_files=False)
        data = json.loads((out / "CLEAK.json").read_text())
        assert len(data["messages"]) == 1
        assert data["messages"][0]["ts"] == "1700000001.0"

    def test_users_preserved(self, tmp_path):
        src = _make_channel_dir(
            tmp_path, channel_id="CUSER",
            users={"U001": "Alice", "U002": "Bob"},
        )
        out = tmp_path / "output"
        convert_channel(src, out, copy_files=False)
        data = json.loads((out / "CUSER.json").read_text())
        assert data["users"] == {"U001": "Alice", "U002": "Bob"}

    def test_private_flag_propagated(self, tmp_path):
        src = _make_channel_dir(tmp_path, channel_id="CPRIV", is_private=True)
        out = tmp_path / "output"
        convert_channel(src, out, copy_files=False)
        data = json.loads((out / "CPRIV.json").read_text())
        assert data["private"] is True

    def test_file_index_built_from_downloaded_only(self, tmp_path):
        manifest = {
            "F001": {"id": "F001", "status": "downloaded", "local_path": "files/F001.png",
                     "filetype": "png", "mimetype": "image/png", "name": "a.png",
                     "title": "a", "url_private": "https://x"},
            "F002": {"id": "F002", "status": "failed_permanent", "local_path": None,
                     "filetype": "png", "mimetype": "image/png", "name": "b.png",
                     "title": "b", "url_private": "https://y", "error": "gone"},
            "F003": {"id": "F003", "status": "failed_transient", "local_path": None,
                     "filetype": "pdf", "mimetype": "application/pdf", "name": "c.pdf",
                     "title": "c", "url_private": "https://z"},
        }
        src = _make_channel_dir(tmp_path, channel_id="CFILE", files_manifest=manifest)
        # Create the actual file so symlink/copy doesn't warn
        (src / "files" / "F001.png").write_bytes(b"fake")
        out = tmp_path / "output"
        convert_channel(src, out, copy_files=False)
        data = json.loads((out / "CFILE.json").read_text())
        assert list(data["fileIndex"].keys()) == ["F001"]
        assert data["fileIndex"]["F001"] == "F001.png"

    def test_file_index_value_is_basename_only(self, tmp_path):
        manifest = {
            "F010": {"id": "F010", "status": "downloaded",
                     "local_path": "files/F010.jpg",
                     "filetype": "jpg", "mimetype": "image/jpeg", "name": "photo.jpg",
                     "title": "Photo", "url_private": "https://x"},
        }
        src = _make_channel_dir(tmp_path, channel_id="CBASE", files_manifest=manifest)
        (src / "files" / "F010.jpg").write_bytes(b"fake")
        out = tmp_path / "output"
        convert_channel(src, out, copy_files=False)
        data = json.loads((out / "CBASE.json").read_text())
        assert "/" not in data["fileIndex"]["F010"]

    def test_files_symlinked_into_output(self, tmp_path):
        manifest = {
            "F099": {"id": "F099", "status": "downloaded",
                     "local_path": "files/F099.png",
                     "filetype": "png", "mimetype": "image/png", "name": "img.png",
                     "title": "img", "url_private": "https://x"},
        }
        src = _make_channel_dir(tmp_path, channel_id="CLINK", files_manifest=manifest)
        real_file = src / "files" / "F099.png"
        real_file.write_bytes(b"\x89PNG")
        out = tmp_path / "output"
        convert_channel(src, out, copy_files=False)
        link = out / "files" / "F099.png"
        assert link.exists(), "Symlink/copy should exist in output files/"
        assert link.read_bytes() == b"\x89PNG"

    def test_files_copied_when_copy_flag_set(self, tmp_path):
        manifest = {
            "F100": {"id": "F100", "status": "downloaded",
                     "local_path": "files/F100.png",
                     "filetype": "png", "mimetype": "image/png", "name": "img.png",
                     "title": "img", "url_private": "https://x"},
        }
        src = _make_channel_dir(tmp_path, channel_id="CCOPY", files_manifest=manifest)
        (src / "files" / "F100.png").write_bytes(b"data")
        out = tmp_path / "output"
        convert_channel(src, out, copy_files=True)
        dst = out / "files" / "F100.png"
        assert dst.is_file()
        assert not dst.is_symlink()

    def test_missing_messages_json_returns_none(self, tmp_path, capsys):
        src = _make_channel_dir(tmp_path, channel_id="CMISS")
        (src / "messages.json").unlink()
        out = tmp_path / "output"
        result = convert_channel(src, out, copy_files=False)
        assert result is None

    def test_missing_metadata_json_returns_none(self, tmp_path, capsys):
        src = _make_channel_dir(tmp_path, channel_id="CMETA")
        (src / "metadata.json").unlink()
        out = tmp_path / "output"
        result = convert_channel(src, out, copy_files=False)
        assert result is None

    def test_empty_files_manifest_gives_empty_file_index(self, tmp_path):
        src = _make_channel_dir(tmp_path, channel_id="CEMPTY", files_manifest={})
        out = tmp_path / "output"
        convert_channel(src, out, copy_files=False)
        data = json.loads((out / "CEMPTY.json").read_text())
        assert data["fileIndex"] == {}

    def test_already_present_files_not_duplicated(self, tmp_path):
        """Running convert_channel twice should not raise and file count stays the same."""
        manifest = {
            "F200": {"id": "F200", "status": "downloaded",
                     "local_path": "files/F200.png",
                     "filetype": "png", "mimetype": "image/png", "name": "x.png",
                     "title": "x", "url_private": "https://x"},
        }
        src = _make_channel_dir(tmp_path, channel_id="CDUP", files_manifest=manifest)
        (src / "files" / "F200.png").write_bytes(b"x")
        out = tmp_path / "output"
        convert_channel(src, out, copy_files=True)
        convert_channel(src, out, copy_files=True)  # second run
        files = list((out / "files").iterdir())
        assert len(files) == 1


# ---------------------------------------------------------------------------
# CLI (via Click test runner)
# ---------------------------------------------------------------------------

class TestCli:

    def test_basic_invocation(self, tmp_path):
        src = _make_channel_dir(tmp_path, channel_id="CCLI1", channel_name="test")
        out = tmp_path / "out"
        runner = CliRunner()
        result = runner.invoke(main, [str(src), "--output", str(out)])
        assert result.exit_code == 0, result.output
        assert (out / "CCLI1.json").is_file()
        assert (out / "channels.json").is_file()

    def test_channels_json_written(self, tmp_path):
        src = _make_channel_dir(tmp_path, channel_id="CCLI2", channel_name="eng")
        out = tmp_path / "out"
        runner = CliRunner()
        runner.invoke(main, [str(src), "--output", str(out)])
        channels = json.loads((out / "channels.json").read_text())
        assert channels == {"CCLI2": "eng"}

    def test_multiple_channels_merged_into_channels_json(self, tmp_path):
        src1 = _make_channel_dir(tmp_path / "a", channel_id="CH001", channel_name="alpha")
        src2 = _make_channel_dir(tmp_path / "b", channel_id="CH002", channel_name="beta")
        out = tmp_path / "out"
        runner = CliRunner()
        result = runner.invoke(main, [str(src1), str(src2), "--output", str(out)])
        assert result.exit_code == 0, result.output
        channels = json.loads((out / "channels.json").read_text())
        assert channels == {"CH001": "alpha", "CH002": "beta"}

    def test_reruns_merge_without_clobbering(self, tmp_path):
        """Running the CLI twice (e.g. adding a second channel) preserves first."""
        src1 = _make_channel_dir(tmp_path / "a", channel_id="CH010", channel_name="one")
        src2 = _make_channel_dir(tmp_path / "b", channel_id="CH011", channel_name="two")
        out = tmp_path / "out"
        runner = CliRunner()
        runner.invoke(main, [str(src1), "--output", str(out)])
        runner.invoke(main, [str(src2), "--output", str(out)])
        channels = json.loads((out / "channels.json").read_text())
        assert "CH010" in channels
        assert "CH011" in channels

    def test_missing_messages_json_exits_nonzero(self, tmp_path):
        src = _make_channel_dir(tmp_path, channel_id="CXFAIL")
        (src / "messages.json").unlink()
        out = tmp_path / "out"
        runner = CliRunner()
        result = runner.invoke(main, [str(src), "--output", str(out)])
        assert result.exit_code != 0

    def test_copy_flag_produces_real_files(self, tmp_path):
        manifest = {
            "F300": {"id": "F300", "status": "downloaded",
                     "local_path": "files/F300.png",
                     "filetype": "png", "mimetype": "image/png", "name": "z.png",
                     "title": "z", "url_private": "https://x"},
        }
        src = _make_channel_dir(tmp_path, channel_id="CCPFLAG", files_manifest=manifest)
        (src / "files" / "F300.png").write_bytes(b"img")
        out = tmp_path / "out"
        runner = CliRunner()
        runner.invoke(main, [str(src), "--output", str(out), "--copy"])
        dst = out / "files" / "F300.png"
        assert dst.is_file()
        assert not dst.is_symlink()
