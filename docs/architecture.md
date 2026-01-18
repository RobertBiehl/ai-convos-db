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

All logic lives in `src/ai_convos/cli.py`. This keeps the codebase simple and avoids import complexity. The file is ~600 lines.

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

## Data Flow

1. **Fetch/Parse**: Read from API or file, produce `ParseResult`
2. **Upsert**: `INSERT OR REPLACE` into DuckDB tables
3. **Index**: Rebuild FTS index on messages table
4. **Query**: CLI commands read from DB, apply filters, format output

## File Layout

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
