"""Portable record/event projection. The immutable event ledger can rebuild every local view."""
import base64, hashlib, json, os, sqlite3
from datetime import date, datetime
from pathlib import Path

import duckdb
from ai_convos.cli import init_schema
from .provenance import capture as capture_graph, connect as graph_connect, project as project_graph, query as graph_query
from .protocol import digest, material_event

STATE = """
CREATE TABLE IF NOT EXISTS event_log(workspace TEXT,event TEXT PRIMARY KEY,cursor INT,direction TEXT,event_json TEXT,envelope TEXT);
CREATE TABLE IF NOT EXISTS history_material(workspace TEXT,event TEXT,event_json TEXT,PRIMARY KEY(workspace,event));
CREATE TABLE IF NOT EXISTS history_outbox(workspace TEXT,target TEXT,event TEXT,event_json TEXT,PRIMARY KEY(workspace,target,event));
CREATE TABLE IF NOT EXISTS published(workspace TEXT,entity TEXT,revision TEXT,event TEXT,PRIMARY KEY(workspace,entity,revision));
CREATE TABLE IF NOT EXISTS cursors(workspace TEXT PRIMARY KEY,cursor INT);
CREATE TABLE IF NOT EXISTS heads(workspace TEXT,entity TEXT,sort_key TEXT,event TEXT,PRIMARY KEY(workspace,entity));
CREATE TABLE IF NOT EXISTS imported_rows(table_name TEXT,row_id TEXT PRIMARY KEY,event TEXT);
CREATE TABLE IF NOT EXISTS lazy_events(workspace TEXT,event TEXT PRIMARY KEY,cursor INT,size INT);
CREATE TABLE IF NOT EXISTS event_sequences(workspace TEXT,author TEXT,seq INT,event TEXT,parents TEXT,PRIMARY KEY(workspace,author,seq));
CREATE TABLE IF NOT EXISTS attachment_chunks(workspace TEXT,author TEXT,blob TEXT,idx INT,total INT,attachment TEXT,sha256 TEXT,size INT,data TEXT,PRIMARY KEY(workspace,author,blob,idx));
CREATE TABLE IF NOT EXISTS attachment_blobs(workspace TEXT,author TEXT,attachment TEXT PRIMARY KEY,path TEXT);
CREATE TABLE IF NOT EXISTS policies(workspace TEXT,kind TEXT,value TEXT,local_root TEXT,PRIMARY KEY(workspace,kind,value));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
"""
TABLES={"conversation.record":"conversations","message.record":"messages","tool.record":"tool_calls","attachment.record":"attachments","artifact.record":"artifacts","file_edit.record":"file_edits"}
COLUMNS={"conversations":["id","source","title","created_at","updated_at","model","cwd","git_branch","project_id","metadata"],"messages":["id","conversation_id","role","content","thinking","created_at","model","metadata","parent_id"],"tool_calls":["id","message_id","tool_name","input","output","status","duration_ms","created_at"],"attachments":["id","message_id","filename","mime_type","size","path","url","created_at"],"artifacts":["id","conversation_id","artifact_type","title","content","language","created_at","version"],"file_edits":["id","message_id","file_path","edit_type","content","created_at","old_content"]}
FKS={"messages":(("conversation_id","conversations"),("parent_id","messages")),"tool_calls":(("message_id","messages"),),"attachments":(("message_id","messages"),),"artifacts":(("conversation_id","conversations"),),"file_edits":(("message_id","messages"),)}

def connect(path): db=graph_connect(path); db.executescript(STATE); return db
def clean(v):
    if isinstance(v,(datetime,date)): return v.isoformat()
    if isinstance(v,dict): return {k:clean(x) for k,x in v.items()}
    if isinstance(v,(list,tuple)): return [clean(x) for x in v]
    return v
