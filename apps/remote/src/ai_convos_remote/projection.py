"""Portable record/event projection. The immutable relay ledger can rebuild every local view."""
import base64, hashlib, json, os, shutil, sqlite3, time
from datetime import date, datetime
from functools import lru_cache
from importlib.metadata import entry_points
from pathlib import Path

import duckdb
from ai_convos.cli import ARCHIVE_COLUMNS as COLUMNS, PROVENANCE_KINDS as PROVENANCE, init_schema, observe_provenance, project_archive_row, project_provenance, project_row_proof, project_workspace_controls, repository as resolve_repository, set_attachment_path
from ai_convos_changegraph.provenance import query as graph_query
from .protocol import digest, logical_row, row_proof

STATE_VERSION="8"
STATE = """
CREATE TABLE IF NOT EXISTS outbox(workspace TEXT,event TEXT,entity TEXT,revision TEXT,author TEXT,seq INT,epoch INT,kind TEXT,payload_v INT,status TEXT,path TEXT,size INT,PRIMARY KEY(workspace,event)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS receipts(workspace TEXT,event TEXT,cursor INT,author TEXT,seq INT,epoch INT,kind TEXT,payload_v INT,entity TEXT,revision TEXT,status TEXT,PRIMARY KEY(workspace,event)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS receipt_cursor ON receipts(workspace,cursor);
CREATE TABLE IF NOT EXISTS history_sources(workspace TEXT,event TEXT,carrier TEXT,PRIMARY KEY(workspace,event)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS publication_heads(workspace TEXT,owner TEXT,entity TEXT,revision TEXT,event TEXT,PRIMARY KEY(workspace,owner,entity)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS cursors(workspace TEXT PRIMARY KEY,cursor INT) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS heads(workspace TEXT,author_user TEXT,entity TEXT,sort_key TEXT,event TEXT,PRIMARY KEY(workspace,author_user,entity)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS lazy_events(workspace TEXT,event TEXT,cursor INT,size INT,PRIMARY KEY(workspace,event)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS deferred_events(workspace TEXT,event TEXT,cursor INT,kind TEXT,payload_v INT,required INT,PRIMARY KEY(workspace,event)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS event_sequences(workspace TEXT,author TEXT,seq INT,event TEXT,PRIMARY KEY(workspace,author,seq)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS sequence_gaps(workspace TEXT,author TEXT,seq INT,parents TEXT,PRIMARY KEY(workspace,author,seq)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS attachment_parts(workspace TEXT,author TEXT,blob TEXT,idx INT,total INT,attachment TEXT,sha256 TEXT,size INT,chunk_hash TEXT,path TEXT,PRIMARY KEY(workspace,author,blob,idx)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS attachment_blobs(workspace TEXT,author TEXT,attachment TEXT,path TEXT,PRIMARY KEY(workspace,author,attachment)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS policies(workspace TEXT,kind TEXT,value TEXT,PRIMARY KEY(workspace,kind,value)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS sync_states(workspace TEXT PRIMARY KEY,lifecycle TEXT NOT NULL,tail INT NOT NULL DEFAULT 0,floor INT NOT NULL DEFAULT 0,error TEXT) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT) WITHOUT ROWID;
"""
STATE_TABLES={"outbox","receipts","history_sources","publication_heads","cursors","heads","lazy_events","deferred_events","event_sequences","sequence_gaps","attachment_parts","attachment_blobs","policies","sync_states","meta"}
STATE_FORBIDDEN={"published","event_log","history_material","history_outbox","history_queue","attachment_chunks","imported_rows","raw_events","repositories","files","file_versions","changesets","edits","checkpoints","assertions","gaps","boundaries","sharing_boundaries"}
TABLES={"conversation.record":"conversations","message.record":"messages","tool.record":"tool_calls","attachment.record":"attachments","artifact.record":"artifacts","file_edit.record":"file_edits"}
CORE_EVENTS={(kind,1) for kind in set(TABLES)|PROVENANCE|{"workspace.policy","workspace.membership","workspace.device","attachment.chunk","history.republish"}}; AUXILIARY_EVENTS={"memory.canonical"}
FKS={"messages":(("conversation_id","conversations"),("parent_id","messages")),"tool_calls":(("message_id","messages"),),"attachments":(("message_id","messages"),),"artifacts":(("conversation_id","conversations"),),"file_edits":(("message_id","messages"),)}

