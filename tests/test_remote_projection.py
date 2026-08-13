import json, os, subprocess
from pathlib import Path

import duckdb
import pytest
from ai_convos.cli import init_schema, project_row_proof
import ai_convos_remote.projection as projection_module
from ai_convos_remote import publish
from ai_convos_remote.projection import apply_row_replica, apply_row_replicas, attest_rows, blob_replicas, bridges, connect, cutover_state, event_support, foreign_id, inspect_state, project, project_many, relocate_attachments, scan, sequence
from ai_convos_remote.protocol import b64, certificate, digest, event, identity, logical_row, public, public_id, row_proof


def git(path,*args): return subprocess.run(("git","-C",str(path),*args),check=True,capture_output=True).stdout.decode().strip()
def source(tmp_path):
    repo=tmp_path/"repo"; repo.mkdir(); git(repo,"init","-q"); git(repo,"config","user.email","a@b.c"); git(repo,"config","user.name","A"); (repo/"a.py").write_text("new\n"); git(repo,"add","."); git(repo,"commit","-qm","init")
    db=duckdb.connect(str(tmp_path/"source.db")); init_schema(db); db.execute("INSERT INTO conversations VALUES ('c','codex','title','2026-01-01','2026-01-01','m',?,NULL,NULL,'{}')",[str(repo)]); db.execute("INSERT INTO messages VALUES ('u','c','user','change it',NULL,'2026-01-01 00:00:00','m','{}',NULL,NULL),('m','c','assistant','done',NULL,'2026-01-01 00:00:01','m','{}',NULL,NULL)"); db.execute("INSERT INTO file_edits VALUES ('e','m',?,'write','new\n','2026-01-01 00:00:01',NULL)",[str(repo/'a.py')]); return repo,db


