import json, os, tomllib
from pathlib import Path

import duckdb, pytest, typer
from typer.testing import CliRunner

from ai_convos import cli
import ai_convos_redact as redact
from ai_convos_remote import publish
from ai_convos_remote.projection import connect
from ai_convos_remote.protocol import b64, identity, open_event


def app():
    root=typer.Typer(); redact.register(root); return root


def test_distribution_metadata_registration_and_remote_dependency():
    project=tomllib.loads((Path(__file__).parents[1]/"apps/redact/pyproject.toml").read_text())["project"]; remote=tomllib.loads((Path(__file__).parents[1]/"apps/remote/pyproject.toml").read_text())["project"]; core=tomllib.loads((Path(__file__).parents[1]/"pyproject.toml").read_text())["project"]
    assert project["dependencies"][0]=="ai-convos-db>=0.7,<0.8" and project["entry-points"]["convos.commands"]=={"redact":"ai_convos_redact:register"} and project["entry-points"]["convos.doctor"]=={"redact":"ai_convos_redact:doctor_status"}
    assert "ai-convos-redact>=0.7,<0.8" in remote["dependencies"] and core["optional-dependencies"]["redact"]==["ai-convos-redact>=0.7,<0.8"]
    help_=CliRunner().invoke(app(),["redact","--help"]).output
    assert "scan" in help_ and "status" in help_


@pytest.mark.parametrize(("kind","secret"),[
    ("private_key","-----BEGIN PRIVATE KEY-----\nvery-secret-material\n-----END PRIVATE KEY-----"),
    ("anthropic_key","sk-ant-"+"a"*32),
    ("openai_key","sk-proj-"+"A"*32),
    ("github_token","ghp_"+"a"*36),
    ("gitlab_token","glpat-"+"A"*24),
    ("aws_access_key","AKIA"+"A"*16),
    ("google_api_key","AIza"+"A"*35),
    ("slack_token","xoxb-"+"1"*12+"-"+"A"*24),
    ("stripe_key","sk_live_"+"A"*24),
    ("pypi_token","pypi-"+"A"*50),
    ("npm_token","npm_"+"A"*36),
    ("jwt","eyJ"+"A"*12+"."+"B"*12+"."+"C"*12),
    ("authorization","Authorization: Bearer "+"A"*32),
    ("credential_url","https://alice:correcthorsebattery@example.com"),
    ("assigned_secret","password=correcthorsebattery"),
    ("assigned_secret","AWS_SECRET_ACCESS_KEY="+"A"*40),
])
def test_high_confidence_secret_families_are_removed(kind,secret):
    safe,findings=redact.inspect({"nested":[f"before {secret} after"]})
    assert secret not in json.dumps(safe) and f"[REDACTED:{kind}]" in json.dumps(safe) and [f["kind"] for f in findings]==[kind]


def test_placeholders_hashes_and_short_examples_are_not_redacted():
    text="api_key=${TOKEN} sk-test abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
    assert redact.scrub(text)==(text,[])


def test_archive_scan_reports_locations_without_secret_values(tmp_path,monkeypatch):
    db=tmp_path/"convos.db"; monkeypatch.setattr(cli,"DB_PATH",db); conn=duckdb.connect(str(db)); cli.init_schema(conn); secret="sk-proj-"+"Z"*32
    conn.execute("INSERT INTO conversations (id,source,title,metadata) VALUES ('c','codex','scan','{}')")
    conn.execute("INSERT INTO messages (id,conversation_id,role,content,metadata) VALUES ('m','c','user',?,'{}')",[f"use {secret}"]); conn.close()
    data=redact.scan_data(); raw=json.dumps(data)
    assert data["status"]=="secrets_found" and data["total"]==1 and data["findings"][0]["table"]=="messages" and data["findings"][0]["row_id"]=="m" and secret not in raw


