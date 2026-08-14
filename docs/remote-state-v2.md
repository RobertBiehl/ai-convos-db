---
summary: "Implementation plan for canonical provenance, rebuildable sync metadata, and a content-free settled remote state."
read_when:
  - Changing remote projection, recovery, or relay protocol
  - Changing provenance ownership or storage
  - Migrating remote state
  - Reviewing remote correctness, performance, or storage
---

# Remote state v2

## Outcome

Remote state v2 keeps the existing DuckDB plus SQLite design, but removes
overlapping authority:

- DuckDB is the canonical archive, including canonical provenance facts.
- DuckDB carries a stable archive ID and a transactional monotonic generation;
  device config pins the last generation that completed remote convergence.
- `state.db` is a rebuildable synchronization index. Pending ciphertext lives
  in mode-0600 outbox files referenced by metadata rows.
- The relay is the durable encrypted signed-event ledger.
- Losing `state.db` never turns imported rows into local rows and never permits
  publication until relay metadata has been rebuilt to the advertised tail.

The client and relay advance together. There is no compatibility layer,
negotiation, or fallback for the old relay protocol. Core DuckDB upgrades are
additive and automatic. Remote state is replaced and rebaselined, not migrated.

## Responsibilities

### Core DuckDB

DuckDB owns user-visible canonical content:

- conversations, messages, tool calls, attachments, artifacts, and file edits;
- repository, checkout, and file identity;
- file versions and exact edit-to-file relationships;
- Git checkpoints and checkpoint-to-edit evidence;
- identity assertions and capture gaps.
- durable `remote.row_origins` attribution for every remotely projected archive
  row, written atomically with that row.
- the single-row `archive_state` identity and generation used to detect file
  replacement and rollback without hashing the archive on every sync.

Canonical provenance uses existing archive identities. It does not duplicate
prompt text, message content, changesets, or file edits. Prompts are resolved
from `messages`; changesets reference the existing message or turn; provenance
edges reference `file_edits.id`.

DuckDB never stores remote cursors, retry state, workspace policy, event
envelopes, or publication receipts. Row origin is the narrow exception because
it determines authorship and whether an archive row may ever be published.
The archive generation is another narrow safety primitive: every core archive
or provenance transaction advances it, and rollback advances are rolled back
with the content transaction.

### Device config

`config.json` owns device keys, workspace keys, pinned signed controls, and the
last archive ID/generation that completed an entire sync. The archive proof is
kept here, rather than only in rebuildable `state.db`, so deleting `state.db`
cannot erase rollback detection. A content-free copy in `state.db` provides a
second safety anchor against a stale config write. Neither copy contains archive
row content, and the proof is not included in a recovered device's server-side
recovery bundle.

### Changegraph

Core captures Git enrichment during every ingestion path and writes the
canonical provenance contract independently of installed applications.
Changegraph is strictly read-only: it owns exact graph queries, blame,
timelines, and presentation, and does not maintain another graph database.

The remote product submits verified typed archive rows, origins, and provenance
facts through core projectors. Remote event shapes do not become core API and
Remote contains no DuckDB mutation SQL.

### Remote state

Long-lived `state.db` rows contain only:

- lifecycle and schema version;
- the cached last-verified archive ID/generation;
- workspace cursors and advertised relay tail;
- `(workspace, owner user, entity) -> (current revision, event)` publication
  heads;
- current imported entity heads keyed by author user, with the signing device
  retained only in event receipts and DuckDB origin attribution;
- exact `(workspace, author, sequence) -> event_id` sequence identity;
- compact parent data only for unresolved out-of-order gaps;
- selected-history event IDs and content-free original-to-carrier mappings;
- lazy/deferred event manifests, policies, retries, and last failure;
- content-free acknowledged event receipts.

The only content-bearing remote working state permitted is an unacknowledged
encrypted outbox file. Plaintext event JSON, acknowledged envelopes,
selected-history material, attachment chunk bodies in SQLite, raw provenance
JSON, prompts, and other derived content are forbidden.

