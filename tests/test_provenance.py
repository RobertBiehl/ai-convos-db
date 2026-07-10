import json, subprocess

import duckdb
from ai_convos.cli import init_schema
from ai_convos_remote.provenance import capture, connect, project, query, repository
from ai_convos_remote.protocol import event, identity


def git(path,*args): return subprocess.run(("git","-C",str(path),*args),check=True,capture_output=True).stdout.decode().strip()
def repo(path,name="x.py",content="one\n"):
    path.mkdir(); git(path,"init","-q"); git(path,"config","user.email","test@example.com"); git(path,"config","user.name","Test"); (path/name).write_text(content); git(path,"add","."); git(path,"commit","-qm","initial"); return path
def core(path,cwd,edits):
    db=duckdb.connect(str(path)); init_schema(db); db.execute("INSERT INTO conversations VALUES ('c','codex','cross repo','2026-01-01','2026-01-01','m',?,NULL,NULL,'{}')",[str(cwd)]); db.execute("INSERT INTO messages VALUES ('u','c','user','make the cross-repo change',NULL,'2025-12-31 23:59:59','m','{}',NULL,NULL),('m','c','assistant','done',NULL,'2026-01-01','m','{}',NULL,NULL)")
    for i,(file,kind,content,old) in enumerate(edits): db.execute("INSERT INTO file_edits VALUES (?,?,?,?,?,'2026-01-01',?)",[f'e{i}','m',str(file),kind,content,old])
    return db


def test_path_independent_repo_cross_repo_changeset_and_projection(tmp_path):
    a,b=repo(tmp_path/"a",content="new a\n"),repo(tmp_path/"b",content="new b\n"); clone=tmp_path/"clone"; subprocess.run(("git","clone","-q",str(a),str(clone)),check=True)
    assert repository(a)["id"] == repository(clone)["id"]
    source=core(tmp_path/"core.db",a,[(a/"x.py","write","new a\n",None),(b/"x.py","write","new b\n",None)]); graph=connect(tmp_path/"graph.db"); records=capture(source,graph,"device-a")
    wire=json.dumps(records); assert str(a) not in wire and str(b) not in wire
    assert len({r["payload"]["id"] for r in records if r["kind"]=="repository.observed"}) == 2
    assert len({r["payload"]["id"] for r in records if r["kind"]=="changeset.observed"}) == 1
    device=identity(); [project(graph,event(device,i+1,r["kind"],r["entity"],r["payload"],[],"2026-01-01T00:00:00Z"),"personal") for i,r in enumerate(records)]
    [project(graph,event(device,100+i,r["kind"],r["entity"],r["payload"],[],"2026-01-02T00:00:00Z"),"team") for i,r in enumerate(records)]
    row=query(graph,"conversation_changes","c")[0]; assert row["repositories"] == 2 and row["files"] == 2 and row["prompt"]=="make the cross-repo change"
    assert len(query(graph,"changeset_files",row["changeset_id"])) == 2
    assert query(graph,"device_activity",device["id"])[0]["events"]==len(records) and query(graph,"current_activity",str(a))[0]["repository"]==repository(a)["id"]
    assert query(graph,"team_activity",f"team|{a}")[0]["workspace"]=="team"


def test_git_checkpoint_exact_commit_link_and_unobserved_gap(tmp_path):
    root=repo(tmp_path/"repo",content="old\n"); (root/"x.py").write_text("new\n"); git(root,"add","x.py"); git(root,"commit","-qm","agent edit"); (root/"manual.py").write_text("outside capture\n")
    source=core(tmp_path/"core.db",root,[(root/"x.py","write","new\n",None)]); graph=connect(tmp_path/"graph.db"); records=capture(source,graph,"d")
    device=identity(); [project(graph,event(device,i+1,r["kind"],r["entity"],r["payload"],[],"2026-01-01T00:00:00Z"),"personal") for i,r in enumerate(records)]
    assert query(graph,"capture_gaps")[0]["path"] == "manual.py"
    assert query(graph,"commit_conversations",git(root,"rev-parse","HEAD"))[0]["conversation"] == "c"
    assert query(graph,"file_history","x.py")[0]["evidence"] == "captured_exact" and query(graph,"file_history","x.py")[0]["prompt"]=="make the cross-repo change"


def test_inferred_assertions_remain_typed_and_reversible(tmp_path):
    graph=connect(tmp_path/"graph.db"); device=identity(); payload={"id":"a1","left":"f1","relation":"inferred_rename","right":"f2","evidence":"git_similarity_90","status":"active","observed_at":"2026-01-01"}
    project(graph,event(device,1,"identity.assertion","a1",payload,[],"2026-01-01T00:00:00Z"),"team")
    assert query(graph,"identity_assertions","f1")[0]["relation"] == "inferred_rename"
    payload={**payload,"id":"a2","status":"retracted","evidence":"user_rejected"}; project(graph,event(device,2,"identity.assertion","a2",payload,["a1"],"2026-01-02T00:00:00Z"),"team")
    assert {r["status"] for r in query(graph,"identity_assertions","f1")} == {"active","retracted"}


