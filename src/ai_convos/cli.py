#!/usr/bin/env python3
"""DuckDB-backed searchable archive for Claude, ChatGPT, and Codex conversations."""

import json, time, zipfile, hashlib, struct, sqlite3, subprocess, ssl, urllib.request, re, os, sysconfig
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from hashlib import pbkdf2_hmac
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import duckdb, typer

app = typer.Typer(help="AI Conversations DB - searchable archive for Claude, ChatGPT, and Codex")
def find_root():
    if r := os.environ.get("CONVOS_PROJECT_ROOT"): return Path(r).expanduser()
    if p := next((p for p in (Path.cwd(), *Path.cwd().parents) if (p / "pyproject.toml").exists()), None): return p
    if (p := Path.home() / ".local/share/ai-convos-db").joinpath("pyproject.toml").exists(): return p
    return Path(__file__).resolve().parents[2]
PROJECT_ROOT = find_root()
DATA_DIR, DB_PATH = PROJECT_ROOT / "data", PROJECT_ROOT / "data" / "convos.db"
STATE_PATH = DATA_DIR / "sync_state.json"

# ---- db helpers ----
def get_db(read_only: bool = False):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if read_only and not DB_PATH.exists():
        return None
    try:
        return duckdb.connect(str(DB_PATH), read_only=read_only)
    except Exception as e:
        if "Conflicting lock is held" in str(e) and read_only:
            raise ValueError("Database is locked for writing; read-only access failed.") from e
        if "Conflicting lock is held" in str(e):
            raise ValueError("Database is locked by another process. Try again after it finishes.") from e
        raise

def load_state():
    if not STATE_PATH.exists(): return {}
    try: return json.loads(STATE_PATH.read_text())
    except Exception: return {}

def save_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))

def detect_source(path: Path):
    if path.is_dir(): return "codex" if (path / "sessions").exists() else "claude-code"
    if path.suffix == ".zip" or "chatgpt" in path.name.lower(): return "chatgpt"
    data = json.loads(path.read_text())
    return "chatgpt" if "mapping" in data[0] else "claude" if "chat_messages" in data[0] else "chatgpt"

def latest_mtime(path: Path, glob: str = "*.jsonl"):
    return max((p.stat().st_mtime for p in path.rglob(glob)), default=0)

