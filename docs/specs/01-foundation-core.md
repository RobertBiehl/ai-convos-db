---
summary: "Spec: the small, exact, in-core wins that make convos programmable and faithful -- convos sql, json/jsonl output, thread tree, plus the cleanup that pays for them."
read_when:
  - Implementing the M1 foundation milestone
  - Adding structured output or the read-only SQL escape hatch
  - Capturing the message thread tree
status: draft (2026-06-06)
---

# Foundation: core API wins (spec 01)

Part of [00-overview](00-overview.md). Scope: the small, exact, in-core wins that
make the target use case programmable and faithful -- plus the cleanup that pays
for them. Everything here is core. Analysis/synthesis is out of scope (see
[02-change-graph](02-change-graph.md) and the deferred apps).

## 0. Enabling cleanup (do this first)

`browser.py` (125 token-aware LoC, ~12.5% of the budget) is dead in the
production path: `cli.py` never imports it, and web fetching is urllib+cookies
(`fetch_chatgpt`/`fetch_claude`). Its only live consumers are `validate_schema`
+ `EXPECTED_SCHEMAS`, used by `tests/test_integrations.py`.

- Move `validate_schema` + `EXPECTED_SCHEMAS` into the test suite (a test helper
  module, not `src/`).
- Delete the Playwright functions; remove `playwright` from `pyproject` deps.
- Net: ~125 core LoC reclaimed, one heavy dependency dropped.

Risk: if a Playwright-based `claude-code-web` source is genuinely planned, gate
it behind an optional extra (`pip install ai-convos-db[web]`) instead of
deleting -- either way it leaves the core budget.

## 1. `convos sql` (read-only)

**Problem.** No escape hatch. Any analysis/extraction the built-in commands
don't cover forces the user to open DuckDB by hand.

**Design.** `convos sql "<query>"` runs on the existing read-only connection
(`_ro()`). A read-only DuckDB connection makes writes fail by construction, so
it is safe and exact -- no query parsing or allow-listing needed. Print rows as
a table; honor `--json`/`--jsonl` (section 2). Optional `--file q.sql` for long
queries.

**Exactness.** It is literally SQL over the stored data -> exact.

**Budget.** ~5 LoC. **Tests.** A `SELECT` returns rows; an `INSERT`/`UPDATE`
fails cleanly with a friendly message (not a traceback).

## 2. `--json` / `--jsonl` output

**Problem.** Only `export` emits structured data. `search`/`query`/`list`/
`show`/`get`/`edits`/`tools`/`sql` are human-formatted via `typer.echo`, so
agents, scripts, and applications cannot consume them. This is the single
biggest blocker to convos being a programmable backend.

**Design.** Add `-f/--format {text,json,jsonl}` (default `text`) to the read
commands. One shared emitter takes a `list[dict]` and either pretty-prints text
(current behavior), dumps a JSON array (`json`), or writes one object per line
(`jsonl`, stream-friendly for large results and agent pipelines). Each command
already has the row tuples; it just names them into dicts before emitting.

This is the API surface every application and the agent skill consume. Update
`skills/agent-convos/SKILL.md` to prefer `--jsonl` for programmatic use.

**Exactness.** Pass-through of stored values -> exact.

**Budget.** ~15-25 LoC (one emitter + flag plumbing). Keep it dense: a single
shared `Option` and one `emit(rows, fmt)` helper reused across commands.

**Tests.** For each read command, `--json` output parses as JSON; `--jsonl`
yields one valid object per line; `text` output is unchanged (snapshot).

## 3. Thread tree (`messages.parent_id`)

**Problem.** Parsing flattens the message *tree* into a time-ordered list.
ChatGPT's `mapping` carries `parent` pointers that are dropped (`parse_chatgpt`,
cli.py ~:420); claude-code jsonl carries `parentUuid` that is dropped. So
branches, regenerations, and "which version won" are lost, and `show`/`get` can
interleave dead branches as if they were one thread.

**Design.** Add `parent_id VARCHAR` to `messages`, migrated on open with an
`ALTER TABLE ... ADD COLUMN` guard mirroring the existing `embedding` migration
(cli.py ~:57). Populate where the source provides it:

- **chatgpt:** `node["parent"]` -> `gen_id("chatgpt", f"{cid}:{parent}")` (clean
  -- message ids already use the node id).
- **claude-code:** `event["parentUuid"]`; requires a `uuid -> message-id` map
  because messages are currently keyed by enumeration index, not by event uuid.
- **codex:** linear -> parent = previous message in the session.
- **claude web/export:** `parent_message_uuid` where present, else `NULL`.

`show`/`get` gain the ability to walk the DAG and render only the winning branch
(default) or `--tree` for all branches.

**Exactness.** Parent ids come straight from the source -> exact; `NULL` where
the source omits them (honest, not guessed).

**Backward-compat.** CLAUDE.md: none unless tested. Ship the `ALTER` migration
plus a parser test, exactly like `embedding`.

**Budget.** ~6-10 LoC (column + per-parser parent + optional traversal).

**Tests.** Parent captured for chatgpt and claude-code fixtures; `NULL` where
the source has no parent; old DBs migrate (a column-exists assertion like the
embedding migration test).

## 4. Query syntax (optional, later)

`cwd:foo role:user "term"` -> structured filters + FTS. Core-eligible (it is
retrieval ergonomics) but deferrable. Keep it out of M1 unless it lands cheaply;
otherwise fold into `search` later.

## Out of scope -> applications

`blame` / `timeline` / time-travel ([02](02-change-graph.md)), `ask`,
related-conversations. They all consume sections 1-3 and must not add core LoC
beyond the captures justified above.
