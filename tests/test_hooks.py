import json
from pathlib import Path
import duckdb, pytest
from typer.testing import CliRunner
from ai_convos import cli

@pytest.fixture
def hooks(tmp_path, monkeypatch):
    data, codex = tmp_path/"data", tmp_path/".codex"; sessions = codex/"sessions"; sessions.mkdir(parents=True)
    for k, v in (("DATA_DIR", data), ("DB_PATH", data/"convos.db"), ("HOOK_DIR", data/"hook_inbox"), ("HOOK_STATE", data/"hook_state.json"), ("HOOK_EMBED_DIRTY", data/"hook_embeddings_dirty")): monkeypatch.setattr(cli, k, v)
    monkeypatch.setenv("CODEX_HOME", str(codex)); monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: None)
    return sessions, data

def transcript(path, user="remember alpha", assistant=None):
    rows = [{"type":"session_meta","timestamp":"2026-01-01T00:00:00Z","payload":{"cwd":"/repo"}},
            {"type":"response_item","timestamp":"2026-01-01T00:00:01Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":user}]}}]
    if assistant: rows.append({"type":"response_item","timestamp":"2026-01-01T00:00:02Z","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":assistant}]}})
    path.write_text("\n".join(json.dumps(x) for x in rows))

def enqueue(path):
    r = CliRunner().invoke(cli.app, ["hook", "codex"], input=json.dumps({"transcript_path":str(path), "cwd":"/private", "session_id":"secret"}))
    assert r.exit_code == 0

def test_hook_is_nonblocking_coalesced_and_private(hooks, monkeypatch):
    sessions, data = hooks; path = sessions/"s.jsonl"; transcript(path)
    monkeypatch.setattr(cli, "get_db", lambda *a, **k: (_ for _ in ()).throw(AssertionError("hook touched db")))
    enqueue(path); enqueue(path)
    queued = list((data/"hook_inbox").glob("*.json")); assert len(queued) == 1
    raw = queued[0].read_text(); assert "remember alpha" not in raw and "secret" not in raw and set(json.loads(raw)) == {"source", "path", "mtime", "size"}

def test_retrieval_drains_idempotently_and_preserves_truncated_rewritten_history(hooks):
    sessions, data = hooks; path = sessions/"s.jsonl"; runner = CliRunner(); transcript(path); enqueue(path)
    assert json.loads(runner.invoke(cli.app, ["search", "remember alpha", "-f", "json"]).output)[0]["source"] == "codex"
    transcript(path, assistant="second answer"); enqueue(path); runner.invoke(cli.app, ["search", "second answer", "-f", "json"])
    conn = duckdb.connect(str(data/"convos.db")); assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2; updated = conn.execute("SELECT updated_at FROM conversations").fetchone()[0]; conn.close()
    transcript(path); enqueue(path); runner.invoke(cli.app, ["search", "remember alpha"])
    conn = duckdb.connect(str(data/"convos.db")); assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2; assert conn.execute("SELECT updated_at FROM conversations").fetchone()[0] == updated; conn.close()
    transcript(path, user="rewritten alpha"); enqueue(path); runner.invoke(cli.app, ["search", "rewritten alpha"])
    conn = duckdb.connect(str(data/"convos.db")); assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3; assert conn.execute("SELECT COUNT(*) FROM messages WHERE content IN ('remember alpha','rewritten alpha')").fetchone()[0] == 2; conn.close()
    enqueue(path); runner.invoke(cli.app, ["search", "rewritten alpha"]); conn = duckdb.connect(str(data/"convos.db")); assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3; conn.close()

def test_hook_rejects_paths_outside_provider_root(hooks):
    _, data = hooks; data.mkdir(); path = data/"outside.jsonl"; transcript(path)
    with pytest.raises(ValueError, match="Invalid codex transcript path"): cli.enqueue_hook("codex", {"transcript_path":str(path)})

def test_install_status_reinstall_and_remove_hooks(tmp_path, monkeypatch):
    claude, codex = tmp_path/"claude", tmp_path/"codex"; claude.mkdir(); codex.mkdir(); monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude)); monkeypatch.setenv("CODEX_HOME", str(codex)); monkeypatch.setattr(cli.shutil, "which", lambda _: "/opt/convos")
    (claude/"settings.json").write_text(json.dumps({"x":1,"hooks":{"Stop":[{"hooks":[{"type":"command","command":"keep me"},{"type":"command","command":"other hook claude-code"}]}]}})); runner = CliRunner()
    for _ in range(2): assert runner.invoke(cli.app, ["install-hooks"]).exit_code == 0
    c, x = json.loads((claude/"settings.json").read_text()), json.loads((codex/"hooks.json").read_text())
    assert c["x"] == 1 and sum(len(g["hooks"]) for g in c["hooks"]["Stop"]) == 3 and len(c["hooks"]["SessionEnd"]) == 1 and len(x["hooks"]["Stop"]) == 1
    assert "claude-code: 2 hooks" in runner.invoke(cli.app, ["install-hooks", "--status"]).output
    assert runner.invoke(cli.app, ["install-hooks", "--remove"]).exit_code == 0
    c, x = json.loads((claude/"settings.json").read_text()), json.loads((codex/"hooks.json").read_text())
    assert c["hooks"] == {"Stop":[{"hooks":[{"type":"command","command":"keep me"},{"type":"command","command":"other hook claude-code"}]}]} and x["hooks"] == {}
