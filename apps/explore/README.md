# ai-convos-explore

Local semantic navigation for an `ai-convos-db` archive:

```bash
convos related CONVERSATION_OR_MESSAGE_ID
convos trail CONVERSATION_OR_MESSAGE_ID
convos map CONVERSATION_OR_MESSAGE_ID
```

Install it with the core product:

```bash
uv tool install "ai-convos-db[explore]"
```

A conversation target uses up to 32 recent embedded human turns as its
semantic fingerprint. A message target uses that exact turn, which is better
for long or multi-topic conversations. Results identify the strongest matching
turn in every neighboring conversation and include an exact `convos read
--around` follow-up.

Everything remains local and read-only. The command uses embeddings already in
the archive, runs no generation model, and makes no network request. Known
agent-injected scaffolding is excluded from seeds and candidates, and exact
duplicate turn bodies are collapsed so copied or continued sessions do not
crowd out distinct results. Run `convos embed` if the selected target has no
embedding yet.

`trail` repeats that exact-evidence pivot as a bounded breadth-first walk. Its
default two-hop, three-wide, 20-node traversal accepts only edges with at least
`0.65` similarity, never revisits a conversation or exact turn, and carries the
message that proves every edge. Text shows parent and child IDs; JSON retains
nodes and edges; JSONL streams a self-contained root and edges; DOT emits the
same graph for an optional, separately installed Graphviz renderer. Depth,
width, score, and total nodes have hard CLI bounds so an exploratory command
cannot accidentally scan without limit.

`map` opens the same trail as a layered, clickable local HTML artifact with an
exact evidence panel and copyable `read --around` command. It has no server,
CDN, or JavaScript dependency. Archive data is carried as base64 JSON under a
restrictive content-security policy and enters the page only through
`textContent`. The mode-`0600` artifact defaults to a unique temporary path;
`--output PATH` refuses overwrite and symlink targets, while `--no-open`
creates it without launching the browser.

See the [complete behavior and limitations](../../docs/explore.md).
