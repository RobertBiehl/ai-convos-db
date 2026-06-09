"""Lean curses browser for the change graph: files -> timeline -> edit detail, `c` pivots
across the file <-> conversation edge so the graph is walkable in both directions."""
import curses, os

from . import _conn, edits_for

_FILES = """SELECT fe.file_path, COUNT(*), MAX(fe.created_at), COALESCE(string_agg(DISTINCT c.source, ','), '?')
FROM file_edits fe LEFT JOIN messages m ON fe.message_id = m.id LEFT JOIN conversations c ON c.id = m.conversation_id
{w} GROUP BY fe.file_path ORDER BY MAX(fe.created_at) DESC"""

def _files(conn, conv=None):
    rows = conn.execute(_FILES.format(w="WHERE m.conversation_id = ?" if conv else ""), [conv] if conv else []).fetchall()
    title = f"conv {conv[:8]} ({(conn.execute('SELECT title FROM conversations WHERE id = ?', [conv]).fetchone() or ['?'])[0]})" if conv else f"{len(rows)} files"
    return dict(title=title[:60], rows=rows, fmt=lambda r: f"{str(r[2])[:16]}  {r[1]:>4} edits  {r[3]:<24} {r[0]}",
                enter=lambda r: _timeline(conn, r[0]))

def _timeline(conn, path):
    return dict(title=path, rows=edits_for(conn, path),
                fmt=lambda e: f"{str(e['ts'])[:16]}  {e['source']:<11} {e['type']:<9} {'exact' if e['type'] == 'write' or e['old'] else 'unk':<5} {e['conv'][:8]}  {(e['prompt'] or chr(10)).splitlines()[0][:70]}",
                enter=lambda e: _detail(conn, path, e), conv=lambda e: _files(conn, e["conv"]) if e["conv"] != "unknown" else None)

def _detail(conn, path, e):
    body = (e["content"] or "").splitlines() if e["type"] == "shell" else \
           ([f"- {l}" for l in e["old"].splitlines()] if e["old"] else ["(no before-state captured)"]) + [f"+ {l}" for l in (e["content"] or "").splitlines()]
    rows = [f"file:  {path}", f"conv:  {e['conv']}  ({e['source']})", f"when:  {e['ts']}   type: {e['type']}", "",
            "prompt:", *[f"  {l}" for l in (e["prompt"] or "(unknown)").splitlines()], "",
            "command:" if e["type"] == "shell" else "change:", *body]
    return dict(title=f"{e['type']} @ {str(e['ts'])[:16]}", rows=rows, fmt=str, enter=None,
                conv=lambda _: _files(conn, e["conv"]) if e["conv"] != "unknown" else None)

def _ui(scr, conn):
    curses.curs_set(0); curses.use_default_colors()
    [curses.init_pair(i, c, -1) for i, c in ((1, curses.COLOR_RED), (2, curses.COLOR_GREEN))]
    stack = [_files(conn)]
    while True:
        v = stack[-1]; h, w = scr.getmaxyx(); rows = v.get("flt", v["rows"])
        sel = v["sel"] = min(v.setdefault("sel", 0), max(len(rows) - 1, 0)); top = max(0, sel - (h - 4))
        scr.erase(); scr.addnstr(0, 0, " > ".join(x["title"] for x in stack)[-(w - 1):], w - 1, curses.A_BOLD)
        for y, r in enumerate(rows[top:top + h - 3]):
            s = v["fmt"](r); attr = curses.color_pair(1) if s.startswith("- ") else curses.color_pair(2) if s.startswith("+ ") else 0
            scr.addnstr(y + 2, 0, s, w - 1, curses.A_REVERSE if top + y == sel else attr)
        scr.addnstr(h - 1, 0, "arrows:move  enter:open  esc:back  c:conversation  /:filter  q:quit"[:w - 1], w - 1, curses.A_DIM)
        k = scr.getch()
        if k == ord("q"): return
        elif k == curses.KEY_UP: v["sel"] = max(sel - 1, 0)
        elif k == curses.KEY_DOWN: v["sel"] = min(sel + 1, max(len(rows) - 1, 0))
        elif k == curses.KEY_PPAGE: v["sel"] = max(sel - (h - 4), 0)
        elif k == curses.KEY_NPAGE: v["sel"] = min(sel + h - 4, max(len(rows) - 1, 0))
        elif k in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT) and rows and v["enter"]: stack.append(v["enter"](rows[sel]))
        elif k in (27, curses.KEY_LEFT, curses.KEY_BACKSPACE, 127) and len(stack) > 1: stack.pop()
        elif k == ord("c") and rows and v.get("conv") and (nv := v["conv"](rows[sel])): stack.append(nv)
        elif k == ord("/") and v["rows"]:
            curses.echo(); scr.addnstr(h - 1, 0, "filter: ".ljust(w - 1), w - 1); q = scr.getstr(h - 1, 8, 60).decode(); curses.noecho()
            v["flt"], v["sel"] = [r for r in v["rows"] if q.lower() in v["fmt"](r).lower()] if q else v["rows"], 0

def browse():
    """Browse the change graph: files -> timeline -> edit diff; `c` pivots to the conversation's files."""
    os.environ.setdefault("ESCDELAY", "25"); conn = _conn()
    try: curses.wrapper(lambda scr: _ui(scr, conn))
    finally: conn.close()