def test_file_relationships_keep_distinct_semantics(tmp_path):
    graph=connect(tmp_path/"graph.db"); device=identity(); relations=("same_content","same_lineage","copied_from","generated_from","inferred_rename")
    for i,relation in enumerate(relations): payload={"id":f"a{i}","left":"file-a","relation":relation,"right":f"file-{i}","evidence":"captured_exact" if i<4 else "git_similarity_90","status":"active","observed_at":"2026-01-01"}; project(graph,event(device,i+1,"identity.assertion",payload["id"],payload,[],"2026-01-01T00:00:00Z"),"team")
    assert {r["relation"] for r in query(graph,"identity_assertions","file-a")}==set(relations)


def test_checkpoint_diff_uses_local_git_evidence(tmp_path):
    root=repo(tmp_path/"repo",content="one\n"); source=core(tmp_path/"core.db",root,[(root/"x.py","write","one\n",None)]); graph=connect(tmp_path/"graph.db"); device=identity(); first=capture(source,graph,"d"); seq=0
    for r in first: seq+=1; project(graph,event(device,seq,r["kind"],r["entity"],r["payload"],[],"2026-01-01T00:00:00Z"),"personal")
    cp1=next(r["payload"]["id"] for r in first if r["kind"]=="git.checkpoint"); (root/"x.py").write_text("two\n"); git(root,"add","x.py"); git(root,"commit","-qm","second"); second=capture(source,graph,"d"); cp2=next(r["payload"]["id"] for r in second if r["kind"]=="git.checkpoint")
    for r in second: seq+=1; project(graph,event(device,seq,r["kind"],r["entity"],r["payload"],[],"2026-01-02T00:00:00Z"),"personal")
    result=query(graph,"checkpoint_diff",f"{cp1}..{cp2}")[0]; assert result["head_before"]!=result["head_after"] and result["changed"]==["M\tx.py"]


def test_concurrent_edits_remain_version_branches(tmp_path):
    graph=connect(tmp_path/"graph.db"); a,b=identity("a"),identity("b"); base={"id":"f","repository":None,"path":"shared.py","kind":"external"}; project(graph,event(a,1,"file.observed","f",base,[],"2026-01-01T00:00:00Z"),"personal")
    for i,(device,after) in enumerate(((a,"after-a"),(b,"after-b")),2):
        cs=f"cs{i}"; project(graph,event(device,i,"changeset.observed",cs,{"id":cs,"conversation":f"c{i}","turn":f"m{i}","prompt":"edit shared","observed_at":"2026-01-01"},[],"2026-01-01T00:00:00Z"),"personal"); project(graph,event(device,i+10,"edit.observed",f"e{i}",{"id":f"e{i}","changeset":cs,"file":"f","repository":None,"type":"edit","before_hash":"same-base","after_hash":after,"evidence":"captured_exact","observed_at":"2026-01-01","origin":f"e{i}"},[],"2026-01-01T00:00:00Z"),"personal")
    rows=query(graph,"file_history","shared.py"); assert len(rows)==2 and {r["after_hash"] for r in rows}=={"after-a","after-b"} and {r["before_hash"] for r in rows}=={"same-base"}


def test_repository_identity_distinguishes_fork_but_preserves_lineage_and_unborn_repo(tmp_path):
    source=repo(tmp_path/"source"); git(source,"remote","add","origin","https://example.com/acme/repo.git"); clone=tmp_path/"clone"; subprocess.run(("git","clone","-q",str(source),str(clone)),check=True); git(clone,"remote","set-url","origin","https://example.com/acme/repo.git"); fork=tmp_path/"fork"; subprocess.run(("git","clone","-q",str(source),str(fork)),check=True); git(fork,"remote","set-url","origin","https://example.com/other/fork.git")
    a,b,c=repository(source),repository(clone),repository(fork); assert a["id"]==b["id"]!=c["id"] and a["lineage"]==b["lineage"]==c["lineage"]
    empty=tmp_path/"empty"; empty.mkdir(); git(empty,"init","-q"); (empty/"new.py").write_text("new\n"); observed=repository(empty); assert observed["head"]=="" and observed["lineage"] is None
    graph=connect(tmp_path/"empty-graph.db"); records=capture(core(tmp_path/"empty-core.db",empty,[(empty/"new.py","write","new\n",None)]),graph,"d"); assert any(r["kind"]=="git.checkpoint" and r["payload"]["head"]=="" for r in records)


def test_capture_ids_are_author_scoped_while_file_identity_is_shared(tmp_path):
    root=repo(tmp_path/"repo"); source=core(tmp_path/"core.db",root,[(root/"x.py","write","one\n",None)]); ga,gb=connect(tmp_path/"a.db"),connect(tmp_path/"b.db"); a,b=capture(source,ga,"device-a"),capture(source,gb,"device-b")
    one=lambda records,kind: next(r["payload"]["id"] for r in records if r["kind"]==kind)
    assert one(a,"edit.observed")!=one(b,"edit.observed") and one(a,"changeset.observed")!=one(b,"changeset.observed") and one(a,"file.observed")==one(b,"file.observed")
