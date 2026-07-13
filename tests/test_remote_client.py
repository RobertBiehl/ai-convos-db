import copy, json, os

import duckdb
import pytest
from cryptography.exceptions import InvalidSignature
from ai_convos.cli import init_schema
from ai_convos_remote import (_upload_batches, add_member, approve_devices, connect, create, fetch_lazy, grant_all, grant_selected, load, pull, publish, refresh, remove_device,
                              setup_client, upload, workspace)
from ai_convos_remote.projection import rebuild, scan
from ai_convos_remote.protocol import event, identity, seal_history, sign_control
from ai_convos_remote_server import action, connect as server_connect


def transport(db):
    def call(cfg,body,auth=True): return action(db,body,cfg.get("token") if auth else None)
    return call
def conversation(title="shared",id="c"):
    cols=["id","source","title","created_at","updated_at","model","cwd","git_branch","project_id","metadata"]
    return {"kind":"conversation.record","entity":f"conversations:{id}","payload":{"table":"conversations","columns":cols,"row":[id,"codex",title,"2026-01-01","2026-01-01",None,None,None,None,"{}"]}}

def test_upload_batches_bound_count_and_wire_size():
    row=lambda size:(None,None,None,"x"*size)
    assert [len(x) for x in _upload_batches([row(1)]*501,1000)]==[500,1] and [len(x) for x in _upload_batches([row(6)]*2,10)]==[1,1]


