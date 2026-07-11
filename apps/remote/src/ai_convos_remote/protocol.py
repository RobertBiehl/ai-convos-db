"""Canonical signed/encrypted event protocol. Wire format v1; server sees envelopes only."""
import base64, hashlib, hmac, json, os
from datetime import datetime, timezone

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

V = 1
def canon(v): return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
def b64(v): return base64.urlsafe_b64encode(v).decode().rstrip("=")
def unb64(v): return base64.urlsafe_b64decode(v + "=" * (-len(v) % 4))
def digest(v): return hashlib.sha256(v if isinstance(v, bytes) else canon(v)).hexdigest()
def public_id(value): return digest(unb64(value))[:32]
def now(): return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
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
    nonce = os.urandom(12); header = dict(v=V, workspace=workspace, epoch=epoch, event=value["id"], author=value["author"], seq=value["seq"], nonce=b64(nonce))
    return {**header, "ciphertext":b64(AESGCM(key).encrypt(nonce, canon(value), canon(header)))}

def open_event(envelope, key, sign_public):
    if envelope["v"] != V: raise ValueError(f"Unsupported envelope version {envelope['v']}")
    header = {k:envelope[k] for k in ("v", "workspace", "epoch", "event", "author", "seq", "nonce")}; value = json.loads(AESGCM(key).decrypt(unb64(header["nonce"]), unb64(envelope["ciphertext"]), canon(header)))
    verify_event(value, sign_public)
    if (value["id"], value["author"], value["seq"]) != (header["event"], header["author"], header["seq"]): raise ValueError("Envelope header mismatch")
    return value

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
