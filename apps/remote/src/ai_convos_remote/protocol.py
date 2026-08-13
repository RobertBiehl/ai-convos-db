"""Canonical signed/encrypted event protocol. Wire format v1; server sees envelopes only."""
import base64, hashlib, hmac, json, os
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

V = 1
ROW_FIELDS_V1={"conversations":("source","title","created_at","updated_at","model","project_id","metadata"),"messages":("conversation_id","role","content","thinking","created_at","model","metadata","parent_id"),"tool_calls":("message_id","tool_name","input","output","status","duration_ms","created_at"),"attachments":("message_id","filename","mime_type","size","created_at"),"artifacts":("conversation_id","artifact_type","title","content","language","created_at","version"),"file_edits":("message_id","file_path","edit_type","content","created_at","old_content")}
ROW_JSON_V1={"metadata","input","output"}; ROW_TIME_V1={"created_at","updated_at"}
ROW_PROOF_FIELDS={"v","kind","workspace","row_kind","row_id","encoding_v","content_hash","revision","previous_revision","state","author_user_id","author_device_id","authorization_epoch","signature"}
def canon(v): return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
def b64(v): return base64.urlsafe_b64encode(v).decode().rstrip("=")
def unb64(v): return base64.urlsafe_b64decode(v + "=" * (-len(v) % 4))
def digest(v): return hashlib.sha256(v if isinstance(v, bytes) else canon(v)).hexdigest()
def public_id(value): return digest(unb64(value))[:32]
def now(): return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
def logical_row(table,columns=(),values=(),identity=None,v=1,state="active"):
    if v!=1 or table not in ROW_FIELDS_V1 or state not in ("active","deleted") or state=="deleted" and (not identity or columns or values) or len(columns)!=len(values) or len(set(columns))!=len(columns): raise ValueError("invalid logical row schema")
    if state=="deleted": return {"v":v,"kind":table,"id":identity,"state":state,"data":None}
    row=dict(zip(columns,values)); required={"id",*ROW_FIELDS_V1[table]}
    if not required<=set(row): raise ValueError("incomplete logical row")
    norm=lambda k,v: json.loads(v) if v is not None and k in ROW_JSON_V1 and isinstance(v,str) else v.isoformat(timespec="microseconds") if v is not None and k in ROW_TIME_V1 and isinstance(v,datetime) else v
    return {"v":v,"kind":table,"id":identity or row["id"],"state":state,"data":{k:norm(k,row[k]) for k in ROW_FIELDS_V1[table]}}
def _priv(cls, value): return cls.from_private_bytes(unb64(value))
def _pub(cls, value): return cls.from_public_bytes(unb64(value))
def _raw(k): return b64(k.private_bytes_raw() if hasattr(k, "private_bytes_raw") else k.public_bytes_raw())

def identity(name="device"):
    sign, box = Ed25519PrivateKey.generate(), X25519PrivateKey.generate(); sp, bp = sign.public_key(), box.public_key()
    return dict(id=digest(sp.public_bytes_raw())[:32], name=name, sign_private=_raw(sign), sign_public=_raw(sp), box_private=_raw(box), box_public=_raw(bp))

def public(identity): return {k:identity[k] for k in ("id", "name", "sign_public", "box_public")}

def certificate(root, user, device):
    body = dict(v=V, user=user, device=public(device), issued_at=now()); body["signature"] = b64(_priv(Ed25519PrivateKey, root["sign_private"]).sign(canon(body)))
    return body

def verify_certificate(cert, root_public):
    sig, body = unb64(cert["signature"]), {k:v for k,v in cert.items() if k != "signature"}; _pub(Ed25519PublicKey, root_public).verify(sig, canon(body))
    if body["v"] != V: raise ValueError(f"Unsupported certificate version {body['v']}")
    return body

def row_proof(device,user,workspace,epoch,row,previous=None):
    claim={"row_kind":row["kind"],"row_id":row["id"],"encoding_v":row["v"],"content_hash":digest(row),"previous_revision":previous,"state":row["state"]}; body={"v":1,"kind":"row.proof","workspace":workspace,**claim,"revision":digest({"v":1,**claim}),"author_user_id":user,"author_device_id":device["id"],"authorization_epoch":epoch}; body["signature"]=b64(_priv(Ed25519PrivateKey,device["sign_private"]).sign(canon(body))); return body

