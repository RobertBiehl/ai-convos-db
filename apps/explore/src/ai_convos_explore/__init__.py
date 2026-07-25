"""Local semantic navigation across the conversation archive."""
import base64, json, os, tempfile, webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import typer

_CLEAN = """COALESCE(m.content,'')!='' AND json_extract_string(m.metadata,'$.history_of') IS NULL
AND NOT regexp_matches(m.content,'^(Base directory for this skill:|# AGENTS\\.md instructions for|<(codex_internal_context|environment_context|local-command-caveat|recommended_plugins|skill)( |>))')"""

def _fail(message):
    typer.echo(message,err=True); raise typer.Exit(1)
def _db():
    from ai_convos.cli import get_db
    try: db=get_db(read_only=True)
    except ValueError as e: _fail(str(e))
    if db is None: _fail("No database. Run `convos init` first.")
    return db
def _clip(value,n): return (value or "")[:n]+("..." if value and len(value)>n else "")
def _target(db,target):
    convs=db.execute("SELECT id,title,source,cwd FROM conversations WHERE starts_with(id,?) ORDER BY id=? DESC,id LIMIT 3",(target,target)).fetchall()
    msgs=db.execute("""SELECT m.id,c.id,c.title,c.source,c.cwd,m.role,m.content,m.embedding FROM messages m JOIN conversations c ON c.id=m.conversation_id
        WHERE starts_with(m.id,?) AND json_extract_string(m.metadata,'$.history_of') IS NULL ORDER BY m.id=? DESC,m.id LIMIT 3""",(target,target)).fetchall()
    exact=[("conversation",*r) for r in convs if r[0]==target]+[("message",*r) for r in msgs if r[0]==target]; found=exact or [("conversation",*r) for r in convs]+[("message",*r) for r in msgs]
    if len(found)!=1: _fail("No matching target" if not found else "Ambiguous target prefix: "+", ".join(r[1] for r in found))
    kind,*row=found[0]
    if kind=="message":
        mid,cid,title,source,cwd,role,content,vector=row
        if vector is None: _fail("Target message has no embedding. Run `convos embed`.")
        return dict(type=kind,id=mid,conversation_id=cid,title=title,source=source,cwd=cwd,role=role,content=content,vector=vector)
    cid,title,source,cwd=row; vectors=db.execute(f"""SELECT m.embedding FROM messages m WHERE m.conversation_id=? AND m.embedding IS NOT NULL AND m.role IN ('user','human') AND {_CLEAN}
        ORDER BY m.created_at DESC NULLS LAST,m.id DESC LIMIT 32""",(cid,)).fetchall() or db.execute(f"""SELECT m.embedding FROM messages m WHERE m.conversation_id=? AND m.embedding IS NOT NULL AND {_CLEAN}
        ORDER BY m.created_at DESC NULLS LAST,m.id DESC LIMIT 32""",(cid,)).fetchall()
    if not vectors: _fail("Target conversation has no usable embeddings. Run `convos embed`.")
    return dict(type=kind,id=cid,conversation_id=cid,title=title,source=source,cwd=cwd,role=None,content=None,vector=[sum(x)/len(vectors) for x in zip(*(r[0] for r in vectors))])
def _neighbors(db,seed,source=None,days=None,role=None,limit=10,context=300,minimum=-1.0,exclude=(),exclude_content=()):
    skip=sorted(set(exclude)|{seed["conversation_id"]}); where=[f"c.id NOT IN ({','.join('?' for _ in skip)})"]; params=[seed["vector"],*skip]
    if exclude_content: content=sorted(exclude_content); where.append(f"m.content NOT IN ({','.join('?' for _ in content)})"); params+=content
    if source: where.append("c.source=?"); params.append(source)
    if days: where.append("m.created_at>?"); params.append(datetime.now()-timedelta(days=days))
    if role: where.append("m.role=?"); params.append(role)
    rows=db.execute(f"""WITH scored AS (SELECT c.id conversation_id,c.title,c.source,c.cwd,c.updated_at,m.id message_id,m.role,m.content,m.created_at,m.embedding vector,
            array_cosine_similarity(m.embedding,?::FLOAT[768]) similarity FROM messages m JOIN conversations c ON c.id=m.conversation_id
            WHERE m.embedding IS NOT NULL AND {_CLEAN} AND {' AND '.join(where)}),
        distinct_turns AS (SELECT *,ROW_NUMBER() OVER(PARTITION BY content ORDER BY updated_at DESC NULLS LAST,message_id) duplicate_rank FROM scored),
        ranked AS (SELECT *,ROW_NUMBER() OVER(PARTITION BY conversation_id ORDER BY similarity DESC,message_id) rank FROM distinct_turns WHERE duplicate_rank=1 AND similarity>=?)
        SELECT similarity,message_id,role,content,created_at,title,source,conversation_id,cwd,vector FROM ranked WHERE rank=1 ORDER BY similarity DESC,conversation_id LIMIT ?""",params+[minimum,limit]).fetchall()
    return [dict(target_type=seed["type"],target_id=seed["id"],target_conversation_id=seed["conversation_id"],similarity=score,message_id=mid,role=r,content=_clip(content,context),raw_content=content,created_at=ts,title=title,source=src,conversation_id=cid,cwd=cwd,vector=vector) for score,mid,r,content,ts,title,src,cid,cwd,vector in rows]
