"""Tests for hybrid search pipeline (RRF math + position-tier blend)."""
import duckdb, pytest
from typer.testing import CliRunner
from ai_convos import cli


def _emb(idx: int, dim: int = 768) -> list[float]:
    v = [0.0] * dim
    v[idx % dim] = 1.0
    return v


@pytest.fixture
def hybrid_db(tmp_path, monkeypatch):
    """Five messages: m1/m3 share an embedding (idx=1) and the term "apple";
    m2/m5 share an embedding (idx=2) and the term "date"; m4 is unrelated."""
    db = tmp_path / "test.db"
    monkeypatch.setattr(cli, "DB_PATH", db)
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    conn = duckdb.connect(str(db))
    cli.init_schema(conn)
    conn.execute(
        "INSERT INTO conversations VALUES (?, 'test', 'Conv', NULL, NULL, NULL, NULL, NULL, NULL, NULL)",
        ["c1"],
    )
    rows = [
        ("m1", "apple banana cherry", _emb(1)),
        ("m2", "banana date elderberry", _emb(2)),
        ("m3", "apple fruit basket", _emb(1)),
        ("m4", "totally unrelated content", _emb(50)),
        ("m5", "date raisin elderberry", _emb(2)),
    ]
    for mid, content, emb in rows:
        conn.execute(
            "INSERT INTO messages VALUES (?, 'c1', 'user', ?, NULL, NULL, NULL, NULL, ?, NULL)",
            [mid, content, emb],
        )
    cli.rebuild_fts_index(conn)
    conn.close()
    return db