def verify_row_proof(value,row,cert,root_public):
    try:
        signed={k:v for k,v in value.items() if k!="signature"}; device=verify_certificate(cert,root_public)["device"]; previous=value["previous_revision"]
        claim={k:value[k] for k in ("row_kind","row_id","encoding_v","content_hash","previous_revision","state")}
        if set(row)!={"v","kind","id","state","data"} or row["kind"] not in ROW_FIELDS_V1 or not isinstance(row["id"],str) or not row["id"] or row["v"]!=1 or row["state"] not in ("active","deleted") or row["data"] is not None and (row["state"]!="active" or set(row["data"])!=set(ROW_FIELDS_V1[row["kind"]])) or row["state"]=="deleted" and row["data"] is not None: raise ValueError
        if set(value)!=ROW_PROOF_FIELDS or value["v"]!=1 or value["kind"]!="row.proof" or not isinstance(value["workspace"],str) or not value["workspace"] or (value["row_kind"],value["row_id"],value["encoding_v"],value["content_hash"],value["state"])!=(row["kind"],row["id"],row["v"],digest(row),row["state"]) or value["revision"]!=digest({"v":1,**claim}) or previous is not None and (not isinstance(previous,str) or len(previous)!=64 or any(c not in "0123456789abcdef" for c in previous) or previous==value["revision"]) or not isinstance(value["authorization_epoch"],int) or isinstance(value["authorization_epoch"],bool) or value["authorization_epoch"]<1 or (cert["user"],device["id"],public_id(root_public),public_id(device["sign_public"]))!=(value["author_user_id"],value["author_device_id"],value["author_user_id"],value["author_device_id"]): raise ValueError
        _pub(Ed25519PublicKey,device["sign_public"]).verify(unb64(value["signature"]),canon(signed)); return value
    except (InvalidSignature,KeyError,TypeError,ValueError) as e: raise ValueError("invalid row proof") from e

def event(device, seq, kind, entity, payload, parents=(), observed_at=None, payload_v=1):
    body = dict(v=V, kind=kind, entity=entity, revision=digest(payload), author=device["id"], seq=seq, parents=list(parents), observed_at=observed_at or now(), payload_v=payload_v, payload=payload)
    body["id"] = digest(body); body["signature"] = b64(_priv(Ed25519PrivateKey, device["sign_private"]).sign(canon(body)))
    return body

def verify_event(value, sign_public):
    if value["v"] != V: raise ValueError(f"Unsupported event version {value['v']}")
    sig, signed = unb64(value["signature"]), {k:v for k,v in value.items() if k != "signature"}; _pub(Ed25519PublicKey, sign_public).verify(sig, canon(signed))
    body = {k:v for k,v in signed.items() if k != "id"}
    if digest(body) != value["id"] or digest(value["payload"]) != value["revision"]: raise ValueError("Invalid event digest")
    return value
PURGE_FIELDS={"v","kind","workspace","event","author","epoch","seq","parents","event_kind","payload_v","superseded_by","signature"}
def purge_certificate(device,workspace,target,parents,anchor):
    body=dict(v=V,kind="event.purge",workspace=workspace,event=target["event"],author=target["author"],epoch=target["epoch"],seq=target["seq"],parents=list(parents),event_kind=target["kind"],payload_v=target["payload_v"],superseded_by=anchor["event"]); body["signature"]=b64(_priv(Ed25519PrivateKey,device["sign_private"]).sign(canon(body))); return body
def verify_purge(value,sign_public):
    if set(value)!=PURGE_FIELDS or value["v"]!=V or value["kind"]!="event.purge" or value["event_kind"]!="memory.canonical" or value["payload_v"]!=1 or not isinstance(value["workspace"],str) or not isinstance(value["author"],str) or len(value["author"])!=32 or any(not isinstance(value[k],str) or len(value[k])!=64 for k in ("event","superseded_by")) or any(not isinstance(value[k],int) or isinstance(value[k],bool) or value[k]<1 for k in ("epoch","seq")) or not isinstance(value["parents"],list) or len(value["parents"])!=(value["seq"]>1) or any(not isinstance(p,str) or len(p)!=64 for p in value["parents"]): raise ValueError("invalid purge certificate")
    try: _pub(Ed25519PublicKey,sign_public).verify(unb64(value["signature"]),canon({k:v for k,v in value.items() if k!="signature"}))
    except (InvalidSignature,KeyError,TypeError,ValueError) as e: raise ValueError("invalid purge certificate") from e
    return value
def signer(devices,author): value=devices[author]["sign_public"]; return value if public_id(value)==author else (_ for _ in ()).throw(ValueError("device signing key mismatch"))
def material_event(value,devices=None,device=None):
    while value["kind"]=="history.republish":
        p=value["payload"]; "sealed" in p or (_ for _ in ()).throw(ValueError("unsealed history event rejected")); value=open_history(p["sealed"],device,value["entity"]) if device and device["id"] in p["sealed"]["keys"] else None
        if value is None: return None
        if devices is not None: verify_event(value,signer(devices,value["author"]))
    return value
