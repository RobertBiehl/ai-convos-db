import json, subprocess

import duckdb
from ai_convos.cli import init_schema
from ai_convos_remote.projection import connect, project, rebuild, scan, sequence
from ai_convos_remote.protocol import event, identity


def git(path,*args): return subprocess.run(("git","-C",str(path),*args),check=True,capture_output=True).stdout.decode().strip()
def source(tmp_path):
    repo=tmp_path/"repo"; repo.mkdir(); git(repo,"init","-q"); git(repo,"config","user.email","a@b.c"); git(repo,"config","user.name","A"); (repo/"a.py").write_text("new\n"); git(repo,"add","."); git(repo,"commit","-qm","init")
    db=duckdb.connect(str(tmp_path/"source.db")); init_schema(db); db.execute("INSERT INTO conversations VALUES ('c','codex','title','2026-01-01','2026-01-01','m',?,NULL,NULL,'{}')",[str(repo)]); db.execute("INSERT INTO messages VALUES ('u','c','user','change it',NULL,'2026-01-01 00:00:00','m','{}',NULL,NULL),('m','c','assistant','done',NULL,'2026-01-01 00:00:01','m','{}',NULL,NULL)"); db.execute("INSERT INTO file_edits VALUES ('e','m',?,'write','new\n','2026-01-01 00:00:01',NULL)",[str(repo/'a.py')]); return repo,db


def test_personal_scan_strips_local_roots_and_rebuilds_duckdb(tmp_path):
    repo,core=source(tmp_path); state=connect(tmp_path/"state.db"); records=scan(core,state,"source-device"); raw=json.dumps(records)
    assert str(repo) not in raw and len(records)>3
    remote=identity("remote"); events=[event(remote,i+1,r["kind"],r["entity"],r["payload"],[],f"2026-01-01T00:00:{i:02d}Z") for i,r in enumerate(records)]
    for i,value in enumerate(events): state.execute("INSERT INTO event_log VALUES (?,?,?,?,?,?)",("personal",value["id"],i,"in",json.dumps(value),"{}")); project(tmp_path/"target.db",state,value,"personal","other-device")
    target=duckdb.connect(str(tmp_path/"target.db"),read_only=True); assert target.execute("SELECT title,cwd FROM conversations").fetchone()==("title",None); assert target.execute("SELECT content FROM messages WHERE role='user'").fetchone()[0]=="change it"; assert target.execute("SELECT file_path FROM file_edits").fetchone()[0]=="a.py"; target.close()
    before=state.execute("SELECT COUNT(*) FROM edits").fetchone()[0]; (tmp_path/"target.db").unlink(); assert rebuild(tmp_path/"target.db",state)==len(events); assert duckdb.connect(str(tmp_path/"target.db"),read_only=True).execute("SELECT COUNT(*) FROM messages").fetchone()[0]==2 and state.execute("SELECT COUNT(*) FROM edits").fetchone()[0]==before


def test_out_of_order_revisions_converge_and_replay_deduplicates(tmp_path):
    state=connect(tmp_path/"state.db"); device=identity(); cols=["id","source","title","created_at","updated_at","model","cwd","git_branch","project_id","metadata"]
    old=event(device,1,"conversation.record","conversations:c",{"table":"conversations","columns":cols,"row":["c","codex","old","2026-01-01","2026-01-01",None,None,None,None,"{}"]},[],"2026-01-01T00:00:00Z")
    new=event(device,2,"conversation.record","conversations:c",{"table":"conversations","columns":cols,"row":["c","codex","new","2026-01-01","2026-01-02",None,None,None,None,"{}"]},[old["id"]],"2026-01-02T00:00:00Z")
    assert project(tmp_path/"db",state,new,"w","different") and not project(tmp_path/"db",state,old,"w","different") and not project(tmp_path/"db",state,new,"w","different")
    assert duckdb.connect(str(tmp_path/"db"),read_only=True).execute("SELECT title FROM conversations").fetchone()[0]=="new"


def test_record_schema_is_fixed_and_same_origin_ids_from_authors_do_not_collide(tmp_path):
    state=connect(tmp_path/"state.db"); a,b=identity("a"),identity("b"); cols=["id","source","title","created_at","updated_at","model","cwd","git_branch","project_id","metadata"]
    for i,device in enumerate((a,b)): value=event(device,1,"conversation.record","conversations:c",{"table":"conversations","columns":cols,"row":["c","codex",device["name"],"2026-01-01","2026-01-01",None,None,None,None,"{}"]},[],f"2026-01-0{i+1}T00:00:00Z"); assert project(tmp_path/"db",state,value,"w","other")
    assert {r[0] for r in duckdb.connect(str(tmp_path/"db"),read_only=True).execute("SELECT title FROM conversations").fetchall()}=={"a","b"}
    bad=event(a,2,"conversation.record","conversations:x",{"table":"conversations","columns":["id); DROP TABLE conversations; --"],"row":["x"]},[],"2026-01-03T00:00:00Z")
    import pytest
    with pytest.raises(ValueError,match="schema"): project(tmp_path/"db",state,bad,"w","other")


