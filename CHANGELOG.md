# Changelog

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
