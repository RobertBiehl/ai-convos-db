"""Client-side enrollment, E2EE keyring, automatic sync, membership, and local queries."""
import json, os, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path; from typing import Optional

import typer
_pending=[]
def register(app): _pending.append(app) if "remote" not in globals() else app.add_typer(remote,name="remote")
from ai_convos.cli import DB_PATH, PROJECT_ROOT, drain_hooks, get_db, install_hooks
from .control import approved, electorate, proposal as device_proposal, record as control_record, sign as control_sign, state_hash, verify_state, vote as device_vote
from .provenance import repository
from .projection import connect, project, project_many, query as graph_query, rebuild as rebuild_projection, scan, sequence
from .protocol import (b64, certificate, digest, event, identity, material_event, open_event, open_key, public, public_id, recover,
                       recovery_bundle, seal_event, seal_history, seal_key, sign_control, signer, unb64, verify_certificate)
from .service import edit_hooks, enable

remote=typer.Typer(help="End-to-end encrypted personal and team synchronization")
def paths(root=None):
    base=Path(root or os.environ.get("CONVOS_PROJECT_ROOT",PROJECT_ROOT))/"remote"; return base,base/"config.json",base/"state.db"
def core_path(root=None): return Path(root or os.environ.get("CONVOS_PROJECT_ROOT",PROJECT_ROOT))/"data"/"convos.db"
def load(root=None):
    _,path,_=paths(root)
    if not path.exists(): raise ValueError("Remote is not configured. Run `convos remote setup`.")
    return json.loads(path.read_text())
def save(cfg,root=None):
    base,path,_=paths(root); base.mkdir(parents=True,exist_ok=True); os.chmod(base,0o700); tmp=path.with_name(f".{path.name}.{os.getpid()}"); tmp.write_text(json.dumps(cfg)); os.chmod(tmp,0o600); os.replace(tmp,path)
def safe_url(url): parsed=urllib.parse.urlparse(url); parsed.scheme=="https" or parsed.hostname in ("127.0.0.1","localhost","::1") or os.environ.get("CONVOS_REMOTE_INSECURE")=="1" or (_ for _ in ()).throw(ValueError("Remote URL must use HTTPS (set CONVOS_REMOTE_INSECURE=1 only on a trusted test network)"))
def request(cfg,body,auth=True):
    safe_url(cfg["url"])
    headers={"Content-Type":"application/json"};
    if auth: headers["Authorization"]="Bearer "+cfg["token"]
    req=urllib.request.Request(cfg["url"].rstrip("/")+"/v1",data=json.dumps(body).encode(),headers=headers,method="POST")
    try: return json.loads(urllib.request.urlopen(req,timeout=10).read())
    except urllib.error.HTTPError as e: raise ValueError(json.loads(e.read())["error"]) from e
def health(cfg): safe_url(cfg["url"]); return json.loads(urllib.request.urlopen(cfg["url"].rstrip("/")+"/v1/health",timeout=3).read())
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
def control_body(cfg,previous,key_,action,members=None,devices=None,removed=None,approval=None):
    return control_sign(cfg["device"],{"v":1,"kind":"workspace.state","workspace":previous["workspace"],"scope":previous["scope"],"revision":previous["revision"]+1,"prev":state_hash(previous),"epoch":previous["epoch"]+(action not in ("history","history_activate")),"key_commitment":digest(key_),"members":members or previous["members"],"devices":devices or previous["devices"],"removed":removed if removed is not None else previous["removed"],"action":action,"approval":approval,"approved_at":time.time()})
