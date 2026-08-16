# Semantic conversation exploration

The optional `convos-explore` product turns an exact conversation or message
into a local semantic neighborhood:

```bash
convos related CONVERSATION_ID
convos related MESSAGE_ID
convos related MESSAGE_ID -s claude-code -d 90 -r user -n 20
convos related MESSAGE_ID -f jsonl
convos trail MESSAGE_ID
convos trail MESSAGE_ID --depth 3 --width 4 --max-nodes 30 --min-score .7
convos trail MESSAGE_ID -f dot
```

Every result is another conversation plus its strongest matching embedded
turn, similarity, provider, timestamp, working directory, and exact IDs. Text
output includes a ready-to-run `convos read CONVERSATION --around MESSAGE`
pivot. Structured output repeats the resolved target identity on every row so
JSONL records remain self-contained.

Conversation targets average up to 32 recent embedded human turns after
removing known injected agent scaffolding. If no usable human turn exists, the
command falls back to other real turns. Message targets use one exact stored
embedding and are preferable when a long conversation covers several topics.
Candidates exclude the target conversation and superseded message history.
Only the strongest turn per conversation is returned, and exact duplicate
content is represented once across copied or continued archives.

## Multi-hop trails

`trail` performs repeated related-turn pivots as a bounded breadth-first walk.
Every edge stores its parent conversation, child conversation, similarity, and
the exact child message that established the relation. The next hop uses that
message rather than averaging the entire newly discovered conversation, so the
path follows the topic that actually connected the two sessions.

Defaults are two hops, three children per visited node, 20 total nodes, and a
minimum similarity of `0.65`. Hard limits are three hops, width eight, and 100
nodes. A conversation and a byte-identical evidence turn can each appear only
once in a trail, preventing cycles and copied sessions from consuming the
budget. Provider, age, and role filters apply at every hop.

Text output includes explicit parent and child prefixes plus exact `read
--around` pivots. JSON returns `{root, nodes, edges}`. JSONL emits one root
record followed by self-contained edge records with their child node. DOT emits
the identical graph without creating a file; visual rendering requires a
separately installed Graphviz `dot` command.

Explore reads the existing DuckDB projection without modifying it. It downloads
no model, invokes no generator, sends no archive text to a service, and does not
silently embed missing records. Run `convos embed` when the selected target is
not embedded. Hybrid indexing and its `doctor` backlog also exclude the same
known scaffolding instead of spending compute and storage on it.

Semantic proximity is navigation evidence, not an identity or truth claim.
Scores are model-relative and should not be compared across embedding model
changes. Each hop can drift even when every edge is individually close; use an
exact message target, a higher `--min-score`, or a shallower trail when the
topic boundary matters. Missing embeddings cannot participate, and paraphrased
duplicate sessions may still appear separately because only byte-identical
turn content is collapsed.
