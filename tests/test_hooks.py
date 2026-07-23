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

@pytest.mark.parametrize("stamp", [None, 100])
def test_sync_rechecks_chatgpt_unchanged_head(hooks, monkeypatch, stamp):
    _, data = hooks; monkeypatch.setattr(cli, "STATE_PATH", data/"sync_state.json"); cli.atomic_json(cli.STATE_PATH, {"web":{"chatgpt":{"browser":"safari","head":f"default:c1:{stamp}"}}}); called = []
    monkeypatch.setattr(cli, "chatgpt_profiles", lambda _: [None]); monkeypatch.setattr(cli, "chatgpt_cookie_base", lambda *a: ({}, "https://chatgpt.com")); monkeypatch.setattr(cli, "chatgpt_headers", lambda *a, **k: {"ChatGPT-Account-ID":"acct"}); monkeypatch.setattr(cli, "fetch_json", lambda *a, **k: {"items":[{"id":"c1","update_time":stamp}]}); monkeypatch.setattr(cli, "fetch_chatgpt", lambda *a, **k: called.append(k) or cli.ParseResult()); monkeypatch.setattr(cli, "get_cookies", lambda *_: {})
    cli.sync(False, 300, False, False, False, False)
    assert called and called[0]["profiles"] == [None]

def test_sync_repairs_legacy_chatgpt_timestamps_before_comparison(hooks, monkeypatch):
    _, data = hooks; data.mkdir(); monkeypatch.setattr(cli, "STATE_PATH", data/"sync_state.json"); cid = cli.gen_id("chatgpt","legacy"); when = cli.ts_from_epoch(200)
    conn = duckdb.connect(str(data/"convos.db")); cli.init_schema(conn); cli.upsert(conn, cli.ParseResult([dict(id=cid,source="chatgpt",title="T",created_at=None,updated_at=None,model=None,cwd=None,git_branch=None,project_id=None,metadata="{}")],[dict(id="legacy-m",conversation_id=cid,role="user",content="old",thinking=None,created_at=when,model=None,metadata="{}",parent_id=None)])); conn.close()
    cli.atomic_json(cli.STATE_PATH,{"web":{"chatgpt":{"browser":"safari","head":"default:old:100"}}}); captured = []
    monkeypatch.setattr(cli,"chatgpt_profiles",lambda _:[None]); monkeypatch.setattr(cli,"chatgpt_cookie_base",lambda *a,**k:({},"https://chatgpt.com")); monkeypatch.setattr(cli,"chatgpt_headers",lambda *a,**k:{}); monkeypatch.setattr(cli,"fetch_json",lambda *a,**k:{"items":[{"id":"new","update_time":300}]}); monkeypatch.setattr(cli,"fetch_chatgpt",lambda *a,**k:captured.append(k) or cli.ParseResult()); monkeypatch.setattr(cli,"get_cookies",lambda *_:{})
    cli.sync(False,300,False,False,False,False); conn = duckdb.connect(str(data/"convos.db"),read_only=True); times = conn.execute("SELECT created_at,updated_at FROM conversations WHERE id=?",[cid]).fetchone(); conn.close()
    assert times == (when,when) and captured[0]["known"][cid] == when.timestamp() and cid in captured[0]["legacy"]

