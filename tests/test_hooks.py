import json
from pathlib import Path
import duckdb, pytest
from typer.testing import CliRunner
from ai_convos import cli

@pytest.fixture
def hooks(tmp_path, monkeypatch):
    data, codex = tmp_path/"data", tmp_path/".codex"; sessions = codex/"sessions"; sessions.mkdir(parents=True)
    for k, v in (("DATA_DIR", data), ("DB_PATH", data/"convos.db"), ("HOOK_DIR", data/"hook_inbox"), ("HOOK_STATE", data/"hook_state.json"), ("HOOK_EMBED_DIRTY", data/"hook_embeddings_dirty"), ("HOOK_FTS_DIRTY", data/"hook_fts_dirty")): monkeypatch.setattr(cli, k, v)
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
    conn = duckdb.connect(str(data/"convos.db")); assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3; assert conn.execute("SELECT COUNT(*) FROM messages WHERE content IN ('remember alpha','rewritten alpha')").fetchone()[0] == 2; meta = json.loads(conn.execute("SELECT metadata FROM messages WHERE content='remember alpha'").fetchone()[0]); assert meta["history_of"] and meta["superseded_at"]; conn.close()
    enqueue(path); runner.invoke(cli.app, ["search", "rewritten alpha"]); conn = duckdb.connect(str(data/"convos.db")); assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3; conn.close()

def test_enqueue_during_drain_survives_for_next_worker(hooks, monkeypatch):
    sessions, data = hooks; path = sessions/"s.jsonl"; transcript(path); enqueue(path); original, raced = cli.upsert, {"done":False}
    def upsert(conn, result):
        out = original(conn, result)
        if not raced["done"]: raced["done"] = True; transcript(path, "newer alpha"); enqueue(path)
        return out
    monkeypatch.setattr(cli, "upsert", upsert); assert cli.drain_hooks() == 1
    assert len(list((data/"hook_inbox").glob("*.json"))) == 1
    monkeypatch.setattr(cli, "upsert", original); assert cli.drain_hooks() == 1
    conn = duckdb.connect(str(data/"convos.db")); assert conn.execute("SELECT COUNT(*) FROM messages WHERE content IN ('remember alpha','newer alpha')").fetchone()[0] == 2; conn.close()

def test_failed_parse_returns_claim_to_queue(hooks, monkeypatch):
    sessions, data = hooks; path = sessions/"s.jsonl"; transcript(path); enqueue(path); original = cli.hook_result
    monkeypatch.setattr(cli, "hook_result", lambda *_: (_ for _ in ()).throw(ValueError("partial transcript"))); assert cli.drain_hooks() == 0
    assert len(list((data/"hook_inbox").glob("*.json"))) == 1 and not list((data/"hook_inbox").glob("*.work"))
    monkeypatch.setattr(cli, "hook_result", original); assert cli.drain_hooks() == 1

def test_orphaned_claim_forces_reindex_after_committed_upsert(hooks):
    sessions, data = hooks; path = sessions/"s.jsonl"; transcript(path); enqueue(path); q = next((data/"hook_inbox").glob("*.json")); work = q.with_suffix(".work"); q.replace(work)
    conn = duckdb.connect(str(data/"convos.db")); cli.init_schema(conn); cli.upsert(conn, cli.hook_result("codex", path)); conn.close()
    assert cli.drain_hooks() == 1
    assert (data/"hook_fts_dirty").exists(); assert cli.flush_fts()
    conn = duckdb.connect(str(data/"convos.db")); assert conn.execute("SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='fts_main_messages'").fetchone()[0] == 1; conn.close()

def test_hook_defers_fts_until_fresh_search(hooks):
    sessions, data = hooks; path = sessions/"s.jsonl"; transcript(path); enqueue(path); assert cli.drain_hooks() == 1
    conn = duckdb.connect(str(data/"convos.db")); assert not conn.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name='fts_main_messages'").fetchone(); conn.close(); assert (data/"hook_fts_dirty").exists()
    hits = json.loads(CliRunner().invoke(cli.app, ["search", "remember alpha", "-f", "json"]).output)
    assert hits[0]["content"] == "remember alpha" and not (data/"hook_fts_dirty").exists()

def test_fts_claim_preserves_new_work(hooks, monkeypatch):
    _, data = hooks; (data/"hook_inbox").mkdir(parents=True); (data/"hook_fts_dirty").touch(); conn = duckdb.connect(str(data/"convos.db")); cli.init_schema(conn); conn.close()
    monkeypatch.setattr(cli, "rebuild_fts_index", lambda _: (data/"hook_fts_dirty").touch()); assert cli.flush_fts()
    assert (data/"hook_fts_dirty").exists() and not list(data.glob(".hook_fts_dirty.*"))

def test_failed_fts_claim_is_restored(hooks, monkeypatch):
    _, data = hooks; (data/"hook_inbox").mkdir(parents=True); (data/"hook_fts_dirty").touch()
    monkeypatch.setattr(cli, "rebuild_fts_index", lambda _: (_ for _ in ()).throw(RuntimeError("index failed")))
    with pytest.raises(RuntimeError, match="index failed"): cli.flush_fts()
    assert (data/"hook_fts_dirty").exists() and not list(data.glob(".hook_fts_dirty.*"))

def test_orphaned_fts_claim_is_retried(hooks, monkeypatch):
    _, data = hooks; (data/"hook_inbox").mkdir(parents=True); (data/".hook_fts_dirty.dead").touch(); seen = []
    monkeypatch.setattr(cli, "rebuild_fts_index", lambda _: seen.append(True)); assert cli.flush_fts()
    assert seen == [True] and not (data/"hook_fts_dirty").exists() and not list(data.glob(".hook_fts_dirty.*"))

def test_embedding_claim_is_scoped_and_preserves_new_work(hooks, monkeypatch):
    _, data = hooks; (data/"hook_inbox").mkdir(parents=True); cli.atomic_json(data/"hook_embeddings_dirty", ["old"]); seen = {}
    def embed(batch, ids): seen.update(batch=batch, ids=ids); cli.atomic_json(data/"hook_embeddings_dirty", ["new"])
    monkeypatch.setattr(cli, "embed_pending", embed); cli.embed_hook_pending()
    assert seen == {"batch":32, "ids":["old"]} and json.loads((data/"hook_embeddings_dirty").read_text()) == ["new"]

def test_failed_embedding_restores_claimed_ids(hooks, monkeypatch):
    _, data = hooks; (data/"hook_inbox").mkdir(parents=True); cli.atomic_json(data/"hook_embeddings_dirty", ["old"])
    monkeypatch.setattr(cli, "embed_pending", lambda *_: (_ for _ in ()).throw(RuntimeError("model failed")))
    with pytest.raises(RuntimeError, match="model failed"): cli.embed_hook_pending()
    assert json.loads((data/"hook_embeddings_dirty").read_text()) == ["old"]

def test_init_still_installs_skills(hooks, monkeypatch):
    called = []; monkeypatch.setattr(cli, "install_skills", lambda: called.append(True))
    assert CliRunner().invoke(cli.app, ["init"]).exit_code == 0 and called == [True]

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
