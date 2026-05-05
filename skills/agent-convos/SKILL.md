---
name: agent-convos
description: Sync and search the local convo DB to update or retrieve conversation context.
---

# Agent Convos

Sync:

```bash
convos sync
convos embed  # backfill hybrid embeddings without web sync
```

Retrieve:

```bash
convos search "query" -n 8 -c 160                   # BM25 — exact terms, fast
convos query "natural language question" -n 8 -c 160  # hybrid — semantic, requires [hybrid] extra
convos list -n 20
convos show <id-prefix> --tools --thinking
convos get <id-prefix> --since 2024-01-01T00:00:00Z
convos get <id-prefix> --after <message-id-prefix>
convos doctor
```

Behavior:
- Optimize relevance and tokens: set `-n` <= 8 and `-c` <= 200 unless user wants more.
- Filter early: use `-s` for source, `-d` for recency, `-r` for role when implied.
- Pick the right command: `search` for known keywords or exact strings; `query` for paraphrased / conceptual lookups (slower, needs models loaded).
- Prefer conversation-level answers: summarize unique conversation IDs/titles, then `show` only when asked.
- Use `sync` as the only fetch/import update command; use `embed` only to backfill hybrid embeddings.
- Expect fast no-op when nothing changed.
- Try available sources/browsers, remember the last working choice.
- Report specific errors if nothing works (cookies, auth, permissions).
- Use `CONVOS_IMPORT_PATHS` for export paths (comma-separated).
- If `convos` is not on PATH, use the repo wrapper: `bin/convos`.
- Use shell commands only; do not use MCP resources for this skill.

Storage:
- DB file: `<root>/data/convos.db`
- Sync state: `<root>/data/sync_state.json`
- Default root: `~/.convos`
- Override root: set `CONVOS_PROJECT_ROOT`