def _connect(path,journal="WAL"):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True); db=sqlite3.connect(path); os.chmod(path,0o600); db.row_factory=sqlite3.Row; db.executescript(f"PRAGMA journal_mode={journal};PRAGMA secure_delete=ON;"+STATE); return db
def _fsync(path):
    fd=os.open(path,os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)
def inspect_state(path,verify=False):
    path=Path(path); base={"path":str(path),"bytes":path.stat().st_size if path.exists() and path.is_file() else 0,"version":None}
    if not path.exists(): return base|{"status":"absent"}
    if path.is_symlink() or not path.is_file(): return base|{"status":"invalid","error":"state path is not a regular file"}
    db=None
    try:
        db=sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True); db.execute("PRAGMA query_only=ON"); integrity=db.execute("PRAGMA quick_check").fetchone()[0] if verify else "ok"; tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        try: version=(db.execute("SELECT value FROM meta WHERE key='state_schema'").fetchone() or [None])[0]
        except sqlite3.Error: version=None
        status="current" if version==STATE_VERSION and STATE_TABLES<=tables and not STATE_FORBIDDEN&tables and integrity=="ok" else "invalid" if version==STATE_VERSION or integrity!="ok" else "incompatible"
        return base|{"status":status,"version":version,"error":None if status!="invalid" else "schema or integrity check failed"}
    except sqlite3.Error as e: return base|{"status":"invalid","error":str(e)}
    finally:
        if db: db.close()
def read_state(path):
    info=inspect_state(path)
    if info["status"]!="current": raise ValueError(f"remote state is {info['status']}")
    db=sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro",uri=True); db.row_factory=sqlite3.Row; db.execute("PRAGMA query_only=ON"); return db
def cutover_state(path):
    path=Path(path); info=inspect_state(path,True)
    if info["status"] not in ("incompatible","invalid") or path.is_symlink() or not path.is_file(): raise ValueError(f"remote state cannot be rebuilt ({info['status']})")
    backups=path.parent/"backups"
    if backups.is_symlink(): raise ValueError("remote state backup directory must not be a symlink")
    backups.mkdir(parents=True,exist_ok=True); os.chmod(backups,0o700); name=f"state-{info['version'] or 'legacy'}-{time.time_ns()}"; stage=backups/f".{name}.{os.getpid()}"; target=backups/name; fresh=path.with_name(f".{path.name}.v{STATE_VERSION}.{os.getpid()}.{time.time_ns()}"); stage.mkdir(mode=0o700); files=[p for p in (path,Path(str(path)+"-wal"),Path(str(path)+"-shm")) if p.exists()]; saved={}
    try:
        for source in files:
            if source.is_symlink() or not source.is_file(): raise ValueError("remote state backup source must be a regular file")
            copy=stage/source.name; shutil.copyfile(source,copy); os.chmod(copy,0o600)
            saved[source.name]={"bytes":copy.stat().st_size,"sha256":file_hash(copy)}
            if file_hash(source)!=saved[source.name]["sha256"]: raise ValueError("remote state backup verification failed")
        report={"from":info["version"] or "legacy","to":int(STATE_VERSION),"backup":str(target),"files":saved}; manifest=stage/"manifest.json"; manifest.write_text(json.dumps(report,sort_keys=True,indent=2)); os.chmod(manifest,0o600); [_fsync(p) for p in [*(stage/p.name for p in files),manifest]]; _fsync(stage)
        new=_connect(fresh,"DELETE")
        try: new.execute("INSERT INTO meta VALUES ('state_schema',?),('state_cutover',?)",(STATE_VERSION,json.dumps(report,sort_keys=True))); new.commit(); valid=new.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
        finally: new.close()
        if not valid: raise ValueError("fresh remote state validation failed")
        os.replace(stage,target); _fsync(backups); [p.unlink(missing_ok=True) for p in (Path(str(path)+"-wal"),Path(str(path)+"-shm"))]; os.replace(fresh,path); os.chmod(path,0o600); _fsync(path); _fsync(path.parent); return report
    except BaseException:
        fresh.unlink(missing_ok=True); Path(str(fresh)+"-journal").unlink(missing_ok=True); stage.exists() and shutil.rmtree(stage); raise