def _records(core,state):
    out=[]; imported={(r[0],r[1]) for r in state.execute("SELECT table_name,row_id FROM imported_rows").fetchall()}
    for kind,table in TABLES.items():
        cur=core.execute(f"SELECT * EXCLUDE (embedding) FROM {table}" if table=="messages" else f"SELECT * FROM {table}"); cols=[d[0] for d in cur.description]
        for values in cur.fetchall():
            row=dict(zip(cols,map(clean,values))); attachment=Path(row["path"]).expanduser() if table=="attachments" and row.get("path") else None; row["embedding"]=None if "embedding" in row else row.get("embedding"); row["cwd"]=None if table=="conversations" else row.get("cwd"); row["path"]=None if table=="attachments" else row.get("path")
            if (table,row["id"]) not in imported: out.append(dict(kind=kind,entity=f"{table}:{row['id']}",payload=dict(table=table,columns=cols,row=[row[c] for c in cols])))
            if attachment and attachment.is_file() and (table,row["id"]) not in imported:
                data=attachment.read_bytes(); blob=hashlib.sha256(data).hexdigest(); chunks=[data[i:i+49152] for i in range(0,len(data),49152)] or [b""]
                out += [dict(kind="attachment.chunk",entity=f"attachment:{row['id']}:{blob}:{i}",payload={"attachment":row["id"],"blob":blob,"index":i,"total":len(chunks),"sha256":blob,"size":len(data),"data":base64.b64encode(chunk).decode()}) for i,chunk in enumerate(chunks)]
    return out
def _team_scope(core,provenance,repositories,roots):
    origins={r["payload"]["origin"] for r in provenance if r["kind"]=="edit.observed" and r["payload"]["repository"] in repositories}
    for eid,path,mid,cid,cwd in core.execute("SELECT fe.id,fe.file_path,m.id,m.conversation_id,c.cwd FROM file_edits fe JOIN messages m ON m.id=fe.message_id JOIN conversations c ON c.id=m.conversation_id").fetchall():
        p=Path(path); p=(Path(cwd)/p if not p.is_absolute() and cwd else p).expanduser().resolve()
        if any(p.is_relative_to(Path(root).expanduser().resolve()) for root in roots): origins.add(eid)
    cs={r["payload"]["changeset"] for r in provenance if r["kind"]=="edit.observed" and r["payload"]["origin"] in origins}; turns={r["payload"]["turn"] for r in provenance if r["kind"]=="changeset.observed" and r["payload"]["id"] in cs}; convs={r["payload"]["conversation"] for r in provenance if r["kind"]=="changeset.observed" and r["payload"]["id"] in cs}
    users=set()
    for turn in turns:
        if row:=core.execute("SELECT conversation_id FROM messages WHERE id=?",(turn,)).fetchone():
            if prompt:=core.execute("SELECT id FROM messages WHERE conversation_id=? AND role='user' AND created_at<=(SELECT created_at FROM messages WHERE id=?) ORDER BY created_at DESC LIMIT 1",(row[0],turn)).fetchone(): users.add(prompt[0])
    return convs,turns|users,cs,origins
def scan(core,graph,device,kind="personal",repositories=(),roots=()):
    provenance=capture_graph(core,graph,device); records=_records(core,graph); imported={r[0] for r in graph.execute("SELECT row_id FROM imported_rows WHERE table_name='file_edits'").fetchall()}; blocked={r["payload"]["changeset"] for r in provenance if r["kind"]=="edit.observed" and r["payload"]["origin"] in imported}; provenance=[r for r in provenance if not (r["kind"]=="edit.observed" and r["payload"]["origin"] in imported or r["kind"] in ("changeset.observed","checkpoint.link") and r["payload"].get("id",r["payload"].get("changeset")) in blocked)]
    edit_paths={r["payload"]["origin"]:r["payload"]["file"] for r in provenance if r["kind"]=="edit.observed"}; file_paths={r["payload"]["id"]:r["payload"]["path"] for r in provenance if r["kind"]=="file.observed"}
    for r in records:
        if r["kind"]=="file_edit.record" and (fid:=edit_paths.get(r["payload"]["row"][0])): r["payload"]["row"][2]=file_paths[fid]
    if kind=="personal": return records+provenance
    convs,msgs,changesets,origins=_team_scope(core,provenance,set(repositories),roots); keep=[]
    allowed_attachments=set()
    for r in records:
        if r["kind"]=="attachment.chunk":
            if r["payload"]["attachment"] in allowed_attachments: keep.append(r)
            continue
        table,row=r["payload"]["table"],r["payload"]["row"]
        if table=="conversations" and row[0] in convs or table=="messages" and row[0] in msgs or table in ("tool_calls","attachments") and row[1] in msgs or table=="file_edits" and row[0] in origins or table=="artifacts" and row[1] in convs: keep.append(r); table=="attachments" and allowed_attachments.add(row[0])
    allowed_files={r["payload"]["file"] for r in provenance if r["kind"]=="edit.observed" and r["payload"]["origin"] in origins}; allowed_repos=set(repositories)
    for r in provenance:
        p,k=r["payload"],r["kind"]
        if k=="changeset.observed" and p["id"] in changesets or k=="edit.observed" and p["origin"] in origins or k=="file.observed" and p["id"] in allowed_files or k=="file.version" and p["file"] in allowed_files or k in ("repository.observed","git.checkpoint","capture.gap") and p.get("repository",p.get("id")) in allowed_repos or k=="checkpoint.link" and p["changeset"] in changesets: keep.append(r)
    for cs in changesets:
        total=sum(r["kind"]=="edit.observed" and r["payload"]["changeset"]==cs for r in provenance); visible=sum(r["kind"]=="edit.observed" and r["payload"]["changeset"]==cs and r["payload"]["origin"] in origins for r in provenance)
        if total>visible: keep.append(dict(kind="changeset.boundary",entity=digest({"changeset":cs,"visible":sorted(origins)}),payload={"changeset":cs,"hidden_count":total-visible}))
    return keep