def test_sync_disables_frontier_when_saved_ids_are_missing(hooks, monkeypatch):
    _, data = hooks; data.mkdir(); monkeypatch.setattr(cli,"STATE_PATH",data/"sync_state.json"); cid = cli.gen_id("chatgpt","present"); missing = cli.gen_id("chatgpt","missing"); conn = duckdb.connect(str(data/"convos.db")); cli.init_schema(conn); cli.upsert(conn,cli.ParseResult([dict(id=cid,source="chatgpt",title="T",created_at=None,updated_at=cli.ts_any(100),model=None,cwd=None,git_branch=None,project_id=None,metadata="{}")],[])); conn.close(); cli.atomic_json(cli.STATE_PATH,{"web":{"chatgpt":{"browser":"safari","head":"default:old:100","frontiers":{"default":{"account":"acct","updated":100}},"coverage":[missing]}}}); captured = []
    monkeypatch.setattr(cli,"chatgpt_profiles",lambda _:[None]); monkeypatch.setattr(cli,"chatgpt_cookie_base",lambda *a,**k:({},"https://chatgpt.com")); monkeypatch.setattr(cli,"chatgpt_headers",lambda *a,**k:{"ChatGPT-Account-ID":"acct"}); monkeypatch.setattr(cli,"fetch_json",lambda *a,**k:{"items":[{"id":"new","update_time":200}]}); monkeypatch.setattr(cli,"fetch_chatgpt",lambda *a,**k:captured.append(k) or cli.ParseResult()); monkeypatch.setattr(cli,"get_cookies",lambda *_:{})
    cli.sync(False,300,False,False,False,False); assert captured[0]["frontiers"] is None

def test_sync_deduplicates_profiles_for_same_chatgpt_account(hooks, monkeypatch):
    _, data = hooks; monkeypatch.setattr(cli,"STATE_PATH",data/"sync_state.json"); captured = []
    monkeypatch.setattr(cli,"chatgpt_profiles",lambda _:["A","B"]); monkeypatch.setattr(cli,"chatgpt_cookie_base",lambda *a,**k:({},"https://chatgpt.com")); monkeypatch.setattr(cli,"chatgpt_headers",lambda *a,**k:{"ChatGPT-Account-ID":"acct"}); monkeypatch.setattr(cli,"fetch_json",lambda *a,**k:{"items":[{"id":"c1","update_time":100}]}); monkeypatch.setattr(cli,"fetch_chatgpt",lambda *a,**k:captured.append(k) or cli.ParseResult()); monkeypatch.setattr(cli,"get_cookies",lambda *_:{})
    cli.sync(False,300,False,False,False,False); assert captured[0]["profiles"]==["A"]

def test_sync_checkpoints_chatgpt_pages_and_retries_only_unfinished(hooks, monkeypatch):
    _, data = hooks; monkeypatch.setattr(cli,"STATE_PATH",data/"sync_state.json"); cid0 = cli.gen_id("chatgpt","ok0"); mid0 = cli.gen_id("chatgpt",f"{cid0}:m"); old = {"browser":"safari","head":"default:old:100","frontiers":{"default":{"account":"acct","updated":100}},"coverage":[cid0]}; cli.atomic_json(cli.STATE_PATH,{"web":{"chatgpt":old}}); fail, details = {"bad":True}, []
    conn = duckdb.connect(str(data/"convos.db")); cli.init_schema(conn); cli.upsert(conn,cli.ParseResult([dict(id=cid0,source="chatgpt",title="T",created_at=cli.ts_any(100),updated_at=cli.ts_any(100),model=None,cwd=None,git_branch=None,project_id=None,metadata=json.dumps({"remote_update_time":100}))],[dict(id=mid0,conversation_id=cid0,role="user",content="ok0",thinking=None,created_at=cli.ts_any(100),model=None,metadata="{}",parent_id=None)])); conn.close()
    items = [{"id":f"ok{i}","create_time":300-i,"update_time":300-i} for i in range(20)]+[{"id":"bad","create_time":200,"update_time":200}]
    monkeypatch.setattr(cli,"chatgpt_profiles",lambda _:[None]); monkeypatch.setattr(cli,"chatgpt_cookie_base",lambda *a,**k:({},"https://chatgpt.com")); monkeypatch.setattr(cli,"chatgpt_headers",lambda *a,**k:{"ChatGPT-Account-ID":"acct"}); monkeypatch.setattr(cli,"get_cookies",lambda *_:{}); monkeypatch.setattr(cli.time,"sleep",lambda _:None)
    def fetch(url,*a,**k):
        if "limit=1&order=updated" in url: return {"items":[items[0]]}
        if "/conversations?" in url:
            offset = int(url.split("offset=")[1].split("&")[0])
            return {"items":items if offset==0 else [],"total":len(items)}
        name = url.rsplit("/",1)[-1]; details.append(name)
        if name=="bad" and fail["bad"]: raise TimeoutError("detail timeout")
        when = next(x["update_time"] for x in items if x["id"]==name)
        return {"mapping":{"m":{"parent":None,"message":{"author":{"role":"user"},"content":{"parts":[name]},"create_time":when}}}}
    monkeypatch.setattr(cli,"fetch_json",fetch); cli.sync(False,300,False,False,False,False)
    conn = duckdb.connect(str(data/"convos.db"),read_only=True); assert {r[0] for r in conn.execute("SELECT content FROM messages").fetchall()}=={f"ok{i}" for i in range(20)}; conn.close(); assert json.loads(cli.STATE_PATH.read_text())["web"]["chatgpt"]==old and (data/"hook_fts_dirty").exists() and len(json.loads((data/"hook_embeddings_dirty").read_text()))==20
    fail["bad"] = False; cli.sync(False,300,False,False,False,False); conn = duckdb.connect(str(data/"convos.db"),read_only=True); assert {r[0] for r in conn.execute("SELECT content FROM messages").fetchall()}=={*(f"ok{i}" for i in range(20)),"bad"}; conn.close()
    saved = json.loads(cli.STATE_PATH.read_text())["web"]["chatgpt"]
    assert details==[*(f"ok{i}" for i in range(20)),"bad","bad"] and saved["frontiers"]=={"default":{"account":"acct","updated":300,"id":"ok0"}} and len(saved["coverage"])==21 and cid0 in saved["coverage"]

