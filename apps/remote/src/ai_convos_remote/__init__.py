"""Client-side enrollment, E2EE keyring, automatic sync, membership, and local queries."""
import fcntl, json, os, sqlite3, time, urllib.error, urllib.parse, urllib.request
from contextlib import contextmanager
from pathlib import Path; from typing import Optional

import typer
from ai_convos_redact import protect as protect_record
_pending=[]
def register(app): _pending.append(app) if "remote" not in globals() else app.add_typer(remote,name="remote")
from ai_convos.cli import PROJECT_ROOT, archive_state as core_archive_state, drain_hooks, init_schema, install_hooks, open_db, project_attachment_body, project_workspace_controls, repository
from .control import CONTROL_V, approved, electorate, proposal as device_proposal, record as control_record, sign as control_sign, state_hash, verify_proposal, verify_state, vote as device_vote
from .projection import SIGNED, TABLES, apply_row_replicas, attest_rows, blob_replicas, bridge_purges, bridge_records, connect, control_chain, cutover_state, event_support, inspect_state, project, project_many, query as graph_query, read_state, relocate_attachments, reset_history, row_replicas, scan, sequence, stored_controls, verify_history
from .protocol import (b64, certificate, digest, event, identity, open_blob, open_event, open_key, open_origin, open_replica, public, public_id, purge_certificate, recover,
                       recovery_bundle, seal_event, seal_key, seal_origin, seal_replica, sign_control, signer, unb64, verify_certificate, verify_purge)
from .service import edit_hooks, enable

remote=typer.Typer(help="End-to-end encrypted personal and team synchronization")
def local_root(root=None): return Path(root or os.environ.get("CONVOS_PROJECT_ROOT",PROJECT_ROOT))
def paths(root=None):
    base=local_root(root)/"remote"; return base,base/"config.json",base/"state.db"
def core_path(root=None): return local_root(root)/"data"/"convos.db"
def archive_info(root=None,create=False):
    path=core_path(root)
    if not path.exists() and not create: return None
    path.parent.mkdir(parents=True,exist_ok=True); db=open_db(path); init_schema(db)
    try: return core_archive_state(db)
    finally: db.close()
def load(root=None):
    _,path,_=paths(root)
    if not path.exists(): raise ValueError("Remote is not configured. Run `convos remote setup`.")
    return json.loads(path.read_text())
def save(cfg,root=None):
    base,path,_=paths(root); base.mkdir(parents=True,exist_ok=True); os.chmod(base,0o700); tmp=path.with_name(f".{path.name}.{os.getpid()}"); tmp.write_text(json.dumps(cfg)); os.chmod(tmp,0o600); os.replace(tmp,path)
def rescue_bindings(cfg,state_path,root=None):
    db=sqlite3.connect(Path(state_path).resolve().as_uri()+"?mode=ro",uri=True)
    try: columns={r[1] for r in db.execute("PRAGMA table_info(policies)").fetchall()}; rows=db.execute("SELECT workspace,value,local_root FROM policies WHERE kind='path' AND local_root IS NOT NULL").fetchall() if "local_root" in columns else []
    finally: db.close()
    for ws,value,path in rows:
        if not isinstance(path,str) or not Path(path).is_absolute(): raise ValueError("legacy path binding is invalid")
        cfg.setdefault("bindings",{})[f"{ws}:{value}"]=path
    if rows: save(cfg,root)
    return len(rows)
