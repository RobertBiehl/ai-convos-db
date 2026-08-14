import copy, json, os, shutil, sqlite3, subprocess
from pathlib import Path

import duckdb
import pytest
import ai_convos_memory as memory_module
import ai_convos_remote as remote_client
import ai_convos_remote.projection as projection_module
from cryptography.exceptions import InvalidSignature
from ai_convos.cli import ARCHIVE_COLUMNS, archive_state, capture_provenance, init_schema, project_archive_row
from ai_convos_remote import (_upload_batches, add_member, approve_device, approve_history, connect, control_body, create, doctor_status, fetch_lazy, grant_all, grant_selected, key, load, pull, publish, refresh, remove_device,
                              request_device, request_history, setup_client, sync_once, upload, workspace)
from ai_convos_remote.control import sign as control_sign, vote as device_vote
from ai_convos_remote.projection import inspect_state, scan
from ai_convos_remote.protocol import certificate, event, identity, seal_event, seal_history, seal_key, sign_control, unb64
from ai_convos_remote_server import action, connect as server_connect


def transport(db):
    def call(cfg,body,auth=True): return action(db,body,cfg.get("token") if auth else None)
    return call
@pytest.fixture(autouse=True)
def no_approval_delay(monkeypatch): monkeypatch.setattr("ai_convos_remote_server.APPROVAL_DELAY",0)
def conversation(title="shared",id="c"):
    cols=["id","source","title","created_at","updated_at","model","cwd","git_branch","project_id","metadata"]
    return {"kind":"conversation.record","entity":f"conversations:{id}","payload":{"table":"conversations","columns":cols,"row":[id,"codex",title,"2026-01-01","2026-01-01",None,None,None,None,"{}"]}}
def write_archive(path,title):
    path.parent.mkdir(parents=True,exist_ok=True); db=duckdb.connect(str(path)); init_schema(db); db.execute("BEGIN"); project_archive_row(db,"conversations",ARCHIVE_COLUMNS["conversations"],["c","codex",title,"2026-01-01","2026-01-01",None,None,None,None,"{}"]); db.execute("COMMIT"); info=archive_state(db); db.close(); return info
def inject(cfg,state,server,ws,kind,payload_v=1):
    seq=int((state.execute("SELECT value FROM meta WHERE key=?",(f"seq:{ws}",)).fetchone() or ["0"])[0])+1; prev=(state.execute("SELECT value FROM meta WHERE key=?",(f"prev:{ws}",)).fetchone() or [None])[0]; value=event(cfg["device"],seq,kind,f"{kind}:1",{"new_field":[1,2,3]},[prev] if prev else (),payload_v=payload_v); action(server,{"op":"upload_many","envelopes":[seal_event(value,ws,cfg["workspaces"][ws]["epoch"],key(cfg,ws,cfg["workspaces"][ws]["epoch"]))]},cfg["token"]); return value

def test_upload_batches_bound_count_and_wire_size():
    row=lambda size:{"size":size}
    assert [len(x) for x in _upload_batches([row(1)]*501,1000)]==[500,1] and [len(x) for x in _upload_batches([row(6)]*2,10)]==[1,1]