def directory_user(found,user): field="id" if len(user)==32 and all(c in "0123456789abcdef" for c in user) else "name"; values=[v for v in found["users"] if v[field]==user]; return values[0] if len(values)==1 and public_id(values[0]["root_public"])==values[0]["id"] else (_ for _ in ()).throw(ValueError("directory user mismatch"))
def update_recovery(cfg,root=None):
    personal={ws for ws,v in cfg["workspaces"].items() if v["kind"]=="personal"}; keys={name:value for name,value in cfg["keys"].items() if name.rsplit(":",1)[0] in personal}; _,bundle=recovery_bundle({"root":cfg["root"],"keys":keys,"workspaces":cfg["workspaces"],"controls":{ws:v for ws,v in cfg["controls"].items() if ws in personal}},unb64(cfg["recovery"])); request(cfg,sign_control(cfg["device"],{"op":"recovery","bundle":bundle})); save(cfg,root)
def refresh(cfg,root=None):
    state=request(cfg,{"op":"state"}); missing={d["id"]:d for w in state["workspaces"] for d in w["devices"] if d["user_id"]==cfg["user"] and not d["certificate"]}; [request(cfg,{"op":"certify","certificate":certificate(cfg["root"],cfg["user"],d)}) for d in missing.values()]; state=request(cfg,{"op":"state"}) if missing else state; cfg["server_state"]=state; changed=False
    for ws in state["workspaces"]:
        previous=None
        for value in ws["controls"]: verify_state(value,previous); previous=value
        pinned=cfg["controls"].get(ws["id"])
        if pinned and (not previous or pinned["revision"]>previous["revision"] or state_hash(pinned) not in {state_hash(v) for v in ws["controls"]}): raise ValueError("workspace control rollback or fork")
        if previous: cfg["controls"][ws["id"]]=previous
        cfg["workspaces"].setdefault(ws["id"],{"name":ws["id"][:8],"kind":ws["kind"],"epoch":ws["epoch"]}); cfg["workspaces"][ws["id"]]["epoch"]=ws["epoch"]
        for wrapped in ws["keys"]:
            name=f"{ws['id']}:{wrapped['epoch']}"
            if name not in cfg["keys"]:
                opened=open_key(json.loads(wrapped["envelope"]),cfg["device"]["box_private"],f"workspace:{ws['id']}:epoch:{wrapped['epoch']}"); controls=[v for v in ws["controls"] if v["epoch"]==wrapped["epoch"]]
                if controls and digest(opened)!=controls[-1]["key_commitment"]: raise ValueError("workspace epoch key commitment mismatch")
                cfg["keys"][name]=b64(opened); changed=True
    save(cfg,root)
    if changed: update_recovery(cfg,root)
    return state
def create(cfg,name,kind="team",root=None):
    ws,key_=digest(os.urandom(32))[:32],os.urandom(32); entry=own_record(cfg); control=control_sign(cfg["device"],{"v":1,"kind":"workspace.state","workspace":ws,"scope":kind,"revision":1,"prev":None,"epoch":1,"key_commitment":digest(key_),"members":{cfg["user"]:{"role":"admin","joined":1,"history_from":1,"selected":[]}},"devices":{cfg["device"]["id"]:entry},"removed":[],"action":"create","approval":None,"approved_at":time.time()}); env=seal_key(key_,cfg["device"]["box_public"],f"workspace:{ws}:epoch:1"); request(cfg,sign_control(cfg["device"],{"op":"create","workspace":ws,"kind":kind,"control":control,"envelopes":{cfg["device"]["id"]:env}})); cfg["workspaces"][ws]={"name":name,"kind":kind,"epoch":1}; cfg["keys"][f"{ws}:1"]=b64(key_); cfg["controls"][ws]=control; update_recovery(cfg,root); membership_event(cfg,ws,1,{cfg["user"]:"admin"},root); return ws