def test_personal_scan_strips_local_roots_and_projects_duckdb(tmp_path):
    repo,core=source(tmp_path); state=connect(tmp_path/"state.db"); records=scan(core,state); raw=json.dumps(records)
    assert str(repo) not in raw and len(records)>3
    remote=identity("remote"); events=[event(remote,i+1,r["kind"],r["entity"],r["payload"],[],f"2026-01-01T00:00:{i:02d}Z") for i,r in enumerate(records)]
    for value in events: project(tmp_path/"target.db",state,value,"personal","other-device",authors={remote["id"]:"remote-user"})
    target=duckdb.connect(str(tmp_path/"target.db"),read_only=True); assert target.execute("SELECT title,cwd FROM conversations").fetchone()==("title",None); assert target.execute("SELECT content FROM messages WHERE role='user'").fetchone()[0]=="change it"; assert target.execute("SELECT file_path FROM file_edits").fetchone()[0]=="a.py"; before=target.execute("SELECT COUNT(*) FROM provenance.file_edit_files").fetchone()[0]; assert target.execute("SELECT x.file_edit_id=fe.id FROM provenance.file_edit_files x JOIN file_edits fe ON fe.id=x.file_edit_id").fetchone()[0]; assert target.execute("SELECT COUNT(*) FROM remote.row_origins").fetchone()[0]==sum(e["kind"] in projection_module.TABLES for e in events); target.close()
    fresh=connect(tmp_path/"fresh-state.db"); imported=duckdb.connect(str(tmp_path/"target.db"),read_only=True); assert scan(imported,fresh)==[]; imported.close(); fresh.close()
    old={"raw_events","repositories","files","file_versions","changesets","edits","changeset_repositories","checkpoints","checkpoint_changesets","assertions","gaps","boundaries"}; assert not old&{r[0] for r in state.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert not {"event_log","history_material","history_outbox","history_queue","sharing_boundaries","attachment_chunks","imported_rows"}&{r[0] for r in state.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_old_state_inspection_is_read_only_and_cutover_preserves_exact_backup(tmp_path):
    path=tmp_path/"state.db"; db=__import__("sqlite3").connect(path); db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)"); db.execute("CREATE TABLE legacy_payload(value TEXT)"); db.execute("INSERT INTO meta VALUES ('state_schema','3')"); db.execute("INSERT INTO legacy_payload VALUES ('only in old state')"); db.commit(); db.close(); before=(path.read_bytes(),path.stat().st_mtime_ns,{p.name for p in tmp_path.iterdir()})
    assert inspect_state(path)["status"]=="incompatible"
    with pytest.raises(ValueError,match="rebuild required"): connect(path)
    assert (path.read_bytes(),path.stat().st_mtime_ns,{p.name for p in tmp_path.iterdir()})==before
    report=cutover_state(path); backup=Path(report["backup"]); old=__import__("sqlite3").connect(backup/"state.db"); assert old.execute("SELECT value FROM legacy_payload").fetchone()[0]=="only in old state"; old.close()
    state=connect(path); assert state.execute("SELECT value FROM meta WHERE key='state_schema'").fetchone()[0]=="1" and json.loads(state.execute("SELECT value FROM meta WHERE key='state_cutover'").fetchone()[0])["backup"]==str(backup); state.close(); assert inspect_state(path)["status"]=="current" and os.stat(backup).st_mode&0o777==0o700 and os.stat(backup/"state.db").st_mode&0o777==0o600


def test_cutover_recovers_corrupt_regular_state_but_refuses_symlink(tmp_path):
    path=tmp_path/"state.db"; path.write_bytes(b"corrupt but preserved"); report=cutover_state(path); assert (Path(report["backup"])/"state.db").read_bytes()==b"corrupt but preserved" and inspect_state(path)["status"]=="current"
    target=tmp_path/"target.db"; target.write_bytes(b"do not touch"); link=tmp_path/"link.db"; link.symlink_to(target)
    with pytest.raises(ValueError,match="cannot be rebuilt"): cutover_state(link)
    assert target.read_bytes()==b"do not touch"


def test_cutover_install_failure_keeps_old_state_and_verified_backup(tmp_path,monkeypatch):
    path=tmp_path/"state.db"; db=__import__("sqlite3").connect(path); db.execute("CREATE TABLE legacy(value TEXT)"); db.execute("INSERT INTO legacy VALUES ('still here')"); db.commit(); db.close(); original=projection_module.os.replace; failed=[False]
    def replace(source,target):
        if Path(target)==path and not failed[0]: failed[0]=True; raise OSError("install failed")
        return original(source,target)
    monkeypatch.setattr(projection_module.os,"replace",replace)
    with pytest.raises(OSError,match="install failed"): cutover_state(path)
    old=__import__("sqlite3").connect(path); assert old.execute("SELECT value FROM legacy").fetchone()[0]=="still here"; old.close(); backup=next((tmp_path/"backups").iterdir()); saved=__import__("sqlite3").connect(backup/"state.db"); assert saved.execute("SELECT value FROM legacy").fetchone()[0]=="still here"; saved.close()
    monkeypatch.setattr(projection_module.os,"replace",original); cutover_state(path); assert inspect_state(path)["status"]=="current"


def test_unchanged_provenance_does_not_republish_but_file_change_does(tmp_path):
    repo,core=source(tmp_path); state=connect(tmp_path/"state.db"); device=identity(); ws="personal"; cfg={"user":"user","device":device,"workspaces":{ws:{"kind":"personal","epoch":1}},"keys":{f"{ws}:1":b64(bytes(range(32)))}}; heads={}
    first=scan(core,state); timed=[r for r in first if r["kind"] in ("git.checkpoint","file.version")]; assert timed and all("observed_at" in r and "observed_at" not in r["payload"] for r in timed)
    assert all(publish(cfg,state,ws,r,tmp_path/"client",True,heads) for r in first); baseline=state.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    assert not any(publish(cfg,state,ws,r,tmp_path/"client",True,heads) for r in scan(core,state)) and state.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]==baseline
    (repo/"a.py").write_text("changed\n"); emitted={r["kind"] for r in scan(core,state) if publish(cfg,state,ws,r,tmp_path/"client",True,heads)}
    assert emitted=={"git.checkpoint","file.version"} and state.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]==baseline+2


