---
summary: "Specification for self-hosted, end-to-end encrypted personal and team synchronization over a global provenance graph."
read_when:
  - Implementing the remote protocol, server, client, or provenance projection
  - Reviewing encryption, membership, recovery, or sharing behavior
  - Verifying the remote synchronization Definition of Done
status: accepted (2026-07-10)
---

# Self-hosted encrypted synchronization

## Product goal

Convos synchronizes a global provenance graph across a person's computers and
authorized team members without giving the server plaintext. Normal use is
automatic: local hooks capture work, a background client publishes immutable
events, and other clients update local search and graph projections. Explicit
sync exists only for repair and backfill.

The graph is global in model, not globally readable. A client assembles the
union of personal, team, and eventually public subgraphs it is authorized to
decrypt. A workspace is an encryption and access-control projection, never the
owner or boundary of a conversation, changeset, repository, or file lineage.

## Architecture and trust boundary

```text
provider transcripts -> local convos DuckDB -> event projector -> encrypted outbox
                                                        |              |
                                                 local graph DB     HTTPS relay
                                                                       |
                                                               opaque event store
                                                                       |
                  local convos DuckDB <- event projector <- encrypted inbox
```

- DuckDB is a rebuildable local search projection, not a wire format.
- The local graph database is a rebuildable typed projection, not evidence.
- Signed immutable events are durable evidence.
- The server stores ciphertext, public device records, workspace ACLs, key
  envelopes, opaque event headers, cursors, invitation state, and quotas.
- Semantic search, embeddings, Git inspection, and graph queries execute only
  on authorized clients.

Server compromise can disclose accounts, workspace membership, device public
keys, event authors, epochs, timing, sizes, IP addresses, and access patterns.
It must not disclose conversation text, tool data, paths, repository URLs,
project names, embeddings, file contents, Git fingerprints, or workspace keys.
The protocol does not hide ciphertext length or prevent denial of service,
rollback, traffic analysis, endpoint compromise, or an authorized recipient
from retaining plaintext.

## Identities

- `user_id`: server account and human membership identity.
- `device_id`: one installation, with independent authentication token,
  Ed25519 signing key, and X25519 encryption key.
- `workspace_id`: personal or team encryption and ACL scope.
- `epoch`: monotonically increasing workspace-key generation.
- `event_id`: SHA-256 of the canonical signed event body.
- `entity_id`: author-scoped immutable origin identity; never assumed globally
  equivalent to an existing local conversation or provider id.
- `repository_id`, `file_id`, `changeset_id`: graph entities asserted by events,
  independent of a workspace and allowed to cross repository boundaries.

Absolute checkout roots and device filesystem identities are local-only. A
checkout maps a local root to repository evidence. Shared paths are repository
relative or opaque external-file identities.

## Cryptographic profile v1

Use `cryptography` recipes and primitives, never local crypto implementations:

- Ed25519 signs device certificates and event bodies.
- X25519 + HKDF-SHA256 + AES-256-GCM seals workspace keys to devices.
- AES-256-GCM encrypts signed event bodies with a fresh 96-bit random nonce.
- SHA-256 identifies canonical event bodies and ciphertext blobs.
- A random 256-bit recovery key encrypts the user root and personal-workspace
  keyring. Team keys require administrator-approved device envelopes.

Canonical JSON uses UTF-8, sorted keys, compact separators, no NaN, and integer
protocol versions. The envelope header is AEAD associated data. It binds the
protocol version, workspace, epoch, event id, author device, and device
sequence. The decrypted event author and id must equal the header. The event
signature must verify against the registered author key.

This profile provides authenticated encryption, authorship, tamper detection,
and future-key exclusion after epoch rotation. It does not provide message
ratcheting or forward secrecy after compromise of an old workspace key. MLS is
a possible later key-management profile, not a v1 dependency.

## Event format

```json
{
  "v": 1,
  "id": "sha256(canonical body without id/signature)",
  "kind": "conversation.record",
  "entity": "origin-scoped id",
  "revision": "sha256(canonical payload)",
  "author": "device id",
  "seq": 42,
  "parents": ["previous device event", "causal input event"],
  "observed_at": "RFC3339 UTC timestamp",
  "payload_v": 1,
  "payload": {}
}
```

The signature covers every field except `signature`; the event id covers the
body before `id` and `signature` are added. Unknown kinds and payload versions
are retained and forwarded even when the local projector cannot interpret
them. Events are never edited or deleted. Corrections, identity links,
retractions, key changes, and tombstones are new events.

Required initial kinds:

- `conversation.record`, `message.record`, `tool.record`, `attachment.record`,
  `artifact.record`, `file_edit.record`
- `repository.observed`, `git.checkpoint`, `file.version`, `edit.observed`
- `changeset.observed`, `changeset.member`, `identity.assertion`
- `capture.gap`, `workspace.policy`, `workspace.membership`

## Envelopes and idempotency

