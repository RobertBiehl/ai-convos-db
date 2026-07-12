import json, plistlib, shutil, sqlite3, subprocess
from pathlib import Path

from typer.testing import CliRunner
import ai_convos_remote as remote_client
from ai_convos import cli
from ai_convos_remote import edit_hooks, enable, setup_client
from ai_convos_remote_server import action, connect


def test_remote_enable_installs_archive_hooks_but_remove_retains_them(monkeypatch):
    installs,services=[],[]; monkeypatch.setattr(remote_client,"install_hooks",lambda remove,status: installs.append((remove,status))); monkeypatch.setattr(remote_client,"enable",lambda path,remove: services.append(remove) or "ok"); runner=CliRunner()
    assert runner.invoke(remote_client.remote,["enable"]).exit_code==0 and runner.invoke(remote_client.remote,["enable","--remove"]).exit_code==0 and installs==[(False,False)] and services==[False,True]


def test_remote_enable_removes_obsolete_wake_hooks(tmp_path,monkeypatch):
    claude,codex=tmp_path/"claude",tmp_path/"codex"; claude.mkdir(); codex.mkdir(); monkeypatch.setenv("CLAUDE_CONFIG_DIR",str(claude)); monkeypatch.setenv("CODEX_HOME",str(codex)); monkeypatch.setenv("CONVOS_PROJECT_ROOT",str(tmp_path/"archive"))
    remote={"type":"command","command":"mkdir -p /tmp/x && touch /tmp/x/wake # ai-convos remote hook"}; core={"type":"command","command":"/opt/convos capture claude-code","statusMessage":"Saving conversation to Convos"}; (claude/"settings.json").write_text(json.dumps({"keep":1,"hooks":{"Stop":[{"hooks":[core,remote]}],"SessionEnd":[{"hooks":[remote]}]}})); (codex/"hooks.json").write_text(json.dumps({"hooks":{"Stop":[{"hooks":[{"type":"command","command":"/opt/convos remote hook"}]}]}})); edit_hooks(); edit_hooks()
    c,x=json.loads((claude/"settings.json").read_text()),json.loads((codex/"hooks.json").read_text()); assert c=={"keep":1,"hooks":{"Stop":[{"hooks":[core]}]}} and x=={"hooks":{}} and not (tmp_path/"archive/remote/wake").exists()


def test_background_services_preserve_custom_root(tmp_path,monkeypatch):
    home=tmp_path/"home"; root=tmp_path/"custom % archive"; calls=[]; binary=home/"custom % bin/convos"; monkeypatch.setenv("HOME",str(home)); monkeypatch.setenv("CLAUDE_CONFIG_DIR",str(home/"claude")); monkeypatch.setenv("CODEX_HOME",str(home/"codex")); monkeypatch.setattr("ai_convos_remote.service.subprocess.run",lambda *a,**k:calls.append(a[0])); monkeypatch.setattr("ai_convos_remote.service.shutil.which",lambda _:str(binary))
    monkeypatch.setattr("ai_convos_remote.service.sys.platform","darwin"); enable(root/"remote"); plist=plistlib.loads((home/"Library/LaunchAgents/com.ai-convos.remote.plist").read_bytes()); assert plist["EnvironmentVariables"]=={"CONVOS_PROJECT_ROOT":str(root.resolve())} and plist["ProgramArguments"][0]==str(binary) and not (home/"codex/hooks.json").exists()
    monkeypatch.setattr("ai_convos_remote.service.sys.platform","linux"); enable(root/"remote"); path=home/".config/systemd/user/convos-remote.service"; unit=path.read_text(); assert f'Environment="CONVOS_PROJECT_ROOT={str(root.resolve()).replace("%","%%")}"' in unit and f'ExecStart="{str(binary).replace("%","%%")}"' in unit
    enable(root/"remote",True); assert not path.exists() and calls[-1]==("systemctl","--user","daemon-reload")


def test_server_backup_is_consistent_and_restorable(tmp_path):
    source,backup=tmp_path/"server.db",tmp_path/"backup.db"; db=connect(source); db.execute("INSERT INTO users VALUES ('u','alice','root',NULL,1)"); db.commit(); db.close()
    subprocess.run((shutil.which("convos-server"),"backup","--db",str(source),"--output",str(backup)),check=True,capture_output=True)
    restored=connect(backup); assert tuple(restored.execute("SELECT id,name FROM users").fetchall()[0])==("u","alice")


def test_top_level_doctor_reports_remote_identity_queue_crypto_and_last_sync(tmp_path,monkeypatch):
    db=connect(tmp_path/"server.db"); monkeypatch.setenv("CONVOS_PROJECT_ROOT",str(tmp_path/"archive")); monkeypatch.setattr("ai_convos_remote.request",lambda cfg,body,auth=True: action(db,body,cfg.get("token") if auth else None)); monkeypatch.setattr("ai_convos_remote.health",lambda cfg:{"ok":True}); setup_client("http://server","alice",root=tmp_path/"archive"); monkeypatch.setattr(cli,"safari_cookie_domains",lambda:[]); monkeypatch.setattr(cli,"chrome_cookie_domains",lambda:[])
    out=CliRunner().invoke(cli.app,["doctor"]).output; assert "remote: reachable" in out and "user=" in out and "device=" in out and "workspaces=1" in out and "epochs=1" in out and "pending=0" in out and "lazy=0" in out and "last=never" in out
