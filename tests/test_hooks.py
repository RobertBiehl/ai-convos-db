import json, os, signal, subprocess, sys, time
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

def enqueue(path,command="capture"):
    r = CliRunner().invoke(cli.app, [command, "codex"], input=json.dumps({"transcript_path":str(path), "cwd":"/private", "session_id":"secret"}))
    assert r.exit_code == 0

def test_hook_is_nonblocking_coalesced_and_private(hooks, monkeypatch):
    sessions, data = hooks; path = sessions/"s.jsonl"; transcript(path)
    monkeypatch.setattr(cli, "get_db", lambda *a, **k: (_ for _ in ()).throw(AssertionError("hook touched db")))
    enqueue(path); enqueue(path,"hook")
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

def test_sync_defers_fts_and_embeddings(hooks, tmp_path, monkeypatch):
    _, data = hooks; src = tmp_path/"import.json"; src.write_text("[]"); monkeypatch.setenv("CONVOS_IMPORT_PATHS", str(src)); monkeypatch.setattr(cli, "STATE_PATH", data/"sync_state.json")
    result = cli.ParseResult(convs=[dict(id="sync-c", source="chatgpt", title="T", created_at=None, updated_at=None, model=None, cwd=None, git_branch=None, project_id=None, metadata="{}")], msgs=[dict(id="sync-m", conversation_id="sync-c", role="user", content="alpha", thinking=None, created_at=None, model=None, metadata="{}", parent_id=None)])
    monkeypatch.setattr(cli, "parse_source", lambda _: result); monkeypatch.setattr(cli, "chatgpt_profiles", lambda _: []); monkeypatch.setattr(cli, "get_cookies", lambda *_: {})
    fail = lambda *_: (_ for _ in ()).throw(AssertionError("foreground consolidation")); monkeypatch.setattr(cli, "flush_fts", fail); monkeypatch.setattr(cli, "embed_hook_pending", fail); old = signal.getsignal(signal.SIGINT)
    try: cli.sync(False, 300, False, False, False, False); assert signal.getsignal(signal.SIGINT) == old
    finally: signal.signal(signal.SIGINT, old)
    assert (data/"hook_fts_dirty").exists() and json.loads((data/"hook_embeddings_dirty").read_text()) == ["sync-m"]

def test_sync_rechecks_chatgpt_head_without_timestamp(hooks, monkeypatch):
    _, data = hooks; monkeypatch.setattr(cli, "STATE_PATH", data/"sync_state.json"); cli.atomic_json(cli.STATE_PATH, {"web":{"chatgpt":{"browser":"safari","head":"default:c1:None"}}}); called = []
    monkeypatch.setattr(cli, "chatgpt_profiles", lambda _: [None]); monkeypatch.setattr(cli, "chatgpt_cookie_base", lambda *a: ({}, "https://chatgpt.com")); monkeypatch.setattr(cli, "chatgpt_headers", lambda *a, **k: {}); monkeypatch.setattr(cli, "fetch_json", lambda *a, **k: {"items":[{"id":"c1","update_time":None}]}); monkeypatch.setattr(cli, "fetch_chatgpt", lambda *a, **k: called.append(k) or cli.ParseResult()); monkeypatch.setattr(cli, "get_cookies", lambda *_: {})
    cli.sync(False, 300, False, False, False, False)
    assert called and called[0]["profiles"] == [None]

