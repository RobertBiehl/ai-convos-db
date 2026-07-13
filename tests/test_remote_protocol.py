import copy, json

import pytest
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from ai_convos_remote.protocol import (b64, certificate, digest, event, fingerprint, identity, open_event, open_key,
                                       material_event, public, recover, recovery_bundle, seal_event, seal_history, seal_key, verify_certificate,
                                       verify_event)

def fixed_identity():
    sign, box = Ed25519PrivateKey.from_private_bytes(bytes(range(32))), X25519PrivateKey.from_private_bytes(bytes(range(32, 64)))
    return dict(id=digest(sign.public_key().public_bytes_raw())[:32], name="laptop", sign_private=b64(sign.private_bytes_raw()),
                sign_public=b64(sign.public_key().public_bytes_raw()), box_private=b64(box.private_bytes_raw()), box_public=b64(box.public_key().public_bytes_raw()))

def test_identity_certificate_and_event_vector():
    root, device = identity("root"), fixed_identity()
    cert = certificate(root, "u1", device)
    assert verify_certificate(cert, root["sign_public"])["device"] == public(device)
    value = event(device, 1, "message.record", "m1", {"content":"hello"}, [], "2026-01-01T00:00:00.000000Z")
    assert value["id"] == "6ee3c8b0416343604b05cad40bed9f9b1d5ebde1af2fbe76f08ac3731d7da1d2"
    assert verify_event(value, device["sign_public"])["payload"]["content"] == "hello"


def test_event_encryption_tamper_signature_and_header_binding():
    device, other, key = identity(), identity(), bytes(range(32))
    value = event(device, 7, "conversation.record", "c1", {"title":"private"}, [], "2026-01-01T00:00:00Z")
    envelope = seal_event(value, "w1", 3, key)
    assert "private" not in json.dumps(envelope) and open_event(envelope, key, device["sign_public"]) == value
    bad = copy.deepcopy(envelope); bad["workspace"] = "w2"
    with pytest.raises(InvalidTag): open_event(bad, key, device["sign_public"])
    with pytest.raises(InvalidSignature): open_event(envelope, key, other["sign_public"])


def test_key_envelope_recovery_and_private_fingerprint():
    device, key = identity(), bytes(reversed(range(32)))
    wrapped = seal_key(key, device["box_public"], "workspace:w1:epoch:2")
    assert open_key(wrapped, device["box_private"]) == key
    with pytest.raises(ValueError,match="mismatched"): open_key(wrapped,device["box_private"],"workspace:w2:epoch:2")
    recovery, bundle = recovery_bundle({"workspace_keys":{"w1:2":key.hex()}}, bytes([9])*32)
    assert recover(bundle, recovery)["workspace_keys"]["w1:2"] == key.hex()
    assert fingerprint(key, "https://example/repo") == "07e9b5f5727490c3d14b5ed15cdfe6f9bb3c83a04995f9e8081a5b8fa2eb6413"


def test_replay_under_another_identity_and_payload_mutation_rejected():
    a, b = identity("a"), identity("b")
    value = event(a, 1, "x.future", "x", {"unknown":True}, [], "2026-01-01T00:00:00Z")
    forged = {**value, "author":b["id"]}
    with pytest.raises((InvalidSignature, ValueError)): verify_event(forged, a["sign_public"])
    changed = copy.deepcopy(value); changed["payload"]["unknown"] = False
    with pytest.raises((InvalidSignature, ValueError)): verify_event(changed, a["sign_public"])


def test_nested_history_verifies_self_certifying_authors():
    source,admin,recipient=identity("source"),identity("admin"),identity("recipient"); inner=event(source,1,"x.future","x",{"value":1}); middle=event(admin,1,"history.republish","history:1",{"sealed":seal_history(inner,[recipient],"history:1")}); outer=event(admin,2,"history.republish","history:2",{"sealed":seal_history(middle,[recipient],"history:2")}); devices={d["id"]:public(d) for d in (source,admin,recipient)}
    assert material_event(outer,devices,recipient)==inner
    devices[source["id"]]=public(admin)
    with pytest.raises(ValueError,match="key mismatch"): material_event(outer,devices,recipient)
    with pytest.raises(ValueError,match="unsealed"): material_event(event(admin,3,"history.republish","legacy",{"event":inner}),devices,recipient)