def setup_client(url,user,device="computer",recovery=None,root=None):
    if recovery:
        bundle=request({"url":url},{"op":"recovery_fetch","user":user},False)["bundle"]; recovered=recover(bundle,recovery); root_id=recovered["root"]; keys,workspaces,controls=recovered["keys"],recovered["workspaces"],recovered.get("controls",{})
    else: root_id,keys,workspaces,controls=identity(user+" root"),{},{},{}
    dev=identity(device); uid=root_id["id"]
    if not recovery: recovery,bundle=recovery_bundle({"root":root_id,"keys":keys,"workspaces":workspaces})
    registered=request({"url":url},{"op":"register","user_name":user,"root_public":root_id["sign_public"],"certificate":certificate(root_id,uid,dev),**({"recovery":bundle} if not workspaces else {})},False)
    cfg={"url":url,"user":uid,"token":registered["token"],"root":root_id,"device":dev,"recovery":recovery,"keys":keys,"workspaces":workspaces,"controls":controls,"server_state":{}}; save(cfg,root)
    if not workspaces: create(cfg,"Personal","personal",root)
    else:
        state=refresh(cfg,root)
        for ws in [w for w in state["workspaces"] if w["role"]=="admin" and w["kind"]=="personal"]:
            rotate(cfg,ws["id"],{m["user_id"]:m["role"] for m in ws["members"] if m["active"]},[d for d in ws["devices"] if d["active"] and d.get("allowed",1)],root=root)
            if ws["kind"]=="personal": grant_all(cfg,ws["id"],uid,root)
    return cfg,recovery
def rotate(cfg,ws,members,devices,deactivate=(),root=None):
    state=refresh(cfg,root); previous=cfg["controls"][ws]; epoch=previous["epoch"]+1; new=os.urandom(32); devices=trusted(devices); old=previous["members"]; meta={u:old.get(u,{"joined":epoch,"history_from":epoch,"selected":[]})|{"role":role} for u,role in members.items()}; removed=sorted(set(previous["removed"])|set(deactivate)|{d for d,r in previous["devices"].items() if r["user"] not in members}); records={d:r for d,r in previous["devices"].items() if r["user"] in members and d not in deactivate}
    if cfg["device"]["id"] not in previous["devices"] and previous["scope"]=="personal":
        entry=own_record(cfg); req=device_proposal(cfg["device"],ws,previous,{**entry,"history":True},time.time()+300); records|={cfg["device"]["id"]:{**entry,"history":True}}; action,approval="personal_recover",{"proposal":req,"votes":[]}
    else:
        action,approval=("remove",None) if deactivate else ("membership",None); new_users=set(members)-set(old)
        records|={d["id"]:server_record(d) for d in devices if d["user_id"] in new_users and d["id"] not in removed and d.get("active",1) and d.get("allowed",1)}
    control=control_body(cfg,previous,new,action,meta,records,removed,approval); envs={d:seal_key(new,r["device"]["box_public"],f"workspace:{ws}:epoch:{epoch}") for d,r in records.items()}; request(cfg,sign_control(cfg["device"],{"op":"rotate","workspace":ws,"control":control,"envelopes":envs})); cfg["keys"][f"{ws}:{epoch}"]=b64(new); cfg["workspaces"][ws]["epoch"]=epoch; cfg["controls"][ws]=control; update_recovery(cfg,root); cfg["device"]["id"] in records and membership_event(cfg,ws,epoch,members,root); return epoch
def publish(cfg,state,ws,record,root=None,defer=False,known=None):
    revision=digest(record["payload"]); old=(record["entity"],revision) in known if known is not None else state.execute("SELECT event FROM published WHERE workspace=? AND entity=? AND revision=?",(ws,record["entity"],revision)).fetchone()
    if old: return old[0] if known is None else None
    seq=int((state.execute("SELECT value FROM meta WHERE key=?",(f"seq:{ws}",)).fetchone() or ["0"])[0])+1; prev=(state.execute("SELECT value FROM meta WHERE key=?",(f"prev:{ws}",)).fetchone() or [None])[0]; value=event(cfg["device"],seq,record["kind"],record["entity"],record["payload"],[prev] if prev else (),record.get("observed_at")); epoch=cfg["workspaces"][ws]["epoch"]; env=seal_event(value,ws,epoch,key(cfg,ws,epoch))
    state.execute("INSERT INTO event_log VALUES (?,?,?,?,?,?)",(ws,value["id"],0,"out",json.dumps(value),json.dumps(env))); state.execute("INSERT INTO published VALUES (?,?,?,?)",(ws,record["entity"],revision,value["id"])); state.execute("INSERT OR REPLACE INTO meta VALUES (?,?),(?,?)",(f"seq:{ws}",str(seq),f"prev:{ws}",value["id"])); sequence(state,ws,value)
    if not defer: state.commit()
    if known is not None: known.add((record["entity"],revision))
    project(core_path(root),state,value,ws,cfg["device"]["id"]); return value["id"]