def foreign_id(workspace,author,table,old): return digest(f"{workspace}:{author}:{table}:{old}")[:16] if old else old
def sequence(state,workspace,value):
    old=state.execute("SELECT event FROM event_sequences WHERE workspace=? AND author=? AND seq=?",(workspace,value["author"],value["seq"])).fetchone()
    if old and old[0]!=value["id"]: raise ValueError("device sequence replay")
    before=state.execute("SELECT event FROM event_sequences WHERE workspace=? AND author=? AND seq=?",(workspace,value["author"],value["seq"]-1)).fetchone(); after=state.execute("SELECT event,parents FROM event_sequences WHERE workspace=? AND author=? AND seq=?",(workspace,value["author"],value["seq"]+1)).fetchone()
    if before and before[0] not in value["parents"] or after and value["id"] not in json.loads(after[1]): raise ValueError("device event chain mismatch")
    state.execute("INSERT OR IGNORE INTO event_sequences VALUES (?,?,?,?,?)",(workspace,value["author"],value["seq"],value["id"],json.dumps(value["parents"])))
    return True
def apply_record(db_path,state,value,workspace,local_device=None,db=None):
    table=TABLES[value["kind"]]; p=value["payload"]; head=f"{value['author']}:{value['entity']}"; sort=f"{value['observed_at']}:{value['id']}"; old=state.execute("SELECT sort_key FROM heads WHERE workspace=? AND entity=?",(workspace,head)).fetchone()
    if value["entity"] != f"{table}:{p['row'][0]}" or p["table"] != table or p["columns"]!=COLUMNS[table] or len(p["row"])!=len(p["columns"]): raise ValueError("record schema/entity mismatch")
    if old and old[0]>=sort: return False
    if value["author"]==local_device: return False
    values=list(p["row"]); values[0]=foreign_id(workspace,value["author"],table,values[0])
    for column,parent in FKS.get(table,()): idx=p["columns"].index(column); values[idx]=foreign_id(workspace,value["author"],parent,values[idx])
    own=db is None
    if own: Path(db_path).parent.mkdir(parents=True,exist_ok=True); db=duckdb.connect(str(db_path)); init_schema(db)
    cols=p["columns"]; db.execute(f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})",values)
    if table=="attachments" and (blob:=state.execute("SELECT path FROM attachment_blobs WHERE workspace=? AND author=? AND attachment=?",(workspace,value["author"],p["row"][0])).fetchone()): db.execute("UPDATE attachments SET path=? WHERE id=?",(blob[0],values[0]))
    if own: db.close()
    state.execute("INSERT OR REPLACE INTO heads VALUES (?,?,?,?)",(workspace,head,sort,value["id"])); state.execute("INSERT OR REPLACE INTO imported_rows VALUES (?,?,?)",(table,values[0],value["id"]));
    if own: state.commit()
    return True
