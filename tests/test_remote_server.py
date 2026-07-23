import copy, json

import pytest
from ai_convos_remote.control import record, sign, state_hash
from ai_convos_remote.protocol import certificate, digest, event, identity, seal_event, seal_key, sign_control
from ai_convos_remote_server import action, connect


def account(db, name):
    root, dev = identity(name + " root"), identity(name + " laptop"); user = root["id"]
    result = action(db, {"op":"register","user_name":name,"root_public":root["sign_public"],"certificate":certificate(root,user,dev),"recovery":{"ciphertext":"opaque"}})
    return {"root":root,"device":dev,"user":user,"token":result["token"]}
def device_record(a): return record(a["user"],a["root"]["sign_public"],a["device"],certificate(a["root"],a["user"],a["device"]))
def create_ws(db,a,ws,key,kind):
    state=sign(a["device"],{"v":1,"kind":"workspace.state","workspace":ws,"scope":kind,"revision":1,"prev":None,"epoch":1,"key_commitment":digest(key),"members":{a["user"]:{"role":"admin","joined":1,"history_from":1,"selected":[]}},"devices":{a["device"]["id"]:device_record(a)},"removed":[],"action":"create","approval":None,"approved_at":0})
    action(db,sign_control(a["device"],{"op":"create","workspace":ws,"kind":kind,"control":state,"envelopes":{a["device"]["id"]:seal_key(key,a["device"]["box_public"],f"workspace:{ws}:epoch:1")}}),a["token"]); return state
def rotate_ws(db,a,previous,key,people):
    epoch=previous["epoch"]+1; members={p["user"]:{"role":role,"joined":previous["members"].get(p["user"],{"joined":epoch})["joined"],"history_from":previous["members"].get(p["user"],{"history_from":epoch})["history_from"],"selected":[]} for p,role in people}; devices={p["device"]["id"]:previous["devices"].get(p["device"]["id"],device_record(p)) for p,_ in people}; state=sign(a["device"],{"v":1,"kind":"workspace.state","workspace":previous["workspace"],"scope":previous["scope"],"revision":previous["revision"]+1,"prev":state_hash(previous),"epoch":epoch,"key_commitment":digest(key),"members":members,"devices":devices,"removed":sorted(set(previous["removed"])|set(previous["devices"])-set(devices)),"action":"membership","approval":None,"approved_at":0}); envs={p["device"]["id"]:seal_key(key,p["device"]["box_public"],f"workspace:{previous['workspace']}:epoch:{epoch}") for p,_ in people}; action(db,sign_control(a["device"],{"op":"rotate","workspace":previous["workspace"],"control":state,"envelopes":envs}),a["token"]); return state
def history_ws(a,previous,user):
    members={**previous["members"],user:{**previous["members"][user],"history_from":1}}; return sign(a["device"],{**{k:v for k,v in previous.items() if k not in ("signature","author")},"revision":previous["revision"]+1,"prev":state_hash(previous),"members":members,"action":"history","approval":None,"approved_at":0})


def test_personal_workspace_idempotency_and_ciphertext_only(tmp_path):
    db = connect(tmp_path/"server.db"); a = account(db,"alice"); key = bytes(range(32)); ws = "personal-alice"
    create_ws(db,a,ws,key,"personal")
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
    state=create_ws(db,a,ws,k1,"team")
    old = seal_event(event(a["device"],1,"message.record","old",{"content":"before bob"},[],"2026-01-01T00:00:00Z"),ws,1,k1); action(db,{"op":"upload","envelope":old},a["token"])
    state=rotate_ws(db,a,state,k2,((a,"admin"),(b,"member")))
    assert action(db,{"op":"pull","workspace":ws,"after":0},b["token"])["events"] == []
    current = seal_event(event(a["device"],2,"message.record","new",{"content":"after bob"},[old["event"]],"2026-01-02T00:00:00Z"),ws,2,k2); action(db,{"op":"upload","envelope":current},a["token"])
    assert [x["envelope"]["event"] for x in action(db,{"op":"pull","workspace":ws,"after":0},b["token"])["events"]] == [current["event"]]
    old_for_b = seal_key(k1,b["device"]["box_public"],f"workspace:{ws}:epoch:1")
    state=history_ws(a,state,b["user"]); action(db,sign_control(a["device"],{"op":"grant_all","workspace":ws,"user":b["user"],"control":state,"envelopes":{"1":{b["device"]["id"]:old_for_b},"2":{b["device"]["id"]:seal_key(k2,b["device"]["box_public"],f"workspace:{ws}:epoch:2")}}}),a["token"])
    assert len(action(db,{"op":"pull","workspace":ws,"after":0},b["token"])["events"]) == 2
    rotate_ws(db,a,state,k3,((a,"admin"),))
    with pytest.raises(PermissionError): action(db,{"op":"pull","workspace":ws,"after":0},b["token"])


def test_device_certificate_recovery_and_author_acl(tmp_path):
    db = connect(tmp_path/"server.db"); a = account(db,"alice")
    with pytest.raises(PermissionError,match="signature"): action(db,{"op":"create","workspace":"stolen","kind":"team","envelope":{}},a["token"])
    with pytest.raises(PermissionError,match="signature"): action(db,{"op":"recovery","bundle":{}},a["token"])
    assert action(db,{"op":"recovery_fetch","user":"alice"})["bundle"] == {"ciphertext":"opaque"}
    second = identity("desktop"); registered = action(db,{"op":"register","user_name":"alice","root_public":a["root"]["sign_public"],"certificate":certificate(a["root"],a["user"],second)})
    assert registered["device"] == second["id"]
    ws,key = "personal",bytes([5])*32; create_ws(db,a,ws,key,"personal")
    forged = seal_event(event(second,1,"x.future","x",{},[],"2026-01-01T00:00:00Z"),ws,1,key)
    with pytest.raises(PermissionError,match="author"): action(db,{"op":"upload","envelope":forged},a["token"])


def test_large_events_are_manifested_then_fetched(tmp_path):
    db=connect(tmp_path/"server.db"); a=account(db,"alice"); ws,key="personal",bytes([7])*32; create_ws(db,a,ws,key,"personal")
    env=seal_event(event(a["device"],1,"future.large","large",{"blob":"x"*70000},[],"2026-01-01T00:00:00Z"),ws,1,key); action(db,{"op":"upload","envelope":env},a["token"]); item=action(db,{"op":"pull","workspace":ws,"after":0},a["token"])["events"][0]
    assert item["lazy"] and "envelope" not in item and action(db,{"op":"fetch","workspace":ws,"event":env["event"]},a["token"])["envelope"]==env


def test_restart_preserves_events_tokens_and_idempotency(tmp_path):
    path=tmp_path/"server.db"; db=connect(path); a=account(db,"alice"); ws,key="personal",bytes([8])*32; create_ws(db,a,ws,key,"personal"); env=seal_event(event(a["device"],1,"x","x",{},[],"2026-01-01T00:00:00Z"),ws,1,key); action(db,{"op":"upload","envelope":env},a["token"]); db.close(); db=connect(path)
    assert action(db,{"op":"upload","envelope":env},a["token"])["created"] is False and action(db,{"op":"pull","workspace":ws,"after":0},a["token"])["events"][0]["envelope"]==env


def test_registration_rejects_public_key_identity_mismatch(tmp_path):
    db=connect(tmp_path/"server.db"); root,device=identity("root"),identity("device"); cert=certificate(root,"not-root-id",device)
    with pytest.raises(ValueError,match="identity id"): action(db,{"op":"register","user_name":"bad","root_public":root["sign_public"],"certificate":cert})
