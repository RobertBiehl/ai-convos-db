"""Opaque self-hosted relay. It authorizes envelopes but never receives content keys."""
import argparse, base64, hashlib, json, os, secrets, sqlite3, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

V=1
APPROVAL_DELAY=int(os.environ.get("CONVOS_REMOTE_APPROVAL_DELAY","3600")); CLOCK_SKEW=30
def canon(v): return json.dumps(v,sort_keys=True,separators=(",",":"),ensure_ascii=True,allow_nan=False).encode()
def unb64(v): return base64.urlsafe_b64decode(v+"="*(-len(v)%4))
def digest(v): return hashlib.sha256(v if isinstance(v,bytes) else canon(v)).hexdigest()
def public_id(value): return digest(unb64(value))[:32]
def verify_certificate(cert,root_public):
    sig,body=unb64(cert["signature"]),{k:v for k,v in cert.items() if k!="signature"}; Ed25519PublicKey.from_public_bytes(unb64(root_public)).verify(sig,canon(body))
    if body["v"]!=V: raise ValueError(f"Unsupported certificate version {body['v']}")
    return body

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,name TEXT UNIQUE,root_public TEXT NOT NULL,recovery TEXT,created REAL);
CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,name TEXT,sign_public TEXT NOT NULL,box_public TEXT NOT NULL,token_hash TEXT UNIQUE NOT NULL,active INT NOT NULL DEFAULT 1,created REAL);
CREATE TABLE IF NOT EXISTS device_certificates(device TEXT PRIMARY KEY,certificate TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workspaces(id TEXT PRIMARY KEY,kind TEXT NOT NULL,epoch INT NOT NULL,created_by TEXT NOT NULL,created REAL);
CREATE TABLE IF NOT EXISTS members(workspace TEXT,user_id TEXT,role TEXT,active INT,joined_epoch INT,history_from INT,PRIMARY KEY(workspace,user_id));
CREATE TABLE IF NOT EXISTS key_envelopes(workspace TEXT,epoch INT,device TEXT,envelope TEXT,PRIMARY KEY(workspace,epoch,device));
CREATE TABLE IF NOT EXISTS workspace_device_exclusions(workspace TEXT,device TEXT,PRIMARY KEY(workspace,device));
CREATE TABLE IF NOT EXISTS events(cursor INTEGER PRIMARY KEY AUTOINCREMENT,workspace TEXT,event TEXT,author TEXT,epoch INT,seq INT,envelope TEXT,wire_hash TEXT,created REAL,UNIQUE(workspace,event));
CREATE UNIQUE INDEX IF NOT EXISTS event_author_sequence ON events(workspace,author,seq);
CREATE TABLE IF NOT EXISTS workspace_controls(workspace TEXT,revision INT,state_hash TEXT UNIQUE,state TEXT,PRIMARY KEY(workspace,revision));
CREATE TABLE IF NOT EXISTS device_proposals(id TEXT PRIMARY KEY,workspace TEXT,base TEXT,target_user TEXT,target_device TEXT,proposal TEXT,not_before REAL,expires REAL,active INT);
CREATE TABLE IF NOT EXISTS device_votes(proposal TEXT,voter_user TEXT,voter_device TEXT,approve INT,vote TEXT,PRIMARY KEY(proposal,voter_user));
"""

def connect(path):
    db = sqlite3.connect(path); db.row_factory = sqlite3.Row; db.executescript("PRAGMA journal_mode=WAL;PRAGMA foreign_keys=ON;PRAGMA busy_timeout=30000;" + SCHEMA); return db
def token_hash(token): return hashlib.sha256(token.encode()).hexdigest()
def auth(db, token):
    row = db.execute("SELECT * FROM devices WHERE token_hash=? AND active=1", (token_hash(token or ""),)).fetchone()
    if not row: raise PermissionError("invalid device token")
    return dict(row)
def member(db, workspace, user, role=None):
    row = db.execute("SELECT * FROM members WHERE workspace=? AND user_id=? AND active=1", (workspace,user)).fetchone()
    if not row or role == "admin" and row["role"] != "admin": raise PermissionError("workspace access denied")
    return dict(row)
def device_member(db,workspace,actor):
    result=member(db,workspace,actor["user_id"]); epoch=db.execute("SELECT epoch FROM workspaces WHERE id=?",(workspace,)).fetchone()[0]
    if db.execute("SELECT 1 FROM workspace_device_exclusions WHERE workspace=? AND device=?",(workspace,actor["id"])).fetchone() or not db.execute("SELECT 1 FROM key_envelopes WHERE workspace=? AND epoch=? AND device=?",(workspace,epoch,actor["id"])).fetchone(): raise PermissionError("device is not authorized for current workspace epoch")
    return result
def verify_request(actor,req):
    try: Ed25519PublicKey.from_public_bytes(unb64(actor["sign_public"])).verify(unb64(req["control_signature"]),canon({k:v for k,v in req.items() if k!="control_signature"}))
    except (InvalidSignature,KeyError,TypeError,ValueError) as e: raise PermissionError("invalid control signature") from e
def certify(db,actor,req):
    root=db.execute("SELECT root_public FROM users WHERE id=?",(actor["user_id"],)).fetchone()[0]; body=verify_certificate(req["certificate"],root); target=db.execute("SELECT * FROM devices WHERE id=? AND user_id=?",(body["device"]["id"],actor["user_id"])).fetchone(); expected={k:target[k] for k in ("id","name","sign_public","box_public")} if target else None
    if body["user"]!=actor["user_id"] or body["device"]!=expected: raise PermissionError("device certificate does not match account")
    db.execute("INSERT OR REPLACE INTO device_certificates VALUES (?,?)",(body["device"]["id"],json.dumps(req["certificate"]))); db.commit(); return {"certified":body["device"]["id"]}
def rows(db, sql, args=()): return [dict(r) for r in db.execute(sql, args).fetchall()]
def verify_signed(value,sign_public):
    signature=unb64(value["signature"]); body={k:v for k,v in value.items() if k!="signature"}; Ed25519PublicKey.from_public_bytes(unb64(sign_public)).verify(signature,canon(body)); return value
def verify_record(value):
    body=verify_certificate(value["certificate"],value["root_public"]); device=value["device"]
    if public_id(value["root_public"])!=value["user"] or public_id(device["sign_public"])!=device["id"] or body["user"]!=value["user"] or body["device"]!=device: raise ValueError("device record mismatch")
    return value
def control_hash(value): return digest(value)
def current_control(db,ws):
    row=db.execute("SELECT state FROM workspace_controls WHERE workspace=? ORDER BY revision DESC LIMIT 1",(ws,)).fetchone(); return json.loads(row[0]) if row else None
def electorate(state,target): return sorted({d["user"] for d in state["devices"].values() if d["user"]!=target})
def verify_proposal(previous,request,now=None,kind="device.proposal"):
    target=verify_record(request["target"]); verify_signed(request,target["device"]["sign_public"])
    moment=time.time() if now is None else float(now)
    if request["v"]!=V or request["kind"]!=kind or request["base"]!=control_hash(previous) or request["workspace"]!=previous["workspace"] or request["epoch"]!=previous["epoch"] or request["certificate_hash"]!=digest(target["certificate"]) or not request["not_before"]<=moment<request["expires"]: raise ValueError("proposal state mismatch")
    return target
def verify_approval(previous,approval,now=None,kind="device.proposal"):
    request,votes=approval["proposal"],approval.get("votes",[]); target=verify_proposal(previous,request,now,kind)
    if kind=="device.proposal" and any(d["user"]==target["user"] for d in previous["devices"].values()): raise ValueError("target user can self approve")
    eligible=set(electorate(previous,target["user"])); by_user={}
    for value in votes:
        record=previous["devices"].get(value["author"])
        if value["v"]!=V or value["kind"]!="device.vote" or type(value["approve"]) is not bool or not record or record["user"]!=value["voter"] or value["voter"] not in eligible: raise ValueError("ineligible vote")
        verify_signed(value,record["device"]["sign_public"])
        if (value["proposal"],value["workspace"],value["base"])!=(control_hash(request),request["workspace"],request["base"]): raise ValueError("vote proposal mismatch")
        if value["voter"] in by_user and by_user[value["voter"]]!=value["approve"]: raise ValueError("conflicting user votes")
        by_user[value["voter"]]=value["approve"]
    needed=len(eligible)//2+1
    if not eligible or sum(v is True for v in by_user.values())<needed: raise ValueError(f"approval requires {needed} of {len(eligible)} votes")
def verify_window(db,request,now):
    row=db.execute("SELECT not_before,expires,active FROM device_proposals WHERE id=?",(control_hash(request),)).fetchone()
    if not row or not row["active"] or not row["not_before"]<=now<row["expires"]: raise ValueError("proposal is not active by relay clock")
def verify_control(db,actor,value,previous=None):
    if value["v"]!=V or value["kind"]!="workspace.state": raise ValueError("unsupported workspace state")
    if value["scope"] not in ("personal","team") or len(value["key_commitment"])!=64 or any(m["role"] not in ("admin","member") for m in value["members"].values()) or any(d["user"] not in value["members"] for d in value["devices"].values()) or set(value["devices"])&set(value["removed"]): raise ValueError("invalid workspace state")
    author=(value["devices"] if previous is None else previous["devices"]).get(value["author"])
    if value["action"]=="personal_recover" and previous is not None: author=value["devices"].get(value["author"])
    if not author or actor["id"]!=value["author"]: raise PermissionError("state author is not authorized")
    verify_signed(value,verify_record(author)["device"]["sign_public"]); [verify_record(d) for d in value["devices"].values()]
    if previous is None:
        if value["revision"]!=1 or value["prev"] is not None or value["epoch"]!=1 or value["author"] not in value["devices"] or value["action"]!="create" or value["members"][author["user"]]["role"]!="admin" or set(value["members"])!={author["user"]} or set(value["devices"])!={value["author"]} or value["removed"]: raise ValueError("invalid genesis state")
        return value
    if (value["workspace"],value["scope"])!=(previous["workspace"],previous["scope"]) or value["revision"]!=previous["revision"]+1 or value["prev"]!=control_hash(previous): raise ValueError("workspace state chain mismatch")
    action=value["action"]; previous_author=previous["devices"].get(value["author"]); admin=bool(previous_author and previous["members"][previous_author["user"]]["role"]=="admin")
    if action in ("membership","remove","history") and not admin: raise PermissionError("admin control required")
    if action in ("self_approve","quorum_approve","personal_recover"):
        now=time.time()
        if abs(float(value["approved_at"])-now)>CLOCK_SKEW: raise ValueError("approval clock mismatch")
        if value["members"]!=previous["members"] or value["removed"]!=previous["removed"] or value["epoch"]!=previous["epoch"]+1: raise ValueError("approval changed workspace policy")
        added=set(value["devices"])-set(previous["devices"])
        if len(added)!=1 or set(previous["devices"])-set(value["devices"]): raise ValueError("approval must add exactly one device")
        target=value["approval"]["proposal"]["target"]; device=next(iter(added))
        if device!=target["device"]["id"] or {k:v for k,v in value["devices"][device].items() if k!="history"}!={k:v for k,v in target.items() if k!="history"} or device in previous["removed"]: raise ValueError("approval target mismatch")
        if action=="self_approve" and (previous_author["user"]!=target["user"] or value["devices"][device]["history"]!=previous_author["history"]): raise PermissionError("self approval permission mismatch")
        if action in ("self_approve","personal_recover"): verify_proposal(previous,value["approval"]["proposal"],now)
        if action!="personal_recover": verify_window(db,value["approval"]["proposal"],now)
        if action=="quorum_approve" and value["devices"][device]["history"] is not False: raise ValueError("quorum approval must be future-only")
        if action=="quorum_approve": verify_approval(previous,value["approval"],now)
        if action=="personal_recover" and (previous.get("scope")!="personal" or target["user"] not in previous["members"]): raise PermissionError("personal recovery mismatch")
    elif action=="history":
        if set(value["devices"])!=set(previous["devices"]) or any({k:v for k,v in d.items() if k!="history"}!={k:v for k,v in previous["devices"][i].items() if k!="history"} or previous["devices"][i]["history"] and not d["history"] for i,d in value["devices"].items()) or value["removed"]!=previous["removed"] or value["epoch"]!=previous["epoch"] or value["key_commitment"]!=previous["key_commitment"] or set(value["members"])!=set(previous["members"]) or any((m["role"],m["joined"])!=(previous["members"][u]["role"],previous["members"][u]["joined"]) for u,m in value["members"].items()): raise ValueError("invalid history transition")
    elif action=="history_activate":
        now=time.time()
        if abs(float(value["approved_at"])-now)>CLOCK_SKEW: raise ValueError("approval clock mismatch")
        target=value["approval"]["proposal"]["target"]; device=target["device"]["id"]; expected={**previous["devices"],device:{**previous["devices"][device],"history":True}}
        if target["history"] is not True or previous["devices"].get(device,{}).get("history") is not False or value["members"]!=previous["members"] or value["devices"]!=expected or value["removed"]!=previous["removed"] or value["epoch"]!=previous["epoch"] or value["key_commitment"]!=previous["key_commitment"]: raise ValueError("invalid history activation")
        verify_approval(previous,value["approval"],now,"history.proposal")
        verify_window(db,value["approval"]["proposal"],now)
    elif action=="remove":
        removed=set(previous["devices"])-set(value["devices"])
        if value["members"]!=previous["members"] or value["epoch"]!=previous["epoch"]+1 or not removed or not removed<=set(value["removed"]) or not set(previous["removed"])<=set(value["removed"]) or any(value["devices"].get(d)!=r for d,r in previous["devices"].items() if d not in removed): raise ValueError("invalid device removal")
    elif action=="membership":
        added_users=set(value["members"])-set(previous["members"]); removed_users=set(previous["members"])-set(value["members"]); added=set(value["devices"])-set(previous["devices"]); removed=set(previous["devices"])-set(value["devices"])
        if value["epoch"]!=previous["epoch"]+1 or value["scope"]=="personal" and value["members"]!=previous["members"] or set(value["devices"])&set(value["removed"]) or any(r["user"] not in added_users for d,r in value["devices"].items() if d in added) or any(r["user"] not in removed_users for d,r in previous["devices"].items() if d in removed) or any(value["devices"].get(d)!=r for d,r in previous["devices"].items() if r["user"] not in removed_users) or any((m["joined"],m["history_from"],m["selected"])!=(previous["members"][u]["joined"],previous["members"][u]["history_from"],previous["members"][u]["selected"]) for u,m in value["members"].items() if u not in added_users) or any((m["joined"],m["history_from"],m["selected"])!=(value["epoch"],value["epoch"],[]) for u,m in value["members"].items() if u in added_users) or not set(previous["removed"])<=set(value["removed"]) or not removed<=set(value["removed"]): raise ValueError("invalid membership transition")
    else: raise ValueError("unknown workspace action")
    return value
def apply_control(db,value,envelopes):
    ws=value["workspace"]; previous=current_control(db,ws)
    if set(envelopes)!=set(value["devices"]): raise ValueError("one key envelope required for every authorized device")
    for old in rows(db,"SELECT user_id FROM members WHERE workspace=?",(ws,)):
        if old["user_id"] not in value["members"]: db.execute("UPDATE members SET active=0 WHERE workspace=? AND user_id=?",(ws,old["user_id"]))
    for user,meta in value["members"].items(): db.execute("INSERT OR REPLACE INTO members VALUES (?,?,?,?,?,?)",(ws,user,meta["role"],1,meta["joined"],meta["history_from"]))
    db.execute("DELETE FROM workspace_device_exclusions WHERE workspace=?",(ws,)); [db.execute("INSERT INTO workspace_device_exclusions VALUES (?,?)",(ws,d)) for d in value["removed"]]
    [db.execute("INSERT INTO key_envelopes VALUES (?,?,?,?)",(ws,value["epoch"],d,json.dumps(env))) for d,env in envelopes.items()]
    db.execute("UPDATE workspaces SET epoch=? WHERE id=?",(value["epoch"],ws)); db.execute("INSERT INTO workspace_controls VALUES (?,?,?,?)",(ws,value["revision"],control_hash(value),json.dumps(value))); return {"workspace":ws,"epoch":value["epoch"],"control":control_hash(value)}
def apply_history(db,value):
    [db.execute("UPDATE members SET history_from=? WHERE workspace=? AND user_id=?",(m["history_from"],value["workspace"],u)) for u,m in value["members"].items()]; db.execute("INSERT INTO workspace_controls VALUES (?,?,?,?)",(value["workspace"],value["revision"],control_hash(value),json.dumps(value))); return {"workspace":value["workspace"],"control":control_hash(value)}

def register(db, req):
    cert, root = req["certificate"], req["root_public"]; body = verify_certificate(cert, root); user, dev = body["user"], body["device"]
    if user!=public_id(root) or dev["id"]!=public_id(dev["sign_public"]): raise ValueError("identity id does not match public key")
    old = db.execute("SELECT root_public FROM users WHERE id=?", (user,)).fetchone()
    if old and old[0] != root: raise PermissionError("user root mismatch")
    if not old: db.execute("INSERT INTO users VALUES (?,?,?,?,?)", (user,req["user_name"],root,json.dumps(req.get("recovery")),time.time()))
    token = secrets.token_urlsafe(32); db.execute("INSERT INTO devices VALUES (?,?,?,?,?,?,?,?)", (dev["id"],user,dev["name"],dev["sign_public"],dev["box_public"],token_hash(token),1,time.time())); db.execute("INSERT INTO device_certificates VALUES (?,?)",(dev["id"],json.dumps(cert)))
    db.commit(); return dict(user=user, device=dev["id"], token=token)

def rotate(db, actor, req):
    previous=current_control(db,req["workspace"])
    if not previous: raise ValueError("workspace control state is not initialized")
    if actor["id"] not in previous["devices"] and req.get("control",{}).get("action")!="personal_recover": raise PermissionError("device is not authorized for current workspace epoch")
    verify_control(db,actor,req["control"],previous)
    history=req.get("history_envelopes",{}); added=set(req["control"]["devices"])-set(previous["devices"])
    if set(history)-added: raise ValueError("history envelopes may target only a newly approved device")
    for device,epochs in history.items():
        record=req["control"]["devices"][device]; start=req["control"]["members"][record["user"]]["history_from"]
        if not record["history"] or any(not start<=int(epoch)<req["control"]["epoch"] for epoch in epochs): raise ValueError("history target is not entitled")
    if req["control"]["action"]=="self_approve" and (device:=next(iter(added))) and req["control"]["devices"][device]["history"] and {int(e) for e in history.get(device,{})}!=set(range(req["control"]["members"][req["control"]["devices"][device]["user"]]["history_from"],previous["epoch"]+1)): raise ValueError("inherited history does not cover the signed entitlement")
    result=apply_control(db,req["control"],req["envelopes"])
    for device,epochs in history.items():
        [db.execute("INSERT OR REPLACE INTO key_envelopes VALUES (?,?,?,?)",(req["workspace"],int(epoch),device,json.dumps(env))) for epoch,env in epochs.items()]
    if approval:=req["control"].get("approval"): db.execute("UPDATE device_proposals SET active=0 WHERE id=?",(control_hash(approval["proposal"]),))
    db.commit(); return result
def store_event(db,actor,env):
    ws=env["workspace"]; device_member(db,ws,actor); epoch=db.execute("SELECT epoch FROM workspaces WHERE id=?",(ws,)).fetchone()[0]
    if env["author"]!=actor["id"] or env["epoch"]!=epoch: raise PermissionError("event author or epoch rejected")
    wire=digest(env); old=db.execute("SELECT wire_hash,cursor FROM events WHERE workspace=? AND event=?",(ws,env["event"])).fetchone()
    if old:
        if old["wire_hash"]!=wire: raise ValueError("event id already has different ciphertext")
        return {"cursor":old["cursor"],"created":False}
    cur=db.execute("INSERT INTO events(workspace,event,author,epoch,seq,envelope,wire_hash,created) VALUES (?,?,?,?,?,?,?,?)",(ws,env["event"],env["author"],env["epoch"],env["seq"],json.dumps(env),wire,time.time())); return {"cursor":cur.lastrowid,"created":True}

def action(db, req, token=None):
    op = req["op"]
    if op == "register": return register(db, req)
    if op == "recovery_fetch":
        row = db.execute("SELECT recovery FROM users WHERE id=? OR name=?", (req["user"],req["user"])).fetchone()
        if not row or not row[0]: raise ValueError("recovery bundle not found")
        return {"bundle":json.loads(row[0])}
    actor = auth(db, token)
    if op == "certify": return certify(db,actor,req)
    if op in ("create","rotate","grant_all","grant_selected","history_activate","reject","recovery"): verify_request(actor,req)
    if op == "create":
        ws,control=req["workspace"],req["control"]; verify_control(db,actor,control)
        if (control["workspace"],control["scope"])!=(ws,req["kind"]): raise ValueError("workspace create scope mismatch")
        db.execute("INSERT INTO workspaces VALUES (?,?,?,?,?)",(ws,req["kind"],1,actor["user_id"],time.time())); result=apply_control(db,control,req["envelopes"]); db.commit(); return result
    if op == "rotate": return rotate(db, actor, req)
    if op == "propose":
        request=req["proposal"]; previous=current_control(db,request["workspace"]); target=verify_record(request["target"]); verify_signed(request,target["device"]["sign_public"])
        pending=request["kind"]=="device.proposal" and target["history"] is False and actor["id"] not in previous["devices"] and actor["id"] not in previous["removed"]
        history=request["kind"]=="history.proposal" and actor["id"] in previous["devices"] and previous["devices"][actor["id"]]["history"] is False and target=={**previous["devices"][actor["id"]],"history":True}
        now=time.time(); delay=APPROVAL_DELAY if len(electorate(previous,target["user"]))==1 and (history or not any(d["user"]==target["user"] for d in previous["devices"].values())) else 0; active=max(request["not_before"],now+delay)
        if request["v"]!=V or request["certificate_hash"]!=digest(target["certificate"]) or actor["id"]!=request["author"] or target["device"]["id"]!=actor["id"] or target["user"]!=actor["user_id"] or request["base"]!=control_hash(previous) or request["epoch"]!=previous["epoch"] or actor["user_id"] not in previous["members"] or not (pending or history) or not active<request["expires"]<=now+86400+CLOCK_SKEW: raise PermissionError("invalid device proposal")
        pid=control_hash(request); db.execute("INSERT INTO device_proposals VALUES (?,?,?,?,?,?,?,?,1)",(pid,request["workspace"],request["base"],actor["user_id"],actor["id"],json.dumps(request),active,request["expires"])); db.commit(); return {"proposal":pid}
    if op == "reject":
        row=db.execute("SELECT proposal,active FROM device_proposals WHERE id=? AND workspace=?",(req["proposal"],req["workspace"])).fetchone(); previous=current_control(db,req["workspace"]); record=previous["devices"].get(actor["id"]); proposal=json.loads(row["proposal"]) if row else None
        if not row or not row["active"] or not record or record["user"]!=actor["user_id"] or proposal["target"]["user"]!=actor["user_id"] or proposal["base"]!=control_hash(previous): raise PermissionError("proposal rejection denied")
        db.execute("UPDATE device_proposals SET active=0 WHERE id=?",(req["proposal"],)); db.commit(); return {"rejected":True}
    if op == "vote":
        value=req["vote"]; proposal_row=db.execute("SELECT proposal,workspace,base,expires,active FROM device_proposals WHERE id=?",(value["proposal"],)).fetchone()
        if not proposal_row or not proposal_row["active"] or proposal_row["expires"]<=time.time(): raise ValueError("proposal is not active")
        previous=current_control(db,proposal_row["workspace"]); record=previous["devices"].get(actor["id"])
        if value["v"]!=V or value["kind"]!="device.vote" or value["workspace"]!=proposal_row["workspace"] or value["voter"]!=actor["user_id"] or value["author"]!=actor["id"] or type(value["approve"]) is not bool or not record or record["user"]!=actor["user_id"] or actor["user_id"]==json.loads(proposal_row["proposal"])["target"]["user"] or value["base"]!=control_hash(previous): raise PermissionError("ineligible vote")
        verify_signed(value,record["device"]["sign_public"]); old=db.execute("SELECT approve FROM device_votes WHERE proposal=? AND voter_user=?",(value["proposal"],actor["user_id"])).fetchone()
        if old and bool(old[0])!=value["approve"]: raise ValueError("conflicting user vote")
        db.execute("INSERT OR REPLACE INTO device_votes VALUES (?,?,?,?,?)",(value["proposal"],actor["user_id"],actor["id"],value["approve"],json.dumps(value))); db.commit(); return {"recorded":True}
    if op == "proposals":
        member(db,req["workspace"],actor["user_id"]); out=[]
        for row in rows(db,"SELECT * FROM device_proposals WHERE workspace=? AND active=1 AND expires>? ORDER BY expires",(req["workspace"],time.time())):
            out.append({"proposal":json.loads(row["proposal"]),"votes":[json.loads(v[0]) for v in db.execute("SELECT vote FROM device_votes WHERE proposal=? ORDER BY voter_user",(row["id"],)).fetchall()]})
        return {"proposals":out}
    if op == "directory":
        return {"users":rows(db, "SELECT id,name,root_public FROM users WHERE name=? OR id=?", (req["user"],req["user"])), "devices":rows(db, "SELECT d.id,d.user_id,d.name,d.sign_public,d.box_public,c.certificate,u.root_public FROM devices d JOIN users u ON u.id=d.user_id LEFT JOIN device_certificates c ON c.device=d.id WHERE d.active=1 AND d.user_id IN (SELECT id FROM users WHERE name=? OR id=?)", (req["user"],req["user"]))}
    if op == "state":
        memberships = rows(db, "SELECT w.id,w.kind,w.epoch,m.role,m.joined_epoch,m.history_from FROM workspaces w JOIN members m ON w.id=m.workspace WHERE m.user_id=? AND m.active=1", (actor["user_id"],))
        for w in memberships:
            w["keys"] = rows(db, "SELECT epoch,envelope FROM key_envelopes WHERE workspace=? AND device=? ORDER BY epoch", (w["id"],actor["id"])); w["devices"] = rows(db, "SELECT DISTINCT d.id,d.user_id,d.name,d.sign_public,d.box_public,d.active,c.certificate,u.root_public,NOT EXISTS(SELECT 1 FROM workspace_device_exclusions x WHERE x.workspace=? AND x.device=d.id) allowed,EXISTS(SELECT 1 FROM key_envelopes k WHERE k.workspace=? AND k.epoch=? AND k.device=d.id) authorized FROM devices d JOIN users u ON u.id=d.user_id LEFT JOIN device_certificates c ON c.device=d.id JOIN members m ON d.user_id=m.user_id WHERE m.workspace=?", (w["id"],w["id"],w["epoch"],w["id"])); w["members"] = rows(db,"SELECT user_id,role,active,joined_epoch,history_from FROM members WHERE workspace=?",(w["id"],)); w["device_authorized"]=bool(db.execute("SELECT 1 FROM key_envelopes WHERE workspace=? AND epoch=? AND device=?",(w["id"],w["epoch"],actor["id"])).fetchone()) and not bool(db.execute("SELECT 1 FROM workspace_device_exclusions WHERE workspace=? AND device=?",(w["id"],actor["id"])).fetchone()); w["controls"]=[json.loads(r[0]) for r in db.execute("SELECT state FROM workspace_controls WHERE workspace=? ORDER BY revision",(w["id"],)).fetchall()]
        return {"user":actor["user_id"],"device":actor["id"],"workspaces":memberships}
    if op == "upload":
        result=store_event(db,actor,req["envelope"]); db.commit(); return result
    if op == "upload_many":
        if len(req["envelopes"])>500: raise ValueError("upload batch limit is 500")
        result=[store_event(db,actor,env) for env in req["envelopes"]]; db.commit(); return {"events":result}
    if op == "pull":
        m = device_member(db,req["workspace"],actor); out = rows(db, "SELECT cursor,event,envelope,LENGTH(envelope) size FROM events WHERE workspace=? AND cursor>? AND epoch>=? AND EXISTS(SELECT 1 FROM key_envelopes k WHERE k.workspace=events.workspace AND k.epoch=events.epoch AND k.device=?) ORDER BY cursor LIMIT ?", (req["workspace"],req.get("after",0),m["history_from"],actor["id"],req.get("limit",500)))
        return {"events":[{"cursor":r["cursor"],**({"lazy":True,"event":r["event"],"size":r["size"]} if r["size"]>65536 else {"envelope":json.loads(r["envelope"])})} for r in out]}
    if op == "fetch":
        m=device_member(db,req["workspace"],actor); row=db.execute("SELECT envelope FROM events WHERE workspace=? AND event=? AND epoch>=? AND EXISTS(SELECT 1 FROM key_envelopes k WHERE k.workspace=events.workspace AND k.epoch=events.epoch AND k.device=?)",(req["workspace"],req["event"],m["history_from"],actor["id"])).fetchone()
        if not row: raise ValueError("event not found")
        return {"envelope":json.loads(row[0])}
    if op == "grant_all":
        if device_member(db,req["workspace"],actor)["role"]!="admin": raise PermissionError("workspace access denied")
        previous=current_control(db,req["workspace"]); verify_control(db,actor,req["control"],previous); member(db,req["workspace"],req["user"]); current=previous["epoch"]; expected={d for d,r in previous["devices"].items() if r["user"]==req["user"]}; epochs={int(epoch) for epoch in req["envelopes"]}
        if len(epochs)!=len(req["envelopes"]) or any(not 1<=epoch<=current for epoch in epochs): raise ValueError("history grant epoch is outside the workspace history")
        if any(set(values)!=expected for values in req["envelopes"].values()): raise ValueError("one key envelope required for every authorized target device")
        existing={(r[0],r[1]) for r in db.execute("SELECT epoch,device FROM key_envelopes WHERE workspace=?",(req["workspace"],)).fetchall()}; provided={(int(epoch),dev) for epoch,values in req["envelopes"].items() for dev in values}; needed={(epoch,dev) for epoch in range(1,current+1) for dev in expected}
        if not expected or not needed<=existing|provided: raise ValueError("history grant does not cover every workspace epoch")
        if req["control"]["members"][req["user"]]["history_from"]!=1 or any(not req["control"]["devices"][d]["history"] for d in expected): raise ValueError("history control does not grant all")
        apply_history(db,req["control"]); [db.execute("INSERT OR REPLACE INTO key_envelopes VALUES (?,?,?,?)", (req["workspace"],int(epoch),dev,json.dumps(env))) for epoch,values in req["envelopes"].items() for dev,env in values.items()]; db.commit(); return {"granted":"all"}
    if op == "grant_selected":
        if device_member(db,req["workspace"],actor)["role"]!="admin": raise PermissionError("workspace access denied")
        previous=current_control(db,req["workspace"]); verify_control(db,actor,req["control"],previous); apply_history(db,req["control"]); db.commit(); return {"granted":"selected"}
    if op == "history_activate":
        previous=current_control(db,req["workspace"]); verify_control(db,actor,req["control"],previous); target=req["control"]["approval"]["proposal"]["target"]; device=target["device"]["id"]; start=previous["members"][target["user"]]["history_from"]; expected=set(range(start,previous["epoch"]+1)); epochs={int(e) for e in req["envelopes"]}
        if epochs!=expected: raise ValueError("history activation does not cover the entitlement")
        apply_history(db,req["control"]); [db.execute("INSERT OR REPLACE INTO key_envelopes VALUES (?,?,?,?)",(req["workspace"],int(epoch),device,json.dumps(env))) for epoch,env in req["envelopes"].items()]; db.execute("UPDATE device_proposals SET active=0 WHERE id=?",(control_hash(req["control"]["approval"]["proposal"]),)); db.commit(); return {"activated":device}
    if op == "recovery":
        db.execute("UPDATE users SET recovery=? WHERE id=?", (json.dumps(req["bundle"]),actor["user_id"])); db.commit(); return {"updated":True}
    raise ValueError(f"unknown operation {op}")

DB = None
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass
    def send(self, status, value):
        body = canon(value); self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self): self.send(200,{"ok":True,"version":1}) if self.path == "/v1/health" else self.send(404,{"error":"not found"})
    def do_POST(self):
        try:
            length=int(self.headers.get("Content-Length","0"))
            if length>64*1024*1024: raise ValueError("request exceeds 64 MiB")
            req = json.loads(self.rfile.read(length) or b"{}"); token = self.headers.get("Authorization","").removeprefix("Bearer "); db = connect(DB)
            try: out = action(db,req,token)
            finally: db.close()
            self.send(200,out)
        except PermissionError as e: self.send(403,{"error":str(e)})
        except (ValueError,KeyError,sqlite3.IntegrityError) as e: self.send(400,{"error":str(e)})
        except Exception as e: self.send(500,{"error":str(e)})

def main(argv=None):
    p = argparse.ArgumentParser(); p.add_argument("command",choices=("serve","backup")); p.add_argument("--db",default=os.environ.get("CONVOS_SERVER_DB","convos-server.db")); p.add_argument("--host",default="127.0.0.1"); p.add_argument("--port",type=int,default=8787); p.add_argument("--output"); a = p.parse_args(argv)
    Path(a.db).parent.mkdir(parents=True,exist_ok=True); db = connect(a.db); db.close()
    if a.command == "backup":
        if not a.output: p.error("backup requires --output")
        Path(a.output).parent.mkdir(parents=True,exist_ok=True); src, dst = sqlite3.connect(a.db), sqlite3.connect(a.output); src.backup(dst); src.close(); dst.close(); print(a.output); return
    global DB; DB = a.db; print(f"convos-server http://{a.host}:{a.port}",flush=True); ThreadingHTTPServer((a.host,a.port),Handler).serve_forever()