def project(db_path,state,value,workspace,local_device=None,db=None):
    if value["kind"] in TABLES: return apply_record(db_path,state,value,workspace,local_device,db)
    if value["kind"]=="workspace.policy":
        p=value["payload"]; state.execute("INSERT OR REPLACE INTO policies VALUES (?,?,?,COALESCE((SELECT local_root FROM policies WHERE workspace=? AND kind=? AND value=?),NULL))",(workspace,p["kind"],p["value"],workspace,p["kind"],p["value"])); state.commit(); return True
    if value["kind"]=="attachment.chunk":
        if value["author"]==local_device: return False
        p=value["payload"]; data=base64.b64decode(p["data"],validate=True); encoded=base64.b64encode(data).decode(); expected=f"attachment:{p['attachment']}:{p['blob']}:{p['index']}"
        if value["entity"]!=expected or p["blob"]!=p["sha256"] or not 0<=p["index"]<p["total"] or p["size"]<0 or len(data)>49152: raise ValueError("attachment chunk schema mismatch")
        meta=state.execute("SELECT total,attachment,sha256,size FROM attachment_chunks WHERE workspace=? AND author=? AND blob=? LIMIT 1",(workspace,value["author"],p["blob"])).fetchone(); old=state.execute("SELECT total,attachment,sha256,size,data FROM attachment_chunks WHERE workspace=? AND author=? AND blob=? AND idx=?",(workspace,value["author"],p["blob"],p["index"])).fetchone()
        if meta and tuple(meta)!=(p["total"],p["attachment"],p["sha256"],p["size"]) or old and tuple(old)!=(p["total"],p["attachment"],p["sha256"],p["size"],encoded): raise ValueError("attachment chunk conflict")
        state.execute("INSERT OR IGNORE INTO attachment_chunks VALUES (?,?,?,?,?,?,?,?,?)",(workspace,value["author"],p["blob"],p["index"],p["total"],p["attachment"],p["sha256"],p["size"],encoded)); rows=state.execute("SELECT idx,data FROM attachment_chunks WHERE workspace=? AND author=? AND blob=? ORDER BY idx",(workspace,value["author"],p["blob"])).fetchall()
        if len(rows)==p["total"]:
            if [r[0] for r in rows]!=list(range(p["total"])): raise ValueError("attachment chunks incomplete")
            content=b"".join(base64.b64decode(r[1]) for r in rows)
            if len(content)!=p["size"] or hashlib.sha256(content).hexdigest()!=p["sha256"]: raise ValueError("attachment hash mismatch")
            path=Path(db_path).parent.parent/"remote/attachments"/workspace/p["blob"]; path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(f".{path.name}.{os.getpid()}"); tmp.write_bytes(content); os.chmod(tmp,0o600); os.replace(tmp,path); state.execute("INSERT OR REPLACE INTO attachment_blobs VALUES (?,?,?,?)",(workspace,value["author"],p["attachment"],str(path))); state.execute("DELETE FROM attachment_chunks WHERE workspace=? AND author=? AND blob=?",(workspace,value["author"],p["blob"])); target=foreign_id(workspace,value["author"],"attachments",p["attachment"]); target_db=db or (duckdb.connect(str(db_path)) if Path(db_path).exists() else None)
            if target_db: target_db.execute("UPDATE attachments SET path=? WHERE id=?",(str(path),target)); db or target_db.close()
        state.commit(); return True
    return project_graph(state,value,workspace)
def project_many(db_path,state,items,local_device=None):
    records=any(v["kind"] in TABLES and v["author"]!=local_device for _,v in items); db=None
    if records: Path(db_path).parent.mkdir(parents=True,exist_ok=True); db=duckdb.connect(str(db_path)); init_schema(db)
    try: [project(db_path,state,v,ws,local_device,db) for ws,v in items]
    finally:
        if db: db.close()
    state.commit(); return len(items)
def rebuild(db_path,state,local_device=None,device=None):
    path=Path(db_path); path.unlink(missing_ok=True); [state.execute(f"DELETE FROM {table}") for table in ("raw_events","repositories","files","file_versions","changesets","edits","changeset_repositories","checkpoints","checkpoint_changesets","assertions","gaps","boundaries","heads","imported_rows","attachment_chunks","attachment_blobs")]; rows=state.execute("SELECT workspace,event_json FROM event_log ORDER BY json_extract(event_json,'$.observed_at'),event").fetchall()
    project_many(path,state,[(workspace,value) for workspace,raw in rows if (value:=material_event(json.loads(raw),device=device))]); return len(rows)
def query(state,name,arg=None): return graph_query(state,name,arg)
