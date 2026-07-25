# ai-convos-ask

Private, cited synthesis over an `ai-convos-db` archive:

```bash
uv tool install "ai-convos-db[ask]"
convos ask --setup
convos ask "What did we decide about memory synchronization?"
convos ask "What did we decide?" --remember
```

`--setup` is the only model-download path. It fetches the pinned local
embedding model and the official 2.50 GB Qwen3-4B Q4_K_M GGUF, then verifies
the generation model's exact size and SHA-256. A normal `ask` invocation
requires both models to be cached and makes no network request. Supply
`--model /path/to/model.gguf` to use another existing local model.

Retrieval uses the core BM25 plus embedding RRF pipeline, takes one pivot per
conversation, and expands each pivot into a bounded three-turn evidence
window. Conversation text is passed only to the in-process local model.
Archived text is explicitly labeled untrusted; generated structured claims
must name available evidence numbers, and the CLI renders those exact
citations. Malformed JSON, missing citations, or nonexistent citations fail
closed and produce labeled evidence instead of an uncited answer.

Use `--evidence-only` to inspect retrieval without loading a generation model,
or `-f json` for a structured answer and exact source records. Every source
includes the conversation and message IDs needed for `convos read --around`.

With `ai-convos-memory` installed too, explicit `--remember` persists only a
citation-validated answer. It scopes retrieval to `--cwd` or the current
directory, removes display-only citation numbers from the canonical text, and
attaches the exact cited turns through Memory's own resolver. Invalid synthesis
and missing Memory support fail before any ledger mutation; ordinary Ask stays
read-only.

See the [complete privacy, setup, and failure contract](../../docs/ask.md).
