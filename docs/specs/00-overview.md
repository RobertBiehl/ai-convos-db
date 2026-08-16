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
- Keep the single-file, <1200-line core. Big features ship as separate
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
and agents. Nothing else earns a place in the 1200-line budget.

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
| related conversations and trails | EXPLORE APP | local one-hop and bounded multi-hop navigation with exact turn evidence | embeddings |
| deterministic project handoff and replay | RESUME APP | combines live Git and cwd-scoped evidence or replays exact messages/tools/edits | read API, file/tool capture |
| provider-stored memory capture | PLANNED CORE | faithful agent state belongs beside conversation provenance; core never writes providers | ingest |
| canonical memory reconciliation and sync-back | MEMORY APP | optional revision ledger, delivery, projection, and remote convergence | provider memory capture |
| reproducible retrieval evaluation | DEV TOOL | private exact relevance judgments, hit@k, and MRR guard retrieval changes | search/query |
| encrypted personal/team synchronization | APP/SERVICE | optional E2EE event transport | protocol, projection, provenance |
| redaction / secret-scan | REDACT APP | mandatory local pre-encryption team policy plus standalone archive audit | remote projection |

## Dependency picture

```
                          CORE  (single file, < 1200 lines)
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
   change-graph    explore             resume/replay     memory
   blame/timeline  semantic trails     exact evidence    canonical sync
     ^needs          ^needs              ^needs            ^needs
     old_content +   embeddings          read + tools      memory capture
     cwd/branch                          + edits

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
`--json`/`sql` surface. **App contract:** depend on `convos`, leave the
core schema stable, and stay within the product budget declared in
`test_budget.py`.

Installed products may additionally expose a `convos.init` callback. Core runs
these after schema, skill, and capture-hook setup. This lifecycle is only for
local, idempotent, non-destructive readiness; it must not download models,
configure remotes, enroll devices, request credentials, or contact services.

## Budget

The token-aware core count must remain below 1200 lines and is enforced by
`tests/test_budget.py`; new core work must reduce or repack existing code before
it consumes the remaining margin. Each installable product under `apps/` has
its own explicit honest budget, and internal modules never become packages just
to evade either boundary.

## Sequencing

- **M1 - Foundation (core).** browser cleanup -> `--json`/`--jsonl` + `convos
  sql` -> `messages.parent_id` + plugin seam. Small, exact, unblocks every app.
- **M2 - Change-graph.** core capture (`file_edits.old_content` plus typed
  provenance) -> read-only app package `convos-changegraph` (`blame` /
  `timeline` / `at`).
- **M3 - semantic navigation.** Explore ships related-conversation and
  exact-turn trail navigation locally.
- **M4 - encrypted remote.** Protocol/server -> personal multi-device -> Git
  provenance -> team policies and membership. See [04](04-remote-sync.md).
- **M5 - sharing hardening.** Standalone local secret audit -> mandatory
  pre-encryption team redaction -> attachment omission and value-free audit.
- **M6 - continuation UX.** Deterministic project resume packet -> live Git
  evidence + exact recent turns -> bounded agent-ready verification handoff ->
  exact message/tool/edit replay.
- **M7 - memory boundary.** Faithfully capture provider-stored memories in core
  without writing providers -> optional canonical ledger, delivery, projection,
  and remote sync in the Memory product.
- **M8 - retrieval quality tooling.** Direct cwd/conversation filters -> private
  exact-ID judgment suites -> literal/hybrid hit@k and MRR regression gates.

## Remote boundary

The June sharing deferral is superseded by [spec 04](04-remote-sync.md). The
reasoning that kept it out of core still stands. The feature has exactly two
installable packages: the remote client and the remote server. Protocol,
transport projection, hooks, and worker code are internal client modules; core
alone performs DuckDB projection and provenance capture. Core remains a
server-free local archive. Existing ids are origin ids, not assumed to be
universal team identities.

## Open questions

- Parent-link availability per source (see [01](01-foundation-core.md) sec 3):
  claude-code jsonl has `parentUuid`; chatgpt `mapping` has `parent`; claude
  web/export varies; codex is linear.
- Per-app budget number (100?) and home (monorepo `apps/` vs separate repos).
