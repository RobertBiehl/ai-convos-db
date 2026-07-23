---
summary: "Install, operate, recover, and use the self-hosted encrypted Convos remote."
read_when:
  - Setting up personal multi-computer synchronization
  - Operating the self-hosted relay
  - Creating a team workspace or managing membership
  - Backing up or recovering remote state
---

# Encrypted remote

The optional remote application synchronizes signed encrypted events. The relay
never receives conversation plaintext, file paths, repository names, embeddings,
attachments, or workspace keys. Search and graph queries remain local.

This is a security-sensitive preview. It uses established primitives through
`cryptography` and has protocol/acceptance tests, but has not received the
independent review required before calling it production-grade encryption.
Payloads and keys remain opaque to a passive relay or database compromise.
Membership, roles, device certificates, removals, history entitlements, epoch
key commitments, and approvals are carried in a client-signed hash chain.
Clients pin the chain they have observed and reject rollback, forks, invalid
transitions, relay metadata that disagrees with the signed head, and keys
outside the signed device entitlement or commitment. A workspace omitted by
the relay is excluded from upload rather than used with stale state. Version 1 does
not provide cross-client gossip or an external transparency log, so a malicious
relay can still withhold updates or partition clients that have not compared
their pinned heads.

## Install from this repository

Install the core and client applications together:

```bash
uv tool install --reinstall "git+https://github.com/RobertBiehl/ai-convos-db.git" \
  --with "ai-convos-remote @ git+https://github.com/RobertBiehl/ai-convos-db.git#subdirectory=apps/remote"
```

For a local checkout, `uv sync --all-extras` installs the same workspace.

Runnable personal and team demonstrations are available under
[`examples/remote`](../examples/remote/README.md). They use synthetic temporary
data and a loopback relay, never the normal conversation archive.

## Run the server

The relay is a single process backed by SQLite. Run it behind an HTTPS reverse
proxy; device bearer tokens must not traverse an untrusted network over HTTP.

```bash
uv tool install --reinstall \
  "ai-convos-remote-server @ git+https://github.com/RobertBiehl/ai-convos-db.git#subdirectory=apps/remote_server"
install -d -m 700 ~/.local/share/convos-server
convos-server serve \
  --db ~/.local/share/convos-server/server.db \
  --host 127.0.0.1 --port 8787
```

For a persistent Linux user service, place this in
`~/.config/systemd/user/convos-server.service`:

```ini
[Unit]
Description=Convos encrypted relay

[Service]
ExecStart=%h/.local/bin/convos-server serve --db %h/.local/share/convos-server/server.db --host 127.0.0.1 --port 8787
Restart=on-failure

[Install]
WantedBy=default.target
```

Enable it with `systemctl --user daemon-reload` followed by
`systemctl --user enable --now convos-server`. On a headless host, enable user
service startup without an interactive login using `loginctl enable-linger`.

Terminate TLS with Caddy, nginx, or another reverse proxy and expose only the
HTTPS endpoint. The server itself has no TLS or public-network configuration.
The client rejects plaintext HTTP except on loopback;
`CONVOS_REMOTE_INSECURE=1` is only for a trusted test network.

## Personal multi-computer setup

On the first computer:

```bash
convos remote setup https://convos.example.com robert --device macbook
convos remote enable
convos doctor
```

`setup` creates the user root, device identity, personal workspace, epoch key,
and recovery key. Store the printed recovery key offline and share the printed
user ID through an authenticated channel before joining a team. Personal policy
is always `all`: no path or repository allowlist is required.

On another computer:

```bash
convos remote recover https://convos.example.com robert --device workstation
convos remote enable
```

The CLI prompts for the recovery key without placing it in shell history or the
process list. Recovery enrolls a new independently signed device, restores the
complete personal history, and rotates the personal workspace. It never copies
a DuckDB file. Team keys are deliberately not in the recovery bundle. A
recovered device remains pending in each team workspace until the same user
authorizes it from an existing device or the other represented team members
approve it.

`enable` installs the standard Convos conversation-capture hooks and a
persistent user service: launchd on macOS and systemd on Linux. Each lifecycle
event has one `convos capture <agent>` command; obsolete remote wake-only hooks
are removed automatically. The hook only updates the local archive. The worker
observes those changes and performs Git inspection, encryption, network I/O,
pulling, and projection. Normal work never requires `convos remote sync`.

## Team workspaces

Users create their own account before an administrator adds them:

```bash
# New team member, on one of their devices
convos remote setup https://convos.example.com alice --device laptop

# Administrator; use Alice's out-of-band user ID, not a directory name
convos remote workspace backend
convos remote invite backend ALICE_USER_ID
convos remote link ~/src/backend backend
```

Name lookup is a convenience for a trusted relay. An out-of-band user ID binds
the invitation to the intended root key even if the relay directory is later
compromised. Before wrapping any workspace or history key, clients verify each
device's user-root-signed certificate and its signing and encryption keys.

