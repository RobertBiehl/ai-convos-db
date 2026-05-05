---
summary: "Database schema: tables, columns, relationships, and FTS setup."
read_when:
  - Writing queries against the database
  - Adding new fields or tables
  - Understanding data model
  - Debugging search issues
---

# Database Schema

DuckDB database at `<root>/data/convos.db`. Default root is `~/.convos` (override with `CONVOS_PROJECT_ROOT`).

## Tables

### conversations

Primary record for each conversation session.

| Column | Type | Description |
|--------|------|-------------|
| id | VARCHAR PK | Deterministic hash of `source:original_id` |
| source | VARCHAR | `chatgpt`, `claude`, `claude-code`, `codex` |
| title | VARCHAR | Conversation title or derived name |
| created_at | TIMESTAMP | First message timestamp |
| updated_at | TIMESTAMP | Last message timestamp |
| model | VARCHAR | Primary model used |
| cwd | VARCHAR | Working directory (CLI tools only) |
| git_branch | VARCHAR | Git branch (CLI tools only) |
| project_id | VARCHAR | Project/gizmo ID if applicable |
| metadata | JSON | Source-specific extra fields |

### messages

Individual messages within conversations. Has FTS index.

| Column | Type | Description |
|--------|------|-------------|
| id | VARCHAR PK | Deterministic hash |
| conversation_id | VARCHAR FK | References conversations.id |
| role | VARCHAR | `user`, `assistant`, `human`, `system`, `tool` |
| content | VARCHAR | Message text content |
| thinking | VARCHAR | Extended thinking/reasoning (Claude) |
| created_at | TIMESTAMP | Message timestamp |
| model | VARCHAR | Model for this specific message |
| metadata | JSON | Source-specific extra fields |
| embedding | FLOAT[768] | embeddinggemma vector for hybrid search (NULL until embedded) |

### tool_calls

Tool/function invocations and results.

| Column | Type | Description |
|--------|------|-------------|
| id | VARCHAR PK | Deterministic hash |
| message_id | VARCHAR FK | References messages.id |
| tool_name | VARCHAR | Tool/function name |
| input | JSON | Tool input parameters |
| output | JSON | Tool output/result |
| status | VARCHAR | `pending`, `complete`, `error` |
| duration_ms | INTEGER | Execution time if available |
| created_at | TIMESTAMP | Invocation timestamp |

### attachments

File and image attachments.

| Column | Type | Description |
|--------|------|-------------|
| id | VARCHAR PK | Deterministic hash |
| message_id | VARCHAR FK | References messages.id |
| filename | VARCHAR | Original filename |
| mime_type | VARCHAR | MIME type |
| size | INTEGER | File size in bytes |
| path | VARCHAR | Local path if downloaded |
| url | VARCHAR | Remote URL if available |
| created_at | TIMESTAMP | Attachment timestamp |

### artifacts

Claude artifacts (code, documents, etc.).

| Column | Type | Description |
|--------|------|-------------|
| id | VARCHAR PK | Deterministic hash |
| conversation_id | VARCHAR FK | References conversations.id |
| artifact_type | VARCHAR | `code`, `document`, etc. |
| title | VARCHAR | Artifact title |
| content | TEXT | Full artifact content |
| language | VARCHAR | Programming language if code |
| created_at | TIMESTAMP | Creation timestamp |
| version | INTEGER | Version number |

### file_edits

File modifications from CLI tools.

| Column | Type | Description |
|--------|------|-------------|
| id | VARCHAR PK | Deterministic hash |
| message_id | VARCHAR FK | References messages.id |
| file_path | VARCHAR | Absolute file path |
| edit_type | VARCHAR | `write`, `edit`, `multiedit`, `shell` |
| content | TEXT | New content or edit description |
| created_at | TIMESTAMP | Edit timestamp |

## Full-Text Search

FTS index on `messages` table covering `content` and `thinking` columns.

```sql
PRAGMA create_fts_index('messages', 'id', 'content', 'thinking', overwrite=1)
```

**Query with FTS:**
```sql
SELECT m.*, fts_main_messages.match_bm25(m.id, 'search term') as score
FROM messages m
WHERE score IS NOT NULL
ORDER BY score DESC
```

## Hybrid Search

`convos query` combines FTS (BM25) with vector similarity over the
`embedding` column using DuckDB's built-in `array_cosine_similarity`. Top-50
from each source is fused with Reciprocal Rank Fusion (`SUM(1/(60+rank))`),
the top-30 fused docs are reranked with Qwen3-Reranker-0.6B, and final order
is a position-tier blend of retrieval rank and reranker score: ranks 0-2 use
0.75/0.25, ranks 3-9 use 0.6/0.4, the rest use 0.4/0.6.

Embeddings are produced by embeddinggemma-300M (768d) with the
`task: search result | document:` prefix at index time and `query:` at query
time. Truncation only — no chunking — at 1600 chars.

Use `convos embed` to backfill missing embeddings without fetching from web
APIs. `convos sync` also embeds new or changed messages after upsert.

The `embedding` column is preserved across upserts when message `content`
is unchanged, so sync only re-embeds new or edited messages.

## ID Generation

All IDs are generated deterministically:

```python
def gen_id(source: str, oid: str) -> str:
    return hashlib.sha256(f"{source}:{oid}".encode()).hexdigest()[:16]
```

This ensures:
- Same record always gets same ID
- Upserts update rather than duplicate
- IDs are stable across syncs

## Common Queries

**Conversations with message counts:**
```sql
SELECT c.*, COUNT(m.id) as msg_count
FROM conversations c
LEFT JOIN messages m ON c.id = m.conversation_id
GROUP BY c.id
ORDER BY c.created_at DESC
```

**Search with context:**
```sql
SELECT m.content, c.title, c.source,
       fts_main_messages.match_bm25(m.id, 'python') as score
FROM messages m
JOIN conversations c ON m.conversation_id = c.id
WHERE score IS NOT NULL
ORDER BY score DESC
LIMIT 20
```

**Most edited files:**
```sql
SELECT file_path, COUNT(*) as edits
FROM file_edits
GROUP BY file_path
ORDER BY edits DESC
LIMIT 10
```

**Tool usage by source:**
```sql
SELECT c.source, tc.tool_name, COUNT(*) as uses
FROM tool_calls tc
JOIN messages m ON tc.message_id = m.id
JOIN conversations c ON m.conversation_id = c.id
GROUP BY c.source, tc.tool_name
ORDER BY uses DESC
```
