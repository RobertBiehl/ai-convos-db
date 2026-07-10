"""Install low-latency agent hooks and a persistent per-user sync worker."""
import json, os, plistlib, shlex, shutil, subprocess, sys
from pathlib import Path

def edit_hooks(remove=False):
    wake=Path(os.environ.get("CONVOS_PROJECT_ROOT",Path.home()/".convos"))/"remote/wake"; cmd=f"mkdir -p {shlex.quote(str(wake.parent))} && touch {shlex.quote(str(wake))} # ai-convos remote hook"; configs=((Path(os.environ.get("CLAUDE_CONFIG_DIR",Path.home()/".claude"))/"settings.json",("Stop","SessionEnd")),(Path(os.environ.get("CODEX_HOME",Path.home()/".codex"))/"hooks.json",("Stop",)))
    for path,events in configs:
        data=json.loads(path.read_text()) if path.exists() else {}; hooks=data.setdefault("hooks",{})
        for name in list(hooks): hooks[name]=[g for g in hooks[name] if not any(h.get("command","").endswith("convos remote hook") for h in g.get("hooks",[]))]; hooks[name] or hooks.pop(name)
        if not remove:
            for name in events: hooks.setdefault(name,[]).append({"hooks":[{"type":"command","command":cmd,"timeout":1,"statusMessage":"Queueing encrypted conversation sync"}]})
        path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(data))
    return cmd
def enable(data_dir,remove=False):
    cmd=edit_hooks(remove); Path(data_dir).mkdir(parents=True,exist_ok=True); label="com.ai-convos.remote"
    if sys.platform!="darwin":
        unit=Path.home()/".config/systemd/user/convos-remote.service"; subprocess.run(("systemctl","--user","disable","--now","convos-remote.service"),capture_output=True)
        if remove: unit.unlink(missing_ok=True); return "Remote background sync removed"
        unit.parent.mkdir(parents=True,exist_ok=True); unit.write_text(f"[Unit]\nDescription=Convos encrypted synchronization\n[Service]\nExecStart={shutil.which('convos') or 'convos'} remote watch --interval 2\nRestart=always\n[Install]\nWantedBy=default.target\n"); subprocess.run(("systemctl","--user","daemon-reload"),check=True); subprocess.run(("systemctl","--user","enable","--now","convos-remote.service"),check=True); return f"Remote background sync enabled; hooks run `{cmd}`"
    plist=Path.home()/"Library/LaunchAgents/com.ai-convos.remote.plist"
    if remove: subprocess.run(("launchctl","bootout",f"gui/{os.getuid()}/{label}"),capture_output=True); plist.unlink(missing_ok=True); return "Remote background sync removed"
    plist.parent.mkdir(parents=True,exist_ok=True); plist.write_bytes(plistlib.dumps({"Label":label,"ProgramArguments":[shutil.which("convos") or "convos","remote","watch","--interval","2"],"KeepAlive":True,"RunAtLoad":True,"StandardOutPath":str(Path(data_dir)/"worker.log"),"StandardErrorPath":str(Path(data_dir)/"worker.log")})); subprocess.run(("launchctl","bootout",f"gui/{os.getuid()}/{label}"),capture_output=True); subprocess.run(("launchctl","bootstrap",f"gui/{os.getuid()}",str(plist)),check=True); return f"Remote background sync enabled; hooks run `{cmd}`"
