# AI Convos DB
[![Stars](https://img.shields.io/github/stars/RobertBiehl/ai-convos-db?style=flat)](https://github.com/RobertBiehl/ai-convos-db/stargazers)
[![Forks](https://img.shields.io/github/forks/RobertBiehl/ai-convos-db?style=flat)](https://github.com/RobertBiehl/ai-convos-db/forks)
[![Issues](https://img.shields.io/github/issues/RobertBiehl/ai-convos-db?style=flat)](https://github.com/RobertBiehl/ai-convos-db/issues)
[![License](https://img.shields.io/badge/license-MIT-blue?style=flat)](LICENSE)

## Quick install
```bash
uv tool install "git+https://github.com/RobertBiehl/ai-convos-db.git"
```

Local-first memory for coding agents. Capture Claude Code and Codex work automatically, retrieve decisions across providers, and optionally synchronize encrypted personal or team memory through a self-hosted relay.

## Why this exists

- Resume work across coding agents without reconstructing old sessions
- Retrieve prior decisions, commands, evidence, and edits without dumping whole transcripts
- Keep ChatGPT, Claude, Claude Code, and Codex history locally searchable
- Keep the same encrypted memory available across computers without path allowlists
- Share project-associated prompts and changes automatically with encrypted team workspaces
- Use a CLI skill and lifecycle hooks; the self-hosted relay is optional

## Features

- Fast full-text search with filters (source, days, role, thinking)
- Hybrid semantic search (BM25 + embeddings + Reciprocal Rank Fusion) via `convos query`
- Fetch from ChatGPT and Claude using browser cookies
- Import exports from ChatGPT, Claude, Claude Code, and Codex
- Capture completed Claude Code + Codex turns just in time with lifecycle hooks
- Optional code-change provenance: blame, timeline, time travel, and graph browsing
- Optional end-to-end encrypted personal multi-device and team synchronization
- Export to JSON or CSV

## Install

Install from GitHub with uv (adds `convos` to PATH in an isolated environment):

```bash
uv tool install "git+https://github.com/RobertBiehl/ai-convos-db.git"
```

`pipx install "git+https://github.com/RobertBiehl/ai-convos-db.git"` is also supported.

Upgrade later with:

```bash
uv tool install --reinstall "git+https://github.com/RobertBiehl/ai-convos-db.git"
convos install-skills
```

The first install may compile `llama-cpp-python` locally and take about a
minute on macOS; later reinstalls reuse the built package.

`convos init` creates the archive and installs the bundled Codex + Claude Code
skills automatically. Refresh the skills without initializing the archive with:

```bash
convos install-skills
```

Optionally add code-change provenance without expanding the core CLI package:

```bash
uv tool install --reinstall "git+https://github.com/RobertBiehl/ai-convos-db.git" \
  --with "ai-convos-changegraph @ git+https://github.com/RobertBiehl/ai-convos-db.git#subdirectory=apps/changegraph"
```

This adds `convos blame`, `timeline`, `at`, `graph`, and `browse`.

The encrypted remote uses one optional client package and one independently
installable server package, so the local archive stays server-free by default.
See [self-hosting, recovery, team policy, and installation](docs/remote.md).
Runnable synthetic scenarios are in [`examples/remote`](examples/remote/README.md).
See [`examples/insights`](examples/insights/README.md) for local decision,
comparison, archive-statistics, and prompt-to-change query recipes.

## Quickstart

```bash
convos init
convos install-hooks
convos sync                  # one-time history/web/import backfill
convos doctor
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
convos query "how do I store vectors in duckdb"    # hybrid: BM25 + embeddings + RRF
```

Both discovery commands return the strongest matching message from each
conversation, so `-n` controls the number of distinct conversation candidates.

Semantic search is included by default. Run `convos embed` or `convos sync`
after install to backfill embeddings with a progress bar; subsequent syncs only
embed new/changed messages. The `embeddinggemma-300m-qat-q8_0` model produces
768d embeddings and runs locally via llama.cpp.

Read a known conversation using an ID prefix from search/query:

```bash
convos read f2b9c5a9 -n 20 -c 2000 -f jsonl
convos read f2b9c5a9 --around 01ab -n 20 -f jsonl
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

Start a new agent session after installing hooks. In Codex, review the user
hook through `/hooks`; after the first completed turn, `convos doctor` should
show a recent `ingest: ... last=...` timestamp.

Hooks enqueue only the local transcript path and file metadata, then return
immediately. A coalescing background drain parses and upserts the transcript;
`search` and `query` rebuild FTS once for all pending changes before reading.
`query` also embeds only
the changed hook messages, while `search` and `sql` avoid loading the embedding
model. `sync` remains the reconciliation path for missed local events, web
providers, pre-hook sessions, and imports rather than a routine local update.

Check the complete local pipeline with `convos doctor`. It reports the running
version, archive/schema/FTS health, embedding backlog, queued ingestion, hook
installation, and web-cookie availability without modifying the archive.

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

This is local-first. Your data never leaves your machine unless you export it
or explicitly configure the optional encrypted remote. The remote receives
ciphertext and synchronization metadata, never workspace keys or plaintext.

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
