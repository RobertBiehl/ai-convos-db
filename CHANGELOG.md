# Changelog

## 0.6.0

- Add signed workspace-state chains for client-verifiable membership, roles, devices, removals, history entitlements, and epoch keys.
- Let an authorized device approve another device for the same user with matching access, including selected-history inheritance.
- Add strict-majority team recovery and separate history activation with one vote per represented user.
- Enforce proposal timing with the server clock and harden relay metadata, proposal, vote, rejection, and history-envelope validation.

## 0.5.0

- Add optional self-hosted end-to-end encrypted personal and team synchronization.
- Add immutable signed events, device enrollment, recovery, epoch rotation, history grants, and opaque server storage.
- Add path-independent Git provenance, cross-repository changesets, checkpoint gaps, and typed local graph views.

## 0.4.0
- Add bundled Codex and Claude Code skills plus just-in-time lifecycle hooks with crash-safe, idempotent queue draining.
- Make literal and hybrid retrieval conversation-first, bound structured output, expose exact message IDs, and add `convos read` for recent or hit-centered context.
- Simplify hybrid ranking to BM25 + embedding Reciprocal Rank Fusion, removing the second local reranker model and reducing cold query latency.
- Add `convos doctor` archive, schema, FTS, embedding, hook, queue, and browser-cookie health checks with concrete repair commands.
- Capture current Codex custom tool calls, outputs, and exact `apply_patch` hunks for local code provenance.
- Add the optional changegraph commands: `blame`, `timeline`, `at`, `graph`, and `browse`.
- Keep DuckDB locks short, wait for active writers, defer FTS rebuilding until retrieval, and preserve superseded message/tool/edit payloads as history.
- Add full sync reconciliation, ChatGPT thread-parent metadata, and incremental local/web import improvements.

## 0.2.0
- Add hybrid semantic search with `convos query`: BM25 + local embeddings + Qwen3 reranking.
- Add `convos embed` to backfill embeddings without fetching new web conversations.
- Preserve existing embeddings during sync unless message content changes.
- Document hybrid search setup and database schema.

## 0.1.3
- Auto-discover Chrome profiles for ChatGPT sync.

## 0.1.2
- Fix ChatGPT web sync for workspace accounts.
- Add optional parse error logging and Chrome profile selection.

## 0.1.1
- Sync output: per-service updated/new convo counts, totals, and timings with -v.
- Fix Claude no-op sync to avoid full re-fetch when unchanged.
- Improve local sync to only reparse changed Codex/Claude Code sessions.
- Add repo-local UV cache wrapper and install script cache default.
- README install command and headings cleanup.
