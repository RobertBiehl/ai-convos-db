---
summary: "Architecture RFC: the core vs application boundary, the dependency graph, and the budget plan for taking convos to the next level."
read_when:
  - Deciding whether a new feature belongs in the core or in an application
  - Understanding the dependency picture and sequencing
  - Onboarding to the next-level roadmap
status: accepted core boundary; sharing decision updated 2026-07-10
---

# Convos: core vs applications (overview RFC)

Supersedes the 2026-05-02 six-option pitch. Detailed specs:
[01-foundation-core](01-foundation-core.md), [02-change-graph](02-change-graph.md).

Decided 2026-06-06:
- Keep the single-file, ~1000-LoC core. Big features ship as separate
  installable **applications** that sit cleanly on top of core. Package
  boundaries represent products; internal modules do not become packages to
  evade a budget.

Updated 2026-07-10: encrypted remote synchronization was revisited and accepted
as optional applications in [04-remote-sync](04-remote-sync.md). It remains out
of core, keeps plaintext retrieval local, and uses encrypted workspace
projections rather than merging DuckDB files or trusting a server with content.

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
| direct cwd/conversation filters | CORE | remove common text-matching SQL fallbacks without a mini-language | search/query |
| change-graph: `blame` / `timeline` | APP | analysis over `file_edits` | old_content, cwd/branch |
| file time-travel (`at`) | APP | reconstruct file @ conversation X | change-graph |
| `convos ask` (RAG + citations) | ASK APP | private local synthesis with fail-closed exact citations | retrieve |
| related conversations, trails, visual maps | EXPLORE APP | local one-hop, bounded multi-hop, and private clickable navigation with exact turn evidence | embeddings |
| deterministic project handoff | RESUME APP | combines live Git state with bounded exact cwd-scoped archive evidence | read API, file/tool capture |
| exact cross-project activity digest/dashboard | PULSE APP | factual presentation over recent cwd, message, file, and tool metadata | read API, file/tool capture |
| reproducible retrieval evaluation | EVAL APP | user-owned exact relevance judgments, hit@k, and MRR over real engines | search/query |
| private conversation browser and replay | LIBRARY APP | human search plus bounded exact messages/tool calls/edits without a write surface | search/query/read, tool/edit capture |
| encrypted personal/team synchronization | APP/SERVICE | optional E2EE event transport | protocol, projection, provenance |
| redaction / secret-scan | REDACT APP | mandatory local pre-encryption team policy plus standalone archive audit | remote projection |

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
   APPLICATIONS  (one package per installable product, explicit line budgets)
   change-graph    time-travel       ask              explore
   blame/timeline  file @ conv X     RAG + citations  near-dup nav
     ^needs                            ^needs            ^needs
     old_content +                     retrieve +        embeddings
     cwd/branch                        gen model

   OPTIONAL REMOTE PRODUCTS (still local-first; see spec 04):
   remote client [protocol, projection, provenance, service] <-> remote server
   personal workspaces sync all; team workspaces receive policy projections
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
`--json`/`sql` surface. **App contract:** depend on `ai-convos-db`, leave the
core schema stable, and stay within the product budget declared in
`test_budget.py`.

Installed products may additionally expose a `convos.init` callback. Core runs
these after schema, skill, and capture-hook setup. This lifecycle is only for
local, idempotent, non-destructive readiness; it must not download models,
configure remotes, enroll devices, request credentials, or contact services.

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
- **Apps:** each installable product is one package under `apps/<name>/`
  (outside the core budget glob, which is `src/ai_convos/*.py`). Small apps
  default to 200 token-aware lines. Larger cohesive products declare one honest
  explicit limit rather than splitting implementation modules into packages.

## Sequencing

- **M1 - Foundation (core).** browser cleanup -> `--json`/`--jsonl` + `convos
  sql` -> `messages.parent_id` + plugin seam. Small, exact, unblocks every app.
- **M2 - Change-graph.** core capture (`file_edits.old_content`) -> app package
  `ai-convos-changegraph` (`blame` / `timeline` / `at`).
- **M3 - optional apps.** Semantic Explore ships related-conversation and
  exact-turn navigation locally; Ask ships explicit local-model setup and
  fail-closed exact-turn citations.
- **M4 - encrypted remote.** Protocol/server -> personal multi-device -> Git
  provenance -> team policies and membership. See [04](04-remote-sync.md).
- **M5 - sharing hardening.** Standalone local secret audit -> mandatory
  pre-encryption team redaction -> attachment omission and value-free audit.
- **M6 - continuation UX.** Deterministic project resume packet -> live Git
  evidence + exact recent turns -> bounded agent-ready verification handoff.
- **M7 - activity UX.** Exact Git-root-grouped recent archive counts -> bounded
  session IDs and read pivots -> private content-free local HTML dashboard.
- **M8 - retrieval quality.** Direct cwd/conversation filters -> private
  exact-ID judgment suites -> literal/hybrid hit@k and MRR regression gates.
- **M9 - human archive UX.** Private loopback Library -> literal or cached-only
  hybrid search -> bounded hit-centered replay of exact messages, tool calls,
  and file edits -> deterministic JSON for agents.

## Remote boundary

The June sharing deferral is superseded by [spec 04](04-remote-sync.md). The
reasoning that kept it out of core still stands. The feature has exactly two
installable packages: the remote client and the remote server. Protocol,
projection, provenance, hooks, and worker code are internal client modules.
Core remains a server-free local archive. Existing ids are origin ids, not
assumed to be universal team identities.

## Open questions

- Parent-link availability per source (see [01](01-foundation-core.md) sec 3):
  claude-code jsonl has `parentUuid`; chatgpt `mapping` has `parent`; claude
  web/export varies; codex is linear.
- Per-app budget number (100?) and home (monorepo `apps/` vs separate repos).
