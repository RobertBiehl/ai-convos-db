---
summary: "Reproducible offline literal and hybrid retrieval evaluation with exact archive relevance judgments."
read_when:
  - Measuring conversation retrieval quality
  - Comparing literal and hybrid search
  - Changing search ranking or filters
---

# Retrieval evaluation

`convos eval` measures whether literal or hybrid retrieval returns exact
conversation/message identities that a user marked relevant. It turns retrieval
quality into a reproducible gate instead of tuning embeddings or prompts by
intuition.

Install the optional product:

```bash
uv tool install "ai-convos-db[eval]"
```

Create a private JSONL file with one judgment per line:

```json
{"name":"memory decision","query":"how should concurrent memory changes be resolved","expect":["b8859834"],"cwd":"/path/to/project"}
{"name":"exact error","query":"Conflicting lock is held","expect":["09a1"],"mode":"literal","source":"codex","k":5}
```

`name`, `query`, and a non-empty `expect` list are required. Each expected value
is an at-least-eight-character prefix of an acceptable conversation or message
ID. Before retrieval, every prefix must resolve to exactly one live archive
record; missing and ambiguous ground truth fails closed. Optional fields are
`mode` (`hybrid`, `literal`, or `both`), `source`, `days`, `role`, `cwd`,
`conversation`, and per-case `k`.

Run one engine or compare both:

```bash
convos eval private-retrieval.jsonl
convos eval private-retrieval.jsonl --mode both -k 8
convos eval private-retrieval.jsonl --mode hybrid --min-hit-rate 0.9 -f json
```

The report includes hit@k, mean reciprocal rank (MRR), per-case rank, returned
IDs/scores, errors, and full-ID `convos read --around` pivots. It never includes
message content. `--min-hit-rate` makes the command exit nonzero when any
evaluated engine falls below the requested gate. Retrieval errors also produce
a nonzero exit.

Hybrid evaluation is strictly local: it will not download the embedding model.
Run `convos embed` explicitly first if the model or message embeddings are
missing. Literal evaluation exercises the real BM25 command and current FTS
index. Both engines use the same source/day/role/project/conversation filters as
normal retrieval.

## What users must supply and maintain

There is no honest automatic ground truth. Users must choose exact relevant IDs,
and those judgments can be incomplete, biased toward known sessions, or stale
after imports change. An expected prefix must remain unambiguous enough to mean
what its author intended. A high score proves performance only on the supplied
cases; it does not prove that every archive question works.

Keep judgment files private when their query text, project paths, or IDs reveal
sensitive work. The evaluator reads them locally and sends nothing over the
network. Commit only sanitized shared suites.