def _public(row): return {k:v for k,v in row.items() if k not in ("vector","raw_content")}
def _child(row): return dict(type="message",id=row["message_id"],conversation_id=row["conversation_id"],title=row["title"],source=row["source"],cwd=row["cwd"],role=row["role"],content=row["content"],vector=row["vector"])
def _walk(db,root,depth=2,width=3,max_nodes=20,minimum=.65,source=None,days=None,role=None,context=160):
    nodes=[dict(conversation_id=root["conversation_id"],title=root["title"],source=root["source"],cwd=root["cwd"],depth=0,seed_type=root["type"],seed_id=root["id"],role=root["role"],content=_clip(root["content"],context))]; edges=[]; frontier=[root]; seen={root["conversation_id"]}; contents={root["content"]} if root["content"] else set()
    for level in range(1,depth+1):
        upcoming=[]
        for parent in frontier:
            if len(nodes)>=max_nodes: break
            for row in _neighbors(db,parent,source,days,role,min(width,max_nodes-len(nodes)),context,minimum,seen,contents):
                child=_child(row); seen.add(child["conversation_id"]); contents.add(row["raw_content"]); upcoming.append(child); node=dict(conversation_id=child["conversation_id"],title=child["title"],source=child["source"],cwd=child["cwd"],depth=level,seed_type="message",seed_id=child["id"],role=child["role"],content=child["content"]); nodes.append(node); edges.append(dict(depth=level,from_conversation_id=parent["conversation_id"],to_conversation_id=child["conversation_id"],similarity=row["similarity"],message_id=row["message_id"],role=row["role"],content=row["content"],created_at=row["created_at"]))
        frontier=upcoming
        if not frontier or len(nodes)>=max_nodes: break
    return dict(root=nodes[0],nodes=nodes,edges=edges)
