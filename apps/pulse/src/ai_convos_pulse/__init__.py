"""Local factual cross-project activity digests and private dashboards."""
import html, json, os, subprocess, webbrowser
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import typer
from ai_convos.cli import PROJECT_ROOT, drain_hooks, get_db
from ai_convos_redact import inspect

NOISE=r"^(Base directory for this skill:|# AGENTS\.md instructions for|<(codex_internal_context|environment_context|local-command-caveat|recommended_plugins|skill)( |>))"

def _plain(value):
    if isinstance(value,str): return "".join(c for c in value if c in "\n\t" or ord(c)>=32)
    if isinstance(value,dict): return {k:_plain(v) for k,v in value.items()}
    if isinstance(value,list): return [_plain(v) for v in value]
    return value
def _safe(value):
    clean,findings=inspect(value); return _plain(clean),len(findings)
def _git_root(value):
    path=Path(value).expanduser()
    if not path.is_dir(): return value
    try:
        result=subprocess.run(("git","-C",str(path),"rev-parse","--show-toplevel"),capture_output=True,text=True,timeout=3)
        return str(Path(result.stdout.strip()).resolve()) if result.returncode==0 else str(path.resolve())
    except (OSError,subprocess.TimeoutExpired): return str(path.resolve())
def pulse_data(days=1,project_limit=12,session_limit=3,include_web=False,min_messages=2,now=None):
    now=(now or datetime.now(timezone.utc)).astimezone(timezone.utc); since=(now-timedelta(days=days)).replace(tzinfo=None); drain_hooks(); db=get_db(read_only=True)
    if db is None: return dict(status="no_archive",generated_at=now.isoformat(),since=since.replace(tzinfo=timezone.utc).isoformat(),days=days,include_web=include_web,min_messages=min_messages,totals=dict(projects=0,conversations=0,messages=0,edits=0,files=0,tools={}),projects=[],projects_truncated=0,short_sessions_omitted=0,redactions=0)
    sessions=db.execute("""SELECT c.id,c.source,c.title,c.cwd,MIN(m.created_at),MAX(m.created_at),COUNT(*),COUNT(*) FILTER (m.role='user'),COUNT(*) FILTER (m.role='assistant')
        FROM conversations c JOIN messages m ON m.conversation_id=c.id WHERE m.created_at>=? AND COALESCE(m.content,'')!='' AND json_extract_string(m.metadata,'$.history_of') IS NULL AND NOT regexp_matches(m.content,?)
        GROUP BY c.id,c.source,c.title,c.cwd ORDER BY MAX(m.created_at) DESC NULLS LAST,c.id""",[since,NOISE]).fetchall()
    last={cid:dict(message_id=mid,role=role,created_at=at) for cid,mid,role,at in db.execute("""SELECT conversation_id,id,role,created_at FROM (SELECT m.conversation_id,m.id,m.role,m.created_at,ROW_NUMBER() OVER(PARTITION BY m.conversation_id ORDER BY m.created_at DESC NULLS LAST,m.id DESC) rank FROM messages m WHERE m.created_at>=? AND COALESCE(m.content,'')!='' AND json_extract_string(m.metadata,'$.history_of') IS NULL AND NOT regexp_matches(m.content,?)) WHERE rank=1""",[since,NOISE]).fetchall()}
    edits={cid:dict(edits=count,paths=set(paths)) for cid,count,paths in db.execute("""SELECT m.conversation_id,COUNT(*),LIST(DISTINCT fe.file_path) FROM file_edits fe JOIN messages m ON m.id=fe.message_id WHERE fe.created_at>=? GROUP BY m.conversation_id""",[since]).fetchall()}
    tools={}
    for cid,status,count in db.execute("""SELECT m.conversation_id,COALESCE(tc.status,'unknown'),COUNT(*) FROM tool_calls tc JOIN messages m ON m.id=tc.message_id WHERE tc.created_at>=? GROUP BY m.conversation_id,COALESCE(tc.status,'unknown')""",[since]).fetchall(): tools.setdefault(cid,{})[status]=count
    db.close(); roots={cwd:_git_root(cwd) for cwd in {r[3] for r in sessions if r[3]}}; projects={}; redactions=omitted=0
    for cid,source,title,cwd,first,last_at,messages,users,assistants in sessions:
        if not cwd and not include_web: continue
        edit=edits.get(cid,{"edits":0,"paths":set()}); tool=tools.get(cid,{})
        if messages<min_messages and not edit["edits"] and not tool: omitted+=1; continue
        scope=roots.get(cwd,"web"); safe,n=_safe(dict(source=source,title=(title or "Untitled").replace("\n"," "),recorded_cwd=cwd)); redactions+=n; item=dict(conversation_id=cid,first_at=first,last_at=last_at,messages=messages,roles={"user":users,"assistant":assistants,"other":messages-users-assistants},edits={"edits":edit["edits"],"files":len(edit["paths"])},tool_status=tool,last=last[cid],read=f"convos read {cid[:8]}",**safe); project=projects.setdefault(scope,dict(scope=scope,sessions=[],messages=0,conversations=0,roles=Counter(),sources=Counter(),edits=0,file_set=set(),tools=Counter(),last_at=last_at))
        project["sessions"].append(item); project["messages"]+=messages; project["conversations"]+=1; project["roles"].update(item["roles"]); project["sources"][source]+=1; project["edits"]+=item["edits"]["edits"]; project["file_set"].update(edit["paths"]); project["tools"].update(item["tool_status"]); project["last_at"]=max(project["last_at"],last_at)
    ordered=sorted(projects.values(),key=lambda p:(p["last_at"],p["scope"]),reverse=True)
    for project in ordered:
        project["roles"],project["sources"],project["tools"]=map(dict,(project["roles"],project["sources"],project["tools"])); project["files"]=len(project.pop("file_set")); project["sessions"]=project["sessions"][:session_limit]; safe,n=_safe(project["scope"]); project["scope"]=safe; redactions+=n
    totals=dict(projects=len(ordered),conversations=sum(p["conversations"] for p in ordered),messages=sum(p["messages"] for p in ordered),edits=sum(p["edits"] for p in ordered),files=sum(p["files"] for p in ordered),tools=dict(sum((Counter(p["tools"]) for p in ordered),Counter())))
    return dict(status="ready" if ordered else "no_activity",generated_at=now.isoformat(),since=since.replace(tzinfo=timezone.utc).isoformat(),days=days,include_web=include_web,min_messages=min_messages,totals=totals,projects=ordered[:project_limit],projects_truncated=max(0,len(ordered)-project_limit),short_sessions_omitted=omitted,redactions=redactions)