def test_unchanged_database_cache_is_exact_and_value_free(tmp_path,monkeypatch):
    db=tmp_path/"convos.db"; monkeypatch.setattr(cli,"DB_PATH",db); monkeypatch.setenv("CONVOS_PROJECT_ROOT",str(tmp_path)); conn=duckdb.connect(str(db)); cli.init_schema(conn); secret="AKIA"+"A"*16; conn.execute("INSERT INTO conversations (id,source,title,metadata) VALUES ('c','codex','scan','{}')"); conn.execute("INSERT INTO messages (id,conversation_id,role,content,metadata) VALUES ('m','c','user',?,'{}')",[secret]); conn.close()
    first=redact.scan_data(True); monkeypatch.setattr(redact,"inspect",lambda *_:pytest.fail("unchanged cache missed")); second=redact.scan_data(True)
    assert not first["cached"] and second["cached"] and first["findings"]==second["findings"] and secret.encode() not in (tmp_path/"redact/scan.json").read_bytes()


def config():
    device=identity("device"); team,personal="team","personal"; keys={team:os.urandom(32),personal:os.urandom(32)}
    return {"user":"user","device":device,"workspaces":{team:{"kind":"team","epoch":1},personal:{"kind":"personal","epoch":1}},"keys":{f"{ws}:1":b64(key) for ws,key in keys.items()}},keys


def message(content,mid="m"):
    return {"kind":"message.record","entity":f"messages:{mid}","payload":{"table":"messages","columns":["id","conversation_id","role","content","thinking","created_at","model","metadata","parent_id"],"row":[mid,"c","user",content,None,"2026-01-01",None,"{}",None]}}


def test_every_team_publish_is_scrubbed_before_encryption_and_personal_is_lossless(tmp_path):
    cfg,keys=config(); state=connect(tmp_path/"remote/state.db"); secret="ghp_"+"A"*36
    publish(cfg,state,"team",message(secret),tmp_path); publish(cfg,state,"personal",message(secret,"p"),tmp_path)
    team_path=Path(state.execute("SELECT path FROM outbox WHERE workspace='team'").fetchone()[0]); personal_path=Path(state.execute("SELECT path FROM outbox WHERE workspace='personal'").fetchone()[0]); team=open_event(json.loads(team_path.read_text()),keys["team"],cfg["device"]["sign_public"]); personal=open_event(json.loads(personal_path.read_text()),keys["personal"],cfg["device"]["sign_public"])
    assert secret not in json.dumps(team) and team["payload"]["row"][3]=="[REDACTED:github_token]" and personal["payload"]["row"][3]==secret and secret.encode() not in (tmp_path/"remote/state.db").read_bytes()
    audit=redact.audit_data(tmp_path); assert audit["status"]=="redacted" and audit["total"]==1 and secret not in json.dumps(audit) and not secret.encode() in (tmp_path/"redact/audit.db").read_bytes()


def test_team_attachments_are_omitted_and_audited_without_reading_body(tmp_path):
    cfg,_=config(); state=connect(tmp_path/"remote/state.db"); record={"kind":"attachment.record","entity":"attachments:a","payload":{"body":"secret"}}
    assert publish(cfg,state,"team",record,tmp_path) is None and state.execute("SELECT COUNT(*) FROM outbox WHERE workspace='team'").fetchone()[0]==0
    assert redact.audit_data(tmp_path)["by_kind"]=={"attachment_omitted":1}


def test_cli_json_never_prints_detected_value(tmp_path,monkeypatch):
    monkeypatch.setenv("CONVOS_PROJECT_ROOT",str(tmp_path)); secret="sk-ant-"+"A"*30; redact._audit(tmp_path,"w",{"entity":"messages:m","kind":"message.record"},[{"kind":"anthropic_key","path":"$.payload","line":1,"start":0}])
    result=CliRunner().invoke(app(),["redact","status","-f","json"])
    assert result.exit_code==0 and json.loads(result.output)["total"]==1 and secret not in result.output and redact.doctor_status()=="redact: 1 automatic team removal recorded"


def test_audit_refuses_symlink(tmp_path):
    target=tmp_path/"target"; target.mkdir(); (tmp_path/"redact").symlink_to(target,target_is_directory=True)
    with pytest.raises(ValueError,match="symlink"): redact._audit(tmp_path,"w",{"entity":"e","kind":"message.record"},[{"kind":"jwt","path":"$.payload","line":1,"start":0}])
    with pytest.raises(ValueError,match="symlink"): redact.audit_data(tmp_path)
