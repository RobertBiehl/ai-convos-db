"""Private loopback-only browser library for the local conversation archive."""
import json, secrets, webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import typer
from typer.testing import CliRunner

PAGE="""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><meta name=robots content="noindex,nofollow"><title>Convos library</title><style>:root{color-scheme:dark;--bg:#090c10;--panel:#11161d;--line:#28313d;--text:#edf3f8;--muted:#8b98a7;--accent:#79c0ff;--warm:#f2cc60}*{box-sizing:border-box}body{margin:0;background:linear-gradient(130deg,#101826 0,var(--bg) 42%);color:var(--text);font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace}main{width:min(1500px,96vw);margin:26px auto}header{display:flex;justify-content:space-between;align-items:end;gap:20px;margin-bottom:22px}h1{font:700 clamp(30px,5vw,64px)/.9 ui-sans-serif,system-ui;letter-spacing:-.06em;margin:0}.eyebrow,.muted{color:var(--muted)}form{display:grid;grid-template-columns:minmax(220px,1fr) repeat(5,minmax(90px,150px)) auto;gap:8px;padding:12px;background:#0d1218cc;border:1px solid var(--line);border-radius:14px;position:sticky;top:8px;z-index:2;backdrop-filter:blur(12px)}input,select,button{font:inherit;color:var(--text);background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:10px}button{cursor:pointer}button:hover,button:focus{border-color:var(--accent)}form button{background:var(--accent);color:#06111c;font-weight:800}.layout{display:grid;grid-template-columns:minmax(300px,430px) 1fr;gap:14px;margin-top:14px;min-height:70vh}.pane{border:1px solid var(--line);background:#0d1218cc;border-radius:14px;overflow:hidden}.pane>h2{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);padding:14px 16px;margin:0;border-bottom:1px solid var(--line)}#results{max-height:75vh;overflow:auto}.hit{width:100%;text-align:left;border:0;border-bottom:1px solid var(--line);border-radius:0;background:transparent;padding:15px}.hit strong,.hit span,.hit small{display:block}.hit strong{margin:4px 0}.hit span,.hit small{color:var(--muted)}#thread{padding:20px;max-height:75vh;overflow:auto}.conversation{margin-bottom:22px}.conversation h2{font:700 28px/1.1 ui-sans-serif,system-ui;margin:0 0 8px}.message{border-left:3px solid var(--line);padding:12px 15px;margin:13px 0;background:#111720}.message .role{color:var(--warm);font-weight:800;text-transform:uppercase;font-size:11px}.message pre,.activity pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:8px 0 0;font:inherit}.activity{margin:10px 0 0;border:1px solid var(--line);border-radius:8px;background:#0b1016;padding:8px 10px}.activity summary{cursor:pointer;color:var(--accent);font-weight:700}.activity.edit summary{color:var(--warm)}.empty{padding:30px;color:var(--muted)}@media(max-width:900px){form{grid-template-columns:1fr 1fr}.layout{grid-template-columns:1fr}.pane,#results,#thread{max-height:none}header{align-items:start;flex-direction:column}}</style><script src=app.js defer></script></head><body><main><header><div><p class=eyebrow>PRIVATE / LOCAL / READ ONLY</p><h1>Convos library</h1></div><p class=muted>Search every provider. Replay exact messages, tools, and edits.</p></header><form id=search><input id=q name=q placeholder="Search exact words..." required autofocus><select name=engine><option value=literal>exact words</option><option value=hybrid>semantic + exact</option></select><select name=source><option value="">all sources</option><option>codex</option><option>claude-code</option><option>chatgpt</option><option>claude</option></select><select name=role><option value="">all roles</option><option>user</option><option>assistant</option><option>human</option></select><input name=days type=number min=1 placeholder="days"><input name=cwd placeholder="project path"><button>Search</button></form><section class=layout><aside class=pane><h2 id=count>Results</h2><div id=results><p class=empty>Search the local archive to begin.</p></div></aside><article class=pane><h2>Session replay</h2><div id=thread><p class=empty>Select a result to replay its bounded evidence.</p></div></article></section></main></body></html>"""
JS="""const $=s=>document.querySelector(s),base=location.pathname,el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n};async function api(path,params){const r=await fetch(base+"api/"+path+"?"+new URLSearchParams(params));const x=await r.json();if(!r.ok)throw Error(x.error||"Request failed");return x}function empty(target,text){target.replaceChildren(el("p","empty",text))}function activity(a){const box=el("details","activity "+a.kind),label=a.kind==="tool"?"TOOL "+(a.name||"?")+" / "+(a.status||"?")+(a.duration_ms!==null&&a.duration_ms!==undefined?" / "+a.duration_ms+"ms":""):"EDIT "+(a.type||"?")+" / "+(a.path||"?"),text=a.kind==="tool"?["INPUT",a.input||"","OUTPUT",a.output||""].join("\\n"):["BEFORE",a.before||"","AFTER",a.after||""].join("\\n");box.append(el("summary","",label),el("pre","",text));return box}async function openHit(hit){const box=$("#thread");empty(box,"Loading session evidence...");try{const data=await api("thread",{conversation:hit.conversation_id,around:hit.message_id}),head=el("div","conversation"),title=el("h2","",data.title||"Untitled"),counts=data.counts.tools+" tools / "+data.counts.edits+" edits";head.append(title,el("p","muted",data.source+" / "+data.conversation_id+(data.cwd?" / "+data.cwd:"")+" / "+counts));const nodes=data.messages.map(m=>{const card=el("section","message"),meta=el("div","role",m.role+" / "+(m.created_at||"?")+" / "+m.id),body=el("pre","",m.content||"");card.append(meta,body,...(m.activity||[]).map(activity));return card});if(data.activity_truncated)nodes.push(el("p","empty","Activity limit reached; use convos replay for a larger bounded view."));box.replaceChildren(head,...nodes)}catch(e){empty(box,e.message)}}function show(hits){const box=$("#results");$("#count").textContent=hits.length+" results";if(!hits.length)return empty(box,"No matching conversations.");box.replaceChildren(...hits.map(hit=>{const b=el("button","hit"),meta=el("small","",hit.source+" / "+(hit.created_at||"?")),title=el("strong","",hit.title||"Untitled"),snippet=el("span","",hit.content||"");b.type="button";b.append(meta,title,snippet);b.addEventListener("click",()=>openHit(hit));return b}))}$("#search").addEventListener("submit",async e=>{e.preventDefault();const box=$("#results"),params=Object.fromEntries(new FormData(e.currentTarget));empty(box,"Searching...");try{show(await api("search",params))}catch(err){empty(box,err.message)}});"""