def test_sync_rolls_back_interrupted_chatgpt_checkpoint(hooks, monkeypatch):
    _, data = hooks; monkeypatch.setattr(cli,"STATE_PATH",data/"sync_state.json"); old = {"browser":"safari","head":"default:old:100","frontiers":{"default":{"account":"acct","updated":100}},"coverage":[]}; cli.atomic_json(cli.STATE_PATH,{"web":{"chatgpt":old}}); cid = cli.gen_id("chatgpt","c1"); mid = cli.gen_id("chatgpt","m1")
    result = cli.ParseResult([dict(id=cid,source="chatgpt",title="T",created_at=cli.ts_any(300),updated_at=cli.ts_any(300),model=None,cwd=None,git_branch=None,project_id=None,metadata=json.dumps({"remote_update_time":300}))],[dict(id=mid,conversation_id=cid,role="user",content="atomic",thinking=None,created_at=cli.ts_any(300),model=None,metadata="{}",parent_id=None)])
    monkeypatch.setattr(cli,"chatgpt_profiles",lambda _:[None]); monkeypatch.setattr(cli,"chatgpt_cookie_base",lambda *a,**k:({},"https://chatgpt.com")); monkeypatch.setattr(cli,"chatgpt_headers",lambda *a,**k:{"ChatGPT-Account-ID":"acct"}); monkeypatch.setattr(cli,"fetch_json",lambda *a,**k:{"items":[{"id":"c1","update_time":300}]}); monkeypatch.setattr(cli,"get_cookies",lambda *_:{})
    def fetched(*a,**k): k["sink"](result); return cli.ParseResult()
    monkeypatch.setattr(cli,"fetch_chatgpt",fetched); real = cli.upsert
    def interrupted(conn,r): real(conn,r); raise RuntimeError("mid-upsert")
    monkeypatch.setattr(cli,"upsert",interrupted)
    cli.sync(False,300,False,False,False,False)
    conn = duckdb.connect(str(data/"convos.db"),read_only=True); assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]==0 and conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]==0; conn.close(); assert json.loads(cli.STATE_PATH.read_text())["web"]["chatgpt"]==old and (data/"hook_fts_dirty").exists() and json.loads((data/"hook_embeddings_dirty").read_text())==[mid]
    monkeypatch.setattr(cli,"upsert",real); cli.sync(False,300,False,False,False,False); conn = duckdb.connect(str(data/"convos.db"),read_only=True); assert conn.execute("SELECT content FROM messages").fetchall()==[("atomic",)]; conn.close()

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
