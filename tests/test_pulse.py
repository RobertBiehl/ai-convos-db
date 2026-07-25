import json, os, subprocess, tomllib
from datetime import datetime, timezone
from pathlib import Path

import duckdb, pytest, typer
from typer.testing import CliRunner

from ai_convos import cli
import ai_convos_pulse as pulse


NOW=datetime(2026,7,25,12,tzinfo=timezone.utc)
def git(path,*args): return subprocess.run(("git","-C",str(path),*args),check=True,capture_output=True,text=True).stdout.strip()
def app():
    root=typer.Typer(); root.command("dummy")(lambda:None); pulse.register(root); return root
def archive(tmp_path,monkeypatch):
    repo=tmp_path/"repo"; sub=repo/"sub"; other=tmp_path/"other"; sub.mkdir(parents=True); other.mkdir()
    for path in (repo,other): git(path,"init","-q")
    db=tmp_path/"convos.db"; monkeypatch.setattr(cli,"DB_PATH",db); monkeypatch.setattr(pulse,"drain_hooks",lambda:None); conn=duckdb.connect(str(db)); cli.init_schema(conn); secret="ghp_"+"A"*36
    conn.executemany("INSERT INTO conversations (id,source,title,cwd,metadata) VALUES (?,?,?,?, '{}')",[("c1","codex",f"<script>x</script> {secret}",str(repo)),("c2","claude-code","Sub",str(sub)),("c3","codex","Other",str(other)),("missing","codex","Gone",str(tmp_path/"gone")),("web","chatgpt","Web",None),("old","codex","Old",str(repo))])
    conn.executemany("INSERT INTO messages (id,conversation_id,role,content,created_at,metadata) VALUES (?,?,?,?,?,?)",[("m1","c1","user","start","2026-07-25 10:00","{}"),("noise","c1","user","<recommended_plugins>ignore","2026-07-25 10:01","{}"),("m2","c1","assistant","done","2026-07-25 10:02","{}"),("history","c1","assistant","old copy","2026-07-25 10:03",'{"history_of":"m2"}'),("s1","c2","tool","ran","2026-07-25 11:00","{}"),("o1","c3","assistant","other","2026-07-25 09:00","{}"),("g1","missing","user","gone","2026-07-25 08:00","{}"),("w1","web","user","web","2026-07-25 07:00","{}"),("old1","old","user","old","2026-07-20","{}")])
    conn.executemany("INSERT INTO file_edits (id,message_id,file_path,edit_type,content,created_at) VALUES (?,?,?,?,?,?)",[("e1","m2",str(repo/"a.py"),"write","a","2026-07-25 10:02"),("e2","m2",str(repo/"a.py"),"write","b","2026-07-25 10:03"),("e3","s1",str(repo/"b.py"),"write","c","2026-07-25 11:00")])
    conn.executemany("INSERT INTO tool_calls (id,message_id,tool_name,status,created_at) VALUES (?,?,?,?,?)",[("t1","m2","pytest","completed","2026-07-25 10:02"),("t2","s1","exec","failed","2026-07-25 11:00"),("t3","s1","exec",None,"2026-07-25 11:01")]); conn.close()
    return repo,other,secret


def test_distribution_metadata_registration_and_help():
    root=Path(__file__).parents[1]; project=tomllib.loads((root/"apps/pulse/pyproject.toml").read_text())["project"]; core=tomllib.loads((root/"pyproject.toml").read_text())["project"]
    assert project["dependencies"][:2]==["ai-convos-db>=0.6,<0.7","ai-convos-redact>=0.1,<0.2"] and project["entry-points"]["convos.commands"]=={"pulse":"ai_convos_pulse:register"} and core["optional-dependencies"]["pulse"]==["ai-convos-pulse>=0.1,<0.2"]
    assert all(word in CliRunner().invoke(app(),["pulse","--help"]).output for word in ("activity","--sessions","--min-messages","--include-web","--open"))