def sign_control(device,body): return {**body,"control_signature":b64(_priv(Ed25519PrivateKey,device["sign_private"]).sign(canon(body)))}
def seal_history(value,devices,context): key,nonce=os.urandom(32),os.urandom(12); return {"nonce":b64(nonce),"ciphertext":b64(AESGCM(key).encrypt(nonce,canon(value),context.encode())),"keys":{d["id"]:seal_key(key,d["box_public"],context) for d in devices}}
def open_history(value,device,context): return json.loads(AESGCM(open_key(value["keys"][device["id"]],device["box_private"],context)).decrypt(unb64(value["nonce"]),unb64(value["ciphertext"]),context.encode()))

def seal_event(value, workspace, epoch, key):
    nonce = os.urandom(12); header = dict(v=V, workspace=workspace, epoch=epoch, event=value["id"], author=value["author"], seq=value["seq"], parents=value["parents"], nonce=b64(nonce))
    return {**header, "ciphertext":b64(AESGCM(key).encrypt(nonce, canon(value), canon(header)))}

def open_event(envelope, key, sign_public):
    if envelope["v"] != V: raise ValueError(f"Unsupported envelope version {envelope['v']}")
    header = {k:envelope[k] for k in ("v", "workspace", "epoch", "event", "author", "seq", "parents", "nonce")}; value = json.loads(AESGCM(key).decrypt(unb64(header["nonce"]), unb64(envelope["ciphertext"]), canon(header)))
    verify_event(value, sign_public)
    if (value["id"], value["author"], value["seq"], value["parents"]) != (header["event"], header["author"], header["seq"], header["parents"]): raise ValueError("Envelope header mismatch")
    return value

def seal_replica(row,proof,workspace,epoch,key,uploader):
    if proof["revision"]!=digest({"v":1,**{k:proof[k] for k in ("row_kind","row_id","encoding_v","content_hash","previous_revision","state")}}) or proof["content_hash"]!=digest(row): raise ValueError("row replica proof mismatch")
    nonce=os.urandom(12); header={"v":1,"kind":"row.replica","workspace":workspace,"revision":proof["revision"],"epoch":epoch,"uploader":uploader,"nonce":b64(nonce)}; return {**header,"ciphertext":b64(AESGCM(key).encrypt(nonce,canon({"row":row,"proof":proof}),canon(header)))}

def open_replica(value,key):
    try:
        if set(value)!={"v","kind","workspace","revision","epoch","uploader","nonce","ciphertext"} or value["v"]!=1 or value["kind"]!="row.replica": raise ValueError
        header={k:value[k] for k in ("v","kind","workspace","revision","epoch","uploader","nonce")}; body=json.loads(AESGCM(key).decrypt(unb64(value["nonce"]),unb64(value["ciphertext"]),canon(header)))
        if set(body)!={"row","proof"} or body["proof"]["revision"]!=value["revision"] or body["proof"]["content_hash"]!=digest(body["row"]): raise ValueError
        return body
    except (InvalidTag,KeyError,TypeError,ValueError) as e: raise ValueError("invalid row replica") from e

def _wrap_key(shared, context): return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"convos-key-v1:" + context.encode()).derive(shared)
def seal_key(key, recipient_public, context):
    ephemeral, nonce = X25519PrivateKey.generate(), os.urandom(12); shared = ephemeral.exchange(_pub(X25519PublicKey, recipient_public)); aad = canon(dict(v=V, context=context, ephemeral=_raw(ephemeral.public_key()), nonce=b64(nonce)))
    return dict(v=V, context=context, ephemeral=_raw(ephemeral.public_key()), nonce=b64(nonce), ciphertext=b64(AESGCM(_wrap_key(shared, context)).encrypt(nonce, key, aad)))

def open_key(value, recipient_private, context=None):
    if value["v"] != V or context is not None and value["context"]!=context: raise ValueError("Unsupported or mismatched key envelope")
    shared = _priv(X25519PrivateKey, recipient_private).exchange(_pub(X25519PublicKey, value["ephemeral"])); aad = canon({k:value[k] for k in ("v", "context", "ephemeral", "nonce")})
    return AESGCM(_wrap_key(shared, value["context"])).decrypt(unb64(value["nonce"]), unb64(value["ciphertext"]), aad)

def recovery_bundle(payload, recovery=None):
    key, nonce = recovery or os.urandom(32), os.urandom(12); header = dict(v=V, kdf="raw-256", nonce=b64(nonce)); header["ciphertext"] = b64(AESGCM(key).encrypt(nonce, canon(payload), canon(header)))
    return b64(key), header

def recover(value, recovery):
    if value["v"] != V or value["kdf"] != "raw-256": raise ValueError("Unsupported recovery bundle")
    header = {k:value[k] for k in ("v", "kdf", "nonce")}; return json.loads(AESGCM(unb64(recovery)).decrypt(unb64(value["nonce"]), unb64(value["ciphertext"]), canon(header)))

def fingerprint(key, value): return hmac.new(key, value if isinstance(value, bytes) else canon(value), hashlib.sha256).hexdigest()