def test_sync_sigint_exits_during_blocked_source(tmp_path):
    src, blocked, ready, done = tmp_path/"import.json", tmp_path/"blocked.json", tmp_path/"ready", tmp_path/"done"; src.write_text("[]"); blocked.write_text("[]")
    code = '''import hashlib,os,sys
from pathlib import Path
from ai_convos import cli
class C:
 def close(self): pass
 def execute(self,*_): return self
 def fetchone(self): return [0]
 def fetchall(self): return []
cli.get_db=lambda *a,**k:C(); cli.init_schema=lambda _:None; cli.drain_hooks=lambda *a,**k:cli.HOOK_DIR.mkdir(parents=True,exist_ok=True) or 0; cli.counts_by_source=lambda _:{}
cli.chatgpt_profiles=lambda _:[]; cli.get_cookies=lambda *_:{}
def parsed(path):
 if path.name=="blocked.json" and os.environ.get("BLOCK")!="0": Path(os.environ["READY"]).touch(); hashlib.pbkdf2_hmac("sha256",b"x",b"y",500_000_000)
 return cli.ParseResult()
def upsert(*_): Path(os.environ["DONE"]).touch(); return 0,0,0,0,0,0,0,{"m"}
cli.parse_source=parsed; cli.upsert=upsert; sys.argv[1:]=["sync"]; cli.sync(False,300,False,False,False,False)'''
    root = tmp_path/"archive"; (root/"data").mkdir(parents=True); (root/"data/sync_state.json").write_text('{"sentinel":1}'); env = {**os.environ, "CONVOS_PROJECT_ROOT":str(root), "CONVOS_IMPORT_PATHS":f"{src},{blocked}", "READY":str(ready), "DONE":str(done)}; p = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while not (ready.exists() and done.exists()) and p.poll() is None and time.monotonic() < deadline: time.sleep(.02)
        assert ready.exists() and done.exists(), f"sync sources did not start (exit={p.poll()})"; time.sleep(.1); p.send_signal(signal.SIGINT)
        try: p.wait(timeout=2)
        except subprocess.TimeoutExpired: p.kill(); p.wait(); pytest.fail("sync ignored Ctrl-C for more than 2 seconds")
        assert p.returncode == -signal.SIGINT and json.loads((root/"data/sync_state.json").read_text()) == {"sentinel":1}
        assert subprocess.run([sys.executable, "-c", code], env={**env, "BLOCK":"0"}, capture_output=True).returncode == 0
        state = json.loads((root/"data/sync_state.json").read_text()); assert len(state["imports"]) == 2 and (root/"data/hook_fts_dirty").exists() and json.loads((root/"data/hook_embeddings_dirty").read_text()) == ["m"]
    finally:
        if p.poll() is None: p.kill(); p.wait()

def test_fts_claim_preserves_new_work(hooks, monkeypatch):
    _, data = hooks; (data/"hook_inbox").mkdir(parents=True); (data/"hook_fts_dirty").touch(); conn = duckdb.connect(str(data/"convos.db")); cli.init_schema(conn); conn.close()
    monkeypatch.setattr(cli, "rebuild_fts_index", lambda _: (data/"hook_fts_dirty").touch()); assert cli.flush_fts()
    assert (data/"hook_fts_dirty").exists() and not list(data.glob(".hook_fts_dirty.*"))

@pytest.mark.parametrize("error", [RuntimeError("index failed"), KeyboardInterrupt()])
def test_failed_fts_claim_is_restored(hooks, monkeypatch, error):
    _, data = hooks; (data/"hook_inbox").mkdir(parents=True); (data/"hook_fts_dirty").touch()
    monkeypatch.setattr(cli, "rebuild_fts_index", lambda _: (_ for _ in ()).throw(error))
    with pytest.raises(type(error)): cli.flush_fts()
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

@pytest.mark.parametrize("error", [RuntimeError("model failed"), KeyboardInterrupt()])
def test_failed_embedding_restores_claimed_ids(hooks, monkeypatch, error):
    _, data = hooks; (data/"hook_inbox").mkdir(parents=True); cli.atomic_json(data/"hook_embeddings_dirty", ["old"])
    monkeypatch.setattr(cli, "embed_pending", lambda *_: (_ for _ in ()).throw(error))
    with pytest.raises(type(error)): cli.embed_hook_pending()
    assert json.loads((data/"hook_embeddings_dirty").read_text()) == ["old"]

def test_init_still_installs_skills(hooks, monkeypatch):
    called = []; monkeypatch.setattr(cli, "install_skills", lambda: called.append(True))
    assert CliRunner().invoke(cli.app, ["init"]).exit_code == 0 and called == [True]

