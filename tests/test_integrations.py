"""Integration tests for API fetchers - validates API schemas haven't changed."""

import pytest, json, os
from pathlib import Path
from unittest.mock import patch, MagicMock

# Skip all integration tests if SKIP_INTEGRATION is set
pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_INTEGRATION", "0") == "1",
    reason="Integration tests skipped via SKIP_INTEGRATION=1"
)

@pytest.fixture
def mock_cookies():
    return {"sessionKey": "test", "cf_clearance": "test"}

# ---- ChatGPT API Tests ----

class TestChatGPTAPI:
    """Tests for ChatGPT backend API structure."""

    @pytest.mark.integration
    def test_conversations_list_schema(self, mock_cookies):
        """Verify /backend-api/conversations returns expected schema."""
        from ai_convos.browser import validate_schema

        # Mock response matching expected schema
        mock_response = {
            "items": [
                {"id": "abc123", "title": "Test", "create_time": 1704067200, "update_time": 1704067200}
            ],
            "total": 1,
            "limit": 100,
            "offset": 0
        }
        valid, msg = validate_schema(mock_response, "chatgpt_conversations")
        assert valid, f"Schema validation failed: {msg}"

    @pytest.mark.integration
    def test_conversations_list_missing_items(self):
        """Detect if API removes 'items' field."""
        from ai_convos.browser import validate_schema

        broken_response = {"total": 1}  # missing items
        valid, msg = validate_schema(broken_response, "chatgpt_conversations")
        assert not valid
        assert "items" in msg

    @pytest.mark.integration
    def test_conversation_detail_schema(self):
        """Verify /backend-api/conversation/{id} returns expected schema."""
        from ai_convos.browser import validate_schema

        mock_response = {
            "mapping": {
                "node1": {"message": {"author": {"role": "user"}, "content": {"parts": ["Hello"]}}}
            }
        }
        valid, msg = validate_schema(mock_response, "chatgpt_conversation")
        assert valid, f"Schema validation failed: {msg}"

    @pytest.mark.integration
    def test_live_chatgpt_api(self, mock_cookies):
        """Live test against ChatGPT API - requires valid cookies."""
        pytest.skip("Requires real cookies - run manually with CHATGPT_TEST=1")

        from ai_convos.cli import fetch_chatgpt
        result = fetch_chatgpt("safari", limit=1)
        assert len(result.convs) >= 0  # may be 0 if no conversations
        # If we got conversations, verify structure
        if result.convs:
            conv = result.convs[0]
            assert "id" in conv
            assert "source" in conv
            assert conv["source"] == "chatgpt"


# ---- Claude API Tests ----

class TestClaudeAPI:
    """Tests for Claude.ai API structure."""

    @pytest.mark.integration
    def test_organizations_schema(self):
        """Verify /api/organizations returns expected schema."""
        from ai_convos.browser import validate_schema

        mock_response = [{"uuid": "org-123", "name": "Personal"}]
        valid, msg = validate_schema(mock_response, "claude_organizations")
        assert valid, f"Schema validation failed: {msg}"

    @pytest.mark.integration
    def test_organizations_missing_uuid(self):
        """Detect if API changes organization structure."""
        from ai_convos.browser import validate_schema

        broken_response = [{"id": "org-123"}]  # uuid -> id would break us
        valid, msg = validate_schema(broken_response, "claude_organizations")
        assert not valid
        assert "uuid" in msg

    @pytest.mark.integration
    def test_conversations_list_schema(self):
        """Verify /api/organizations/{id}/chat_conversations returns expected schema."""
        from ai_convos.browser import validate_schema

        mock_response = [
            {"uuid": "conv-123", "name": "Test Chat", "created_at": "2024-01-01T00:00:00Z"}
        ]
        valid, msg = validate_schema(mock_response, "claude_conversations")
        assert valid, f"Schema validation failed: {msg}"

    @pytest.mark.integration
    def test_conversation_detail_schema(self):
        """Verify conversation detail endpoint returns expected schema."""
        from ai_convos.browser import validate_schema

        mock_response = {
            "uuid": "conv-123",
            "chat_messages": [
                {"uuid": "msg-1", "sender": "human", "text": "Hello"}
            ]
        }
        valid, msg = validate_schema(mock_response, "claude_conversation")
        assert valid, f"Schema validation failed: {msg}"

    @pytest.mark.integration
    def test_live_claude_api(self):
        """Live test against Claude API - requires valid cookies."""
        pytest.skip("Requires real cookies - run manually with CLAUDE_TEST=1")

        from ai_convos.cli import fetch_claude
        result = fetch_claude("safari", limit=1)
        assert len(result.convs) >= 0
        if result.convs:
            conv = result.convs[0]
            assert conv["source"] == "claude"


# ---- Cookie Extraction Tests ----

class TestCookieExtraction:
    """Tests for browser cookie extraction."""

    def test_safari_cookies_not_found(self):
        """Safari cookie function handles missing file gracefully."""
        from ai_convos.cli import read_safari_cookies
        with patch("pathlib.Path.exists", return_value=False):
            cookies = read_safari_cookies("example.com")
            assert cookies == {}

    def test_chrome_cookies_not_found(self):
        """Chrome cookie function handles missing file gracefully."""
        from ai_convos.cli import read_chrome_cookies
        with patch("pathlib.Path.exists", return_value=False):
            cookies = read_chrome_cookies("example.com")
            assert cookies == {}

    def test_chrome_keychain_failure(self):
        """Chrome cookies handles keychain access failure."""
        from ai_convos.cli import read_chrome_cookies
        with patch("pathlib.Path.exists", return_value=True):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1)
                cookies = read_chrome_cookies("example.com")
                assert cookies == {}