def render(data):
    typer.echo(f"# Convos pulse\n\nExact local archive activity since `{data['since']}`. Counts are factual; no task status is inferred.")
    total=data["totals"]; typer.echo(f"\nProjects: {total['projects']} | conversations: {total['conversations']} | messages: {total['messages']} | edits: {total['edits']} | files: {total['files']}")
    for project in data["projects"]:
        typer.echo(f"\n## {project['scope']}\n\n- Last activity: `{project['last_at']}`\n- Conversations: {project['conversations']}; messages: {project['messages']}; edits: {project['edits']}; files: {project['files']}\n- Sources: "+", ".join(f"{k}={v}" for k,v in sorted(project["sources"].items())))
        for row in project["sessions"]: typer.echo(f"  - `{row['last_at']}` [{row['source']}] {row['title']} - {row['messages']} messages, {row['edits']['edits']} edits, last role `{row['last']['role']}` (`{row['last']['message_id']}`); `{row['read']}`")
    data["projects_truncated"] and typer.echo(f"\n{data['projects_truncated']} additional project(s) omitted."); typer.echo(f"\nShort sessions without captured edits/tools omitted: {data['short_sessions_omitted']}. Secret-shaped metadata spans masked: {data['redactions']}.")
def html_data(data):
    esc=lambda v:html.escape(str(v)); total=data["totals"]; cards=[]
    for project in data["projects"]:
        sessions="".join(f"<li><time>{esc(s['last_at'])}</time><strong>{esc(s['title'])}</strong><span>{esc(s['source'])} / {s['messages']} messages / {s['edits']['edits']} edits / last {esc(s['last']['role'])}</span><code>{esc(s['conversation_id'])}</code></li>" for s in project["sessions"])
        cards.append(f"<article><header><h2>{esc(project['scope'])}</h2><time>{esc(project['last_at'])}</time></header><div class=metrics><b>{project['conversations']} conversations</b><b>{project['messages']} messages</b><b>{project['edits']} edits</b><b>{project['files']} files</b></div><ul>{sessions}</ul></article>")
    return f"""<!doctype html><html><head><meta charset=utf-8><meta name=robots content=noindex,nofollow><meta http-equiv=Content-Security-Policy content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"><meta name=viewport content="width=device-width,initial-scale=1"><title>Convos pulse</title><style>:root{{color-scheme:dark;--bg:#0d1117;--card:#161b22;--line:#30363d;--text:#e6edf3;--muted:#8b949e;--accent:#7ee787}}*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(circle at 20% 0,#182235,var(--bg) 35%);color:var(--text);font:15px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}}main{{width:min(1180px,92vw);margin:48px auto}}h1{{font-size:clamp(36px,7vw,82px);letter-spacing:-.07em;margin:0}}.sub{{color:var(--muted)}}.hero{{display:flex;gap:12px;flex-wrap:wrap;margin:28px 0 38px}}.hero b,.metrics b{{border:1px solid var(--line);border-radius:999px;padding:7px 12px;color:var(--accent)}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:18px}}article{{background:linear-gradient(145deg,#1a2029,var(--card));border:1px solid var(--line);border-radius:16px;padding:20px;box-shadow:0 18px 50px #0005}}article header{{display:flex;justify-content:space-between;gap:12px}}h2{{font-size:17px;overflow-wrap:anywhere;margin:0}}time,.sub,li span{{color:var(--muted)}}.metrics{{display:flex;gap:7px;flex-wrap:wrap;margin:18px 0}}.metrics b{{font-size:12px;color:var(--text)}}ul{{list-style:none;padding:0;margin:0}}li{{border-top:1px solid var(--line);padding:12px 0;display:grid;gap:3px}}li strong{{overflow-wrap:anywhere}}code{{color:var(--accent)}}footer{{margin:32px 0;color:var(--muted)}}</style></head><body><main><p class=sub>LOCAL ACTIVITY / {esc(data['since'])}</p><h1>Convos pulse</h1><div class=hero><b>{total['projects']} projects</b><b>{total['conversations']} conversations</b><b>{total['messages']} messages</b><b>{total['edits']} edits</b></div><section class=grid>{''.join(cards)}</section><footer>Generated {esc(data['generated_at'])}. Exact metadata only; no task status inferred. {data['short_sessions_omitted']} short session(s) without edits/tools omitted. {data['redactions']} secret-shaped span(s) masked.</footer></main></body></html>"""
