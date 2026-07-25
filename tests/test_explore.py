import base64, json, os, re, subprocess, sys
from pathlib import Path

import duckdb
import typer
from typer.testing import CliRunner

from ai_convos import cli
import ai_convos_explore as explore
from ai_convos_explore import register

def emb(x,y=0.0):
    value=[0.0]*768; value[0],value[1]=x,y; return value
def archive(tmp_path,monkeypatch):
    path=tmp_path/"convos.db"; monkeypatch.setattr(cli,"DB_PATH",path); db=duckdb.connect(str(path)); cli.init_schema(db)
    db.executemany("INSERT INTO conversations (id,source,title,updated_at,cwd) VALUES (?,?,?,?,?)",[
        ("target000","codex","Memory API","2026-01-05","/repo"),("near0000","codex","Memory sync design","2026-01-04","/repo"),
        ("copy0000","codex","Copied design","2026-01-03","/repo"),("other000","chatgpt","Other topic","2026-01-02",None),("claude00","claude-code","Claude memory","2026-01-01","/repo"),("hop00000","chatgpt","Second hop","2025-12-31",None)])
    db.executemany("INSERT INTO messages (id,conversation_id,role,content,created_at,metadata,embedding) VALUES (?,?,?,?,?,'{}',?)",[
        ("noise000","target000","user","# AGENTS.md instructions for /repo","2026-01-01",emb(0,1)),("seed0000","target000","user","design a deterministic memory sync API","2026-01-02",emb(1)),
        ("nearmsg0","near0000","assistant","canonical revisions with deterministic sync","2026-01-03",emb(1,.1)),("copymsg0","copy0000","assistant","canonical revisions with deterministic sync","2026-01-02",emb(1,.1)),
        ("othermsg","other000","user","grow tomatoes in a greenhouse","2026-01-02",emb(0,1)),("claudem0","claude00","user","portable memory synchronization","2026-01-01",emb(1,.4)),("hopmsg00","hop00000","assistant","second hop evidence","2025-12-31",emb(.42,.91))])
    db.close(); app=typer.Typer(); app.command("dummy")(lambda: None); register(app); return app
def test_related_conversation_uses_clean_human_seed_and_collapses_duplicates(tmp_path,monkeypatch):
    app=archive(tmp_path,monkeypatch); result=CliRunner().invoke(app,["related","target","-f","json"]); rows=json.loads(result.output)
    assert result.exit_code==0 and rows[0]["conversation_id"]=="near0000" and rows[0]["message_id"]=="nearmsg0" and rows[0]["target_type"]=="conversation" and rows[0]["target_id"]=="target000"
    assert "target000" not in {r["conversation_id"] for r in rows} and "copy0000" not in {r["conversation_id"] for r in rows} and len({r["content"] for r in rows})==len(rows)
def test_related_exact_message_filters_and_emits_self_contained_jsonl(tmp_path,monkeypatch):
    app=archive(tmp_path,monkeypatch); result=CliRunner().invoke(app,["related","seed0000","-s","claude-code","-f","jsonl"]); rows=[json.loads(line) for line in result.output.splitlines()]
    assert result.exit_code==0 and [r["conversation_id"] for r in rows]==["claude00"] and rows[0]["target_type"]=="message" and rows[0]["target_id"]=="seed0000" and rows[0]["target_conversation_id"]=="target000"
def test_related_text_has_exact_read_pivot_and_bounds_content(tmp_path,monkeypatch):
    app=archive(tmp_path,monkeypatch); result=CliRunner().invoke(app,["related","seed0000","-n","1","-c","9"])
    assert result.exit_code==0 and "Related to message seed0000" in result.output and "canonical..." in result.output and "convos read near0000 --around nearmsg0" in result.output
def test_related_rejects_unknown_ambiguous_and_unembedded_targets(tmp_path,monkeypatch):
    app=archive(tmp_path,monkeypatch); db=duckdb.connect(str(tmp_path/"convos.db")); db.execute("INSERT INTO conversations (id,source,title) VALUES ('target111','x','ambiguous'),('blank000','x','blank')"); db.execute("INSERT INTO messages (id,conversation_id,role,content,metadata) VALUES ('blankmsg','blank000','user','not embedded','{}')"); db.close()
    assert CliRunner().invoke(app,["related","missing"]).exit_code==1 and CliRunner().invoke(app,["related","target"]).exit_code==1
    for target in ("blank000","blankmsg"):
        result=CliRunner().invoke(app,["related",target]); assert result.exit_code==1 and "convos embed" in result.output
def test_trail_walks_multiple_hops_without_cycles_or_duplicate_turns(tmp_path,monkeypatch):
    app=archive(tmp_path,monkeypatch); result=CliRunner().invoke(app,["trail","seed0000","--depth","2","--width","2","--min-score",".5","-f","json"]); data=json.loads(result.output)
    assert result.exit_code==0 and [n["conversation_id"] for n in data["nodes"]]==["target000","near0000","claude00","hop00000"]
    assert [(e["from_conversation_id"],e["to_conversation_id"]) for e in data["edges"]]==[("target000","near0000"),("target000","claude00"),("near0000","hop00000")]
    assert len({n["conversation_id"] for n in data["nodes"]})==len(data["nodes"]) and "copy0000" not in {n["conversation_id"] for n in data["nodes"]} and data["edges"][-1]["message_id"]=="hopmsg00"
