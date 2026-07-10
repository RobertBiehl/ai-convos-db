# AI Convos DB
[![Stars](https://img.shields.io/github/stars/RobertBiehl/ai-convos-db?style=flat)](https://github.com/RobertBiehl/ai-convos-db/stargazers)
[![Forks](https://img.shields.io/github/forks/RobertBiehl/ai-convos-db?style=flat)](https://github.com/RobertBiehl/ai-convos-db/forks)
[![Issues](https://img.shields.io/github/issues/RobertBiehl/ai-convos-db?style=flat)](https://github.com/RobertBiehl/ai-convos-db/issues)
[![License](https://img.shields.io/badge/license-MIT-blue?style=flat)](LICENSE)

## Quick install
```bash
pipx install "git+https://github.com/RobertBiehl/ai-convos-db.git"
```

Local-first, searchable archive for ChatGPT, Claude, and Codex conversations. One file, one DB, fast full-text search.

## Why this exists

- Keep all your AI chats in one place
- Search across providers with DuckDB FTS
- Import exports or fetch directly from your browser cookies
- Track tools, attachments, and edits

## Features

- Fast full-text search with filters (source, days, role, thinking)
- Hybrid semantic search (BM25 + embeddings + Qwen3 reranker) via `convos query`
- Fetch from ChatGPT and Claude using browser cookies
- Import exports from ChatGPT, Claude, Claude Code, and Codex
- Sync Claude Code + Codex sessions on a schedule
- Export to JSON or CSV

## Install

Install from GitHub with pipx (adds `convos` to PATH in an isolated environment):

```bash
pipx install "git+https://github.com/RobertBiehl/ai-convos-db.git"
```

Upgrade later with:

```bash
pipx upgrade ai-convos-db
```

Pipx installs the CLI only. Copy the bundled skills into Codex + Claude Code with:

```bash
convos install-skills
```

## Quickstart

```bash
convos init
convos install-skills
convos install-hooks
convos sync
convos search "prompt" -s claude -n 10
convos query "conceptual search"
```

If Safari cookies are protected by macOS privacy, `sync` will fall back to Chrome.

## Common commands

Search:

```bash
convos search "vector database" -s chatgpt -d 30   # BM25 only
convos search "reasoning" --thinking
convos read f2b9c5a9 -n 20 -f jsonl              # bounded recent context from one result
convos embed                                      # backfill embeddings, no web sync
convos query "how do I store vectors in duckdb"    # hybrid: BM25 + embeddings + rerank
```

Semantic search is included by default. Run `convos embed` or `convos sync`
after install to backfill embeddings with a progress bar; subsequent syncs only
embed new/changed messages. Models used: `embeddinggemma-300m-qat-q8_0` for
embeddings (768d) and `Qwen3-Reranker-0.6B` for reranking. Both are GGUF and
run locally via llama.cpp.

Read a known conversation using an ID prefix from search/query:

```bash
convos read f2b9c5a9 -n 20 -c 2000 -f jsonl
```

List and analyze with read-only DuckDB SQL (schema in `docs/database.md`):

```bash
convos sql "SELECT id, title, created_at FROM conversations ORDER BY created_at DESC LIMIT 20" -f json
```

Sync:

```bash
convos sync
convos sync -w -i 600
```

Local Claude Code and Codex sessions can be ingested after each completed turn:

```bash
convos install-hooks             # user-level Claude Code + Codex hooks
convos install-hooks --status
convos install-hooks --remove    # remove only ai-convos-db hook handlers
```

Hooks enqueue only the local transcript path and file metadata, then return
immediately. A coalescing background drain parses and upserts the transcript;
read commands flush any pending work before querying. `query` also embeds only
the changed hook messages, while `search` and `sql` avoid loading the embedding
model. `sync` remains the reconciliation path for missed local events, web
providers, pre-hook sessions, and imports rather than a routine local update.

Auto-import export paths with:

```bash
CONVOS_IMPORT_PATHS="~/Downloads/chatgpt-export.zip,~/.claude/projects" convos sync
```

Export:

```bash
convos export out.json -f json
convos export out.csv -f csv -s claude
```

## Example output

```bash
convos sync
```
```text
Syncing Claude Code (2 convs, 118 msgs, 12 tools, 0 attachs, 4 edits)
Syncing Codex (8 convs, 214 msgs, 19 tools, 0 attachs, 0 edits)
Syncing ChatGPT (142 convs, 1734 msgs, 97 tools, 12 attachs, 0 edits)
Syncing Claude (96 convs, 842 msgs, 0 tools, 5 attachs, 0 edits)
Updated Codex (0 new, 1 updated convs; 0 convs, 9 msgs, 0 tools, 0 attachs, 0 edits processed)
Updated 0 new, 1 updated convs; 9 msgs, 0 tools, 0 attachs, 0 edits
Total: 248 convs, 2908 msgs, 128 tools, 17 attachs, 4 edits
```

```bash
convos search "vector database" -s chatgpt -d 30
```
```text
f2b9c5a9  ChatGPT  "Indexing embeddings with DuckDB"  2026-01-14T09:22:11Z
8a1d0c3e  ChatGPT  "Choosing ANN libraries"           2026-01-10T18:03:42Z
```

```bash
convos read f2b9c5a9 -f jsonl
```
```text
{"id":"01ab...","role":"user","content":"How do I store vectors in DuckDB?","thinking":null,"created_at":"2026-01-14 09:22:11"}
{"id":"02cd...","role":"assistant","content":"Use a table with a FLOAT[] column and an HNSW index...","thinking":null,"created_at":"2026-01-14 09:22:42"}
```

## Data model

Data lives in `<root>/data/convos.db` (DuckDB). Default root is `~/.convos` (override with `CONVOS_PROJECT_ROOT`).

- `conversations`
- `messages`
- `tool_calls`
- `attachments`
- `artifacts`
- `file_edits`

## Privacy and security

This is local-first. Your data never leaves your machine unless you export it.

On macOS, Safari cookie access requires Full Disk Access for your terminal.
If you prefer not to grant it, use Chrome cookies with `-b chrome`.

## FAQ

Q: Why is fetch failing on Safari?
A: macOS blocks access to Safari cookies without Full Disk Access. Use `-b chrome` or grant access.

Q: Where is the database stored?
A: `~/.convos/data/convos.db` by default (override with `CONVOS_PROJECT_ROOT`).

Q: Can I reset the DB?
A: Delete `~/.convos/data/convos.db` (or `<root>/data/convos.db`) and re-run `convos init`.

## Contributing

PRs welcome. Keep changes small and focused. See `AGENTS.md` for architecture and coding style.

## Agent usage

Agents should use the CLI only. See `skills/agent-convos/SKILL.md`.
For setup and usage with Codex/Claude, see `docs/skills-setup.md`.