def test_settled_state_is_payload_size_independent_and_contains_no_bodies(tmp_path,monkeypatch):
    def settled(root,size):
        server=server_connect(tmp_path/f"{root.name}.server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); cfg,_=setup_client("http://server","alice",root=root); ws=workspace(cfg,"Personal"); state=connect(root/"remote/state.db"); marker=f"settled-{size}-marker-"+"x"*size; eid=publish(cfg,state,ws,conversation(marker),root); pending=next((root/"remote/outbox").iterdir()); assert marker.encode() not in pending.read_bytes() and state.execute("SELECT COUNT(*) FROM outbox WHERE event=?",(eid,)).fetchone()[0]==1; upload(cfg,state,root); state.execute("PRAGMA wal_checkpoint(TRUNCATE)"); pages=state.execute("PRAGMA page_count").fetchone()[0]; columns={r[1] for table, in state.execute("SELECT name FROM sqlite_master WHERE type='table'") for r in state.execute(f"PRAGMA table_info({table})")}; counts=tuple(state.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("receipts","publication_heads","event_sequences")); state.close(); files=[root/"remote/state.db",root/"remote/state.db-wal"]
        assert not list((root/"remote/outbox").iterdir()) and all(marker.encode() not in path.read_bytes() for path in files if path.exists()) and not {"event_json","envelope","content","data"}&columns
        return (root/"remote/state.db").stat().st_size,pages,counts
    assert settled(tmp_path/"one",1024)==settled(tmp_path/"two",1024*1024)


def test_remote_scan_is_read_only_and_does_not_self_trigger(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); monkeypatch.setattr("ai_convos_remote.drain_hooks",lambda:None); root=tmp_path/"client"; setup_client("http://server","alice",root=root); repo=root/"repo"; repo.mkdir(parents=True); subprocess.run(("git","-C",str(repo),"init","-q"),check=True); subprocess.run(("git","-C",str(repo),"config","user.email","a@b.c"),check=True); subprocess.run(("git","-C",str(repo),"config","user.name","A"),check=True); (repo/"a.py").write_text("one\n"); subprocess.run(("git","-C",str(repo),"add","."),check=True); subprocess.run(("git","-C",str(repo),"commit","-qm","initial"),check=True)
    path=root/"data/convos.db"; path.parent.mkdir(); db=duckdb.connect(str(path)); init_schema(db); db.execute("INSERT INTO conversations VALUES ('c','codex','provenance','2026-01-01','2026-01-01',NULL,?,NULL,NULL,'{}')",[str(repo)]); db.execute("INSERT INTO messages VALUES ('m','c','assistant','done',NULL,'2026-01-01',NULL,'{}',NULL,NULL)"); db.execute("INSERT INTO file_edits VALUES ('e','m',?,'write','one\n','2026-01-01',NULL)",[str(repo/"a.py")]); db.close(); capture_provenance(path); sync_once(root,True)
    state=connect(root/"remote/state.db"); ws=workspace(load(root),"Personal"); stamp=int(state.execute("SELECT value FROM meta WHERE key=?",(f"core_mtime:{ws}",)).fetchone()[0]); state.close(); db=duckdb.connect(str(path),read_only=True); observed=db.execute("SELECT observed_at FROM provenance.repositories").fetchone()[0]; db.close(); count=server.execute("SELECT COUNT(*) FROM events").fetchone()[0]; assert stamp==path.stat().st_mtime_ns
    sync_once(root); db=duckdb.connect(str(path),read_only=True); assert db.execute("SELECT observed_at FROM provenance.repositories").fetchone()[0]==observed; db.close(); assert server.execute("SELECT COUNT(*) FROM events").fetchone()[0]==count


def test_personal_recovery_multidevice_delivery_and_replay(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a,b=tmp_path/"a",tmp_path/"b"
    alice,recovery=setup_client("http://server","alice","laptop",root=a); ws=workspace(alice,"Personal"); state_a=connect(a/"remote/state.db"); publish(alice,state_a,ws,conversation(),a); upload(alice,state_a,a)
    desktop,_=setup_client("http://server","alice","desktop",recovery,root=b); state_b=connect(b/"remote/state.db"); pull(desktop,state_b,b); pull(desktop,state_b,b)
    db=duckdb.connect(str(b/"data/convos.db"),read_only=True); assert db.execute("SELECT title FROM conversations").fetchall()==[("shared",)] and db.execute("SELECT author_user_id FROM remote.row_origins").fetchone()[0]==alice["user"]; db.close(); head=state_b.execute("SELECT event FROM publication_heads WHERE workspace=? AND owner=? AND entity='conversations:c'",(ws,alice["user"])).fetchone()[0]; assert publish(load(b),state_b,ws,conversation(),b)==head and not state_b.execute("SELECT 1 FROM outbox").fetchone()
    assert len(load(b)["keys"])==2 and server.execute("SELECT epoch FROM workspaces WHERE id=?",(ws,)).fetchone()[0]==2
    assert os.stat(a/"remote").st_mode&0o777==0o700 and os.stat(a/"remote/config.json").st_mode&0o777==0o600 and os.stat(a/"remote/state.db").st_mode&0o777==0o600


def test_epoch_boundary_flushes_pending_events_before_signing(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a,b=tmp_path/"a",tmp_path/"b"; alice,_=setup_client("http://server","alice",root=a); setup_client("http://server","bob",root=b); team=create(alice,"Team","team",a); state=connect(a/"remote/state.db"); event_id=publish(alice,state,team,conversation("pending"),a); state.close(); add_member(alice,team,"bob",root=a); control=load(a)["controls"][team]
    cursor,seq=server.execute("SELECT cursor,seq FROM events WHERE event=?",(event_id,)).fetchone(); assert control["boundary"]["heads"][alice["device"]["id"]]=={"seq":seq,"event":event_id} and control["boundary"]["tail"]==cursor


def test_deleted_state_adopts_an_intact_import_only_archive(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); monkeypatch.setattr("ai_convos_remote.drain_hooks",lambda:None); a,b=tmp_path/"a",tmp_path/"b"; alice,recovery=setup_client("http://server","alice","laptop",root=a); ws=workspace(alice,"Personal"); state=connect(a/"remote/state.db"); publish(alice,state,ws,conversation(),a); upload(alice,state,a); state.close(); desktop,_=setup_client("http://server","alice","desktop",recovery,root=b); state=connect(b/"remote/state.db"); pull(desktop,state,b); state.close(); before=server.execute("SELECT COUNT(*) FROM events").fetchone()[0]; state_path=b/"remote/state.db"; state_path.unlink(); sync_once(b,True)
    db=duckdb.connect(str(b/"data/convos.db"),read_only=True); assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]==db.execute("SELECT COUNT(*) FROM remote.row_origins").fetchone()[0]==1; db.close(); assert server.execute("SELECT COUNT(*) FROM events").fetchone()[0]==before


def test_personal_sync_automatically_bridges_encrypted_memory_between_devices(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); monkeypatch.setattr("ai_convos_remote.drain_hooks",lambda:None); a,b=tmp_path/"a",tmp_path/"b"; alice,recovery=setup_client("http://server","alice","laptop",root=a); setup_client("http://server","alice","desktop",recovery,root=b)
    monkeypatch.delenv("CONVOS_MEMORY_DB",raising=False); monkeypatch.setenv("CONVOS_PROJECT_ROOT",str(a)); created=memory_module.remember_data("relay cannot read this","global"); sync_once(a,True); wire="".join(r[0] for r in server.execute("SELECT envelope FROM events").fetchall())
    assert "relay cannot read this" not in wire and str(a) not in wire
    sync_once(b,True); db=sqlite3.connect(b/"memory/state.db"); assert db.execute("SELECT content FROM canonicals").fetchall()==[("relay cannot read this",)]; db.close()
    large="second device-safe revision\n"+"x"*70000; memory_module.remember_data(large,"global",created["id"]); sync_once(a,True); sync_once(b,True); count=server.execute("SELECT COUNT(*) FROM events").fetchone()[0]; sync_once(a,True); sync_once(b,True)
    db=sqlite3.connect(b/"memory/state.db"); assert db.execute("SELECT content FROM canonicals").fetchall()==[(large,)] and db.execute("SELECT COUNT(*) FROM remote_parts").fetchone()[0]==0; db.close(); state=connect(b/"remote/state.db"); assert state.execute("SELECT COUNT(*) FROM lazy_events").fetchone()[0]==0; state.close(); assert server.execute("SELECT COUNT(*) FROM events").fetchone()[0]==count and "second device-safe revision" not in "".join(r[0] for r in server.execute("SELECT envelope FROM events").fetchall())
    state=connect(a/"remote/state.db"); old=[r[0] for r in state.execute("SELECT event FROM receipts WHERE kind='memory.canonical' AND status='active'").fetchall()]; state.close(); memory_module.forget_data(created["id"],"global"); sync_once(a,True); assert server.execute(f"SELECT COUNT(*) FROM events WHERE event IN ({','.join('?'*len(old))})",old).fetchone()[0]==0 and server.execute(f"SELECT COUNT(*) FROM event_purges WHERE event IN ({','.join('?'*len(old))})",old).fetchone()[0]==len(old)
    sync_once(b,True); db=sqlite3.connect(b/"memory/state.db"); assert db.execute("SELECT COUNT(*) FROM canonicals").fetchone()[0]==db.execute("SELECT COUNT(*) FROM sources").fetchone()[0]==0; db.close(); state=connect(b/"remote/state.db"); rows=[tuple(r) for r in state.execute("SELECT status FROM receipts WHERE kind='memory.canonical'").fetchall()]; state.close(); assert rows==[("deleted",)] and b"second device-safe revision" not in (b/"remote/state.db").read_bytes()
    recreated=memory_module.remember_data(large,"global"); assert recreated["id"]!=created["id"]; sync_once(a,True); sync_once(b,True); db=sqlite3.connect(b/"memory/state.db"); assert db.execute("SELECT content FROM canonicals").fetchall()==[(large,)]; db.close()


def test_lost_purge_response_retries_without_resurrecting_forgotten_memory(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); direct=transport(server); monkeypatch.setattr("ai_convos_remote.request",direct); monkeypatch.setattr("ai_convos_remote.drain_hooks",lambda:None); a=tmp_path/"a"; setup_client("http://server","alice",root=a); monkeypatch.delenv("CONVOS_MEMORY_DB",raising=False); monkeypatch.setenv("CONVOS_PROJECT_ROOT",str(a)); created=memory_module.remember_data("purge retry secret","global"); sync_once(a,True); state=connect(a/"remote/state.db"); old=[r[0] for r in state.execute("SELECT event FROM receipts WHERE kind='memory.canonical'").fetchall()]; state.close(); memory_module.forget_data(created["id"],"global"); lost=[False]
    def request_lost(cfg,body,auth=True):
        result=direct(cfg,body,auth)
        if body["op"]=="purge" and not lost[0]: lost[0]=True; raise ConnectionError("purge response lost")
        return result
    monkeypatch.setattr("ai_convos_remote.request",request_lost)
    with pytest.raises(ConnectionError,match="response lost"): sync_once(a,True)
    state=connect(a/"remote/state.db"); assert state.execute(f"SELECT COUNT(*) FROM receipts WHERE event IN ({','.join('?'*len(old))})",old).fetchone()[0]==len(old); state.close(); assert server.execute(f"SELECT COUNT(*) FROM event_purges WHERE event IN ({','.join('?'*len(old))})",old).fetchone()[0]==len(old)
    monkeypatch.setattr("ai_convos_remote.request",direct); sync_once(a,True); state=connect(a/"remote/state.db"); assert state.execute(f"SELECT COUNT(*) FROM receipts WHERE event IN ({','.join('?'*len(old))})",old).fetchone()[0]==0; state.close()


def test_relay_cannot_fabricate_purge_history_or_mutate_client_state(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); direct=transport(server); monkeypatch.setattr("ai_convos_remote.request",direct); monkeypatch.setattr("ai_convos_remote.drain_hooks",lambda:None); a,b=tmp_path/"a",tmp_path/"b"; _,recovery=setup_client("http://server","alice","laptop",root=a); setup_client("http://server","alice","desktop",recovery,root=b); monkeypatch.delenv("CONVOS_MEMORY_DB",raising=False); monkeypatch.setenv("CONVOS_PROJECT_ROOT",str(a)); created=memory_module.remember_data("relay forgery target","global"); sync_once(a,True); sync_once(b,True); memory_module.forget_data(created["id"],"global"); sync_once(a,True); state=connect(b/"remote/state.db"); ws=workspace(load(b),"Personal"); before=state.execute("SELECT cursor FROM cursors WHERE workspace=?",(ws,)).fetchone()[0]; active=state.execute("SELECT COUNT(*) FROM receipts WHERE kind='memory.canonical' AND status='active'").fetchone()[0]; state.close()
    def forged(cfg,body,auth=True):
        result=copy.deepcopy(direct(cfg,body,auth))
        if body["op"]=="pull":
            for item in result["events"]:
                if "purge" in item: item["purge"]["event"]="0"*64
        return result
    monkeypatch.setattr("ai_convos_remote.request",forged)
    state=connect(b/"remote/state.db")
    with pytest.raises(ValueError,match="purge certificate"): pull(load(b),state,b)
    state.close()
    state=connect(b/"remote/state.db"); assert state.execute("SELECT cursor FROM cursors WHERE workspace=?",(ws,)).fetchone()[0]==before and state.execute("SELECT COUNT(*) FROM receipts WHERE kind='memory.canonical' AND status='active'").fetchone()[0]==active and state.execute("SELECT lifecycle FROM sync_states WHERE workspace=?",(ws,)).fetchone()[0]=="blocked"; state.close()
    monkeypatch.setattr("ai_convos_remote.request",direct); state=connect(b/"remote/state.db"); pull(load(b),state,b); assert not state.execute("SELECT 1 FROM receipts WHERE kind='memory.canonical' AND status='active'").fetchone(); state.close()


def test_fresh_remote_state_recovers_signed_purge_without_ciphertext(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); monkeypatch.setattr("ai_convos_remote.drain_hooks",lambda:None); a,c=tmp_path/"a",tmp_path/"c"; _,recovery=setup_client("http://server","alice","laptop",root=a); monkeypatch.delenv("CONVOS_MEMORY_DB",raising=False); monkeypatch.setenv("CONVOS_PROJECT_ROOT",str(a)); created=memory_module.remember_data("recover deleted history","global"); sync_once(a,True); memory_module.forget_data(created["id"],"global"); sync_once(a,True); setup_client("http://server","alice","fresh",recovery,root=c); monkeypatch.setenv("CONVOS_PROJECT_ROOT",str(c)); sync_once(c,True); state=connect(c/"remote/state.db"); ws=workspace(load(c),"Personal"); assert state.execute("SELECT lifecycle FROM sync_states WHERE workspace=?",(ws,)).fetchone()[0]=="ready" and not state.execute("SELECT 1 FROM sequence_gaps").fetchone() and not state.execute("SELECT 1 FROM receipts WHERE kind='memory.canonical' AND status='active'").fetchone() and state.execute("SELECT 1 FROM receipts WHERE kind='memory.canonical' AND status='deleted'").fetchone(); state.close()


def test_device_certificates_reject_relay_key_substitution_without_auto_certifying(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); direct=transport(server); monkeypatch.setattr("ai_convos_remote.request",direct); a,b,c=tmp_path/"a",tmp_path/"b",tmp_path/"c"; alice,recovery=setup_client("http://server","alice",root=a); bob,_=setup_client("http://server","bob",root=b); team=create(alice,"Team","team",a); server.execute("DELETE FROM device_certificates WHERE device=?",(alice["device"]["id"],)); server.commit(); desktop,_=setup_client("http://server","alice","desktop",recovery,root=c); state=refresh(desktop,c); assert server.execute("SELECT COUNT(*) FROM device_certificates WHERE device IN (?,?)",(alice["device"]["id"],desktop["device"]["id"])).fetchone()[0]==1 and next(w for w in state["workspaces"] if w["kind"]=="personal")["device_authorized"]; attacker=identity("attacker")
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
    monkeypatch.setattr("ai_convos_remote.request",direct); request_device(desktop,team,c,0); monkeypatch.setattr("ai_convos_remote.request",tamper("state",alice["device"]["id"]))
    assert approve_device(alice,team,desktop["device"]["id"],root=a)["approved"]
    monkeypatch.setattr("ai_convos_remote.request",direct); add_member(alice,team,bob["user"],root=a); server.execute("DELETE FROM device_certificates WHERE device=?",(bob["device"]["id"],)); server.commit(); add_member(alice,team,bob["user"],True,root=a); assert server.execute("SELECT active FROM members WHERE workspace=? AND user_id=?",(team,bob["user"])).fetchone()[0]==0
    mallory,_=setup_client("http://server","mallory",root=tmp_path/"m"); action(server,{"op":"certify","certificate":certificate(bob["root"],bob["user"],bob["device"])},bob["token"]); server.execute("UPDATE users SET name=? WHERE id=?",(bob["user"],mallory["user"])); server.commit(); add_member(alice,team,bob["user"],root=a); assert server.execute("SELECT active FROM members WHERE workspace=? AND user_id=?",(team,bob["user"])).fetchone()[0]==1 and not server.execute("SELECT 1 FROM members WHERE workspace=? AND user_id=?",(team,mallory["user"])).fetchone()
    def substitute(cfg,body,auth=True):
        result=copy.deepcopy(direct(cfg,{**body,"user":mallory["user"]},auth)) if body["op"]=="directory" else direct(cfg,body,auth)
        if body["op"]=="directory": result["users"][0]["name"]=bob["user"]
        return result
    monkeypatch.setattr("ai_convos_remote.request",substitute)
    with pytest.raises(ValueError,match="directory user"): add_member(alice,team,bob["user"],root=a)


@pytest.mark.parametrize("field,value",(("id","wrong"),("kind","personal"),("epoch",1)))
def test_refresh_rejects_relay_workspace_metadata(tmp_path,monkeypatch,field,value):
    server=server_connect(tmp_path/"server.db"); direct=transport(server); monkeypatch.setattr("ai_convos_remote.request",direct); a,b=tmp_path/"a",tmp_path/"b"; alice,_=setup_client("http://server","alice",root=a); bob,_=setup_client("http://server","bob",root=b); team=create(alice,"Team","team",a); add_member(alice,team,"bob",root=a)
    def tamper(cfg,body,auth=True):
        result=copy.deepcopy(direct(cfg,body,auth))
        if body["op"]=="state":
            found=next(w for w in result["workspaces"] if w["id"]==team); found[field]=value
        return result
    monkeypatch.setattr("ai_convos_remote.request",tamper)
    with pytest.raises(ValueError,match="metadata"): refresh(bob,b)
    assert team not in load(b)["workspaces"]


def test_refresh_rejects_relay_key_beyond_signed_history(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); direct=transport(server); monkeypatch.setattr("ai_convos_remote.request",direct); a,b=tmp_path/"a",tmp_path/"b"; alice,_=setup_client("http://server","alice",root=a); bob,_=setup_client("http://server","bob",root=b); team=create(alice,"Team","team",a); old=alice["keys"][f"{team}:1"]; add_member(alice,team,"bob",root=a)
    def tamper(cfg,body,auth=True):
        result=copy.deepcopy(direct(cfg,body,auth))
        if body["op"]=="state":
            next(w for w in result["workspaces"] if w["id"]==team)["keys"].append({"epoch":1,"envelope":json.dumps(seal_key(unb64(old),bob["device"]["box_public"],f"workspace:{team}:epoch:1"))})
        return result
    monkeypatch.setattr("ai_convos_remote.request",tamper)
    with pytest.raises(ValueError,match="entitlement"): refresh(bob,b)


def test_relay_workspace_omission_stops_stale_upload(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); direct=transport(server); monkeypatch.setattr("ai_convos_remote.request",direct); a=tmp_path/"a"; alice,_=setup_client("http://server","alice",root=a); team=create(alice,"Team","team",a); state=connect(a/"remote/state.db"); baseline=server.execute("SELECT COUNT(*) FROM events WHERE workspace=?",(team,)).fetchone()[0]; pending=publish(alice,state,team,conversation("must stay local"),a)
    def omit(cfg,body,auth=True):
        result=copy.deepcopy(direct(cfg,body,auth))
        if body["op"]=="state": result["workspaces"]=[w for w in result["workspaces"] if w["id"]!=team]
        return result
    monkeypatch.setattr("ai_convos_remote.request",omit); upload(alice,state,a)
    assert team in load(a)["workspaces"] and server.execute("SELECT COUNT(*) FROM events WHERE workspace=?",(team,)).fetchone()[0]==baseline and state.execute("SELECT COUNT(*) FROM outbox WHERE event=?",(pending,)).fetchone()[0]==1


def test_team_default_selected_complete_history_and_removal(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a,b=tmp_path/"a",tmp_path/"b"
    alice,_=setup_client("http://server","alice","laptop",root=a); bob,_=setup_client("http://server","bob","desktop",root=b); team=create(alice,"Team","team",a); sa,sb=connect(a/"remote/state.db"),connect(b/"remote/state.db")
    old=publish(alice,sa,team,conversation("before bob"),a); upload(alice,sa,a); add_member(alice,team,"bob",root=a); bob=load(b); pull(bob,sb,b); assert not (b/"data/convos.db").exists()
    publish(alice,sa,team,conversation("after bob","new"),a); upload(alice,sa,a); bob=load(b); pull(bob,sb,b); assert duckdb.connect(str(b/"data/convos.db"),read_only=True).execute("SELECT title FROM conversations").fetchall()==[("after bob",)]
    assert grant_selected(alice,sa,team,"bob",[old],a)==1; bob=load(b); pull(bob,sb,b); assert {r[0] for r in duckdb.connect(str(b/"data/convos.db"),read_only=True).execute("SELECT title FROM conversations").fetchall()}=={"before bob","after bob"}
    previous=alice["controls"][team]; members={**previous["members"],bob["user"]:{**previous["members"][bob["user"]],"history_from":1}}; control=control_body(alice,previous,key(alice,team,previous["epoch"]),"history",members=members); incomplete={"op":"grant_all","workspace":team,"user":bob["user"],"control":control,"envelopes":{}}
    with pytest.raises(ValueError,match="every workspace epoch"): action(server,sign_control(alice["device"],incomplete),alice["token"])
    future={**incomplete,"envelopes":{"999":{bob["device"]["id"]:{}}}}
    with pytest.raises(ValueError,match="outside"): action(server,sign_control(alice["device"],future),alice["token"])
    assert grant_all(alice,team,"bob",a)>=2; bob=load(b); pull(bob,sb,b); assert any(name.endswith(":1") for name in load(b)["keys"])
    add_member(alice,team,"bob",True,root=a); bob=load(b); pull(bob,sb,b); assert team not in {w["id"] for w in load(b)["server_state"]["workspaces"]} and f"{team}:3" not in load(b)["keys"]


def test_unknown_required_event_blocks_ready_without_storing_content(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a,b=tmp_path/"a",tmp_path/"b"; alice,recovery=setup_client("http://server","alice","laptop",root=a); desktop,_=setup_client("http://server","alice","desktop",recovery,root=b); ws=workspace(desktop,"Personal"); source=connect(b/"remote/state.db"); value=inject(desktop,source,server,ws,"future.opaque")
    target=connect(a/"remote/state.db")
    with pytest.raises(ValueError,match="required event is unsupported"): pull(load(a),target,a)
    assert tuple(target.execute("SELECT kind,payload_v,required FROM deferred_events WHERE event=?",(value["id"],)).fetchone())==("future.opaque",1,1) and target.execute("SELECT lifecycle FROM sync_states WHERE workspace=?",(ws,)).fetchone()[0]=="blocked" and b"new_field" not in (a/"remote/state.db").read_bytes() and action(server,{"op":"fetch","workspace":ws,"event":value["id"]},desktop["token"])["envelope"]["event"]==value["id"]
    bridge={"events":{("future.opaque",1)},"project":lambda root,state,event,workspace,device:True}; monkeypatch.setattr(projection_module,"bridges",lambda:[bridge]); result=pull(load(a),target,a); assert result[ws]["cursor"]==result[ws]["tail"] and not target.execute("SELECT 1 FROM deferred_events").fetchone() and target.execute("SELECT lifecycle FROM sync_states WHERE workspace=?",(ws,)).fetchone()[0]=="ready"


def test_uninstalled_auxiliary_event_defers_without_blocking_core(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a,b=tmp_path/"a",tmp_path/"b"; alice,recovery=setup_client("http://server","alice","laptop",root=a); desktop,_=setup_client("http://server","alice","desktop",recovery,root=b); ws=workspace(desktop,"Personal"); value=inject(desktop,connect(b/"remote/state.db"),server,ws,"memory.canonical"); monkeypatch.setattr(projection_module,"bridges",lambda:[])
    target=connect(a/"remote/state.db"); result=pull(load(a),target,a); assert result[ws]["cursor"]==result[ws]["tail"] and tuple(target.execute("SELECT kind,payload_v,required FROM deferred_events WHERE event=?",(value["id"],)).fetchone())==("memory.canonical",1,0) and target.execute("SELECT lifecycle FROM sync_states WHERE workspace=?",(ws,)).fetchone()[0]=="ready"


def test_large_record_is_fetched_during_convergent_pull(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a,b=tmp_path/"a",tmp_path/"b"; alice,recovery=setup_client("http://server","alice",root=a); desktop,_=setup_client("http://server","alice","desktop",recovery,root=b); alice=load(a); ws=workspace(alice,"Personal"); state_a,state_b=connect(a/"remote/state.db"),connect(b/"remote/state.db")
    publish(alice,state_a,ws,conversation("x"*70000),a); upload(alice,state_a,a); result=pull(desktop,state_b,b); assert state_b.execute("SELECT COUNT(*) FROM lazy_events").fetchone()[0]==0 and result[ws]["cursor"]==result[ws]["tail"] and duckdb.connect(str(b/"data/convos.db"),read_only=True).execute("SELECT length(title) FROM conversations").fetchone()[0]==70000


def test_lazy_fetch_rejects_swapped_envelope(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); direct=transport(server); monkeypatch.setattr("ai_convos_remote.request",direct); a,b=tmp_path/"a",tmp_path/"b"; alice,recovery=setup_client("http://server","alice",root=a); desktop,_=setup_client("http://server","alice","desktop",recovery,root=b); alice=load(a); ws=workspace(alice,"Personal"); sa,sb=connect(a/"remote/state.db"),connect(b/"remote/state.db"); [publish(alice,sa,ws,conversation(str(i)*70000,str(i)),a) for i in range(2)]; upload(alice,sa,a); ids=[r[0] for r in server.execute("SELECT event FROM events WHERE LENGTH(envelope)>65536 ORDER BY event").fetchall()]
    def swapped(cfg,body,auth=True): return direct(cfg,{**body,"event":ids[1]} if body["op"]=="fetch" and body["event"]==ids[0] else body,auth)
    monkeypatch.setattr("ai_convos_remote.request",swapped)
    with pytest.raises(ValueError,match="mismatch"): pull(desktop,sb,b)
    assert sb.execute("SELECT lifecycle FROM sync_states WHERE workspace=?",(ws,)).fetchone()[0]=="blocked"


def test_attachment_bytes_are_redacted_lazy_and_reassembled(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a,b=tmp_path/"a",tmp_path/"b"; alice,recovery=setup_client("http://server","alice",root=a); desktop,_=setup_client("http://server","alice","desktop",recovery,root=b); alice=load(a); ws=workspace(alice,"Personal"); state_a,state_b=connect(a/"remote/state.db"),connect(b/"remote/state.db")
    payload=bytes(range(256))*800; source=tmp_path/"private"/"evidence.bin"; source.parent.mkdir(); source.write_bytes(payload); (a/"data").mkdir(); core=duckdb.connect(str(a/"data/convos.db")); init_schema(core); core.execute("INSERT INTO conversations VALUES ('c','codex','attachment','2026-01-01','2026-01-01',NULL,NULL,NULL,NULL,'{}')"); core.execute("INSERT INTO messages VALUES ('m','c','user','see file',NULL,'2026-01-01',NULL,'{}',NULL,NULL)"); core.execute("INSERT INTO attachments VALUES ('a','m','evidence.bin','application/octet-stream',?,?,NULL,'2026-01-01')",(len(payload),str(source))); records=scan(core,state_a); core.close()
    assert str(source) not in json.dumps(records) and sum(r["kind"]=="attachment.chunk" for r in records)>1; [publish(alice,state_a,ws,r,a) for r in records]; upload(alice,state_a,a); pull(desktop,state_b,b); path=duckdb.connect(str(b/"data/convos.db"),read_only=True).execute("SELECT path FROM attachments").fetchone()[0]; assert open(path,"rb").read()==payload and os.stat(path).st_mode&0o777==0o600 and state_b.execute("SELECT COUNT(*) FROM lazy_events").fetchone()[0]==state_b.execute("SELECT COUNT(*) FROM attachment_parts").fetchone()[0]==0


def test_deleted_state_rebaselines_before_publishing_existing_archive(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); direct=transport(server); monkeypatch.setattr("ai_convos_remote.request",direct); monkeypatch.setattr("ai_convos_remote.drain_hooks",lambda:None); a,b=tmp_path/"a",tmp_path/"b"; alice,recovery=setup_client("http://server","alice","laptop",root=a); setup_client("http://server","alice","desktop",recovery,root=b); alice=load(a); ws=workspace(alice,"Personal"); sa=connect(a/"remote/state.db"); publish(alice,sa,ws,conversation("remote","remote"),a); upload(alice,sa,a); sa.close(); sync_once(b,True); core=duckdb.connect(str(b/"data/convos.db"),read_only=True); restored=core.execute("SELECT id,title FROM conversations").fetchall(); origins=core.execute("SELECT * FROM remote.row_origins").fetchall(); core.close(); assert restored==[("remote","remote")] and not origins
    before=server.execute("SELECT COUNT(*) FROM events").fetchone()[0]; state_path=b/"remote/state.db"; [Path(str(state_path)+suffix).unlink(missing_ok=True) for suffix in ("-wal","-shm")]; state_path.unlink(); legacy=sqlite3.connect(state_path); legacy.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)"); legacy.execute("CREATE TABLE legacy_payload(value TEXT)"); legacy.execute("INSERT INTO meta VALUES ('state_schema','3')"); legacy.execute("INSERT INTO legacy_payload VALUES ('preserve me')"); legacy.commit(); legacy.close(); db=duckdb.connect(str(b/"data/convos.db")); db.execute("INSERT INTO conversations VALUES ('local','codex','new local','2026-01-02','2026-01-02',NULL,NULL,NULL,NULL,'{}')"); db.close()
    def offline(cfg,body,auth=True):
        if body["op"]=="pull": raise ConnectionError("relay unavailable")
        return direct(cfg,body,auth)
    monkeypatch.setattr("ai_convos_remote.request",offline)
    with pytest.raises(ConnectionError,match="unavailable"): sync_once(b,True)
    state=connect(state_path); report=json.loads(state.execute("SELECT value FROM meta WHERE key='state_cutover'").fetchone()[0]); state.close(); assert server.execute("SELECT COUNT(*) FROM events").fetchone()[0]==before and inspect_state(state_path)["status"]=="current" and Path(report["backup"]).is_dir(); backup=sqlite3.connect(Path(report["backup"])/"state.db"); assert backup.execute("SELECT value FROM legacy_payload").fetchone()[0]=="preserve me"; backup.close()
    monkeypatch.setattr("ai_convos_remote.request",direct); sync_once(b,True); assert server.execute("SELECT COUNT(*) FROM events").fetchone()[0]==before+1
    state=connect(b/"remote/state.db"); assert state.execute("SELECT lifecycle FROM sync_states WHERE workspace=?",(ws,)).fetchone()[0]=="ready" and not state.execute("SELECT 1 FROM sqlite_master WHERE name='imported_rows'").fetchone(); state.close(); core=duckdb.connect(str(b/"data/convos.db"),read_only=True); assert core.execute("SELECT id,title FROM conversations ORDER BY id").fetchall()==[("local","new local"),("remote","remote")] and not core.execute("SELECT * FROM remote.row_origins").fetchall(); core.close()


def test_missing_archive_recovers_owned_rows_without_republishing(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); monkeypatch.setattr("ai_convos_remote.drain_hooks",lambda:None); root=tmp_path/"client"; cfg,_=setup_client("http://server","alice",root=root); path=root/"data/convos.db"; write_archive(path,"latest"); sync_once(root,True); before=server.execute("SELECT COUNT(*) FROM events").fetchone()[0]; db=duckdb.connect(str(path),read_only=True); old=archive_state(db)[0]; db.close(); path.unlink(); remember=remote_client.remember_archive; monkeypatch.setattr(remote_client,"remember_archive",lambda *args:(_ for _ in ()).throw(ConnectionError("after recovery")))
    with pytest.raises(ConnectionError,match="after recovery"): sync_once(root,True)
    monkeypatch.setattr(remote_client,"remember_archive",remember); sync_once(root,True)
    db=duckdb.connect(str(path),read_only=True); assert db.execute("SELECT id,title FROM conversations").fetchall()==[("c","latest")] and not db.execute("SELECT * FROM remote.row_origins").fetchall() and archive_state(db)[0]!=old; db.close(); state=connect(root/"remote/state.db"); ws=workspace(load(root),"Personal"); assert state.execute("SELECT lifecycle FROM sync_states WHERE workspace=?",(ws,)).fetchone()[0]=="ready"; state.close(); assert server.execute("SELECT COUNT(*) FROM events").fetchone()[0]==before


@pytest.mark.parametrize("lost_anchor",("state","config"))
def test_rolled_back_archive_recovers_additively_and_blocks_reversion(tmp_path,monkeypatch,lost_anchor):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); monkeypatch.setattr("ai_convos_remote.drain_hooks",lambda:None); root=tmp_path/"client"; setup_client("http://server","alice",root=root); path=root/"data/convos.db"; first=write_archive(path,"old"); sync_once(root,True); backup=tmp_path/"old.db"; shutil.copyfile(path,backup); second=write_archive(path,"current"); assert first[0]==second[0] and first[1]<second[1]; sync_once(root,True); before=server.execute("SELECT COUNT(*) FROM events").fetchone()[0]; shutil.copyfile(backup,path)
    if lost_anchor=="state": (root/"remote/state.db").unlink()
    else: cfg=load(root); cfg.pop("archive"); remote_client.save(cfg,root)
    with pytest.raises(ValueError,match="recovered additively"): sync_once(root,True)
    db=duckdb.connect(str(path),read_only=True); recovered=(archive_state(db)[1],db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]); assert {r[0] for r in db.execute("SELECT title FROM conversations").fetchall()}=={"old","current"} and db.execute("SELECT author_user_id FROM remote.row_origins").fetchone()[0]==load(root)["user"]; db.close()
    with pytest.raises(ValueError,match="recovered additively"): sync_once(root,True)
    db=duckdb.connect(str(path),read_only=True); assert (archive_state(db)[1],db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0])==recovered; db.close(); state=connect(root/"remote/state.db"); ws=workspace(load(root),"Personal"); assert state.execute("SELECT lifecycle FROM sync_states WHERE workspace=?",(ws,)).fetchone()[0]=="blocked" and state.execute("SELECT value FROM meta WHERE key=?",(f"archive_mode:{ws}",)).fetchone()[0]=="import"; state.close(); assert server.execute("SELECT COUNT(*) FROM events").fetchone()[0]==before


def test_ready_rejects_history_missing_before_signed_checkpoint(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a,b=tmp_path/"a",tmp_path/"b"; alice,recovery=setup_client("http://server","alice","laptop",root=a); ws=workspace(alice,"Personal"); state=connect(a/"remote/state.db"); publish(alice,state,ws,conversation("must survive"),a); upload(alice,state,a); desktop,_=setup_client("http://server","alice","desktop",recovery,root=b); server.execute("DELETE FROM events WHERE workspace=? AND author=?",(ws,alice["device"]["id"])); server.commit(); target=connect(b/"remote/state.db")
    with pytest.raises(ValueError,match="signed history checkpoint"): pull(desktop,target,b)
    assert target.execute("SELECT lifecycle FROM sync_states WHERE workspace=?",(ws,)).fetchone()[0]=="blocked"


def test_doctor_reports_legacy_state_without_modifying_it(tmp_path,monkeypatch):
    root=tmp_path/"client"; remote=root/"remote"; remote.mkdir(parents=True); (remote/"config.json").write_text(json.dumps({"url":"http://server","user":"user","device":{"id":"device"},"workspaces":{},"keys":{}})); path=remote/"state.db"; db=sqlite3.connect(path); db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)"); db.execute("INSERT INTO meta VALUES ('state_schema','3')"); db.commit(); db.close(); before=(path.read_bytes(),path.stat().st_mtime_ns,{p.name for p in remote.iterdir()}); monkeypatch.setenv("CONVOS_PROJECT_ROOT",str(root)); monkeypatch.setattr("ai_convos_remote.health",lambda cfg:{"ok":True})
    assert "state=incompatible" in doctor_status() and "backup+rebaseline" in doctor_status()
    assert (path.read_bytes(),path.stat().st_mtime_ns,{p.name for p in remote.iterdir()})==before


def test_pull_converges_past_relay_batch_limit(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); direct=transport(server); monkeypatch.setattr("ai_convos_remote.request",direct); a,b=tmp_path/"a",tmp_path/"b"; alice,recovery=setup_client("http://server","alice","laptop",root=a); desktop,_=setup_client("http://server","alice","desktop",recovery,root=b); alice=load(a); ws=workspace(alice,"Personal"); state=connect(a/"remote/state.db")
    [publish(alice,state,ws,conversation(f"row {i}",f"row-{i}"),a,True) for i in range(510)]; state.commit(); upload(alice,state,a); calls=[]
    def counted(cfg,body,auth=True): calls.append(body["op"]); return direct(cfg,body,auth)
    monkeypatch.setattr("ai_convos_remote.request",counted); result=pull(desktop,connect(b/"remote/state.db"),b); db=duckdb.connect(str(b/"data/convos.db"),read_only=True); assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]==510; db.close(); assert calls.count("pull")>=2 and result[ws]["cursor"]==result[ws]["tail"]


def test_crash_after_duckdb_projection_replays_before_cursor_commit(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); direct=transport(server); monkeypatch.setattr("ai_convos_remote.request",direct); a,b=tmp_path/"a",tmp_path/"b"; alice,recovery=setup_client("http://server","alice","laptop",root=a); desktop,_=setup_client("http://server","alice","desktop",recovery,root=b); alice=load(a); ws=workspace(alice,"Personal"); state=connect(a/"remote/state.db"); publish(alice,state,ws,conversation("projected once","once"),a); upload(alice,state,a); target=connect(b/"remote/state.db"); real=remote_client.project_many; tables=("receipts","event_sequences","heads","cursors")
    def crash(*args,**kwargs): result=real(*args,**kwargs); raise ConnectionError("after DuckDB commit")
    monkeypatch.setattr(remote_client,"project_many",crash)
    with pytest.raises(ConnectionError,match="DuckDB"): pull(desktop,target,b)
    assert [target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables]==[0,0,0,0] and target.execute("SELECT lifecycle FROM sync_states WHERE workspace=?",(ws,)).fetchone()[0]=="blocked"
    db=duckdb.connect(str(b/"data/convos.db"),read_only=True); assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]==db.execute("SELECT COUNT(*) FROM remote.row_origins").fetchone()[0]==1; db.close()
    monkeypatch.setattr(remote_client,"project_many",real); result=pull(desktop,target,b); assert result[ws]["cursor"]==result[ws]["tail"] and duckdb.connect(str(b/"data/convos.db"),read_only=True).execute("SELECT COUNT(*) FROM conversations").fetchone()[0]==1


def test_team_user_multiple_devices_and_admin_device_removal(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a,b,c=tmp_path/"a",tmp_path/"b",tmp_path/"c"; alice,_=setup_client("http://server","alice",root=a); bob,recovery=setup_client("http://server","bob","laptop",root=b); team=create(alice,"Team","team",a); add_member(alice,team,"bob",root=a); bob=load(b); pull(bob,connect(b/"remote/state.db"),b); bob2,_=setup_client("http://server","bob","desktop",load(b)["recovery"],root=c)
    assert f"{team}:2" not in bob2["keys"]; pull(bob2,connect(c/"remote/state.db"),c); assert not (c/"data/convos.db").exists(); request_device(bob2,team,c,0); bob=load(b); result=approve_device(bob,team,bob2["device"]["id"],root=b); assert result["approved"] and result["history"]>=1; bob2=load(c); pull(bob2,connect(c/"remote/state.db"),c)
    alice=load(a); publish(alice,connect(a/"remote/state.db"),team,conversation("team device"),a); upload(alice,connect(a/"remote/state.db"),a); bob2=load(c); pull(bob2,connect(c/"remote/state.db"),c); assert duckdb.connect(str(c/"data/convos.db"),read_only=True).execute("SELECT title FROM conversations").fetchone()[0]=="team device"
    alice=load(a); remove_device(alice,team,bob2["device"]["id"],a); bob2=load(c); pull(bob2,connect(c/"remote/state.db"),c); state=refresh(bob2,c); assert next(w for w in state["workspaces"] if w["kind"]=="personal")["device_authorized"] and not next(w for w in state["workspaces"] if w["id"]==team)["device_authorized"] and server.execute("SELECT active FROM devices WHERE id=?",(bob2["device"]["id"],)).fetchone()[0]==1


def test_pending_or_removed_admin_device_cannot_authorize_itself(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a,b=tmp_path/"a",tmp_path/"b"; laptop,recovery=setup_client("http://server","alice","laptop",root=a); team=create(laptop,"Team","team",a); desktop,_=setup_client("http://server","alice","desktop",recovery,root=b)
    assert server.execute("SELECT epoch FROM workspaces WHERE id=?",(team,)).fetchone()[0]==1 and not server.execute("SELECT 1 FROM key_envelopes WHERE workspace=? AND device=?",(team,desktop["device"]["id"])).fetchone(); req={"op":"rotate","workspace":team,"epoch":2,"members":{laptop["user"]:"admin"},"envelopes":{d["id"]:{} for d in (laptop["device"],desktop["device"])}}
    grant={"op":"grant_all","workspace":team,"user":laptop["user"],"envelopes":{}}
    with pytest.raises(PermissionError,match="signature"): action(server,sign_control(desktop["device"],req),laptop["token"])
    with pytest.raises(PermissionError,match="authorized"): action(server,sign_control(desktop["device"],grant),desktop["token"])
    with pytest.raises(PermissionError,match="authorized"): action(server,sign_control(desktop["device"],req),desktop["token"])
    request_device(desktop,team,b,0)
    with pytest.raises(PermissionError,match="vote"): approve_device(desktop,team,desktop["device"]["id"],root=b)
    assert approve_device(load(a),team,desktop["device"]["id"],False,root=a)=={"approved":False,"rejected":True}
    with pytest.raises(ValueError,match="not found"): approve_device(load(a),team,desktop["device"]["id"],root=a)
    request_device(desktop,team,b,0)
    laptop=load(a); approve_device(laptop,team,desktop["device"]["id"],root=a); laptop=load(a); remove_device(laptop,team,desktop["device"]["id"],a); req|={"epoch":4,"activate_devices":[desktop["device"]["id"]]}
    with pytest.raises(PermissionError,match="authorized"): action(server,sign_control(desktop["device"],grant),desktop["token"])
    with pytest.raises(PermissionError,match="authorized"): action(server,sign_control(desktop["device"],req),desktop["token"])
    with pytest.raises(ValueError,match="not pending"): request_device(load(b),team,b,0)
    grant_all(laptop,team,"alice",a); assert not server.execute("SELECT 1 FROM key_envelopes WHERE workspace=? AND epoch=3 AND device=?",(team,desktop["device"]["id"])).fetchone()
    assert server.execute("SELECT epoch FROM workspaces WHERE id=?",(team,)).fetchone()[0]==3 and server.execute("SELECT 1 FROM workspace_device_exclusions WHERE workspace=? AND device=?",(team,desktop["device"]["id"])).fetchone() and not server.execute("SELECT 1 FROM key_envelopes WHERE workspace=? AND epoch=4",(team,)).fetchone()
    personal=workspace(laptop,"Personal"); remove_device(laptop,personal,desktop["device"]["id"],a); req|={"workspace":personal,"epoch":4}
    grant|={"workspace":personal}
    with pytest.raises(PermissionError,match="authorized"): action(server,sign_control(desktop["device"],grant),desktop["token"])
    with pytest.raises(PermissionError,match="authorized"): action(server,sign_control(desktop["device"],req),desktop["token"])
    assert server.execute("SELECT epoch FROM workspaces WHERE id=?",(personal,)).fetchone()[0]==3 and server.execute("SELECT 1 FROM workspace_device_exclusions WHERE workspace=? AND device=?",(personal,desktop["device"]["id"])).fetchone()


def test_orphan_device_requires_user_majority_and_inherits_role_not_history(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a,b,c,d=tmp_path/"a",tmp_path/"b",tmp_path/"c",tmp_path/"d"; alice,recovery=setup_client("http://server","alice","laptop",root=a); bob,_=setup_client("http://server","bob",root=b); carol,_=setup_client("http://server","carol",root=c); team=create(alice,"Team","team",a); add_member(alice,team,"bob",root=a); alice=load(a); add_member(alice,team,"carol",root=a); recovered,_=setup_client("http://server","alice","recovered",recovery,root=d); alice=load(a); remove_device(alice,team,alice["device"]["id"],a); proposal=request_device(recovered,team,d,0); bad=control_sign(bob["device"],{**{k:v for k,v in device_vote(bob["device"],bob["user"],proposal).items() if k not in ("author","signature")},"voter":recovered["user"]})
    with pytest.raises(PermissionError,match="vote"): action(server,{"op":"vote","vote":bad},bob["token"])
    forged=control_sign(recovered["device"],{**{k:v for k,v in proposal.items() if k not in ("author","signature")},"certificate_hash":"0"*64})
    with pytest.raises(PermissionError,match="proposal"): action(server,{"op":"propose","proposal":forged},recovered["token"])
    assert server.execute("SELECT COUNT(*) FROM device_votes").fetchone()[0]==0 and server.execute("SELECT COUNT(*) FROM device_proposals").fetchone()[0]==1
    first=approve_device(load(b),team,recovered["device"]["id"],root=b); assert first=={"approved":False,"votes":1,"needed":2}
    final=approve_device(load(c),team,recovered["device"]["id"],root=c); assert final["approved"] and final["history"]==0
    state=refresh(load(d),d); control=next(w for w in state["workspaces"] if w["id"]==team)["controls"][-1]; assert control["members"][recovered["user"]]["role"]=="admin" and not control["devices"][recovered["device"]["id"]]["history"]


def test_history_can_be_approved_later_and_sync_rewinds(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a,b,c=tmp_path/"a",tmp_path/"b",tmp_path/"c"; alice,recovery=setup_client("http://server","alice","laptop",root=a); bob,_=setup_client("http://server","bob",root=b); team=create(alice,"Team","team",a); sa=connect(a/"remote/state.db"); publish(alice,sa,team,conversation("old"),a); upload(alice,sa,a); add_member(alice,team,"bob",root=a); grant_all(alice,team,"bob",a); recovered,_=setup_client("http://server","alice","recovered",recovery,root=c); alice=load(a); remove_device(alice,team,alice["device"]["id"],a); request_device(recovered,team,c,0); assert approve_device(load(b),team,recovered["device"]["id"],root=b)["history"]==0
    bob=load(b); publish(bob,connect(b/"remote/state.db"),team,conversation("new","new"),b); upload(bob,connect(b/"remote/state.db"),b); recovered=load(c); sc=connect(c/"remote/state.db"); pull(recovered,sc,c); assert duckdb.connect(str(c/"data/convos.db"),read_only=True).execute("SELECT title FROM conversations").fetchall()==[("new",)]
    request_history(recovered,team,c,0); result=approve_history(load(b),team,recovered["device"]["id"],root=b); assert result["approved"] and result["history"]>=4
    recovered=load(c); pull(recovered,sc,c); assert {r[0] for r in duckdb.connect(str(c/"data/convos.db"),read_only=True).execute("SELECT title FROM conversations").fetchall()}=={"old","new"}


def test_same_user_approval_rewraps_selected_history(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a,b,c=tmp_path/"a",tmp_path/"b",tmp_path/"c"; alice,_=setup_client("http://server","alice",root=a); bob,recovery=setup_client("http://server","bob","laptop",root=b); team=create(alice,"Team","team",a); sa,sb=connect(a/"remote/state.db"),connect(b/"remote/state.db"); old=publish(alice,sa,team,conversation("selected"),a); upload(alice,sa,a); add_member(alice,team,"bob",root=a); grant_selected(alice,sa,team,"bob",[old],a); pull(load(b),sb,b); desktop,_=setup_client("http://server","bob","desktop",recovery,root=c); request_device(desktop,team,c,0); assert approve_device(load(b),team,desktop["device"]["id"],root=b)["approved"]
    pull(load(c),connect(c/"remote/state.db"),c); assert duckdb.connect(str(c/"data/convos.db"),read_only=True).execute("SELECT title FROM conversations").fetchone()[0]=="selected"


def test_relay_clock_enforces_proposal_delay(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); monkeypatch.setattr("ai_convos_remote_server.APPROVAL_DELAY",3600); a,b,c=tmp_path/"a",tmp_path/"b",tmp_path/"c"; alice,recovery=setup_client("http://server","alice",root=a); setup_client("http://server","bob",root=b); team=create(alice,"Team","team",a); add_member(alice,team,"bob",root=a); recovered,_=setup_client("http://server","alice","recovered",recovery,root=c); remove_device(alice,team,alice["device"]["id"],a); request_device(recovered,team,c,0)
    with pytest.raises(ValueError,match="active"): approve_device(load(b),team,recovered["device"]["id"],root=b)


def test_republished_history_verifies_embedded_author(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a,b=tmp_path/"a",tmp_path/"b"; alice,_=setup_client("http://server","alice",root=a); bob,_=setup_client("http://server","bob",root=b); team=create(alice,"Team","team",a); add_member(alice,team,"bob",root=a); inner=event(bob["device"],1,"conversation.record","conversations:inner",conversation("signed","inner")["payload"]); inner["payload"]["row"][2]="forged"; state=connect(a/"remote/state.db")
    entity="history:forged"; publish(alice,state,team,{"kind":"history.republish","entity":entity,"payload":{"target":bob["user"],"sealed":seal_history(inner,[bob["device"]],entity)}},a); upload(alice,state,a)
    target=connect(b/"remote/state.db")
    with pytest.raises(InvalidSignature): pull(load(b),target,b)
    assert not target.execute("SELECT 1 FROM receipts WHERE kind='history.republish'").fetchone() and not (b/"data/convos.db").exists()


def test_selected_history_cannot_cross_workspaces(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a,b=tmp_path/"a",tmp_path/"b"; alice,_=setup_client("http://server","alice",root=a); setup_client("http://server","bob",root=b); first,second=create(alice,"First","team",a),create(alice,"Second","team",a); add_member(alice,first,"bob",root=a); add_member(alice,second,"bob",root=a); state=connect(a/"remote/state.db"); eid=publish(alice,state,first,conversation("first-only"),a); upload(alice,state,a); before=server.execute("SELECT COUNT(*) FROM events WHERE workspace=?",(second,)).fetchone()[0]
    with pytest.raises(ValueError,match="receipt"): grant_selected(alice,state,second,"bob",[eid],a)
    assert server.execute("SELECT COUNT(*) FROM events WHERE workspace=?",(second,)).fetchone()[0]==before


def test_selected_history_is_encrypted_to_target_devices(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); monkeypatch.setattr("ai_convos_remote.request",transport(server)); a,b,c=tmp_path/"a",tmp_path/"b",tmp_path/"c"; alice,_=setup_client("http://server","alice",root=a); setup_client("http://server","bob",root=b); setup_client("http://server","carol",root=c); team=create(alice,"Team","team",a); state=connect(a/"remote/state.db"); old=publish(alice,state,team,conversation("old-secret"),a); upload(alice,state,a); add_member(alice,team,"bob",root=a); add_member(alice,team,"carol",root=a); grant_selected(alice,state,team,"bob",[old],a); sb,sc=connect(b/"remote/state.db"),connect(c/"remote/state.db"); pull(load(b),sb,b); pull(load(c),sc,c)
    assert duckdb.connect(str(b/"data/convos.db"),read_only=True).execute("SELECT title FROM conversations").fetchone()[0]=="old-secret" and not (c/"data/convos.db").exists(); assert b"old-secret" not in (c/"remote/state.db").read_bytes()


def test_lost_upload_response_and_interrupted_pull_recover_idempotently(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); direct=transport(server); monkeypatch.setattr("ai_convos_remote.request",direct); a,b=tmp_path/"a",tmp_path/"b"; alice,recovery=setup_client("http://server","alice",root=a); desktop,_=setup_client("http://server","alice","desktop",recovery,root=b); alice=load(a); ws=workspace(alice,"Personal"); state_a,state_b=connect(a/"remote/state.db"),connect(b/"remote/state.db"); baseline=server.execute("SELECT COUNT(*) FROM events").fetchone()[0]; publish(alice,state_a,ws,conversation("crash safe"),a)
    def lost(cfg,body,auth=True):
        result=direct(cfg,body,auth)
        if body["op"]=="upload_many": raise ConnectionError("response lost")
        return result
    monkeypatch.setattr("ai_convos_remote.request",lost)
    with pytest.raises(ConnectionError): upload(alice,state_a,a)
    assert state_a.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]==1 and server.execute("SELECT COUNT(*) FROM events").fetchone()[0]==baseline+1
    monkeypatch.setattr("ai_convos_remote.request",direct); upload(alice,state_a,a); assert state_a.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]==0 and state_a.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]>0
    def cut(cfg,body,auth=True):
        result=direct(cfg,body,auth)
        if body["op"]=="pull": raise ConnectionError("pull interrupted")
        return result
    monkeypatch.setattr("ai_convos_remote.request",cut)
    with pytest.raises(ConnectionError): pull(desktop,state_b,b)
    assert not state_b.execute("SELECT * FROM cursors").fetchall()
    monkeypatch.setattr("ai_convos_remote.request",direct); pull(desktop,state_b,b); assert duckdb.connect(str(b/"data/convos.db"),read_only=True).execute("SELECT title FROM conversations").fetchone()[0]=="crash safe"


def test_lost_upload_response_survives_epoch_rotation_without_resealing(tmp_path,monkeypatch):
    server=server_connect(tmp_path/"server.db"); direct=transport(server); monkeypatch.setattr("ai_convos_remote.request",direct); a,b=tmp_path/"a",tmp_path/"b"; alice,recovery=setup_client("http://server","alice","laptop",root=a); ws=workspace(alice,"Personal"); state=connect(a/"remote/state.db"); eid=publish(alice,state,ws,conversation("rotate after lost response"),a); original=json.loads(next((a/"remote/outbox").iterdir()).read_text()); lost=[False]
    def drop(cfg,body,auth=True):
        result=direct(cfg,body,auth)
        if body["op"]=="upload_many" and not lost[0]: lost[0]=True; raise ConnectionError("response lost")
        return result
    monkeypatch.setattr("ai_convos_remote.request",drop)
    with pytest.raises(ConnectionError,match="response lost"): upload(alice,state,a)
    monkeypatch.setattr("ai_convos_remote.request",direct); setup_client("http://server","alice","desktop",recovery,root=b); alice=load(a); upload(alice,state,a); stored=json.loads(server.execute("SELECT envelope FROM events WHERE event=?",(eid,)).fetchone()[0])
    assert stored==original and state.execute("SELECT epoch FROM receipts WHERE event=?",(eid,)).fetchone()[0]==1 and not state.execute("SELECT 1 FROM outbox WHERE event=?",(eid,)).fetchone()