def connect(path):
    path=Path(path); info=inspect_state(path)
    if info["status"]=="incompatible": raise ValueError(f"remote state rebuild required ({info['version'] or 'legacy'} -> {STATE_VERSION}); run `convos remote sync`")
    if info["status"]=="invalid": raise ValueError(f"invalid remote state: {info['error']}")
    db=_connect(path)
    if info["status"]=="absent": db.execute("INSERT INTO meta VALUES ('state_schema',?)",(STATE_VERSION,)); db.commit()
    return db
@lru_cache(maxsize=1)
def bridges():
    result=[entry.load()() for entry in entry_points(group="convos.remote")]
    if any(set(b)!={"v","events","records","project","purges"} or b["v"]!=4 or isinstance(b["v"],bool) or any(not callable(b[k]) for k in ("records","project","purges")) or any(not isinstance(e,tuple) or len(e)!=2 or not isinstance(e[0],str) or not isinstance(e[1],int) or isinstance(e[1],bool) or e[1]<1 for e in b["events"]) for b in result): raise ValueError("Unsupported remote bridge")
    return result
def event_support(value):
    kind,version=value["kind"],value["payload_v"]
    if not isinstance(kind,str) or not isinstance(version,int) or isinstance(version,bool) or version<1: raise ValueError("invalid event schema")
    installed={event for bridge in bridges() for event in bridge["events"]}
    return "supported" if (kind,version) in CORE_EVENTS or (kind,version) in installed else "optional" if kind in AUXILIARY_EVENTS and not any(event[0]==kind for event in installed) else "required"
def bridge_records(root,state,workspace,kind): return [record for bridge in bridges() for record in bridge["records"](root,state,workspace,kind)]
def bridge_purges(root,state,workspace,kind):
    values=[value for bridge in bridges() for value in bridge["purges"](root,state,workspace,kind)]
    if any(not isinstance(v,dict) or set(v)!={"event","superseded_by"} or any(not isinstance(v[k],str) or len(v[k])!=64 for k in v) or v["event"]==v["superseded_by"] for v in values): raise ValueError("invalid remote purge intent")
    return [dict(event=e,superseded_by=s) for e,s in sorted({(v["event"],v["superseded_by"]) for v in values})]
def clean(v):
    if isinstance(v,(datetime,date)): return v.isoformat()
    if isinstance(v,dict): return {k:clean(x) for k,x in v.items()}
    if isinstance(v,(list,tuple)): return [clean(x) for x in v]
    return v
def file_hash(path):
    with Path(path).open("rb") as source: return hashlib.file_digest(source,"sha256").hexdigest()
def relocate_attachments(db_path,remote_root):
    db_path,remote_root=Path(db_path),Path(remote_root)
    if not db_path.is_file() or not remote_root.exists(): return 0
    if remote_root.is_symlink() or not remote_root.is_dir(): raise ValueError("legacy attachment root must be a regular directory")
    db=duckdb.connect(str(db_path)); rows=db.execute("SELECT id,path,size FROM attachments WHERE path IS NOT NULL").fetchall(); moved=[]; target_root=db_path.parent/"attachments"; begun=False
    try:
        for row_id,value,size in rows:
            source=Path(value)
            if not source.is_absolute(): continue
            if not source.is_relative_to(remote_root.absolute()): continue
            if source.is_symlink() or not source.is_file() or source.resolve()!=source or size is not None and source.stat().st_size!=size: raise ValueError("legacy attachment body is unsafe or inconsistent")
            if target_root.is_symlink(): raise ValueError("archive attachment root must not be a symlink")
            target_root.mkdir(parents=True,exist_ok=True); os.chmod(target_root,0o700); blob=file_hash(source); target=target_root/blob
            if target.exists():
                if target.is_symlink() or not target.is_file() or target.stat().st_size!=source.stat().st_size or file_hash(target)!=blob: raise ValueError("archive attachment body conflicts")
            else:
                tmp=target.with_name(f".{target.name}.{os.getpid()}"); shutil.copyfile(source,tmp); os.chmod(tmp,0o600); _fsync(tmp); os.replace(tmp,target); _fsync(target_root)
            os.chmod(target,0o600); moved.append((row_id,source,target))
        db.execute("BEGIN"); begun=True; [set_attachment_path(db,row_id,target) for row_id,source,target in moved]; db.execute("COMMIT"); begun=False
    except BaseException:
        if begun: db.execute("ROLLBACK")
        raise
    finally: db.close()
    [source.unlink(missing_ok=True) for row_id,source,target in moved if source!=target]; return len(moved)
