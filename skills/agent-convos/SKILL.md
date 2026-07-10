---
name: agent-convos
description: Retrieve prior AI conversation context and keep the local archive current. Use whenever the user asks to recall, find, continue, summarize, compare, or verify information from past ChatGPT, Claude, Claude Code, or Codex conversations, including earlier plans, decisions, commands, evidence, or work sessions.
---

# Agent Convos

Retrieve with these commands:

```bash
convos query "natural language question" -n 8 -f jsonl   # conceptual/paraphrased discovery; default when wording is uncertain
convos search "exact terms" -n 8 -c 160 -f jsonl         # known terms, quotes, ids, filenames; fast BM25
convos read abc123 -n 20 -c 2000 -f jsonl                 # bounded recent context from one known conversation
convos sql "SELECT ..." -f jsonl                          # structured filters, joins, counts, and history
```

Output: add `-f jsonl` (stream, one JSON object per line) or `-f json` (array);
default is human text. Prefer `jsonl` when parsing programmatically.

Schema (write `convos sql` against these tables):

- `conversations(id, source, title, created_at, updated_at, model, cwd, git_branch, project_id, metadata JSON)`
- `messages(id, conversation_id, role, content, thinking, created_at, model, metadata JSON, embedding FLOAT[768], parent_id)`
- `tool_calls(id, message_id, tool_name, input JSON, output JSON, status, duration_ms, created_at)`
- `attachments(id, message_id, filename, mime_type, size, path, url, created_at)`
- `file_edits(id, message_id, file_path, edit_type, content, created_at, old_content)`
- Full-text: `fts_main_messages.match_bm25(id, 'terms')` -> score, `NULL` when no match; indexed over `content`+`thinking`.

Common `sql` recipes:

```bash
# recent conversations for a source
convos sql "SELECT id, title, created_at FROM conversations WHERE source='claude' ORDER BY created_at DESC LIMIT 20" -f json
# which conversation/prompt touched a file
convos sql "SELECT fe.created_at, fe.edit_type, c.id, c.title FROM file_edits fe JOIN messages m ON fe.message_id=m.id JOIN conversations c ON m.conversation_id=c.id WHERE fe.file_path LIKE '%routes.js' ORDER BY fe.created_at" -f jsonl
# counts by source
convos sql "SELECT source, COUNT(*) FROM conversations GROUP BY source" -f json
```

Behavior:

- Discover first with `query` for concepts/paraphrases or `search` for known literal text, then use `read` on the strongest conversation candidates. Use `sql` for structured questions.
- `read` resolves a unique conversation-id prefix, selects the newest `-n` messages, and returns them chronologically; raise `-n` or `-c` when more history is required.
- SQL text matching is available but usually a worse discovery path than `query`/`search`; reserve SQL primarily for known ids, fields, relations, ordering, and aggregation.
- `search`/`query` accept `-s` source, `-d` days, `-r` role, `-n` limit, `-c` context; for any richer filter, use `sql`.
- Optimize discovery relevance and tokens: keep search/query `-n` <= 8 and `-c` <= 200 unless the user wants more.
- `sql` runs on a read-only connection, so writes fail by construction; it is safe for arbitrary `SELECT`s.
- Installed coding-agent hooks make local Claude Code and Codex turns available just in time; read commands flush pending hook work automatically.
- Sync when the request needs fresh web conversations, imports, missed-hook reconciliation, or the user explicitly asks for an update.
- Use `sync` as the only fetch/import update command; use `embed` only to backfill hybrid embeddings.
- Expect a fast no-op when nothing changed. Report specific errors (cookies, auth, permissions) if a fetch fails.
- Use `CONVOS_IMPORT_PATHS` for export paths (comma-separated) consumed by `sync`.
- If `convos` is not on PATH, use the repo wrapper: `bin/convos`.
- Use shell commands only; do not use MCP resources for this skill.

Sync:

```bash
convos sync                 # fetch/import new or changed source data
convos embed                # backfill hybrid embeddings without a web sync
convos install-hooks        # install user-level Claude Code + Codex JIT ingestion
```

Storage:

- DB file: `<root>/data/convos.db`
- Sync state: `<root>/data/sync_state.json`
- Default root: `~/.convos` (override with `CONVOS_PROJECT_ROOT`)