_HTML="""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'">
<title>Convos semantic map</title><style>
:root{color-scheme:dark;--bg:#111318;--panel:#1a1e26;--line:#5d6b85;--text:#edf1f7;--muted:#9ba8bb;--accent:#79b8ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px ui-monospace,SFMono-Regular,Menlo,monospace}
header{height:64px;padding:12px 18px;border-bottom:1px solid #303744;background:#151820}header h1{font-size:16px;margin:0 0 6px}header p{color:var(--muted);margin:0}
#layout{display:grid;grid-template-columns:minmax(0,1fr) 360px;height:calc(100vh - 64px)}#viewport{overflow:auto;position:relative}
#stage{position:relative;min-height:100%;min-width:100%}svg{position:absolute;inset:0;overflow:visible}
.node{position:absolute;width:240px;min-height:74px;padding:10px;text-align:left;color:var(--text);background:var(--panel);border:1px solid #465166;border-left:4px solid var(--accent);border-radius:8px;cursor:pointer;box-shadow:0 4px 16px #0005}
.node:hover,.node:focus{border-color:#9ac7ff;outline:none}.node strong,.node small{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.node small{color:var(--muted);margin-top:7px}
aside{border-left:1px solid #303744;background:#151820;padding:18px;overflow:auto}aside h2{font-size:15px;margin:0 0 10px}aside .meta{color:var(--muted);line-height:1.5;white-space:pre-wrap}
pre{white-space:pre-wrap;word-break:break-word;background:#0e1015;border:1px solid #303744;padding:12px;border-radius:7px;line-height:1.45}code{color:#b7d7ff}
.edge{stroke:var(--line);stroke-width:1.5}.score{fill:#a9b9d0;font-size:11px}
@media(max-width:800px){#layout{grid-template-columns:1fr;grid-template-rows:65vh auto}aside{border-left:0;border-top:1px solid #303744}}
</style></head><body><header><h1 id="heading">Convos semantic map</h1><p id="summary"></p></header>
<div id="layout"><main id="viewport"><div id="stage"><svg id="edges"></svg><div id="nodes"></div></div></main>
<aside><h2 id="detail-title">Select a conversation</h2><div class="meta" id="detail-meta"></div><pre id="detail-content"></pre><code id="detail-command"></code></aside></div>
<script>
const bytes=Uint8Array.from(atob("__PAYLOAD__"),c=>c.charCodeAt(0)),data=JSON.parse(new TextDecoder().decode(bytes));
const layers=new Map();for(const n of data.nodes){const a=layers.get(n.depth)||[];n.order=a.length;a.push(n);layers.set(n.depth,a)}
const maxDepth=Math.max(...data.nodes.map(n=>n.depth)),maxRows=Math.max(...[...layers.values()].map(a=>a.length)),W=(maxDepth+1)*310+80,H=Math.max(620,maxRows*118+100),pos=new Map();
for(const n of data.nodes)pos.set(n.conversation_id,{x:40+n.depth*310,y:50+n.order*118});
const stage=document.getElementById("stage"),svg=document.getElementById("edges");stage.style.width=W+"px";stage.style.height=H+"px";svg.setAttribute("viewBox",`0 0 ${W} ${H}`);svg.setAttribute("width",W);svg.setAttribute("height",H);
const S=(tag,attrs)=>{const e=document.createElementNS("http://www.w3.org/2000/svg",tag);for(const [k,v] of Object.entries(attrs))e.setAttribute(k,v);return e};
for(const e of data.edges){const a=pos.get(e.from_conversation_id),b=pos.get(e.to_conversation_id);svg.append(S("line",{x1:a.x+240,y1:a.y+37,x2:b.x,y2:b.y+37,class:"edge"}));const t=S("text",{x:(a.x+b.x+240)/2,y:(a.y+b.y)/2+30,class:"score"});t.textContent=e.similarity.toFixed(3);svg.append(t)}
const edgeTo=new Map(data.edges.map(e=>[e.to_conversation_id,e])),nodes=document.getElementById("nodes"),title=document.getElementById("detail-title"),meta=document.getElementById("detail-meta"),body=document.getElementById("detail-content"),command=document.getElementById("detail-command");
function select(n){const e=edgeTo.get(n.conversation_id);title.textContent=n.title||"Untitled";meta.textContent=`${n.source} | depth ${n.depth}\nconversation ${n.conversation_id}`+(e?`\nmessage ${e.message_id} | similarity ${e.similarity.toFixed(3)}\n${e.role} @ ${e.created_at||"?"}`:"");body.textContent=(e&&e.content)||n.content||"Root conversation";command.textContent=e?`convos read ${n.conversation_id.slice(0,8)} --around ${e.message_id.slice(0,8)}`:`convos read ${n.conversation_id.slice(0,8)}`}
for(const n of data.nodes){const p=pos.get(n.conversation_id),b=document.createElement("button"),strong=document.createElement("strong"),small=document.createElement("small");b.className="node";b.style.left=p.x+"px";b.style.top=p.y+"px";strong.textContent=n.title||"Untitled";small.textContent=`${n.source} | ${n.conversation_id.slice(0,8)} | depth ${n.depth}`;b.append(strong,small);b.addEventListener("click",()=>select(n));nodes.append(b)}
document.getElementById("heading").textContent=`Semantic map: ${data.root.title||"Untitled"}`;document.getElementById("summary").textContent=`${data.nodes.length} conversations | ${data.edges.length} evidence edges | local read-only artifact`;select(data.root);
</script></body></html>"""
def _html(result): return _HTML.replace("__PAYLOAD__",base64.b64encode(json.dumps(result,default=str,separators=(",",":")).encode()).decode())
def _write(fd,text):
    with os.fdopen(fd,"w") as f: f.write(text); f.flush(); os.fsync(f.fileno())
def _artifact(text,output=None):
    if output is None:
        fd,name=tempfile.mkstemp(prefix="convos-semantic-map-",suffix=".html"); path=Path(name)
        try: _write(fd,text)
        except BaseException: path.unlink(missing_ok=True); raise
        return path
    requested=Path(output).expanduser()
    if requested.is_symlink() or requested.exists(): _fail(f"Output already exists or is unsafe: {requested}")
    requested.parent.mkdir(parents=True,exist_ok=True); path=requested.parent.resolve()/requested.name; fd,name=tempfile.mkstemp(prefix=f".{path.name}.",dir=path.parent); tmp=Path(name)
    try:
        _write(fd,text); os.chmod(tmp,0o600)
        try: os.link(tmp,path)
        except FileExistsError: _fail(f"Output already exists or is unsafe: {path}")
    finally: tmp.unlink(missing_ok=True)
    return path