### Relay

The relay owns durable opaque envelopes and encrypted blobs. It reports:

- the exact workspace tail;
- the earliest retained cursor available to the requesting device;
- event envelopes or lazy manifests after a cursor;
- exact envelope fetch by event ID;
- self-contained author-signed purge certificates where eligible personal
  memory bodies were removed. Each certificate binds the original envelope
  header to a retained later signed deletion event, closes the original author
  sequence only after client verification, and permanently denies replay.

The relay never interprets payloads. Server database and blob storage are one
backup unit.

## Canonical provenance schema

Core initializes a `provenance` DuckDB schema with these relations:

- `repositories`
- `repository_checkouts`
- `files`
- `file_versions`
- `file_edit_files`
- `git_checkpoints`
- `checkpoint_edits`
- `assertions`
- `capture_gaps`

Core also initializes `remote.row_origins`, a compact canonical attribution
relation containing the physical row, source row, workspace, author user and
device, signed source event, logical content key, and observation time. It
contains identifiers only, never event bodies or duplicated archive content.
Imported physical IDs derive from `(workspace, author user, table, source row)`.
The verified device certificate supplies the user; projection fails when that
mapping is absent. Content equality is never used to infer row identity.

Local absolute checkout paths are local-only and are never serialized to a
remote canonical event. Remote workspace boundaries and device activity remain
remote metadata or views, not canonical archive facts.

Semantic identities are independent of the signing device:

- edit identity is `file_edits.id`;
- changeset identity is the existing message or turn identity;
- file identity is repository plus normalized relative path;
- checkpoint identity is repository plus Git head and working-tree state hash;
- author and device belong to signed-event metadata.

Revisions remain `digest(payload)`, but historical revisions are not heads.
Publishing compares a local payload only with the current user-scoped head, so
an exact `A -> B -> A` transition emits the final `A` while unchanged scans do
not emit duplicates. Acknowledged event receipts remain separate history.

## Lifecycle and recovery

Every configured state is exactly one of:

```text
UNINITIALIZED -> REBASELINING -> READY
                         \----> BLOCKED
```

Only `READY` may discover and publish new local archive revisions.

An absent, incompatible, or incomplete state with configured workspaces enters
`REBASELINING`. Recovery:

1. Acquires the same exclusive lock used by the worker and explicit sync.
2. Refreshes the complete signed control chain. Every epoch boundary commits
   the exact relay tail plus each author's `(sequence, event_id)` head.
3. Starts full-history replay at genesis, or limited-history replay at the
   signed boundary for the first entitled epoch.
4. Decrypts, authenticates, validates sequence identity, and materializes each
   event.
5. Rebuilds acknowledged receipts, publication and import heads, exact
   sequences, and cursors while projecting archive rows and their canonical
   DuckDB origins.
6. Verifies that the advertised accessible tail was reached, every applicable
   signed author checkpoint is present, and no required sequence gap or
   unsupported required event remains.
7. Commits `READY`.

An unavailable relay, missing key, sequence conflict, or incomplete retained
history enters `BLOCKED`. Local retrieval remains available. Publication stays
disabled until recovery succeeds or the user explicitly creates a new remote
history.

Archive recovery is deliberately asymmetric:

- With an intact archive, deleting `state.db` rebuilds only sync metadata and
  adopts the existing DuckDB rows and durable origins.
- A missing or genuinely empty archive replays the personal ledger under the
  original row IDs, restoring owned canonical rows without marking them as
  imports or publishing them again.
- A different archive ID or generation below the pinned value is never
  overwritten. Relay rows are recovered additively under foreign IDs beside
  the suspect native rows, and the workspace remains `BLOCKED`, so stale rows
  cannot be published as reversions.

Recovery mode remains durable across crashes until the final pull and archive
proof commit. Retrying a blocked rollback does not replay or duplicate the
additive copy. Resolution is recoverable and explicit: preserve the suspect
DuckDB, replace it with a fresh empty archive, then sync to perform native
relay restoration before reconciling any unsynchronized rows from the preserved
copy.