def encrypted_file(root,event,value):
    path=paths(root)[0]/"outbox"/f"{event}.json"; path.parent.mkdir(parents=True,exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink(): raise ValueError("encrypted outbox path must not be a symlink")
    os.chmod(path.parent,0o700); raw=json.dumps(value,separators=(",",":")).encode(); tmp=path.with_name(f".{path.name}.{os.getpid()}"); handle=tmp.open("xb"); os.chmod(tmp,0o600); handle.write(raw); handle.close(); os.replace(tmp,path); return path,len(raw)
def receipt(state,ws,value,cursor,epoch):
    status=value["payload"].get("status") if isinstance(value["payload"],dict) and value["payload"].get("status") in ("active","deleted") else None
    state.execute("INSERT OR REPLACE INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?,?)",(ws,value["id"],cursor,value["author"],value["seq"],epoch,value["kind"],value["payload_v"],value["entity"],value["revision"],status))
def safe_url(url): parsed=urllib.parse.urlparse(url); parsed.scheme=="https" or parsed.hostname in ("127.0.0.1","localhost","::1") or os.environ.get("CONVOS_REMOTE_INSECURE")=="1" or (_ for _ in ()).throw(ValueError("Remote URL must use HTTPS (set CONVOS_REMOTE_INSECURE=1 only on a trusted test network)"))
def request(cfg,body,auth=True):
    safe_url(cfg["url"])
    headers={"Content-Type":"application/json"};
    if auth: headers["Authorization"]="Bearer "+cfg["token"]
    req=urllib.request.Request(cfg["url"].rstrip("/")+"/v1",data=json.dumps(body).encode(),headers=headers,method="POST")
    try: return json.loads(urllib.request.urlopen(req,timeout=10).read())
    except urllib.error.HTTPError as e: raise ValueError(json.loads(e.read())["error"]) from e
def health(cfg): safe_url(cfg["url"]); result=json.loads(urllib.request.urlopen(cfg["url"].rstrip("/")+"/v1/health",timeout=3).read()); result.get("version")==1 or (_ for _ in ()).throw(ValueError("relay protocol v1 required")); return result
@contextmanager
def sync_lock(root):
    path=paths(root)[0]/"sync.lock"; path.parent.mkdir(parents=True,exist_ok=True); handle=path.open("a+"); os.chmod(path,0o600); fcntl.flock(handle,fcntl.LOCK_EX)
    try: handle.seek(0); handle.truncate(); handle.write(str(os.getpid())); handle.flush(); yield
    finally: fcntl.flock(handle,fcntl.LOCK_UN); handle.close()
def workspace(cfg,value):
    hits=[k for k,v in cfg["workspaces"].items() if k.startswith(value) or v["name"]==value]
    if len(hits)!=1: raise ValueError(f"Workspace must match exactly one of: {', '.join(v['name'] for v in cfg['workspaces'].values())}")
    return hits[0]
def key(cfg,ws,epoch): return unb64(cfg["keys"][f"{ws}:{epoch}"])
def trusted(devices):
    for d in devices: d["certificate"] or (_ for _ in ()).throw(ValueError("device certificate missing")); body=verify_certificate(json.loads(d["certificate"]),d["root_public"]); signer({d["id"]:d},d["id"]); (public_id(d["root_public"])==d["user_id"] and body["user"]==d["user_id"] and body["device"]==public(d)) or (_ for _ in ()).throw(ValueError("device certificate mismatch"))
    return devices
def server_record(d,history=True): return control_record(d["user_id"],d["root_public"],d,json.loads(d["certificate"]) if isinstance(d["certificate"],str) else d["certificate"],history)
def own_record(cfg,history=True): return control_record(cfg["user"],cfg["root"]["sign_public"],cfg["device"],certificate(cfg["root"],cfg["user"],cfg["device"]),history)
def control_body(cfg,previous,key_,action,members=None,devices=None,removed=None,approval=None,boundary=None):
    advance=action not in ("history","history_activate"); boundary=(boundary or (_ for _ in ()).throw(ValueError("new workspace epoch requires a signed history boundary"))) if advance else previous["boundary"]; return control_sign(cfg["device"],{"v":CONTROL_V,"kind":"workspace.state","workspace":previous["workspace"],"scope":previous["scope"],"revision":previous["revision"]+1,"prev":state_hash(previous),"epoch":previous["epoch"]+advance,"boundary":boundary,"key_commitment":digest(key_),"members":members or previous["members"],"devices":devices or previous["devices"],"removed":removed if removed is not None else previous["removed"],"action":action,"approval":approval,"approved_at":time.time()})
def access_from(cfg,ws):
    head=cfg["controls"][ws]; device=cfg["device"]["id"]; first=min(value["epoch"] for remote in cfg["server_state"]["workspaces"] if remote["id"]==ws for value in remote["controls"] if device in value["devices"]); return head["members"][cfg["user"]]["history_from"] if head["devices"][device]["history"] else first
def sequence_heads(state,ws): return {author:{"seq":seq,"event":event} for author,seq,event in state.execute("SELECT s.author,s.seq,s.event FROM event_sequences s JOIN (SELECT author,MAX(seq) seq FROM event_sequences WHERE workspace=? GROUP BY author) h ON h.author=s.author AND h.seq=s.seq WHERE s.workspace=?",(ws,ws)).fetchall()}
def prepare_archive(cfg,state,root=None,create=True):
    info=archive_info(root); empty=not info or info[1:]==(0,0); cached=[(state.execute("SELECT value FROM meta WHERE key=?",(key,)).fetchone() or [None])[0] for key in ("archive_id","archive_generation")]; proofs=[p for p in (cfg.get("archive"),{"id":cached[0],"generation":int(cached[1])} if all(cached) else None) if p]; existing={r[0].split(":",1)[1]:r[1] for r in state.execute("SELECT key,value FROM meta WHERE key LIKE 'archive_mode:%'").fetchall()}; fresh=not state.execute("SELECT 1 FROM sync_states LIMIT 1").fetchone(); mode=None
    if existing and not empty: return existing
    if not existing and not proofs: mode="native" if empty else "adopt"
    elif not existing and (not info or any(info[0]!=p["id"] or info[1]<p["generation"] for p in proofs)): mode="native" if empty else "import"
    elif not existing and fresh: mode="adopt"
    if not existing and not mode: return None
    if create and not info: archive_info(root,True)
    active={w["id"] for w in cfg["server_state"]["workspaces"] if w["device_authorized"]}
    for ws in active:
        personal=cfg["workspaces"][ws]["kind"]=="personal"; selected=existing.get(ws,"native" if personal else "adopt") if existing else mode if personal else "adopt"; selected="native" if existing and selected=="import" else selected; state.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",(f"archive_mode:{ws}",selected)); state.execute("DELETE FROM meta WHERE key=?",(f"boundary:{ws}",)); state.execute("INSERT OR REPLACE INTO sync_states VALUES (?,'rebaselining',COALESCE((SELECT tail FROM sync_states WHERE workspace=?),0),COALESCE((SELECT floor FROM sync_states WHERE workspace=?),0),NULL)",(ws,ws,ws))
    state.commit(); return existing or mode
def remember_archive(cfg,state,root=None):
    if info:=archive_info(root): cfg["archive"]={"id":info[0],"generation":info[1]}; save(cfg,root); state.execute("INSERT OR REPLACE INTO meta VALUES ('archive_id',?),('archive_generation',?)",(info[0],str(info[1]))); state.execute("DELETE FROM meta WHERE key LIKE 'archive_mode:%'")
def next_boundary(cfg,ws,root=None):
    epoch=cfg["controls"][ws]["epoch"]+1
    if cfg["device"]["id"] in cfg["controls"][ws]["devices"]:
        state=connect(paths(root)[2])
        try: upload(cfg,state,root,{ws}); prepare_archive(cfg,state,root,False); pull(cfg,state,root); local=sequence_heads(state,ws); remote=request(cfg,{"op":"ledger","workspace":ws})
        finally: state.close()
        if local!=remote["heads"]: raise ValueError("relay ledger changed while creating history boundary")
    else: remote=request(cfg,{"op":"ledger","workspace":ws})
    return {"epoch":epoch,**remote}
def directory_user(found,user): field="id" if len(user)==32 and all(c in "0123456789abcdef" for c in user) else "name"; values=[v for v in found["users"] if v[field]==user]; return values[0] if len(values)==1 and public_id(values[0]["root_public"])==values[0]["id"] else (_ for _ in ()).throw(ValueError("directory user mismatch"))
def update_recovery(cfg,root=None):
    personal={ws for ws,v in cfg["workspaces"].items() if v["kind"]=="personal"}; keys={name:value for name,value in cfg["keys"].items() if name.rsplit(":",1)[0] in personal}; _,bundle=recovery_bundle({"root":cfg["root"],"keys":keys,"workspaces":cfg["workspaces"],"controls":{ws:v for ws,v in cfg["controls"].items() if ws in personal}},unb64(cfg["recovery"])); request(cfg,sign_control(cfg["device"],{"op":"recovery","bundle":bundle})); save(cfg,root)
def refresh(cfg,root=None):
    state=request(cfg,{"op":"state"}); cfg["server_state"]=state; changed=False; seen=set()
    if (state["user"],state["device"])!=(cfg["user"],cfg["device"]["id"]): raise ValueError("relay identity metadata mismatch")
    for ws in state["workspaces"]:
        if ws["id"] in seen: raise ValueError("duplicate relay workspace")
        seen.add(ws["id"])
        previous=None
        for value in ws["controls"]: verify_state(value,previous); previous=value
        if not previous or (ws["id"],ws["kind"],ws["epoch"])!=(previous["workspace"],previous["scope"],previous["epoch"]): raise ValueError("relay workspace metadata does not match signed state")
        member=previous["members"].get(cfg["user"]); authorized=cfg["device"]["id"] in previous["devices"] and cfg["device"]["id"] not in previous["removed"]
        if not member or (ws["role"],ws["history_from"],bool(ws["device_authorized"]))!=(member["role"],member["history_from"],authorized): raise ValueError("relay access metadata does not match signed state")
        pinned=cfg["controls"].get(ws["id"])
        if pinned and (not previous or pinned["revision"]>previous["revision"] or state_hash(pinned) not in {state_hash(v) for v in ws["controls"]}): raise ValueError("workspace control rollback or fork")
        cfg["controls"][ws["id"]]=previous; cfg["workspaces"].setdefault(ws["id"],{"name":ws["id"][:8]}); cfg["workspaces"][ws["id"]].update(kind=previous["scope"],epoch=previous["epoch"]); ws.update(kind=previous["scope"],epoch=previous["epoch"],role=member["role"],history_from=member["history_from"],device_authorized=authorized)
        first=min((v["epoch"] for v in ws["controls"] if cfg["device"]["id"] in v["devices"]),default=previous["epoch"]); start=member["history_from"] if authorized and previous["devices"][cfg["device"]["id"]]["history"] else first; epochs=[int(v["epoch"]) for v in ws["keys"]]
        if authorized and (len(epochs)!=len(set(epochs)) or any(not start<=epoch<=previous["epoch"] for epoch in epochs)): raise ValueError("relay key envelope exceeds signed entitlement")
        for wrapped in ws["keys"] if authorized else []:
            name=f"{ws['id']}:{wrapped['epoch']}"
            if name not in cfg["keys"]:
                opened=open_key(json.loads(wrapped["envelope"]),cfg["device"]["box_private"],f"workspace:{ws['id']}:epoch:{wrapped['epoch']}"); controls=[v for v in ws["controls"] if v["epoch"]==wrapped["epoch"]]
                if not controls or digest(opened)!=controls[-1]["key_commitment"]: raise ValueError("workspace epoch key commitment mismatch")
                cfg["keys"][name]=b64(opened); changed=True
    save(cfg,root)
    if changed: update_recovery(cfg,root)
    return state
def create(cfg,name,kind="team",root=None):
    ws,key_=digest(os.urandom(32))[:32],os.urandom(32); entry=own_record(cfg); control=control_sign(cfg["device"],{"v":CONTROL_V,"kind":"workspace.state","workspace":ws,"scope":kind,"revision":1,"prev":None,"epoch":1,"boundary":{"epoch":1,"tail":0,"heads":{}},"key_commitment":digest(key_),"members":{cfg["user"]:{"role":"admin","joined":1,"history_from":1}},"devices":{cfg["device"]["id"]:entry},"removed":[],"action":"create","approval":None,"approved_at":time.time()}); env=seal_key(key_,cfg["device"]["box_public"],f"workspace:{ws}:epoch:1"); request(cfg,sign_control(cfg["device"],{"op":"create","workspace":ws,"kind":kind,"control":control,"envelopes":{cfg["device"]["id"]:env}})); cfg["workspaces"][ws]={"name":name,"kind":kind,"epoch":1}; cfg["keys"][f"{ws}:1"]=b64(key_); cfg["controls"][ws]=control; update_recovery(cfg,root); membership_event(cfg,ws,1,{cfg["user"]:"admin"},root); return ws
def setup_client(url,user,device="computer",recovery=None,root=None):
    if recovery:
        bundle=request({"url":url},{"op":"recovery_fetch","user":user},False)["bundle"]; recovered=recover(bundle,recovery); root_id=recovered["root"]; keys,workspaces,controls=recovered["keys"],recovered["workspaces"],recovered.get("controls",{})
    else: root_id,keys,workspaces,controls=identity(user+" root"),{},{},{}
    dev=identity(device); uid=root_id["id"]
    if not recovery: recovery,bundle=recovery_bundle({"root":root_id,"keys":keys,"workspaces":workspaces})
    registered=request({"url":url},{"op":"register","user_name":user,"root_public":root_id["sign_public"],"certificate":certificate(root_id,uid,dev),**({"recovery":bundle} if not workspaces else {})},False)
    cfg={"url":url,"name":user,"user":uid,"token":registered["token"],"root":root_id,"device":dev,"recovery":recovery,"keys":keys,"workspaces":workspaces,"controls":controls,"bindings":{},"server_state":{}}; save(cfg,root)
    if not workspaces: create(cfg,"Personal","personal",root)
    else:
        state=refresh(cfg,root)
        for ws in [w for w in state["workspaces"] if cfg["controls"][w["id"]]["members"][uid]["role"]=="admin" and cfg["controls"][w["id"]]["scope"]=="personal"]:
            rotate(cfg,ws["id"],{u:m["role"] for u,m in cfg["controls"][ws["id"]]["members"].items()},[],root=root); grant_all(cfg,ws["id"],uid,root)
    return cfg,recovery
def rehome_client(cfg,url,root=None):
    recovery,bundle=recovery_bundle({"root":cfg["root"],"keys":{},"workspaces":{}}); registered=request({"url":url},{"op":"register","user_name":cfg["name"],"root_public":cfg["root"]["sign_public"],"certificate":certificate(cfg["root"],cfg["user"],cfg["device"]),"recovery":bundle},False); fresh={**cfg,"url":url,"token":registered["token"],"recovery":recovery,"keys":{},"workspaces":{},"controls":{},"bindings":{},"server_state":{}}; save(fresh,root); create(fresh,"Personal","personal",root); return fresh,recovery
def rotate(cfg,ws,members,devices,deactivate=(),root=None):
    state=refresh(cfg,root); previous=cfg["controls"][ws]; epoch=previous["epoch"]+1; boundary=next_boundary(cfg,ws,root); new=os.urandom(32); devices=trusted(devices); old=previous["members"]; meta={u:old.get(u,{"joined":epoch,"history_from":epoch})|{"role":role} for u,role in members.items()}; removed=sorted(set(previous["removed"])|set(deactivate)|{d for d,r in previous["devices"].items() if r["user"] not in members}); records={d:r for d,r in previous["devices"].items() if r["user"] in members and d not in deactivate}
    if cfg["device"]["id"] not in previous["devices"] and previous["scope"]=="personal":
        entry=own_record(cfg); req=device_proposal(cfg["device"],ws,previous,{**entry,"history":True},time.time()+300); records|={cfg["device"]["id"]:{**entry,"history":True}}; action,approval="personal_recover",{"proposal":req,"votes":[]}
    else:
        action,approval=("remove",None) if deactivate else ("membership",None); new_users=set(members)-set(old)
        records|={d["id"]:server_record(d) for d in devices if d["user_id"] in new_users and d["id"] not in removed and d.get("active",1) and d.get("allowed",1)}
    control=control_body(cfg,previous,new,action,meta,records,removed,approval,boundary); envs={d:seal_key(new,r["device"]["box_public"],f"workspace:{ws}:epoch:{epoch}") for d,r in records.items()}; request(cfg,sign_control(cfg["device"],{"op":"rotate","workspace":ws,"control":control,"envelopes":envs})); cfg["keys"][f"{ws}:{epoch}"]=b64(new); cfg["workspaces"][ws]["epoch"]=epoch; cfg["controls"][ws]=control; update_recovery(cfg,root); cfg["device"]["id"] in records and membership_event(cfg,ws,epoch,members,root); return epoch
def publish(cfg,state,ws,record,root=None,defer=False,heads=None):
    if cfg["workspaces"][ws]["kind"]=="team" and (record:=protect_record(record,root,ws)) is None: return None
    payload_v=record.get("payload_v",1)
    if event_support({"kind":record["kind"],"payload_v":payload_v})!="supported": raise ValueError(f"unsupported event schema: {record['kind']} payload_v={payload_v}")
    revision=digest(record["payload"]); old=heads.get(record["entity"]) if heads is not None else state.execute("SELECT revision,event FROM publication_heads WHERE workspace=? AND owner=? AND entity=?",(ws,cfg["user"],record["entity"])).fetchone()
    if heads is not None and old==revision: return None
    if heads is None and old and old["revision"]==revision: return old["event"]
    seq=int((state.execute("SELECT value FROM meta WHERE key=?",(f"seq:{ws}",)).fetchone() or ["0"])[0])+1; prev=(state.execute("SELECT value FROM meta WHERE key=?",(f"prev:{ws}",)).fetchone() or [None])[0]; value=event(cfg["device"],seq,record["kind"],record["entity"],record["payload"],[prev] if prev else (),record.get("observed_at"),payload_v); epoch=cfg["workspaces"][ws]["epoch"]; env=seal_event(value,ws,epoch,key(cfg,ws,epoch)); path,size=encrypted_file(root,value["id"],env); status=record["payload"].get("status") if isinstance(record["payload"],dict) and record["payload"].get("status") in ("active","deleted") else None
    state.execute("INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(ws,value["id"],record["entity"],revision,value["author"],seq,epoch,record["kind"],payload_v,status,str(path),size)); state.execute("INSERT OR REPLACE INTO publication_heads VALUES (?,?,?,?,?)",(ws,cfg["user"],record["entity"],revision,value["id"])); state.execute("INSERT OR REPLACE INTO meta VALUES (?,?),(?,?)",(f"seq:{ws}",str(seq),f"prev:{ws}",value["id"])); sequence(state,ws,value)
    if not defer: state.commit()
    if heads is not None: heads[record["entity"]]=revision
    project(core_path(root),state,value,ws,cfg["device"]["id"],root=root,authors={cfg["device"]["id"]:cfg["user"]}); return value["id"]
def membership_event(cfg,ws,epoch,members,root=None):
    state=connect(paths(root)[2]); publish(cfg,state,ws,{"kind":"workspace.membership","entity":f"membership:{epoch}","payload":{"epoch":epoch,"members":members}},root); upload(cfg,state,root); state.close()
def control_event(cfg,ws,action,target,root=None):
    state=connect(paths(root)[2]); value=cfg["controls"][ws]; publish(cfg,state,ws,{"kind":"workspace.device","entity":f"device:{target}:{value['revision']}","payload":{"action":action,"device":target,"revision":value["revision"],"state":state_hash(value)}},root); upload(cfg,state,root); state.close()
def _upload_batches(rows,limit=8*1024*1024):
    batch,size=[],0
    for row in rows:
        if batch and (len(batch)==500 or size+row["size"]>limit): yield batch; batch,size=[],0
        batch.append(row); size+=row["size"]
    if batch: yield batch
def upload(cfg,state,root=None,workspaces=None):
    active={w["id"] for w in refresh(cfg,root)["workspaces"]}
    if workspaces is not None: active&=set(workspaces)
    rows=[r for r in state.execute("SELECT * FROM outbox ORDER BY workspace,seq").fetchall() if r["workspace"] in active]
    for batch in _upload_batches(rows):
        prepared=[]; acknowledged=[]
        for row in batch:
            expected=paths(root)[0]/"outbox"/f"{row['event']}.json"
            if Path(row["path"])!=expected or expected.is_symlink(): raise ValueError("invalid encrypted outbox path")
            env=json.loads(expected.read_text()); current=cfg["workspaces"][row["workspace"]]["epoch"]
            if env["epoch"]!=current:
                try: found=request(cfg,{"op":"fetch","workspace":row["workspace"],"event":row["event"]})
                except ValueError as e:
                    if str(e)!="event not found": raise
                    value=open_event(env,key(cfg,row["workspace"],env["epoch"]),cfg["device"]["sign_public"]); env=seal_event(value,row["workspace"],current,key(cfg,row["workspace"],current)); path,size=encrypted_file(root,row["event"],env); state.execute("UPDATE outbox SET epoch=?,path=?,size=? WHERE workspace=? AND event=?",(current,str(path),size,row["workspace"],row["event"]))
                else:
                    if found["envelope"]!=env or not isinstance(found["cursor"],int) or isinstance(found["cursor"],bool): raise ValueError("relay upload acknowledgement mismatch")
                    acknowledged.append((row,env,expected,{"cursor":found["cursor"]})); continue
            prepared.append((row,env,expected))
        state.commit(); result=request(cfg,{"op":"upload_many","envelopes":[p[1] for p in prepared]})["events"] if prepared else []
        if len(result)!=len(prepared) or any(not isinstance(r.get("cursor"),int) or isinstance(r["cursor"],bool) or r["cursor"]<1 for r in result): raise ValueError("relay upload acknowledgement mismatch")
        for row,env,path,ack in acknowledged+[(row,env,path,ack) for (row,env,path),ack in zip(prepared,result)]:
            state.execute("INSERT OR REPLACE INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?,?)",(row["workspace"],row["event"],ack["cursor"],row["author"],row["seq"],env["epoch"],row["kind"],row["payload_v"],row["entity"],row["revision"],row["status"])); state.execute("DELETE FROM outbox WHERE workspace=? AND event=?",(row["workspace"],row["event"]))
        state.commit(); [path.unlink(missing_ok=True) for row,env,path,ack in acknowledged+[(row,env,path,ack) for (row,env,path),ack in zip(prepared,result)]]
    upload_replicas(cfg,state,root,active); upload_blobs(cfg,state,root,active)
def queue_replica(state,root,env):
    key_=(env["workspace"],env["replica"],env["epoch"],env["uploader"])
    if state.execute("SELECT 1 FROM replica_outbox WHERE workspace=? AND replica=? AND epoch=? AND uploader=? UNION ALL SELECT 1 FROM replica_receipts WHERE workspace=? AND replica=? AND epoch=? LIMIT 1",key_+key_[:3]).fetchone(): return False
    path,size=encrypted_file(root,"replica-"+digest(key_),env); state.execute("INSERT INTO replica_outbox VALUES (?,?,?,?,?,?)",(*key_,str(path),size)); return True
def reconcile_replicas(cfg,state,root,ws,envelopes):
    for page in (envelopes[i:i+500] for i in range(0,len(envelopes),500)):
        ids=[e["replica"] for e in page]; present=request(cfg,{"op":"replica_reconcile","workspace":ws,"replicas":ids})["present"]
        if not isinstance(present,dict) or not set(present)<=set(ids) or any(not isinstance(v,int) or isinstance(v,bool) or v<1 for v in present.values()): raise ValueError("relay replica inventory mismatch")
        for env in page:
            if env["replica"] in present: state.execute("INSERT OR REPLACE INTO replica_receipts VALUES (?,?,?,?)",(ws,env["replica"],env["epoch"],present[env["replica"]]))
            else: queue_replica(state,root,env)
def upload_replicas(cfg,state,root,workspaces):
    rows=[r for r in state.execute("SELECT * FROM replica_outbox ORDER BY workspace,replica").fetchall() if r["workspace"] in workspaces]
    for batch in _upload_batches(rows):
        prepared=[]
        for row in batch:
            path=Path(row["path"]); env=json.loads(path.read_text())
            if (env["workspace"],env["replica"],env["epoch"],env["uploader"])!=(row["workspace"],row["replica"],row["epoch"],row["uploader"]) or path.is_symlink(): raise ValueError("invalid replica outbox")
            prepared.append((row,env,path))
        result=request(cfg,{"op":"replica_upload_many","envelopes":[p[1] for p in prepared]})["replicas"]
        if len(result)!=len(prepared): raise ValueError("relay replica acknowledgement mismatch")
        for (row,env,path),ack in zip(prepared,result): state.execute("INSERT OR REPLACE INTO replica_receipts VALUES (?,?,?,?)",(env["workspace"],env["replica"],env["epoch"],ack["cursor"])); state.execute("DELETE FROM replica_outbox WHERE workspace=? AND replica=? AND epoch=? AND uploader=?",(env["workspace"],env["replica"],env["epoch"],env["uploader"])); path.unlink(missing_ok=True)
        state.commit()
def reconcile_blobs(cfg,state,root,ws,envelopes):
    for page in (envelopes[i:i+500] for i in range(0,len(envelopes),500)):
        ids=[e["blob"] for e in page]; present=request(cfg,{"op":"blob_reconcile","workspace":ws,"blobs":ids})["present"]
        if not isinstance(present,dict) or not set(present)<=set(ids) or any(not isinstance(v,int) or isinstance(v,bool) or v<1 for v in present.values()): raise ValueError("relay blob inventory mismatch")
        for env in page:
            if env["blob"] in present: state.execute("INSERT OR REPLACE INTO blob_receipts VALUES (?,?,?,?)",(ws,env["blob"],env["epoch"],present[env["blob"]]))
            elif not state.execute("SELECT 1 FROM blob_outbox WHERE workspace=? AND blob=? AND epoch=?",(ws,env["blob"],env["epoch"])).fetchone(): path,size=encrypted_file(root,"blob-"+digest((ws,env["blob"],env["epoch"],env["uploader"])),env); state.execute("INSERT INTO blob_outbox VALUES (?,?,?,?,?,?)",(ws,env["blob"],env["epoch"],env["uploader"],str(path),size))
def upload_blobs(cfg,state,root,workspaces):
    for row in [r for r in state.execute("SELECT * FROM blob_outbox ORDER BY workspace,blob").fetchall() if r["workspace"] in workspaces]:
        path=Path(row["path"]); env=json.loads(path.read_text())
        if path.is_symlink() or (env["workspace"],env["blob"],env["epoch"],env["uploader"])!=(row["workspace"],row["blob"],row["epoch"],row["uploader"]): raise ValueError("invalid blob outbox")
        ack=request(cfg,{"op":"blob_upload","envelope":env}); state.execute("INSERT OR REPLACE INTO blob_receipts VALUES (?,?,?,?)",(row["workspace"],row["blob"],row["epoch"],ack["cursor"])); state.execute("DELETE FROM blob_outbox WHERE workspace=? AND blob=? AND epoch=? AND uploader=?",(row["workspace"],row["blob"],row["epoch"],row["uploader"])); state.commit(); path.unlink(missing_ok=True)
def drop_event(state,ws,event):
    state.execute("DELETE FROM publication_heads WHERE workspace=? AND event=?",(ws,event)); state.execute("DELETE FROM receipts WHERE workspace=? AND event=?",(ws,event)); state.execute("DELETE FROM lazy_events WHERE workspace=? AND event=?",(ws,event)); state.execute("DELETE FROM deferred_events WHERE workspace=? AND event=?",(ws,event))
def purge_events(cfg,state,ws,intents):
    if cfg["workspaces"][ws]["kind"]!="personal": raise ValueError("event purge requires a personal workspace")
    certificates=[]
    for intent in intents:
        target=state.execute("SELECT * FROM receipts WHERE workspace=? AND event=?",(ws,intent["event"])).fetchone(); anchor=state.execute("SELECT * FROM receipts WHERE workspace=? AND event=?",(ws,intent["superseded_by"])).fetchone(); head=anchor and state.execute("SELECT event FROM publication_heads WHERE workspace=? AND owner=? AND entity=?",(ws,cfg["user"],anchor["entity"])).fetchone()
        if not target or not anchor or (target["author"],target["kind"],target["payload_v"],target["status"])!=(cfg["device"]["id"],"memory.canonical",1,"active") or (anchor["author"],anchor["kind"],anchor["payload_v"],anchor["status"])!=(target["author"],"memory.canonical",1,"deleted") or anchor["seq"]<=target["seq"] or target["entity"].rsplit(":part:",1)[0]!=anchor["entity"] or not head or head[0]!=anchor["event"]: raise ValueError("invalid purge intent")
        previous=state.execute("SELECT event FROM event_sequences WHERE workspace=? AND author=? AND seq=?",(ws,target["author"],target["seq"]-1)).fetchone() if target["seq"]>1 else None
        if target["seq"]>1 and not previous: raise ValueError("purge target parent is unavailable")
        certificates.append(purge_certificate(cfg["device"],ws,target,[previous[0]] if previous else [],anchor))
    for i in range(0,len(certificates),500):
        batch=certificates[i:i+500]; request(cfg,{"op":"purge","workspace":ws,"certificates":batch}); [drop_event(state,ws,value["event"]) for value in batch]; state.commit()
def pull_origins(cfg,state,root,ws):
    sid=ws["id"]; values=request(cfg,{"op":"origin_pull","workspace":sid})["origins"]
    if not values: return {r[0] for r in state.execute("SELECT origin FROM origin_bindings WHERE workspace=?",(sid,)).fetchall()}
    core=open_db(core_path(root)); init_schema(core); core.execute("BEGIN")
    try:
        for item in values:
            env=item["envelope"]; controls=control_chain(open_origin(env,key(cfg,sid,env["epoch"]))); origin=controls[0]["workspace"]
            if env["workspace"]!=sid or not isinstance(item["cursor"],int) or isinstance(item["cursor"],bool) or not access_from(cfg,sid)<=env["epoch"]<=cfg["controls"][sid]["epoch"] or origin==sid: raise ValueError("origin bundle response mismatch")
            project_workspace_controls(core,controls); state.execute("INSERT OR REPLACE INTO origin_bindings VALUES (?,?,?,?,?)",(sid,origin,env["origin"],env["epoch"],item["cursor"]))
        state.commit(); core.execute("COMMIT"); return {r[0] for r in state.execute("SELECT origin FROM origin_bindings WHERE workspace=?",(sid,)).fetchall()}
    except BaseException: core.execute("ROLLBACK"); state.rollback(); raise
    finally: core.close()
def pull_row_replicas(cfg,state,root,ws,recover=None,origins=()):
    sid=ws["id"]; after=int((state.execute("SELECT value FROM meta WHERE key=?",(f"replica_cursor:{sid}",)).fetchone() or [0])[0]); total=0; controls=ws["controls"]+stored_controls(core_path(root),origins)
    while True:
        result=request(cfg,{"op":"replica_pull","workspace":sid,"after":after,"limit":500}); floor,tail=result["floor"],result["tail"]
        if not all(isinstance(v,int) and not isinstance(v,bool) and v>=0 for v in (floor,tail)) or floor>tail and tail or after>tail: raise ValueError("relay replica cursor window is invalid")
        bodies=[]; cursor=after
        for item in result["replicas"]:
            env=item["envelope"]
            if not isinstance(item["cursor"],int) or isinstance(item["cursor"],bool) or not cursor<item["cursor"]<=tail or env["workspace"]!=sid or not access_from(cfg,sid)<=env["epoch"]<=cfg["controls"][sid]["epoch"]: raise ValueError("row replica envelope response mismatch")
            cursor=item["cursor"]
            bodies.append(open_replica(env,key(cfg,sid,env["epoch"])))
        apply_row_replicas(core_path(root),bodies,sid,controls,recover,cfg["user"])
        for item in result["replicas"]:
            env=item["envelope"]; state.execute("INSERT OR REPLACE INTO replica_receipts VALUES (?,?,?,?)",(sid,env["replica"],env["epoch"],item["cursor"])); after=max(after,item["cursor"]); total+=1
        state.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",(f"replica_cursor:{sid}",str(after))); state.commit()
        if after>=tail: return total
        if not result["replicas"]: raise ValueError("relay replica tail cannot be reached")
def pull_blobs(cfg,state,root,ws):
    sid=ws["id"]; after=int((state.execute("SELECT value FROM meta WHERE key=?",(f"blob_cursor:{sid}",)).fetchone() or [0])[0]); total=0
    while True:
        result=request(cfg,{"op":"blob_pull","workspace":sid,"after":after,"limit":20}); floor,tail=result["floor"],result["tail"]
        if not all(isinstance(v,int) and not isinstance(v,bool) and v>=0 for v in (floor,tail)) or floor>tail and tail or after>tail: raise ValueError("relay blob cursor window is invalid")
        cursor=after
        for item in result["blobs"]:
            env=item["envelope"]
            if not isinstance(item["cursor"],int) or isinstance(item["cursor"],bool) or not cursor<item["cursor"]<=tail or env["workspace"]!=sid or not access_from(cfg,sid)<=env["epoch"]<=cfg["controls"][sid]["epoch"]: raise ValueError("blob replica envelope response mismatch")
            data,body_hash=open_blob(env,key(cfg,sid,env["epoch"])); project_attachment_body(core_path(root),data,body_hash); cursor=item["cursor"]; state.execute("INSERT OR REPLACE INTO blob_receipts VALUES (?,?,?,?)",(sid,env["blob"],env["epoch"],cursor)); after=cursor; total+=1
        state.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",(f"blob_cursor:{sid}",str(after))); state.commit()
        if after>=tail: return total
        if not result["blobs"]: raise ValueError("relay blob tail cannot be reached")
def pull(cfg,state,root=None):
    root=local_root(root)
    server=refresh(cfg,root)
    summary={}
    for ws in server["workspaces"]:
        if not ws["device_authorized"]: continue
        sid=ws["id"]; current=state.execute("SELECT lifecycle FROM sync_states WHERE workspace=?",(sid,)).fetchone(); lifecycle=current[0] if current else "rebaselining"; state.execute("INSERT OR REPLACE INTO sync_states VALUES (?,?,COALESCE((SELECT tail FROM sync_states WHERE workspace=?),0),COALESCE((SELECT floor FROM sync_states WHERE workspace=?),0),NULL)",(sid,lifecycle,sid,sid)); state.commit()
        records={r["device"]["id"]:r for control in ws["controls"] for r in control["devices"].values()}; devices={device:r["device"] for device,r in records.items()}; authors={device:r["user"] for device,r in records.items()}
        after=(state.execute("SELECT cursor FROM cursors WHERE workspace=?",(sid,)).fetchone() or [0])[0]; seen=(state.execute("SELECT value FROM meta WHERE key=?",(f"history_from:{sid}",)).fetchone() or [str(ws["history_from"])])[0]; earliest=min([k["epoch"] for k in ws["keys"]],default=ws["epoch"]); old_key=int((state.execute("SELECT value FROM meta WHERE key=?",(f"key_from:{sid}",)).fetchone() or [earliest])[0]); start=access_from(cfg,sid); candidates=[control["boundary"] for control in ws["controls"] if control["boundary"]["epoch"]==start]; boundary=candidates[0] if candidates and all(value==candidates[0] for value in candidates) else (_ for _ in ()).throw(ValueError("signed history boundary is ambiguous")); proof=digest(boundary); old_proof=(state.execute("SELECT value FROM meta WHERE key=?",(f"boundary:{sid}",)).fetchone() or [None])[0]; mode=(state.execute("SELECT value FROM meta WHERE key=?",(f"archive_mode:{sid}",)).fetchone() or [None])[0]; recover=mode if mode=="adopt" or ws["kind"]=="personal" and mode in ("native","import") else None
        deferred=state.execute("SELECT kind,payload_v,required FROM deferred_events WHERE workspace=?",(sid,)).fetchall(); reclassify=any(event_support(r)!=("required" if r["required"] else "optional") for r in deferred)
        if not current or ws["history_from"]<int(seen) or earliest<old_key or proof!=old_proof or reclassify: after=0; reset_history(state,sid,boundary); state.execute("INSERT OR REPLACE INTO meta VALUES (?,?),(?,?),(?,?)",(f"boundary:{sid}",proof,f"replica_cursor:{sid}","0",f"blob_cursor:{sid}","0")); state.commit()
        try:
            total=0; origins=pull_origins(cfg,state,root,ws); replicas=pull_row_replicas(cfg,state,root,ws,recover,origins); blobs=pull_blobs(cfg,state,root,ws)
            while True:
                result=request(cfg,{"op":"pull","workspace":sid,"after":after,"limit":500}); floor,tail=result["floor"],result["tail"]
                if not all(isinstance(v,int) and not isinstance(v,bool) and v>=0 for v in (floor,tail)) or floor>tail and tail or after>tail: raise ValueError("relay cursor window is invalid")
                incoming=[]
                for item in sorted(result["events"],key=lambda value:"purge" in value):
                    if cert:=item.get("purge"):
                        verify_purge(cert,signer(devices,cert["author"])); support=event_support({"kind":cert["event_kind"],"payload_v":cert["payload_v"]}); anchor=state.execute("SELECT * FROM receipts WHERE workspace=? AND event=?",(sid,cert["superseded_by"])).fetchone(); target=state.execute("SELECT * FROM receipts WHERE workspace=? AND event=?",(sid,cert["event"])).fetchone()
                        if cert["workspace"]!=sid or ws["kind"]!="personal" or support=="required" or not anchor or (anchor["author"],anchor["kind"],anchor["payload_v"],anchor["status"])!=(cert["author"],"memory.canonical",1,"deleted") or anchor["seq"]<=cert["seq"] or target and ((target["author"],target["epoch"],target["seq"],target["kind"],target["payload_v"],target["status"])!=(cert["author"],cert["epoch"],cert["seq"],cert["event_kind"],cert["payload_v"],"active") or target["entity"].rsplit(":part:",1)[0]!=anchor["entity"]): raise ValueError("invalid purge certificate")
                        sequence(state,sid,{"id":cert["event"],"author":cert["author"],"seq":cert["seq"],"parents":cert["parents"]}); drop_event(state,sid,cert["event"])
                        if cert["author"]==cfg["device"]["id"] and cert["seq"]>=int((state.execute("SELECT value FROM meta WHERE key=?",(f"seq:{sid}",)).fetchone() or ["0"])[0]): state.execute("INSERT OR REPLACE INTO meta VALUES (?,?),(?,?)",(f"seq:{sid}",str(cert["seq"]),f"prev:{sid}",cert["event"]))
                        after=max(after,item["cursor"]); continue
                    env=request(cfg,{"op":"fetch","workspace":sid,"event":item["event"]})["envelope"] if item.get("lazy") else item["envelope"]
                    if (env["workspace"],env["event"])!=(sid,item.get("event",env["event"])) or not access_from(cfg,sid)<=env["epoch"]<=cfg["controls"][sid]["epoch"]: raise ValueError("event envelope response mismatch")
                    value=open_event(env,key(cfg,sid,env["epoch"]),signer(devices,env["author"])); support=event_support(value); sequence(state,sid,value); receipt(state,sid,value,item["cursor"],env["epoch"])
                    if authors[value["author"]]==cfg["user"]:
                        state.execute("INSERT OR REPLACE INTO publication_heads VALUES (?,?,?,?,?)",(sid,cfg["user"],value["entity"],value["revision"],value["id"]))
                    if value["author"]==cfg["device"]["id"]:
                        if value["seq"]>=int((state.execute("SELECT value FROM meta WHERE key=?",(f"seq:{sid}",)).fetchone() or ["0"])[0]): state.execute("INSERT OR REPLACE INTO meta VALUES (?,?),(?,?)",(f"seq:{sid}",str(value["seq"]),f"prev:{sid}",value["id"]))
                    if support!="supported": state.execute("INSERT OR REPLACE INTO deferred_events VALUES (?,?,?,?,?,?)",(sid,value["id"],item["cursor"],value["kind"],value["payload_v"],support=="required"))
                    else: incoming.append((sid,value))
                    after=max(after,item["cursor"])
                project_many(core_path(root),state,incoming,cfg["device"]["id"],root,False,authors,recover,cfg["user"]); total+=len(result["events"]); state.execute("INSERT OR REPLACE INTO cursors VALUES (?,?)",(sid,after)); state.execute("INSERT OR REPLACE INTO meta VALUES (?,?),(?,?)",(f"history_from:{sid}",str(ws["history_from"]),f"key_from:{sid}",str(earliest))); state.commit()
                if after>=tail: break
                if not result["events"]: raise ValueError("relay tail cannot be reached")
            verify_history(state,sid,ws["controls"],start)
            if mode=="import": raise ValueError("archive rollback or replacement recovered additively; publication remains blocked")
            state.execute("INSERT OR REPLACE INTO sync_states VALUES (?,'ready',?,?,NULL)",(sid,tail,floor)); state.commit(); summary[sid]={"events":total,"replicas":replicas,"blobs":blobs,"cursor":after,"tail":tail,"floor":floor}
        except Exception as e:
            state.rollback(); state.execute("INSERT OR REPLACE INTO sync_states VALUES (?,'blocked',COALESCE((SELECT tail FROM sync_states WHERE workspace=?),0),COALESCE((SELECT floor FROM sync_states WHERE workspace=?),0),?)",(sid,sid,sid,str(e))); state.commit(); raise
    return summary
def fetch_lazy(cfg,state,event_id=None,root=None):
    root=local_root(root)
    server=refresh(cfg,root); controls={ws["id"]:ws["controls"] for ws in server["workspaces"]}; sql="SELECT workspace,event,cursor FROM lazy_events"+(" WHERE event=?" if event_id else ""); rows=[r for r in state.execute(sql,(event_id,) if event_id else ()).fetchall() if r[0] in controls]
    for ws,eid,cursor in rows:
        records={r["device"]["id"]:r for control in controls[ws] for r in control["devices"].values()}; devices={device:r["device"] for device,r in records.items()}; authors={device:r["user"] for device,r in records.items()}
        env=request(cfg,{"op":"fetch","workspace":ws,"event":eid})["envelope"]
        if (env["workspace"],env["event"])!=(ws,eid) or not access_from(cfg,ws)<=env["epoch"]<=cfg["controls"][ws]["epoch"]: raise ValueError("lazy event response mismatch")
        value=open_event(env,key(cfg,ws,env["epoch"]),signer(devices,env["author"])); support=event_support(value); sequence(state,ws,value); receipt(state,ws,value,cursor,env["epoch"]); support=="supported" and project(core_path(root),state,value,ws,cfg["device"]["id"],root=root,batch=True,authors=authors); support!="supported" and state.execute("INSERT OR REPLACE INTO deferred_events VALUES (?,?,?,?,?,?)",(ws,value["id"],cursor,value["kind"],value["payload_v"],support=="required")); state.execute("DELETE FROM lazy_events WHERE event=?",(eid,))
    state.commit(); return len(rows)
def sync_once(root=None,force=False):
    root=local_root(root)
    with sync_lock(root):
        cfg=load(root); _,_,state_path=paths(root); info=inspect_state(state_path,force); cutover=None
        relocate_attachments(core_path(root),paths(root)[0]/"attachments")
        if info["status"] in ("incompatible","invalid"): refresh(cfg,root); info["status"]=="incompatible" and rescue_bindings(cfg,state_path,root); cutover=cutover_state(state_path)
        state=connect(state_path)
        try:
            drain_hooks(); refresh(cfg,root); ready={r[0] for r in state.execute("SELECT workspace FROM sync_states WHERE lifecycle='ready'").fetchall()}; upload(cfg,state,root,ready); prepare_archive(cfg,state,root); pull(cfg,state,root); ready={r[0] for r in state.execute("SELECT workspace FROM sync_states WHERE lifecycle='ready'").fetchall()}; path=core_path(root); stamp=path.stat().st_mtime_ns if path.exists() else 0; active={w["id"] for w in cfg["server_state"]["workspaces"]}; scans=[(ws,meta) for ws,meta in cfg["workspaces"].items() if ws in ready and ws in active and f"{ws}:{meta['epoch']}" in cfg["keys"] and (force or stamp!=int((state.execute("SELECT value FROM meta WHERE key=?",(f"core_mtime:{ws}",)).fetchone() or ["0"])[0]))] if path.is_file() else []
            if scans:
                core=open_db(path,True); batches=[]
                for ws,meta in scans:
                    pol=state.execute("SELECT kind,value FROM policies WHERE workspace=?",(ws,)).fetchall(); repos=[p[1] for p in pol if p[0]=="repository"]; roots=[cfg.get("bindings",{}).get(f"{ws}:{p[1]}") for p in pol if p[0]=="path" and cfg.get("bindings",{}).get(f"{ws}:{p[1]}")]; records=[safe for r in scan(core,state,meta["kind"],repos,roots) if (safe:=protect_record(r,root,ws) if meta["kind"]=="team" else r) is not None]; batches.append((ws,records))
                core.close()
                for ws,records in batches:
                    bindings={r[0]:r[1] for r in state.execute("SELECT origin,epoch FROM origin_bindings WHERE workspace=?",(ws,)).fetchall()}; origins=set(bindings); attest_rows(path,cfg,ws,records,origins); keys={epoch:key(cfg,ws,epoch) for epoch in range(access_from(cfg,ws),cfg["workspaces"][ws]["epoch"]+1) if f"{ws}:{epoch}" in cfg["keys"]}; known={r[0] for r in state.execute("SELECT replica FROM replica_receipts WHERE workspace=? UNION SELECT replica FROM replica_outbox WHERE workspace=?",(ws,ws)).fetchall()}; reconcile_replicas(cfg,state,root,ws,row_replicas(path,cfg,ws,records,keys,known,origins,bindings)); known_blobs={r[0] for r in state.execute("SELECT blob FROM blob_receipts WHERE workspace=? UNION SELECT blob FROM blob_outbox WHERE workspace=?",(ws,ws)).fetchall()}; reconcile_blobs(cfg,state,root,ws,blob_replicas(path,cfg,ws,records,keys,known_blobs,origins,bindings)); heads={r[0]:r[1] for r in state.execute("SELECT entity,revision FROM publication_heads WHERE workspace=? AND owner=?",(ws,cfg["user"])).fetchall()}; [publish(cfg,state,ws,r,root,True,heads) for r in records if r["kind"] not in SIGNED]
                final=path.stat().st_mtime_ns; [state.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",(f"core_mtime:{ws}",str(final))) for ws,meta in scans]
            [publish(cfg,state,ws,r,root,True,heads) for ws,meta in cfg["workspaces"].items() if ws in ready and ws in active and meta["kind"]=="personal" and f"{ws}:{meta['epoch']}" in cfg["keys"] for heads in [{r[0]:r[1] for r in state.execute("SELECT entity,revision FROM publication_heads WHERE workspace=? AND owner=?",(ws,cfg["user"])).fetchall()}] for r in bridge_records(root,state,ws,meta["kind"])]; state.commit(); upload(cfg,state,root,ready)
            for ws,meta in cfg["workspaces"].items():
                if ws in ready and ws in active and meta["kind"]=="personal": purge_events(cfg,state,ws,bridge_purges(root,state,ws,meta["kind"]))
            pull(cfg,state,root); remember_archive(cfg,state,root); state.execute("INSERT OR REPLACE INTO meta VALUES ('last_sync',?)",(str(time.time()),)); state.commit()
        finally: state.close()
        return cutover
def bind_origin(cfg,state,ws,origin,root=None):
    root=local_root(root); controls=control_chain(stored_controls(core_path(root),{origin})); current=cfg["workspaces"][ws]["epoch"]
    db=open_db(core_path(root),True); exists=db.execute("SELECT 1 FROM remote.row_proofs WHERE workspace_id=?",(origin,)).fetchone(); db.close()
    if origin==ws or not exists: raise ValueError("origin has no retained row proofs")
    env=seal_origin(controls,ws,current,key(cfg,ws,current),cfg["device"]["id"]); ack=request(cfg,{"op":"origin_upload","envelope":env}); isinstance(ack.get("cursor"),int) and not isinstance(ack["cursor"],bool) or (_ for _ in ()).throw(ValueError("origin upload acknowledgement mismatch")); state.execute("INSERT OR REPLACE INTO origin_bindings VALUES (?,?,?,?,?)",(ws,origin,env["origin"],current,ack["cursor"])); state.execute("DELETE FROM meta WHERE key=?",(f"core_mtime:{ws}",)); state.commit(); return len(controls)
def add_member(cfg,ws,user,remove=False,root=None):
    refresh(cfg,root); members={u:m["role"] for u,m in cfg["controls"][ws]["members"].items()}; devices=[]
    if remove:
        found=request(cfg,{"op":"directory","user":user}); target=directory_user(found,user) if found["users"] else {"id":user}; members.pop(target["id"],None)
    else:
        found=request(cfg,{"op":"directory","user":user}); trusted(found["devices"]); target=directory_user(found,user); members[target["id"]]="member"; devices+=found["devices"]
    return rotate(cfg,ws,members,[d for d in {d["id"]:d for d in devices}.values() if d["user_id"] in members],root=root)
def grant_all(cfg,ws,user,root=None):
    found=request(cfg,{"op":"directory","user":user}); target=directory_user(found,user); refresh(cfg,root); previous=cfg["controls"][ws]; devices=[r["device"] for r in previous["devices"].values() if r["user"]==target["id"]]; envelopes={}
    for name,value in cfg["keys"].items():
        if name.startswith(ws+":"):
            epoch=int(name.rsplit(":",1)[1]); envelopes[str(epoch)]={d["id"]:seal_key(unb64(value),d["box_public"],f"workspace:{ws}:epoch:{epoch}") for d in devices}
    members={**previous["members"],target["id"]:{**previous["members"][target["id"]],"history_from":1}}; records={d:{**r,"history":True} if r["user"]==target["id"] else r for d,r in previous["devices"].items()}; control=control_body(cfg,previous,key(cfg,ws,previous["epoch"]),"history",members,records); request(cfg,sign_control(cfg["device"],{"op":"grant_all","workspace":ws,"user":target["id"],"control":control,"envelopes":envelopes})); cfg["controls"][ws]=control; save(cfg,root); return len(envelopes)
def remove_device(cfg,ws,device_id,root=None):
    refresh(cfg,root); members={u:m["role"] for u,m in cfg["controls"][ws]["members"].items()}; return rotate(cfg,ws,members,[],[device_id],root)
def request_device(cfg,ws,root=None,delay=3600):
    refresh(cfg,root); base=cfg["controls"][ws]
    if cfg["user"] not in base["members"] or cfg["device"]["id"] in base["devices"] or cfg["device"]["id"] in base["removed"]: raise ValueError("device is not pending for this workspace")
    target=own_record(cfg,False); voters=electorate(base,cfg["user"]); not_before=time.time()+delay if len(voters)==1 and not any(d["user"]==cfg["user"] for d in base["devices"].values()) else time.time(); value=device_proposal(cfg["device"],ws,base,target,time.time()+86400,not_before); request(cfg,{"op":"propose","proposal":value}); return value
def proposals(cfg,ws): return request(cfg,{"op":"proposals","workspace":ws})["proposals"]
def pending(cfg,ws,device_id,kind):
    base=cfg["controls"][ws]; found=[p for p in proposals(cfg,ws) if p["proposal"]["kind"]==kind and p["proposal"]["target"]["device"]["id"]==device_id and p["proposal"]["base"]==state_hash(base)]
    if len(found)!=1: raise ValueError("pending device proposal not found")
    return base,found[0]
def approve_device(cfg,ws,device_id,approve=True,root=None):
    refresh(cfg,root); base,item=pending(cfg,ws,device_id,"device.proposal"); target=item["proposal"]["target"]; same=cfg["user"]==target["user"] and cfg["device"]["id"] in base["devices"]
    if same and not approve:
        request(cfg,sign_control(cfg["device"],{"op":"reject","workspace":ws,"proposal":state_hash(item["proposal"])})); return {"approved":False,"rejected":True}
    if same: verify_proposal(base,item["proposal"])
    if not same:
        request(cfg,{"op":"vote","vote":device_vote(cfg["device"],cfg["user"],item["proposal"],approve)}); item=next(p for p in proposals(cfg,ws) if state_hash(p["proposal"])==state_hash(item["proposal"]))
        yes=len({v["voter"] for v in item["votes"] if v["approve"]}); needed=len(electorate(base,target["user"]))//2+1
        if not approve or yes<needed: return {"approved":False,"votes":yes,"needed":needed}
        approved(base,item["proposal"],item["votes"])
    new=os.urandom(32); epoch=base["epoch"]+1; boundary=next_boundary(cfg,ws,root); inherit=base["devices"][cfg["device"]["id"]]["history"] if same else False; entry={**target,"history":inherit}; records={**base["devices"],device_id:entry}; action="self_approve" if same else "quorum_approve"; proof={"proposal":item["proposal"],"votes":item["votes"]}; control=control_body(cfg,base,new,action,devices=records,approval=proof,boundary=boundary); envs={d:seal_key(new,r["device"]["box_public"],f"workspace:{ws}:epoch:{epoch}") for d,r in records.items()}; start=base["members"][target["user"]]["history_from"]; history={name.rsplit(":",1)[1]:seal_key(unb64(value),entry["device"]["box_public"],f"workspace:{ws}:epoch:{name.rsplit(':',1)[1]}") for name,value in cfg["keys"].items() if inherit and name.startswith(ws+":") and int(name.rsplit(":",1)[1])>=start}
    body={"op":"rotate","workspace":ws,"control":control,"envelopes":envs}; history and body.update(history_envelopes={device_id:history})
    request(cfg,sign_control(cfg["device"],body)); cfg["keys"][f"{ws}:{epoch}"]=b64(new); cfg["workspaces"][ws]["epoch"]=epoch; cfg["controls"][ws]=control; update_recovery(cfg,root); control_event(cfg,ws,action,device_id,root); return {"approved":True,"epoch":epoch,"history":len(history)}
def request_history(cfg,ws,root=None,delay=3600):
    refresh(cfg,root); base=cfg["controls"][ws]; current=base["devices"].get(cfg["device"]["id"])
    if not current or current["history"]: raise ValueError("device does not need history approval")
    voters=electorate(base,cfg["user"]); value=device_proposal(cfg["device"],ws,base,{**current,"history":True},time.time()+86400,time.time()+delay if len(voters)==1 else time.time(),kind="history.proposal"); request(cfg,{"op":"propose","proposal":value}); return value
def approve_history(cfg,ws,device_id,approve=True,root=None):
    refresh(cfg,root); base,item=pending(cfg,ws,device_id,"history.proposal"); target=item["proposal"]["target"]; request(cfg,{"op":"vote","vote":device_vote(cfg["device"],cfg["user"],item["proposal"],approve)}); item=next(p for p in proposals(cfg,ws) if state_hash(p["proposal"])==state_hash(item["proposal"])); yes=len({v["voter"] for v in item["votes"] if v["approve"]}); needed=len(electorate(base,target["user"]))//2+1
    if not approve or yes<needed: return {"approved":False,"votes":yes,"needed":needed}
    approved(base,item["proposal"],item["votes"],kind="history.proposal"); device=target["device"]["id"]; records={**base["devices"],device:{**base["devices"][device],"history":True}}; proof={"proposal":item["proposal"],"votes":item["votes"]}; control=control_body(cfg,base,key(cfg,ws,base["epoch"]),"history_activate",devices=records,approval=proof); start=base["members"][target["user"]]["history_from"]; envs={str(epoch):seal_key(key(cfg,ws,epoch),target["device"]["box_public"],f"workspace:{ws}:epoch:{epoch}") for epoch in range(start,base["epoch"]+1)}
    request(cfg,sign_control(cfg["device"],{"op":"history_activate","workspace":ws,"control":control,"envelopes":envs})); cfg["controls"][ws]=control; save(cfg,root); control_event(cfg,ws,"history_activate",device,root); return {"approved":True,"history":len(envs)}

@remote.command("setup")
def setup_cmd(url:str,user:str,device:str=typer.Option("computer","--device")): cfg,recovery=setup_client(url,user,device); typer.echo(f"Personal workspace ready. User ID: {cfg['user']}. Recovery key (store offline): {recovery}")
@remote.command("recover")
def recover_cmd(url:str,user:str,device:str=typer.Option("computer","--device"),recovery:Optional[str]=typer.Option(None,"--recovery")): setup_client(url,user,device,recovery or typer.prompt("Recovery key",hide_input=True)); typer.echo("Device enrolled and keys rotated")
@remote.command("rehome")
def rehome_cmd(url:str): cfg,recovery=rehome_client(load(),url); typer.echo(f"Fresh relay ready. User ID retained: {cfg['user']}. New recovery key (store offline): {recovery}")
@remote.command("workspace")
def workspace_cmd(name:str): cfg=load(); typer.echo(create(cfg,name,"team"))
@remote.command("invite")
def invite_cmd(space:str,user:str): cfg=load(); typer.echo(f"epoch {add_member(cfg,workspace(cfg,space),user)}")
@remote.command("remove")
def remove_cmd(space:str,user:str): cfg=load(); typer.echo(f"epoch {add_member(cfg,workspace(cfg,space),user,True)}")
@remote.command("grant-all")
def grant_all_cmd(space:str,user:str): cfg=load(); typer.echo(f"Granted {grant_all(cfg,workspace(cfg,space),user)} epochs")
@remote.command("refound")
def refound_cmd(space:str,origin_workspace_id:str): cfg=load(); state=connect(paths()[2]); count=bind_origin(cfg,state,workspace(cfg,space),origin_workspace_id); state.close(); sync_once(force=True); typer.echo(f"Bound {count} signed origin controls")
@remote.command("origins")
def origins_cmd(): db=open_db(core_path(),True); typer.echo(json.dumps([{"workspace":r[0],"rows":r[1]} for r in db.execute("SELECT workspace_id,COUNT(*) FROM remote.row_proofs GROUP BY workspace_id ORDER BY workspace_id").fetchall()])); db.close()
@remote.command("remove-device")
def remove_device_cmd(space:str,device_id:str): cfg=load(); typer.echo(f"epoch {remove_device(cfg,workspace(cfg,space),device_id)}")
@remote.command("request-device")
def request_device_cmd(space:str): cfg=load(); value=request_device(cfg,workspace(cfg,space)); typer.echo(f"Pending device {value['target']['device']['id']}; certificate {value['certificate_hash']}")
@remote.command("approve-device")
def approve_device_cmd(space:str,device_id:str,reject:bool=typer.Option(False,"--reject")): cfg=load(); typer.echo(json.dumps(approve_device(cfg,workspace(cfg,space),device_id,not reject)))
@remote.command("approvals")
def approvals_cmd(space:str): cfg=load(); typer.echo(json.dumps(proposals(cfg,workspace(cfg,space))))
@remote.command("request-history")
def request_history_cmd(space:str): cfg=load(); value=request_history(cfg,workspace(cfg,space)); typer.echo(f"Pending history approval for {value['target']['device']['id']}")
@remote.command("approve-history")
def approve_history_cmd(space:str,device_id:str,reject:bool=typer.Option(False,"--reject")): cfg=load(); typer.echo(json.dumps(approve_history(cfg,workspace(cfg,space),device_id,not reject)))
@remote.command("link")
def link_cmd(path:Path,space:str):
    cfg=load(); ws=workspace(cfg,space); state=connect(paths()[2]); resolved=path.resolve(); repo=repository(resolved); kind,value=("repository",repo["id"]) if repo else ("path",digest(os.urandom(32))[:32]); state.execute("INSERT OR REPLACE INTO policies VALUES (?,?,?)",(ws,kind,value)); state.execute("DELETE FROM meta WHERE key LIKE 'core_mtime:%'"); state.commit(); kind=="path" and cfg.setdefault("bindings",{}).update({f"{ws}:{value}":str(resolved)}); save(cfg)
    publish(cfg,state,ws,{"kind":"workspace.policy","entity":f"policy:{kind}:{value}","payload":{"kind":kind,"value":value}}); upload(cfg,state); typer.echo(f"{kind} {value} -> {cfg['workspaces'][ws]['name']}")
@remote.command("sync")
def sync_cmd(): result=sync_once(force=True); typer.echo("Remote synchronized"+(f"; previous state preserved at {result['backup']}" if result else ""))
@remote.command("fetch")
def fetch_cmd(event_id:Optional[str]=None): typer.echo(f"Fetched {fetch_lazy(load(),connect(paths()[2]),event_id)} lazy events")
@remote.command("watch")
def watch(interval:int=typer.Option(2,"--interval")):
    while True:
        try: sync_once()
        except Exception as e: paths()[0].mkdir(parents=True,exist_ok=True); (paths()[0]/"last_error").write_text(str(e))
        time.sleep(interval)
@remote.command("enable")
def enable_cmd(remove:bool=typer.Option(False,"--remove")): not remove and install_hooks(False,False); typer.echo(enable(paths()[0],remove))
def doctor_status():
    try:
        cfg=load(); info=inspect_state(paths()[2],True)
        try: online="reachable" if health(cfg)["ok"] else "error"
        except Exception: online="error"
        if info["status"]!="current": return f"remote: {online}, state={info['status']}, schema={info['version'] or 'unknown'}, next_sync={'backup+rebaseline' if info['status'] in ('incompatible','invalid') else 'initialize'}"
        state=read_state(paths()[2])
        try: pending=state.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]; lazy=state.execute("SELECT COUNT(*) FROM lazy_events").fetchone()[0]; deferred,required=state.execute("SELECT COUNT(*),COALESCE(SUM(required),0) FROM deferred_events").fetchone(); lifecycle=",".join(f"{r[0][:8]}:{r[1]}" for r in state.execute("SELECT workspace,lifecycle FROM sync_states ORDER BY workspace").fetchall()) or "uninitialized"; last=(state.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone() or ["never"])[0]; backup=(state.execute("SELECT value FROM meta WHERE key='state_cutover'").fetchone() or [None])[0]
        finally: state.close()
        return f"remote: {online}, user={cfg['user'][:8]}, device={cfg['device']['id'][:8]}, workspaces={len(cfg['workspaces'])}, epochs={len(cfg['keys'])}, lifecycle={lifecycle}, pending={pending}, lazy={lazy}, deferred={deferred}, required={required}, last={last}"+(f", backup={json.loads(backup)['backup']}" if backup else "")
    except Exception as e: return f"remote: unavailable ({e})"
@remote.command("doctor")
def doctor_cmd(): typer.echo(doctor_status())
@remote.command("graph")
def graph_cmd(view:str,arg:Optional[str]=None): typer.echo(json.dumps(graph_query(core_path(),view,arg),default=str))
[register(app) for app in _pending]; _pending.clear()