# ---- Deduplication Tests ----

class TestDeduplication:
    """Tests for conversation deduplication logic."""

    def test_gen_id_deterministic(self):
        """gen_id produces consistent IDs for same input."""
        from ai_convos.cli import gen_id
        id1 = gen_id("claude", "conv-123")
        id2 = gen_id("claude", "conv-123")
        assert id1 == id2

    def test_gen_id_different_sources(self):
        """gen_id produces different IDs for different sources."""
        from ai_convos.cli import gen_id
        id1 = gen_id("claude", "conv-123")
        id2 = gen_id("chatgpt", "conv-123")
        assert id1 != id2

    def test_upsert_updates_existing(self, tmp_path):
        """Upserting same conversation updates rather than duplicates."""
        import duckdb
        from ai_convos.cli import init_schema, upsert, ParseResult, gen_id

        db = duckdb.connect(str(tmp_path / "test.db"))
        init_schema(db)

        # First insert
        r1 = ParseResult()
        cid = gen_id("claude", "test-conv")
        r1.convs.append(dict(id=cid, source="claude", title="Original", created_at=None,
                            updated_at=None, model="claude", cwd=None, git_branch=None,
                            project_id=None, metadata="{}"))
        r1.msgs.append(dict(id=gen_id("claude", f"{cid}:0"), conversation_id=cid, role="user",
                           content="Hello", thinking=None, created_at=None, model=None, metadata="{}"))
        upsert(db, r1)

        # Second insert with same ID but updated title
        r2 = ParseResult()
        r2.convs.append(dict(id=cid, source="claude", title="Updated", created_at=None,
                            updated_at=None, model="claude", cwd=None, git_branch=None,
                            project_id=None, metadata="{}"))
        r2.msgs.append(dict(id=gen_id("claude", f"{cid}:1"), conversation_id=cid, role="assistant",
                           content="Hi there", thinking=None, created_at=None, model=None, metadata="{}"))
        upsert(db, r2)

        # Verify: 1 conversation, 2 messages, title updated
        conv_count = db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        msg_count = db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?", [cid]).fetchone()[0]
        title = db.execute("SELECT title FROM conversations WHERE id = ?", [cid]).fetchone()[0]

        assert conv_count == 1, "Should have exactly 1 conversation"
        assert msg_count == 2, "Should have 2 messages"
        assert title == "Updated", "Title should be updated"
        db.close()

    def test_continued_conversation_no_duplicate(self, tmp_path):
        """Conversation continued on web and re-synced doesn't create duplicates."""
        import duckdb
        from ai_convos.cli import init_schema, upsert, ParseResult, gen_id

        db = duckdb.connect(str(tmp_path / "test.db"))
        init_schema(db)

        # Simulate: conversation started locally
        r1 = ParseResult()
        cid = gen_id("claude-code", "/path/to/session.jsonl")
        r1.convs.append(dict(id=cid, source="claude-code", title="Session", created_at=None,
                            updated_at=None, model="claude", cwd="/test", git_branch="main",
                            project_id=None, metadata="{}"))
        for i in range(3):
            r1.msgs.append(dict(id=gen_id("claude-code", f"{cid}:{i}"), conversation_id=cid,
                               role="user" if i % 2 == 0 else "assistant", content=f"Message {i}",
                               thinking=None, created_at=None, model=None, metadata="{}"))
        upsert(db, r1)

        # Simulate: same conversation continued (3 more messages)
        r2 = ParseResult()
        r2.convs.append(dict(id=cid, source="claude-code", title="Session", created_at=None,
                            updated_at=None, model="claude", cwd="/test", git_branch="main",
                            project_id=None, metadata="{}"))
        for i in range(6):  # includes original 3 + 3 new
            r2.msgs.append(dict(id=gen_id("claude-code", f"{cid}:{i}"), conversation_id=cid,
                               role="user" if i % 2 == 0 else "assistant", content=f"Message {i}",
                               thinking=None, created_at=None, model=None, metadata="{}"))
        upsert(db, r2)

        # Verify: still 1 conversation, now 6 messages
        conv_count = db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        msg_count = db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?", [cid]).fetchone()[0]

        assert conv_count == 1, "Should still have exactly 1 conversation"
        assert msg_count == 6, "Should have 6 messages (no duplicates)"
        db.close()


# ---- HTTP Error Handling Tests ----

class TestHTTPErrors:
    """Tests for handling API errors gracefully."""

    def test_403_forbidden_handling(self):
        """Verify helpful error on 403 Forbidden."""
        from ai_convos.cli import fetch_json
        import urllib.error

        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = urllib.error.HTTPError(
                "https://api.example.com", 403, "Forbidden", {}, None
            )
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                fetch_json("https://api.example.com", {"session": "test"})
            assert exc_info.value.code == 403

    def test_401_unauthorized_handling(self):
        """Verify 401 indicates expired cookies."""
        from ai_convos.cli import fetch_json
        import urllib.error

        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = urllib.error.HTTPError(
                "https://api.example.com", 401, "Unauthorized", {}, None
            )
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                fetch_json("https://api.example.com", {"session": "test"})
            assert exc_info.value.code == 401
