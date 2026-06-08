---
summary: "Architecture RFC: the core vs application boundary, the dependency graph, and the budget plan for taking convos to the next level."
read_when:
  - Deciding whether a new feature belongs in the core or in an application
  - Understanding the dependency picture and sequencing
  - Onboarding to the next-level roadmap
status: draft (2026-06-06)
---

# Convos: core vs applications (overview RFC)

Supersedes the 2026-05-02 six-option pitch. Detailed specs:
[01-foundation-core](01-foundation-core.md), [02-change-graph](02-change-graph.md).

Decided 2026-06-06:
- Keep the single-file, ~1000-LoC core. Big features ship as separate
  installable **applications** (~100 LoC each) that sit cleanly on top of core.
- **Defer all sharing** (MCP server, team database, remote/cross-user access).
  It reverses local-first; revisit later, never in core.

## Target use case (this is what "core" means)

convos is a local-first archive of every AI conversation across providers, that
humans and agents can search to retrieve relevant past context. The agent skill
says it in one line: *"sync and search the local convo DB to update or retrieve
conversation context."*

So **core = ingest everything + store it faithfully + retrieve it**, for humans
and agents. Nothing else earns a place in the 1000-LoC budget.

## The boundary rule

**Core owns** ingest (`parse_*`/`fetch_*`/`sync`), the schema, retrieval
(`search`/`query`/`list`/`show`/`get`), and the primitives that make retrieval
*programmable* and *faithful*. A change belongs in core only if it is ingest,
schema, retrieval, or a **capture that nothing downstream can reconstruct**.

**Applications own** analysis, synthesis, presentation, navigation. They depend
on core's read API; they never require a core schema change. If an app needs
data that is not captured, the *capture* is proposed into core minimally and
separately from the app's logic.

> Litmus test: *"Could this be built by reading the DB via `--json`/`sql`,
> without editing `cli.py`?"* Yes -> application. If it needs newly captured
> data or changes retrieval -> only that capture/retrieval part is core; the
> rest is still an application.

## Feature catalog, mapped

| Feature | Layer | Why | Depends on |
|---|---|---|---|
| `--json` / `--jsonl` on read commands | CORE | the programmable API surface every app + agent consumes | - |
| `convos sql` (read-only) | CORE | exact extraction/analysis, no bolt-on | - |
| thread tree (`messages.parent_id`) | CORE | retrieval fidelity; reconstructable only at ingest | ingest |
| plugin seam (entry points) | CORE | the clean attach point for every app | - |
| `file_edits.old_content` capture | CORE | change-graph needs it; only ingest can capture it | ingest |
| query syntax (`cwd: role: "term"`) | CORE (later) | search ergonomics | search |
| change-graph: `blame` / `timeline` | APP | analysis over `file_edits` | old_content, cwd/branch |
| file time-travel (`at`) | APP | reconstruct file @ conversation X | change-graph |
| `convos ask` (RAG + citations) | APP | synthesis; needs a generation model | retrieve |
| related conversations | APP | navigation | embeddings |
| sharing: MCP / team / merge | DEFERRED | reverses local-first | redaction, auth |
| redaction / secret-scan | DEFERRED | gated on sharing | ingest |

## Dependency picture

```
                          CORE  (single file, < 1000 LoC)
  ingest                  store / schema             retrieve
  parse_* / fetch_*  -->  conversations          --> search (BM25)
  sync                    messages   [+parent_id]     query  (hybrid)
                          tool_calls                  list / show / get
                          attachments
                          file_edits [+old_content]
                               |
       programmable API surface  (what apps AND agents consume)
       --json / --jsonl  .....  convos sql (read-only)
                               |
                          plugin seam   (entry points group: convos.commands)
        _______________________|________________________
       |               |                |                |
   APPLICATIONS  (separate packages, ~100 LoC each, read-only, no core schema edits)
   change-graph    time-travel       ask              related
   blame/timeline  file @ conv X     RAG + citations  near-dup nav
     ^needs                            ^needs            ^needs
     old_content +                     retrieve +        embeddings
     cwd/branch                        gen model

   DEFERRED (reverses local-first; lives in its own package/repo when revisited):
   sharing / MCP / team   <-- needs redaction + auth + merge-by-id (gen_id makes
                              merge cheap, since ids are machine-independent)
```

## The plugin seam (how apps attach without polluting core)

Core adds ~6 LoC: discover installed plugins and let each register subcommands
on the Typer `app`.

```python
from importlib.metadata import entry_points
for ep in entry_points(group="convos.commands"):
    try: ep.load()(app)          # register(app): app.add_typer(...) / app.command(...)
    except Exception as e: typer.echo(f"plugin {ep.name} failed: {e}", err=True)
```

(One of the few justified `try/except`s: a broken plugin must not kill the CLI.)

An application declares:

```toml
[project.entry-points."convos.commands"]
changegraph = "ai_convos_changegraph:register"
```

Core also exposes a tiny **public read API** so apps don't reach into privates:
`get_db(read_only=True)`, the schema (documented in `docs/database.md`), and the
`--json`/`sql` surface. **App contract:** depend on `ai-convos-db`, open the DB
read-only, stay <= ~100 LoC, never edit the core schema.

## Budget plan

- **Now:** 998 / 1000 token-aware LoC (`cli.py` 868, `browser.py` 125, init/main 5).
- **Reclaim first:** `browser.py` is dead in the production path -- `cli.py`
  never imports it, and all web fetching is urllib+cookies. Its only live use is
  `validate_schema` / `EXPECTED_SCHEMAS` in `tests/test_integrations.py`. Move
  those into the test suite, delete the Playwright code, and drop the
  `playwright` dependency. Result: core ~**873 / 1000** (~127 headroom) and one
  heavy dependency gone.
- **Spend (core):** `sql` ~5, `json`/`jsonl` ~20, `parent_id` ~6, plugin seam
  ~6 -> ~37 LoC. Fits comfortably.
- **Apps:** each is its own package under `apps/<name>/` (outside the core
  budget glob, which is `src/ai_convos/*.py`). Enforce a parametrized
  ~100-LoC-per-app budget test so the discipline carries over.

## Sequencing

- **M1 - Foundation (core).** browser cleanup -> `--json`/`--jsonl` + `convos
  sql` -> `messages.parent_id` + plugin seam. Small, exact, unblocks every app.
- **M2 - Change-graph.** core capture (`file_edits.old_content`) -> app package
  `ai-convos-changegraph` (`blame` / `timeline` / `at`).
- **M3 - optional apps.** `ask`, related-conversations, and (if cheap) query
  syntax.
- **Deferred.** sharing / MCP / team + redaction.

## Deferred, and why

Sharing (the original ideas #1 MCP and #4 team DB) reverses convos's core
promise that *data never leaves your machine*, and MCP was already parked once.
Deferred by decision on 2026-06-06. When revisited: redaction/secret-scan and
per-repo/user scoping are prerequisites, it lives in its own package/repo (never
core), and the deterministic `gen_id` already makes merge-by-id cheap.

## Open questions

- Query syntax: fold into `search`, or its own thin layer? Core or app?
- Parent-link availability per source (see [01](01-foundation-core.md) sec 3):
  claude-code jsonl has `parentUuid`; chatgpt `mapping` has `parent`; claude
  web/export varies; codex is linear.
- Per-app budget number (100?) and home (monorepo `apps/` vs separate repos).
