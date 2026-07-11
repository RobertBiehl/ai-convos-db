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
Version 1 does not provide a transparency log or MLS consensus, so an actively
malicious relay can still manipulate availability and membership views before a
later client-signed rotation.

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
a DuckDB file. Team keys are deliberately not in the recovery bundle: a new
team device remains pending until a workspace admin runs
`convos remote approve-devices WORKSPACE`.

`enable` adds lightweight Claude Code and Codex hooks and installs a persistent
user service: launchd on macOS and systemd on Linux. Hooks only touch a wake
marker. The worker performs Git inspection, encryption, network I/O, pulling,
and projection. Normal work never requires `convos remote sync`.

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
convos remote approve-devices backend
```

New members receive no old events or keys by default. `grant-selected`
republishes chosen signed evidence with its content key wrapped only to the
member's authorized devices. `grant-all` wraps old epoch keys to those devices
and resets their history boundary. User or device removal rotates the epoch.
Device removal is workspace-specific, so it does not disable the device's
personal workspace or unrelated teams. It cannot erase plaintext or keys
already obtained.

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
- `worker.log`, `last_error`, `wake`: operational state

Absolute checkout roots remain only in `state.db`. They are not placed in event
payloads, server storage, repository fixtures, CI artifacts, or logs.
