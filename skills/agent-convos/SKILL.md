---
name: agent-convos
description: Sync and search the local convo DB to update or retrieve conversation context.
---

# Agent Convos

Sync:

```bash
convos sync                 # fetch/import all sources (no-op when nothing changed)
convos embed                # backfill hybrid embeddings without a web sync
```

Retrieve. There are only three retrieval commands; everything else (listing,
reading a conversation, filtering, counts, file history) is a `sql` query over
the schema below:

```bash
convos search "exact terms" -n 8 -c 160 -f jsonl         # BM25, fast
convos query "natural language question" -n 8 -f jsonl   # hybrid (semantic), slower, loads local models
convos sql "SELECT ..." -f jsonl                          # read-only DuckDB SQL, the general tool
```

Output: add `-f jsonl` (stream, one JSON object per line) or `-f json` (array);
default is human text. Prefer `jsonl` when parsing programmatically.

Schema (write `convos sql` against these tables):

- `conversations(id, source, title, created_at, updated_at, model, cwd, git_branch, project_id, metadata JSON)`
- `messages(id, conversation_id, role, content, thinking, created_at, model, metadata JSON, embedding FLOAT[768])`
- `tool_calls(id, message_id, tool_name, input JSON, output JSON, status, duration_ms, created_at)`
- `attachments(id, message_id, filename, mime_type, size, path, url, created_at)`
- `file_edits(id, message_id, file_path, edit_type, content, created_at)`
- Full-text: `fts_main_messages.match_bm25(id, 'terms')` -> score, `NULL` when no match; indexed over `content`+`thinking`.

Common `sql` recipes:

```bash
# read one conversation in order (ids from search/query are prefixes)
convos sql "SELECT role, content FROM messages WHERE conversation_id LIKE 'abc%' ORDER BY created_at" -f jsonl
# recent conversations for a source
convos sql "SELECT id, title, created_at FROM conversations WHERE source='claude' ORDER BY created_at DESC LIMIT 20" -f json
# which conversation/prompt touched a file
convos sql "SELECT fe.created_at, fe.edit_type, c.id, c.title FROM file_edits fe JOIN messages m ON fe.message_id=m.id JOIN conversations c ON m.conversation_id=c.id WHERE fe.file_path LIKE '%routes.js' ORDER BY fe.created_at" -f jsonl
# counts by source
convos sql "SELECT source, COUNT(*) FROM conversations GROUP BY source" -f json
```

Behavior:

- Pick the command: `search` for known keywords/exact strings; `query` for paraphrased/conceptual lookups (slower, needs models); `sql` for listing, reading a conversation, filters, joins, counts -- anything structured.
- `search`/`query` accept `-s` source, `-d` days, `-r` role, `-n` limit, `-c` context; for any richer filter, use `sql`.
- Optimize relevance and tokens: keep `-n` <= 8 and `-c` <= 200 unless the user wants more.
- `sql` runs on a read-only connection, so writes fail by construction; it is safe for arbitrary `SELECT`s.
- Use `sync` as the only fetch/import update command; use `embed` only to backfill hybrid embeddings.
- Expect a fast no-op when nothing changed. Report specific errors (cookies, auth, permissions) if a fetch fails.
- Use `CONVOS_IMPORT_PATHS` for export paths (comma-separated) consumed by `sync`.
- If `convos` is not on PATH, use the repo wrapper: `bin/convos`.
- Use shell commands only; do not use MCP resources for this skill.

Storage:

- DB file: `<root>/data/convos.db`
- Sync state: `<root>/data/sync_state.json`
- Default root: `~/.convos` (override with `CONVOS_PROJECT_ROOT`)
```