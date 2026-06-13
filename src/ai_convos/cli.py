#!/usr/bin/env python3
import json, time, zipfile, hashlib, struct, sqlite3, subprocess, ssl, urllib.request, re, os, sysconfig, site, math
from importlib.metadata import entry_points
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
    return Path.home() / ".convos"
PROJECT_ROOT = find_root()
DATA_DIR, DB_PATH = PROJECT_ROOT / "data", PROJECT_ROOT / "data" / "convos.db"
STATE_PATH = DATA_DIR / "sync_state.json"

# ---- db helpers ----
def get_db(read_only: bool = False):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if read_only and not DB_PATH.exists():
        return None
    for i in range(30 if read_only else 1):
        try: return duckdb.connect(str(DB_PATH), read_only=read_only)
        except Exception as e:
            if "Conflicting lock is held" not in str(e): raise
            if read_only and i < 29: time.sleep(1); continue
            raise ValueError("Database is locked by another convos process. Try again after `convos sync` finishes.") from e

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
        thinking VARCHAR, created_at TIMESTAMP, model VARCHAR, metadata JSON, embedding FLOAT[768])""")
    if not conn.execute("SELECT 1 FROM information_schema.columns WHERE table_name='messages' AND column_name='embedding'").fetchone():
        conn.execute("ALTER TABLE messages ADD COLUMN embedding FLOAT[768]")
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
    conn.execute("ALTER TABLE file_edits ADD COLUMN IF NOT EXISTS old_content TEXT")
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

def read_chrome_cookies(domain: str, profile: str | None = None) -> dict[str, str]:
    profile = profile or os.environ.get("CONVOS_CHROME_PROFILE", "Default")
    db_path = Path.home() / "Library/Application Support/Google/Chrome" / profile / "Cookies"
    if not db_path.exists(): db_path = Path.home() / "Library/Application Support/Google/Chrome" / profile / "Network/Cookies"
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
            cookies[name] = (v[32:] if not (v := decrypted[:-decrypted[-1]])[:32].isascii() else v).decode('utf-8', errors='ignore')
    conn.close()
    return cookies

def get_cookies(domain: str, browser: str = "safari", profile: str | None = None) -> dict[str, str]:
    return read_safari_cookies(domain) if browser == "safari" else read_chrome_cookies(domain, profile=profile)

def get_cookies_any(domains: list[str], browser: str = "safari", profile: str | None = None) -> dict[str, str]:
    cookies = {}
    for d in domains: cookies.update(get_cookies(d, browser, profile=profile))
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

def chrome_cookie_domains(profile: str | None = None):
    profile = profile or os.environ.get("CONVOS_CHROME_PROFILE", "Default")
    db_path = Path.home() / "Library/Application Support/Google/Chrome" / profile / "Cookies"
    if not db_path.exists(): db_path = Path.home() / "Library/Application Support/Google/Chrome" / profile / "Network/Cookies"
    if not db_path.exists(): return set()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro&nolock=1", uri=True)
    domains = {r[0] for r in conn.execute("SELECT DISTINCT host_key FROM cookies")}
    conn.close()
    return domains

def chrome_profiles() -> list[str]:
    base = Path.home() / "Library/Application Support/Google/Chrome"
    if not base.exists(): return []
    return [p.name for p in base.iterdir() if p.is_dir() and ((p / "Cookies").exists() or (p / "Network/Cookies").exists())]

def chatgpt_profiles(browser: str) -> list[str | None]:
    if browser != "chrome": return [None]
    if prof := os.environ.get("CONVOS_CHROME_PROFILE"): return [prof]
    return chrome_profiles() or [None]

def chatgpt_cookie_base(browser: str, hosts: list[tuple[str, list[str]]], profile: str | None):
    for url, domains in hosts:
        if c := get_cookies_any(domains, browser, profile=profile): return c, url
    raise ValueError(f"No ChatGPT cookies found in {browser}" + (f" profile {profile}" if profile else ""))

def chatgpt_headers(cookies, base, ua, debug_profile: str | None = None):
    headers = {"Origin": base, "Referer": f"{base}/", "User-Agent": ua, "Accept": "application/json",
               "Accept-Language": "en-US,en;q=0.9", "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors",
               "Sec-Fetch-Dest": "empty"}
    try:
        session = fetch_json(f"{base}/api/auth/session", cookies, headers)
        if token := session.get("accessToken"): headers["Authorization"] = f"Bearer {token}"
        if aid := session.get("account", {}).get("id"): headers["ChatGPT-Account-ID"] = aid
        if debug_profile: typer.echo(f"  chatgpt chrome profile={debug_profile} user={session.get('user', {}).get('email')}", flush=True)
    except Exception:
        pass
    return headers

def merge_results(dst: "ParseResult", src: "ParseResult"):
    dst.convs += src.convs; dst.msgs += src.msgs; dst.tools += src.tools; dst.attachs += src.attachs

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

def log_parse_error(context: str, err: Exception):
    if not os.environ.get("CONVOS_PARSE_LOG"):
        return
    typer.echo(f"  parse error ({context}): {type(err).__name__}: {err}", err=True)

def safe_parse(context: str, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        log_parse_error(context, e)
        return None

def parse_source(path: Path, source: Optional[str] = None) -> ParseResult:
    parsers = {"chatgpt": parse_chatgpt, "claude": parse_claude, "claude-code": parse_claude_code, "codex": parse_codex}
    src = source or detect_source(path)
    if src not in parsers: raise ValueError(f"Unknown source: {src}")
    return parsers[src](path)

# ---- web fetchers ----
def fetch_chatgpt(browser: str = "safari", limit: int = 0) -> ParseResult:
    hosts = [("https://chatgpt.com", ["chatgpt.com"]),
             ("https://chat.openai.com", ["chat.openai.com", "openai.com"])]
    debug = os.environ.get("CONVOS_CHATGPT_DEBUG")
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
          if browser == "safari" else
          "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    def fetch_with_profile(profile: str | None) -> ParseResult:
        cookies, base = chatgpt_cookie_base(browser, hosts, profile)
        headers = chatgpt_headers(cookies, base, ua, debug_profile=profile if debug else None)
        r = ParseResult()
        def parse_item_raw(item):
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
        def parse_item(item): return safe_parse(f"chatgpt web conv {item.get('id') if isinstance(item, dict) else 'unknown'}", parse_item_raw, item)
        offset, total, fetched, seen = 0, None, 0, set()
        while True:
            data = fetch_json(f"{base}/backend-api/conversations?offset={offset}&limit=100", cookies, headers, timeout=60)
            total = total if total is not None else data.get("total")
            items, keys = data.get("items", []), ",".join(data.keys())
            if debug: print(f"  chatgpt page offset={offset} items={len(items)} total={total} keys={keys}", flush=True)
            if not items: break
            page = [it for it in items if it["id"] not in seen][: (limit - fetched) if limit > 0 else len(items)]
            for it in page: seen.add(it["id"])
            with ThreadPoolExecutor(max_workers=min(4, len(page))) as ex:
                results = [x for x in ex.map(parse_item, page) if x] if page else []
            r.convs += [x["conv"] for x in results]; r.msgs += [m for x in results for m in x["msgs"]]
            r.tools += [t for x in results for t in x["tools"]]; r.attachs += [a for x in results for a in x["attachs"]]
            fetched += len(results)
            offset += len(items)
            if not page: break
            if limit > 0 and fetched >= limit: break
            if total and fetched > total: total = None
            typer.echo(f"  fetched {fetched}{'/' + str(total) if total else ''}")
        return r
    out, errs = ParseResult(), []
    for profile in chatgpt_profiles(browser):
        try: merge_results(out, fetch_with_profile(profile))
        except Exception as e: errs.append(f"{profile or 'default'}: {e}")
    if errs and not out.convs: raise ValueError("ChatGPT fetch failed -- " + " | ".join(errs))
    return out

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
    def parse_item_raw(item):
        nonlocal fetched
        updated = ts_from_iso(item.get("updated_at") or item.get("created_at"))
        if since and updated and updated <= since:
            return False
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
        return True
    for idx, item in enumerate(items):
        cid = item.get("uuid") if isinstance(item, dict) else "unknown"
        did_fetch = safe_parse(f"claude web conv {cid}", parse_item_raw, item)
        if did_fetch is False:
            if idx == len(items)-1 or (idx+1) % step == 0: print(f"  claude fetched {fetched}/{len(items)}", flush=True)
            continue
        if idx == len(items)-1 or (idx+1) % step == 0: print(f"  claude fetched {fetched}/{len(items)}", flush=True)
    return r

# ---- file parsers ----
def parse_chatgpt(path: Path) -> ParseResult:
    data = json.load(zipfile.ZipFile(path).open('conversations.json')) if path.suffix == ".zip" else json.loads(path.read_text())
    r = ParseResult()
    def parse_conv(c):
        cid, gizmo = gen_id("chatgpt", c.get("id", "")), c.get("gizmo_id")
        conv = dict(id=cid, source="chatgpt", title=c.get("title"), created_at=ts_from_epoch(c.get("create_time")),
                    updated_at=ts_from_epoch(c.get("update_time")), model=c.get("default_model_slug"), cwd=None, git_branch=None,
                    project_id=gizmo, metadata=json.dumps({"gizmo_id": gizmo}) if gizmo else "{}")
        msgs, tools, attachs = [], [], []
        for nid, node in c.get("mapping", {}).items():
            if not (msg := node.get("message")): continue
            mid, role, meta = gen_id("chatgpt", f"{cid}:{nid}"), msg.get("author", {}).get("role", "unknown"), msg.get("metadata", {})
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
        return dict(conv=conv, msgs=msgs, tools=tools, attachs=attachs)
    for idx, c in enumerate(data):
        cid = c.get("id") if isinstance(c, dict) else idx
        if p := safe_parse(f"chatgpt export conv {cid}", parse_conv, c):
            r.convs.append(p["conv"]); r.msgs += p["msgs"]; r.tools += p["tools"]; r.attachs += p["attachs"]
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
    parsed = [p for idx, c in enumerate(data)
              if (p := safe_parse(f"claude export conv {c.get('uuid') if isinstance(c, dict) else idx}", parse_conv, c))]
    return ParseResult(convs=[p["conv"] for p in parsed], msgs=[m for p in parsed for m in p["msgs"]],
                      attachs=[a for p in parsed for a in p["attachs"]])

def load_jsonl(path: Path) -> list[dict]:
    out = []
    for i, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip(): continue
        try:
            out.append(json.loads(line))
        except Exception as e:
            log_parse_error(f"jsonl {path} line {i}", e)
    return out

def parse_claude_code_session(jsonl: Path) -> dict:
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
                    edit_type=t["name"].lower(), content=t["input"].get("content") or t["input"].get("new_string", ""), created_at=ts,
                    old_content=t["input"].get("old_string"))
               for j, t in enumerate(c["tools"]) if t.get("name") in ("Write", "Edit", "MultiEdit") and t.get("input", {}).get("file_path")]

    msgs = [make_msg(idx, i, e) for idx, (i, e) in enumerate(msg_events) if (c := extract_content(e["message"].get("content", "")))["text"] or c["tools"]]  # keep tool-only turns: tools/edits reference them
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
    sessions = [s for jsonl in (files or projects_dir.rglob("*.jsonl"))
                if (s := safe_parse(f"claude-code session {jsonl}", parse_claude_code_session, jsonl))]
    return ParseResult(
        convs=[s["conv"] for s in sessions], msgs=[m for s in sessions for m in s["msgs"]],
        tools=[t for s in sessions for t in s["tools"]], edits=[e for s in sessions for e in s["edits"]])

def parse_codex_session(jsonl: Path) -> dict | None:
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

    mitems = [(i, p, t) for i, p in items if p.get("type") == "message" and p.get("role") not in ("developer", "system") and (t := extract_msg_text(p))]
    msgs = [dict(id=gen_id(src, f"{cid}:{i}"), conversation_id=cid, role=p["role"], content=t.strip(),
                thinking=None, created_at=timestamps[i] if i < len(timestamps) else None, model=None, metadata="{}")
           for i, p, t in mitems]
    if not msgs: return None
    anchor = lambda k: gen_id(src, f"{cid}:{next((i for i, _, _ in reversed(mitems) if i <= k), mitems[0][0])}")  # function_call items are not messages; attach to nearest preceding one

    tools = [dict(id=gen_id(src, f"tool:{cid}:{i}"), message_id=anchor(i), tool_name=p["name"],
                 input=json.dumps(args), output="{}", status="pending", duration_ms=None,
                 created_at=timestamps[i] if i < len(timestamps) else None)
            for i, p in items if p.get("type") == "function_call" and (args := norm_args(p))] + \
           [dict(id=gen_id(src, f"toolout:{cid}:{i}"), message_id=anchor(i), tool_name=p.get("call_id"),
                 input="{}", output=json.dumps(p.get("output", "")), status="complete", duration_ms=None,
                 created_at=timestamps[i] if i < len(timestamps) else None)
            for i, p in items if p.get("type") == "function_call_output"]

    def patch_edits(args):
        """File edits from shell commands, exact or skipped: apply_patch hunks (context+minus -> context+plus,
        replays like Edit old/new), heredoc writes (full content), plain redirects (file exact, content unknown)."""
        cmd = args.get("cmd") or args.get("command") or ""
        cmd = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        root = args.get("workdir") or meta.get("cwd") or ""
        if "*** Begin Patch" not in cmd:
            head = cmd.split("\n", 1)[0]
            if (hm := re.search(r"<<-?\s*'?(\w+)'?", head)) and (tm := re.search(r"(?:(?<![0-9&])>{1,2}\s*|\btee\s+(?:-a\s+)?)([^\s;|&<>'\"]+)", head)) \
               and (body := re.search(rf"\n(.*)\n{hm.group(1)}\s*$", cmd, re.S)) and tm.group(1) != "/dev/null":
                return [(os.path.join(root, tm.group(1)), "write", body.group(1), None)]
            if (tm := re.search(r"(?<![0-9&])>{1,2}\s*([^\s;|&<>'\"]+\.[A-Za-z]{1,5})\b", head)) and tm.group(1) != "/dev/null":
                return [(os.path.join(root, tm.group(1)), "shell", cmd, None)]
            return []
        out, path, op, old, new = [], None, None, [], []
        def flush():
            if path and (old or new or op != "edit"): out.append((path, op, "\n".join(new), "\n".join(old) or None))
            old.clear(); new.clear()
        for ln in cmd.split("*** Begin Patch", 1)[1].split("*** End Patch", 1)[0].splitlines():
            if m := re.match(r"\*\*\* (Update|Add|Delete) File: (.+)", ln):
                flush(); op, path = {"Update": "edit", "Add": "write", "Delete": "delete"}[m.group(1)], os.path.join(root, m.group(2).strip())
            elif ln.startswith("@@"): flush()
            elif ln.startswith("***"): pass  # e.g. *** End of File
            elif ln.startswith("+"): new.append(ln[1:])
            elif ln.startswith("-"): old.append(ln[1:])
            elif path: old.append(ln[1:] if ln.startswith(" ") else ln); new.append(ln[1:] if ln.startswith(" ") else ln)
        flush(); return out

    edits = [dict(id=gen_id(src, f"edit:{cid}:{i}:{j}"), message_id=anchor(i), file_path=fp, edit_type=op,
                 content=c, created_at=timestamps[i] if i < len(timestamps) else None, old_content=o)
            for i, p in items if p.get("type") == "function_call" and p.get("name") in ("exec_command", "shell_command", "shell")
            and (args := norm_args(p)) for j, (fp, op, c, o) in enumerate(patch_edits(args))]

    return {
        "conv": dict(id=cid, source=src, title=meta.get("cwd") or jsonl.stem,
                    created_at=timestamps[0] if timestamps else None, updated_at=timestamps[-1] if timestamps else None,
                    model=meta.get("model_provider", "openai"), cwd=meta.get("cwd"), git_branch=None, project_id=None,
                    metadata=json.dumps({"cli_version": meta.get("cli_version"), "session_id": jsonl.stem})),
        "msgs": msgs, "tools": tools, "edits": edits}

def parse_codex(codex_dir: Path, files: list[Path] | None = None) -> ParseResult:
    sessions_dir = codex_dir / "sessions"
    if not sessions_dir.exists(): return ParseResult()
    sessions = [s for jsonl in (files or sessions_dir.rglob("*.jsonl"))
                if (s := safe_parse(f"codex session {jsonl}", parse_codex_session, jsonl))]
    return ParseResult(
        convs=[s["conv"] for s in sessions], msgs=[m for s in sessions for m in s["msgs"]],
        tools=[t for s in sessions for t in s["tools"]], edits=[e for s in sessions for e in s["edits"]])

_MSG_UPS = "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,NULL) ON CONFLICT(id) DO UPDATE SET conversation_id=excluded.conversation_id, role=excluded.role, content=excluded.content, thinking=excluded.thinking, created_at=excluded.created_at, model=excluded.model, metadata=excluded.metadata, embedding=CASE WHEN messages.content IS DISTINCT FROM excluded.content THEN NULL ELSE messages.embedding END"

def upsert(conn, r: ParseResult):
    cids, mids = [c["id"] for c in r.convs], [m["id"] for m in r.msgs]
    cur = conn.execute
    existing = set(cur(f"SELECT id FROM conversations WHERE id IN ({','.join(['?']*len(cids))})", cids).fetchall()) if cids else set()
    existing_msgs = set(cur(f"SELECT id FROM messages WHERE id IN ({','.join(['?']*len(mids))})", mids).fetchall()) if mids else set()
    new_convs = set(cids) - {x[0] for x in existing}
    new_msgs = set(mids) - {x[0] for x in existing_msgs}
    updated = {m["conversation_id"] for m in r.msgs if m["id"] in new_msgs} - new_convs
    for c in r.convs: conn.execute("INSERT OR REPLACE INTO conversations VALUES (?,?,?,?,?,?,?,?,?,?)", list(c.values()))
    for m in r.msgs: conn.execute(_MSG_UPS, list(m.values()))
    for t in r.tools: conn.execute("INSERT OR REPLACE INTO tool_calls VALUES (?,?,?,?,?,?,?,?)", list(t.values()))
    for a in r.attachs: conn.execute("INSERT OR REPLACE INTO attachments VALUES (?,?,?,?,?,?,?,?)", list(a.values()))
    for a in r.artifacts: conn.execute("INSERT OR REPLACE INTO artifacts VALUES (?,?,?,?,?,?,?,?)", list(a.values()))
    for e in r.edits: conn.execute("INSERT OR REPLACE INTO file_edits VALUES (?,?,?,?,?,?,?)", list(e.values()))
    return len(r.convs), len(r.msgs), len(r.tools), len(r.attachs), len(r.edits), len(new_convs), len(updated)

# ---- embeddings ----
_MODELS, _MCFG, _LLAMA_LOG = {}, {"emb": dict(repo_id="ggml-org/embeddinggemma-300m-qat-q8_0-GGUF", filename="embeddinggemma-300m-qat-Q8_0.gguf", embedding=True, n_ctx=16384, n_batch=2048, n_ubatch=2048, n_seq_max=8, n_gpu_layers=-1), "rr": dict(repo_id="ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF", filename="qwen3-reranker-0.6b-q8_0.gguf", n_ctx=4096, logits_all=True, n_gpu_layers=-1)}, None
def _llama(role: str):
    global _LLAMA_LOG
    if role not in _MODELS:
        from llama_cpp import Llama; import llama_cpp.llama_cpp as lc, warnings; warnings.filterwarnings("ignore", message="The `local_dir_use_symlinks` argument is deprecated.*", category=UserWarning)
        if _LLAMA_LOG is None: _LLAMA_LOG = lc.llama_log_callback(lambda *_: None); lc.llama_log_set(_LLAMA_LOG, None)
        cfg = _MCFG[role].copy(); nseq = cfg.pop("n_seq_max", 0)
        if nseq:
            orig = lc.llama_context_default_params
            lc.llama_context_default_params = lambda o=orig, n=nseq: (setattr(p := o(), "n_seq_max", n) or p)
        try: _MODELS[role] = Llama.from_pretrained(**cfg, verbose=False)
        finally:
            if nseq: lc.llama_context_default_params = orig
    return _MODELS[role]
def embed_texts(ss: list[str], doc: bool = False) -> list[list[float]]:
    p = "task: search result | document: " if doc else "task: search result | query: "
    return [d["embedding"] for d in _llama("emb").create_embedding([p + (s or "")[:1600] for s in ss])["data"]]
def embed_text(s: str, doc: bool = False) -> list[float]:
    return embed_texts([s], doc)[0]
RR_PROMPT = '<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n<Instruct>: Given a search query, find conversation messages relevant to it.\n<Query>: {q}\n<Document>: {d}<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n'
def rerank(query: str, docs: list[str]) -> list[float]:
    rr, qq = _llama("rr"), query[:512]
    return [(lambda l: (math.exp(l.get("yes",-100))/(math.exp(l.get("yes",-100))+math.exp(l.get("no",-100)))) if math.exp(l.get("yes",-100))+math.exp(l.get("no",-100))>0 else 0.0)(rr.create_completion(RR_PROMPT.format(q=qq, d=(d or "")[:3000]), max_tokens=1, temperature=0.0, logprobs=10)["choices"][0]["logprobs"]["top_logprobs"][0]) for d in docs]
def embed_pending(conn, batch: int = 32):
    Q = "FROM messages WHERE embedding IS NULL AND content IS NOT NULL AND content != ''"
    if not (n := conn.execute(f"SELECT COUNT(*) {Q}").fetchone()[0]): return
    typer.echo(f"Embedding {n} messages..."); done = 0
    while rows := conn.execute(f"SELECT id, content {Q} ORDER BY LEAST(length(content),1600) LIMIT ?", [batch]).fetchall():
        for ch in [rows[i:i+_MCFG["emb"]["n_seq_max"]] for i in range(0, len(rows), _MCFG["emb"]["n_seq_max"])]:
            conn.executemany("UPDATE messages SET embedding=? WHERE id=?", [(e, mid) for (mid, _), e in zip(ch, embed_texts([c for _, c in ch], doc=True))])
        done += len(rows); typer.echo(f"  {done}/{n}\r", nl=False)
    typer.echo()

# ---- commands ----
def _ro():
    try: c = get_db(read_only=True)
    except ValueError as e: typer.echo(str(e), err=True); return None
    if c is None: typer.echo("Database not found. Run `convos init` or `convos sync`."); return None
    if not ensure_db_ready(c): c.close(); return None
    return c
def _hybrid_ro():
    if (c := _ro()) is None: return None
    if c.execute("SELECT 1 FROM information_schema.columns WHERE table_name='messages' AND column_name='embedding'").fetchone(): return c
    c.close(); c = get_db(); init_schema(c); c.close(); return _ro()
def _filt(source, days, role):
    w, p = [], []
    if source: w.append("c.source = ?"); p.append(source)
    if days: w.append("m.created_at > ?"); p.append(datetime.now() - timedelta(days=days))
    if role: w.append("m.role = ?"); p.append(role)
    return w, p
def _fmt_hit(content, ts, role, title, src, cid, cwd, q, ctx, meta):
    p = (content or "")[:ctx] + ("..." if content and len(content) > ctx else "")
    for w in q.split(): p = re.sub(f"({re.escape(w)})", r"\033[1;33m\1\033[0m", p, flags=re.I)
    typer.echo(f"\n{'='*60}\n[{src}] {title or 'Untitled'}{f' @ {cwd}' if cwd else ''} ({cid[:8]})\n{role} @ {ts or '?'} ({meta})\n{'-'*40}\n{p}")

def emit(data, fmt):
    if fmt == "jsonl" and isinstance(data, list): [typer.echo(json.dumps(r, default=str)) for r in data]
    else: typer.echo(json.dumps(data, default=str))

@app.command()
def init():
    conn = get_db(); init_schema(conn); rebuild_fts_index(conn); conn.close()
    install_skills()
    typer.echo(f"Database initialized at {DB_PATH}")

@app.command()
def search(query: str, source: Optional[str] = typer.Option(None, "-s"), days: Optional[int] = typer.Option(None, "-d"), role: Optional[str] = typer.Option(None, "-r"), thinking: bool = typer.Option(False, "--thinking", "-t"), limit: int = typer.Option(20, "-n"), context: int = typer.Option(300, "-c"), fmt: str = typer.Option("text", "-f", "--format")):
    if (conn := _ro()) is None: return
    try: load_fts(conn)
    except ValueError as e: conn.close(); typer.echo(str(e)); return
    w, p = _filt(source, days, role)
    results = conn.execute(f"""SELECT m.content, m.thinking, m.role, m.created_at, fts_main_messages.match_bm25(m.id, ?) as score, c.title, c.source, c.id, c.cwd
        FROM messages m JOIN conversations c ON m.conversation_id = c.id WHERE score IS NOT NULL{' AND ' + ' AND '.join(w) if w else ''} ORDER BY score DESC LIMIT ?""", [query] + p + [limit]).fetchall()
    conn.close()
    if fmt != "text": emit([dict(role=r, content=content, thinking=think, created_at=ts, score=score, title=title, source=src, conversation_id=cid, cwd=cwd) for content, think, r, ts, score, title, src, cid, cwd in results], fmt); return
    if not results: typer.echo("No results"); return
    for content, think, r, ts, score, title, src, cid, cwd in results:
        _fmt_hit(content, ts, r, title, src, cid, cwd, query, context, f"score: {score:.2f}")
        if thinking and think: typer.echo(f"\n[THINKING]\n{think[:500]}{'...' if len(think)>500 else ''}")
    typer.echo(f"\n{len(results)} results")

@app.command("query")
def query_cmd(q: str, source: Optional[str] = typer.Option(None, "-s"), days: Optional[int] = typer.Option(None, "-d"), role: Optional[str] = typer.Option(None, "-r"), limit: int = typer.Option(10, "-n"), context: int = typer.Option(300, "-c"), fmt: str = typer.Option("text", "-f", "--format")):
    if (conn := _hybrid_ro()) is None: return
    if not conn.execute("SELECT COUNT(*) FROM messages WHERE embedding IS NOT NULL").fetchone()[0]:
        conn.close(); typer.echo("No embeddings yet. Run `pip install ai-convos-db[hybrid]` and `convos embed`, or use `convos search` for BM25 only.", err=True); return
    try: load_fts(conn)
    except ValueError as e: conn.close(); typer.echo(str(e)); return
    try: qv = embed_text(q, doc=False)
    except Exception as e: conn.close(); typer.echo(f"Hybrid embedding failed: {e}", err=True); return
    w, p = _filt(source, days, role)
    rows = conn.execute(f"""WITH qe AS (SELECT ?::FLOAT[768] AS v),
        fts AS (SELECT id, ROW_NUMBER() OVER (ORDER BY score DESC) AS r FROM (SELECT id, fts_main_messages.match_bm25(id, ?) AS score FROM messages) s WHERE score IS NOT NULL LIMIT 50),
        vec AS (SELECT m.id, ROW_NUMBER() OVER (ORDER BY array_cosine_similarity(m.embedding, qe.v) DESC) AS r FROM messages m, qe WHERE m.embedding IS NOT NULL LIMIT 50),
        fused AS (SELECT id, SUM(1.0/(60+r)) AS rrf FROM (SELECT id, r FROM fts UNION ALL SELECT id, r FROM vec) GROUP BY id)
        SELECT m.role, m.content, m.created_at, c.title, c.source, c.id, c.cwd FROM fused JOIN messages m ON m.id = fused.id JOIN conversations c ON c.id = m.conversation_id
        WHERE 1=1{' AND ' + ' AND '.join(w) if w else ''} ORDER BY fused.rrf DESC LIMIT 30""", [qv, q] + p).fetchall()
    conn.close()
    if not rows: typer.echo("No results"); return
    try: rrs = rerank(q, [r[1] or "" for r in rows])
    except Exception as e: typer.echo(f"Hybrid rerank failed: {e}", err=True); return
    W = lambda i: (0.75, 0.25) if i < 3 else (0.6, 0.4) if i < 10 else (0.4, 0.6)
    ranked = sorted([(W(i)[0]*(1.0/(i+1)) + W(i)[1]*rr, row, rr) for i, (row, rr) in enumerate(zip(rows, rrs))], reverse=True, key=lambda x: x[0])
    if fmt != "text": emit([dict(score=score, role=r, content=content, created_at=ts, title=title, source=src, conversation_id=cid, cwd=cwd, rerank=rr) for score, (r, content, ts, title, src, cid, cwd), rr in ranked[:limit]], fmt); return
    for score, (r, content, ts, title, src, cid, cwd), rr in ranked[:limit]: _fmt_hit(content, ts, r, title, src, cid, cwd, q, context, f"score: {score:.3f}, rerank: {rr:.2f}")
    typer.echo(f"\n{min(len(ranked), limit)} results")

@app.command("embed")
def embed_cmd(batch: int = typer.Option(32, "-b")):
    conn = get_db(); init_schema(conn)
    try: embed_pending(conn, batch); typer.echo("Embeddings ready")
    except Exception as e: typer.echo(f"Embedding failed: {e}", err=True)
    conn.close()

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
    rel = Path("skills") / "agent-convos" / "SKILL.md"
    shares = [Path(p) / "share" / "ai-convos-db" for p in [sysconfig.get_paths().get("data", ""), site.getuserbase()]]
    roots = [PROJECT_ROOT, Path(__file__).resolve().parents[2], *shares]
    skill = next((r / rel for r in roots if (r / rel).exists()), roots[-1] / rel)
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
    if (conn := _ro()) is None: return
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
        profiles = chatgpt_profiles(browser)
        errors = []
        heads = []
        ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
              if browser == "safari" else
              "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        for profile in profiles:
            try:
                cookies, base = chatgpt_cookie_base(browser, hosts, profile)
                headers = chatgpt_headers(cookies, base, ua)
                items = fetch_json(f"{base}/backend-api/conversations?offset=0&limit=1", cookies, headers)["items"]
                if items: heads.append(f"{profile or 'default'}:{items[0]['id']}:{items[0].get('update_time')}")
            except Exception as e:
                errors.append(f"chatgpt.com{f'/{profile}' if profile else ''}: {e}")
        if heads: return "|".join(heads)
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
        forced = os.environ.get(f"CONVOS_{name.upper()}_BROWSER")
        order = [forced] if forced else [pref.get("browser")] + [b for b in ("safari", "chrome") if b != pref.get("browser")]
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
        if errors: typer.echo(f"{name}: no cookies found -- skipped" if all("cookies" in e.lower() for e in errors) else f"{name} sync failed: " + " | ".join(errors))
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
        try: embed_pending(conn)
        except ImportError: pass
        if dirty: save_state(state)
        verbose and typer.echo(f"Total sync time {time.perf_counter()-t0:.2f}s")
        return total, newc, updc
    if watch:
        typer.echo(f"Daemon mode (interval: {interval}s)")
        while True: r, n, u = do_sync(); typer.echo(f"[{datetime.now().isoformat()}] {n} new, {u} updated convs; {r[1]} msgs, {r[2]} tools, {r[3]} attachs, {r[4]} edits"); time.sleep(interval)
    else:
        r, n, u = do_sync(); typer.echo(f"Updated {n} new, {u} updated convs; {r[1]} msgs, {r[2]} tools, {r[3]} attachs, {r[4]} edits processed")
        fmt = lambda v: f"{v[0]} convs, {v[1]} msgs, {v[2]} tools, {v[3]} attachs, {v[4]} edits"; total = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("conversations", "messages", "tool_calls", "attachments", "file_edits")]; typer.echo(f"Total: {fmt(total)}"); conn.close()

@app.command()
def sql(query: str, fmt: str = typer.Option("text", "-f", "--format")):
    if (conn := _ro()) is None: return
    try: cur = conn.execute(query); cols = [d[0] for d in cur.description]; rows = cur.fetchall()
    except Exception as e: conn.close(); typer.echo(f"Query failed: {e}", err=True); return
    conn.close()
    if fmt != "text": emit([dict(zip(cols, r)) for r in rows], fmt); return
    typer.echo(" | ".join(cols)); [typer.echo(" | ".join("" if v is None else str(v) for v in r)) for r in rows]; typer.echo(f"\n{len(rows)} rows")

# ---- plugin seam: installed apps register subcommands (entry point group convos.commands) ----
for _ep in entry_points(group="convos.commands"):
    try: _ep.load()(app)
    except Exception as _e: typer.echo(f"plugin {_ep.name} failed: {_e}", err=True)  # a broken plugin must not kill the CLI

if __name__ == "__main__": app()