def membership_event(cfg,ws,epoch,members,root=None):
    state=connect(paths(root)[2]); publish(cfg,state,ws,{"kind":"workspace.membership","entity":f"membership:{epoch}","payload":{"epoch":epoch,"members":members}},root); upload(cfg,state,root); state.close()
def control_event(cfg,ws,action,target,root=None):
    state=connect(paths(root)[2]); value=cfg["controls"][ws]; publish(cfg,state,ws,{"kind":"workspace.device","entity":f"device:{target}:{value['revision']}","payload":{"action":action,"device":target,"revision":value["revision"],"state":state_hash(value)}},root); upload(cfg,state,root); state.close()
def _upload_batches(rows,limit=8*1024*1024):
    batch,size=[],0
    for row in rows:
        if batch and (len(batch)==500 or size+len(row[3])>limit): yield batch; batch,size=[],0
        batch.append(row); size+=len(row[3])
    if batch: yield batch
def upload(cfg,state,root=None):
    refresh(cfg,root)
    rows=state.execute("SELECT workspace,event,event_json,envelope FROM event_log WHERE direction='out' AND cursor=0 ORDER BY rowid").fetchall()
    for batch in _upload_batches(rows):
        envs=[]
        for ws,eid,raw,wrapped in batch:
            current=cfg["workspaces"][ws]["epoch"]; env=json.loads(wrapped)
            if env["epoch"]!=current: env=seal_event(json.loads(raw),ws,current,key(cfg,ws,current)); state.execute("UPDATE event_log SET envelope=? WHERE event=?",(json.dumps(env),eid))
            envs.append(env)
        result=request(cfg,{"op":"upload_many","envelopes":envs})["events"]; [state.execute("UPDATE event_log SET cursor=?,envelope=NULL WHERE event=?",(r["cursor"],batch[i][1])) for i,r in enumerate(result)]; state.commit()
def pull(cfg,state,root=None):
    server=refresh(cfg,root); devices={r["device"]["id"]:r["device"] for ws in server["workspaces"] for control in ws["controls"] for r in control["devices"].values()}
    for ws in server["workspaces"]:
        if not ws["device_authorized"]: continue
        after=(state.execute("SELECT cursor FROM cursors WHERE workspace=?",(ws["id"],)).fetchone() or [0])[0]; seen=(state.execute("SELECT value FROM meta WHERE key=?",(f"history_from:{ws['id']}",)).fetchone() or [str(ws["history_from"])])[0]; earliest=min([k["epoch"] for k in ws["keys"]],default=ws["epoch"]); old_key=int((state.execute("SELECT value FROM meta WHERE key=?",(f"key_from:{ws['id']}",)).fetchone() or [earliest])[0])
        if ws["history_from"]<int(seen) or earliest<old_key: after=0
        result=request(cfg,{"op":"pull","workspace":ws["id"],"after":after,"limit":500}); incoming=[]
        for item in result["events"]:
            if item.get("lazy"):
                state.execute("INSERT OR IGNORE INTO lazy_events VALUES (?,?,?,?)",(ws["id"],item["event"],item["cursor"],item["size"])); after=max(after,item["cursor"]); continue
            env=item["envelope"]; value=open_event(env,key(cfg,ws["id"],env["epoch"]),signer(devices,env["author"])); material=material_event(value,devices,cfg["device"]); sequence(state,ws["id"],value); state.execute("INSERT OR IGNORE INTO event_log VALUES (?,?,?,?,?,NULL)",(ws["id"],value["id"],item["cursor"],"in",json.dumps(value)))
            if material: incoming.append((ws["id"],material))
            after=max(after,item["cursor"])
        project_many(core_path(root),state,incoming,cfg["device"]["id"])
        state.execute("INSERT OR REPLACE INTO cursors VALUES (?,?)",(ws["id"],after)); state.execute("INSERT OR REPLACE INTO meta VALUES (?,?),(?,?)",(f"history_from:{ws['id']}",str(ws["history_from"]),f"key_from:{ws['id']}",str(earliest))); state.commit()