def test_row_attestation_survives_state_loss_and_tracks_change_and_reversion(tmp_path):
    repo,core=source(tmp_path); state=connect(tmp_path/"state.db"); records=scan(core,state); core.close(); state.close(); root,device=identity("root"),identity("device"); user=public_id(root["sign_public"]); cert=certificate(root,user,device); entry={"user":user,"root_public":root["sign_public"],"device":public(device),"certificate":cert,"history":True}; control={"workspace":"w","revision":1,"epoch":1,"devices":{device["id"]:entry}}; cfg={"user":user,"device":device,"workspaces":{"w":{"kind":"personal","epoch":1}},"controls":{"w":control},"server_state":{"workspaces":[{"id":"w","controls":[control]}]}}
    assert attest_rows(tmp_path/"source.db",cfg,"w",records)==4 and attest_rows(tmp_path/"source.db",cfg,"w",records)==0; Path(tmp_path/"state.db").unlink(); db=duckdb.connect(str(tmp_path/"source.db")); db.execute("UPDATE conversations SET title='changed'"); db.close(); state=connect(tmp_path/"new-state.db"); core=duckdb.connect(str(tmp_path/"source.db"),read_only=True); changed=scan(core,state); core.close(); assert attest_rows(tmp_path/"source.db",cfg,"w",changed)==1; db=duckdb.connect(str(tmp_path/"source.db")); db.execute("UPDATE conversations SET title='title'"); db.close(); core=duckdb.connect(str(tmp_path/"source.db"),read_only=True); reverted=scan(core,state); core.close(); assert attest_rows(tmp_path/"source.db",cfg,"w",reverted)==1
    db=duckdb.connect(str(tmp_path/"source.db"),read_only=True); rows=db.execute("SELECT content_hash,revision,previous_revision FROM remote.row_proofs WHERE row_kind='conversations' ORDER BY previous_revision NULLS FIRST").fetchall(); assert len(rows)==3 and rows[0][0]==rows[2][0] and len({r[1] for r in rows})==3 and rows[1][2]==rows[0][1] and rows[2][2]==rows[1][1] and db.execute("SELECT COUNT(*) FROM remote.workspace_controls").fetchone()[0]==1; db.close()


def test_refounded_successor_keeps_origin_and_separates_current_authorization(tmp_path):
    repo,core=source(tmp_path); state=connect(tmp_path/"state.db"); records=scan(core,state); core.close(); root,device=identity("root"),identity("device"); user=public_id(root["sign_public"]); cert=certificate(root,user,device); entry={"user":user,"root_public":root["sign_public"],"device":public(device),"certificate":cert,"history":True}; control=lambda ws:{"workspace":ws,"revision":1,"epoch":1,"devices":{device["id"]:entry}}; cfg=lambda ws:{"user":user,"device":device,"workspaces":{ws:{"kind":"team","epoch":1}},"controls":{ws:control(ws)},"server_state":{"workspaces":[{"id":ws,"controls":[control(ws)]}]}}; assert attest_rows(tmp_path/"source.db",cfg("origin"),"origin",records)==4; db=duckdb.connect(str(tmp_path/"source.db")); old=db.execute("SELECT revision FROM remote.row_proofs WHERE row_kind='conversations'").fetchone()[0]; db.execute("UPDATE conversations SET title='after refound'"); db.close(); core=duckdb.connect(str(tmp_path/"source.db"),read_only=True); changed=scan(core,state); core.close(); assert attest_rows(tmp_path/"source.db",cfg("replacement"),"replacement",changed,{"origin"})==1
    db=duckdb.connect(str(tmp_path/"source.db"),read_only=True); assert db.execute("SELECT workspace_id,authorization_workspace_id,previous_revision FROM remote.row_proofs WHERE previous_revision IS NOT NULL").fetchone()==("origin","replacement",old); db.close()