def related(target: str, source: Optional[str]=typer.Option(None,"-s"), days: Optional[int]=typer.Option(None,"-d",min=1), role: Optional[str]=typer.Option(None,"-r"), limit: int=typer.Option(10,"-n",min=1,max=100), context: int=typer.Option(300,"-c",min=1), fmt: str=typer.Option("text","-f","--format")):
    """Find conversations semantically related to a conversation or exact turn."""
    db=_db()
    seed=_target(db,target); data=[_public(row) for row in _neighbors(db,seed,source,days,role,limit,context)]; db.close()
    if fmt!="text":
        if fmt=="jsonl": [typer.echo(json.dumps(row,default=str)) for row in data]
        else: typer.echo(json.dumps(data,default=str))
        return
    typer.echo(f"Related to {seed['type']} {seed['id']} in [{seed['source']}] {seed['title'] or 'Untitled'} ({seed['conversation_id']})")
    if not data: typer.echo("No related conversations"); return
    for i,row in enumerate(data,1): typer.echo(f"\n{i}. {row['similarity']:.3f} [{row['source']}] {row['title'] or 'Untitled'} ({row['conversation_id']})\n   {row['role']} @ {row['created_at'] or '?'} ({row['message_id']})\n   {row['content']}\n   read: convos read {row['conversation_id'][:8]} --around {row['message_id'][:8]}")
def trail(target: str, depth: int=typer.Option(2,"--depth",min=1,max=3), width: int=typer.Option(3,"--width",min=1,max=8), max_nodes: int=typer.Option(20,"--max-nodes",min=2,max=100), minimum: float=typer.Option(.65,"--min-score",min=-1,max=1), source: Optional[str]=typer.Option(None,"-s"), days: Optional[int]=typer.Option(None,"-d",min=1), role: Optional[str]=typer.Option(None,"-r"), context: int=typer.Option(160,"-c",min=1), fmt: str=typer.Option("text","-f","--format")):
    """Walk a bounded multi-hop semantic trail with exact evidence."""
    db=_db(); root=_target(db,target); result=_walk(db,root,depth,width,max_nodes,minimum,source,days,role,context); db.close(); nodes,edges=result["nodes"],result["edges"]
    if fmt=="json": typer.echo(json.dumps(result,default=str)); return
    if fmt=="jsonl":
        typer.echo(json.dumps(dict(record="root",**nodes[0]),default=str))
        [typer.echo(json.dumps(dict(record="edge",**edge,node=next(n for n in nodes if n["conversation_id"]==edge["to_conversation_id"])),default=str)) for edge in edges]; return
    if fmt=="dot":
        esc=lambda value:str(value or "Untitled").replace("\\","\\\\").replace('"','\\"').replace("\n"," ")
        typer.echo("digraph trail {\n"+ "\n".join([f'  "{n["conversation_id"]}" [label="{esc(n["source"])} | {esc(n["title"])} | {n["conversation_id"][:8]}"];' for n in nodes]+[f'  "{e["from_conversation_id"]}" -> "{e["to_conversation_id"]}" [label="{e["similarity"]:.3f} | {e["message_id"][:8]}"];' for e in edges])+"\n}"); return
    typer.echo(f"Semantic trail from {root['type']} {root['id']}\n[0] [{root['source']}] {root['title'] or 'Untitled'} ({root['conversation_id']})")
    by_id={n["conversation_id"]:n for n in nodes}
    for edge in edges:
        node=by_id[edge["to_conversation_id"]]; indent="  "*(edge["depth"]-1); typer.echo(f"\n{indent}-> {edge['similarity']:.3f} [{edge['from_conversation_id'][:8]} -> {node['conversation_id'][:8]}] [{node['source']}] {node['title'] or 'Untitled'} ({node['conversation_id']})\n{indent}   evidence: {edge['role']} @ {edge['created_at'] or '?'} ({edge['message_id']})\n{indent}   {edge['content']}\n{indent}   read: convos read {node['conversation_id'][:8]} --around {edge['message_id'][:8]}")
def map_cmd(target: str, depth: int=typer.Option(2,"--depth",min=1,max=3), width: int=typer.Option(3,"--width",min=1,max=8), max_nodes: int=typer.Option(20,"--max-nodes",min=2,max=100), minimum: float=typer.Option(.65,"--min-score",min=-1,max=1), source: Optional[str]=typer.Option(None,"-s"), days: Optional[int]=typer.Option(None,"-d",min=1), role: Optional[str]=typer.Option(None,"-r"), context: int=typer.Option(600,"-c",min=1), output: Optional[Path]=typer.Option(None,"-o","--output"), open_browser: bool=typer.Option(True,"--open/--no-open")):
    """Open a private self-contained visual semantic map."""
    db=_db(); root=_target(db,target); result=_walk(db,root,depth,width,max_nodes,minimum,source,days,role,context); db.close(); path=_artifact(_html(result),output).resolve(); opened=False
    if open_browser:
        try: opened=webbrowser.open(path.as_uri(),new=2)
        except Exception as e: typer.echo(f"Browser did not open: {e}",err=True)
    typer.echo(f"Semantic map: {path}{' (browser opened)' if opened else ''}")
def register(app: typer.Typer):
    for command in (related,trail): app.command()(command)
    app.command("map")(map_cmd)
