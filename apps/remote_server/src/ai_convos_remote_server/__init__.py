"""Opaque self-hosted relay. It authorizes envelopes but never receives content keys."""
import argparse, base64, hashlib, json, os, secrets, sqlite3, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

V=1
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
def verify_control(actor,req):
    try: Ed25519PublicKey.from_public_bytes(unb64(actor["sign_public"])).verify(unb64(req["control_signature"]),canon({k:v for k,v in req.items() if k!="control_signature"}))
    except (InvalidSignature,KeyError,TypeError,ValueError) as e: raise PermissionError("invalid control signature") from e
def certify(db,actor,req):
    root=db.execute("SELECT root_public FROM users WHERE id=?",(actor["user_id"],)).fetchone()[0]; body=verify_certificate(req["certificate"],root); target=db.execute("SELECT * FROM devices WHERE id=? AND user_id=?",(body["device"]["id"],actor["user_id"])).fetchone(); expected={k:target[k] for k in ("id","name","sign_public","box_public")} if target else None
    if body["user"]!=actor["user_id"] or body["device"]!=expected: raise PermissionError("device certificate does not match account")
    db.execute("INSERT OR REPLACE INTO device_certificates VALUES (?,?)",(body["device"]["id"],json.dumps(req["certificate"]))); db.commit(); return {"certified":body["device"]["id"]}
def rows(db, sql, args=()): return [dict(r) for r in db.execute(sql, args).fetchall()]

def register(db, req):
    cert, root = req["certificate"], req["root_public"]; body = verify_certificate(cert, root); user, dev = body["user"], body["device"]
    if user!=public_id(root) or dev["id"]!=public_id(dev["sign_public"]): raise ValueError("identity id does not match public key")
    old = db.execute("SELECT root_public FROM users WHERE id=?", (user,)).fetchone()
    if old and old[0] != root: raise PermissionError("user root mismatch")
    if not old: db.execute("INSERT INTO users VALUES (?,?,?,?,?)", (user,req["user_name"],root,json.dumps(req.get("recovery")),time.time()))
    token = secrets.token_urlsafe(32); db.execute("INSERT INTO devices VALUES (?,?,?,?,?,?,?,?)", (dev["id"],user,dev["name"],dev["sign_public"],dev["box_public"],token_hash(token),1,time.time())); db.execute("INSERT INTO device_certificates VALUES (?,?)",(dev["id"],json.dumps(cert)))
    db.commit(); return dict(user=user, device=dev["id"], token=token)