def test_row_attestation_refuses_to_guess_between_concurrent_heads(tmp_path):
    repo,core=source(tmp_path); state=connect(tmp_path/"state.db"); records=scan(core,state); core.close(); state.close(); root,device=identity("root"),identity("device"); user=public_id(root["sign_public"]); cert=certificate(root,user,device); entry={"user":user,"root_public":root["sign_public"],"device":public(device),"certificate":cert,"history":True}; control={"workspace":"w","revision":1,"epoch":1,"devices":{device["id"]:entry}}; cfg={"user":user,"device":device,"workspaces":{"w":{"kind":"personal","epoch":1}},"controls":{"w":control},"server_state":{"workspaces":[{"id":"w","controls":[control]}]}}; attest_rows(tmp_path/"source.db",cfg,"w",records)
    db=duckdb.connect(str(tmp_path/"source.db")); base=db.execute("SELECT revision FROM remote.row_proofs WHERE row_kind='conversations'").fetchone()[0]; row=lambda title:projection_module.logical_row("conversations",["id","source","title","created_at","updated_at","model","cwd","git_branch","project_id","metadata"],["c","codex",title,"2026-01-01","2026-01-01","m",str(repo),None,None,"{}"]); [project_row_proof(db,projection_module.row_proof(device,user,"w",1,row(title),base),root["sign_public"],cert) for title in ("branch-a","branch-b")]; db.execute("UPDATE conversations SET title='third'"); db.close(); fresh=connect(tmp_path/"fresh.db"); core=duckdb.connect(str(tmp_path/"source.db"),read_only=True); current=scan(core,fresh); core.close(); fresh.close()
    with pytest.raises(ValueError,match="row revision conflict"): attest_rows(tmp_path/"source.db",cfg,"w",current)


def test_incoming_concurrent_row_is_retained_without_overwrite(tmp_path):
    root,device=identity("root"),identity("device"); user=public_id(root["sign_public"]); cert=certificate(root,user,device); entry={"user":user,"root_public":root["sign_public"],"device":public(device),"certificate":cert,"history":True}; control={"workspace":"w","revision":1,"epoch":1,"devices":{device["id"]:entry}}; fields=["id","source","title","created_at","updated_at","model","cwd","git_branch","project_id","metadata"]; row=lambda title:logical_row("conversations",fields,["c","codex",title,"2026-01-01","2026-01-01",None,None,None,None,"{}"]); first=row_proof(device,user,"w",1,row("base")); a=row_proof(device,user,"w",1,row("branch-a"),first["revision"]); b=row_proof(device,user,"w",1,row("branch-b"),first["revision"])
    assert apply_row_replica(tmp_path/"db",{"row":row("base"),"proof":first},"w",[control]) and apply_row_replica(tmp_path/"db",{"row":row("branch-a"),"proof":a},"w",[control]) and not apply_row_replica(tmp_path/"db",{"row":row("branch-b"),"proof":b},"w",[control]); db=duckdb.connect(str(tmp_path/"db"),read_only=True); assert db.execute("SELECT title FROM conversations").fetchone()[0]=="branch-a" and json.loads(db.execute("SELECT CAST(body AS VARCHAR) FROM remote.row_conflicts").fetchone()[0])["data"]["title"]=="branch-b" and db.execute("SELECT COUNT(DISTINCT revision) FROM remote.row_proofs").fetchone()[0]==3 and db.execute("SELECT proof_id IS NOT NULL FROM remote.row_origins").fetchone()[0]; db.close()


