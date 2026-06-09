"""Tests for the changegraph app (spec 02): exact replay or labeled unknown."""

import duckdb
import typer

from ai_convos.cli import init_schema
from ai_convos_changegraph import cut, edits_for, register, replay


def _e(type, content, old=None, ts="2024-01-01 00:00:00", conv="c1", source="claude-code", prompt="p"):
    return dict(type=type, content=content, old=old, ts=ts, conv=conv, source=source, prompt=prompt)


def test_replay_write_then_edit():
    text, prov = replay([_e("write", "a\nb\nc"), _e("edit", "B", old="b", conv="c2")])
    assert text == ["a", "B", "c"]
    assert [p["conv"] for p in prov] == ["c1", "c2", "c1"]


def test_replay_multiline_edit():
    text, prov = replay([_e("write", "a\nb\nc\nd"), _e("edit", "x\ny\nz", old="b\nc", conv="c2")])
    assert text == ["a", "x", "y", "z", "d"]
    assert [p["conv"] for p in prov] == ["c1", "c2", "c2", "c2", "c1"]


def test_replay_shrinking_edit():
    text, prov = replay([_e("write", "a\nb\nc"), _e("edit", "", old="b\n", conv="c2")])
    assert text == ["a", "c"]
    assert len(prov) == 2


def test_replay_unknown_on_shell():
    assert replay([_e("write", "a"), _e("shell", "sed -i s/a/b/ f")]) == (None, None)


def test_replay_unknown_on_unmatched_old():
    assert replay([_e("write", "a"), _e("edit", "y", old="zzz")]) == (None, None)


def test_replay_unknown_on_missing_old():
    assert replay([_e("write", "a"), _e("multiedit", "y")]) == (None, None)


def test_replay_write_resets_unknown():
    text, prov = replay([_e("write", "a"), _e("shell", "x"), _e("write", "b\nc", conv="c2")])
    assert text == ["b", "c"]
    assert all(p["conv"] == "c2" for p in prov)


def test_cut_by_conv_and_timestamp():
    edits = [_e("write", "a", conv="aaa111", ts="2024-01-01 00:00:00"),
             _e("edit", "b", old="a", conv="bbb222", ts="2024-02-01 00:00:00")]
    assert cut(edits, None) == edits
    assert cut(edits, "aaa") == edits[:1]
    assert cut(edits, "bbb") == edits
    assert cut(edits, "2024-01") == edits[:1]
    assert cut(edits, "2024-03") == edits


def test_edits_for_attributes_prompt(tmp_path):
    conn = duckdb.connect(str(tmp_path / "t.db"))
    init_schema(conn)
    conn.execute("INSERT INTO conversations VALUES ('c1','claude-code','t','2024-01-01','2024-01-01','m',NULL,NULL,NULL,'{}')")
    conn.execute("INSERT INTO messages VALUES ('u1','c1','user','please edit',NULL,'2024-01-01 00:00:00',NULL,'{}',NULL),"
                 "('a1','c1','assistant','done',NULL,'2024-01-01 00:00:01',NULL,'{}',NULL)")
    conn.execute("INSERT INTO file_edits VALUES ('e1','a1','/f.py','write','x','2024-01-01 00:00:01',NULL)")
    edits = edits_for(conn, "/f.py")
    conn.close()
    assert len(edits) == 1
    assert edits[0]["prompt"] == "please edit"
    assert edits[0]["conv"] == "c1"
    assert edits[0]["type"] == "write"


def test_register_adds_commands():
    app = typer.Typer()
    register(app)
    assert {c.callback.__name__ for c in app.registered_commands} == {"blame", "timeline", "at", "graph", "browse"}


def test_orphaned_edits_labeled_unknown(tmp_path):
    conn = duckdb.connect(str(tmp_path / "t.db"))
    init_schema(conn)
    conn.execute("INSERT INTO file_edits VALUES ('e1','gone','/f.py','write','x','2024-01-01',NULL)")
    edits = edits_for(conn, "/f.py")
    conn.close()
    assert edits[0]["conv"] == "unknown"
    assert edits[0]["source"] == "?"
    assert edits[0]["prompt"] is None


def _tui_db(tmp_path):
    conn = duckdb.connect(str(tmp_path / "t.db"))
    init_schema(conn)
    conn.execute("INSERT INTO conversations VALUES ('c1','claude-code','fix greeting','2024-01-01','2024-01-01','m',NULL,NULL,NULL,'{}')")
    conn.execute("INSERT INTO messages VALUES ('u1','c1','user','please fix',NULL,'2024-01-01 00:00:00',NULL,'{}',NULL),"
                 "('a1','c1','assistant','',NULL,'2024-01-01 00:00:01',NULL,'{}',NULL)")
    conn.execute("INSERT INTO file_edits VALUES ('e1','a1','/f.py','edit','new','2024-01-01 00:00:01','old')")
    return conn


def test_tui_views_walk_the_graph(tmp_path):
    from ai_convos_changegraph.tui import _detail, _files, _timeline
    conn = _tui_db(tmp_path)
    files = _files(conn)
    assert len(files["rows"]) == 1 and "/f.py" in files["fmt"](files["rows"][0])
    tl = files["enter"](files["rows"][0])
    assert tl["title"] == "/f.py" and "please fix" in tl["fmt"](tl["rows"][0])
    detail = tl["enter"](tl["rows"][0])
    assert "- old" in detail["rows"] and "+ new" in detail["rows"]
    pivot = detail["conv"](None)  # edit -> its conversation's files
    assert "fix greeting" in pivot["title"] and len(pivot["rows"]) == 1
    conn.close()


def test_tui_unknown_conv_has_no_pivot(tmp_path):
    from ai_convos_changegraph.tui import _timeline
    conn = duckdb.connect(str(tmp_path / "t.db"))
    init_schema(conn)
    conn.execute("INSERT INTO file_edits VALUES ('e1','gone','/f.py','shell','rm x','2024-01-01',NULL)")
    tl = _timeline(conn, "/f.py")
    assert "unknown" in tl["fmt"](tl["rows"][0])
    assert tl["conv"](tl["rows"][0]) is None
    conn.close()