## Sync order

A ready synchronization cycle:

1. Acquire the process lock and refresh signed control state.
2. Retry already committed encrypted outbox entries.
3. Pull and project to the relay's advertised tail.
4. Read core-captured local canonical changes and reconcile them.
5. Atomically enqueue new signed encrypted events and update publication heads.
6. Upload the outbox.
7. Pull once more if upload or concurrent writers advanced the tail.
8. Report exact convergence or a nonzero partial/blocked result.

A rebaselining cycle performs only steps 1 through 3 until it becomes `READY`.
It cannot scan or publish the existing archive.

## Commit protocol

The relay is a redo log; no distributed transaction is required.

Incoming batches:

1. Decrypt and verify in bounded memory.
2. Apply idempotent DuckDB projection in one transaction.
3. Commit SQLite heads, receipts, sequences, and cursor in one transaction.

The cursor is always committed last. A crash after DuckDB commit replays the
same batch; deterministic upserts make the retry harmless. The cursor can lag
the projection but cannot lead it.

Outgoing events:

1. Sign and seal the event.
2. Atomically write its encrypted envelope to a mode-0600 file.
3. Commit the file reference, exact sequence identity, and publication head
   atomically in SQLite. A crash before this commit can leave only an
   unreferenced encrypted file; a committed row always has its file.
4. Upload idempotently by event ID.
5. Replace the outbox row with a content-free receipt, commit, and remove the
   encrypted file after acknowledgement.

## Content lifecycle

Outgoing plaintext is never written to `state.db`. Epoch rotation reopens a
pending envelope in memory and reseals it.

Incoming plaintext exists in memory only until DuckDB projection commits.
Dispatch is exact on `(kind, payload_v)`. Unknown families and unsupported core
versions are required by default and block publication; only receiver-known,
archive-isolated auxiliary families whose product is not installed may defer
without blocking core readiness. An installed but outdated product blocks its
workspace from publishing. Both leave a content-free manifest and remain
fetchable from the relay. When a handler becomes available, Remote replays from
the signed history boundary.

Selected-history grants fetch exact envelopes from the relay, decrypt and
verify them in memory, reseal them for authorized devices, upload, and discard
the material.

Attachment chunks remain encrypted until streamed to a mode-0600 temporary
file. Hash and size validation precede atomic rename to the final attachment
path. SQLite stores only the manifest and completion metadata.

## Exact sequence storage

Sequence safety is not approximated. State retains an exact event ID for every
author sequence so a second signed event at an old sequence is detectable.
Identifiers use exact content-free text columns. Composite metadata tables use
`WITHOUT ROWID`. Parent lists are retained only for unresolved gaps; contiguous
history retains exact sequence identity plus the current chain head.
Epoch-advancing control records sign `{epoch, tail, heads}` and the relay accepts
one only when it exactly matches the ledger inside the same write transaction.
Same-epoch history controls must retain that boundary. A limited-history client
seeds its sequence chains from the signed boundary; a full-history client starts
at genesis. Relay retention floors are delivery hints, never completeness
proofs. A relay can still withhold a newest suffix created after the last signed
boundary; detecting that requires gossip or an external transparency witness
and remains outside this release.

## Performance

- Pull responses include an exact tail; explicit sync loops until it reaches
  that tail.
- Decryption and signature validation are bounded batches.
- DuckDB uses staging tables and set-based upserts rather than per-event
  commits.
- SQLite uses transactions and `executemany`.
- The fast path consumes changed local identifiers; a full deterministic
  reconciliation remains the repair path.
- Hooks continue to perform no Git or network work and retain p95 below 100 ms.
- Background work may obey time and byte budgets; explicit sync converges in
  one invocation.

The fixed local projector release gate is at least 500 verified and projected
events per second on the reference machine with a deterministic 100,000-event
fixture. CI uses a relative regression threshold. A real-archive benchmark is
local-only and publishes no content.