def _records(core,state,blobs=True):
    out=[]; imported=set(core.execute("SELECT table_name,physical_row_id FROM remote.row_origins").fetchall())
    for kind,table in TABLES.items():
        cur=core.execute(f"SELECT * EXCLUDE (embedding) FROM {table}" if table=="messages" else f"SELECT * FROM {table}"); cols=[d[0] for d in cur.description]
        for values in cur.fetchall():
            row=dict(zip(cols,map(clean,values))); attachment=Path(row["path"]).expanduser() if table=="attachments" and row.get("path") else None; row["embedding"]=None if "embedding" in row else row.get("embedding"); row["cwd"]=None if table=="conversations" else row.get("cwd"); row["path"]=None if table=="attachments" else row.get("path")
            if (table,row["id"]) not in imported: out.append(dict(kind=kind,entity=f"{table}:{row['id']}",payload=dict(table=table,columns=cols,row=[row[c] for c in cols])))
            if blobs and attachment and attachment.is_file() and (table,row["id"]) not in imported:
                data=attachment.read_bytes(); blob=hashlib.sha256(data).hexdigest(); chunks=[data[i:i+49152] for i in range(0,len(data),49152)] or [b""]
                out += [dict(kind="attachment.chunk",entity=f"attachment:{row['id']}:{blob}:{i}",payload={"attachment":row["id"],"blob":blob,"index":i,"total":len(chunks),"sha256":blob,"size":len(data),"data":base64.b64encode(chunk).decode()}) for i,chunk in enumerate(chunks)]
    return out
def _under(path,cwd,roots):
    p=Path(path); p=(Path(cwd)/p if not p.is_absolute() and cwd else p).expanduser().resolve(); return any(p.is_relative_to(root) for root in roots)
def _team_scope(core,provenance,repositories,roots):
    roots=[Path(p).expanduser().resolve() for p in roots]; checkouts=[Path(r[0]).expanduser().resolve() for r in core.execute("SELECT root FROM provenance.repository_checkouts WHERE repository IN (SELECT UNNEST(?))",[list(repositories)]).fetchall()] if repositories else []; allowed=roots+checkouts; edit_repos={r["payload"]["id"]:r["payload"]["repository"] for r in provenance if r["kind"]=="edit.observed"}; rows=core.execute("SELECT fe.id,fe.file_path,m.conversation_id,c.cwd FROM file_edits fe JOIN messages m ON m.id=fe.message_id JOIN conversations c ON c.id=m.conversation_id").fetchall(); cwd_rows=core.execute("SELECT id,cwd FROM conversations WHERE cwd IS NOT NULL").fetchall(); cwd_repos={cwd:repo["id"] if (repo:=resolve_repository(cwd)) else None for cwd in {cwd for cid,cwd in cwd_rows if not allowed or not _under(cwd,None,allowed)}}
    return {cid for eid,path,cid,cwd in rows if edit_repos.get(eid) in repositories or _under(path,cwd,roots)}|{cid for cid,cwd in cwd_rows if allowed and _under(cwd,None,allowed) or cwd_repos.get(cwd) in repositories}