def fetch_lazy(cfg,state,event_id=None,root=None):
    server=refresh(cfg,root); devices={r["device"]["id"]:r["device"] for ws in server["workspaces"] for control in ws["controls"] for r in control["devices"].values()}; sql="SELECT workspace,event,cursor FROM lazy_events"+(" WHERE event=?" if event_id else ""); rows=state.execute(sql,(event_id,) if event_id else ()).fetchall()
    for ws,eid,cursor in rows:
        env=request(cfg,{"op":"fetch","workspace":ws,"event":eid})["envelope"]
        if (env["workspace"],env["event"])!=(ws,eid): raise ValueError("lazy event response mismatch")
        value=open_event(env,key(cfg,ws,env["epoch"]),signer(devices,env["author"])); material=material_event(value,devices,cfg["device"]); sequence(state,ws,value); state.execute("INSERT OR IGNORE INTO event_log VALUES (?,?,?,?,?,NULL)",(ws,eid,cursor,"in",json.dumps(value))); material and project(core_path(root),state,material,ws,cfg["device"]["id"]); state.execute("DELETE FROM lazy_events WHERE event=?",(eid,))
    state.commit(); return len(rows)
def sync_once(root=None,force=False):
    cfg=load(root); _,_,state_path=paths(root); state=connect(state_path); drain_hooks(); upload(cfg,state,root); core=get_db(read_only=True)
    stamp=core_path(root).stat().st_mtime_ns if core_path(root).exists() else 0; previous=int((state.execute("SELECT value FROM meta WHERE key='core_mtime'").fetchone() or ["0"])[0])
    if core and (force or stamp!=previous):
        for ws,meta in cfg["workspaces"].items():
            if f"{ws}:{meta['epoch']}" not in cfg["keys"]: continue
            pol=state.execute("SELECT kind,value,local_root FROM policies WHERE workspace=?",(ws,)).fetchall(); repos=[p[1] for p in pol if p[0]=="repository"]; roots=[p[2] for p in pol if p[0]=="path" and p[2]]
            known={(r[0],r[1]) for r in state.execute("SELECT entity,revision FROM published WHERE workspace=?",(ws,)).fetchall()}; [publish(cfg,state,ws,r,root,True,known) for r in scan(core,state,cfg["device"]["id"],meta["kind"],repos,roots)]
        state.execute("INSERT OR REPLACE INTO meta VALUES ('core_mtime',?)",(str(stamp),)); state.commit()
    if core: core.close()
    upload(cfg,state,root); pull(cfg,state,root); state.execute("INSERT OR REPLACE INTO meta VALUES ('last_sync',?)",(str(time.time()),)); state.commit(); state.close()
def add_member(cfg,ws,user,remove=False,root=None):
    state=refresh(cfg,root); w=next(x for x in state["workspaces"] if x["id"]==ws); members={m["user_id"]:m["role"] for m in w["members"] if m["active"]}; devices=[d for d in w["devices"] if d["active"] and d.get("allowed",1)]
    if remove:
        found=request(cfg,{"op":"directory","user":user}); target=directory_user(found,user) if found["users"] else {"id":user}; members.pop(target["id"],None)
    else:
        found=request(cfg,{"op":"directory","user":user}); trusted(found["devices"]); target=directory_user(found,user); members[target["id"]]="member"; devices+=found["devices"]
    return rotate(cfg,ws,members,[d for d in {d["id"]:d for d in devices}.values() if d["user_id"] in members],root=root)
