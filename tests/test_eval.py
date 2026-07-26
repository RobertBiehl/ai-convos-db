import json
from pathlib import Path

import duckdb, pytest, typer
from typer.testing import CliRunner

from ai_convos import cli
from evals import retrieval as ev


def app():
    root=typer.Typer(); root.command("dummy")(lambda:None); root.command("eval")(ev.eval_cmd); return root
def archive(tmp_path,monkeypatch):
    repo=tmp_path/"repo"; (repo/"sub").mkdir(parents=True); other=tmp_path/"other"; other.mkdir(); db=tmp_path/"convos.db"; monkeypatch.setattr(cli,"DB_PATH",db); monkeypatch.setattr(cli,"DATA_DIR",tmp_path); monkeypatch.setattr(cli,"drain_hooks",lambda *a,**k:None); monkeypatch.setattr(ev,"drain_hooks",lambda:None)
    conn=duckdb.connect(str(db)); cli.init_schema(conn); conn.executemany("INSERT INTO conversations (id,source,title,cwd) VALUES (?,?,?,?)",[("c1abcdef12345678","codex","Memory",str(repo/"sub")),("c2abcdef12345678","codex","Other",str(other))]); conn.executemany("INSERT INTO messages (id,conversation_id,role,content) VALUES (?,?,?,?)",[("m1abcdef12345678","c1abcdef12345678","user","canonical memory ledger"),("m2abcdef12345678","c2abcdef12345678","user","canonical cooking ledger")]); cli.rebuild_fts_index(conn); conn.close(); return repo
def cases(path,rows):
    path.write_text("\n".join(json.dumps(r) for r in rows)); return path


def test_development_tool_help():
    command=typer.main.get_command(app()).commands["eval"]; options={opt for param in command.params for opt in param.opts}
    assert "exact-ID" in command.help and {"--mode","--min-hit-rate","--format"} <= options


@pytest.mark.parametrize("row,error",[
    ({}, "name, query"),
    ({"name":"x","query":"q","expect":[]}, "name, query"),
    ({"name":"x","query":"q","expect":["c1234567"],"wat":1}, "unknown"),
    ({"name":"x","query":"q","expect":["c1234567"],"mode":"magic"}, "invalid option"),
    ({"name":"x","query":"q","expect":["c1234567"],"k":0}, "invalid option"),
])
def test_case_contract_fails_closed(tmp_path,row,error):
    with pytest.raises(ValueError,match=error): ev.load_cases(cases(tmp_path/"cases.jsonl",[row]))
    with pytest.raises(ValueError,match="no cases"): ev.load_cases((tmp_path/"empty").write_text("") or tmp_path/"empty")


def test_literal_eval_uses_direct_scope_and_reports_no_content(tmp_path,monkeypatch):
    repo=archive(tmp_path,monkeypatch); path=cases(tmp_path/"cases.jsonl",[{"name":"memory decision","query":"canonical ledger","expect":["c1abcdef"],"mode":"literal","cwd":str(repo)}]); data=ev.run(ev.load_cases(path)); raw=json.dumps(data)
    assert data["status"]=="ready" and data["engines"]["literal"]=={"runs":1,"hits":1,"hit_rate":1.0,"mrr":1.0,"errors":0}
    assert data["results"][0]["returned"][0]["conversation_id"]=="c1abcdef12345678" and data["results"][0]["read"]=="convos read c1abcdef12345678 --around m1abcdef12345678"
    assert "canonical memory" not in raw and "content" not in raw


def test_hybrid_eval_passes_offline_filters_and_computes_rank(tmp_path,monkeypatch):
    seen=[]
    def hits(q,source,days,role,k,local_only=False,cwd=None,conversation=None):
        seen.append((q,source,days,role,k,local_only,cwd,conversation)); return [dict(conversation_id="wrong",message_id="x",score=.2),dict(conversation_id="target123",message_id="turn123",score=.1)]
    monkeypatch.setattr(ev,"hybrid_hits",hits); path=cases(tmp_path/"cases.jsonl",[{"name":"target","query":"meaning","expect":["target12"],"source":"codex","days":7,"role":"assistant","cwd":"/repo","conversation":"target","k":3}]); data=ev.run(ev.load_cases(path))
    assert seen==[("meaning","codex",7,"assistant",3,True,"/repo","target")] and data["engines"]["hybrid"]["mrr"]==.5 and data["results"][0]["rank"]==2


def test_both_engines_and_json_cli_threshold(tmp_path,monkeypatch):
    archive(tmp_path,monkeypatch); monkeypatch.setattr(ev,"hybrid_hits",lambda *a,**k:[dict(conversation_id="c1abcdef12345678",message_id="m1abcdef12345678",score=.5)])
    path=cases(tmp_path/"cases.jsonl",[{"name":"both","query":"canonical memory","expect":["c1abcdef"],"mode":"both"}]); result=CliRunner().invoke(app(),["eval",str(path),"-f","json","--min-hit-rate","1"])
    data=json.loads(result.output); assert result.exit_code==0 and data["runs"]==2 and set(data["engines"])=={"hybrid","literal"} and all(m["hit_rate"]==1 for m in data["engines"].values())
    monkeypatch.setattr(ev,"hybrid_hits",lambda *a,**k:[]); miss=CliRunner().invoke(app(),["eval",str(path),"-f","json","--mode","hybrid","--min-hit-rate","1"])
    assert miss.exit_code==1


def test_case_errors_are_reported_without_aborting_remaining_runs(tmp_path,monkeypatch):
    monkeypatch.setattr(ev,"hybrid_hits",lambda *a,**k:(_ for _ in ()).throw(ValueError("model unavailable"))); path=cases(tmp_path/"cases.jsonl",[{"name":"a","query":"q","expect":["c1234567"]},{"name":"b","query":"q","expect":["c1234567"]}]); data=ev.run(ev.load_cases(path))
    assert data["status"]=="errors" and data["engines"]["hybrid"]["errors"]==2 and [r["status"] for r in data["results"]]==["error","error"]


def test_ground_truth_prefixes_must_exist_uniquely(tmp_path,monkeypatch):
    archive(tmp_path,monkeypatch)
    with pytest.raises(ValueError,match="missing"): ev.ground([{"expect":["missing1"]}])
    conn=duckdb.connect(str(tmp_path/"convos.db")); conn.execute("INSERT INTO conversations (id,source) VALUES ('c1abcdef99999999','codex')"); conn.close()
    with pytest.raises(ValueError,match="ambiguous"): ev.ground([{"expect":["c1abcdef"]}])
