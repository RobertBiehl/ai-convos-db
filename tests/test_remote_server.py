import copy, json

import pytest
from ai_convos_remote.protocol import certificate, event, identity, seal_event, seal_key
from ai_convos_remote_server import action, connect


def account(db, name):
    root, dev = identity(name + " root"), identity(name + " laptop"); user = root["id"]
    result = action(db, {"op":"register","user_name":name,"root_public":root["sign_public"],"certificate":certificate(root,user,dev),"recovery":{"ciphertext":"opaque"}})
    return {"root":root,"device":dev,"user":user,"token":result["token"]}


def test_personal_workspace_idempotency_and_ciphertext_only(tmp_path):
    db = connect(tmp_path/"server.db"); a = account(db,"alice"); key = bytes(range(32)); ws = "personal-alice"
    action(db,{"op":"create","workspace":ws,"kind":"personal","envelope":seal_key(key,a["device"]["box_public"],f"workspace:{ws}:epoch:1")},a["token"])
    value = event(a["device"],1,"message.record","m1",{"content":"server must not see this"},[],"2026-01-01T00:00:00Z"); envelope = seal_event(value,ws,1,key)
    first = action(db,{"op":"upload","envelope":envelope},a["token"]); second = action(db,{"op":"upload","envelope":envelope},a["token"])
    assert first["created"] and not second["created"] and first["cursor"] == second["cursor"]
    assert "server must not see this" not in (tmp_path/"server.db").read_bytes().decode(errors="ignore")
    assert action(db,{"op":"pull","workspace":ws,"after":0},a["token"])["events"][0]["envelope"] == envelope
    bad = copy.deepcopy(envelope); bad["ciphertext"] = bad["ciphertext"][:-1] + ("A" if bad["ciphertext"][-1] != "A" else "B")
    with pytest.raises(ValueError,match="different ciphertext"): action(db,{"op":"upload","envelope":bad},a["token"])
    same_seq = seal_event(event(a["device"],1,"message.record","m2",{"content":"different"},[],"2026-01-02T00:00:00Z"),ws,1,key)
    with pytest.raises(Exception): action(db,{"op":"upload","envelope":same_seq},a["token"])


def test_team_add_default_history_grant_remove_and_rotation(tmp_path):
    db = connect(tmp_path/"server.db"); a, b = account(db,"alice"), account(db,"bob"); ws = "team"; k1,k2,k3 = bytes([1])*32,bytes([2])*32,bytes([3])*32
    action(db,{"op":"create","workspace":ws,"kind":"team","envelope":seal_key(k1,a["device"]["box_public"],f"workspace:{ws}:epoch:1")},a["token"])
    old = seal_event(event(a["device"],1,"message.record","old",{"content":"before bob"},[],"2026-01-01T00:00:00Z"),ws,1,k1); action(db,{"op":"upload","envelope":old},a["token"])
    env2 = {d["id"]:seal_key(k2,d["box_public"],f"workspace:{ws}:epoch:2") for d in (a["device"],b["device"])}
    action(db,{"op":"rotate","workspace":ws,"epoch":2,"members":{a["user"]:"admin",b["user"]:"member"},"envelopes":env2},a["token"])
    assert action(db,{"op":"pull","workspace":ws,"after":0},b["token"])["events"] == []
    current = seal_event(event(a["device"],2,"message.record","new",{"content":"after bob"},[old["event"]],"2026-01-02T00:00:00Z"),ws,2,k2); action(db,{"op":"upload","envelope":current},a["token"])
    assert [x["envelope"]["event"] for x in action(db,{"op":"pull","workspace":ws,"after":0},b["token"])["events"]] == [current["event"]]
    old_for_b = seal_key(k1,b["device"]["box_public"],f"workspace:{ws}:epoch:1")
    action(db,{"op":"grant_all","workspace":ws,"user":b["user"],"envelopes":{"1":{b["device"]["id"]:old_for_b}}},a["token"])
    assert len(action(db,{"op":"pull","workspace":ws,"after":0},b["token"])["events"]) == 2
    env3 = {a["device"]["id"]:seal_key(k3,a["device"]["box_public"],f"workspace:{ws}:epoch:3")}
    action(db,{"op":"rotate","workspace":ws,"epoch":3,"members":{a["user"]:"admin"},"envelopes":env3},a["token"])
    with pytest.raises(PermissionError): action(db,{"op":"pull","workspace":ws,"after":0},b["token"])


def test_device_certificate_recovery_and_author_acl(tmp_path):
    db = connect(tmp_path/"server.db"); a = account(db,"alice")
    assert action(db,{"op":"recovery_fetch","user":"alice"})["bundle"] == {"ciphertext":"opaque"}
    second = identity("desktop"); registered = action(db,{"op":"register","user_name":"alice","root_public":a["root"]["sign_public"],"certificate":certificate(a["root"],a["user"],second)})
    assert registered["device"] == second["id"]
    ws,key = "personal",bytes([5])*32; action(db,{"op":"create","workspace":ws,"kind":"personal","envelope":seal_key(key,a["device"]["box_public"],f"workspace:{ws}:epoch:1")},a["token"])
    forged = seal_event(event(second,1,"x.future","x",{},[],"2026-01-01T00:00:00Z"),ws,1,key)
    with pytest.raises(PermissionError,match="author"): action(db,{"op":"upload","envelope":forged},a["token"])


def test_large_events_are_manifested_then_fetched(tmp_path):
    db=connect(tmp_path/"server.db"); a=account(db,"alice"); ws,key="personal",bytes([7])*32; action(db,{"op":"create","workspace":ws,"kind":"personal","envelope":seal_key(key,a["device"]["box_public"],f"workspace:{ws}:epoch:1")},a["token"])
    env=seal_event(event(a["device"],1,"future.large","large",{"blob":"x"*70000},[],"2026-01-01T00:00:00Z"),ws,1,key); action(db,{"op":"upload","envelope":env},a["token"]); item=action(db,{"op":"pull","workspace":ws,"after":0},a["token"])["events"][0]
    assert item["lazy"] and "envelope" not in item and action(db,{"op":"fetch","workspace":ws,"event":env["event"]},a["token"])["envelope"]==env


def test_restart_preserves_events_tokens_and_idempotency(tmp_path):
    path=tmp_path/"server.db"; db=connect(path); a=account(db,"alice"); ws,key="personal",bytes([8])*32; action(db,{"op":"create","workspace":ws,"kind":"personal","envelope":seal_key(key,a["device"]["box_public"],f"workspace:{ws}:epoch:1")},a["token"]); env=seal_event(event(a["device"],1,"x","x",{},[],"2026-01-01T00:00:00Z"),ws,1,key); action(db,{"op":"upload","envelope":env},a["token"]); db.close(); db=connect(path)
    assert action(db,{"op":"upload","envelope":env},a["token"])["created"] is False and action(db,{"op":"pull","workspace":ws,"after":0},a["token"])["events"][0]["envelope"]==env


def test_registration_rejects_public_key_identity_mismatch(tmp_path):
    db=connect(tmp_path/"server.db"); root,device=identity("root"),identity("device"); cert=certificate(root,"not-root-id",device)
    with pytest.raises(ValueError,match="identity id"): action(db,{"op":"register","user_name":"bad","root_public":root["sign_public"],"certificate":cert})