def scan(core,graph,kind="personal",repositories=(),roots=()):
    provenance=observe_provenance(core); records=_records(core,graph,kind=="personal")
    edit_paths={r["payload"]["id"]:r["payload"]["file"] for r in provenance if r["kind"]=="edit.observed"}; file_paths={r["payload"]["id"]:r["payload"]["path"] for r in provenance if r["kind"]=="file.observed"}
    for r in records:
        if r["kind"]=="file_edit.record" and (fid:=edit_paths.get(r["payload"]["row"][0])): r["payload"]["row"][2]=file_paths[fid]
    if kind=="personal": return records+provenance
    convs=_team_scope(core,provenance,set(repositories),roots); keep=[]; msgs={r["payload"]["row"][0] for r in records if r["kind"]=="message.record" and r["payload"]["row"][1] in convs}; edits={r["payload"]["row"][0] for r in records if r["kind"]=="file_edit.record" and r["payload"]["row"][1] in msgs}
    for r in records:
        table,row=r["payload"]["table"],r["payload"]["row"]
        if table=="conversations" and row[0] in convs or table=="messages" and row[0] in msgs or table in ("tool_calls","attachments","file_edits") and row[1] in msgs or table=="artifacts" and row[1] in convs: keep.append(r)
    allowed_files={r["payload"]["file"] for r in provenance if r["kind"]=="edit.observed" and r["payload"]["id"] in edits}; allowed_repos={r["payload"]["repository"] for r in provenance if r["kind"]=="edit.observed" and r["payload"]["id"] in edits}
    for r in provenance:
        p,k=r["payload"],r["kind"]
        if k=="edit.observed" and p["id"] in edits or k=="file.observed" and p["id"] in allowed_files or k=="file.version" and p["file"] in allowed_files or k in ("repository.observed","git.checkpoint") and p.get("repository",p.get("id")) in allowed_repos or k=="checkpoint.link" and p["edit"] in edits: keep.append(r)
    return keep
def attest_rows(db_path,cfg,workspace,records):
    controls=next(w["controls"] for w in cfg["server_state"]["workspaces"] if w["id"]==workspace); device=cfg["device"]; signer=cfg["controls"][workspace]["devices"][device["id"]]; db=duckdb.connect(str(db_path)); init_schema(db); made=0; db.execute("BEGIN")
    try:
        project_workspace_controls(db,controls)
        for record in (r for r in records if r["kind"] in TABLES):
            p=record["payload"]; row=logical_row(p["table"],p["columns"],p["row"]); heads=db.execute("SELECT DISTINCT p.revision,p.content_hash FROM remote.row_proofs p WHERE p.workspace_id=? AND p.row_kind=? AND p.source_row_id=? AND p.author_user_id=? AND NOT EXISTS (SELECT 1 FROM remote.row_proofs c WHERE c.workspace_id=p.workspace_id AND c.row_kind=p.row_kind AND c.source_row_id=p.source_row_id AND c.author_user_id=p.author_user_id AND c.previous_revision=p.revision)",(workspace,row["kind"],row["id"],cfg["user"])).fetchall(); current=digest(row)
            if any(h[1]==current for h in heads): continue
            if len(heads)>1: raise ValueError(f"row revision conflict: {row['kind']}:{row['id']}")
            proof=row_proof(device,cfg["user"],workspace,cfg["workspaces"][workspace]["epoch"],row,heads[0][0] if heads else None); project_row_proof(db,proof,signer["root_public"],signer["certificate"]); made+=1
        db.execute("COMMIT"); return made
    except BaseException: db.execute("ROLLBACK"); raise
    finally: db.close()
def author_user(value,authors): return (authors or {}).get(value["author"]) or (_ for _ in ()).throw(ValueError("verified author user required"))
def foreign_id(workspace,author_user,table,old): return digest(f"{workspace}:{author_user}:{table}:{old}")[:16] if old else old
def attachment_path(db_path,db,target,path):
    target_db=db or (duckdb.connect(str(db_path)) if Path(db_path).exists() else None); own=target_db is not None and db is None
    try: own and target_db.execute("BEGIN"); target_db and set_attachment_path(target_db,target,path); own and target_db.execute("COMMIT")
    except BaseException:
        if own: target_db.execute("ROLLBACK")
        raise
    finally:
        if own: target_db.close()
