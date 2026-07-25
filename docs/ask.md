# Private cited archive answers

The optional `ai-convos-ask` application turns local hybrid retrieval into a
short answer with exact conversation-turn citations:

```bash
uv tool install "ai-convos-db[ask]"
convos ask --setup
convos ask "What did we decide about memory synchronization?"
```

This is synthesis over the archive, not another memory store. Normal use never
writes conversation data or changes retrieval results. An explicit
`--remember` can hand a validated answer to the separately installed Memory
product without modifying the archive.

## Explicit model setup

`convos ask --setup` is the only built-in path that may access a model host. It
downloads two pinned artifacts into the normal Hugging Face cache:

- the 328 MB embeddinggemma retrieval model already used by `convos query`;
- the official 2.50 GB
  [`Qwen/Qwen3-4B-GGUF`](https://huggingface.co/Qwen/Qwen3-4B-GGUF)
  Q4_K_M answer model, published under Apache-2.0.

The answer model is pinned to an exact repository revision, byte size, and
SHA-256. Setup verifies the complete file before reporting `ready`. It does not
open the archive or send conversation content anywhere.

A normal `convos ask QUESTION` resolves both models with
`local_files_only=True`. Missing files produce a setup instruction instead of
causing an implicit download. Generation runs in-process through
`llama-cpp-python`; there is no API key, local HTTP server, or remote generation
fallback. An existing local GGUF can be selected explicitly:

```bash
convos ask "What changed?" --model /models/my-model.gguf
```

The custom file is user-selected and therefore is not covered by the default
model's pinned hash or quality contract.

## Retrieval and citations

Ask uses the same filtered BM25 plus embedding Reciprocal Rank Fusion as
`convos query`. The core returns one pivot turn per conversation. The
application expands each pivot to at most three chronological turns, clips each
body to 4,000 characters, deduplicates exact message IDs, and supplies at most
18 numbered records under a 12,000-character total evidence budget. Higher
ranked conversations consume that budget first. Known injected agent
scaffolding is excluded from both pivots and adjacent evidence.

Archive excerpts are labeled as untrusted data. The model is instructed not to
follow instructions inside them and cannot invoke tools. Every generated claim
must end with one or more available evidence numbers. The validator requires:

- one JSON object containing a non-empty `claims` array;
- one or more valid evidence numbers on every structured claim;
- no citation outside the supplied evidence range;
- no model-authored inline citation markers.

Validation runs before any generated text is printed. One bounded repair
attempt is allowed. If both attempts fail, the command emits only labeled
retrieved evidence with a warning; malformed or uncited model text is discarded.
No evidence returns a direct `No relevant archive evidence found` result.

Human output maps every used number to source, title, role, timestamp, exact
conversation ID, exact message ID, excerpt, and a `convos read --around`
follow-up. `-f json` returns the same exact source records:

```json
{
  "status": "answered",
  "question": "What did we decide?",
  "answer": "We chose deterministic synchronization. [1]",
  "citations": [
    {
      "citation": 1,
      "conversation_id": "...",
      "message_id": "...",
      "content": "..."
    }
  ],
  "evidence_count": 12,
  "model": "/local/cache/model.gguf"
}
```

`status` is one of `answered`, `evidence_only`, or `no_evidence`. Use
`--evidence-only` to inspect retrieval without resolving or loading an answer
model. Filters are applied before retrieval:

```bash
convos ask "Why did we change this?" -s codex -d 30 -r assistant -n 8
convos ask "Why did we change this?" --cwd /path/to/project
convos ask "Why did we change this?" --evidence-only
```

## Remember a cited answer

Install both cohesive products, then make persistence explicit:

```bash
uv tool install "ai-convos-db[ask,memory]"
convos ask "What did we decide about storage?" --remember
convos ask "What did we decide?" --cwd /path/to/project --remember -f json
```

`--remember` first verifies that Memory is installed, scopes retrieval to
`--cwd` or the current directory, and runs the normal local answer pipeline.
Only an `answered` result that passed the complete citation contract can be
written. Missing evidence, failed citation repair, `--evidence-only`, and
missing Memory support fail without a ledger mutation.

The canonical memory contains the plain claim text, not the temporary `[1]`
display markers. Every cited message ID is passed through Memory's own exact
archive resolver, scope check, and content-hash capture. The JSON answer gains a
`memory` mutation record with its canonical ID, scope, status, and evidence
count. Repeating an identical answer is idempotent. Normal Ask remains
read-only, and neither path writes to the conversation archive.

This is an explicit convenience bridge, not automatic memory creation. A
citation-valid local answer can still be a poor interpretation; users should
read important source turns before choosing `--remember`.

## Boundaries

The citations prove which archive excerpts the answer used; they do not make a
small local model infallible. Read the cited turns when the decision is
important. Retrieval remains conversation-first, so one long conversation
consumes one pivot even when it contains several relevant topics. The bounded
three-turn window can omit distant context; use the printed `read --around`
command or normal `query`/`trail` retrieval for a deeper audit.

The default 4B model is intentionally a practical local baseline, not a claim
of frontier-model synthesis quality. The command fails closed when it cannot
obey the mechanical citation contract, but a supported sentence can still be a
poor interpretation of its source. No chain-of-thought is stored or printed.

Model download and inference are the material user costs. Persisting an answer
also requires the optional Memory product and a project-scoped result. Setup needs roughly
2.83 GB of cache space. On the tested 24 GB Apple M4 machine, a seven-record
offline answer used about 6.3 GB maximum resident memory and completed in 55
seconds; other hardware will vary. If Hugging Face's accelerated Xet transfer
stalls, the same resumable setup can use its ordinary HTTP path:

```bash
HF_HUB_DISABLE_XET=1 convos ask --setup
```