def rotate(db, actor, req):
    ws = req["workspace"]; member(db, ws, actor["user_id"], "admin"); current = db.execute("SELECT epoch,kind FROM workspaces WHERE id=?", (ws,)).fetchone(); excluded={r[0] for r in db.execute("SELECT device FROM workspace_device_exclusions WHERE workspace=?",(ws,)).fetchall()}
    if actor["id"] in excluded or (current["kind"]!="personal" and not db.execute("SELECT 1 FROM key_envelopes WHERE workspace=? AND epoch=? AND device=?",(ws,current["epoch"],actor["id"])).fetchone()): raise PermissionError("device is not authorized for current workspace epoch")
    if req["epoch"] != current["epoch"] + 1: raise ValueError(f"epoch must be {current['epoch'] + 1}")
    wanted, envelopes = req["members"], req["envelopes"]; devices = rows(db, "SELECT * FROM devices WHERE active=1")
    excluded|=set(req.get("deactivate_devices",[])); excluded-=set(req.get("activate_devices",[])); expected={d["id"] for d in devices if d["user_id"] in wanted and d["id"] not in excluded}
    if set(envelopes) != expected: raise ValueError("one key envelope required for every active member device")
    for old in rows(db, "SELECT user_id,joined_epoch,history_from FROM members WHERE workspace=?", (ws,)):
        if old["user_id"] not in wanted: db.execute("UPDATE members SET active=0 WHERE workspace=? AND user_id=?", (ws,old["user_id"]))
    for user, role in wanted.items():
        old = db.execute("SELECT joined_epoch,history_from FROM members WHERE workspace=? AND user_id=?", (ws,user)).fetchone(); joined, history = (old if old else (req["epoch"],req["epoch"]))
        db.execute("INSERT OR REPLACE INTO members VALUES (?,?,?,?,?,?)", (ws,user,role,1,joined,history))
    [db.execute("INSERT INTO key_envelopes VALUES (?,?,?,?)", (ws,req["epoch"],dev,json.dumps(env))) for dev,env in envelopes.items()]
    [db.execute("INSERT OR IGNORE INTO workspace_device_exclusions VALUES (?,?)",(ws,d)) for d in req.get("deactivate_devices",[])]; [db.execute("DELETE FROM workspace_device_exclusions WHERE workspace=? AND device=?",(ws,d)) for d in req.get("activate_devices",[])]
    db.execute("UPDATE workspaces SET epoch=? WHERE id=?", (req["epoch"],ws)); db.commit(); return {"workspace":ws,"epoch":req["epoch"]}
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
    if op in ("create","rotate","grant_all","recovery"): verify_control(actor,req)
    if op == "create":
        ws, env = req["workspace"], req["envelope"]; db.execute("INSERT INTO workspaces VALUES (?,?,?,?,?)", (ws,req["kind"],1,actor["user_id"],time.time())); db.execute("INSERT INTO members VALUES (?,?,?,?,?,?)", (ws,actor["user_id"],"admin",1,1,1)); db.execute("INSERT INTO key_envelopes VALUES (?,?,?,?)", (ws,1,actor["id"],json.dumps(env))); db.commit(); return {"workspace":ws,"epoch":1}
    if op == "rotate": return rotate(db, actor, req)
    if op == "directory":
        return {"users":rows(db, "SELECT id,name,root_public FROM users WHERE name=? OR id=?", (req["user"],req["user"])), "devices":rows(db, "SELECT d.id,d.user_id,d.name,d.sign_public,d.box_public,c.certificate,u.root_public FROM devices d JOIN users u ON u.id=d.user_id LEFT JOIN device_certificates c ON c.device=d.id WHERE d.active=1 AND d.user_id IN (SELECT id FROM users WHERE name=? OR id=?)", (req["user"],req["user"]))}
    if op == "state":
        memberships = rows(db, "SELECT w.id,w.kind,w.epoch,m.role,m.joined_epoch,m.history_from FROM workspaces w JOIN members m ON w.id=m.workspace WHERE m.user_id=? AND m.active=1", (actor["user_id"],))
        for w in memberships:
            w["keys"] = rows(db, "SELECT epoch,envelope FROM key_envelopes WHERE workspace=? AND device=? ORDER BY epoch", (w["id"],actor["id"])); w["devices"] = rows(db, "SELECT DISTINCT d.id,d.user_id,d.name,d.sign_public,d.box_public,d.active,c.certificate,u.root_public,NOT EXISTS(SELECT 1 FROM workspace_device_exclusions x WHERE x.workspace=? AND x.device=d.id) allowed,EXISTS(SELECT 1 FROM key_envelopes k WHERE k.workspace=? AND k.epoch=? AND k.device=d.id) authorized FROM devices d JOIN users u ON u.id=d.user_id LEFT JOIN device_certificates c ON c.device=d.id JOIN members m ON d.user_id=m.user_id WHERE m.workspace=?", (w["id"],w["id"],w["epoch"],w["id"])); w["members"] = rows(db,"SELECT user_id,role,active,joined_epoch,history_from FROM members WHERE workspace=?",(w["id"],)); w["device_authorized"]=bool(db.execute("SELECT 1 FROM key_envelopes WHERE workspace=? AND epoch=? AND device=?",(w["id"],w["epoch"],actor["id"])).fetchone()) and not bool(db.execute("SELECT 1 FROM workspace_device_exclusions WHERE workspace=? AND device=?",(w["id"],actor["id"])).fetchone())
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
        member(db,req["workspace"],req["user"]); current=db.execute("SELECT epoch FROM workspaces WHERE id=?",(req["workspace"],)).fetchone()[0]; expected={r[0] for r in db.execute("SELECT id FROM devices WHERE user_id=? AND active=1 AND NOT EXISTS(SELECT 1 FROM workspace_device_exclusions WHERE workspace=? AND device=devices.id) AND EXISTS(SELECT 1 FROM key_envelopes WHERE workspace=? AND epoch=? AND device=devices.id)",(req["user"],req["workspace"],req["workspace"],current)).fetchall()}; epochs={int(epoch) for epoch in req["envelopes"]}
        if len(epochs)!=len(req["envelopes"]) or any(not 1<=epoch<=current for epoch in epochs): raise ValueError("history grant epoch is outside the workspace history")
        if any(set(values)!=expected for values in req["envelopes"].values()): raise ValueError("one key envelope required for every authorized target device")
        existing={(r[0],r[1]) for r in db.execute("SELECT epoch,device FROM key_envelopes WHERE workspace=?",(req["workspace"],)).fetchall()}; provided={(int(epoch),dev) for epoch,values in req["envelopes"].items() for dev in values}; needed={(epoch,dev) for epoch in range(1,current+1) for dev in expected}
        if not expected or not needed<=existing|provided: raise ValueError("history grant does not cover every workspace epoch")
        db.execute("UPDATE members SET history_from=1 WHERE workspace=? AND user_id=?", (req["workspace"],req["user"])); [db.execute("INSERT OR REPLACE INTO key_envelopes VALUES (?,?,?,?)", (req["workspace"],int(epoch),dev,json.dumps(env))) for epoch,values in req["envelopes"].items() for dev,env in values.items()]; db.commit(); return {"granted":"all"}
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
