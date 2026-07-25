import json, shutil, subprocess, threading, tomllib
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

import duckdb, pytest, typer
from typer.testing import CliRunner

from ai_convos import cli
import ai_convos_library as library


def app():
    root=typer.Typer(); root.command("dummy")(lambda:None); library.register(root); return root
def archive(tmp_path,monkeypatch):
    repo=tmp_path/"repo"; (repo/"sub").mkdir(parents=True); other=tmp_path/"other"; other.mkdir(); db=tmp_path/"convos.db"; monkeypatch.setattr(cli,"DB_PATH",db); monkeypatch.setattr(cli,"DATA_DIR",tmp_path); monkeypatch.setattr(cli,"drain_hooks",lambda *a,**k:None); monkeypatch.setattr(library,"drain_hooks",lambda:None)
    conn=duckdb.connect(str(db)); cli.init_schema(conn); conn.executemany("INSERT INTO conversations (id,source,title,cwd) VALUES (?,?,?,?)",[("c1abcdef12345678","codex","<script>Memory</script>",str(repo/"sub")),("c2abcdef12345678","claude-code","Other",str(other))]); conn.executemany("INSERT INTO messages (id,conversation_id,role,content,created_at,metadata) VALUES (?,?,?,?,?,?)",[("m1abcdef12345678","c1abcdef12345678","user","libraryneedle <img src=x onerror=alert(1)>","2026-01-01","{}"),("m2abcdef12345678","c1abcdef12345678","assistant","safe answer","2026-01-02","{}"),("m3abcdef12345678","c2abcdef12345678","user","libraryneedle outside","2026-01-03","{}"),("oldabcdef123456","c1abcdef12345678","assistant","superseded","2025-01-01",'{\"history_of\":\"m2abcdef12345678\"}')]); cli.rebuild_fts_index(conn); conn.close(); return repo
def get(url):
    with urlopen(url,timeout=3) as response: return response.status,response.headers,response.read()


def test_distribution_metadata_registration_and_help():
    root=Path(__file__).parents[1]; project=tomllib.loads((root/"apps/library/pyproject.toml").read_text())["project"]; core=tomllib.loads((root/"pyproject.toml").read_text())["project"]
    assert project["dependencies"][0]=="ai-convos-db>=0.6,<0.7" and project["entry-points"]["convos.commands"]=={"library":"ai_convos_library:register"} and core["optional-dependencies"]["library"]==["ai-convos-library>=0.1,<0.2"]
    help_=CliRunner().invoke(app(),["library","--help"]).output
    replay=CliRunner().invoke(app(),["replay","--help"]).output
    assert all(word in help_ for word in ("private","read-only","--port","--no-open")) and all(word in replay for word in ("messages","tool calls","edits","--activity"))


def test_search_and_thread_use_real_core_retrieval_with_project_scope(tmp_path,monkeypatch):
    repo=archive(tmp_path,monkeypatch); conn=duckdb.connect(str(cli.DB_PATH)); conn.execute("INSERT INTO tool_calls (id,message_id,tool_name,input,output,status,duration_ms,created_at) VALUES ('t1','m2abcdef12345678','exec','{\"cmd\":\"<script>\"}','{\"result\":\"ok\"}','complete',12,'2026-01-02 00:01'),('stale','oldabcdef123456','hidden','{}','{}','complete',1,'2025-01-01')"); conn.execute("INSERT INTO file_edits (id,message_id,file_path,edit_type,content,created_at,old_content) VALUES ('e1','m2abcdef12345678',?,'edit','after <img>','2026-01-02 00:02','before')",[str(repo/"x.py")]); conn.close()
    hits=library.search_data({"q":["libraryneedle"],"cwd":[str(repo)]}); thread=library.thread_data({"conversation":["c1abcdef"],"around":["m1abcdef"]})
    assert len(hits)==1 and hits[0]["conversation_id"]=="c1abcdef12345678" and "<img" in hits[0]["content"]
    assert thread["title"]=="<script>Memory</script>" and [m["id"] for m in thread["messages"]]==["m1abcdef12345678","m2abcdef12345678"] and "superseded" not in json.dumps(thread)
    assert [a["kind"] for a in thread["messages"][1]["activity"]]==["tool","edit"] and thread["messages"][1]["activity"][0]["duration_ms"]==12 and thread["counts"]=={"tools":1,"edits":1} and "hidden" not in json.dumps(thread)


def test_semantic_search_is_cached_only_and_bounded(monkeypatch):
    seen=[]
    monkeypatch.setattr(library,"hybrid_hits",lambda *a,**k:seen.append((a,k)) or [dict(content="x"*600,conversation_id="c",message_id="m",source="codex",title="T")])
    hits=library.search_data({"q":["meaning"],"engine":["hybrid"],"source":["codex"],"cwd":["/repo"],"limit":["99"]})
    assert seen==[(("meaning","codex",None,None,30),{"local_only":True,"cwd":"/repo"})] and hits[0]["content"]=="x"*500+"..."
    with pytest.raises(ValueError,match="engine"): library.search_data({"q":["x"],"engine":["network"]})