def grant_all(cfg,ws,user,root=None):
    found=request(cfg,{"op":"directory","user":user}); target=directory_user(found,user); refresh(cfg,root); previous=cfg["controls"][ws]; allowed={d for d,r in previous["devices"].items() if r["user"]==target["id"]}; devices=trusted([d for w in cfg["server_state"]["workspaces"] if w["id"]==ws for d in w["devices"] if d["id"] in allowed]); envelopes={}
    for name,value in cfg["keys"].items():
        if name.startswith(ws+":"):
            epoch=int(name.rsplit(":",1)[1]); envelopes[str(epoch)]={d["id"]:seal_key(unb64(value),d["box_public"],f"workspace:{ws}:epoch:{epoch}") for d in devices}
    members={**previous["members"],target["id"]:{**previous["members"][target["id"]],"history_from":1}}; records={d:{**r,"history":True} if r["user"]==target["id"] else r for d,r in previous["devices"].items()}; control=control_body(cfg,previous,key(cfg,ws,previous["epoch"]),"history",members,records); request(cfg,sign_control(cfg["device"],{"op":"grant_all","workspace":ws,"user":target["id"],"control":control,"envelopes":envelopes})); cfg["controls"][ws]=control; save(cfg,root); return len(envelopes)
def grant_selected(cfg,state,ws,user,event_ids,root=None):
    if not event_ids: return 0
    found=request(cfg,{"op":"directory","user":user}); target=directory_user(found,user); refresh(cfg,root); previous=cfg["controls"][ws]; devices=trusted([d for w in cfg["server_state"]["workspaces"] if w["id"]==ws for d in w["devices"] if d["user_id"]==target["id"] and d["active"] and d["allowed"] and d["authorized"]]); rows=state.execute(f"SELECT event,event_json FROM event_log WHERE workspace=? AND event IN ({','.join('?'*len(event_ids))})",(ws,*event_ids)).fetchall()
    if not rows: return 0
    if not devices: raise ValueError("target has no authorized workspace devices")
    members={**previous["members"],target["id"]:{**previous["members"][target["id"]],"selected":sorted(set(previous["members"][target["id"]]["selected"])|{e for e,_ in rows})}}; records={d:{**r,"history":True} if r["user"]==target["id"] else r for d,r in previous["devices"].items()}; control=control_body(cfg,previous,key(cfg,ws,previous["epoch"]),"history",members,records); request(cfg,sign_control(cfg["device"],{"op":"grant_selected","workspace":ws,"control":control})); cfg["controls"][ws]=control; save(cfg,root)
    [publish(cfg,state,ws,{"kind":"history.republish","entity":(entity:=f"history:{target['id']}:{eid}"),"payload":{"target":target["id"],"sealed":seal_history(json.loads(raw),devices,entity)}} ,root) for eid,raw in rows]; upload(cfg,state,root); return len(rows)
