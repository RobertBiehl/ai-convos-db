import json, subprocess, sys

import duckdb
import pytest
import ai_convos.cli as core_module
from ai_convos.cli import ARCHIVE_COLUMNS, archive_state, capture_provenance as capture, init_schema, project_archive_row, project_provenance as project, provenance_digest as digest, repository
from ai_convos_changegraph.provenance import query
from ai_convos_remote.protocol import event, identity


def git(path,*args): return subprocess.run(("git","-C",str(path),*args),check=True,capture_output=True).stdout.decode().strip()
def repo(path,name="x.py",content="one\n"):
    path.mkdir(); git(path,"init","-q"); git(path,"config","user.email","test@example.com"); git(path,"config","user.name","Test"); (path/name).write_text(content); git(path,"add","."); git(path,"commit","-qm","initial"); return path
def core(path,cwd,edits):
    db=duckdb.connect(str(path)); init_schema(db); db.execute("INSERT INTO conversations VALUES ('c','codex','cross repo','2026-01-01','2026-01-01','m',?,NULL,NULL,'{}')",[str(cwd)]); db.execute("INSERT INTO messages VALUES ('u','c','user','make the cross-repo change',NULL,'2025-12-31 23:59:59','m','{}',NULL,NULL),('m','c','assistant','done',NULL,'2026-01-01','m','{}',NULL,NULL)")
    for i,(file,kind,content,old) in enumerate(edits): db.execute("INSERT INTO file_edits VALUES (?,?,?,?,?,'2026-01-01',?)",[f'e{i}','m',str(file),kind,content,old])
    return db
def graph(path):
    db=duckdb.connect(str(path)); init_schema(db); return db


