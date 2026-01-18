AI Convos DB
============

[![Stars](https://img.shields.io/github/stars/RobertBiehl/ai-convos-db?style=flat)](https://github.com/RobertBiehl/ai-convos-db/stargazers)
[![Forks](https://img.shields.io/github/forks/RobertBiehl/ai-convos-db?style=flat)](https://github.com/RobertBiehl/ai-convos-db/forks)
[![Issues](https://img.shields.io/github/issues/RobertBiehl/ai-convos-db?style=flat)](https://github.com/RobertBiehl/ai-convos-db/issues)
[![License](https://img.shields.io/badge/license-MIT-blue?style=flat)](LICENSE)

Local-first, searchable archive for ChatGPT, Claude, and Codex conversations. One file, one DB, fast full-text search.

Why this exists
---------------

- Keep all your AI chats in one place
- Search across providers with DuckDB FTS
- Import exports or fetch directly from your browser cookies
- Track tools, attachments, and edits

Features
--------

- Fast full-text search with filters (source, days, role, thinking)
- Fetch from ChatGPT and Claude using browser cookies
- Import exports from ChatGPT, Claude, Claude Code, and Codex
- Sync Claude Code + Codex sessions on a schedule
- Export to JSON or CSV

Install
-------

One-line install (adds `convos` to PATH):

```bash
curl -fsSL https://raw.githubusercontent.com/RobertBiehl/ai-convos-db/main/scripts/install.sh | bash
```

Recommended (global CLI):

```bash
pipx install .
```

With uv:

```bash
uv tool install .
```

Local dev:

```bash
uv run convos --help
```

Install skills (Codex + Claude Code):

```bash
uv run convos install-skills
```

Quickstart
----------

Install and run with uv:

```bash
uv run convos init
uv run convos sync
uv run convos search "prompt" -s claude -n 10
```

If Safari cookies are protected by macOS privacy, `sync` will fall back to Chrome.

Common commands
---------------

Search:

```bash
uv run convos search "vector database" -s chatgpt -d 30
uv run convos search "reasoning" --thinking
```

Show a conversation:

```bash
uv run convos list -n 20
uv run convos show <id-prefix> --tools --thinking
uv run convos get <id-prefix> --since 2024-01-01T00:00:00Z
uv run convos get <id-prefix> --after <message-id-prefix>
```

Sync:

```bash
uv run convos sync
uv run convos sync -w -i 600
```

Auto-import export paths with:

```bash
CONVOS_IMPORT_PATHS="~/Downloads/chatgpt-export.zip,~/.claude/projects" uv run convos sync
```

Export:

```bash
uv run convos export out.json -f json
uv run convos export out.csv -f csv -s claude
```

Data model
----------

Data lives in `data/convos.db` (DuckDB) with these core tables:

- `conversations`
- `messages`
- `tool_calls`
- `attachments`
- `artifacts`
- `file_edits`

Privacy and security
--------------------

This is local-first. Your data never leaves your machine unless you export it.

On macOS, Safari cookie access requires Full Disk Access for your terminal.
If you prefer not to grant it, use Chrome cookies with `-b chrome`.

FAQ
---

Q: Why is fetch failing on Safari?
A: macOS blocks access to Safari cookies without Full Disk Access. Use `-b chrome` or grant access.

Q: Where is the database stored?
A: `data/convos.db` in the repo.

Q: Can I reset the DB?
A: Delete `data/convos.db` and re-run `uv run convos init`.

Roadmap
-------

- Provider-specific enrichments (projects, files, artifacts)
- TUI browser
- Simple web UI

Contributing
------------

PRs welcome. Keep changes small and focused. See `AGENTS.md` for architecture and coding style.

Agent usage
-----------

Agents should use the CLI only. See `skills/agent-convos/SKILL.md`.
For setup and usage with Codex/Claude, see `docs/skills-setup.md`.