def test_personal_recovery_multidevice_delivery_and_replay(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a,b=tmp_path/"a",tmp_path/"b"
    alice,recovery=setup_client("http://server","alice","laptop",root=a); ws=workspace(alice,"Personal"); state_a=connect(a/"remote/state.db"); publish(alice,state_a,ws,conversation(),a); upload(alice,state_a,a)
    desktop,_=setup_client("http://server","alice","desktop",recovery,root=b); state_b=connect(b/"remote/state.db"); pull(desktop,state_b,b); pull(desktop,state_b,b)
    db=duckdb.connect(str(b/"data/convos.db"),read_only=True); assert db.execute("SELECT title FROM conversations").fetchall()==[("shared",)]; db.close()
    assert len(load(b)["keys"])==2 and server.execute("SELECT epoch FROM workspaces WHERE id=?",(ws,)).fetchone()[0]==2
    assert os.stat(a/"remote").st_mode&0o777==0o700 and os.stat(a/"remote/config.json").st_mode&0o777==0o600 and os.stat(a/"remote/state.db").st_mode&0o777==0o600


def test_device_certificates_reject_relay_key_substitution_and_upgrade_legacy(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); direct=transport(server); monkeypatch.setattr("ai_convos_remote.request",direct); a,b,c=tmp_path/"a",tmp_path/"b",tmp_path/"c"; alice,recovery=setup_client("http://server","alice",root=a); bob,_=setup_client("http://server","bob",root=b); team=create(alice,"Team","team",a); server.execute("DELETE FROM device_certificates WHERE device=?",(alice["device"]["id"],)); server.commit(); desktop,_=setup_client("http://server","alice","desktop",recovery,root=c); state=refresh(desktop,c); assert server.execute("SELECT COUNT(*) FROM device_certificates WHERE device IN (?,?)",(alice["device"]["id"],desktop["device"]["id"])).fetchone()[0]==2 and next(w for w in state["workspaces"] if w["kind"]=="personal")["device_authorized"]; attacker=identity("attacker")
    def tamper(op,device):
        def call(cfg,body,auth=True):
            result=copy.deepcopy(direct(cfg,body,auth))
            if body["op"]==op:
                for workspace_ in result.get("workspaces",[]):
                    for found in workspace_["devices"]:
                        if found["id"]==device: found["box_public"]=attacker["box_public"]
                for found in result.get("devices",[]): found["box_public"]=attacker["box_public"]
            return result
        return call
    monkeypatch.setattr("ai_convos_remote.request",tamper("directory",""))
    with pytest.raises(ValueError,match="certificate"): add_member(alice,team,"bob",root=a)
    monkeypatch.setattr("ai_convos_remote.request",tamper("state",alice["device"]["id"]))
    with pytest.raises(ValueError,match="certificate"): approve_devices(alice,team,a)
    monkeypatch.setattr("ai_convos_remote.request",direct); add_member(alice,team,bob["user"],root=a); server.execute("DELETE FROM device_certificates WHERE device=?",(bob["device"]["id"],)); server.commit(); add_member(alice,team,bob["user"],True,root=a); assert server.execute("SELECT active FROM members WHERE workspace=? AND user_id=?",(team,bob["user"])).fetchone()[0]==0
    mallory,_=setup_client("http://server","mallory",root=tmp_path/"m"); refresh(bob,b); server.execute("UPDATE users SET name=? WHERE id=?",(bob["user"],mallory["user"])); server.commit(); add_member(alice,team,bob["user"],root=a); assert server.execute("SELECT active FROM members WHERE workspace=? AND user_id=?",(team,bob["user"])).fetchone()[0]==1 and not server.execute("SELECT 1 FROM members WHERE workspace=? AND user_id=?",(team,mallory["user"])).fetchone()
    def substitute(cfg,body,auth=True):
        result=copy.deepcopy(direct(cfg,{**body,"user":mallory["user"]},auth)) if body["op"]=="directory" else direct(cfg,body,auth)
        if body["op"]=="directory": result["users"][0]["name"]=bob["user"]
        return result
    monkeypatch.setattr("ai_convos_remote.request",substitute)
    with pytest.raises(ValueError,match="directory user"): add_member(alice,team,bob["user"],root=a)


def test_team_default_selected_complete_history_and_removal(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a,b=tmp_path/"a",tmp_path/"b"
    alice,_=setup_client("http://server","alice","laptop",root=a); bob,_=setup_client("http://server","bob","desktop",root=b); team=create(alice,"Team","team",a); sa,sb=connect(a/"remote/state.db"),connect(b/"remote/state.db")
    old=publish(alice,sa,team,conversation("before bob"),a); upload(alice,sa,a); add_member(alice,team,"bob",root=a); bob=load(b); pull(bob,sb,b); assert not (b/"data/convos.db").exists()
    publish(alice,sa,team,conversation("after bob","new"),a); upload(alice,sa,a); bob=load(b); pull(bob,sb,b); assert duckdb.connect(str(b/"data/convos.db"),read_only=True).execute("SELECT title FROM conversations").fetchall()==[("after bob",)]
    assert grant_selected(alice,sa,team,"bob",[old],a)==1; bob=load(b); pull(bob,sb,b); assert {r[0] for r in duckdb.connect(str(b/"data/convos.db"),read_only=True).execute("SELECT title FROM conversations").fetchall()}=={"before bob","after bob"}
    incomplete={"op":"grant_all","workspace":team,"user":bob["user"],"envelopes":{}}
    with pytest.raises(ValueError,match="every workspace epoch"): action(server,sign_control(alice["device"],incomplete),alice["token"])
    future={**incomplete,"envelopes":{"999":{bob["device"]["id"]:{}}}}
    with pytest.raises(ValueError,match="outside"): action(server,sign_control(alice["device"],future),alice["token"])
    assert grant_all(alice,team,"bob",a)>=2; bob=load(b); pull(bob,sb,b); assert any(name.endswith(":1") for name in load(b)["keys"])
    add_member(alice,team,"bob",True,root=a); bob=load(b); pull(bob,sb,b); assert team not in {w["id"] for w in load(b)["server_state"]["workspaces"]} and f"{team}:3" not in load(b)["keys"]
    rebuild(b/"rebuilt.db",sb,device=bob["device"]); assert {r[0] for r in duckdb.connect(str(b/"rebuilt.db"),read_only=True).execute("SELECT title FROM conversations").fetchall()}=={"before bob","after bob"}


def test_unknown_events_survive_client_projection(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a=tmp_path/"a"; cfg,_=setup_client("http://server","alice",root=a); ws=workspace(cfg,"Personal"); state=connect(a/"remote/state.db")
    eid=publish(cfg,state,ws,{"kind":"future.opaque","entity":"future:1","payload":{"new_field":[1,2,3]}},a); upload(cfg,state,a)
    assert json.loads(state.execute("SELECT event_json FROM event_log WHERE event=?",(eid,)).fetchone()[0])["payload"]["new_field"]==[1,2,3]


def test_large_record_is_lazy_until_explicit_fetch(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a,b=tmp_path/"a",tmp_path/"b"; alice,recovery=setup_client("http://server","alice",root=a); desktop,_=setup_client("http://server","alice","desktop",recovery,root=b); alice=load(a); ws=workspace(alice,"Personal"); state_a,state_b=connect(a/"remote/state.db"),connect(b/"remote/state.db")
    publish(alice,state_a,ws,conversation("x"*70000),a); upload(alice,state_a,a); pull(desktop,state_b,b); assert state_b.execute("SELECT COUNT(*) FROM lazy_events").fetchone()[0]==1 and not (b/"data/convos.db").exists()
    assert fetch_lazy(desktop,state_b,root=b)==1 and duckdb.connect(str(b/"data/convos.db"),read_only=True).execute("SELECT length(title) FROM conversations").fetchone()[0]==70000


def test_lazy_fetch_rejects_swapped_envelope(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); direct=transport(server); monkeypatch.setattr("ai_convos_remote.request",direct); a,b=tmp_path/"a",tmp_path/"b"; alice,recovery=setup_client("http://server","alice",root=a); desktop,_=setup_client("http://server","alice","desktop",recovery,root=b); alice=load(a); ws=workspace(alice,"Personal"); sa,sb=connect(a/"remote/state.db"),connect(b/"remote/state.db"); [publish(alice,sa,ws,conversation(str(i)*70000,str(i)),a) for i in range(2)]; upload(alice,sa,a); pull(desktop,sb,b); ids=[r[0] for r in sb.execute("SELECT event FROM lazy_events ORDER BY event").fetchall()]
    def swapped(cfg,body,auth=True): return direct(cfg,{**body,"event":ids[1]} if body["op"]=="fetch" else body,auth)
    monkeypatch.setattr("ai_convos_remote.request",swapped)
    with pytest.raises(ValueError,match="mismatch"): fetch_lazy(desktop,sb,ids[0],b)
    assert sb.execute("SELECT COUNT(*) FROM lazy_events").fetchone()[0]==2 and not (b/"data/convos.db").exists()


def test_attachment_bytes_are_redacted_lazy_and_reassembled(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a,b=tmp_path/"a",tmp_path/"b"; alice,recovery=setup_client("http://server","alice",root=a); desktop,_=setup_client("http://server","alice","desktop",recovery,root=b); alice=load(a); ws=workspace(alice,"Personal"); state_a,state_b=connect(a/"remote/state.db"),connect(b/"remote/state.db")
    payload=bytes(range(256))*800; source=tmp_path/"private"/"evidence.bin"; source.parent.mkdir(); source.write_bytes(payload); (a/"data").mkdir(); core=duckdb.connect(str(a/"data/convos.db")); init_schema(core); core.execute("INSERT INTO conversations VALUES ('c','codex','attachment','2026-01-01','2026-01-01',NULL,NULL,NULL,NULL,'{}')"); core.execute("INSERT INTO messages VALUES ('m','c','user','see file',NULL,'2026-01-01',NULL,'{}',NULL,NULL)"); core.execute("INSERT INTO attachments VALUES ('a','m','evidence.bin','application/octet-stream',?,?,NULL,'2026-01-01')",(len(payload),str(source))); records=scan(core,state_a,alice["device"]["id"]); core.close()
    assert str(source) not in json.dumps(records) and sum(r["kind"]=="attachment.chunk" for r in records)>1; [publish(alice,state_a,ws,r,a) for r in records]; upload(alice,state_a,a); pull(desktop,state_b,b); target=duckdb.connect(str(b/"data/convos.db"),read_only=True); assert target.execute("SELECT path FROM attachments").fetchone()[0] is None; target.close(); chunks=state_b.execute("SELECT COUNT(*) FROM lazy_events").fetchone()[0]; assert chunks>1
    assert fetch_lazy(desktop,state_b,root=b)==chunks; path=duckdb.connect(str(b/"data/convos.db"),read_only=True).execute("SELECT path FROM attachments").fetchone()[0]; assert open(path,"rb").read()==payload and os.stat(path).st_mode&0o777==0o600


def test_team_user_multiple_devices_and_admin_device_removal(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a,b,c=tmp_path/"a",tmp_path/"b",tmp_path/"c"; alice,_=setup_client("http://server","alice",root=a); bob,recovery=setup_client("http://server","bob","laptop",root=b); team=create(alice,"Team","team",a); add_member(alice,team,"bob",root=a); bob=load(b); pull(bob,connect(b/"remote/state.db"),b); bob2,_=setup_client("http://server","bob","desktop",load(b)["recovery"],root=c)
    assert f"{team}:2" not in bob2["keys"]; pull(bob2,connect(c/"remote/state.db"),c); assert not (c/"data/convos.db").exists(); alice=load(a); approve_devices(alice,team,a); bob2=load(c); pull(bob2,connect(c/"remote/state.db"),c)
    alice=load(a); publish(alice,connect(a/"remote/state.db"),team,conversation("team device"),a); upload(alice,connect(a/"remote/state.db"),a); bob2=load(c); pull(bob2,connect(c/"remote/state.db"),c); assert duckdb.connect(str(c/"data/convos.db"),read_only=True).execute("SELECT title FROM conversations").fetchone()[0]=="team device"
    alice=load(a); remove_device(alice,team,bob2["device"]["id"],a); bob2=load(c); pull(bob2,connect(c/"remote/state.db"),c); state=refresh(bob2,c); assert next(w for w in state["workspaces"] if w["kind"]=="personal")["device_authorized"] and not next(w for w in state["workspaces"] if w["id"]==team)["device_authorized"] and server.execute("SELECT active FROM devices WHERE id=?",(bob2["device"]["id"],)).fetchone()[0]==1


def test_pending_or_removed_admin_device_cannot_authorize_itself(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a,b=tmp_path/"a",tmp_path/"b"; laptop,recovery=setup_client("http://server","alice","laptop",root=a); team=create(laptop,"Team","team",a); desktop,_=setup_client("http://server","alice","desktop",recovery,root=b)
    assert server.execute("SELECT epoch FROM workspaces WHERE id=?",(team,)).fetchone()[0]==1 and not server.execute("SELECT 1 FROM key_envelopes WHERE workspace=? AND device=?",(team,desktop["device"]["id"])).fetchone(); req={"op":"rotate","workspace":team,"epoch":2,"members":{laptop["user"]:"admin"},"envelopes":{d["id"]:{} for d in (laptop["device"],desktop["device"])}}
    grant={"op":"grant_all","workspace":team,"user":laptop["user"],"envelopes":{}}
    with pytest.raises(PermissionError,match="signature"): action(server,sign_control(desktop["device"],req),laptop["token"])
    with pytest.raises(PermissionError,match="authorized"): action(server,sign_control(desktop["device"],grant),desktop["token"])
    with pytest.raises(PermissionError,match="authorized"): action(server,sign_control(desktop["device"],req),desktop["token"])
    grant_all(laptop,team,"alice",a); assert not server.execute("SELECT 1 FROM key_envelopes WHERE workspace=? AND device=?",(team,desktop["device"]["id"])).fetchone(); pending={**grant,"envelopes":{"1":{d["id"]:{} for d in (laptop["device"],desktop["device"])}}}
    with pytest.raises(ValueError,match="authorized target"): action(server,sign_control(laptop["device"],pending),laptop["token"])
    laptop=load(a); approve_devices(laptop,team,a); remove_device(laptop,team,desktop["device"]["id"],a); req|={"epoch":4,"activate_devices":[desktop["device"]["id"]]}
    with pytest.raises(PermissionError,match="authorized"): action(server,sign_control(desktop["device"],grant),desktop["token"])
    with pytest.raises(PermissionError,match="authorized"): action(server,sign_control(desktop["device"],req),desktop["token"])
    bad={**grant,"envelopes":{"1":{desktop["device"]["id"]:{}}}}
    with pytest.raises(ValueError,match="authorized target"): action(server,sign_control(laptop["device"],bad),laptop["token"])
    grant_all(laptop,team,"alice",a); assert not server.execute("SELECT 1 FROM key_envelopes WHERE workspace=? AND epoch=3 AND device=?",(team,desktop["device"]["id"])).fetchone()
    assert server.execute("SELECT epoch FROM workspaces WHERE id=?",(team,)).fetchone()[0]==3 and server.execute("SELECT 1 FROM workspace_device_exclusions WHERE workspace=? AND device=?",(team,desktop["device"]["id"])).fetchone() and not server.execute("SELECT 1 FROM key_envelopes WHERE workspace=? AND epoch=4",(team,)).fetchone()
    personal=workspace(laptop,"Personal"); remove_device(laptop,personal,desktop["device"]["id"],a); req|={"workspace":personal,"epoch":4}
    grant|={"workspace":personal}
    with pytest.raises(PermissionError,match="authorized"): action(server,sign_control(desktop["device"],grant),desktop["token"])
    with pytest.raises(PermissionError,match="authorized"): action(server,sign_control(desktop["device"],req),desktop["token"])
    assert server.execute("SELECT epoch FROM workspaces WHERE id=?",(personal,)).fetchone()[0]==3 and server.execute("SELECT 1 FROM workspace_device_exclusions WHERE workspace=? AND device=?",(personal,desktop["device"]["id"])).fetchone()


def test_republished_history_verifies_embedded_author(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a,b=tmp_path/"a",tmp_path/"b"; alice,_=setup_client("http://server","alice",root=a); bob,_=setup_client("http://server","bob",root=b); team=create(alice,"Team","team",a); add_member(alice,team,"bob",root=a); inner=event(bob["device"],1,"conversation.record","conversations:inner",conversation("signed","inner")["payload"]); inner["payload"]["row"][2]="forged"; state=connect(a/"remote/state.db")
    entity="history:forged"; publish(alice,state,team,{"kind":"history.republish","entity":entity,"payload":{"target":bob["user"],"sealed":seal_history(inner,[bob["device"]],entity)}},a); upload(alice,state,a)
    target=connect(b/"remote/state.db")
    with pytest.raises(InvalidSignature): pull(load(b),target,b)
    assert not target.execute("SELECT 1 FROM event_log WHERE json_extract(event_json,'$.kind')='history.republish'").fetchone() and not (b/"data/convos.db").exists()


def test_selected_history_cannot_cross_workspaces(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a,b=tmp_path/"a",tmp_path/"b"; alice,_=setup_client("http://server","alice",root=a); setup_client("http://server","bob",root=b); first,second=create(alice,"First","team",a),create(alice,"Second","team",a); add_member(alice,first,"bob",root=a); add_member(alice,second,"bob",root=a); state=connect(a/"remote/state.db"); eid=publish(alice,state,first,conversation("first-only"),a); upload(alice,state,a); before=server.execute("SELECT COUNT(*) FROM events WHERE workspace=?",(second,)).fetchone()[0]
    assert grant_selected(alice,state,second,"bob",[eid],a)==0 and server.execute("SELECT COUNT(*) FROM events WHERE workspace=?",(second,)).fetchone()[0]==before


def test_selected_history_is_encrypted_to_target_devices(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a,b,c=tmp_path/"a",tmp_path/"b",tmp_path/"c"; alice,_=setup_client("http://server","alice",root=a); setup_client("http://server","bob",root=b); setup_client("http://server","carol",root=c); team=create(alice,"Team","team",a); state=connect(a/"remote/state.db"); old=publish(alice,state,team,conversation("old-secret"),a); upload(alice,state,a); add_member(alice,team,"bob",root=a); add_member(alice,team,"carol",root=a); grant_selected(alice,state,team,"bob",[old],a); sb,sc=connect(b/"remote/state.db"),connect(c/"remote/state.db"); pull(load(b),sb,b); pull(load(c),sc,c)
    assert duckdb.connect(str(b/"data/convos.db"),read_only=True).execute("SELECT title FROM conversations").fetchone()[0]=="old-secret" and not (c/"data/convos.db").exists(); assert "old-secret" not in "".join(r[0] for r in sc.execute("SELECT event_json FROM event_log").fetchall())


def test_lost_upload_response_and_interrupted_pull_recover_idempotently(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); direct=transport(server); monkeypatch.setattr("ai_convos_remote.request",direct); a,b=tmp_path/"a",tmp_path/"b"; alice,recovery=setup_client("http://server","alice",root=a); desktop,_=setup_client("http://server","alice","desktop",recovery,root=b); alice=load(a); ws=workspace(alice,"Personal"); state_a,state_b=connect(a/"remote/state.db"),connect(b/"remote/state.db"); baseline=server.execute("SELECT COUNT(*) FROM events").fetchone()[0]; publish(alice,state_a,ws,conversation("crash safe"),a)
    def lost(cfg,body,auth=True):
        result=direct(cfg,body,auth)
        if body["op"]=="upload_many": raise ConnectionError("response lost")
        return result
    monkeypatch.setattr("ai_convos_remote.request",lost)
    with pytest.raises(ConnectionError): upload(alice,state_a,a)
    assert state_a.execute("SELECT COUNT(*) FROM event_log WHERE direction='out' AND cursor=0").fetchone()[0]==1 and server.execute("SELECT COUNT(*) FROM events").fetchone()[0]==baseline+1
    monkeypatch.setattr("ai_convos_remote.request",direct); upload(alice,state_a,a); assert state_a.execute("SELECT COUNT(*) FROM event_log WHERE direction='out' AND cursor=0").fetchone()[0]==0 and state_a.execute("SELECT COUNT(*) FROM event_log WHERE direction='out' AND envelope IS NOT NULL").fetchone()[0]==0
    def cut(cfg,body,auth=True):
        result=direct(cfg,body,auth)
        if body["op"]=="pull": raise ConnectionError("pull interrupted")
        return result
    monkeypatch.setattr("ai_convos_remote.request",cut)
    with pytest.raises(ConnectionError): pull(desktop,state_b,b)
    assert not state_b.execute("SELECT * FROM cursors").fetchall()
    monkeypatch.setattr("ai_convos_remote.request",direct); pull(desktop,state_b,b); assert duckdb.connect(str(b/"data/convos.db"),read_only=True).execute("SELECT title FROM conversations").fetchone()[0]=="crash safe"