def test_trail_bounds_filters_and_streams_self_contained_edges(tmp_path,monkeypatch):
    app=archive(tmp_path,monkeypatch); bounded=json.loads(CliRunner().invoke(app,["trail","seed0000","--max-nodes","2","-f","json"]).output); assert len(bounded["nodes"])==2 and len(bounded["edges"])==1
    filtered=json.loads(CliRunner().invoke(app,["trail","seed0000","-s","claude-code","-f","json"]).output); assert [n["source"] for n in filtered["nodes"][1:]]==["claude-code"]
    rows=[json.loads(line) for line in CliRunner().invoke(app,["trail","seed0000","--max-nodes","2","-f","jsonl"]).output.splitlines()]; assert rows[0]["record"]=="root" and rows[1]["record"]=="edge" and rows[1]["node"]["conversation_id"]==rows[1]["to_conversation_id"]
def test_trail_text_and_dot_keep_exact_evidence(tmp_path,monkeypatch):
    app=archive(tmp_path,monkeypatch); text=CliRunner().invoke(app,["trail","seed0000","--max-nodes","2","-c","9"]).output; dot=CliRunner().invoke(app,["trail","seed0000","--max-nodes","2","-f","dot"]).output
    assert "Semantic trail from message seed0000" in text and "evidence: assistant" in text and "convos read near0000 --around nearmsg0" in text and "canonical..." in text
    assert dot.startswith("digraph trail {") and '"target000" -> "near0000"' in dot and "nearmsg0" in dot
def test_explore_surfaces_locked_archive_without_traceback(tmp_path,monkeypatch):
    app=archive(tmp_path,monkeypatch); monkeypatch.setattr(cli,"get_db",lambda read_only=False: (_ for _ in ()).throw(ValueError("Archive is locked.")))
    for command in ("related","trail"):
        result=CliRunner().invoke(app,[command,"seed0000"]); assert result.exit_code==1 and "Archive is locked." in result.output and "Traceback" not in result.output
def map_payload(path):
    text=path.read_text(); encoded=re.search(r'atob\("([A-Za-z0-9+/=]+)"\)',text).group(1); return text,json.loads(base64.b64decode(encoded))
def test_map_is_private_self_contained_and_treats_archive_text_as_data(tmp_path,monkeypatch):
    app=archive(tmp_path,monkeypatch); evil='</script><img src=x onerror="alert(1)">'; db=duckdb.connect(str(tmp_path/"convos.db")); db.execute("DELETE FROM messages WHERE conversation_id='copy0000'"); db.execute("UPDATE conversations SET title=? WHERE id='near0000'",[evil]); db.execute("UPDATE messages SET content=? WHERE id='nearmsg0'",[evil]); db.close(); path=tmp_path/"map.html"
    result=CliRunner().invoke(app,["map","seed0000","--max-nodes","2","-o",str(path),"--no-open"]); text,data=map_payload(path)
    assert result.exit_code==0 and str(path) in result.output and os.stat(path).st_mode&0o777==0o600 and evil not in text and "innerHTML" not in text and "Content-Security-Policy" in text
    assert data["nodes"][1]["title"]==evil and data["edges"][0]["content"]==evil and len(data["nodes"])==2
def test_map_refuses_overwrite_and_symlink_outputs(tmp_path,monkeypatch):
    app=archive(tmp_path,monkeypatch); existing=tmp_path/"existing.html"; existing.write_text("sentinel"); outside=tmp_path/"outside.html"; outside.write_text("outside"); link=tmp_path/"link.html"; link.symlink_to(outside)
    for path in (existing,link):
        result=CliRunner().invoke(app,["map","seed0000","-o",str(path),"--no-open"]); assert result.exit_code==1 and "already exists or is unsafe" in result.output
    assert existing.read_text()=="sentinel" and outside.read_text()=="outside"
def test_map_opens_exact_file_uri_and_default_artifact_is_private(tmp_path,monkeypatch):
    app=archive(tmp_path,monkeypatch); calls=[]; monkeypatch.setattr(explore.webbrowser,"open",lambda uri,new=0: calls.append((uri,new)) or True); explicit=tmp_path/"opened.html"
    result=CliRunner().invoke(app,["map","seed0000","--max-nodes","2","-o",str(explicit)]); assert result.exit_code==0 and calls==[(explicit.resolve().as_uri(),2)] and "(browser opened)" in result.output
    default=CliRunner().invoke(app,["map","seed0000","--max-nodes","2","--no-open"]); path=Path(default.output.strip().removeprefix("Semantic map: "))
    assert default.exit_code==0 and path.is_file() and path.name.startswith("convos-semantic-map-") and os.stat(path).st_mode&0o777==0o600; path.unlink()
def test_explore_distribution_and_registration():
    import tomllib
    from pathlib import Path
    project=tomllib.loads((Path(__file__).parents[1]/"apps/explore/pyproject.toml").read_text())["project"]; app=typer.Typer(); register(app)
    assert project["readme"]=="README.md" and project["dependencies"][0]=="ai-convos-db>=0.6,<0.7" and project["entry-points"]["convos.commands"]=={"explore":"ai_convos_explore:register"} and {c.name or c.callback.__name__ for c in app.registered_commands}=={"related","trail","map"}
def test_explore_import_does_not_reenter_core_plugin_discovery():
    result=subprocess.run([sys.executable,"-c","import sys,ai_convos_explore; assert 'ai_convos.cli' not in sys.modules"],text=True,capture_output=True)
    assert result.returncode==0 and result.stderr==""
