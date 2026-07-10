# ai-convos-changegraph

Change graph over the convos DB ([spec 02](../../docs/specs/02-change-graph.md)):
*which conversation -- which prompt -- changed this file, or this line?*

Install it beside the core tool from GitHub:

```bash
uv tool install --reinstall "git+https://github.com/RobertBiehl/ai-convos-db.git" \
  --with "ai-convos-changegraph @ git+https://github.com/RobertBiehl/ai-convos-db.git#subdirectory=apps/changegraph"
```

It registers through the `convos.commands` entry-point group:

```bash
convos blame <file> [--line N] [--at <conv|ts>]   # per-line attribution
convos timeline <file>                            # chronological cross-provider edit history
convos at <file> <conv|ts>                        # reconstruct file content at that point
convos graph [<file>|<conv>] [-f json|dot]        # file <-> conversation edge set
convos browse                                     # curses TUI over the graph (below)
```

`browse` walks the graph interactively: **files** (ranked by last AI edit) -> a file's
**timeline** (cross-provider edits with the triggering prompt, flagged exact/unknown) ->
**edit detail** (full prompt + colored -/+ before/after diff). `c` pivots from any edit to
its conversation's file list, traversing the file <-> conversation edge in the other
direction. `/` filters, esc goes back. Orphaned edits (transcript deleted before ingest)
appear labeled `unknown` rather than being hidden.

Exactness boundary: attribution is computed by replaying captured `old_content` edits;
shell edits or missing/unmatched before-state make content *unknown* (never invented)
until the next full write. Reads the DB read-only; no core schema changes.