def test_rrf_sql_formula():
    """RRF CTE: each source contributes 1/(60+rank); sums across sources."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE fts (id VARCHAR, r INT)")
    conn.execute("CREATE TABLE vec (id VARCHAR, r INT)")
    conn.execute("INSERT INTO fts VALUES ('a', 1), ('b', 2), ('c', 3)")
    conn.execute("INSERT INTO vec VALUES ('a', 1), ('b', 2), ('d', 3)")
    rows = dict(conn.execute("""
        SELECT id, SUM(1.0/(60+r)) AS rrf FROM (SELECT id, r FROM fts UNION ALL SELECT id, r FROM vec)
        GROUP BY id ORDER BY rrf DESC
    """).fetchall())
    assert rows["a"] == pytest.approx(1/61 + 1/61, rel=1e-9)
    assert rows["b"] == pytest.approx(1/62 + 1/62, rel=1e-9)
    assert rows["c"] == pytest.approx(1/63, rel=1e-9)
    assert rows["d"] == pytest.approx(1/63, rel=1e-9)
    # 'a' (top in both) ranks highest
    top = max(rows, key=rows.get)
    assert top == "a"


def test_query_pipeline_end_to_end(hybrid_db, monkeypatch):
    """Querying for 'apple' returns the strongest relevant message, not unrelated content."""
    monkeypatch.setattr(cli, "embed_text", lambda s, doc=False: _emb(1))
    monkeypatch.setattr(cli, "rerank", lambda q, ds: [0.5] * len(ds))
    r = CliRunner().invoke(cli.app, ["query", "apple", "-n", "5"])
    assert r.exit_code == 0, r.output
    out = r.output
    assert "apple banana cherry" in out
    assert "totally unrelated" not in out


def test_query_returns_one_hit_per_conversation(hybrid_db, monkeypatch):
    monkeypatch.setattr(cli, "embed_text", lambda s, doc=False: _emb(1)); monkeypatch.setattr(cli, "rerank", lambda q, ds: [0.5] * len(ds))
    r = CliRunner().invoke(cli.app, ["query", "apple", "-n", "5", "-f", "json"])
    assert r.exit_code == 0
    assert len(__import__("json").loads(r.output)) == 1


def test_query_filters_candidates_and_skips_injected_boilerplate(tmp_path, monkeypatch):
    db = tmp_path / "test.db"; monkeypatch.setattr(cli, "DB_PATH", db); monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    conn = duckdb.connect(str(db)); cli.init_schema(conn)
    conn.execute("INSERT INTO conversations VALUES ('noise','noise','N',NULL,NULL,NULL,NULL,NULL,NULL,NULL), ('target','target','T',NULL,NULL,NULL,NULL,NULL,NULL,NULL)")
    rows = [(f"n{i}", "noise", "user", "needle noise", _emb(1)) for i in range(55)] + [("skill", "target", "user", "Base directory for this skill: needle", _emb(1)), ("wanted", "target", "user", "needle wanted", _emb(1))]
    conn.executemany("INSERT INTO messages (id,conversation_id,role,content,embedding) VALUES (?,?,?,?,?)", rows); cli.rebuild_fts_index(conn); conn.close()
    monkeypatch.setattr(cli, "embed_text", lambda s, doc=False: _emb(1)); monkeypatch.setattr(cli, "rerank", lambda q, ds: [0.5] * len(ds))
    r = CliRunner().invoke(cli.app, ["query", "needle", "-s", "target", "-n", "5", "-f", "json"]); hits = __import__("json").loads(r.output)
    assert [h["content"] for h in hits] == ["needle wanted"]


def test_query_no_embeddings_returns_friendly_error(tmp_path, monkeypatch):
    """When no rows have embeddings, query_cmd prints a guidance message and exits cleanly."""
    db = tmp_path / "test.db"
    monkeypatch.setattr(cli, "DB_PATH", db)
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    conn = duckdb.connect(str(db))
    cli.init_schema(conn)
    conn.execute("INSERT INTO conversations VALUES ('c1', 'test', 'Conv', NULL, NULL, NULL, NULL, NULL, NULL, NULL)")
    conn.execute("INSERT INTO messages VALUES ('m1', 'c1', 'user', 'hello', NULL, NULL, NULL, NULL, NULL, NULL)")
    cli.rebuild_fts_index(conn)
    conn.close()
    r = CliRunner().invoke(cli.app, ["query", "hello"])
    assert r.exit_code == 0
    assert "No embeddings yet" in (r.output + (r.stderr if r.stderr_bytes is not None else ""))


def test_query_migrates_old_db_before_embedding_check(tmp_path, monkeypatch):
    """Old databases have no embedding column; query should migrate then print guidance."""
    db = tmp_path / "test.db"
    monkeypatch.setattr(cli, "DB_PATH", db)
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    conn = duckdb.connect(str(db))
    conn.execute("CREATE TABLE conversations (id VARCHAR PRIMARY KEY, source VARCHAR NOT NULL, title VARCHAR, created_at TIMESTAMP, updated_at TIMESTAMP, model VARCHAR, cwd VARCHAR, git_branch VARCHAR, project_id VARCHAR, metadata JSON)")
    conn.execute("CREATE TABLE messages (id VARCHAR PRIMARY KEY, conversation_id VARCHAR NOT NULL, role VARCHAR NOT NULL, content VARCHAR, thinking VARCHAR, created_at TIMESTAMP, model VARCHAR, metadata JSON)")
    conn.execute("INSERT INTO conversations VALUES ('c1', 'test', 'Conv', NULL, NULL, NULL, NULL, NULL, NULL, NULL)")
    conn.execute("INSERT INTO messages VALUES ('m1', 'c1', 'user', 'hello', NULL, NULL, NULL, NULL)")
    conn.close()
    r = CliRunner().invoke(cli.app, ["query", "hello"])
    assert r.exit_code == 0
    assert "No embeddings yet" in (r.output + (r.stderr if r.stderr_bytes is not None else ""))
    conn = duckdb.connect(str(db))
    assert conn.execute("SELECT 1 FROM information_schema.columns WHERE table_name='messages' AND column_name='embedding'").fetchone()
    conn.close()


def test_read_commands_handle_locked_db(monkeypatch):
    """Read commands should print a friendly lock message instead of a traceback."""
    monkeypatch.setattr(cli, "get_db", lambda read_only=False: (_ for _ in ()).throw(ValueError("Database is locked by another convos process.")))
    r = CliRunner().invoke(cli.app, ["search", "x"])
    assert r.exit_code == 0
    assert "locked" in (r.output + (r.stderr if r.stderr_bytes is not None else ""))


@pytest.mark.parametrize("read_only", [True, False])
def test_db_waits_for_writer(tmp_path, monkeypatch, read_only):
    """Readers and writers retry transient DuckDB locks before giving up."""
    db, calls = tmp_path / "test.db", {"n": 0}; db.touch()
    monkeypatch.setattr(cli, "DB_PATH", db); monkeypatch.setattr(cli, "DATA_DIR", tmp_path); monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    def connect(path, read_only=False):
        calls["n"] += 1
        if calls["n"] < 3: raise duckdb.IOException("Conflicting lock is held")
        return "conn"
    monkeypatch.setattr(cli.duckdb, "connect", connect)
    assert cli.get_db(read_only=read_only) == "conn"
    assert calls["n"] == 3


def test_tier_blend_top3_weights():
    """Position-tier blend: ranks 0-2 → 0.75/0.25, 3-9 → 0.6/0.4, 10+ → 0.4/0.6."""
    W = lambda i: (0.75, 0.25) if i < 3 else (0.6, 0.4) if i < 10 else (0.4, 0.6)
    assert W(0) == (0.75, 0.25)
    assert W(2) == (0.75, 0.25)
    assert W(3) == (0.6, 0.4)
    assert W(9) == (0.6, 0.4)
    assert W(10) == (0.4, 0.6)
    assert W(99) == (0.4, 0.6)


def test_sql_select_and_blocks_writes(tmp_path, monkeypatch):
    """convos sql runs read-only SELECTs (json + text) and fails writes cleanly."""
    db = tmp_path / "test.db"
    monkeypatch.setattr(cli, "DB_PATH", db); monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    conn = duckdb.connect(str(db)); cli.init_schema(conn)
    conn.execute("INSERT INTO conversations VALUES ('c1','test','T',NULL,NULL,NULL,NULL,NULL,NULL,NULL)"); conn.close()
    r = CliRunner().invoke(cli.app, ["sql", "SELECT id, source FROM conversations", "-f", "json"])
    assert r.exit_code == 0, r.output
    assert '"c1"' in r.output and '"test"' in r.output
    w = CliRunner().invoke(cli.app, ["sql", "UPDATE conversations SET title='x'"])
    assert w.exit_code == 0
    assert "Query failed" in (w.output + (w.stderr if w.stderr_bytes is not None else ""))


def test_json_output_formats(tmp_path, monkeypatch):
    """-f json emits an array; -f jsonl emits one object per line; across read commands."""
    import json as _json
    db = tmp_path / "test.db"
    monkeypatch.setattr(cli, "DB_PATH", db); monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    conn = duckdb.connect(str(db)); cli.init_schema(conn)
    conn.execute("INSERT INTO conversations VALUES ('c1','test','T',NULL,NULL,NULL,NULL,NULL,NULL,NULL)")
    conn.execute("INSERT INTO messages VALUES ('m1','c1','user','hello',NULL,NULL,NULL,NULL,NULL,NULL)")
    conn.execute("INSERT INTO messages VALUES ('m2','c1','assistant','hi there',NULL,NULL,NULL,NULL,NULL,NULL)")
    cli.rebuild_fts_index(conn); conn.close()
    data = _json.loads(CliRunner().invoke(cli.app, ["sql", "SELECT id, source FROM conversations", "-f", "json"]).output)
    assert data == [{"id": "c1", "source": "test"}]
    out = CliRunner().invoke(cli.app, ["sql", "SELECT role FROM messages ORDER BY role", "-f", "jsonl"]).output
    objs = [_json.loads(l) for l in out.strip().splitlines() if l.strip()]
    assert [o["role"] for o in objs] == ["assistant", "user"]
    sd = _json.loads(CliRunner().invoke(cli.app, ["search", "hello", "-f", "json"]).output)
    assert isinstance(sd, list)


def test_search_rebuilds_missing_fts_index(tmp_path, monkeypatch):
    db = tmp_path / "test.db"; monkeypatch.setattr(cli, "DB_PATH", db); monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    conn = duckdb.connect(str(db)); cli.init_schema(conn)
    conn.execute("INSERT INTO conversations VALUES ('c1','test','T',NULL,NULL,NULL,NULL,NULL,NULL,NULL)")
    conn.execute("INSERT INTO messages VALUES ('m1','c1','user','recoverable',NULL,NULL,NULL,NULL,NULL,NULL)"); conn.close()
    r = CliRunner().invoke(cli.app, ["search", "recoverable", "-f", "json"])
    assert r.exit_code == 0 and __import__("json").loads(r.output)[0]["conversation_id"] == "c1"


def test_export_parameterizes_source_and_path(tmp_path, monkeypatch):
    db = tmp_path / "test.db"; monkeypatch.setattr(cli, "DB_PATH", db); monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    conn = duckdb.connect(str(db)); cli.init_schema(conn)
    conn.execute("INSERT INTO conversations VALUES ('c1',?, 'T',NULL,NULL,NULL,NULL,NULL,NULL,NULL)", ["quoted'source"])
    conn.execute("INSERT INTO messages VALUES ('m1','c1','user','hello',NULL,NULL,NULL,NULL,NULL,NULL)"); conn.close()
    out = tmp_path / "quoted'out.csv"; r = CliRunner().invoke(cli.app, ["export", str(out), "-f", "csv", "-s", "quoted'source"])
    assert r.exit_code == 0 and "hello" in out.read_text()
