import json, os, shutil, sqlite3, subprocess, sys, time
from pathlib import Path

from typer.testing import CliRunner
from ai_convos import cli
from ai_convos_remote import edit_hooks, hook_cmd, setup_client
from ai_convos_remote_server import action, connect


def test_remote_hooks_are_private_fast_idempotent_and_removable(tmp_path,monkeypatch):
    claude,codex=tmp_path/"claude",tmp_path/"codex"; claude.mkdir(); codex.mkdir(); monkeypatch.setenv("CLAUDE_CONFIG_DIR",str(claude)); monkeypatch.setenv("CODEX_HOME",str(codex)); monkeypatch.setenv("CONVOS_PROJECT_ROOT",str(tmp_path/"archive"))
    (claude/"settings.json").write_text(json.dumps({"keep":1,"hooks":{"Stop":[{"hooks":[{"type":"command","command":"keep"}]}]}})); edit_hooks(); edit_hooks()
    c,x=json.loads((claude/"settings.json").read_text()),json.loads((codex/"hooks.json").read_text()); assert c["keep"]==1 and sum(h["command"].endswith("remote hook") for gs in c["hooks"].values() for g in gs for h in g["hooks"])==2 and sum(h["command"].endswith("remote hook") for gs in x["hooks"].values() for g in gs for h in g["hooks"])==1
    samples=[]
    for _ in range(30): start=time.perf_counter(); hook_cmd(); samples.append((time.perf_counter()-start)*1000)
    assert sorted(samples)[28] < 100 and (tmp_path/"archive/remote/wake").exists()
    env={**os.environ,"CONVOS_PROJECT_ROOT":str(tmp_path/"cli-archive")}; subprocess.run((shutil.which("convos"),"remote","hook"),env=env,check=True); samples=[]
    for _ in range(25): start=time.perf_counter(); subprocess.run((shutil.which("convos"),"remote","hook"),env=env,check=True); samples.append((time.perf_counter()-start)*1000)
    assert sorted(samples)[23] < 100
    edit_hooks(True); c=json.loads((claude/"settings.json").read_text()); assert c["hooks"]=={"Stop":[{"hooks":[{"type":"command","command":"keep"}]}]}


def test_server_backup_is_consistent_and_restorable(tmp_path):
    source,backup=tmp_path/"server.db",tmp_path/"backup.db"; db=connect(source); db.execute("INSERT INTO users VALUES ('u','alice','root',NULL,1)"); db.commit(); db.close()
    subprocess.run((shutil.which("convos-server"),"backup","--db",str(source),"--output",str(backup)),check=True,capture_output=True)
    restored=connect(backup); assert tuple(restored.execute("SELECT id,name FROM users").fetchall()[0])==("u","alice")


def test_top_level_doctor_reports_remote_identity_queue_crypto_and_last_sync(tmp_path,monkeypatch):
    db=connect(tmp_path/"server.db"); monkeypatch.setenv("CONVOS_PROJECT_ROOT",str(tmp_path/"archive")); monkeypatch.setattr("ai_convos_remote.request",lambda cfg,body,auth=True: action(db,body,cfg.get("token") if auth else None)); monkeypatch.setattr("ai_convos_remote.health",lambda cfg:{"ok":True}); setup_client("http://server","alice",root=tmp_path/"archive"); monkeypatch.setattr(cli,"safari_cookie_domains",lambda:[]); monkeypatch.setattr(cli,"chrome_cookie_domains",lambda:[])
    out=CliRunner().invoke(cli.app,["doctor"]).output; assert "remote: reachable" in out and "user=" in out and "device=" in out and "workspaces=1" in out and "epochs=1" in out and "pending=0" in out and "lazy=0" in out and "last=never" in out