def sequence(state,workspace,value):
    old=state.execute("SELECT event FROM event_sequences WHERE workspace=? AND author=? AND seq=?",(workspace,value["author"],value["seq"])).fetchone()
    if old and old[0]!=value["id"]: raise ValueError("device sequence replay")
    if old: return True
    before=state.execute("SELECT event FROM event_sequences WHERE workspace=? AND author=? AND seq=?",(workspace,value["author"],value["seq"]-1)).fetchone(); after=state.execute("SELECT event FROM event_sequences WHERE workspace=? AND author=? AND seq=?",(workspace,value["author"],value["seq"]+1)).fetchone(); gap=after and state.execute("SELECT parents FROM sequence_gaps WHERE workspace=? AND author=? AND seq=?",(workspace,value["author"],value["seq"]+1)).fetchone()
    if value["seq"]==1 and value["parents"] or before and before[0] not in value["parents"] or after and (not gap or value["id"] not in json.loads(gap[0])): raise ValueError("device event chain mismatch")
    state.execute("INSERT INTO event_sequences VALUES (?,?,?,?)",(workspace,value["author"],value["seq"],value["id"]))
    if value["seq"]>1 and not before: state.execute("INSERT INTO sequence_gaps VALUES (?,?,?,?)",(workspace,value["author"],value["seq"],json.dumps(value["parents"])))
    if after: state.execute("DELETE FROM sequence_gaps WHERE workspace=? AND author=? AND seq=?",(workspace,value["author"],value["seq"]+1))
    return True
def reset_history(state,workspace,boundary):
    [state.execute(f"DELETE FROM {table} WHERE workspace=?",(workspace,)) for table in ("receipts","history_sources","cursors","heads","lazy_events","deferred_events","event_sequences","sequence_gaps")]; [state.execute("INSERT INTO event_sequences VALUES (?,?,?,?)",(workspace,author,head["seq"],head["event"])) for author,head in boundary["heads"].items()]
def verify_history(state,workspace,controls,start):
    gaps=state.execute("SELECT author,seq FROM sequence_gaps WHERE workspace=? ORDER BY author,seq LIMIT 1",(workspace,)).fetchone()
    if gaps: raise ValueError(f"required event sequence is incomplete at {gaps[0]}:{gaps[1]}")
    if deferred:=state.execute("SELECT kind,payload_v FROM deferred_events WHERE workspace=? AND required=1 ORDER BY cursor LIMIT 1",(workspace,)).fetchone(): raise ValueError(f"required event is unsupported: {deferred[0]} payload_v={deferred[1]}")
    for control in controls:
        if control["boundary"]["epoch"]>=start:
            for author,head in control["boundary"]["heads"].items():
                if (state.execute("SELECT event FROM event_sequences WHERE workspace=? AND author=? AND seq=?",(workspace,author,head["seq"])).fetchone() or [None])[0]!=head["event"]: raise ValueError("signed history checkpoint is incomplete")
def apply_record(db_path,state,value,workspace,local_device=None,db=None,authors=None,recover=None,local_user=None):
    table=TABLES[value["kind"]]; p=value["payload"]
    if value["entity"] != f"{table}:{p['row'][0]}" or p["table"] != table or p["columns"]!=COLUMNS[table] or len(p["row"])!=len(p["columns"]): raise ValueError("record schema/entity mismatch")
    user=author_user(value,authors); sort=f"{value['observed_at']}:{value['id']}"; old=state.execute("SELECT sort_key FROM heads WHERE workspace=? AND author_user=? AND entity=?",(workspace,user,value["entity"])).fetchone()
    if old and old[0]>=sort: return False
    owned=bool(recover and user==local_user); native=owned and recover=="native"
    if owned and recover=="adopt": return False
    if value["author"]==local_device and not owned: return False
    mapped=lambda table,old:old if native else foreign_id(workspace,user,table,old); values=list(p["row"]); values[0]=mapped(table,values[0])
    for column,parent in FKS.get(table,()): idx=p["columns"].index(column); values[idx]=mapped(parent,values[idx])
    own=db is None
    if own: Path(db_path).parent.mkdir(parents=True,exist_ok=True); db=duckdb.connect(str(db_path)); init_schema(db); db.execute("BEGIN")
    try:
        project_archive_row(db,table,p["columns"],values,None if native else {"workspace_id":workspace,"author_user_id":user,"author_device_id":value["author"],"source_row_id":p["row"][0],"source_event_id":value["id"],"content_key":value["entity"],"observed_at":value["observed_at"]})
        if table=="attachments" and (blob:=state.execute("SELECT path FROM attachment_blobs WHERE workspace=? AND author=? AND attachment=?",(workspace,value["author"],p["row"][0])).fetchone()): set_attachment_path(db,values[0],blob[0])
        if own: db.execute("COMMIT")
    except BaseException:
        if own: db.execute("ROLLBACK")
        raise
    finally:
        if own: db.close()
    state.execute("INSERT OR REPLACE INTO heads VALUES (?,?,?,?,?)",(workspace,user,value["entity"],sort,value["id"]));
    if own: state.commit()
    return True