An event is signed, then encrypted independently under its workspace epoch key.
The server accepts `PUT(workspace_id, event_id)` only from an active device in
that workspace. Repeating the identical upload succeeds without allocating a
new cursor. A different ciphertext for an existing event id is rejected.

The server assigns an increasing delivery cursor after atomically persisting an
envelope. Pull is `events after cursor`, may return duplicates or out of order,
and carries no semantic ordering guarantee. Clients deduplicate by event id,
verify before persistence, and project in deterministic `(observed_at, id)`
order where no causal relationship exists. Per-workspace, per-device `seq` and
previous-event parents detect replay and interior gaps in either arrival order;
clocks never establish causality.

Large event bodies and attachments use encrypted blobs. Pull returns manifests;
clients fetch bodies eagerly only when required for the active local projection
and fetch attachments on demand.

## Membership, devices, and recovery

The first device creates a user root, personal workspace, epoch key, and random
recovery key. Server login tokens authorize API access but cannot sign events or
decrypt data. The user root signs device certificates. An existing device or a
recovered user root can enroll another device.

Workspace admins sign membership events. Every add or removal advances the
epoch and creates key envelopes for every currently authorized device. Removed
devices receive no new envelope and cannot decrypt future events. They retain
all plaintext and old keys already obtained.

New members receive only the new epoch by default. Complete-history grant seals
selected old epoch keys to their devices. Selected-history grant republishes
chosen immutable events under the current epoch so unrelated old content and
keys are not disclosed.

The recovery bundle contains the user root private material and personal
workspace keyring, encrypted by the recovery key and stored as an opaque server
blob. Team keys are excluded: a newly recovered team device must receive the
current epoch through an administrator-approved rotation. Loss of every
enrolled device and the recovery key is permanent personal-workspace data loss.

## Automatic personal and team projection

Personal policy is `all`: every captured conversation record and provenance
event is encrypted to the personal workspace without path or repository
allowlists. Users can opt out with local exclusions, but never need to opt in.

Team policy is based on repositories and path roots, not manual conversation
sharing. Each turn and tool/edit event is independently associated with zero or
more repositories. A conversation and changeset may span any number of them.
When a changeset touches several repositories selected by one workspace, it is
published as one changeset. When it crosses access boundaries, each workspace
receives an encrypted projection containing its allowed nodes and an opaque
boundary edge, never private paths or content.

## Git and file evidence

Git is a durable checkpoint layer below the temporal resolution of edit events.
A repository observation contains encrypted normalized remote fingerprints,
root/anchor commit ids, and ancestry evidence. Logical repository identity uses
normalized non-local remotes when available, while a separate lineage id uses
root commits. Clones can match; forks remain distinct repositories connected by
shared lineage. URLs and absolute roots are never server-visible.

`git.checkpoint` records HEAD, index/worktree digest, branch, and observed file
versions. Fine-grained edits link versions between checkpoints. Replaying
captured edits from checkpoint A to B proves completeness only when resulting
hashes match. Otherwise the projector emits `capture.gap` with relation
`unobserved_change`; it never assigns a prompt or tool.

File identity is evidence-based:

- equal content is `same_content`, not automatically the same file;
- explicit captured move can assert `same_lineage` exactly;
- copy creates a distinct file with `copied_from`;
- generation creates `generated_from`;
- Git similarity creates a reversible `inferred_rename` assertion;
- user confirmation is an explicit signed assertion.

## Stable application contract

Applications consume typed projection APIs/views, not envelopes. Initial views:

- `file_history`
- `changeset_files`
- `conversation_changes`
- `commit_conversations`
- `repository_activity`
- `identity_assertions`
- `capture_gaps`

The protocol package owns canonical encoding and crypto. The server owns opaque
storage and ACLs. The remote client owns enrollment, keyring, sync, background
work, and DuckDB projection. The provenance package owns Git inspection and the
typed graph projection. None belongs in the 1,000-line core CLI.

## Operational invariants

- Hooks enqueue only local identifiers and return without network or Git work.
- Background workers scan, enrich, encrypt, upload, pull, verify, and project.
- Retrieval never waits for the network.
- Explicit sync reconciles the full local archive with the event ledger.
- Server database and blob directory are backed up together from a consistent
  snapshot and restored without decryption.
- Real archives and evaluation datasets remain local and untracked.

## Completion evidence

Automated acceptance must run one server, two devices for one user, and a second
user with two devices. It must prove enrollment, automatic personal delivery,
team policy projection, default-no-history invitation, explicit selected/all
history, removal and rotation, tamper rejection, retry idempotency, offline and
out-of-order convergence, crash recovery, backup/restore, empty projection
rebuild, cross-repository changesets, checkpoint gaps, local-only queries, lazy
blobs, and hook p95 below 100 ms. The test uses synthetic fixtures; a separate
local-only benchmark may use the real archive and must publish no content.

## Non-goals

No Git hosting, server plaintext search, searchable encryption, enclave compute,
perfect attribution of unobserved changes, automatic resolution of every
identity ambiguity, public federation, mobile/web client, generalized graph
database, or claim of production-grade cryptographic assurance without an
independent review.