def test_path_independent_repo_cross_repo_changeset_and_canonical_schema(tmp_path):
    a,b=repo(tmp_path/"a",content="new a\n"),repo(tmp_path/"b",content="new b\n"); clone=tmp_path/"clone"; subprocess.run(("git","clone","-q",str(a),str(clone)),check=True); assert repository(a)["id"]==repository(clone)["id"]
    path=tmp_path/"core.db"; db=core(path,a,[(a/"x.py","write","new a\n",None),(b/"x.py","write","new b\n",None)]); db.close(); records=capture(path); db=duckdb.connect(str(path)); wire=json.dumps(records); assert str(a) not in wire and str(b) not in wire and not any(r["kind"]=="changeset.observed" for r in records)
    assert len({r["payload"]["id"] for r in records if r["kind"]=="repository.observed"})==2 and {r["payload"]["id"] for r in records if r["kind"]=="edit.observed"}=={"e0","e1"}
    row=query(db,"conversation_changes","c")[0]; assert row["repositories"]==2 and row["files"]==2 and row["prompt"]=="make the cross-repo change" and row["changeset_id"]=="m"
    assert len(query(db,"changeset_files","m"))==2 and query(db,"current_activity",str(a))[0]["repository"]==repository(a)["id"]
    tables={r[0] for r in db.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='provenance'").fetchall()}; assert tables=={"repositories","repository_checkouts","files","file_versions","file_edit_files","git_checkpoints","checkpoint_edits"}
    columns={r[0] for r in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='provenance'").fetchall()}; assert not columns&{"prompt","content","payload","workspace","author"}


def test_core_schema_upgrade_adds_canonical_schemas_without_rewriting_archive(tmp_path):
    db=duckdb.connect(str(tmp_path/"core.db")); init_schema(db); identity=archive_state(db)[0]; db.execute("INSERT INTO conversations VALUES ('keep','codex','preserved','2026-01-01','2026-01-01',NULL,NULL,NULL,NULL,'{}')"); db.execute("DROP SCHEMA provenance CASCADE"); db.execute("DROP SCHEMA remote CASCADE"); init_schema(db)
    assert db.execute("SELECT title FROM conversations WHERE id='keep'").fetchone()[0]=="preserved" and db.execute("SELECT COUNT(*) FROM provenance.repositories").fetchone()[0]==db.execute("SELECT COUNT(*) FROM remote.row_origins").fetchone()[0]==0 and archive_state(db)[0]==identity


def test_provenance_upgrade_preserves_edit_evidence_and_removes_unused_graph_tables(tmp_path):
    db=graph(tmp_path/"graph.db"); db.execute("ALTER TABLE provenance.file_edit_files RENAME COLUMN old_content_hash TO before_hash"); db.execute("ALTER TABLE provenance.file_edit_files RENAME COLUMN new_content_hash TO after_hash"); db.execute("ALTER TABLE provenance.git_checkpoints DROP COLUMN capture_source"); db.execute("CREATE TABLE provenance.assertions(id VARCHAR)"); db.execute("CREATE TABLE provenance.capture_gaps(id VARCHAR)"); db.execute("INSERT INTO provenance.file_edit_files VALUES ('e','f','old','new','captured_exact')"); init_schema(db)
    assert db.execute("SELECT old_content_hash,new_content_hash FROM provenance.file_edit_files").fetchone()==("old","new") and not {"assertions","capture_gaps"}&{r[0] for r in db.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='provenance'").fetchall()}


def test_archive_generation_is_transactional_and_counts_only_owned_rows(tmp_path):
    db=duckdb.connect(str(tmp_path/"core.db")); init_schema(db); initial=archive_state(db); row=["c","codex","one","2026-01-01","2026-01-01",None,None,None,None,"{}"]
    db.execute("BEGIN"); project_archive_row(db,"conversations",ARCHIVE_COLUMNS["conversations"],row); assert archive_state(db)[1:]==(initial[1]+1,1); db.execute("ROLLBACK"); assert archive_state(db)==initial


def test_git_checkpoint_is_capture_observation_not_dirty_path_gap(tmp_path):
    root=repo(tmp_path/"repo",content="old\n"); (root/"x.py").write_text("new\n"); git(root,"add","x.py"); git(root,"commit","-qm","agent edit"); (root/"manual.py").write_text("outside capture\n")
    path=tmp_path/"core.db"; db=core(path,root,[(root/"x.py","write","new\n",None)]); db.close(); capture(path); db=duckdb.connect(str(path))
    checkpoint=query(db,"checkpoint_states")[0]; assert "manual.py" in json.loads(checkpoint["paths"]) and checkpoint["capture_source"]=="sync" and query(db,"commit_conversations",git(root,"rev-parse","HEAD"))[0]["conversation"]=="c"
    assert query(db,"file_history","x.py")[0]["evidence"]=="captured_exact" and query(db,"file_history","x.py")[0]["prompt"]=="make the cross-repo change"


def test_provenance_failure_rolls_back_only_enrichment(tmp_path,monkeypatch):
    root=repo(tmp_path/"repo"); path=tmp_path/"core.db"; db=core(path,root,[(root/"x.py","write","one\n",None)]); write=core_module.project_provenance; generation=archive_state(db)[1]; db.close()
    def fail(conn,value,*args):
        if value["kind"]=="file.observed": raise OSError("git evidence failed")
        return write(conn,value,*args)
    monkeypatch.setattr(core_module,"project_provenance",fail)
    with pytest.raises(OSError,match="evidence"): capture(path)
    db=duckdb.connect(str(path))
    assert db.execute("SELECT COUNT(*) FROM file_edits").fetchone()[0]==1 and db.execute("SELECT COUNT(*) FROM provenance.repositories").fetchone()[0]==0 and db.execute("SELECT COUNT(*) FROM provenance.repository_checkouts").fetchone()[0]==0 and archive_state(db)[1]==generation


def test_checkpoint_diff_uses_local_git_evidence(tmp_path):
    root=repo(tmp_path/"repo",content="one\n"); path=tmp_path/"core.db"; db=core(path,root,[(root/"x.py","write","one\n",None)]); db.close(); first=capture(path); cp1=next(r["payload"]["id"] for r in first if r["kind"]=="git.checkpoint"); (root/"x.py").write_text("two\n"); git(root,"add","x.py"); git(root,"commit","-qm","second"); second=capture(path); cp2=next(r["payload"]["id"] for r in second if r["kind"]=="git.checkpoint"); db=duckdb.connect(str(path))
    result=query(db,"checkpoint_diff",f"{cp1}..{cp2}")[0]; assert result["head_before"]!=result["head_after"] and result["changed"]==["M\tx.py"]


def test_concurrent_edits_remain_version_branches(tmp_path):
    db=graph(tmp_path/"graph.db"); db.execute("INSERT INTO conversations VALUES ('c2','codex','a','2026-01-01','2026-01-01',NULL,NULL,NULL,NULL,'{}'),('c3','codex','b','2026-01-01','2026-01-01',NULL,NULL,NULL,NULL,'{}')"); db.execute("INSERT INTO messages VALUES ('m2','c2','assistant','',NULL,'2026-01-01',NULL,'{}',NULL,NULL),('m3','c3','assistant','',NULL,'2026-01-01',NULL,'{}',NULL,NULL)"); db.execute("INSERT INTO file_edits VALUES ('e2','m2','shared.py','edit','a','2026-01-01','base'),('e3','m3','shared.py','edit','b','2026-01-01','base')")
    a,b=identity("a"),identity("b"); file_id=digest({"repository":None,"path":"shared.py"}); project(db,event(a,1,"file.observed",file_id,{"id":file_id,"repository":None,"path":"shared.py","kind":"external"},[],"2026-01-01T00:00:00Z"))
    for i,(device,after) in enumerate(((a,"after-a"),(b,"after-b")),2): payload={"id":f"e{i}","turn":f"m{i}","file":file_id,"repository":None,"old_content_hash":"same-base","new_content_hash":after,"evidence":"captured_exact"}; project(db,event(device,i,"edit.observed",payload["id"],payload,[],"2026-01-01T00:00:00Z"))
    rows=query(db,"file_history","shared.py"); assert len(rows)==2 and {r["new_content_hash"] for r in rows}=={"after-a","after-b"} and {r["old_content_hash"] for r in rows}=={"same-base"}


def test_repository_identity_distinguishes_fork_but_preserves_lineage_and_unborn_repo(tmp_path):
    source=repo(tmp_path/"source"); git(source,"remote","add","origin","https://example.com/acme/repo.git"); clone=tmp_path/"clone"; subprocess.run(("git","clone","-q",str(source),str(clone)),check=True); git(clone,"remote","set-url","origin","https://example.com/acme/repo.git"); fork=tmp_path/"fork"; subprocess.run(("git","clone","-q",str(source),str(fork)),check=True); git(fork,"remote","set-url","origin","https://example.com/other/fork.git")
    a,b,c=repository(source),repository(clone),repository(fork); assert a["id"]==b["id"]!=c["id"] and a["lineage"]==b["lineage"]==c["lineage"]
    empty=tmp_path/"empty"; empty.mkdir(); git(empty,"init","-q"); (empty/"new.py").write_text("new\n"); observed=repository(empty); assert observed["head"]=="" and observed["lineage"] is None
    path=tmp_path/"empty-core.db"; db=core(path,empty,[(empty/"new.py","write","new\n",None)]); db.close(); records=capture(path); assert any(r["kind"]=="git.checkpoint" and r["payload"]["head"]=="" for r in records)


def test_semantic_capture_ids_are_existing_archive_identities(tmp_path):
    root=repo(tmp_path/"repo"); path=tmp_path/"core.db"; db=core(path,root,[(root/"x.py","write","one\n",None)]); db.close(); records=capture(path); one=lambda kind:next(r["payload"]["id"] for r in records if r["kind"]==kind)
    assert one("edit.observed")=="e0" and one("file.observed")==digest({"repository":repository(root)["id"],"path":"x.py"})


def test_provenance_git_observation_does_not_hold_duckdb_lock(tmp_path,monkeypatch):
    root=repo(tmp_path/"repo"); path=tmp_path/"core.db"; db=core(path,root,[(root/"x.py","write","one\n",None)]); db.close(); run=core_module._git_run
    def unlocked(repo,*args): subprocess.run((sys.executable,"-c","import duckdb,sys; duckdb.connect(sys.argv[1]).close()",str(path)),check=True); return run(repo,*args)
    monkeypatch.setattr(core_module,"_git_run",unlocked); capture(path)