def remove_device(cfg,ws,device_id,root=None):
    state=refresh(cfg,root); w=next(x for x in state["workspaces"] if x["id"]==ws); members={m["user_id"]:m["role"] for m in w["members"] if m["active"]}; devices=[d for d in w["devices"] if d["active"] and d.get("allowed",1) and d["id"]!=device_id]; return rotate(cfg,ws,members,devices,[device_id],root)
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
    if not same:
        request(cfg,{"op":"vote","vote":device_vote(cfg["device"],cfg["user"],item["proposal"],approve)}); item=next(p for p in proposals(cfg,ws) if state_hash(p["proposal"])==state_hash(item["proposal"]))
        yes=len({v["voter"] for v in item["votes"] if v["approve"]}); needed=len(electorate(base,target["user"]))//2+1
        if not approve or yes<needed: return {"approved":False,"votes":yes,"needed":needed}
        approved(base,item["proposal"],item["votes"])
    new=os.urandom(32); epoch=base["epoch"]+1; inherit=base["devices"][cfg["device"]["id"]]["history"] if same else False; entry={**target,"history":inherit}; records={**base["devices"],device_id:entry}; action="self_approve" if same else "quorum_approve"; proof={"proposal":item["proposal"],"votes":item["votes"]}; control=control_body(cfg,base,new,action,devices=records,approval=proof); envs={d:seal_key(new,r["device"]["box_public"],f"workspace:{ws}:epoch:{epoch}") for d,r in records.items()}; history={name.rsplit(":",1)[1]:seal_key(unb64(value),entry["device"]["box_public"],f"workspace:{ws}:epoch:{name.rsplit(':',1)[1]}") for name,value in cfg["keys"].items() if inherit and name.startswith(ws+":")}
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
@remote.command("workspace")
def workspace_cmd(name:str): cfg=load(); typer.echo(create(cfg,name,"team"))
@remote.command("invite")
def invite_cmd(space:str,user:str): cfg=load(); typer.echo(f"epoch {add_member(cfg,workspace(cfg,space),user)}")
@remote.command("remove")
def remove_cmd(space:str,user:str): cfg=load(); typer.echo(f"epoch {add_member(cfg,workspace(cfg,space),user,True)}")
@remote.command("grant-all")
def grant_all_cmd(space:str,user:str): cfg=load(); typer.echo(f"Granted {grant_all(cfg,workspace(cfg,space),user)} epochs")
@remote.command("grant-selected")
def grant_selected_cmd(space:str,user:str,events:list[str]): cfg=load(); typer.echo(f"Granted {grant_selected(cfg,connect(paths()[2]),workspace(cfg,space),user,events)} events")
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
    cfg=load(); ws=workspace(cfg,space); state=connect(paths()[2]); repo=repository(path.resolve()); kind,value=("repository",repo["id"]) if repo else ("path",digest(os.urandom(32))[:32]); state.execute("INSERT OR REPLACE INTO policies VALUES (?,?,?,?)",(ws,kind,value,str(path.resolve()))); state.execute("DELETE FROM meta WHERE key='core_mtime'"); state.commit(); publish(cfg,state,ws,{"kind":"workspace.policy","entity":f"policy:{kind}:{value}","payload":{"kind":kind,"value":value}}); upload(cfg,state); typer.echo(f"{kind} {value} -> {cfg['workspaces'][ws]['name']}")
@remote.command("sync")
def sync_cmd(): sync_once(force=True); typer.echo("Remote synchronized")
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
        cfg=load(); state=connect(paths()[2]); pending=state.execute("SELECT COUNT(*) FROM event_log WHERE direction='out' AND cursor=0").fetchone()[0]; lazy=state.execute("SELECT COUNT(*) FROM lazy_events").fetchone()[0]; last=(state.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone() or ["never"])[0]; online="reachable" if health(cfg)["ok"] else "error"; return f"remote: {online}, user={cfg['user'][:8]}, device={cfg['device']['id'][:8]}, workspaces={len(cfg['workspaces'])}, epochs={len(cfg['keys'])}, pending={pending}, lazy={lazy}, last={last}"
    except Exception as e: return f"remote: unavailable ({e})"
@remote.command("doctor")
def doctor_cmd(): typer.echo(doctor_status())
@remote.command("graph")
def graph_cmd(view:str,arg:Optional[str]=None): typer.echo(json.dumps(graph_query(connect(paths()[2]),view,arg),default=str))
@remote.command("rebuild")
def rebuild_cmd(output:Path): cfg=load(); typer.echo(f"Projected {rebuild_projection(output,connect(paths()[2]),device=cfg['device'])} events into {output}")
[register(app) for app in _pending]; _pending.clear()
