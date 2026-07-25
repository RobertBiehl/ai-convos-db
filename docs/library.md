---
summary: "Private read-only browser and deterministic session replay for the local archive."
read_when:
  - Browsing conversations as a human
  - Understanding Library privacy boundaries
---

# Conversation Library

`ai-convos-library` is a human-facing browser and bounded session flight
recorder over the existing local archive. It adds no schema and has no write
API.

## Install and run

```bash
uv tool install "ai-convos-db[library]"
convos library
```

The command prints a fresh tokenized URL, opens it by default, and serves only
on `127.0.0.1`. Keep the terminal process running while using the page; press
Ctrl-C to stop it. Use `--no-open` to print the URL without launching a browser,
or `--port 8123` to request one loopback port. Port `0`, the default, lets the
operating system choose an available port.

The page supports:

- exact-word BM25 search, available without embeddings;
- optional semantic + exact hybrid search using cached local embeddings;
- source, role, recent-day, and project-directory filters;
- one strongest turn from each matching conversation;
- a bounded 40-message replay centered on the selected hit;
- expandable tool input/output, status, duration, and before/after file edits
  nested under the exact originating message.

Hybrid mode never downloads a model. Run `convos embed` explicitly once if the
retrieval model or message embeddings are missing. Exact-word mode remains
available without that setup.

## Deterministic CLI replay

Use the same evidence without a browser:

```bash
convos replay CONVERSATION_ID
convos replay CONVERSATION_ID --around MESSAGE_ID -n 40 --activity 120
convos replay CONVERSATION_ID -f json
```

Conversation and message IDs may be unique prefixes. Replay uses the core
hit-centered message reader, then joins only tool calls and edits whose exact
message IDs are in that bounded window. Events are ordered by message position,
event timestamp, and stable ID. Superseded message history, orphan activity,
thinking, artifacts, and attachments are not included.

`-n` bounds messages, `-c` independently clips every message and activity
payload, and `--activity` caps the combined tool/edit count. The text and JSON
results explicitly mark activity truncation. A replay is evidence of what was
captured, not proof that an absent tool or edit never happened.

## Privacy and security boundary

The server is loopback-only, read-only, and uses a new unguessable URL token on
every run. It serves no third-party assets, sends no archive text to a network
service, disables caching, and renders archive fields as text rather than HTML.
The API bounds query length, result count, snippets, message payloads, replay
size, and combined tool/edit activity.

The token prevents accidental access and ordinary cross-site requests; it is not
an authentication system against malware or another hostile process running as
the same operating-system user. Conversation titles, excerpts, tool
inputs/outputs, edit contents, paths, IDs, and opened turns are present in the
browser process and its memory. Do not expose
the port through a proxy, share the URL, or use the page on a machine where
untrusted local software can read your browser. Closing the tab does not stop
the server; Ctrl-C does. Restarting invalidates the old URL.

Library does not edit, delete, export, sync, or summarize conversations. Search
results are bounded retrieval evidence, not a claim that the archive is
complete or current; use `convos doctor` and `convos sync` when freshness
matters.
