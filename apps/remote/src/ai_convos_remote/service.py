"""Install a persistent per-user sync worker and remove obsolete wake hooks."""
import json, os, plistlib, shutil, subprocess, sys
from pathlib import Path

def edit_hooks(remove=False,root=None):
    configs=(Path(os.environ.get("CLAUDE_CONFIG_DIR",Path.home()/".claude"))/"settings.json",Path(os.environ.get("CODEX_HOME",Path.home()/".codex"))/"hooks.json")
    for path in configs:
        if not path.exists(): continue
        data=json.loads(path.read_text()); hooks=data.get("hooks",{})
        for name in list(hooks):
            for group in hooks[name]: group["hooks"]=[h for h in group.get("hooks",[]) if not h.get("command","").endswith("convos remote hook")]
            hooks[name]=[g for g in hooks[name] if g.get("hooks")]; hooks[name] or hooks.pop(name)
        path.write_text(json.dumps(data))
def enable(data_dir,remove=False):
    data_dir=Path(data_dir).resolve(); edit_hooks(); data_dir.mkdir(parents=True,exist_ok=True); root=str(data_dir.parent); label="com.ai-convos.remote"
    if sys.platform!="darwin":
        unit=Path.home()/".config/systemd/user/convos-remote.service"; subprocess.run(("systemctl","--user","disable","--now","convos-remote.service"),capture_output=True)
        if remove: unit.unlink(missing_ok=True); subprocess.run(("systemctl","--user","daemon-reload"),check=True); return "Remote background sync removed"
        unit.parent.mkdir(parents=True,exist_ok=True); unit.write_text(f"[Unit]\nDescription=Convos encrypted synchronization\n[Service]\nEnvironment={json.dumps('CONVOS_PROJECT_ROOT='+root.replace('%','%%'))}\nExecStart={json.dumps((shutil.which('convos') or 'convos').replace('%','%%'))} remote watch --interval 2\nRestart=always\n[Install]\nWantedBy=default.target\n"); subprocess.run(("systemctl","--user","daemon-reload"),check=True); subprocess.run(("systemctl","--user","enable","--now","convos-remote.service"),check=True); return "Remote background sync enabled"
    plist=Path.home()/"Library/LaunchAgents/com.ai-convos.remote.plist"
    if remove: subprocess.run(("launchctl","bootout",f"gui/{os.getuid()}/{label}"),capture_output=True); plist.unlink(missing_ok=True); return "Remote background sync removed"
    plist.parent.mkdir(parents=True,exist_ok=True); plist.write_bytes(plistlib.dumps({"Label":label,"ProgramArguments":[shutil.which("convos") or "convos","remote","watch","--interval","2"],"EnvironmentVariables":{"CONVOS_PROJECT_ROOT":root},"KeepAlive":True,"RunAtLoad":True,"StandardOutPath":str(data_dir/"worker.log"),"StandardErrorPath":str(data_dir/"worker.log")})); subprocess.run(("launchctl","bootout",f"gui/{os.getuid()}/{label}"),capture_output=True); subprocess.run(("launchctl","bootstrap",f"gui/{os.getuid()}",str(plist)),check=True); return "Remote background sync enabled"