def project(db_path,state,value,workspace,local_device=None,db=None,root=None,batch=False,authors=None,recover=None,local_user=None):
    if event_support(value)!="supported": return False
    if value["kind"] in TABLES: return apply_record(db_path,state,value,workspace,local_device,db,authors,recover,local_user)
    if value["kind"]=="workspace.policy":
        p=value["payload"]; state.execute("INSERT OR REPLACE INTO policies VALUES (?,?,?)",(workspace,p["kind"],p["value"])); batch or state.commit(); return True
    if value["kind"]=="attachment.chunk":
        p=value["payload"]; user=author_user(value,authors); owned=bool(recover and user==local_user); native=owned and recover=="native"
        if owned and recover=="adopt": return False
        if value["author"]==local_device and not owned: return False
        data=base64.b64decode(p["data"],validate=True); expected=f"attachment:{p['attachment']}:{p['blob']}:{p['index']}"; base=Path(db_path).parent/"attachments"; parts=Path(db_path).parent.parent/"remote/attachments/.parts"/digest(workspace)[:32]/value["author"]/p["blob"]; path=base/p["blob"]
        if value["entity"]!=expected or p["blob"]!=p["sha256"] or len(p["blob"])!=64 or any(c not in "0123456789abcdef" for c in p["blob"]) or not 0<=p["index"]<p["total"] or p["size"]<0 or len(data)>49152: raise ValueError("attachment chunk schema mismatch")
        if base.is_symlink() or (working:=Path(db_path).parent.parent/"remote/attachments").is_symlink() or any(x.is_symlink() for x in (parts,*parts.parents) if x==working or x.is_relative_to(working)): raise ValueError("attachment directory must not be a symlink")
        base.mkdir(parents=True,exist_ok=True); os.chmod(base,0o700); working.mkdir(parents=True,exist_ok=True); os.chmod(working,0o700)
        if path.exists():
            if path.is_symlink() or path.stat().st_size!=p["size"] or file_hash(path)!=p["sha256"]: raise ValueError("attachment file conflict")
            stale=state.execute("SELECT path FROM attachment_parts WHERE workspace=? AND author=? AND blob=?",(workspace,value["author"],p["blob"])).fetchall(); state.execute("INSERT OR REPLACE INTO attachment_blobs VALUES (?,?,?,?)",(workspace,value["author"],p["attachment"],str(path))); state.execute("DELETE FROM attachment_parts WHERE workspace=? AND author=? AND blob=?",(workspace,value["author"],p["blob"])); [Path(r[0]).unlink(missing_ok=True) for r in stale if Path(r[0]).parent==parts]; target=p["attachment"] if native else foreign_id(workspace,user,"attachments",p["attachment"]); attachment_path(db_path,db,target,path)
            batch or state.commit(); return True
        chunk_hash=hashlib.sha256(data).hexdigest(); part=parts/str(p["index"]); meta=state.execute("SELECT total,attachment,sha256,size FROM attachment_parts WHERE workspace=? AND author=? AND blob=? LIMIT 1",(workspace,value["author"],p["blob"])).fetchone(); old=state.execute("SELECT total,attachment,sha256,size,chunk_hash,path FROM attachment_parts WHERE workspace=? AND author=? AND blob=? AND idx=?",(workspace,value["author"],p["blob"],p["index"])).fetchone()
        if meta and tuple(meta)!=(p["total"],p["attachment"],p["sha256"],p["size"]) or old and tuple(old)!=(p["total"],p["attachment"],p["sha256"],p["size"],chunk_hash,str(part)): raise ValueError("attachment chunk conflict")
        if part.exists():
            if part.is_symlink() or part.stat().st_size!=len(data) or file_hash(part)!=chunk_hash: raise ValueError("attachment chunk conflict")
        else:
            part.parent.mkdir(parents=True,exist_ok=True); tmp=part.with_name(f".{part.name}.{os.getpid()}"); out=tmp.open("xb"); os.chmod(tmp,0o600); out.write(data); out.close(); os.replace(tmp,part)
        state.execute("INSERT OR IGNORE INTO attachment_parts VALUES (?,?,?,?,?,?,?,?,?,?)",(workspace,value["author"],p["blob"],p["index"],p["total"],p["attachment"],p["sha256"],p["size"],chunk_hash,str(part))); rows=state.execute("SELECT idx,path FROM attachment_parts WHERE workspace=? AND author=? AND blob=? ORDER BY idx",(workspace,value["author"],p["blob"])).fetchall()
        if len(rows)==p["total"]:
            if [r[0] for r in rows]!=list(range(p["total"])): raise ValueError("attachment chunks incomplete")
            path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(f".{path.name}.{os.getpid()}"); out=tmp.open("xb"); os.chmod(tmp,0o600)
            try:
                for row in rows:
                    with Path(row[1]).open("rb") as source: shutil.copyfileobj(source,out,65536)
                out.flush(); os.fsync(out.fileno()); out.close()
                if tmp.stat().st_size!=p["size"] or file_hash(tmp)!=p["sha256"]: raise ValueError("attachment hash mismatch")
                os.replace(tmp,path); _fsync(base)
            except BaseException: out.close(); tmp.unlink(missing_ok=True); raise
            state.execute("INSERT OR REPLACE INTO attachment_blobs VALUES (?,?,?,?)",(workspace,value["author"],p["attachment"],str(path))); state.execute("DELETE FROM attachment_parts WHERE workspace=? AND author=? AND blob=?",(workspace,value["author"],p["blob"])); [Path(r[1]).unlink(missing_ok=True) for r in rows]; target=p["attachment"] if native else foreign_id(workspace,user,"attachments",p["attachment"]); attachment_path(db_path,db,target,path)
        batch or state.commit(); return True
    if root is not None:
        for bridge in bridges():
            if (result:=bridge["project"](root,state,value,workspace,local_device)) is not None: return result
    if value["kind"] not in PROVENANCE: return False
    user=author_user(value,authors); owned=bool(recover and user==local_user); native=owned and recover=="native"
    if owned and recover=="adopt": return True
    if value["author"]==local_device and not owned: return True
    own=db is None
    if own: Path(db_path).parent.mkdir(parents=True,exist_ok=True); db=duckdb.connect(str(db_path)); init_schema(db); db.execute("BEGIN")
    try: result=project_provenance(db,value,lambda table,old:old if native else foreign_id(workspace,user,table,old)); own and db.execute("COMMIT"); return result
    except BaseException:
        if own: db.execute("ROLLBACK")
        raise
    finally:
        if own: db.close()
def project_many(db_path,state,items,local_device=None,root=None,commit=True,authors=None,recover=None,local_user=None):
    records=any(v["kind"] in set(TABLES)|PROVENANCE and (v["author"]!=local_device or recover and author_user(v,authors)==local_user) for _,v in items); db=None
    if records: Path(db_path).parent.mkdir(parents=True,exist_ok=True); db=duckdb.connect(str(db_path)); init_schema(db)
    committed=False
    try:
        if db: db.execute("BEGIN")
        [project(db_path,state,v,ws,local_device,db,root,True,authors,recover,local_user) for ws,v in items]
        if db: db.execute("COMMIT"); committed=True
        commit and state.commit()
    except BaseException:
        if db and not committed: db.execute("ROLLBACK")
        state.rollback(); raise
    finally:
        if db: db.close()
    return len(items)
def query(db_path,name,arg=None):
    db=duckdb.connect(str(db_path),read_only=True)
    try: return graph_query(db,name,arg)
    finally: db.close()