def test_static_browser_uses_text_nodes_and_no_external_assets():
    assert "http://" not in library.PAGE and "https://" not in library.PAGE and "<script src=app.js" in library.PAGE
    assert ".textContent=" in library.JS and "innerHTML" not in library.JS and "eval(" not in library.JS and 'el("details"' in library.JS
    assert "<script>Memory" not in library.PAGE and "<img src=x" not in library.PAGE
    if node:=shutil.which("node"): subprocess.run([node,"--check","-"],input=library.JS,text=True,capture_output=True,check=True)


def test_tokenized_loopback_http_api_and_security_headers(tmp_path,monkeypatch):
    repo=archive(tmp_path,monkeypatch); conn=duckdb.connect(str(cli.DB_PATH)); conn.execute("INSERT INTO tool_calls (id,message_id,tool_name,input,output,status,created_at) VALUES ('t1','m1abcdef12345678','exec','{\"x\":\"<script>\"}','{}','complete','2026-01-01 00:01')"); conn.close(); server=library.make_server(token="private-token"); thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    try:
        assert server.server_address[0]=="127.0.0.1" and "/private-token/" in server.url
        status,headers,page=get(server.url); assert status==200 and b"Convos library" in page and headers["Cache-Control"]=="no-store" and "frame-ancestors 'none'" in headers["Content-Security-Policy"] and headers["Referrer-Policy"]=="no-referrer" and headers["Cross-Origin-Resource-Policy"]=="same-origin" and "camera=()" in headers["Permissions-Policy"]
        _,js_headers,script=get(server.url+"app.js"); assert js_headers.get_content_type()=="text/javascript" and b"textContent" in script
        query=urlencode({"q":"libraryneedle","cwd":str(repo)}); _,api_headers,raw=get(server.url+"api/search?"+query); hits=json.loads(raw); assert api_headers.get_content_type()=="application/json" and [h["conversation_id"] for h in hits]==["c1abcdef12345678"]
        _,_,raw=get(server.url+"api/thread?"+urlencode({"conversation":"c1abcdef","around":"m1abcdef"})); replay=json.loads(raw); assert replay["messages"][0]["id"]=="m1abcdef12345678" and replay["messages"][0]["activity"][0]["name"]=="exec" and "<script>" in replay["messages"][0]["activity"][0]["input"]
        with pytest.raises(HTTPError) as wrong: get(server.url.replace("/private-token/","/wrong/"))
        assert wrong.value.code==404
    finally: server.shutdown(); server.server_close(); thread.join()


def test_api_bounds_inputs_and_sanitizes_internal_errors(tmp_path,monkeypatch):
    archive(tmp_path,monkeypatch); server=library.make_server(token="token"); thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    try:
        with pytest.raises(HTTPError) as bad: get(server.url+"api/search?"+urlencode({"q":"x"*501}))
        assert bad.value.code==400 and "too long" in bad.value.read().decode()
        monkeypatch.setattr(library,"search_data",lambda _:(_ for _ in ()).throw(RuntimeError("private traceback detail")))
        with pytest.raises(HTTPError) as failed: get(server.url+"api/search?q=x")
        assert failed.value.code==500 and failed.value.read()==b'{"error": "Archive request failed"}'
    finally: server.shutdown(); server.server_close(); thread.join()


def test_replay_cli_orders_and_bounds_exact_activity(tmp_path,monkeypatch):
    repo=archive(tmp_path,monkeypatch); conn=duckdb.connect(str(cli.DB_PATH)); conn.execute("INSERT INTO tool_calls (id,message_id,tool_name,input,output,status,duration_ms,created_at) VALUES ('t1','m2abcdef12345678','exec','{\"cmd\":\"123456789\"}','{\"out\":\"done\"}','complete',9,'2026-01-02 00:01')"); conn.execute("INSERT INTO file_edits (id,message_id,file_path,edit_type,content,created_at,old_content) VALUES ('e1','m2abcdef12345678',?,'edit','after','2026-01-02 00:02','before')",[str(repo/"x.py")]); conn.close()
    data=json.loads(CliRunner().invoke(app(),["replay","c1abcdef","-a","m2abcdef","-n","2","-c","8","--activity","1","-f","json"]).output)
    assert [m["id"] for m in data["messages"]]==["m1abcdef12345678","m2abcdef12345678"] and data["messages"][0]["content"]=="libraryn..."
    assert data["messages"][1]["activity"][0]["kind"]=="tool" and len(data["messages"][1]["activity"][0]["input"])==11 and data["activity_truncated"] and data["counts"]=={"tools":1,"edits":0}
    text=CliRunner().invoke(app(),["replay","c1abcdef","--activity","2"]).output
    assert "TOOL exec complete [t1]" in text and f"EDIT edit {repo/'x.py'} [e1]" in text and "2 messages, 1 tool, 1 edit" in text
    assert CliRunner().invoke(app(),["replay","missing"]).exit_code==1


def test_cli_delegates_to_server_runner(monkeypatch):
    seen=[]; monkeypatch.setattr(library,"run_server",lambda port,open_:seen.append((port,open_)))
    result=CliRunner().invoke(app(),["library","--port","8123","--no-open"])
    assert result.exit_code==0 and seen==[(8123,False)]