def test_row_replica_page_is_atomic(tmp_path):
    root,device=identity("root"),identity("device"); user=public_id(root["sign_public"]); cert=certificate(root,user,device); entry={"user":user,"root_public":root["sign_public"],"device":public(device),"certificate":cert,"history":True}; control={"workspace":"w","revision":1,"epoch":1,"devices":{device["id"]:entry}}; fields=["id","source","title","created_at","updated_at","model","cwd","git_branch","project_id","metadata"]; row=logical_row("conversations",fields,["c","codex","valid","2026-01-01","2026-01-01",None,None,None,None,"{}"]); proof=row_proof(device,user,"w",1,row); bad={**row,"data":{**row["data"],"title":"tampered"}}
    with pytest.raises(ValueError,match="invalid row proof"): apply_row_replicas(tmp_path/"db",[{"row":row,"proof":proof},{"row":bad,"proof":proof}],"w",[control])
    db=duckdb.connect(str(tmp_path/"db"),read_only=True); assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]==db.execute("SELECT COUNT(*) FROM remote.row_proofs").fetchone()[0]==db.execute("SELECT COUNT(*) FROM remote.workspace_controls").fetchone()[0]==0; db.close()


def test_provenance_projection_uses_signed_event_timestamp(tmp_path):
    state=connect(tmp_path/"state.db"); device=identity(); vid=digest({"file":"f","content":"h"}); payload={"id":vid,"file":"f","content_hash":"h"}; project(tmp_path/"db",state,event(device,1,"file.version",vid,payload,[],"2026-01-01T00:00:00Z"),"w",authors={device["id"]:"user"})
    db=duckdb.connect(str(tmp_path/"db"),read_only=True); assert str(db.execute("SELECT observed_at FROM provenance.file_versions WHERE id=?",(vid,)).fetchone()[0])=="2026-01-01 00:00:00"; db.close()


def test_optional_projection_bridge_contract_fails_closed(monkeypatch):
    class Entry:
        def load(self): return lambda:{"v":2,"records":lambda *_:[],"project":lambda *_:None}
    bridges.cache_clear(); monkeypatch.setattr(projection_module,"entry_points",lambda **_:[Entry()])
    with pytest.raises(ValueError,match="Unsupported remote bridge"): bridges()
    bridges.cache_clear()


def test_event_support_is_exact_and_unknowns_fail_closed(monkeypatch):
    monkeypatch.setattr(projection_module,"bridges",lambda:[]); classify=lambda kind,version:event_support({"kind":kind,"payload_v":version}); assert classify("conversation.record",1)=="supported" and classify("conversation.record",2)==classify("future.opaque",1)=="required" and classify("memory.canonical",2)=="optional"; monkeypatch.setattr(projection_module,"bridges",lambda:[{"events":{("memory.canonical",1)}}]); assert classify("memory.canonical",2)=="required"


def test_out_of_order_revisions_converge_and_replay_deduplicates(tmp_path):
    state=connect(tmp_path/"state.db"); device=identity(); cols=["id","source","title","created_at","updated_at","model","cwd","git_branch","project_id","metadata"]
    old=event(device,1,"conversation.record","conversations:c",{"table":"conversations","columns":cols,"row":["c","codex","old","2026-01-01","2026-01-01",None,None,None,None,"{}"]},[],"2026-01-01T00:00:00Z")
    new=event(device,2,"conversation.record","conversations:c",{"table":"conversations","columns":cols,"row":["c","codex","new","2026-01-01","2026-01-02",None,None,None,None,"{}"]},[old["id"]],"2026-01-02T00:00:00Z")
    authors={device["id"]:"user"}; assert project(tmp_path/"db",state,new,"w","different",authors=authors) and not project(tmp_path/"db",state,old,"w","different",authors=authors) and not project(tmp_path/"db",state,new,"w","different",authors=authors)
    assert duckdb.connect(str(tmp_path/"db"),read_only=True).execute("SELECT title FROM conversations").fetchone()[0]=="new"


