"""Tests for local file format parsers."""

import pytest, json, tempfile, zipfile
from pathlib import Path
from datetime import datetime


# ---- Claude Code JSONL Parser Tests ----

class TestClaudeCodeParser:
    """Tests for parsing Claude Code session JSONL files."""

    def test_parse_basic_session(self, tmp_path):
        """Parse a minimal Claude Code session."""
        from ai_convos.cli import parse_claude_code

        session_dir = tmp_path / ".claude" / "projects" / "-test-project"
        session_dir.mkdir(parents=True)

        jsonl = session_dir / "session-123.jsonl"
        jsonl.write_text("\n".join([
            json.dumps({"type": "system", "timestamp": "2024-01-01T00:00:00Z", "cwd": "/test", "gitBranch": "main"}),
            json.dumps({"type": "human", "timestamp": "2024-01-01T00:00:01Z", "message": {"content": "Hello"}}),
            json.dumps({"type": "assistant", "timestamp": "2024-01-01T00:00:02Z", "message": {"content": [{"type": "text", "text": "Hi there!"}]}}),
        ]))

        result = parse_claude_code(tmp_path / ".claude" / "projects")

        assert len(result.convs) == 1
        assert len(result.msgs) == 2
        assert result.convs[0]["cwd"] == "/test"
        assert result.convs[0]["git_branch"] == "main"
        assert result.msgs[0]["role"] == "human"
        assert result.msgs[1]["role"] == "assistant"

    def test_parse_thinking_blocks(self, tmp_path):
        """Parse session with thinking blocks."""
        from ai_convos.cli import parse_claude_code

        session_dir = tmp_path / ".claude" / "projects" / "-test"
        session_dir.mkdir(parents=True)

        jsonl = session_dir / "session.jsonl"
        jsonl.write_text("\n".join([
            json.dumps({"type": "assistant", "timestamp": "2024-01-01T00:00:00Z", "message": {"content": [
                {"type": "thinking", "thinking": "Let me think about this..."},
                {"type": "text", "text": "Here's my answer."}
            ]}}),
        ]))

        result = parse_claude_code(tmp_path / ".claude" / "projects")

        assert len(result.msgs) == 1
        assert result.msgs[0]["thinking"] == "Let me think about this..."
        assert result.msgs[0]["content"] == "Here's my answer."

    def test_parse_tool_calls(self, tmp_path):
        """Parse session with tool calls."""
        from ai_convos.cli import parse_claude_code

        session_dir = tmp_path / ".claude" / "projects" / "-test"
        session_dir.mkdir(parents=True)

        jsonl = session_dir / "session.jsonl"
        jsonl.write_text("\n".join([
            json.dumps({"type": "assistant", "timestamp": "2024-01-01T00:00:00Z", "message": {"content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/test.py"}},
                {"type": "text", "text": "Let me read that file."}
            ]}}),
        ]))

        result = parse_claude_code(tmp_path / ".claude" / "projects")

        assert len(result.tools) == 1
        assert result.tools[0]["tool_name"] == "Read"

    def test_parse_file_edits(self, tmp_path):
        """Parse session with file edits."""
        from ai_convos.cli import parse_claude_code

        session_dir = tmp_path / ".claude" / "projects" / "-test"
        session_dir.mkdir(parents=True)

        jsonl = session_dir / "session.jsonl"
        jsonl.write_text("\n".join([
            json.dumps({"type": "assistant", "timestamp": "2024-01-01T00:00:00Z", "message": {"content": [
                {"type": "tool_use", "name": "Write", "input": {"file_path": "/test.py", "content": "print('hello')"}},
                {"type": "text", "text": "Created file."}
            ]}}),
        ]))

        result = parse_claude_code(tmp_path / ".claude" / "projects")

        assert len(result.edits) == 1
        assert result.edits[0]["file_path"] == "/test.py"
        assert result.edits[0]["edit_type"] == "write"

    def test_empty_session_skipped(self, tmp_path):
        """Empty sessions (no messages) are skipped."""
        from ai_convos.cli import parse_claude_code

        session_dir = tmp_path / ".claude" / "projects" / "-test"
        session_dir.mkdir(parents=True)

        jsonl = session_dir / "session.jsonl"
        jsonl.write_text(json.dumps({"type": "system", "timestamp": "2024-01-01T00:00:00Z"}))

        result = parse_claude_code(tmp_path / ".claude" / "projects")

        assert len(result.convs) == 0


# ---- Codex Parser Tests ----