def test_team_scope_includes_prompt_turn_and_linked_repo_only(tmp_path):
    repo,core=source(tmp_path); state=connect(tmp_path/"state.db"); personal=scan(core,state,"d"); rid=next(r["payload"]["id"] for r in personal if r["kind"]=="repository.observed"); team=scan(core,state,"d","team",[rid],[])
    kinds=[r["kind"] for r in team]; assert kinds.count("conversation.record")==1 and kinds.count("message.record")==2 and "file_edit.record" in kinds and "changeset.observed" in kinds


def test_cross_repo_changeset_is_sliced_per_edit_with_opaque_boundary(tmp_path):
    first,core=source(tmp_path); second=tmp_path/"second"; second.mkdir(); git(second,"init","-q"); git(second,"config","user.email","a@b.c"); git(second,"config","user.name","A"); (second/"private.py").write_text("private\n"); git(second,"add","."); git(second,"commit","-qm","init"); core.execute("INSERT INTO file_edits VALUES ('private','m',?,'write','private\n','2026-01-01 00:00:01',NULL)",[str(second/'private.py')]); state=connect(tmp_path/"state.db"); all_records=scan(core,state,"d"); repos={r["payload"]["remotes"][0] if r["payload"]["remotes"] else r["payload"]["id"]:r["payload"]["id"] for r in all_records if r["kind"]=="repository.observed"}; first_id=next(r["payload"]["id"] for r in all_records if r["kind"]=="repository.observed" and r["payload"]["head"]==git(first,"rev-parse","HEAD"))
    sliced=scan(core,state,"d","team",[first_id],[]); assert sum(r["kind"]=="file_edit.record" for r in sliced)==1 and sum(r["kind"]=="edit.observed" for r in sliced)==1; boundary=next(r for r in sliced if r["kind"]=="changeset.boundary"); assert boundary["payload"]["hidden_count"]==1 and "private.py" not in json.dumps(sliced)
    both=scan(core,state,"d","team",[r["payload"]["id"] for r in all_records if r["kind"]=="repository.observed"],[]); assert sum(r["kind"]=="edit.observed" for r in both)==2 and not any(r["kind"]=="changeset.boundary" for r in both)


def test_path_policy_uses_path_boundary_not_string_prefix(tmp_path):
    allowed,private=tmp_path/"project",tmp_path/"project-private"; allowed.mkdir(); private.mkdir(); (allowed/"a.py").write_text("a"); (private/"b.py").write_text("b"); core=duckdb.connect(str(tmp_path/"core.db")); init_schema(core); core.execute("INSERT INTO conversations VALUES ('c','codex','paths','2026-01-01','2026-01-01','m',?,NULL,NULL,'{}')",[str(tmp_path)]); core.execute("INSERT INTO messages VALUES ('m','c','assistant','done',NULL,'2026-01-01','m','{}',NULL,NULL)"); core.execute("INSERT INTO file_edits VALUES ('a','m',?,'write','a','2026-01-01',NULL),('b','m',?,'write','b','2026-01-01',NULL)",[str(allowed/'a.py'),str(private/'b.py')]); state=connect(tmp_path/"state.db")
    records=scan(core,state,"d","team",[],[str(allowed)]); assert sum(r["kind"]=="file_edit.record" for r in records)==1 and "project-private" not in json.dumps(records)


def test_per_workspace_device_chain_accepts_reorder_and_rejects_replay_or_bad_parent(tmp_path):
    state=connect(tmp_path/"state.db"); device=identity(); first=event(device,1,"x","1",{},[],"2026-01-01T00:00:00Z"); second=event(device,2,"x","2",{},[first["id"]],"2026-01-01T00:00:01Z"); assert sequence(state,"team",second) and sequence(state,"team",first)
    bad=event(device,3,"x","3",{},["wrong"],"2026-01-01T00:00:02Z")
    import pytest
    with pytest.raises(ValueError,match="chain"): sequence(state,"team",bad)
    replay=event(device,2,"x","other",{},[first["id"]],"2026-01-01T00:00:03Z")
    with pytest.raises(ValueError,match="replay"): sequence(state,"team",replay)
    assert sequence(state,"personal",replay)


def test_attachment_chunk_conflicts_are_rejected(tmp_path):
    state=connect(tmp_path/"state.db"); device=identity(); data="eA=="; payload={"attachment":"a","blob":"2d711642b726b04401627ca9fbac32f5da7e5c8530fb1903cc4db02258717921","index":0,"total":2,"sha256":"2d711642b726b04401627ca9fbac32f5da7e5c8530fb1903cc4db02258717921","size":2,"data":data}; one=event(device,1,"attachment.chunk",f"attachment:a:{payload['blob']}:0",payload,[],"2026-01-01T00:00:00Z"); assert project(tmp_path/"db",state,one,"w","other")
    import pytest
    changed={**payload,"total":3}; conflict=event(device,2,"attachment.chunk",f"attachment:a:{payload['blob']}:0",changed,[one["id"]],"2026-01-01T00:00:01Z")
    with pytest.raises(ValueError,match="conflict"): project(tmp_path/"db",state,conflict,"w","other")