Linking a Git checkout stores its checkout root only on that device. Encrypted
root-commit and remote evidence identifies other clones. A linked non-Git path
uses a local root binding. Every relevant turn, tool, edit, checkpoint, and
changeset is shared automatically after the one-time link.

A changeset may span several repositories. Each edit is authorized separately.
When only part belongs to a workspace, recipients get allowed edits plus an
opaque boundary record, never private paths or content.

Membership and history:

```bash
convos remote grant-selected backend alice EVENT_ID [EVENT_ID ...]
convos remote grant-all backend alice
convos remote remove backend alice
convos remote remove-device backend DEVICE_ID
```

New members receive no old events or keys by default. `grant-selected`
republishes chosen signed evidence with its content key wrapped only to the
member's authorized devices. `grant-all` wraps old epoch keys to those devices
and resets their history boundary. User or device removal rotates the epoch.
Device removal is workspace-specific, so it does not disable the device's
personal workspace or unrelated teams. It cannot erase plaintext or keys
already obtained.

### Device approval

A workspace is one independently encrypted sync scope: the automatically
created personal workspace or one named team workspace such as `backend`. It
has its own signed member/role map, authorized device roster, removal
tombstones, key epochs, and history policy. Approval for one workspace grants
nothing in another.

On the pending device:

```bash
convos remote request-device backend
```

The request signs the exact workspace ID, current signed-state hash and epoch,
user ID, device ID, root-certified signing/encryption keys, certificate hash,
nonce, activation time, and expiry. It grants nothing by itself.

An existing device belonging to the same user can approve it immediately:

```bash
convos remote approve-device backend DEVICE_ID
```

The new device inherits that device's workspace access, the user's existing
role, and the same history-inheritance flag. No administrator action is needed.
Selected-history evidence is rewrapped to the new device; a durable local
outbox retries that publication after a crash. An explicit rejection
invalidates the proposal, and an explicitly removed device ID cannot use this
path or be reauthorized.

If the user has no authorized device in the workspace, authorization requires a
strict majority of the other users represented by authorized devices in the
signed roster. Each user gets one vote even if they have several devices; the
requesting user is excluded. Every voter runs the same `approve-device`
command. The final vote atomically advances the signed state and rotates the
workspace epoch. In a two-user team this is one vote from the other user, with
a one-hour activation delay enforced against the relay's clock and stored
proposal window; a client-supplied approval timestamp cannot bypass or revive
it. A one-user team with no authorized device has no electorate and cannot use
team voting.

Majority recovery is future-only. It restores the user's existing membership
and role but does not silently release older keys. History grants remain
administrator-controlled with `grant-selected` and `grant-all`. Alternatively,
the recovered device can ask the other represented users to activate the
history entitlement it already had:

```bash
# Recovered device
convos remote request-history backend

# Other represented users, one vote each
convos remote approve-history backend DEVICE_ID
```

History activation is a separate signed majority decision and does not change
membership or role. It can only install epoch keys held by the device that
finalizes the vote and selected evidence available to that device; voting
cannot recreate material that no remaining device has.
On the recovered device, the next ordinary sync detects that its earliest
available epoch moved backward, rewinds the delivery cursor, and idempotently
imports all newly decryptable events. `convos remote approvals backend` shows
active device and history proposals.

## Daily operation

```bash
convos doctor
convos remote doctor
convos remote graph repository_activity --arg REPOSITORY_ID
convos remote graph conversation_changes --arg CONVERSATION_ID
convos remote fetch                 # materialize deferred large events
convos remote sync                  # explicit repair/backfill only
```

The worker writes errors to `<root>/remote/last_error` (by default,
`~/.convos/remote/last_error`). Queries never wait for the server. `doctor`
reports connectivity, identity, workspaces, epochs, pending uploads, deferred
events, and last successful synchronization.

## Backup and restore

Back up a consistent server snapshot while it is running:

```bash
convos-server backup \
  --db ~/.local/share/convos-server/server.db \
  --output ~/backups/convos-server.db
```

Restore by stopping the relay, replacing its database with the snapshot, and
starting it again. The backup contains ciphertext, ACL metadata, key envelopes,
and delivery cursors, but no workspace key. Clients can safely retry uploads and
pulls after rollback because event insertion and local projection are
idempotent.

Client recovery requires the server backup plus the user's recovery key. Loss
of every enrolled device and the recovery key is permanent data loss.

## Local files

Remote client state lives under `<root>/remote/` (by default,
`~/.convos/remote/`):

- `config.json`: mode `0600`, device private keys, token, encrypted-workspace
  keyring, and local workspace labels
- `state.db`: signed event ledger, cursors, outbox, typed graph projection,
  local checkout mappings, and deferred-event manifests
- `worker.log`, `last_error`: operational state

Absolute checkout roots remain only in `state.db`. They are not placed in event
payloads, server storage, repository fixtures, CI artifacts, or logs.
