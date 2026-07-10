"""Git-anchored, path-independent local provenance projection. Evidence stays revisable."""
import json, os, sqlite3, subprocess
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from .protocol import canon, digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_events(id TEXT PRIMARY KEY,workspace TEXT,kind TEXT,entity TEXT,author TEXT,seq INT,observed_at TEXT,payload TEXT);
CREATE TABLE IF NOT EXISTS repositories(id TEXT PRIMARY KEY,lineage TEXT,roots TEXT,remotes TEXT,last_head TEXT,observed_at TEXT);
CREATE TABLE IF NOT EXISTS checkouts(id TEXT PRIMARY KEY,repository TEXT,root TEXT UNIQUE,branch TEXT,head TEXT);
CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY,repository TEXT,path TEXT,kind TEXT);
CREATE TABLE IF NOT EXISTS file_versions(id TEXT PRIMARY KEY,file_id TEXT,content_hash TEXT,observed_at TEXT);
CREATE TABLE IF NOT EXISTS changesets(id TEXT PRIMARY KEY,conversation TEXT,turn TEXT,prompt TEXT,observed_at TEXT);
CREATE TABLE IF NOT EXISTS edits(id TEXT PRIMARY KEY,changeset_id TEXT,file_id TEXT,edit_type TEXT,before_hash TEXT,after_hash TEXT,evidence TEXT,observed_at TEXT,origin TEXT);
CREATE TABLE IF NOT EXISTS changeset_repositories(changeset_id TEXT,repository TEXT,PRIMARY KEY(changeset_id,repository));
CREATE TABLE IF NOT EXISTS checkpoints(id TEXT PRIMARY KEY,repository TEXT,head TEXT,state_hash TEXT,paths TEXT,observed_at TEXT);
CREATE TABLE IF NOT EXISTS checkpoint_changesets(checkpoint_id TEXT,changeset_id TEXT,evidence TEXT,PRIMARY KEY(checkpoint_id,changeset_id));
CREATE TABLE IF NOT EXISTS assertions(id TEXT PRIMARY KEY,left_id TEXT,relation TEXT,right_id TEXT,evidence TEXT,status TEXT,observed_at TEXT);
CREATE TABLE IF NOT EXISTS gaps(id TEXT PRIMARY KEY,repository TEXT,checkpoint_id TEXT,path TEXT,relation TEXT,observed_at TEXT);
CREATE TABLE IF NOT EXISTS boundaries(id TEXT PRIMARY KEY,workspace TEXT,changeset_id TEXT,hidden_count INT);
CREATE VIEW IF NOT EXISTS file_history AS SELECT e.observed_at,f.repository,f.path,e.edit_type,e.evidence,e.changeset_id,c.conversation,c.turn,c.prompt,e.before_hash,e.after_hash FROM edits e JOIN files f ON f.id=e.file_id JOIN changesets c ON c.id=e.changeset_id;
CREATE VIEW IF NOT EXISTS changeset_files AS SELECT c.id,c.conversation,c.turn,c.prompt,f.repository,f.path,e.edit_type,e.evidence FROM changesets c JOIN edits e ON e.changeset_id=c.id JOIN files f ON f.id=e.file_id;
CREATE VIEW IF NOT EXISTS conversation_changes AS SELECT c.conversation,c.id changeset_id,c.prompt,COUNT(DISTINCT cr.repository) repositories,COUNT(DISTINCT e.file_id) files,MIN(e.observed_at) first_edit,MAX(e.observed_at) last_edit FROM changesets c JOIN edits e ON e.changeset_id=c.id LEFT JOIN changeset_repositories cr ON cr.changeset_id=c.id GROUP BY c.conversation,c.id,c.prompt;
CREATE VIEW IF NOT EXISTS commit_conversations AS SELECT p.head,c.conversation,c.id changeset_id,p.repository,cc.evidence FROM checkpoint_changesets cc JOIN checkpoints p ON p.id=cc.checkpoint_id JOIN changesets c ON c.id=cc.changeset_id;
CREATE VIEW IF NOT EXISTS repository_activity AS SELECT cr.repository,c.conversation,c.id changeset_id,COUNT(e.id) edits,MAX(e.observed_at) last_edit FROM changeset_repositories cr JOIN changesets c ON c.id=cr.changeset_id LEFT JOIN edits e ON e.changeset_id=c.id GROUP BY cr.repository,c.conversation,c.id;
CREATE VIEW IF NOT EXISTS workspace_repository_activity AS SELECT r.workspace,cr.repository,c.conversation,c.id changeset_id,COUNT(DISTINCT e.id) edits,MAX(e.observed_at) last_edit FROM raw_events r JOIN edits e ON r.kind='edit.observed' AND r.entity=e.id JOIN changesets c ON c.id=e.changeset_id JOIN changeset_repositories cr ON cr.changeset_id=c.id GROUP BY r.workspace,cr.repository,c.conversation,c.id;
CREATE VIEW IF NOT EXISTS identity_assertions AS SELECT * FROM assertions;
CREATE VIEW IF NOT EXISTS capture_gaps AS SELECT * FROM gaps;
CREATE VIEW IF NOT EXISTS device_activity AS SELECT author,workspace,COUNT(*) events,MIN(observed_at) first_event,MAX(observed_at) last_event FROM raw_events GROUP BY author,workspace;
CREATE VIEW IF NOT EXISTS checkpoint_states AS SELECT repository,head,state_hash,paths,observed_at FROM checkpoints;
CREATE VIEW IF NOT EXISTS sharing_boundaries AS SELECT * FROM boundaries;
CREATE VIEW IF NOT EXISTS repository_lineages AS SELECT lineage,COUNT(*) repositories,group_concat(id) repository_ids FROM repositories GROUP BY lineage;
"""

def connect(path):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True); db=sqlite3.connect(path); os.chmod(path,0o600); db.row_factory=sqlite3.Row; db.executescript("PRAGMA journal_mode=WAL;"+SCHEMA); return db
def run(root,*args): return subprocess.run(("git","-C",str(root),*args),capture_output=True,check=True).stdout
def maybe(root,*args):
    try: return run(root,*args)
    except subprocess.CalledProcessError: return b""
@lru_cache(maxsize=4096)
def _git_root(path):
    probe=path if path.is_dir() else path.parent
    try: return Path(run(probe,"rev-parse","--show-toplevel").decode().strip()).resolve()
    except (subprocess.CalledProcessError,FileNotFoundError): return None
def _remotes(root):
    out=[]
    for line in run(root,"remote","-v").decode(errors="replace").splitlines():
        if "\t" in line and not (url:=line.split("\t",1)[1].rsplit(" ",1)[0]).startswith(("/","file:")): out.append(url.replace("git@github.com:","https://github.com/").removesuffix(".git"))
    return sorted(set(out))
@lru_cache(maxsize=256)
def _repository(root):
    root=Path(root); roots=sorted(maybe(root,"rev-list","--max-parents=0","HEAD").decode().split()); remotes=_remotes(root); lineage=digest({"git_roots":roots}) if roots else None
    return dict(id=digest({"remotes":remotes}) if remotes else lineage or digest({"local":str(root)}),lineage=lineage,root=str(root),roots=roots,remotes=remotes,checkout=digest(str(root))[:32])
def repository(path):
    root=_git_root(path)
    if not root: return None
    return {**_repository(str(root)),"head":maybe(root,"rev-parse","--verify","HEAD").decode().strip(),"branch":maybe(root,"symbolic-ref","--short","HEAD").decode().strip()}
def _where(path,cwd,device,cache=None):
    p=Path(path); p=(Path(cwd)/p if not p.is_absolute() and cwd else p).expanduser().resolve(); root=_git_root(p); repo=(cache.get(str(root)) if cache is not None and root else None) or (repository(root) if root else None)
    if repo and cache is not None: cache[repo["root"]]=repo
    if repo: return repo, p.relative_to(repo["root"]).as_posix(), "repository"
    return None, f"external/{digest(f'{device}:{p}')[:24]}/{p.name}", "external"
def _changed(root):
    raw=run(root,"status","--porcelain=v1","-z").decode(errors="replace"); return sorted({x[3:].split(" -> ")[-1] for x in raw.split("\0") if len(x)>3})
def _checkpoint(repo):
    paths=_changed(repo["root"]); state=digest((maybe(repo["root"],"diff","--binary","HEAD")+maybe(repo["root"],"diff","--binary","--cached","HEAD")) if repo["head"] else run(repo["root"],"status","--porcelain=v1","-z")); ts=datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
    return dict(id=digest({"repository":repo["id"],"head":repo["head"],"state":state}),repository=repo["id"],head=repo["head"],state_hash=state,paths=paths,observed_at=ts)
def _record(kind,entity,payload): return dict(kind=kind,entity=entity,payload=payload)

def capture(core, graph, device):
    _git_root.cache_clear(); _repository.cache_clear()
    sql="""SELECT fe.id,fe.file_path,fe.edit_type,fe.content,fe.old_content,CAST(fe.created_at AS VARCHAR),m.id,m.conversation_id,c.cwd FROM file_edits fe JOIN messages m ON m.id=fe.message_id JOIN conversations c ON c.id=m.conversation_id ORDER BY fe.created_at,fe.id"""
    edits=[dict(zip(("origin","path","type","content","old","ts","turn","conversation","cwd"),r)) for r in core.execute(sql).fetchall()]; records=[]; repos={}; touched={}; versions={}; repo_cache={}
    for e in edits:
        repo,path,kind=_where(e["path"],e["cwd"],device,repo_cache); rid=repo["id"] if repo else None; fid=digest({"repository":rid or f"external:{device}","path":path}); cid=digest({"device":device,"conversation":e["conversation"],"turn":e["turn"]}); eid=digest({"device":device,"edit":e["origin"]}); prompt=(core.execute("SELECT content FROM messages WHERE conversation_id=? AND role='user' AND created_at<=? ORDER BY created_at DESC LIMIT 1",(e["conversation"],e["ts"])).fetchone() or [None])[0]
        if repo and rid not in repos: repos[rid]=repo; graph.execute("INSERT OR REPLACE INTO checkouts VALUES (?,?,?,?,?)",(repo["checkout"],rid,repo["root"],repo["branch"],repo["head"])); records.append(_record("repository.observed",rid,{k:repo[k] for k in ("id","lineage","roots","remotes","head")}))
        full=digest(Path(repo["root"],path).read_bytes()) if repo and Path(repo["root"],path).is_file() else None
        records += [_record("changeset.observed",cid,{"id":cid,"conversation":e["conversation"],"turn":e["turn"],"prompt":prompt,"observed_at":e["ts"]}), _record("file.observed",fid,{"id":fid,"repository":rid,"path":path,"kind":kind}), _record("edit.observed",eid,{"id":eid,"changeset":cid,"file":fid,"repository":rid,"type":e["type"],"before_hash":digest((e["old"] or "").encode()) if e["old"] is not None else None,"after_hash":digest((e["content"] or "").encode()),"evidence":"captured_exact" if e["type"]=="write" or e["old"] is not None else "content_unknown","observed_at":e["ts"],"origin":e["origin"]})]
        if full: versions[(rid,fid)]=(cid,digest((e["content"] or "").encode()),full,path)
        if rid: touched.setdefault(rid,set()).add(path)
    for rid,repo in repos.items():
        cp=_checkpoint(repo); records.append(_record("git.checkpoint",cp["id"],cp))
        for (r,fid),(cid,after,full,path) in versions.items():
            if r==rid: vid=digest({"file":fid,"content":full}); records.append(_record("file.version",vid,{"id":vid,"file":fid,"content_hash":full,"observed_at":cp["observed_at"]}))
            if r==rid and path not in cp["paths"] and after==full: records.append(_record("checkpoint.link",digest({"checkpoint":cp["id"],"changeset":cid}),{"checkpoint":cp["id"],"changeset":cid,"evidence":"full_content_match"}))
        for path in set(cp["paths"])-touched.get(rid,set()): records.append(_record("capture.gap",digest({"checkpoint":cp["id"],"path":path}),{"repository":rid,"checkpoint":cp["id"],"path":path,"relation":"unobserved_change","observed_at":cp["observed_at"]}))
    graph.commit(); return records

def project(db,value,workspace):
    p,k=value["payload"],value["kind"]; db.execute("INSERT OR IGNORE INTO raw_events VALUES (?,?,?,?,?,?,?,?)",(value["id"],workspace,k,value["entity"],value["author"],value["seq"],value["observed_at"],json.dumps(p)))
    if k=="repository.observed": db.execute("INSERT OR REPLACE INTO repositories VALUES (?,?,?,?,?,?)",(p["id"],p.get("lineage"),json.dumps(p["roots"]),json.dumps(p["remotes"]),p["head"],value["observed_at"]))
    elif k=="changeset.observed": db.execute("INSERT OR IGNORE INTO changesets VALUES (?,?,?,?,?)",(p["id"],p["conversation"],p["turn"],p.get("prompt"),p["observed_at"]))
    elif k=="file.observed": db.execute("INSERT OR IGNORE INTO files VALUES (?,?,?,?)",(p["id"],p["repository"],p["path"],p["kind"]))
    elif k=="file.version": db.execute("INSERT OR IGNORE INTO file_versions VALUES (?,?,?,?)",(p["id"],p["file"],p["content_hash"],p["observed_at"]))
    elif k=="edit.observed":
        db.execute("INSERT OR IGNORE INTO edits VALUES (?,?,?,?,?,?,?,?,?)",(p["id"],p["changeset"],p["file"],p["type"],p["before_hash"],p["after_hash"],p["evidence"],p["observed_at"],p["origin"])); p["repository"] and db.execute("INSERT OR IGNORE INTO changeset_repositories VALUES (?,?)",(p["changeset"],p["repository"]))
    elif k=="git.checkpoint": db.execute("INSERT OR IGNORE INTO checkpoints VALUES (?,?,?,?,?,?)",(p["id"],p["repository"],p["head"],p["state_hash"],json.dumps(p["paths"]),p["observed_at"]))
    elif k=="checkpoint.link": db.execute("INSERT OR IGNORE INTO checkpoint_changesets VALUES (?,?,?)",(p["checkpoint"],p["changeset"],p["evidence"]))
    elif k=="identity.assertion": db.execute("INSERT OR IGNORE INTO assertions VALUES (?,?,?,?,?,?,?)",(p["id"],p["left"],p["relation"],p["right"],p["evidence"],p.get("status","active"),p["observed_at"]))
    elif k=="capture.gap": db.execute("INSERT OR IGNORE INTO gaps VALUES (?,?,?,?,?,?)",(value["entity"],p["repository"],p["checkpoint"],p["path"],p["relation"],p["observed_at"]))
    elif k=="changeset.boundary": db.execute("INSERT OR IGNORE INTO boundaries VALUES (?,?,?,?)",(value["entity"],workspace,p["changeset"],p["hidden_count"]))
    db.commit(); return k in {"repository.observed","changeset.observed","file.observed","file.version","edit.observed","git.checkpoint","checkpoint.link","identity.assertion","capture.gap","changeset.boundary"}

def query(db,name,arg=None):
    if name=="checkpoint_diff":
        before,after=arg.split("..",1); a,b=(db.execute("SELECT * FROM checkpoints WHERE id=?",(x,)).fetchone() for x in (before,after))
        if not a or not b or a["repository"]!=b["repository"]: raise ValueError("checkpoint ids must exist in one repository")
        checkout=db.execute("SELECT root FROM checkouts WHERE repository=? LIMIT 1",(a["repository"],)).fetchone(); available=bool(checkout and a["head"] and b["head"]); changed=run(checkout[0],"diff","--name-status",a["head"],b["head"]).decode().splitlines() if available else None
        return [dict(repository=a["repository"],before=before,after=after,head_before=a["head"],head_after=b["head"],available=available,changed=changed,state_changed=a["state_hash"]!=b["state_hash"])]
    if name=="current_activity":
        repo=repository(Path(arg or ".").resolve())
        return query(db,"repository_activity",repo["id"] if repo else "")
    if name=="team_activity":
        workspace,path=arg.split("|",1); repo=repository(Path(path).resolve())
        return [dict(r) for r in db.execute("SELECT * FROM workspace_repository_activity WHERE workspace=? AND repository=?",(workspace,repo["id"] if repo else "")).fetchall()]
    allowed={"file_history":"path","changeset_files":"id","conversation_changes":"conversation","commit_conversations":"head","repository_activity":"repository","workspace_repository_activity":"repository","identity_assertions":"left_id","capture_gaps":"repository","device_activity":"author","checkpoint_states":"repository","sharing_boundaries":"changeset_id","repository_lineages":"lineage"}
    if name not in allowed: raise ValueError(f"unknown graph view {name}")
    sql=f"SELECT * FROM {name}"; params=()
    if arg is not None: sql+=f" WHERE {allowed[name]}=?"; params=(arg,)
    return [dict(r) for r in db.execute(sql,params).fetchall()]