def _core(): from ai_convos import cli; return cli
def drain_hooks(*a,**k): return _core().drain_hooks(*a,**k)
def get_db(*a,**k): return _core().get_db(*a,**k)
def hybrid_hits(*a,**k): return _core().hybrid_hits(*a,**k)
def _invoke(args):
    result=CliRunner().invoke(_core().app,args)
    if result.exit_code: raise ValueError(result.output.strip() or "Archive command failed")
    try: return json.loads(result.output)
    except json.JSONDecodeError as e: raise ValueError(result.output.strip() or "Archive command failed") from e
def search_data(params):
    q=params.get("q",[""])[0].strip(); engine=params.get("engine",["literal"])[0]
    if not q: return []
    if len(q)>500: raise ValueError("Query is too long")
    if engine not in ("literal","hybrid"): raise ValueError("Invalid search engine")
    try: n=max(1,min(30,int(params.get("limit",["20"])[0]))); days=params.get("days",[""])[0]; days=int(days) if days else None
    except ValueError as e: raise ValueError("Invalid numeric filter") from e
    values={key:params.get(key,[""])[0].strip() for key in ("source","role","cwd")}
    if days is not None and days<1: raise ValueError("Days must be positive")
    if engine=="hybrid":
        return [{**r,"content":(c:=r["content"] or "")[:500]+("..." if len(c)>500 else "")} for r in hybrid_hits(q,values["source"] or None,days,values["role"] or None,n,local_only=True,cwd=values["cwd"] or None)]
    args=["search",q,"-n",str(n),"-c","500","-f","json"]
    for key,flag in (("source","-s"),("role","-r"),("cwd","--cwd")):
        if value:=values[key]: args.extend((flag,value))
    if days:
        args.extend(("-d",str(days)))
    return _invoke(args)