def _write(path,content):
    path=path.expanduser(); path=path if path.is_absolute() else Path.cwd()/path
    if path.is_symlink() or path.exists() and not path.is_file(): raise ValueError("Pulse output must be a regular non-symlink file")
    path=path.resolve(); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(f".{path.name}.{os.getpid()}"); tmp.touch(mode=0o600,exist_ok=False); tmp.write_text(content); os.replace(tmp,path); return path
def pulse_cmd(days:int=typer.Option(1,"-d",min=1,max=365),projects:int=typer.Option(12,"-n",min=1,max=50),sessions:int=typer.Option(3,"--sessions",min=1,max=10),min_messages:int=typer.Option(2,"--min-messages",min=1,max=100),include_web:bool=typer.Option(False,"--include-web"),fmt:str=typer.Option("markdown","-f","--format"),output:Optional[Path]=typer.Option(None,"-o","--output"),open_:bool=typer.Option(False,"--open")):
    """Show exact recent AI activity across projects."""
    if fmt not in ("markdown","json","html"): raise typer.BadParameter("must be markdown, json, or html","--format")
    if output and fmt!="html" or open_ and fmt!="html": raise typer.BadParameter("--output and --open require --format html")
    data=pulse_data(days,projects,sessions,include_web,min_messages); content=json.dumps(data,default=str) if fmt=="json" else html_data(data) if fmt=="html" else None
    if fmt=="markdown": render(data); return
    if output or open_:
        path=_write(output or Path(PROJECT_ROOT)/"pulse/pulse.html",content); typer.echo(str(path)); open_ and webbrowser.open(path.as_uri()); return
    typer.echo(content)
def register(app): app.command("pulse")(pulse_cmd)