def test_projection_batch_rolls_back_duckdb_and_state_together(tmp_path):
    state=connect(tmp_path/"state.db"); device=identity(); cols=["id","source","title","created_at","updated_at","model","cwd","git_branch","project_id","metadata"]; payload={"table":"conversations","columns":cols,"row":["c","codex","valid","2026-01-01","2026-01-01",None,None,None,None,"{}"]}
    good=event(device,1,"conversation.record","conversations:c",payload,[],"2026-01-01T00:00:00Z"); bad=event(device,2,"conversation.record","conversations:wrong",{**payload,"row":["bad",*payload["row"][1:]]},[good["id"]],"2026-01-01T00:00:01Z")
    with pytest.raises(ValueError,match="schema"): project_many(tmp_path/"db",state,[("w",good),("w",bad)],"other",authors={device["id"]:"user"})
    db=duckdb.connect(str(tmp_path/"db"),read_only=True); assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]==0 and db.execute("SELECT COUNT(*) FROM remote.row_origins").fetchone()[0]==0; db.close(); assert not state.execute("SELECT * FROM heads").fetchall()


def test_record_schema_is_fixed_and_same_origin_ids_from_authors_do_not_collide(tmp_path):
    state=connect(tmp_path/"state.db"); a,b=identity("a"),identity("b"); cols=["id","source","title","created_at","updated_at","model","cwd","git_branch","project_id","metadata"]
    for i,device in enumerate((a,b)): value=event(device,1,"conversation.record","conversations:c",{"table":"conversations","columns":cols,"row":["c","codex",device["name"],"2026-01-01","2026-01-01",None,None,None,None,"{}"]},[],f"2026-01-0{i+1}T00:00:00Z"); assert project(tmp_path/"db",state,value,"w","other",authors={device["id"]:f"user-{i}"})
    db=duckdb.connect(str(tmp_path/"db"),read_only=True); assert {r[0] for r in db.execute("SELECT title FROM conversations").fetchall()}=={"a","b"} and {r[0] for r in db.execute("SELECT author_user_id FROM remote.row_origins").fetchall()}=={"user-0","user-1"}; db.close()
    bad=event(a,2,"conversation.record","conversations:x",{"table":"conversations","columns":["id); DROP TABLE conversations; --"],"row":["x"]},[],"2026-01-03T00:00:00Z")
    import pytest
    with pytest.raises(ValueError,match="schema"): project(tmp_path/"db",state,bad,"w","other")


def test_same_user_source_identity_converges_across_devices_and_requires_verified_owner(tmp_path):
    state=connect(tmp_path/"state.db"); a,b=identity("a"),identity("b"); cols=["id","source","title","created_at","updated_at","model","cwd","git_branch","project_id","metadata"]; authors={a["id"]:"user",b["id"]:"user"}
    values=[event(device,1,"conversation.record","conversations:c",{"table":"conversations","columns":cols,"row":["c","codex",device["name"],"2026-01-01","2026-01-01",None,None,None,None,"{}"]},[],f"2026-01-0{i+1}T00:00:00Z") for i,device in enumerate((a,b))]
    with pytest.raises(ValueError,match="verified author user"): project(tmp_path/"db",state,values[0],"w","other")
    assert all(project(tmp_path/"db",state,value,"w","other",authors=authors) for value in values); db=duckdb.connect(str(tmp_path/"db"),read_only=True); origin=db.execute("SELECT physical_row_id,author_user_id,author_device_id FROM remote.row_origins").fetchone(); assert db.execute("SELECT COUNT(*),MAX(title) FROM conversations").fetchone()==(1,"b") and origin==(foreign_id("w","user","conversations","c"),"user",b["id"]); db.close(); assert tuple(state.execute("SELECT author_user,entity,event FROM heads").fetchone())==("user","conversations:c",values[1]["id"])