def _clip(value,n): return None if value is None else str(value)[:n]+("..." if len(str(value))>n else "")
def replay_data(ref,around="",limit=20,context=2000,activity=100):
    if not ref or len(ref)>64 or len(around)>64: raise ValueError("Invalid conversation reference")
    drain_hooks(); db=get_db(read_only=True)
    if db is None: raise ValueError("Archive not found")
    rows=db.execute("SELECT id,title,source,cwd FROM conversations WHERE starts_with(id,?) ORDER BY updated_at DESC NULLS LAST LIMIT 2",[ref]).fetchall(); db.close()
    if len(rows)!=1: raise ValueError("Conversation reference is missing or ambiguous")
    cid,title,source,cwd=rows[0]; args=["read",cid,"-n",str(limit),"-c",str(context),"-f","json"]
    if around: args.extend(("--around",around))
    messages=_invoke(args); mids=[m["id"] for m in messages]; events=[]
    if mids and activity:
        values=",".join("(?,?)" for _ in mids); params=[x for i,mid in enumerate(mids) for x in (mid,i)]+[activity+1]; db=get_db(read_only=True)
        rows=db.execute(f"""WITH chosen(id,pos) AS (VALUES {values}), events AS (
            SELECT c.pos,'tool' kind,t.id,t.message_id,t.created_at,t.tool_name event_label,t.status,t.duration_ms,CAST(t.input AS VARCHAR) before_text,CAST(t.output AS VARCHAR) after_text FROM tool_calls t JOIN chosen c ON c.id=t.message_id
            UNION ALL SELECT c.pos,'edit',e.id,e.message_id,e.created_at,e.file_path,e.edit_type,NULL,e.old_content,e.content FROM file_edits e JOIN chosen c ON c.id=e.message_id)
            SELECT kind,id,message_id,created_at,event_label,status,duration_ms,before_text,after_text FROM events ORDER BY pos,created_at NULLS LAST,id LIMIT ?""",params).fetchall(); db.close()
        events=[(mid,dict(kind=kind,id=eid,message_id=mid,created_at=str(at) if at else None,**(dict(name=label,status=status,duration_ms=duration,input=_clip(before,context),output=_clip(after,context)) if kind=="tool" else dict(path=label,type=status,before=_clip(before,context),after=_clip(after,context))))) for kind,eid,mid,at,label,status,duration,before,after in rows[:activity]]
    messages=[{**m,"activity":[e for mid,e in events if mid==m["id"]]} for m in messages]; counts={kind:sum(e["kind"]==kind for _,e in events) for kind in ("tool","edit")}
    return dict(conversation_id=cid,title=title,source=source,cwd=cwd,messages=messages,counts=dict(tools=counts["tool"],edits=counts["edit"]),activity_truncated=len(rows)>activity if mids and activity else False)
def thread_data(params):
    try: limit=max(1,min(40,int(params.get("limit",["40"])[0]))); context=max(1,min(4000,int(params.get("context",["4000"])[0]))); activity=max(0,min(120,int(params.get("activity",["120"])[0])))
    except ValueError as e: raise ValueError("Invalid replay bound") from e
    return replay_data(params.get("conversation",[""])[0],params.get("around",[""])[0],limit,context,activity)