def test_exact_activity_collapses_git_roots_and_excludes_noise(tmp_path,monkeypatch):
    repo,other,secret=archive(tmp_path,monkeypatch); data=pulse.pulse_data(min_messages=1,now=NOW); raw=json.dumps(data,default=str); projects={p["scope"]:p for p in data["projects"]}
    assert data["status"]=="ready" and data["totals"]=={"projects":3,"conversations":4,"messages":5,"edits":3,"files":2,"tools":{"completed":1,"failed":1,"unknown":1}}
    assert set(projects)=={str(repo),str(other),str(tmp_path/"gone")} and projects[str(repo)]["conversations"]==2 and projects[str(repo)]["files"]==2 and projects[str(repo)]["edits"]==3
    assert projects[str(repo)]["roles"]=={"user":1,"assistant":1,"other":1} and projects[str(repo)]["tools"]=={"completed":1,"failed":1,"unknown":1}
    first=next(s for s in projects[str(repo)]["sessions"] if s["conversation_id"]=="c1")
    assert first["messages"]==2 and first["last"]=={"message_id":"m2","role":"assistant","created_at":datetime(2026,7,25,10,2)} and first["read"]=="convos read c1"
    assert secret not in raw and "\u001b" not in raw and "<script>" in raw and "[REDACTED:github_token]" in raw and data["redactions"]==1 and "recommended_plugins" not in raw and '"web"' not in raw and '"old"' not in raw


def test_web_opt_in_and_limits_are_explicit(tmp_path,monkeypatch):
    archive(tmp_path,monkeypatch); data=pulse.pulse_data(project_limit=2,session_limit=1,include_web=True,min_messages=1,now=NOW)
    assert data["totals"]["projects"]==4 and len(data["projects"])==2 and data["projects_truncated"]==2 and all(len(p["sessions"])==1 for p in data["projects"])
    assert pulse.pulse_data(days=1,include_web=True,now=datetime(2027,1,1,tzinfo=timezone.utc))["status"]=="no_activity"


def test_default_short_session_filter_is_exact_and_reversible(tmp_path,monkeypatch):
    repo,_,_=archive(tmp_path,monkeypatch); data=pulse.pulse_data(include_web=True,now=NOW)
    assert data["short_sessions_omitted"]==3 and data["totals"]["projects"]==1 and data["projects"][0]["scope"]==str(repo)
    assert pulse.pulse_data(include_web=True,min_messages=1,now=NOW)["short_sessions_omitted"]==0


def test_missing_archive_is_an_empty_factual_result(tmp_path,monkeypatch):
    monkeypatch.setattr(cli,"DB_PATH",tmp_path/"missing.db"); monkeypatch.setattr(pulse,"drain_hooks",lambda:None)
    data=pulse.pulse_data(now=NOW)
    assert data["status"]=="no_archive" and data["projects"]==[] and data["totals"]["messages"]==0


def test_markdown_json_and_html_are_content_free_and_safe(tmp_path,monkeypatch):
    _,_,secret=archive(tmp_path,monkeypatch); runner=CliRunner(); result=runner.invoke(app(),["pulse","-f","json","--include-web"]); data=json.loads(result.output)
    assert result.exit_code==0 and secret not in result.output and "content" not in result.output and data["include_web"]
    data=pulse.pulse_data(include_web=True,now=NOW); text=CliRunner().invoke(app(),["pulse"]).output; page=pulse.html_data(data)
    assert "no task status is inferred" in text and "convos read" in text and "Exact metadata only" in page and "&lt;script&gt;" in page and "<script>x</script>" not in page and secret not in page
    assert "Content-Security-Policy" in page and "frame-ancestors 'none'" in page and "http://" not in page and "https://" not in page


def test_private_atomic_output_and_cli_validation(tmp_path,monkeypatch):
    archive(tmp_path,monkeypatch); out=tmp_path/"private"/"pulse.html"; result=CliRunner().invoke(app(),["pulse","-f","html","-o",str(out)])
    assert result.exit_code==0 and out.exists() and os.stat(out).st_mode&0o777==0o600 and result.output.strip()==str(out)
    link=tmp_path/"link"; link.symlink_to(out); assert CliRunner().invoke(app(),["pulse","-f","html","-o",str(link)]).exit_code!=0
    assert CliRunner().invoke(app(),["pulse","-o",str(out)]).exit_code!=0 and CliRunner().invoke(app(),["pulse","-f","bad"]).exit_code!=0


def test_open_uses_private_default_dashboard(tmp_path,monkeypatch):
    archive(tmp_path,monkeypatch); opened=[]; monkeypatch.setattr(pulse,"PROJECT_ROOT",tmp_path/"home"); monkeypatch.setattr(pulse.webbrowser,"open",lambda uri:opened.append(uri))
    result=CliRunner().invoke(app(),["pulse","-f","html","--open"]); path=tmp_path/"home/pulse/pulse.html"
    assert result.exit_code==0 and path.exists() and opened==[path.as_uri()]