def test_publication_head_allows_exact_reversion(tmp_path):
    state=connect(tmp_path/"state.db"); device=identity(); ws="personal"; cfg={"user":"user","device":device,"workspaces":{ws:{"kind":"personal","epoch":1}},"keys":{f"{ws}:1":b64(bytes(range(32)))}}; record=lambda title:{"kind":"conversation.record","entity":"conversations:c","payload":{"table":"conversations","columns":["id","source","title","created_at","updated_at","model","cwd","git_branch","project_id","metadata"],"row":["c","codex",title,"2026-01-01","2026-01-01",None,None,None,None,"{}"]}}
    first=publish(cfg,state,ws,record("a"),tmp_path,True); assert publish(cfg,state,ws,record("a"),tmp_path,True)==first; second=publish(cfg,state,ws,record("b"),tmp_path,True); reverted=publish(cfg,state,ws,record("a"),tmp_path,True); head=state.execute("SELECT owner,revision,event FROM publication_heads").fetchone(); assert len({first,second,reverted})==3 and state.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]==3 and tuple(head)==("user",digest(record("a")["payload"]),reverted)


def test_team_scope_includes_prompt_turn_and_linked_repo_only(tmp_path):
    repo,core=source(tmp_path); state=connect(tmp_path/"state.db"); personal=scan(core,state); rid=next(r["payload"]["id"] for r in personal if r["kind"]=="repository.observed"); team=scan(core,state,"team",[rid],[])
    kinds=[r["kind"] for r in team]; assert kinds.count("conversation.record")==1 and kinds.count("message.record")==2 and "file_edit.record" in kinds and "edit.observed" in kinds and "changeset.observed" not in kinds


def test_repository_policy_resolves_unobserved_worktree_conversation(tmp_path):
    repo,core=source(tmp_path); worktree=tmp_path/"worktree"; git(repo,"worktree","add","-qb","worktree-test",str(worktree)); core.execute("INSERT INTO conversations VALUES ('w','codex','worktree','2026-01-02','2026-01-02','m',?,NULL,NULL,'{}')",[str(worktree)]); core.execute("INSERT INTO messages VALUES ('wm','w','user','inspect',NULL,'2026-01-02','m','{}',NULL,NULL)"); state=connect(tmp_path/"state.db"); rid=next(r["payload"]["id"] for r in scan(core,state) if r["kind"]=="repository.observed"); team=scan(core,state,"team",[rid],[])
    assert {r["payload"]["row"][0] for r in team if r["kind"]=="conversation.record"}=={"c","w"}


def test_team_projection_never_reads_attachment_bodies(tmp_path,monkeypatch):
    repo,core=source(tmp_path); body=tmp_path/"secret.bin"; body.write_bytes(b"secret"); core.execute("INSERT INTO attachments (id,message_id,filename,path) VALUES ('a','m','secret.bin',?)",[str(body)]); state=connect(tmp_path/"state.db"); personal=scan(core,state); rid=next(r["payload"]["id"] for r in personal if r["kind"]=="repository.observed")
    original=Path.read_bytes; monkeypatch.setattr(Path,"read_bytes",lambda path:pytest.fail("team projection read attachment body") if path==body else original(path))
    records=scan(core,state,"team",[rid],[])
    assert any(r["kind"]=="attachment.record" for r in records) and not any(r["kind"]=="attachment.chunk" for r in records) and blob_replicas(tmp_path/"source.db",{"workspaces":{"team":{"kind":"team"}}},"team",records,{})==[]


def test_team_policy_routes_complete_cross_repo_conversation(tmp_path):
    first,core=source(tmp_path); second=tmp_path/"second"; second.mkdir(); git(second,"init","-q"); git(second,"config","user.email","a@b.c"); git(second,"config","user.name","A"); (second/"private.py").write_text("private\n"); git(second,"add","."); git(second,"commit","-qm","init"); core.execute("INSERT INTO file_edits VALUES ('private','m',?,'write','private\n','2026-01-01 00:00:01',NULL)",[str(second/'private.py')]); state=connect(tmp_path/"state.db"); all_records=scan(core,state); repos={r["payload"]["remotes"][0] if r["payload"]["remotes"] else r["payload"]["id"]:r["payload"]["id"] for r in all_records if r["kind"]=="repository.observed"}; first_id=next(r["payload"]["id"] for r in all_records if r["kind"]=="repository.observed" and r["payload"]["head"]==git(first,"rev-parse","HEAD"))
    routed=scan(core,state,"team",[first_id],[]); assert sum(r["kind"]=="file_edit.record" for r in routed)==sum(r["kind"]=="edit.observed" for r in routed)==2 and {r["kind"] for r in routed}>={"conversation.record","message.record","repository.observed"} and not any(r["kind"]=="turn.boundary" for r in routed)


