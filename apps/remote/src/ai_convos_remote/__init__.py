"""Client-side enrollment, E2EE keyring, automatic sync, membership, and local queries."""
import json, os, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path; from typing import Optional

import typer
_pending=[]
def register(app): _pending.append(app) if "remote" not in globals() else app.add_typer(remote,name="remote")
from ai_convos.cli import DB_PATH, PROJECT_ROOT, drain_hooks, get_db, install_hooks
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
def directory_user(found,user): field="id" if len(user)==32 and all(c in "0123456789abcdef" for c in user) else "name"; values=[v for v in found["users"] if v[field]==user]; return values[0] if len(values)==1 and public_id(values[0]["root_public"])==values[0]["id"] else (_ for _ in ()).throw(ValueError("directory user mismatch"))
def update_recovery(cfg,root=None):
    personal={ws for ws,v in cfg["workspaces"].items() if v["kind"]=="personal"}; keys={name:value for name,value in cfg["keys"].items() if name.rsplit(":",1)[0] in personal}; _,bundle=recovery_bundle({"root":cfg["root"],"keys":keys,"workspaces":cfg["workspaces"]},unb64(cfg["recovery"])); request(cfg,sign_control(cfg["device"],{"op":"recovery","bundle":bundle})); save(cfg,root)
def refresh(cfg,root=None):
    state=request(cfg,{"op":"state"}); missing={d["id"]:d for w in state["workspaces"] for d in w["devices"] if d["user_id"]==cfg["user"] and not d["certificate"]}; [request(cfg,{"op":"certify","certificate":certificate(cfg["root"],cfg["user"],d)}) for d in missing.values()]; state=request(cfg,{"op":"state"}) if missing else state; cfg["server_state"]=state; changed=False
    for ws in state["workspaces"]:
        cfg["workspaces"].setdefault(ws["id"],{"name":ws["id"][:8],"kind":ws["kind"],"epoch":ws["epoch"]}); cfg["workspaces"][ws["id"]]["epoch"]=ws["epoch"]
        for wrapped in ws["keys"]:
            name=f"{ws['id']}:{wrapped['epoch']}"
            if name not in cfg["keys"]: cfg["keys"][name]=b64(open_key(json.loads(wrapped["envelope"]),cfg["device"]["box_private"],f"workspace:{ws['id']}:epoch:{wrapped['epoch']}")); changed=True
    save(cfg,root)
    if changed: update_recovery(cfg,root)
    return state
def create(cfg,name,kind="team",root=None):
    ws,key_=digest(os.urandom(32))[:32],os.urandom(32); env=seal_key(key_,cfg["device"]["box_public"],f"workspace:{ws}:epoch:1"); request(cfg,sign_control(cfg["device"],{"op":"create","workspace":ws,"kind":kind,"envelope":env})); cfg["workspaces"][ws]={"name":name,"kind":kind,"epoch":1}; cfg["keys"][f"{ws}:1"]=b64(key_); update_recovery(cfg,root); membership_event(cfg,ws,1,{cfg["user"]:"admin"},root); return ws
def setup_client(url,user,device="computer",recovery=None,root=None):
    if recovery:
        bundle=request({"url":url},{"op":"recovery_fetch","user":user},False)["bundle"]; recovered=recover(bundle,recovery); root_id=recovered["root"]; keys,workspaces=recovered["keys"],recovered["workspaces"]
    else: root_id,keys,workspaces=identity(user+" root"),{},{}
    dev=identity(device); uid=root_id["id"]
    if not recovery: recovery,bundle=recovery_bundle({"root":root_id,"keys":keys,"workspaces":workspaces})
    registered=request({"url":url},{"op":"register","user_name":user,"root_public":root_id["sign_public"],"certificate":certificate(root_id,uid,dev),**({"recovery":bundle} if not workspaces else {})},False)
    cfg={"url":url,"user":uid,"token":registered["token"],"root":root_id,"device":dev,"recovery":recovery,"keys":keys,"workspaces":workspaces,"server_state":{}}; save(cfg,root)
    if not workspaces: create(cfg,"Personal","personal",root)
    else:
        state=refresh(cfg,root)
        for ws in [w for w in state["workspaces"] if w["role"]=="admin" and w["kind"]=="personal"]:
            rotate(cfg,ws["id"],{m["user_id"]:m["role"] for m in ws["members"] if m["active"]},[d for d in ws["devices"] if d["active"] and d.get("allowed",1)],root=root)
            if ws["kind"]=="personal": grant_all(cfg,ws["id"],uid,root)
    return cfg,recovery