If exact replay remains too slow, a later client may upload a signed encrypted
per-device metadata checkpoint. A checkpoint is verified and followed by exact
tail replay; it is an accelerator, never authority.

## Storage invariants

- Settled state has no plaintext or acknowledged envelope body columns.
- Persistent state size depends on identifier/event count, not payload length.
- Acknowledging otherwise identical 1 KiB and 1 MiB events leaves equal
  persistent metadata.
- Synthetic plaintext cannot be found in settled SQLite database or WAL files.
- The old raw-event and duplicate provenance stores are removed.
- State cutover records exact backup file bytes and hashes.

## Schema lifecycle

Core owns its DuckDB schema lifecycle. Every mutating core entry point runs the
idempotent schema initializer, which adds provenance, origins, and
`archive_state` to an existing archive without rewriting its conversation
tables. This is the frictionless user-data migration path and is covered by a
preservation test.

Remote state schema 7 has no compatibility transform in this pre-stability
release.
Inspection and doctor are side-effect-free. An absent state is initialized as
empty metadata. A mutating sync handles an incompatible or damaged regular
state under the normal exclusive sync lock:

1. Verify that signed relay control state is reachable; if not, leave the old
   state untouched.
2. Copy the exact SQLite database, WAL, and shared-memory files into a private
   backup bundle and verify every copy by SHA-256.
3. Write a manifest with exact sizes and hashes, then finalize the bundle.
4. Create and integrity-check a fresh current state beside the old file.
5. Atomically install the fresh state and retain the backup indefinitely.
6. Pull, authenticate, and project the relay through its advertised tail.
7. Enter `READY` before scanning or publishing any local revision.

The backup is deliberately restorable rather than a bespoke encrypted format.
It retains the old file's sensitivity, may contain legacy plaintext, is mode
`0600` inside a mode-`0700` directory, and is never deleted automatically.
Historical canonical facts stranded in a legacy state remain recoverable from
that bundle; this cutover does not make Remote or Changegraph parse the old
schema.

The relay protocol itself is not migrated. Client and server v3, backed by a
fresh relay database at SQLite `user_version=5`, are deployed together after
old workers are stopped.

## Product behavior

`convos remote sync` exits successfully only when every authorized workspace is
at its advertised tail with no required deferred event. It emits exact counts,
not guessed percentages or time estimates.

`convos remote doctor` reports lifecycle, pending, lazy, deferred, and required
deferred counts, last successful sync, and any retained cutover backup. For an
old state it reports the pending backup/rebaseline without opening it for writes.
A normal user never runs SQL, manually reseeds imported rows, fetches lazy
events, or vacuums SQLite.

## Stacked delivery

Implementation lands as reviewable one-commit pull requests:

1. Persist this contract.
2. Add lifecycle, recovery gating, relay tail, and pull-first synchronization.
3. Add batched crash-safe projection and full convergence.
4. Move canonical provenance to DuckDB and unify changegraph.
5. Introduce metadata-only state and relay-backed history/chunk handling.
6. Add safe state cutover, diagnostics, benchmarks, and release evidence.

Each pull request targets the branch immediately below it. No pull request
contains merge commits or fixup commits.

## Required acceptance

- Deleting state with DuckDB intact uploads zero rows before full rebaseline.
- Deleting state does not erase the archive rollback proof held in device
  config.
- Missing DuckDB recovery restores owned rows without imported attribution or
  relay echo.
- Non-empty DuckDB rollback recovers additively and remains blocked across
  retries.
- Imported rows never echo; one genuinely new local row publishes once.
- Recovery with an unavailable or incomplete relay fails closed.
- Crash injection at each DuckDB/SQLite/upload boundary converges exactly.
- Duplicate delivery and out-of-order delivery are idempotent.
- Conflicting event identities at one author sequence are rejected.
- Missing history before a later signed author checkpoint prevents `READY`.
- Explicit sync reaches the advertised tail in one invocation.
- Settled state contains no synthetic plaintext.
- State growth after acknowledgement is independent of payload size.
- Existing local archive queries remain offline and network-independent.