def init_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS conversations (
        id VARCHAR PRIMARY KEY, source VARCHAR NOT NULL, title VARCHAR, created_at TIMESTAMP, updated_at TIMESTAMP,
        model VARCHAR, cwd VARCHAR, git_branch VARCHAR, project_id VARCHAR, metadata JSON)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS messages (
        id VARCHAR PRIMARY KEY, conversation_id VARCHAR NOT NULL, role VARCHAR NOT NULL, content VARCHAR,
        thinking VARCHAR, created_at TIMESTAMP, model VARCHAR, metadata JSON)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS tool_calls (
        id VARCHAR PRIMARY KEY, message_id VARCHAR NOT NULL, tool_name VARCHAR, input JSON, output JSON,
        status VARCHAR, duration_ms INTEGER, created_at TIMESTAMP)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS attachments (
        id VARCHAR PRIMARY KEY, message_id VARCHAR NOT NULL, filename VARCHAR, mime_type VARCHAR,
        size INTEGER, path VARCHAR, url VARCHAR, created_at TIMESTAMP)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS artifacts (
        id VARCHAR PRIMARY KEY, conversation_id VARCHAR NOT NULL, artifact_type VARCHAR, title VARCHAR,
        content TEXT, language VARCHAR, created_at TIMESTAMP, version INTEGER)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS file_edits (
        id VARCHAR PRIMARY KEY, message_id VARCHAR NOT NULL, file_path VARCHAR, edit_type VARCHAR,
        content TEXT, created_at TIMESTAMP)""")
    conn.execute("INSTALL fts; LOAD fts")

def counts_by_source(conn):
    q = [("conversations", "source", 0), ("messages m JOIN conversations c ON c.id = m.conversation_id", "c.source", 1),
         ("tool_calls tc JOIN messages m ON tc.message_id = m.id JOIN conversations c ON c.id = m.conversation_id", "c.source", 2),
         ("attachments a JOIN messages m ON a.message_id = m.id JOIN conversations c ON c.id = m.conversation_id", "c.source", 3),
         ("file_edits fe JOIN messages m ON fe.message_id = m.id JOIN conversations c ON c.id = m.conversation_id", "c.source", 4)]
    out = {}; [out.setdefault(src, [0]*5).__setitem__(i, n) for tbl, col, i in q for src, n in conn.execute(f"SELECT {col}, COUNT(*) FROM {tbl} GROUP BY {col}").fetchall()]; return out

def load_fts(conn, allow_install: bool = False):
    try:
        if allow_install:
            conn.execute("INSTALL fts")
        conn.execute("LOAD fts")
    except Exception as e:
        raise ValueError("FTS extension not available. Run `convos init` once with network access.") from e

def ensure_fts_index(conn):
    exists = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'fts_main_messages'"
    ).fetchone()
    if not exists:
        conn.execute("PRAGMA create_fts_index('messages', 'id', 'content', 'thinking', overwrite=1)")

def rebuild_fts_index(conn):
    conn.execute("PRAGMA create_fts_index('messages', 'id', 'content', 'thinking', overwrite=1)")

def ensure_db_ready(conn):
    exists = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'messages'"
    ).fetchone()
    if not exists:
        typer.echo("Database not initialized. Run `convos init` or `convos sync`.")
        return False
    return True

def gen_id(source: str, oid: str) -> str: return hashlib.sha256(f"{source}:{oid}".encode()).hexdigest()[:16]
def ts_from_epoch(t):
    if t is None or t == "": return None
    try: return datetime.fromtimestamp(float(t))
    except Exception: return None
def ts_from_iso(t): return datetime.fromisoformat(t.replace("Z", "+00:00")) if t else None

def extract_content(content) -> dict:
    """Extract all content types from a message content field. Returns dict with text, thinking, tools, attachments."""
    if isinstance(content, str): return {"text": content, "thinking": None, "tools": [], "attachments": []}
    if not isinstance(content, list): return {"text": "", "thinking": None, "tools": [], "attachments": []}
    blocks = [b for b in content if isinstance(b, dict)]
    return {
        "text": "\n".join(b.get("text", "") or b.get("thinking", "") if b.get("type") in ("text", None) else "" for b in blocks).strip() or
                "\n".join(str(b) for b in content if isinstance(b, str)).strip(),
        "thinking": "\n".join(b["thinking"] for b in blocks if b.get("type") == "thinking" and b.get("thinking")).strip() or None,
        "tools": [{"name": b["name"], "input": b.get("input", {}), "id": b.get("id")} for b in blocks if b.get("type") == "tool_use"] +
                 [{"id": b.get("tool_use_id"), "output": b.get("content", "")} for b in blocks if b.get("type") == "tool_result"],
        "attachments": [{"filename": b.get("name", b.get("file_name")), "mime_type": b.get("content_type", b.get("file_type")),
                        "size": b.get("size", b.get("file_size")), "url": b.get("asset_pointer", b.get("url"))}
                       for b in blocks if b.get("type") in ("image_asset_pointer", "file") or b.get("content_type") in ("image_asset_pointer", "file")]
    }

# ---- cookie extraction ----
def read_safari_cookies(domain: str) -> dict[str, str]:
    path = Path.home() / "Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies"
    if not path.exists(): path = Path.home() / "Library/Cookies/Cookies.binarycookies"
    if not path.exists(): return {}
    cookies = {}
    target = domain.lstrip(".").lower()
    try:
        with open(path, 'rb') as f:
            if f.read(4) != b'cook': return {}
            num_pages = struct.unpack('>I', f.read(4))[0]
            page_sizes = [struct.unpack('>I', f.read(4))[0] for _ in range(num_pages)]
            for size in page_sizes:
                page = f.read(size)
                if page[:4] != b'\x00\x00\x01\x00': continue
                num_cookies = struct.unpack('<I', page[4:8])[0]
                offsets = [struct.unpack('<I', page[8+i*4:12+i*4])[0] for i in range(num_cookies)]
                for off in offsets:
                    url_off, name_off, val_off = struct.unpack('<I', page[off+16:off+20])[0], struct.unpack('<I', page[off+20:off+24])[0], struct.unpack('<I', page[off+28:off+32])[0]
                    def read_str(o): return page[off+o:page.find(b'\x00', off+o)].decode('utf-8', errors='ignore')
                    c_domain, c_name, c_val = read_str(url_off), read_str(name_off), read_str(val_off)
                    cd = c_domain.lstrip(".").lower()
                    if target in cd or cd in target or cd.endswith(target) or target.endswith(cd):
                        cookies[c_name] = c_val
    except PermissionError as e:
        raise ValueError(
            "Safari cookies are not readable. Grant Full Disk Access to your terminal or use -b chrome."
        ) from e
    return cookies

def read_chrome_cookies(domain: str) -> dict[str, str]:
    db_path = Path.home() / "Library/Application Support/Google/Chrome/Default/Cookies"
    if not db_path.exists(): db_path = Path.home() / "Library/Application Support/Google/Chrome/Default/Network/Cookies"
    if not db_path.exists(): return {}
    result = subprocess.run(["security", "find-generic-password", "-w", "-a", "Chrome", "-s", "Chrome Safe Storage"], capture_output=True, text=True)
    if result.returncode != 0: return {}
    key = pbkdf2_hmac('sha1', result.stdout.strip().encode(), b'saltysalt', 1003, 16)
    cookies = {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro&nolock=1", uri=True)
    for name, encrypted, host in conn.execute("SELECT name, encrypted_value, host_key FROM cookies WHERE host_key LIKE ?", (f"%{domain}%",)):
        if encrypted[:3] == b'v10':
            cipher = Cipher(algorithms.AES(key), modes.CBC(b' ' * 16))
            decrypted = cipher.decryptor().update(encrypted[3:]) + cipher.decryptor().finalize()
            cookies[name] = decrypted[:-decrypted[-1]].decode('utf-8', errors='ignore')
    conn.close()
    return cookies

def get_cookies(domain: str, browser: str = "safari") -> dict[str, str]:
    return read_safari_cookies(domain) if browser == "safari" else read_chrome_cookies(domain)

def get_cookies_any(domains: list[str], browser: str = "safari") -> dict[str, str]:
    cookies = {}
    for d in domains: cookies.update(get_cookies(d, browser))
    return cookies

def safari_cookie_domains():
    path = Path.home() / "Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies"
    if not path.exists(): path = Path.home() / "Library/Cookies/Cookies.binarycookies"
    if not path.exists(): return set()
    domains = set()
    with open(path, 'rb') as f:
        if f.read(4) != b'cook': return domains
        num_pages = struct.unpack('>I', f.read(4))[0]
        page_sizes = [struct.unpack('>I', f.read(4))[0] for _ in range(num_pages)]
        for size in page_sizes:
            page = f.read(size)
            if page[:4] != b'\x00\x00\x01\x00': continue
            num_cookies = struct.unpack('<I', page[4:8])[0]
            offsets = [struct.unpack('<I', page[8+i*4:12+i*4])[0] for i in range(num_cookies)]
            for off in offsets:
                url_off = struct.unpack('<I', page[off+16:off+20])[0]
                end = page.find(b'\x00', off+url_off)
                if end != -1: domains.add(page[off+url_off:end].decode('utf-8', errors='ignore'))
    return domains

def chrome_cookie_domains():
    db_path = Path.home() / "Library/Application Support/Google/Chrome/Default/Cookies"
    if not db_path.exists(): db_path = Path.home() / "Library/Application Support/Google/Chrome/Default/Network/Cookies"
    if not db_path.exists(): return set()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro&nolock=1", uri=True)
    domains = {r[0] for r in conn.execute("SELECT DISTINCT host_key FROM cookies")}
    conn.close()
    return domains

def fetch_json(url: str, cookies: dict[str, str], headers: dict = None, timeout: int = 30, retries: int = 2) -> dict:
    parts = []
    for k, v in cookies.items():
        s = f"{k}={v}"
        try: s.encode("latin-1")
        except UnicodeEncodeError: continue
        parts.append(s)
    cookie_str = "; ".join(parts)
    hdrs = {"Cookie": cookie_str, "User-Agent": "Mozilla/5.0", "Accept": "application/json", **(headers or {})}
    req = urllib.request.Request(url, headers=hdrs)
    for i in range(retries+1):
        try:
            with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=timeout) as resp:
                return json.loads(resp.read())
        except Exception as e:
            if i == retries: raise
            time.sleep(1 + i)

# ---- result type ----
class ParseResult:
    def __init__(self, convs=None, msgs=None, tools=None, attachs=None, artifacts=None, edits=None):
        self.convs, self.msgs, self.tools = convs or [], msgs or [], tools or []
        self.attachs, self.artifacts, self.edits = attachs or [], artifacts or [], edits or []

def parse_source(path: Path, source: Optional[str] = None) -> ParseResult:
    parsers = {"chatgpt": parse_chatgpt, "claude": parse_claude, "claude-code": parse_claude_code, "codex": parse_codex}
    src = source or detect_source(path)
    if src not in parsers: raise ValueError(f"Unknown source: {src}")
    return parsers[src](path)

# ---- web fetchers ----
def fetch_chatgpt(browser: str = "safari", limit: int = 0) -> ParseResult:
    hosts = [("https://chatgpt.com", ["chatgpt.com"]),
             ("https://chat.openai.com", ["chat.openai.com", "openai.com"])]
    cookies, base = None, None
    for url, domains in hosts:
        if c := get_cookies_any(domains, browser): cookies, base = c, url; break
    if not cookies: raise ValueError(f"No ChatGPT cookies found in {browser}")
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
          if browser == "safari" else
          "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    headers = {"Origin": base, "Referer": f"{base}/", "User-Agent": ua, "Accept": "application/json",
               "Accept-Language": "en-US,en;q=0.9", "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors",
               "Sec-Fetch-Dest": "empty"}
    try:
        session = fetch_json(f"{base}/api/auth/session", cookies, headers)
        if token := session.get("accessToken"): headers["Authorization"] = f"Bearer {token}"
    except Exception:
        pass
    r = ParseResult()
    def parse_item(item):
        cid = gen_id("chatgpt", item["id"])
        gizmo = item.get("gizmo_id")
        conv = fetch_json(f"{base}/backend-api/conversation/{item['id']}", cookies, headers, timeout=60)
        msgs, tools, attachs = [], [], []
        for nid, node in conv.get("mapping", {}).items():
            if not (msg := node.get("message")): continue
            mid = gen_id("chatgpt", f"{cid}:{nid}")
            role, meta = msg.get("author", {}).get("role", "unknown"), msg.get("metadata", {})
            ts, model = ts_from_epoch(msg.get("create_time")), meta.get("model_slug")
            if role == "tool" or meta.get("invoked_plugin"):
                tools.append(dict(id=gen_id("chatgpt", f"tool:{mid}"), message_id=mid, tool_name=meta.get("invoked_plugin", {}).get("namespace", role),
                                  input=json.dumps(meta.get("args", {})), output=json.dumps(msg.get("content", {})), status="complete", duration_ms=None, created_at=ts))
            for i, part in enumerate(msg.get("content", {}).get("parts", []) if msg.get("content") else []):
                if isinstance(part, dict) and part.get("content_type") in ("image_asset_pointer", "file"):
                    attachs.append(dict(id=gen_id("chatgpt", f"attach:{mid}:{i}"), message_id=mid, filename=part.get("name", ""),
                                        mime_type=part.get("content_type"), size=part.get("size"), path=None, url=part.get("asset_pointer"), created_at=ts))
                elif isinstance(part, str) and part.strip():
                    msgs.append(dict(id=mid, conversation_id=cid, role=role, content=part.strip(), thinking=None, created_at=ts, model=model, metadata=json.dumps(meta)))
        return dict(conv=dict(id=cid, source="chatgpt", title=item.get("title"), created_at=ts_from_epoch(item.get("create_time")),
                              updated_at=ts_from_epoch(item.get("update_time")), model=item.get("model"), cwd=None, git_branch=None,
                              project_id=gizmo, metadata=json.dumps({"gizmo_id": gizmo}) if gizmo else "{}"),
                    msgs=msgs, tools=tools, attachs=attachs)
    offset, total, fetched, seen = 0, None, 0, set()
    debug = os.environ.get("CONVOS_CHATGPT_DEBUG")
    while True:
        data = fetch_json(f"{base}/backend-api/conversations?offset={offset}&limit=100", cookies, headers, timeout=60)
        if total is None: total = data.get("total")
        items = data.get("items", [])
        if debug: print(f"  chatgpt page offset={offset} items={len(items)} total={total} keys={','.join(data.keys())}", flush=True)
        if not items: break
        page = [it for it in items if it["id"] not in seen][: (limit - fetched) if limit > 0 else len(items)]
        for it in page: seen.add(it["id"])
        with ThreadPoolExecutor(max_workers=min(4, len(page))) as ex:
            results = list(ex.map(parse_item, page)) if page else []
        r.convs += [x["conv"] for x in results]; r.msgs += [m for x in results for m in x["msgs"]]
        r.tools += [t for x in results for t in x["tools"]]; r.attachs += [a for x in results for a in x["attachs"]]
        fetched += len(results)
        offset += len(items)
        if not page: break
        if limit > 0 and fetched >= limit: break
        if total and fetched > total: total = None
        typer.echo(f"  fetched {fetched}{'/' + str(total) if total else ''}")
    return r

def fetch_claude(browser: str = "safari", limit: int = 0, since: datetime = None) -> ParseResult:
    cookies = get_cookies("claude.ai", browser)
    if not cookies: raise ValueError(f"No Claude cookies found in {browser}")
    headers = {"Origin": "https://claude.ai", "Referer": "https://claude.ai/",
               "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
               "Accept": "application/json", "Accept-Language": "en-US,en;q=0.9",
               "anthropic-client-sha": "unknown", "anthropic-client-version": "unknown"}
    print("  claude listing...", flush=True)
    orgs = fetch_json("https://claude.ai/api/organizations", cookies, headers)
    org_id = orgs[0]["uuid"] if orgs else None
    if not org_id: raise ValueError("Could not get Claude org ID")
    r = ParseResult()
    data = fetch_json(f"https://claude.ai/api/organizations/{org_id}/chat_conversations", cookies, headers)
    items = data if limit == 0 else data[:limit]
    if items: print(f"  claude total {len(items)}", flush=True)
    fetched, step = 0, max(1, len(items)//10)
    for idx, item in enumerate(items):
        updated = ts_from_iso(item.get("updated_at") or item.get("created_at"))
        if since and updated and updated <= since:
            if idx == len(items)-1 or (idx+1) % step == 0: print(f"  claude fetched {fetched}/{len(items)}", flush=True)
            continue
        cid = gen_id("claude", item["uuid"])
        project = item.get("project_uuid")
        r.convs.append(dict(id=cid, source="claude", title=item.get("name"), created_at=ts_from_iso(item.get("created_at")),
                           updated_at=ts_from_iso(item.get("updated_at")), model=item.get("model"), cwd=None, git_branch=None,
                           project_id=project, metadata=json.dumps({"project_uuid": project}) if project else "{}"))
        conv = fetch_json(f"https://claude.ai/api/organizations/{org_id}/chat_conversations/{item['uuid']}", cookies, headers)
        for m in conv.get("chat_messages", []):
            mid = gen_id("claude", f"{cid}:{m.get('uuid', '')}")
            ts = ts_from_iso(m.get("created_at"))
            for i, a in enumerate(m.get("attachments", [])):
                r.attachs.append(dict(id=gen_id("claude", f"attach:{mid}:{i}"), message_id=mid, filename=a.get("file_name"),
                                     mime_type=a.get("file_type"), size=a.get("file_size"), path=None, url=a.get("url"), created_at=ts))
            content = m.get("content", []) if isinstance(m.get("content"), list) else [{"type": "text", "text": m.get("text", "")}]
            text_parts = []
            for j, block in enumerate(content):
                if isinstance(block, dict):
                    if block.get("type") == "tool_use":
                        r.tools.append(dict(id=gen_id("claude", f"tool:{mid}:{j}"), message_id=mid, tool_name=block.get("name"),
                                           input=json.dumps(block.get("input", {})), output="{}", status="pending", duration_ms=None, created_at=ts))
                    elif block.get("type") == "tool_result":
                        r.tools.append(dict(id=gen_id("claude", f"toolres:{mid}:{j}"), message_id=mid, tool_name=block.get("tool_use_id"),
                                           input="{}", output=json.dumps(block.get("content", "")), status="complete", duration_ms=None, created_at=ts))
                    elif block.get("type") == "text": text_parts.append(block.get("text", ""))
                elif isinstance(block, str): text_parts.append(block)
            if text := "\n".join(text_parts).strip():
                r.msgs.append(dict(id=mid, conversation_id=cid, role=m.get("sender", "unknown"), content=text, thinking=None, created_at=ts, model=None, metadata="{}"))
        fetched += 1
        if idx == len(items)-1 or (idx+1) % step == 0: print(f"  claude fetched {fetched}/{len(items)}", flush=True)
    return r

# ---- file parsers ----
def parse_chatgpt(path: Path) -> ParseResult:
    data = json.load(zipfile.ZipFile(path).open('conversations.json')) if path.suffix == ".zip" else json.loads(path.read_text())
    r = ParseResult()
    for c in data:
        cid, gizmo = gen_id("chatgpt", c.get("id", "")), c.get("gizmo_id")
        r.convs.append(dict(id=cid, source="chatgpt", title=c.get("title"), created_at=ts_from_epoch(c.get("create_time")),
                           updated_at=ts_from_epoch(c.get("update_time")), model=c.get("default_model_slug"), cwd=None, git_branch=None,
                           project_id=gizmo, metadata=json.dumps({"gizmo_id": gizmo}) if gizmo else "{}"))
        for nid, node in c.get("mapping", {}).items():
            if not (msg := node.get("message")): continue
            mid, role, meta = gen_id("chatgpt", f"{cid}:{nid}"), msg.get("author", {}).get("role", "unknown"), msg.get("metadata", {})
            ts, model = ts_from_epoch(msg.get("create_time")), meta.get("model_slug")
            if role == "tool" or meta.get("invoked_plugin"):
                r.tools.append(dict(id=gen_id("chatgpt", f"tool:{mid}"), message_id=mid, tool_name=meta.get("invoked_plugin", {}).get("namespace", role),
                                   input=json.dumps(meta.get("args", {})), output=json.dumps(msg.get("content", {})), status="complete", duration_ms=None, created_at=ts))
            for i, part in enumerate(msg.get("content", {}).get("parts", []) if msg.get("content") else []):
                if isinstance(part, dict) and part.get("content_type") in ("image_asset_pointer", "file"):
                    r.attachs.append(dict(id=gen_id("chatgpt", f"attach:{mid}:{i}"), message_id=mid, filename=part.get("name", ""),
                                         mime_type=part.get("content_type"), size=part.get("size"), path=None, url=part.get("asset_pointer"), created_at=ts))
                elif isinstance(part, str) and part.strip():
                    r.msgs.append(dict(id=mid, conversation_id=cid, role=role, content=part.strip(), thinking=None, created_at=ts, model=model, metadata=json.dumps(meta)))
    return r

def parse_claude(path: Path) -> ParseResult:
    data = json.loads(path.read_text())
    def parse_conv(c):
        cid = gen_id("claude", c["uuid"] if "uuid" in c else c["id"])
        msgs_data = c.get("chat_messages", [])
        return {
            "conv": dict(id=cid, source="claude", title=c.get("name") or c.get("title"), created_at=ts_from_iso(c.get("created_at")),
                        updated_at=ts_from_iso(c.get("updated_at")), model=c.get("model"), cwd=None, git_branch=None, project_id=None, metadata="{}"),
            "msgs": [dict(id=(mid := gen_id("claude", f"{cid}:{m['uuid'] if 'uuid' in m else m['id']}")), conversation_id=cid,
                        role=m.get("sender", "unknown"), content=ec["text"], thinking=ec["thinking"],
                        created_at=ts_from_iso(m.get("created_at")), model=None, metadata="{}")
                    for m in msgs_data if (ec := extract_content(m.get("text") or m.get("content", "")))["text"]],
            "attachs": [dict(id=gen_id("claude", f"attach:{gen_id('claude', f'{cid}:{m['uuid'] if 'uuid' in m else m['id']}')}:{i}"),
                            message_id=gen_id("claude", f"{cid}:{m['uuid'] if 'uuid' in m else m['id']}"),
                            filename=a.get("file_name"), mime_type=a.get("file_type"), size=a.get("file_size"),
                            path=None, url=a.get("url"), created_at=ts_from_iso(m.get("created_at")))
                       for m in msgs_data for i, a in enumerate(m.get("attachments", []))]}
    parsed = [parse_conv(c) for c in data]
    return ParseResult(convs=[p["conv"] for p in parsed], msgs=[m for p in parsed for m in p["msgs"]],
                      attachs=[a for p in parsed for a in p["attachs"]])

def load_jsonl(path: Path) -> list[dict]:
    def loads(line):
        try: return json.loads(line)
        except Exception: return None
    return [e for line in path.read_text().splitlines() if line.strip() and (e := loads(line))]

def parse_claude_code_session(jsonl: Path) -> dict:
    """Parse single Claude Code session, returns dict with conv, msgs, tools, edits or None if empty."""
    events = load_jsonl(jsonl)
    if not events: return None
    cid, src = gen_id("claude-code", str(jsonl)), "claude-code"
    timestamps = [ts_from_iso(e["timestamp"]) for e in events if "timestamp" in e]
    system = next((e for e in events if e.get("type") == "system"), {})
    msg_events = [(i, e) for i, e in enumerate(events) if "message" in e]

    def make_msg(idx, i, e):
        c = extract_content(e["message"].get("content", e["message"].get("text", "")))
        return dict(id=gen_id(src, f"{cid}:{idx}"), conversation_id=cid, role=e["type"],
                   content=c["text"], thinking=c["thinking"], created_at=ts_from_iso(e.get("timestamp")),
                   model="claude" if e["type"] == "assistant" else None, metadata="{}")

    def make_tools(idx, i, e):
        c, ts = extract_content(e["message"].get("content", [])), ts_from_iso(e.get("timestamp"))
        mid = gen_id(src, f"{cid}:{idx}")
        return [dict(id=gen_id(src, f"tool:{cid}:{idx}:{j}"), message_id=mid, tool_name=t.get("name", t.get("id")),
                    input=json.dumps(t.get("input", {})), output=json.dumps(t.get("output", "")) if "output" in t else "{}",
                    status="complete" if "output" in t else "pending", duration_ms=None, created_at=ts) for j, t in enumerate(c["tools"])]

    def make_edits(idx, i, e):
        c, ts = extract_content(e["message"].get("content", [])), ts_from_iso(e.get("timestamp"))
        mid = gen_id(src, f"{cid}:{idx}")
        return [dict(id=gen_id(src, f"edit:{cid}:{idx}:{j}"), message_id=mid, file_path=t["input"]["file_path"],
                    edit_type=t["name"].lower(), content=t["input"].get("content") or t["input"].get("new_string", ""), created_at=ts)
               for j, t in enumerate(c["tools"]) if t.get("name") in ("Write", "Edit", "MultiEdit") and t.get("input", {}).get("file_path")]

    msgs = [make_msg(idx, i, e) for idx, (i, e) in enumerate(msg_events) if extract_content(e["message"].get("content", ""))["text"]]
    if not msgs: return None
    return {
        "conv": dict(id=cid, source=src, title=f"{jsonl.parent.name.replace('-Users-', '~/').replace('-', '/')} ({jsonl.stem[:8]})",
                    created_at=timestamps[0] if timestamps else None, updated_at=timestamps[-1] if timestamps else None,
                    model="claude", cwd=system.get("cwd"), git_branch=system.get("gitBranch"), project_id=None,
                    metadata=json.dumps({"session_id": jsonl.stem})),
        "msgs": msgs,
        "tools": [t for idx, (i, e) in enumerate(msg_events) for t in make_tools(idx, i, e)],
        "edits": [ed for idx, (i, e) in enumerate(msg_events) for ed in make_edits(idx, i, e)]}

def parse_claude_code(projects_dir: Path, files: list[Path] | None = None) -> ParseResult:
    sessions = [s for jsonl in (files or projects_dir.rglob("*.jsonl")) if (s := parse_claude_code_session(jsonl))]
    return ParseResult(
        convs=[s["conv"] for s in sessions], msgs=[m for s in sessions for m in s["msgs"]],
        tools=[t for s in sessions for t in s["tools"]], edits=[e for s in sessions for e in s["edits"]])

def parse_codex_session(jsonl: Path) -> dict | None:
    """Parse single Codex session, returns dict with conv, msgs, tools, edits or None if empty."""
    events = load_jsonl(jsonl)
    if not events: return None
    cid, src = gen_id("codex", str(jsonl)), "codex"
    timestamps = [ts_from_iso(e["timestamp"]) for e in events if "timestamp" in e]
    meta = next((e["payload"] for e in events if e.get("type") == "session_meta"), {})
    items = [(i, e["payload"]) for i, e in enumerate(events) if e.get("type") == "response_item" and "payload" in e]

    def extract_msg_text(p):
        return "\n".join(b["text"] for b in p.get("content", []) if isinstance(b, dict) and b.get("type") in ("input_text", "output_text", "text") and b.get("text"))
    def norm_args(p):
        return json.loads(a) if isinstance((a := p.get("arguments", {})), str) else a

    msgs = [dict(id=gen_id(src, f"{cid}:{i}"), conversation_id=cid, role=p["role"], content=text.strip(),
                thinking=None, created_at=timestamps[i] if i < len(timestamps) else None, model=None, metadata="{}")
           for i, p in items if p.get("type") == "message" and p.get("role") not in ("developer", "system") and (text := extract_msg_text(p))]
    if not msgs: return None

    tools = [dict(id=gen_id(src, f"tool:{cid}:{i}"), message_id=gen_id(src, f"{cid}:{i}"), tool_name=p["name"],
                 input=json.dumps(args), output="{}", status="pending", duration_ms=None,
                 created_at=timestamps[i] if i < len(timestamps) else None)
            for i, p in items if p.get("type") == "function_call" and (args := norm_args(p))] + \
           [dict(id=gen_id(src, f"toolout:{cid}:{i}"), message_id=gen_id(src, f"{cid}:{i}"), tool_name=p.get("call_id"),
                 input="{}", output=json.dumps(p.get("output", "")), status="complete", duration_ms=None,
                 created_at=timestamps[i] if i < len(timestamps) else None)
            for i, p in items if p.get("type") == "function_call_output"]

    edits = [dict(id=gen_id(src, f"edit:{cid}:{i}:{j}"), message_id=gen_id(src, f"{cid}:{i}"),
                 file_path=m.group(1), edit_type="shell", content=cmd,
                 created_at=timestamps[i] if i < len(timestamps) else None)
            for i, p in items if p.get("type") == "function_call" and p.get("name") == "shell"
            and (args := norm_args(p)) and (c := args.get("command")) and (cmd := " ".join(c) if isinstance(c, list) else c)
            for j, pat in enumerate([r'(?:cat|echo).*[>].*?([^\s>]+)', r'(?:sed|awk).*?([^\s]+)$'])
            if (m := re.search(pat, cmd))]

    return {
        "conv": dict(id=cid, source=src, title=meta.get("cwd") or jsonl.stem,
                    created_at=timestamps[0] if timestamps else None, updated_at=timestamps[-1] if timestamps else None,
                    model=meta.get("model_provider", "openai"), cwd=meta.get("cwd"), git_branch=None, project_id=None,
                    metadata=json.dumps({"cli_version": meta.get("cli_version"), "session_id": jsonl.stem})),
        "msgs": msgs, "tools": tools, "edits": edits}

def parse_codex(codex_dir: Path, files: list[Path] | None = None) -> ParseResult:
    sessions_dir = codex_dir / "sessions"
    if not sessions_dir.exists(): return ParseResult()
    sessions = [s for jsonl in (files or sessions_dir.rglob("*.jsonl")) if (s := parse_codex_session(jsonl))]
    return ParseResult(
        convs=[s["conv"] for s in sessions], msgs=[m for s in sessions for m in s["msgs"]],
        tools=[t for s in sessions for t in s["tools"]], edits=[e for s in sessions for e in s["edits"]])

def upsert(conn, r: ParseResult):
    cids, mids = [c["id"] for c in r.convs], [m["id"] for m in r.msgs]
    cur = conn.execute
    existing = set(cur(f"SELECT id FROM conversations WHERE id IN ({','.join(['?']*len(cids))})", cids).fetchall()) if cids else set()
    existing_msgs = set(cur(f"SELECT id FROM messages WHERE id IN ({','.join(['?']*len(mids))})", mids).fetchall()) if mids else set()
    new_convs = set(cids) - {x[0] for x in existing}
    new_msgs = set(mids) - {x[0] for x in existing_msgs}
    updated = {m["conversation_id"] for m in r.msgs if m["id"] in new_msgs} - new_convs
    for c in r.convs: conn.execute("INSERT OR REPLACE INTO conversations VALUES (?,?,?,?,?,?,?,?,?,?)", list(c.values()))
    for m in r.msgs: conn.execute("INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?)", list(m.values()))
    for t in r.tools: conn.execute("INSERT OR REPLACE INTO tool_calls VALUES (?,?,?,?,?,?,?,?)", list(t.values()))
    for a in r.attachs: conn.execute("INSERT OR REPLACE INTO attachments VALUES (?,?,?,?,?,?,?,?)", list(a.values()))
    for a in r.artifacts: conn.execute("INSERT OR REPLACE INTO artifacts VALUES (?,?,?,?,?,?,?,?)", list(a.values()))
    for e in r.edits: conn.execute("INSERT OR REPLACE INTO file_edits VALUES (?,?,?,?,?,?)", list(e.values()))
    return len(r.convs), len(r.msgs), len(r.tools), len(r.attachs), len(r.edits), len(new_convs), len(updated)

# ---- commands ----
@app.command()
def init():
    conn = get_db(); init_schema(conn); rebuild_fts_index(conn); conn.close()
    install_skills()
    typer.echo(f"Database initialized at {DB_PATH}")

@app.command()
def search(query: str, source: Optional[str] = typer.Option(None, "-s"), days: Optional[int] = typer.Option(None, "-d"),
           role: Optional[str] = typer.Option(None, "-r"), thinking: bool = typer.Option(False, "--thinking", "-t"),
           limit: int = typer.Option(20, "-n"), context: int = typer.Option(300, "-c")):
    """Full-text search with filters. Use --thinking to include reasoning."""
    conn = get_db(read_only=True)
    if conn is None: typer.echo("Database not found. Run `convos init` or `convos sync`."); return
    if not ensure_db_ready(conn): conn.close(); return
    try:
        load_fts(conn)
    except ValueError as e:
        conn.close(); typer.echo(str(e)); return
    exists = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'fts_main_messages'"
    ).fetchone()
    if not exists:
        conn.close()
        try:
            wconn = get_db()
            init_schema(wconn); rebuild_fts_index(wconn); wconn.close()
            conn = get_db(read_only=True)
            load_fts(conn)
        except Exception:
            typer.echo("FTS index missing and DB is locked. Try again after the writer finishes.")
            return
    where_parts, params = ["score IS NOT NULL"], [query]
    if source: where_parts.append("c.source = ?"); params.append(source)
    if days: where_parts.append("m.created_at > ?"); params.append(datetime.now() - timedelta(days=days))
    if role: where_parts.append("m.role = ?"); params.append(role)
    params.append(limit)
    results = conn.execute(f"""
        SELECT m.content, m.thinking, m.role, m.created_at, fts_main_messages.match_bm25(m.id, ?) as score, c.title, c.source, c.id, c.cwd
        FROM messages m JOIN conversations c ON m.conversation_id = c.id WHERE {' AND '.join(where_parts)} ORDER BY score DESC LIMIT ?
    """, params).fetchall()
    conn.close()
    if not results: typer.echo("No results"); return
    for content, think, role, ts, score, title, src, cid, cwd in results:
        preview = content[:context] + "..." if len(content) > context else content
        for word in query.split(): preview = re.sub(f"({re.escape(word)})", r"\033[1;33m\1\033[0m", preview, flags=re.I)
        loc = f" @ {cwd}" if cwd else ""
        typer.echo(f"\n{'='*60}\n[{src}] {title or 'Untitled'}{loc} ({cid[:8]})\n{role} @ {ts or '?'} (score: {score:.2f})\n{'-'*40}\n{preview}")
        if thinking and think: typer.echo(f"\n[THINKING]\n{think[:500]}{'...' if len(think)>500 else ''}")
    typer.echo(f"\n{len(results)} results")

@app.command("list")
def list_convos(source: Optional[str] = typer.Option(None, "-s"), days: Optional[int] = typer.Option(None, "-d"),
                cwd: Optional[str] = typer.Option(None, "--cwd"), limit: int = typer.Option(50, "-n")):
    conn = get_db(read_only=True)
    if conn is None: typer.echo("Database not found. Run `convos init` or `convos sync`."); return
    if not ensure_db_ready(conn): conn.close(); return
    where_parts, params = [], []
    if source: where_parts.append("source = ?"); params.append(source)
    if days: where_parts.append("created_at > ?"); params.append(datetime.now() - timedelta(days=days))
    if cwd: where_parts.append("cwd LIKE ?"); params.append(f"%{cwd}%")
    where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    params.append(limit)
    rows = conn.execute(f"""SELECT id, source, title, created_at, cwd, git_branch, (SELECT COUNT(*) FROM messages WHERE conversation_id = c.id)
        FROM conversations c {where} ORDER BY created_at DESC LIMIT ?""", params).fetchall()
    conn.close()
    for cid, src, title, ts, cwd, branch, cnt in rows:
        loc = f" [{cwd}]" if cwd else ""
        br = f" ({branch})" if branch else ""
        typer.echo(f"{cid[:8]}  [{src:12}]  {str(ts)[:16] if ts else '?':16}  {cnt:3} msgs  {(title or 'Untitled')[:30]}{loc}{br}")
    typer.echo(f"\n{len(rows)} conversations")

@app.command()
def show(conv_id: str, tools_: bool = typer.Option(False, "--tools", "-t"), thinking: bool = typer.Option(False, "--thinking")):
    conn = get_db(read_only=True)
    if conn is None: typer.echo("Database not found. Run `convos init` or `convos sync`."); return
    if not ensure_db_ready(conn): conn.close(); return
    conv = conn.execute("SELECT id, source, title, created_at, model, cwd, git_branch, project_id FROM conversations WHERE id LIKE ?", [f"{conv_id}%"]).fetchone()
    if not conv: typer.echo("Not found"); return
    cid, src, title, ts, model, cwd, branch, proj = conv
    typer.echo(f"{'='*60}\n[{src}] {title or 'Untitled'}\nID: {cid}\nCreated: {ts}\nModel: {model}")
    if cwd: typer.echo(f"Directory: {cwd}")
    if branch: typer.echo(f"Branch: {branch}")
    if proj: typer.echo(f"Project: {proj}")
    typer.echo(f"{'='*60}\n")

    msgs = conn.execute("SELECT role, content, thinking, created_at, model FROM messages WHERE conversation_id = ? ORDER BY created_at", [cid]).fetchall()
    for role, content, think, mts, mmodel in msgs:
        model_str = f" [{mmodel}]" if mmodel else ""
        typer.echo(f"\n--- {role.upper()}{model_str} @ {mts or '?'} ---\n{content}")
        if thinking and think: typer.echo(f"\n[THINKING]\n{think}")

    if tools_:
        tcs = conn.execute("SELECT tool_name, input, output, status, duration_ms FROM tool_calls tc JOIN messages m ON tc.message_id = m.id WHERE m.conversation_id = ?", [cid]).fetchall()
        if tcs:
            typer.echo(f"\n{'='*60}\nTOOL CALLS ({len(tcs)})\n{'='*60}")
            for name, inp, out, status, dur in tcs:
                dur_str = f" ({dur}ms)" if dur else ""
                typer.echo(f"\n{name} [{status}]{dur_str}\nIn: {inp[:200]}{'...' if len(inp)>200 else ''}\nOut: {out[:200]}{'...' if len(out)>200 else ''}")
    conn.close()

@app.command()
def get(conv_id: str, since: Optional[str] = typer.Option(None, "--since"), after: Optional[str] = typer.Option(None, "--after"),
        limit: int = typer.Option(50, "-n"), thinking: bool = typer.Option(False, "--thinking")):
    conn = get_db(read_only=True)
    if conn is None: typer.echo("Database not found. Run `convos init` or `convos sync`."); return
    if not ensure_db_ready(conn): conn.close(); return
    conv = conn.execute("SELECT id, source, title FROM conversations WHERE id LIKE ?", [f"{conv_id}%"]).fetchone()
    if not conv: typer.echo("Not found"); return
    cid, src, title = conv
    where, params = ["conversation_id = ?"], [cid]
    if since: where.append("created_at > ?"); params.append(ts_from_iso(since))
    if after:
        row = conn.execute("SELECT created_at FROM messages WHERE id LIKE ? AND conversation_id = ? ORDER BY created_at LIMIT 1", [f"{after}%", cid]).fetchone()
        if not row or not row[0]: conn.close(); typer.echo("After message not found or missing timestamp"); return
        where.append("created_at > ?"); params.append(row[0])
    params.append(limit)
    msgs = conn.execute(f"SELECT id, role, content, thinking, created_at, model FROM messages WHERE {' AND '.join(where)} ORDER BY created_at LIMIT ?", params).fetchall()
    conn.close()
    typer.echo(f"{'='*60}\n[{src}] {title or 'Untitled'}\nID: {cid}\n{'='*60}\n")
    for mid, role, content, think, mts, mmodel in msgs:
        model_str = f" [{mmodel}]" if mmodel else ""
        typer.echo(f"\n--- {role.upper()}{model_str} @ {mts or '?'} ({mid[:8]}) ---\n{content}")
        if thinking and think: typer.echo(f"\n[THINKING]\n{think}")
    typer.echo(f"\n{len(msgs)} messages")

@app.command()
def edits(path: Optional[str] = typer.Argument(None), limit: int = typer.Option(30, "-n")):
    """List file edits."""
    conn = get_db(read_only=True)
    if conn is None: typer.echo("Database not found. Run `convos init` or `convos sync`."); return
    if not ensure_db_ready(conn): conn.close(); return
    if path:
        results = conn.execute("SELECT file_path, edit_type, content, created_at FROM file_edits WHERE file_path LIKE ? ORDER BY created_at DESC LIMIT ?", [f"%{path}%", limit]).fetchall()
    else:
        results = conn.execute("SELECT file_path, edit_type, content, created_at FROM file_edits ORDER BY created_at DESC LIMIT ?", [limit]).fetchall()
    conn.close()
    for fp, et, content, ts in results:
        typer.echo(f"\n{'-'*40}\n{fp} [{et}] @ {ts}\n{content[:200]}{'...' if len(content)>200 else ''}")
    typer.echo(f"\n{len(results)} edits")

@app.command()
def tools(query: Optional[str] = typer.Argument(None), limit: int = typer.Option(30, "-n")):
    conn = get_db(read_only=True)
    if conn is None: typer.echo("Database not found. Run `convos init` or `convos sync`."); return
    if not ensure_db_ready(conn): conn.close(); return
    if query:
        results = conn.execute("SELECT tool_name, input, output, status, created_at FROM tool_calls WHERE tool_name ILIKE ? OR input ILIKE ? OR output ILIKE ? ORDER BY created_at DESC LIMIT ?",
                              [f"%{query}%"]*3 + [limit]).fetchall()
    else:
        results = conn.execute("SELECT tool_name, input, output, status, created_at FROM tool_calls ORDER BY created_at DESC LIMIT ?", [limit]).fetchall()
    conn.close()
    for name, inp, out, status, ts in results:
        typer.echo(f"\n{'-'*40}\n{name} [{status}] @ {ts}\nIn: {inp[:100]}{'...' if len(inp)>100 else ''}\nOut: {out[:100]}{'...' if len(out)>100 else ''}")
    typer.echo(f"\n{len(results)} tool calls")

@app.command()
def doctor(verbose: bool = typer.Option(False, "-v")):
    def has(domains, host): return any(host in d or d in host for d in domains)
    targets = ["chatgpt.com", "chat.openai.com", "openai.com", "claude.ai"]
    for name, getter in [("safari", safari_cookie_domains), ("chrome", chrome_cookie_domains)]:
        try: domains = getter()
        except PermissionError: typer.echo(f"{name}: no access to cookies"); continue
        summary = ", ".join(f"{t}={'yes' if has(domains, t) else 'no'}" for t in targets)
        typer.echo(f"{name}: {summary}")
        if verbose:
            cg = read_safari_cookies("chatgpt.com") if name == "safari" else read_chrome_cookies("chatgpt.com")
            keys = set(cg.keys())
            sig = [k for k in ["__Secure-next-auth.session-token", "__Secure-next-auth.session-token.0",
                               "__Secure-next-auth.session-token.1", "cf_clearance", "__cf_bm"] if k in keys]
            typer.echo(f"{name}: chatgpt cookies={len(cg)} keys={','.join(sig) if sig else 'none'}")

@app.command()
def install_skills():
    skill = PROJECT_ROOT / "skills" / "agent-convos" / "SKILL.md"
    if not skill.exists():
        data_root = Path(sysconfig.get_paths().get("data", ""))
        skill = data_root / "share" / "ai-convos-db" / "skills" / "agent-convos" / "SKILL.md"
    if not skill.exists(): typer.echo(f"Missing skill: {skill}", err=True); raise typer.Exit(1)
    text = skill.read_text()
    for base in [Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "skills",
                 Path.home() / ".claude" / "skills"]:
        dest = base / "agent-convos" / "SKILL.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
        typer.echo(f"Installed {dest}")

@app.command()
def export(output: Path, fmt: str = typer.Option("json", "-f"), source: Optional[str] = typer.Option(None, "-s")):
    conn = get_db(read_only=True)
    if conn is None: typer.echo("Database not found. Run `convos init` or `convos sync`."); return
    if not ensure_db_ready(conn): conn.close(); return
    where = f"WHERE c.source = '{source}'" if source else ""
    if fmt == "json":
        rows = conn.execute(f"SELECT c.id, c.source, c.title, c.created_at, c.updated_at, c.model, c.cwd, c.git_branch, c.project_id FROM conversations c {where}").fetchall()
        result = []
        for r in rows:
            msgs = [dict(role=m[0], content=m[1], thinking=m[2], created_at=str(m[3]) if m[3] else None, model=m[4])
                   for m in conn.execute("SELECT role, content, thinking, created_at, model FROM messages WHERE conversation_id = ? ORDER BY created_at", [r[0]]).fetchall()]
            tcs = [dict(tool=t[0], input=json.loads(t[1]), output=json.loads(t[2]), status=t[3])
                  for t in conn.execute("SELECT tool_name, input, output, status FROM tool_calls tc JOIN messages m ON tc.message_id = m.id WHERE m.conversation_id = ?", [r[0]]).fetchall()]
            edits = [dict(file=e[0], type=e[1], content=e[2])
                    for e in conn.execute("SELECT fe.file_path, fe.edit_type, fe.content FROM file_edits fe JOIN messages m ON fe.message_id = m.id WHERE m.conversation_id = ?", [r[0]]).fetchall()]
            result.append(dict(id=r[0], source=r[1], title=r[2], created_at=str(r[3]) if r[3] else None, updated_at=str(r[4]) if r[4] else None,
                              model=r[5], cwd=r[6], git_branch=r[7], project_id=r[8], messages=msgs, tool_calls=tcs, file_edits=edits))
        output.write_text(json.dumps(result, indent=2))
    else:
        conn.execute(f"COPY (SELECT c.id, c.source, c.title, c.cwd, m.role, m.content, m.created_at FROM conversations c JOIN messages m ON c.id = m.conversation_id {where} ORDER BY c.created_at, m.created_at) TO '{output}' (HEADER)")
    conn.close(); typer.echo(f"Exported to {output}")

@app.command()
def sync(watch: bool = typer.Option(False, "-w"), interval: int = typer.Option(300, "-i"), claude_code: bool = True, codex: bool = True, verbose: bool = typer.Option(False, "-v", "--verbose")):
    conn = get_db(); init_schema(conn)
    state, dirty = load_state(), False
    local, web, imports = state.setdefault("local", {}), state.setdefault("web", {}), state.setdefault("imports", {})
    def set_state(section, key, val):
        nonlocal dirty
        if state.setdefault(section, {}).get(key) != val: state[section][key] = val; dirty = True
    def plan_local(name, path, parser):
        if not path.exists(): return None
        if name in ("codex", "claude-code"):
            files = [p for p in path.rglob("*.jsonl")]
            prev, mt = local.get(name, {}).get("files", {}), {str(p): p.stat().st_mtime for p in files}
            if not (chg := [p for p in files if mt.get(str(p), 0) > prev.get(str(p), 0)]): return None
            return dict(name=name, label=name.replace("-", " ").title(), source=name, func=lambda p=path, fs=chg: parser(p, fs), state=("local", name, {"files": mt}))
        mtime = latest_mtime(path)
        if mtime <= local.get(name, {}).get("mtime", 0): return None
        return dict(name=name, label=name.replace("-", " ").title(), source=name, func=lambda p=path: parser(p), state=("local", name, {"mtime": mtime}))
    def probe_chatgpt(browser):
        hosts = [("https://chatgpt.com", ["chatgpt.com"]),
                 ("https://chat.openai.com", ["chat.openai.com", "openai.com"])]
        errors = []
        for base, domains in hosts:
            cookies = get_cookies_any(domains, browser)
            if not cookies: errors.append(f"{'/'.join(domains)}: no cookies"); continue
            ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
                  if browser == "safari" else
                  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
            headers = {"Origin": base, "Referer": f"{base}/", "User-Agent": ua, "Accept": "application/json",
                       "Accept-Language": "en-US,en;q=0.9", "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors",
                       "Sec-Fetch-Dest": "empty"}
            try:
                session = fetch_json(f"{base}/api/auth/session", cookies, headers)
                if not session or not session.get("user"): raise ValueError("not authenticated")
                if token := session.get("accessToken"): headers["Authorization"] = f"Bearer {token}"
                items = fetch_json(f"{base}/backend-api/conversations?offset=0&limit=1", cookies, headers)["items"]
                if not items: return ""
                item = items[0]
                return f"{item['id']}:{item.get('update_time')}"
            except Exception as e:
                errors.append(f"{'/'.join(domains)}: {e}")
        raise ValueError(f"ChatGPT request failed in {browser}: " + " | ".join(errors)) if errors else ValueError("ChatGPT request failed")
    def probe_claude(browser):
        cookies = get_cookies("claude.ai", browser)
        if not cookies: raise ValueError(f"No Claude cookies found in {browser}")
        headers = {"Origin": "https://claude.ai", "Referer": "https://claude.ai/",
                   "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
                   "Accept": "application/json", "Accept-Language": "en-US,en;q=0.9",
                   "anthropic-client-sha": "unknown", "anthropic-client-version": "unknown"}
        orgs = fetch_json("https://claude.ai/api/organizations", cookies, headers)
        org_id = orgs[0]["uuid"] if orgs else None
        if not org_id: raise ValueError("Could not get Claude org ID")
        items = fetch_json(f"https://claude.ai/api/organizations/{org_id}/chat_conversations", cookies, headers)
        if not items: return None
        item = items[0]
        return f"{item['uuid']}:{item.get('updated_at') or item.get('created_at')}"
    def plan_web(name, fetcher, probe):
        pref = web.get(name, {})
        order = [pref.get("browser")] + [b for b in ("safari", "chrome") if b != pref.get("browser")]
        errors = []
        for b in [x for x in order if x]:
            try:
                head = probe(b); lu = head.split(":", 1)[1] if (name == "claude" and head and ":" in head) else None
                if head is not None and head == pref.get("head"):
                    set_state("web", name, {"browser": b, "head": head, **({"last_updated": lu} if lu else {})}); return None
                last = pref.get("last_updated")
                since = ts_from_iso(last) if (name == "claude" and last) else None
                st = {"browser": b, "head": head, **({"last_updated": lu} if lu else {})}
                func = (lambda b=b, since=since: fetcher(b, since=since)) if name == "claude" else (lambda b=b: fetcher(b))
                return dict(name=name, label=name.title(), source=name, func=func, state=("web", name, st))
            except Exception as e:
                errors.append(f"{b}: {e}")
        if errors: typer.echo(f"{name} sync failed: " + " | ".join(errors))
        return None
    def plan_import(path: Path):
        if not path.exists(): return None
        mtime = latest_mtime(path) if path.is_dir() else path.stat().st_mtime
        if mtime <= imports.get(str(path), {}).get("mtime", 0): return None
        return dict(name=f"import:{path}", label=f"import:{path}", func=lambda p=path: parse_source(p), state=("imports", str(path), {"mtime": mtime}))
    def do_sync():
        nonlocal dirty
        t0 = time.perf_counter(); dirty, total, changed, jobs, newc, updc = False, [0]*5, False, [], 0, 0
        cur = counts_by_source(conn); fmt = lambda v: f"{v[0]} convs, {v[1]} msgs, {v[2]} tools, {v[3]} attachs, {v[4]} edits"
        start = lambda label, src=None: typer.echo(f"Syncing {label}" if not src else f"Syncing {label} ({fmt(cur.setdefault(src, [0]*5))})")
        if paths := [Path(p).expanduser() for p in os.environ.get("CONVOS_IMPORT_PATHS", "").split(",") if p.strip()]:
            start("imports")
            jobs += [j for p in paths if (j := plan_import(p))]
        if claude_code and (p := Path.home() / ".claude" / "projects").exists():
            start("Claude Code", "claude-code"); jobs += [j for j in [plan_local("claude-code", p, parse_claude_code)] if j]
        if codex and (p := Path.home() / ".codex").exists():
            start("Codex", "codex"); jobs += [j for j in [plan_local("codex", p, parse_codex)] if j]
        start("ChatGPT", "chatgpt"); jobs += [j for j in [plan_web("chatgpt", fetch_chatgpt, probe_chatgpt)] if j]
        start("Claude", "claude"); jobs += [j for j in [plan_web("claude", fetch_claude, probe_claude)] if j]
        verbose and typer.echo(f"Planning took {time.perf_counter()-t0:.2f}s")
        if jobs:
            with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as ex:
                futs = {ex.submit(j["func"]): {**j, "t": time.perf_counter()} for j in jobs}
                for fut in as_completed(futs):
                    j = futs[fut]
                    try: r = fut.result()
                    except Exception as e: typer.echo(f"{j['name']} failed: {e}"); continue
                    c, m, t, a, e, n, u = upsert(conn, r)
                    total = [total[i]+v for i, v in enumerate([c, m, t, a, e])]
                    newc, updc = newc+n, updc+u
                    changed |= any([c, m, t, a, e])
                    if st := j.get("state"): set_state(*st)
                    if src := j.get("source"): typer.echo(f"Updated {j['label']} ({n} new, {u} updated convs; {fmt([c, m, t, a, e])} processed){' in %.2fs' % (time.perf_counter()-j['t']) if verbose else ''}")
        if changed: rebuild_fts_index(conn)
        if dirty: save_state(state)
        verbose and typer.echo(f"Total sync time {time.perf_counter()-t0:.2f}s")
        return total, newc, updc
    if watch:
        typer.echo(f"Daemon mode (interval: {interval}s)")
        while True: r, n, u = do_sync(); typer.echo(f"[{datetime.now().isoformat()}] {n} new, {u} updated convs; {r[1]} msgs, {r[2]} tools, {r[3]} attachs, {r[4]} edits"); time.sleep(interval)
    else:
        r, n, u = do_sync(); typer.echo(f"Updated {n} new, {u} updated convs; {r[1]} msgs, {r[2]} tools, {r[3]} attachs, {r[4]} edits")
        cur = counts_by_source(conn); fmt = lambda v: f"{v[0]} convs, {v[1]} msgs, {v[2]} tools, {v[3]} attachs, {v[4]} edits"; total = [sum(v[i] for v in cur.values()) for i in range(5)]; typer.echo(f"Total: {fmt(total)}"); conn.close()

@app.command()
def stats():
    conn = get_db(read_only=True)
    if conn is None: typer.echo("Database not found. Run `convos init` or `convos sync`."); return
    if not ensure_db_ready(conn): conn.close(); return
    typer.echo(f"Conversations: {conn.execute('SELECT COUNT(*) FROM conversations').fetchone()[0]}")
    typer.echo(f"Messages: {conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]}")
    typer.echo(f"Tool calls: {conn.execute('SELECT COUNT(*) FROM tool_calls').fetchone()[0]}")
    typer.echo(f"Attachments: {conn.execute('SELECT COUNT(*) FROM attachments').fetchone()[0]}")
    typer.echo(f"File edits: {conn.execute('SELECT COUNT(*) FROM file_edits').fetchone()[0]}")
    typer.echo(f"With thinking: {conn.execute('SELECT COUNT(*) FROM messages WHERE thinking IS NOT NULL').fetchone()[0]}")
    typer.echo("\nBy source:")
    for src, cnt, msgs in conn.execute("SELECT source, COUNT(*), (SELECT COUNT(*) FROM messages m JOIN conversations c2 ON m.conversation_id=c2.id WHERE c2.source=c.source) FROM conversations c GROUP BY source").fetchall():
        typer.echo(f"  {src}: {cnt} convs, {msgs} msgs")
    typer.echo("\nTop directories:")
    for cwd, cnt in conn.execute("SELECT cwd, COUNT(*) as c FROM conversations WHERE cwd IS NOT NULL GROUP BY cwd ORDER BY c DESC LIMIT 5").fetchall():
        typer.echo(f"  {cwd}: {cnt}")
    typer.echo("\nTop tools:")
    for name, cnt in conn.execute("SELECT tool_name, COUNT(*) as c FROM tool_calls GROUP BY tool_name ORDER BY c DESC LIMIT 10").fetchall():
        typer.echo(f"  {name}: {cnt}")
    typer.echo("\nMost edited files:")
    for fp, cnt in conn.execute("SELECT file_path, COUNT(*) as c FROM file_edits GROUP BY file_path ORDER BY c DESC LIMIT 5").fetchall():
        typer.echo(f"  {fp}: {cnt}")
    conn.close()

if __name__ == "__main__": app()