def rotate(cfg,ws,members,devices,deactivate=(),root=None):
    state=refresh(cfg,root); current=next(w for w in state["workspaces"] if w["id"]==ws); epoch=current["epoch"]+1; new=os.urandom(32); devices=trusted(devices); envs={d["id"]:seal_key(new,d["box_public"],f"workspace:{ws}:epoch:{epoch}") for d in devices if d["user_id"] in members and d.get("active",1) and d.get("allowed",1)}
    request(cfg,sign_control(cfg["device"],{"op":"rotate","workspace":ws,"epoch":epoch,"members":members,"envelopes":envs,"deactivate_devices":list(deactivate)})); cfg["keys"][f"{ws}:{epoch}"]=b64(new); cfg["workspaces"][ws]["epoch"]=epoch; update_recovery(cfg,root); membership_event(cfg,ws,epoch,members,root); return epoch
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
    server=refresh(cfg,root); devices={d["id"]:d for ws in server["workspaces"] for d in ws["devices"]}
    for ws in server["workspaces"]:
        if not ws["device_authorized"]: continue
        after=(state.execute("SELECT cursor FROM cursors WHERE workspace=?",(ws["id"],)).fetchone() or [0])[0]; seen=(state.execute("SELECT value FROM meta WHERE key=?",(f"history_from:{ws['id']}",)).fetchone() or [str(ws["history_from"])])[0]
        if ws["history_from"]<int(seen): after=0
        result=request(cfg,{"op":"pull","workspace":ws["id"],"after":after,"limit":500}); incoming=[]
        for item in result["events"]:
            if item.get("lazy"):
                state.execute("INSERT OR IGNORE INTO lazy_events VALUES (?,?,?,?)",(ws["id"],item["event"],item["cursor"],item["size"])); after=max(after,item["cursor"]); continue
            env=item["envelope"]; value=open_event(env,key(cfg,ws["id"],env["epoch"]),signer(devices,env["author"])); material=material_event(value,devices,cfg["device"]); sequence(state,ws["id"],value); state.execute("INSERT OR IGNORE INTO event_log VALUES (?,?,?,?,?,NULL)",(ws["id"],value["id"],item["cursor"],"in",json.dumps(value)))
            if material: incoming.append((ws["id"],material))
            after=max(after,item["cursor"])
        project_many(core_path(root),state,incoming,cfg["device"]["id"])
        state.execute("INSERT OR REPLACE INTO cursors VALUES (?,?)",(ws["id"],after)); state.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",(f"history_from:{ws['id']}",str(ws["history_from"]))); state.commit()
def fetch_lazy(cfg,state,event_id=None,root=None):
    server=refresh(cfg,root); devices={d["id"]:d for ws in server["workspaces"] for d in ws["devices"]}; sql="SELECT workspace,event,cursor FROM lazy_events"+(" WHERE event=?" if event_id else ""); rows=state.execute(sql,(event_id,) if event_id else ()).fetchall()
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
    found=request(cfg,{"op":"directory","user":user}); target=directory_user(found,user); devices=trusted([d for w in refresh(cfg,root)["workspaces"] if w["id"]==ws for d in w["devices"] if d["user_id"]==target["id"] and d["active"] and d["allowed"] and d["authorized"]]); envelopes={}
    for name,value in cfg["keys"].items():
        if name.startswith(ws+":"):
            epoch=int(name.rsplit(":",1)[1]); envelopes[str(epoch)]={d["id"]:seal_key(unb64(value),d["box_public"],f"workspace:{ws}:epoch:{epoch}") for d in devices}
    request(cfg,sign_control(cfg["device"],{"op":"grant_all","workspace":ws,"user":target["id"],"envelopes":envelopes})); return len(envelopes)
def grant_selected(cfg,state,ws,user,event_ids,root=None):
    found=request(cfg,{"op":"directory","user":user}); target=directory_user(found,user); devices=trusted([d for w in refresh(cfg,root)["workspaces"] if w["id"]==ws for d in w["devices"] if d["user_id"]==target["id"] and d["active"] and d["allowed"] and d["authorized"]]); rows=state.execute(f"SELECT event,event_json FROM event_log WHERE workspace=? AND event IN ({','.join('?'*len(event_ids))})",(ws,*event_ids)).fetchall()
    if not devices: raise ValueError("target has no authorized workspace devices")
    [publish(cfg,state,ws,{"kind":"history.republish","entity":(entity:=f"history:{target['id']}:{eid}"),"payload":{"target":target["id"],"sealed":seal_history(json.loads(raw),devices,entity)}} ,root) for eid,raw in rows]; upload(cfg,state,root); return len(rows)
def remove_device(cfg,ws,device_id,root=None):
    state=refresh(cfg,root); w=next(x for x in state["workspaces"] if x["id"]==ws); members={m["user_id"]:m["role"] for m in w["members"] if m["active"]}; devices=[d for d in w["devices"] if d["active"] and d.get("allowed",1) and d["id"]!=device_id]; return rotate(cfg,ws,members,devices,[device_id],root)
def approve_devices(cfg,ws,root=None):
    state=refresh(cfg,root); w=next(x for x in state["workspaces"] if x["id"]==ws); members={m["user_id"]:m["role"] for m in w["members"] if m["active"]}; return rotate(cfg,ws,members,[d for d in w["devices"] if d["active"] and d.get("allowed",1)],root=root)

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
@remote.command("approve-devices")
def approve_devices_cmd(space:str): cfg=load(); typer.echo(f"epoch {approve_devices(cfg,workspace(cfg,space))}")
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