class TestCodexParser:
    """Tests for parsing Codex session JSONL files."""

    def test_parse_basic_session(self, tmp_path):
        """Parse a minimal Codex session."""
        from ai_convos.cli import parse_codex

        sessions_dir = tmp_path / ".codex" / "sessions" / "2024" / "01"
        sessions_dir.mkdir(parents=True)

        jsonl = sessions_dir / "session-123.jsonl"
        jsonl.write_text("\n".join([
            json.dumps({"type": "session_meta", "timestamp": "2024-01-01T00:00:00Z", "payload": {"cwd": "/test", "model_provider": "openai"}}),
            json.dumps({"type": "response_item", "timestamp": "2024-01-01T00:00:01Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hello"}]}}),
            json.dumps({"type": "response_item", "timestamp": "2024-01-01T00:00:02Z", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hi!"}]}}),
        ]))

        result = parse_codex(tmp_path / ".codex")

        assert len(result.convs) == 1
        assert len(result.msgs) == 2
        assert result.convs[0]["cwd"] == "/test"
        assert result.convs[0]["model"] == "openai"

    def test_parse_function_calls(self, tmp_path):
        """Parse Codex session with function calls."""
        from ai_convos.cli import parse_codex

        sessions_dir = tmp_path / ".codex" / "sessions"
        sessions_dir.mkdir(parents=True)

        jsonl = sessions_dir / "session.jsonl"
        jsonl.write_text("\n".join([
            json.dumps({"type": "session_meta", "timestamp": "2024-01-01T00:00:00Z", "payload": {}}),
            json.dumps({"type": "response_item", "timestamp": "2024-01-01T00:00:01Z", "payload": {"type": "function_call", "name": "shell", "arguments": {"command": "ls -la"}}}),
            json.dumps({"type": "response_item", "timestamp": "2024-01-01T00:00:02Z", "payload": {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "Done"}]}}),
        ]))

        result = parse_codex(tmp_path / ".codex")

        assert len(result.tools) == 1
        assert result.tools[0]["tool_name"] == "shell"

    def test_skip_system_messages(self, tmp_path):
        """System and developer messages are skipped."""
        from ai_convos.cli import parse_codex

        sessions_dir = tmp_path / ".codex" / "sessions"
        sessions_dir.mkdir(parents=True)

        jsonl = sessions_dir / "session.jsonl"
        jsonl.write_text("\n".join([
            json.dumps({"type": "session_meta", "timestamp": "2024-01-01T00:00:00Z", "payload": {}}),
            json.dumps({"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "text", "text": "System prompt"}]}}),
            json.dumps({"type": "response_item", "payload": {"type": "message", "role": "system", "content": [{"type": "text", "text": "Instructions"}]}}),
            json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hello"}]}}),
        ]))

        result = parse_codex(tmp_path / ".codex")

        assert len(result.msgs) == 1
        assert result.msgs[0]["role"] == "user"


# ---- ChatGPT Export Parser Tests ----

class TestChatGPTExportParser:
    """Tests for parsing ChatGPT export files."""

    def test_parse_json_export(self, tmp_path):
        """Parse ChatGPT JSON export."""
        from ai_convos.cli import parse_chatgpt

        export = tmp_path / "conversations.json"
        export.write_text(json.dumps([{
            "id": "conv-123",
            "title": "Test Chat",
            "create_time": 1704067200,
            "update_time": 1704067200,
            "mapping": {
                "node1": {"message": {"author": {"role": "user"}, "content": {"parts": ["Hello"]}, "create_time": 1704067200}},
                "node2": {"message": {"author": {"role": "assistant"}, "content": {"parts": ["Hi!"]}, "create_time": 1704067201}},
            }
        }]))

        result = parse_chatgpt(export)

        assert len(result.convs) == 1
        assert result.convs[0]["title"] == "Test Chat"
        assert len(result.msgs) == 2

    def test_parse_zip_export(self, tmp_path):
        """Parse ChatGPT ZIP export."""
        from ai_convos.cli import parse_chatgpt

        export_data = json.dumps([{
            "id": "conv-456",
            "title": "Zipped Chat",
            "mapping": {
                "n1": {"message": {"author": {"role": "user"}, "content": {"parts": ["Test"]}}}
            }
        }])

        zip_path = tmp_path / "export.zip"
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr("conversations.json", export_data)

        result = parse_chatgpt(zip_path)

        assert len(result.convs) == 1
        assert result.convs[0]["title"] == "Zipped Chat"

    def test_parse_with_attachments(self, tmp_path):
        """Parse ChatGPT export with image attachments."""
        from ai_convos.cli import parse_chatgpt

        export = tmp_path / "conversations.json"
        export.write_text(json.dumps([{
            "id": "conv-789",
            "mapping": {
                "n1": {"message": {
                    "author": {"role": "user"},
                    "content": {"parts": [
                        {"content_type": "image_asset_pointer", "asset_pointer": "file://image.png", "name": "screenshot.png"}
                    ]}
                }}
            }
        }]))

        result = parse_chatgpt(export)

        assert len(result.attachs) == 1
        assert result.attachs[0]["filename"] == "screenshot.png"

    def test_parse_with_gizmo(self, tmp_path):
        """Parse ChatGPT export with custom GPT (gizmo)."""
        from ai_convos.cli import parse_chatgpt

        export = tmp_path / "conversations.json"
        export.write_text(json.dumps([{
            "id": "conv-gizmo",
            "gizmo_id": "g-abc123",
            "mapping": {}
        }]))

        result = parse_chatgpt(export)

        assert result.convs[0]["project_id"] == "g-abc123"


# ---- Claude Export Parser Tests ----

class TestClaudeExportParser:
    """Tests for parsing Claude.ai export files."""

    def test_parse_basic_export(self, tmp_path):
        """Parse Claude JSON export."""
        from ai_convos.cli import parse_claude

        export = tmp_path / "conversations.json"
        export.write_text(json.dumps([{
            "uuid": "conv-123",
            "name": "Test Chat",
            "created_at": "2024-01-01T00:00:00Z",
            "chat_messages": [
                {"uuid": "msg-1", "sender": "human", "text": "Hello"},
                {"uuid": "msg-2", "sender": "assistant", "text": "Hi there!"},
            ]
        }]))

        result = parse_claude(export)

        assert len(result.convs) == 1
        assert result.convs[0]["title"] == "Test Chat"
        assert len(result.msgs) == 2

    def test_parse_with_attachments(self, tmp_path):
        """Parse Claude export with attachments."""
        from ai_convos.cli import parse_claude

        export = tmp_path / "conversations.json"
        export.write_text(json.dumps([{
            "uuid": "conv-456",
            "chat_messages": [
                {
                    "uuid": "msg-1",
                    "sender": "human",
                    "text": "Here's a file",
                    "attachments": [
                        {"file_name": "doc.pdf", "file_type": "application/pdf", "file_size": 1024}
                    ]
                }
            ]
        }]))

        result = parse_claude(export)

        assert len(result.attachs) == 1
        assert result.attachs[0]["filename"] == "doc.pdf"

    def test_parse_content_blocks(self, tmp_path):
        """Parse Claude export with content blocks format."""
        from ai_convos.cli import parse_claude

        export = tmp_path / "conversations.json"
        export.write_text(json.dumps([{
            "uuid": "conv-789",
            "chat_messages": [
                {
                    "uuid": "msg-1",
                    "sender": "assistant",
                    "content": [
                        {"type": "text", "text": "Part 1"},
                        {"type": "text", "text": "Part 2"},
                    ]
                }
            ]
        }]))

        result = parse_claude(export)

        # Content blocks should be joined
        assert "Part 1" in result.msgs[0]["content"]


# ---- ID Generation Tests ----

class TestIDGeneration:
    """Tests for consistent ID generation."""

    def test_id_length(self):
        """Generated IDs are 16 characters."""
        from ai_convos.cli import gen_id
        assert len(gen_id("test", "123")) == 16

    def test_id_hex(self):
        """Generated IDs are valid hex."""
        from ai_convos.cli import gen_id
        id_ = gen_id("test", "123")
        int(id_, 16)  # should not raise

    def test_id_consistent_across_syncs(self):
        """Same file path produces same conversation ID."""
        from ai_convos.cli import gen_id

        # Simulating multiple syncs of same session
        path = "/Users/test/.claude/projects/-test/session-abc.jsonl"
        id1 = gen_id("claude-code", path)
        id2 = gen_id("claude-code", path)
        assert id1 == id2


# ---- Timestamp Parsing Tests ----

class TestTimestampParsing:
    """Tests for timestamp parsing utilities."""

    def test_epoch_to_datetime(self):
        """Parse Unix epoch timestamp."""
        from ai_convos.cli import ts_from_epoch
        dt = ts_from_epoch(1704067200)  # 2024-01-01 00:00:00 UTC
        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 1

    def test_epoch_none(self):
        """None epoch returns None."""
        from ai_convos.cli import ts_from_epoch
        assert ts_from_epoch(None) is None

    def test_iso_to_datetime(self):
        """Parse ISO format timestamp."""
        from ai_convos.cli import ts_from_iso
        dt = ts_from_iso("2024-01-01T12:00:00Z")
        assert dt.year == 2024
        assert dt.hour == 12

    def test_iso_none(self):
        """None ISO returns None."""
        from ai_convos.cli import ts_from_iso
        assert ts_from_iso(None) is None
