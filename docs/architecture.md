---
summary: "System architecture: data flow, components, and design decisions."
read_when:
  - Understanding how the system works
  - Adding new features
  - Debugging data flow issues
---

# Architecture

## Overview

Single-file CLI that normalizes conversations from multiple AI providers into a unified DuckDB database with full-text search.

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Data Sources   │     │   Processors    │     │    Storage      │
├─────────────────┤     ├─────────────────┤     ├─────────────────┤
│ ChatGPT API     │────▶│ fetch_chatgpt   │────▶│                 │
│ Claude API      │────▶│ fetch_claude    │     │   ParseResult   │
│ Claude Code     │────▶│ parse_claude_*  │────▶│   (normalized)  │
│ Codex           │────▶│ parse_codex     │     │                 │
│ Export files    │────▶│ parse_*         │     └────────┬────────┘
└─────────────────┘     └─────────────────┘              │
                                                         ▼
                                              ┌─────────────────┐
                                              │    DuckDB       │
                                              ├─────────────────┤
                                              │ conversations   │
                                              │ messages (FTS)  │
                                              │ tool_calls      │
                                              │ attachments     │
                                              │ file_edits      │
                                              └─────────────────┘
```

## Key Design Decisions

### Single File

All logic lives in `src/ai_convos/cli.py`. This keeps the codebase simple and avoids import complexity. The file (and total `src/ai_convos/`) is held under the 1000-line budget enforced by `tests/test_budget.py`.

### ParseResult Normalization

Every data source produces a `ParseResult` containing:
- `convs` - conversation metadata
- `msgs` - message content
- `tools` - tool call records
- `attachs` - file/image attachments
- `artifacts` - Claude artifacts (code blocks, etc.)
- `edits` - file edit operations

This normalization happens at parse time, not query time.

### Deterministic IDs

IDs are SHA256 hashes of `source:original_id`. This ensures:
- Same conversation always gets same ID
- Re-syncing updates rather than duplicates
- IDs are stable across machines

### Cookie-Based Auth

Web fetchers extract cookies from Safari or Chrome to authenticate with APIs. No passwords stored, no OAuth flows. Cookies expire naturally.

### FTS via DuckDB

Full-text search uses DuckDB's FTS extension with BM25 scoring. The index covers `content` and `thinking` columns. Index is rebuilt after each sync.

### Hybrid Semantic Search (optional)

`convos query` adds vector retrieval on top of BM25. Embeddings live in
`messages.embedding` (FLOAT[768], NULL until embedded). Vector similarity is
computed brute-force via DuckDB's `array_cosine_similarity` — at the current
scale (tens of thousands of messages) this is fast enough that a vector index
(VSS/HNSW) would only add complexity.

Pipeline: filtered BM25 top-50 ∪ cosine top-50 → Reciprocal Rank Fusion → one
message per conversation → Qwen3-Reranker top-16 → position-tier blend (rank
0-2: 0.75/0.25, 3-9: 0.6/0.4, 10+: 0.4/0.6).
Models load lazily, so users pay model startup/download cost only when using
`convos query`, `convos embed`, or sync-time embedding.

The package depends on `llama-cpp-python` + `huggingface-hub`. Both models are
GGUF, downloaded on first call and cached by huggingface-hub.

## Data Flow

1. **Fetch/Parse**: Read from API or file without holding the DuckDB lock
2. **Upsert**: Acquire the writer briefly per completed `ParseResult`
3. **Index**: Rebuild FTS under a short writer connection
4. **Embed**: Compute vectors unlocked, then update each batch under a short writer connection

### Just-in-time local ingestion

Claude Code and Codex lifecycle hooks write coalesced records under
`data/hook_inbox/`; records contain only provider, transcript path, size, and
mtime. The hook process never opens DuckDB. A detached drain parses stable
transcript snapshots without a DB lock, upserts each snapshot under a short
writer connection, rebuilds FTS once, and records the processed snapshot.

`search`, `query`, and `sql` drain pending records before opening their read
connection. `query` also embeds hook-ingested messages when the local dirty
marker is present. Ingest is additive: missing records are never deleted, and
rewritten records preserve the prior payload under a deterministic history id.
`sync` drains the same inbox before its normal local/web reconciliation.
5. **Query**: CLI commands read from DB, apply filters, format output

## File Layout

User data (DuckDB + state) lives under `~/.convos` by default (override with `CONVOS_PROJECT_ROOT`).

```
ai-convos-db/
├── src/ai_convos/
│   ├── __init__.py      # exports app
│   ├── cli.py           # all logic
│   └── browser.py       # playwright helpers (optional)
├── data/
│   └── convos.db        # DuckDB database
├── tests/
│   ├── test_integrations.py
│   └── test_parsers.py
└── docs/                # this documentation
```

## Extension Points

**Adding a new source:**
1. Write `fetch_X` or `parse_X` returning `ParseResult`
2. Register in `fetchers` or `parsers` dict
3. Add tests

**Adding new metadata:**
1. Prefer storing in `metadata` JSON column
2. Only add schema columns for frequently queried fields

**Adding new table:**
1. Add to `init_schema()`
2. Add to `ParseResult` class
3. Add to `upsert()` function