def test_path_policy_match_routes_complete_conversation(tmp_path):
    allowed,private=tmp_path/"project",tmp_path/"project-private"; allowed.mkdir(); private.mkdir(); (allowed/"a.py").write_text("a"); (private/"b.py").write_text("b"); core=duckdb.connect(str(tmp_path/"core.db")); init_schema(core); core.execute("INSERT INTO conversations VALUES ('c','codex','paths','2026-01-01','2026-01-01','m',?,NULL,NULL,'{}')",[str(tmp_path)]); core.execute("INSERT INTO messages VALUES ('m','c','assistant','done',NULL,'2026-01-01','m','{}',NULL,NULL)"); core.execute("INSERT INTO file_edits VALUES ('a','m',?,'write','a','2026-01-01',NULL),('b','m',?,'write','b','2026-01-01',NULL)",[str(allowed/'a.py'),str(private/'b.py')]); state=connect(tmp_path/"state.db")
    records=scan(core,state,"team",[],[str(allowed)]); assert sum(r["kind"]=="file_edit.record" for r in records)==2 and not any(r["kind"]=="turn.boundary" for r in records) and str(private) not in json.dumps(records)


def test_per_workspace_device_chain_accepts_reorder_and_rejects_replay_or_bad_parent(tmp_path):
    state=connect(tmp_path/"state.db"); device=identity(); first=event(device,1,"x","1",{},[],"2026-01-01T00:00:00Z"); second=event(device,2,"x","2",{},[first["id"]],"2026-01-01T00:00:01Z"); assert sequence(state,"team",second) and sequence(state,"team",first)
    assert state.execute("SELECT COUNT(*) FROM event_sequences").fetchone()[0]==2 and state.execute("SELECT COUNT(*) FROM sequence_gaps").fetchone()[0]==0
    bad=event(device,3,"x","3",{},["wrong"],"2026-01-01T00:00:02Z")
    import pytest
    with pytest.raises(ValueError,match="chain"): sequence(state,"team",bad)
    replay=event(device,2,"x","other",{},[first["id"]],"2026-01-01T00:00:03Z")
    with pytest.raises(ValueError,match="replay"): sequence(state,"team",replay)
    assert sequence(state,"personal",replay)


def test_completed_remote_attachment_is_rescued_into_archive_storage(tmp_path):
    db_path=tmp_path/"data/convos.db"; db_path.parent.mkdir(); db=duckdb.connect(str(db_path)); init_schema(db); db.execute("INSERT INTO conversations VALUES ('c','codex','attachment','2026-01-01','2026-01-01',NULL,NULL,NULL,NULL,'{}')"); db.execute("INSERT INTO messages VALUES ('m','c','user','file',NULL,'2026-01-01',NULL,'{}',NULL,NULL)"); old=tmp_path/"remote/attachments/w/blob"; old.parent.mkdir(parents=True); old.write_bytes(b"canonical"); db.execute("INSERT INTO attachments (id,message_id,filename,size,path) VALUES ('a','m','a.bin',?,?)",(old.stat().st_size,str(old))); db.close()
    assert relocate_attachments(db_path,tmp_path/"remote/attachments")==1 and not old.exists() and relocate_attachments(db_path,tmp_path/"remote/attachments")==0
    db=duckdb.connect(str(db_path),read_only=True); path=Path(db.execute("SELECT path FROM attachments WHERE id='a'").fetchone()[0]); db.close(); assert path.parent==tmp_path/"data/attachments" and path.read_bytes()==b"canonical" and os.stat(path).st_mode&0o777==0o600