def test_doctor_reports_archive_ingest_and_hook_health(hooks, monkeypatch):
    _, data = hooks; claude = data/"claude"; claude.mkdir(parents=True); monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude)); monkeypatch.setattr(cli, "safari_cookie_domains", lambda: []); monkeypatch.setattr(cli, "chrome_cookie_domains", lambda: [])
    conn = duckdb.connect(str(data/"convos.db")); cli.init_schema(conn); conn.execute("INSERT INTO conversations VALUES ('c','codex','T',NULL,'2026-01-01',NULL,NULL,NULL,NULL,NULL)"); conn.execute("INSERT INTO messages VALUES ('m','c','user','hello',NULL,NULL,NULL,NULL,NULL,NULL)"); cli.rebuild_fts_index(conn); conn.close()
    cli.atomic_json(data/"hook_state.json", {"x":[1767225600000000000,1]}); cli.atomic_json(data/"hook_embeddings_dirty", ["m"]); cli.atomic_json(data/"hook_inbox/q.json", {"source":"codex"})
    r = CliRunner().invoke(cli.app, ["doctor"]); assert r.exit_code == 0
    assert "convos:" in r.output and "archive: 1 convs, 1 msgs, 1 unembedded" in r.output and "schema=ready, fts=yes" in r.output and "repair: convos embed" in r.output
    assert "ingest: pending=1, embedding_ids=1, embedding_claims=0, last=2026-01-01" in r.output and "codex: 0 hooks" in r.output

def test_doctor_surfaces_schema_skew(hooks, monkeypatch):
    _, data = hooks; data.mkdir(); monkeypatch.setattr(cli, "safari_cookie_domains", lambda: []); monkeypatch.setattr(cli, "chrome_cookie_domains", lambda: [])
    conn = duckdb.connect(str(data/"convos.db")); conn.execute("CREATE TABLE messages (id VARCHAR, content VARCHAR)"); conn.close()
    r = CliRunner().invoke(cli.app, ["doctor"]); assert r.exit_code == 0
    assert "schema=missing:" in r.output and "messages.embedding" in r.output and "fts=no" in r.output and "repair: convos init" in r.output

def test_hook_rejects_paths_outside_provider_root(hooks):
    _, data = hooks; data.mkdir(); path = data/"outside.jsonl"; transcript(path)
    with pytest.raises(ValueError, match="Invalid codex transcript path"): cli.enqueue_hook("codex", {"transcript_path":str(path)})

def test_install_status_reinstall_and_remove_hooks(tmp_path, monkeypatch):
    claude, codex, archive = tmp_path/"claude", tmp_path/"codex", tmp_path/"archive root"; claude.mkdir(); codex.mkdir(); monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude)); monkeypatch.setenv("CODEX_HOME", str(codex)); monkeypatch.setenv("CONVOS_PROJECT_ROOT",str(archive)); monkeypatch.setattr(cli.shutil, "which", lambda _: "/opt/convos")
    (claude/"settings.json").write_text(json.dumps({"x":1,"hooks":{"Stop":[{"hooks":[{"type":"command","command":"keep me"},{"type":"command","command":"other hook claude-code"},{"type":"command","command":"/old/convos hook claude-code","statusMessage":"Updating conversation archive"},{"type":"command","command":"touch /tmp/wake # ai-convos remote hook"}]}]}})); runner = CliRunner()
    first = runner.invoke(cli.app, ["install-hooks"]); second = runner.invoke(cli.app, ["install-hooks"]); assert first.exit_code == second.exit_code == 0 and "`/hooks`" in first.output
    c, x = json.loads((claude/"settings.json").read_text()), json.loads((codex/"hooks.json").read_text())
    handler=x["hooks"]["Stop"][0]["hooks"][0]; assert c["x"] == 1 and sum(len(g["hooks"]) for g in c["hooks"]["Stop"]) == 3 and len(c["hooks"]["SessionEnd"]) == 1 and len(x["hooks"]["Stop"]) == 1 and handler["command"]==f"CONVOS_PROJECT_ROOT='{archive}' /opt/convos capture codex" and handler["timeout"]==5 and handler["statusMessage"]=="Saving conversation to Convos"
    status = runner.invoke(cli.app, ["install-hooks", "--status"]).output; assert "claude-code: 2 hooks" in status and "`/hooks`" not in status
    assert runner.invoke(cli.app, ["install-hooks", "--remove"]).exit_code == 0
    c, x = json.loads((claude/"settings.json").read_text()), json.loads((codex/"hooks.json").read_text())
    assert c["hooks"] == {"Stop":[{"hooks":[{"type":"command","command":"keep me"},{"type":"command","command":"other hook claude-code"}]}]} and x["hooks"] == {}
