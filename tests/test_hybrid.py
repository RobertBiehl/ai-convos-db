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
            "INSERT INTO messages VALUES (?, 'c1', 'user', ?, NULL, NULL, NULL, NULL, ?)",
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
    """Querying for 'apple' with embedding pointing at idx=1 ranks m1/m3 above m4."""
    monkeypatch.setattr(cli, "embed_text", lambda s, doc=False: _emb(1))
    monkeypatch.setattr(cli, "rerank", lambda q, ds: [0.5] * len(ds))
    r = CliRunner().invoke(cli.app, ["query", "apple", "-n", "5"])
    assert r.exit_code == 0, r.output
    out = r.output
    # m1 and m3 hit on both FTS ("apple") and vector (idx=1) — must outrank m4
    pos = lambda needle: out.index(needle) if needle in out else 10**9
    assert pos("apple banana cherry") < pos("totally unrelated")
    assert pos("apple fruit basket") < pos("totally unrelated")


def test_query_no_embeddings_returns_friendly_error(tmp_path, monkeypatch):
    """When no rows have embeddings, query_cmd prints a guidance message and exits cleanly."""
    db = tmp_path / "test.db"
    monkeypatch.setattr(cli, "DB_PATH", db)
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    conn = duckdb.connect(str(db))
    cli.init_schema(conn)
    conn.execute("INSERT INTO conversations VALUES ('c1', 'test', 'Conv', NULL, NULL, NULL, NULL, NULL, NULL, NULL)")
    conn.execute("INSERT INTO messages VALUES ('m1', 'c1', 'user', 'hello', NULL, NULL, NULL, NULL, NULL)")
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
    r = CliRunner().invoke(cli.app, ["stats"])
    assert r.exit_code == 0
    assert "locked" in (r.output + (r.stderr if r.stderr_bytes is not None else ""))


def test_tier_blend_top3_weights():
    """Position-tier blend: ranks 0-2 → 0.75/0.25, 3-9 → 0.6/0.4, 10+ → 0.4/0.6."""
    W = lambda i: (0.75, 0.25) if i < 3 else (0.6, 0.4) if i < 10 else (0.4, 0.6)
    assert W(0) == (0.75, 0.25)
    assert W(2) == (0.75, 0.25)
    assert W(3) == (0.6, 0.4)
    assert W(9) == (0.6, 0.4)
    assert W(10) == (0.4, 0.6)
    assert W(99) == (0.4, 0.6)