def _handler(token):
    prefix=f"/{token}/"
    class Handler(BaseHTTPRequestHandler):
        def send(self,status,body,kind):
            raw=body if isinstance(body,bytes) else body.encode(); self.send_response(status); self.send_header("Content-Type",kind); self.send_header("Content-Length",str(len(raw))); self.send_header("Cache-Control","no-store"); self.send_header("Content-Security-Policy","default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"); self.send_header("Referrer-Policy","no-referrer"); self.send_header("Permissions-Policy","camera=(), microphone=(), geolocation=()"); self.send_header("Cross-Origin-Opener-Policy","same-origin"); self.send_header("Cross-Origin-Resource-Policy","same-origin"); self.send_header("X-Content-Type-Options","nosniff"); self.send_header("X-Frame-Options","DENY"); self.end_headers(); self.wfile.write(raw)
        def do_GET(self):
            url=urlsplit(self.path)
            if not url.path.startswith(prefix): self.send(404,b"Not found","text/plain; charset=utf-8"); return
            route=url.path[len(prefix):]
            try:
                if route=="": self.send(200,PAGE,"text/html; charset=utf-8")
                elif route=="app.js": self.send(200,JS,"text/javascript; charset=utf-8")
                elif route=="api/search": self.send(200,json.dumps(search_data(parse_qs(url.query)),default=str),"application/json")
                elif route=="api/thread": self.send(200,json.dumps(thread_data(parse_qs(url.query)),default=str),"application/json")
                else: self.send(404,b"Not found","text/plain; charset=utf-8")
            except ValueError as e: self.send(400,json.dumps({"error":str(e)}),"application/json")
            except Exception: self.send(500,json.dumps({"error":"Archive request failed"}),"application/json")
        def log_message(self,*_): pass
    return Handler
def make_server(port=0,token=None):
    token=token or secrets.token_urlsafe(24); server=HTTPServer(("127.0.0.1",port),_handler(token)); server.url=f"http://127.0.0.1:{server.server_port}/{token}/"; return server
def run_server(port=0,open_=True):
    server=make_server(port); typer.echo(server.url); open_ and webbrowser.open(server.url)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
def library_cmd(port:int=typer.Option(0,"--port",min=0,max=65535),open_:bool=typer.Option(True,"--open/--no-open")):
    """Open a private read-only browser for the local archive."""
    run_server(port,open_)
def replay_cmd(conversation:str,around:str=typer.Option("","--around","-a"),limit:int=typer.Option(20,"-n",min=1,max=100),context:int=typer.Option(2000,"-c",min=1,max=10000),activity:int=typer.Option(100,"--activity",min=0,max=200),fmt:str=typer.Option("text","-f","--format")):
    """Replay exact messages, tool calls, and edits from one conversation."""
    try: data=replay_data(conversation,around,limit,context,activity)
    except ValueError as e: typer.echo(str(e),err=True); raise typer.Exit(1)
    if fmt=="json": typer.echo(json.dumps(data,default=str)); return
    if fmt!="text": typer.echo("Format must be text or json",err=True); raise typer.Exit(1)
    lines=[f"[{data['source']}] {data['title'] or 'Untitled'}{f''' @ {data['cwd']}''' if data['cwd'] else ''} ({data['conversation_id']})"]
    for m in data["messages"]:
        lines.extend(("",f"{m['role']} @ {m['created_at'] or '?'} [{m['id']}]",m["content"] or ""))
        for a in m["activity"]:
            lines.extend((f"  {'TOOL '+(a['name'] or '?')+' '+(a['status'] or '?') if a['kind']=='tool' else 'EDIT '+(a['type'] or '?')+' '+(a['path'] or '?')} [{a['id']}]",*(f"    {k}: {a[k]}" for k in (("input","output") if a["kind"]=="tool" else ("before","after")) if a[k] is not None)))
    count=lambda n,s:f"{n} {s}{'' if n==1 else 's'}"; lines.append(f"\n{count(len(data['messages']),'message')}, {count(data['counts']['tools'],'tool')}, {count(data['counts']['edits'],'edit')}{', activity truncated' if data['activity_truncated'] else ''}"); typer.echo("\n".join(lines))
def register(app): app.command("library")(library_cmd); app.command("replay")(replay_cmd)
